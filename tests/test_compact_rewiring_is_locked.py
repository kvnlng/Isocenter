"""Compaction's offset rewiring runs under the lock that guards it (#295).

`SqliteStore._pixel_swap_lock` exists so that an instance's pixel loader
and its hash are never read half-swapped: #274 was a redaction that
rebound one and not the other, after which the instance read back
unredacted pixels under a full redaction attestation, self-consistently,
because the stale hash matched the stale frame.

`compact()`'s rewiring loop rebinds `offset` and `length` on every
sidecar loader in the graph and did so **outside** that lock. Two field
assignments per instance is a torn read a reader can land inside: a
loader on a new offset with an old length reads the wrong bytes, or runs
off the end of the file. The loop was safe only by the convention
written into `compact()`'s docstring -- "nothing else may be writing
pixel state while this runs" -- which nothing enforced.

The rewiring is now `Session._rewire_sidecar_loaders`, and the tests
below call it directly rather than through `compact()`. That is
deliberate: `compact()` leads with `save(sync=True)`, whose own pixel
prepass also takes `_pixel_swap_lock`, so a front-door test could not
tell the loop's acquisition from the save's.
"""
import threading

import numpy as np
import pytest

from isocenter.entities import Instance, Patient, Series, Study
from isocenter.session import DicomSession


@pytest.fixture
def session(tmp_path):
    s = DicomSession(persistence_file=str(tmp_path / "rewire.db"))
    yield s
    s.close()


def _one_pixel_instance(session, uid="1.1.1"):
    """A persisted instance carrying a real `SidecarPixelLoader`."""
    inst = Instance(uid, "1.2.840.10008.5.1.4.1.1.2", 1, file_path=None)
    inst.set_pixel_data(
        np.random.randint(0, 255, (16, 16), dtype=np.uint8))
    session.store_backend.persist_pixel_data(inst)

    patient = Patient("P1", "Test^Patient")
    study = Study("ST1", "20230101")
    series = Series("SE1", "CT", 1)
    patient.studies.append(study)
    study.series.append(series)
    series.instances.append(inst)
    session.store.patients.append(patient)
    session.save(sync=True)
    return inst


def _on_helper(fn):
    """Run `fn` on a bounded daemon, keeping whatever it raised.

    The error is kept rather than swallowed: a helper that dies of an
    `AttributeError` sets its Event just as promptly as one that
    finished, and a negative timing assertion cannot tell the two apart.
    """
    done = threading.Event()
    errors = []

    def run():
        try:
            fn()
        except BaseException as exc:  # pylint: disable=broad-except
            errors.append(exc)
        finally:
            done.set()

    threading.Thread(target=run, daemon=True).start()
    return done, errors


def test_the_rebind_waits_for_the_pixel_swap_lock(session):
    """The loop must not rebind a loader another writer is reading (#295)."""
    inst = _one_pixel_instance(session)
    assert inst._pixel_loader is not None, (
        "the instance has no sidecar loader, so there is nothing for the "
        "rewiring to rebind and this test would pass vacuously")

    holder_has_it = threading.Event()
    let_go = threading.Event()

    def hold_the_lock():
        with session.store_backend._pixel_swap_lock:
            holder_has_it.set()
            let_go.wait(20)

    threading.Thread(target=hold_the_lock, daemon=True).start()
    assert holder_has_it.wait(5), "the helper never took _pixel_swap_lock"

    done, errors = _on_helper(
        lambda: session._rewire_sidecar_loaders(
            {inst.sop_instance_uid: (999, 8)}, {}))

    assert not done.wait(0.5), (
        f"(helper raised: {errors}) " if errors else "") + (
        "the rewiring rebound a loader while another writer held "
        "_pixel_swap_lock: offset and length are two assignments, and a "
        "reader landing between them gets the wrong bytes or runs off "
        "the end of the sidecar (#295)")

    let_go.set()
    assert not errors, f"the rewiring raised: {errors[0]!r}"
    assert done.wait(15), (
        "the rewiring never finished after the lock was released")
    assert (inst._pixel_loader.offset, inst._pixel_loader.length) == (999, 8), (
        "the rewiring took the lock but did not rebind the loader")


def test_compact_refuses_while_a_save_is_in_flight(session, monkeypatch):
    """The precondition is checked, not merely documented (#295).

    Placement is load-bearing and is asserted here: the refusal happens
    **after** `save(sync=True)` and **before** `compact_sidecar()`.
    Refusing after the rewrite would leave the file compacted and every
    in-memory loader on a pre-compaction offset -- worse than not
    checking at all.

    **What this cannot use is a genuinely queued save.** Since #294
    `compact()`'s opening `save(sync=True)` drains the manager, so an
    item put on the queue beforehand is gone before the check runs. The
    guard is therefore exercised at its wiring: `has_pending_saves()` is
    forced True, which is the state a producer queueing a save *after*
    that flush returns actually reaches. That residual race is not
    closed by this change and is not claimed to be.
    """
    _one_pixel_instance(session)

    called = []
    monkeypatch.setattr(
        session.store_backend, "compact_sidecar",
        lambda: called.append("compact_sidecar") or {})
    monkeypatch.setattr(
        session.persistence_manager, "has_pending_saves", lambda: True)

    with pytest.raises(RuntimeError, match="(?i)pending save"):
        session.compact()

    assert called == [], (
        "compact_sidecar() ran before the precondition was checked: the "
        "sidecar is rewritten and every in-memory loader is left on a "
        "pre-compaction offset (#295)")


def test_compact_still_patches_every_loader(session):
    """The end-to-end guarantee the lock must not trade away (#295).

    **Passes on unfixed code, deliberately.** A rewiring that took the
    lock and stopped rebinding would satisfy the first test and destroy
    this one.
    """
    inst = _one_pixel_instance(session)
    expected = inst.get_pixel_data().copy()

    session.compact()

    inst.unload_pixel_data()
    assert np.array_equal(inst.get_pixel_data(), expected), (
        "an instance read the wrong bytes back after compaction: its "
        "loader was left on a pre-compaction offset")
