"""`unload_pixel_data()` must keep the promise its docstring makes (#293).

Its contract is "clear the resident array only when it can be brought
back". The check was `file_path or _pixel_loader`, which asks whether
there is *a* way to load *a* frame -- not whether that frame is the one
being dropped.

`set_pixel_data()` replaces `pixel_array` and deliberately leaves
`_pixel_loader` alone. So after a save has bound a loader, replacing the
pixels leaves the guard passing while the resident array and the stored
frame have diverged. Unloading then discards the new pixels; the next
`save_all` takes `_persist_pixels`' `arr is None` arm, which re-records
the loader's own offset, length and hash -- the *old* frame -- and marks
the instance persisted. Store, sidecar, memory and `_pixel_hash` all
agree afterwards, so every integrity check passes and nothing anywhere
says the replacement was lost.

The fix is a divergence flag plus a refusal, and a second, explicitly
named spelling (`discard_pixel_data()`) for the callers that mean "drop
this, I know it is unsaved". Two behaviours, two names -- not an alias,
so "one spelling per behaviour" is untouched.

The two tests that matter most are the last two rather than the first.
A refusal is easy to make too wide, and `release_memory()` reports only
counts, so an over-broad refusal turns the one operation this library
offers for reclaiming RAM into a silent no-op on a 100GB dataset. The
first test pins that the refusal exists; the over-refusal tests pin that
it is narrow, on the path where a missed clear-site would actually bite.
"""
import logging

import numpy as np
import pytest

from isocenter.entities import Instance, Patient, Series, Study
from isocenter.persistence import SqliteStore, SidecarPixelLoader
from isocenter.session import DicomSession

from tests.test_pixel_geometry_pipeline import write_source


def _graph(uid, arr, pid="P1"):
    inst = Instance(uid, "1.2.840.10008.5.1.4.1.1.7", 1)
    inst.set_pixel_data(arr)
    pat = Patient(pid, "Test Patient")
    st = Study("ST-" + pid, "20230101")
    se = Series("SE-" + pid, "OT", 1)
    pat.studies.append(st)
    st.series.append(se)
    se.instances.append(inst)
    return pat, inst


def _saved(tmp_path, name, arr):
    """A store holding one saved instance, with its loader bound."""
    store = SqliteStore(str(tmp_path / name))
    pat, inst = _graph("div.1", arr)
    store.save_all([pat])
    assert isinstance(inst._pixel_loader, SidecarPixelLoader), (
        "setup: the save should have bound a sidecar loader")
    return store, pat, inst


def test_unloading_a_replaced_array_is_refused(tmp_path):
    """The array that has never been written anywhere must not be dropped.

    `set_pixel_data()` after a save is the exact state the old guard
    could not see: `_pixel_loader` is still there, so the check passed,
    and the loader points at the frame the replacement superseded.
    """
    store, _, inst = _saved(tmp_path, "refuse.db",
                            np.full((8, 8), 3, dtype=np.uint8))
    try:
        replacement = np.full((8, 8), 7, dtype=np.uint8)
        inst.set_pixel_data(replacement)

        assert inst.unload_pixel_data() is False, (
            "unload_pixel_data() cleared an array that had been replaced "
            "since the last save, so the only copy of those pixels is "
            "gone and the loader still points at the frame they replaced")
        np.testing.assert_array_equal(inst.pixel_array, replacement)
    finally:
        store.stop()


def test_a_replaced_and_unloaded_array_does_not_vanish_from_the_store(
        tmp_path):
    """The loss itself, end to end -- not just the guard.

    Before the fix: the unload succeeds, the save takes
    `_persist_pixels`' `arr is None` arm and re-records the loader's own
    offset/length/hash, and the instance is marked persisted. A reopened
    session reads the OLD frame, `_pixel_hash` matches it, and nothing
    reports a loss. Nothing else in the suite constructs this state.

    The assertion is deliberately a disjunction. "The new pixels are in
    the store" and "the instance still says it has them to save" are both
    acceptable outcomes; what is not acceptable is a store holding the
    old frame under an instance that reports itself saved.
    """
    original = np.full((8, 8), 3, dtype=np.uint8)
    replacement = np.full((8, 8), 7, dtype=np.uint8)
    db = str(tmp_path / "loss.db")

    store = SqliteStore(db)
    pat, inst = _graph("div.1", original)
    store.save_all([pat])

    inst.set_pixel_data(replacement)
    inst.unload_pixel_data()
    store.save_all([pat])
    still_dirty = inst.has_unsaved_changes
    store.stop()

    reopened = SqliteStore(db)
    try:
        hydrated = (reopened.load_patient("P1")
                    .studies[0].series[0].instances[0])
        stored = hydrated.get_pixel_data()
        assert np.array_equal(stored, replacement) or still_dirty, (
            "the replacement pixels are gone: the store holds the frame "
            "they replaced and the instance reported nothing left to "
            "save, so no integrity check and no report can ever notice")
    finally:
        reopened.stop()


def test_release_memory_still_frees_a_saved_instance(tmp_path):
    """The refusal must be narrow, or memory release silently stops.

    `release_memory()` only logs counts, so an over-broad refusal is
    invisible: the sweep runs, reports nothing freed, and a 100GB session
    holds every frame it ever read. This is the guard on the fix, not on
    the defect.
    """
    src = tmp_path / "in"
    src.mkdir()
    arr = np.arange(4 * 4, dtype=np.uint16).reshape((4, 4))
    write_source(src / "a.dcm", arr, sop_uid="1.2.900.1")
    write_source(src / "b.dcm", arr, sop_uid="1.2.900.2", instance_num=2)

    with DicomSession(str(tmp_path / "free.db")) as session:
        session.ingest(str(src))
        session.save(sync=True)
        instances = [i for p in session.store.patients
                     for st in p.studies for se in st.series
                     for i in se.instances]
        assert len(instances) == 2, "setup: expected two ingested instances"
        for inst in instances:
            assert inst.get_pixel_data() is not None

        with caplog_at_info() as records:
            session.release_memory()

        for inst in instances:
            assert inst.pixel_array is None, (
                "release_memory() left a saved, unmodified frame resident "
                "-- the refusal added for #293 is too wide and memory "
                "release has silently become a no-op")
        assert "0/2" not in " ".join(records), records


def test_release_memory_frees_an_instance_whose_pixels_were_replaced_and_saved(
        tmp_path):
    """The path the persistence clear-sites are on.

    `get_pixel_data()` clears the divergence flag itself, so a
    read-then-release sweep never reaches the clear-sites in
    `persistence.py`. Only replace-then-save does. A missed clear there
    makes every replaced-and-saved instance permanently unfreeable, and
    nothing goes red -- which is the same silent no-op the test above
    guards, reached through the door that fix actually opened.
    """
    src = tmp_path / "in"
    src.mkdir()
    arr = np.arange(4 * 4, dtype=np.uint16).reshape((4, 4))
    write_source(src / "a.dcm", arr, sop_uid="1.2.901.1")

    with DicomSession(str(tmp_path / "free2.db")) as session:
        session.ingest(str(src))
        session.save(sync=True)
        inst = session.store.patients[0].studies[0].series[0].instances[0]

        replacement = np.full((4, 4), 5, dtype=np.uint16)
        inst.set_pixel_data(replacement)
        session.save(sync=True)

        session.release_memory()

        assert inst.pixel_array is None, (
            "a replaced array that has since been written to the sidecar "
            "is recoverable and must be freeable; a clear-site in "
            "persistence.py was missed (#293)")
        np.testing.assert_array_equal(inst.get_pixel_data(), replacement)


def test_discard_pixel_data_still_refuses_an_instance_with_nowhere_to_reload(
        tmp_path):
    """`discard_pixel_data()` is today's unload, exactly -- refusal included.

    The redaction `finally` blocks moved to it, and the behaviour they
    depend on at `isocenter/services.py` is the *refusal* for an instance
    with neither a loader nor a `file_path`: a partial redaction is kept
    resident rather than dropped, because zeroing is monotone and a
    partial redaction has removed more PHI than none. Widening
    `discard_pixel_data()` to an unconditional clear would silently
    reverse that.
    """
    inst = Instance("mem.only", "1.2.3", 1)
    inst.set_pixel_data(np.zeros((4, 4), dtype=np.uint16))
    assert inst.file_path is None and inst._pixel_loader is None

    assert inst.discard_pixel_data() is False
    assert inst.pixel_array is not None


def test_discard_pixel_data_drops_a_replaced_array_on_purpose(tmp_path):
    """The second spelling has to actually differ from the first.

    If `discard_pixel_data()` also refused a diverged array, the two
    redaction `finally` blocks would stop dropping the partially-zeroed
    array they exist to drop, and a failed redaction would leave the
    mutation resident for the next save to publish.
    """
    store, _, inst = _saved(tmp_path, "discard.db",
                            np.full((8, 8), 3, dtype=np.uint8))
    try:
        inst.set_pixel_data(np.full((8, 8), 7, dtype=np.uint8))

        assert inst.discard_pixel_data() is True
        assert inst.pixel_array is None
    finally:
        store.stop()


class caplog_at_info:
    """Collect INFO records without needing pytest's caplog fixture here."""

    def __enter__(self):
        self.records = []
        self.handler = _ListHandler(self.records)
        self.logger = logging.getLogger("isocenter")
        self.previous = self.logger.level
        self.logger.setLevel(logging.INFO)
        self.logger.addHandler(self.handler)
        return self.records

    def __exit__(self, *exc):
        self.logger.removeHandler(self.handler)
        self.logger.setLevel(self.previous)
        return False


class _ListHandler(logging.Handler):
    def __init__(self, sink):
        super().__init__()
        self.sink = sink

    def emit(self, record):
        self.sink.append(record.getMessage())


# --- the clear-sites, pinned one at a time -------------------------------
#
# The refusal is only half the fix. Every place that *writes* the resident
# array has to clear the flag, or the instance becomes permanently
# unfreeable and `release_memory()` turns into a silent no-op -- it logs
# counts and nothing else. Each test below dies if one specific clear-site
# is removed; the sweep that produced them found that only the
# `_persist_pixels` rebind was killed by the tests above.


def test_a_reloaded_array_is_freeable_again_through_the_loader(tmp_path):
    """`get_pixel_data()`'s loader arm must clear the flag.

    Reached by the redaction path: a swap is discarded through
    `discard_pixel_data()`, and the next read pulls the stored frame back
    through the sidecar loader. That array IS what is stored, so refusing
    to free it again would be wrong -- and permanent.
    """
    store, _, inst = _saved(tmp_path, "reload.db",
                            np.full((8, 8), 3, dtype=np.uint8))
    try:
        inst.set_pixel_data(np.full((8, 8), 7, dtype=np.uint8))
        assert inst.discard_pixel_data() is True

        assert inst.get_pixel_data() is not None
        assert inst.unload_pixel_data() is True, (
            "an array just read back from the sidecar cannot have "
            "diverged from it, but unloading was refused -- the "
            "clear-site in get_pixel_data()'s loader arm is missing")
        assert inst.pixel_array is None
    finally:
        store.stop()


def test_a_reloaded_array_is_freeable_again_through_the_file(tmp_path):
    """`get_pixel_data()`'s pydicom arm must clear the flag too.

    Same claim on the other read path. The loader is removed so the read
    falls through to `file_path`, which is the arm an ingested instance
    takes once its sidecar reference is gone.
    """
    src = tmp_path / "in"
    src.mkdir()
    arr = np.arange(16, dtype=np.uint16).reshape((4, 4))
    write_source(src / "a.dcm", arr, sop_uid="1.2.902.1")

    with DicomSession(str(tmp_path / "file.db")) as session:
        session.ingest(str(src))
        inst = session.store.patients[0].studies[0].series[0].instances[0]
        assert inst.file_path, "setup: the ingested instance must be file-backed"
        inst._pixel_loader = None

        inst.set_pixel_data(np.full((4, 4), 5, dtype=np.uint16))
        assert inst.discard_pixel_data() is True

        assert inst.get_pixel_data() is not None
        assert inst.unload_pixel_data() is True, (
            "an array just read back from its source file cannot have "
            "diverged from it, but unloading was refused -- the "
            "clear-site in get_pixel_data()'s pydicom arm is missing")


def test_persisting_a_swapped_array_makes_it_freeable(tmp_path):
    """`persist_pixel_data()` writes the array, so it must clear the flag.

    This is the entry point the redaction pass uses to make a swap
    durable before dropping it. Anything that publishes the resident
    bytes ends the divergence; miss this one and a redacted instance is
    unfreeable for the rest of the session.
    """
    store, _, inst = _saved(tmp_path, "persist.db",
                            np.full((8, 8), 3, dtype=np.uint8))
    try:
        inst.set_pixel_data(np.full((8, 8), 7, dtype=np.uint8))
        store.persist_pixel_data(inst)

        assert inst.unload_pixel_data() is True, (
            "persist_pixel_data() wrote these bytes and re-pointed the "
            "loader at them, so the array is recoverable -- the "
            "clear-site there is missing")
    finally:
        store.stop()


def test_replacing_pixels_with_identical_bytes_leaves_them_freeable(tmp_path):
    """The de-duplication arm of `_persist_pixels` must clear the flag.

    Writing the same bytes twice appends nothing -- the loader already
    points at them. That early return skips the rebind below it, so it
    needs its own clear, and an instance re-set to bytes it already had
    would otherwise be pinned in memory forever.
    """
    original = np.full((8, 8), 3, dtype=np.uint8)
    store, pat, inst = _saved(tmp_path, "dedup.db", original)
    try:
        inst.set_pixel_data(original.copy())
        assert inst._pixel_array_unwritten is True, (
            "setup: set_pixel_data must record the divergence even when "
            "the bytes happen to match")
        store.save_all([pat])

        assert inst.unload_pixel_data() is True, (
            "these exact bytes are already in the sidecar and the loader "
            "already points at them, so the array is recoverable -- the "
            "clear-site in the de-duplication arm is missing")
    finally:
        store.stop()


def test_an_in_place_mutation_is_lost_by_release_memory(tmp_path):
    """The limit `release_memory()` now states, executable (#323).

    Characterization: green on both sides, because #323 changes prose and
    nothing else. Its job is the one no assertion on prose can do -- a
    docstring cannot be asserted on without pinning today's wording
    rather than its truth, and a later "fix" that changed this behaviour
    while leaving the docstring alone would make the prose false again
    with nothing red.

    **Reaching the writeable arm takes three steps, and the shape of the
    setup is a finding.** A frame that arrived from the sidecar or from
    pydicom is `np.frombuffer`-backed and **not writeable**, so the
    one-liner `unload_pixel_data()`'s docstring gives -- `arr =
    inst.get_pixel_data(); arr[...] = 0` -- raises on a freshly ingested
    instance rather than diverging. That is also why
    `RedactionService._redact_instance_pixels` has two arms: its
    not-writeable arm copies and calls `set_pixel_data()`, and only the
    array that leaves *that* arm is writeable. So the sequence here is
    the sequence in the code -- replace, save, then mutate in place --
    and it is the second redaction pass over an instance, not the first,
    that loses its work.

    The save between is load-bearing and asserted, not assumed:
    `_pixel_array_unwritten` must be **False** at the moment of the
    mutation, or this would be characterizing #293's refusal instead of
    the hole beside it.
    """
    src = tmp_path / "in"
    src.mkdir()
    original = np.arange(4 * 4, dtype=np.uint16).reshape((4, 4))
    write_source(src / "a.dcm", original, sop_uid="1.2.901.1")

    with DicomSession(str(tmp_path / "inplace.db")) as session:
        session.ingest(str(src))
        session.save(sync=True)
        inst = session.store.patients[0].studies[0].series[0].instances[0]

        assert not inst.get_pixel_data().flags.writeable, (
            "setup: a file-backed frame is expected to be read-only, "
            "which is why the writeable arm needs the replacement below")

        # The not-writeable arm of `_redact_instance_pixels`, in two
        # lines: copy, hand it over, and let a save write it.
        inst.set_pixel_data(inst.get_pixel_data().copy())
        session.save(sync=True)

        arr = inst.get_pixel_data()
        assert arr is inst.pixel_array, (
            "setup: get_pixel_data() handed back a copy, so an in-place "
            "mutation could not reach the instance and this test would "
            "prove nothing")
        assert arr.flags.writeable, (
            "setup: the replacement did not leave a writeable array, so "
            "the arm being characterized is unreachable from here")
        assert not inst._pixel_array_unwritten, (  # pylint: disable=protected-access
            "setup: the save did not clear the divergence flag, so this "
            "would characterize #293's refusal and not the hole next to "
            "it")

        # The writeable arm of `_redact_instance_pixels`: mutate in
        # place, never call `set_pixel_data`, nothing records it.
        arr[...] = 0

        session.release_memory()

        # The attribute is the only observable here: `release_memory()`
        # returns None and reports its counts to the log. Resident is
        # still the right thing to assert on -- the array was
        # materialized two statements ago, so the only way it survives
        # this call is the unload refusing it or never being reached.
        assert inst.pixel_array is None, (
            "release_memory() refused the in-place mutation, or never "
            "reached this instance; either way the frame is still "
            "resident and the limit #323 narrowed the docstring to "
            "state no longer holds")

        assert np.array_equal(inst.get_pixel_data(), original), (
            "the reloaded frame is not the pre-mutation one -- the "
            "in-place mutation survived, and release_memory()'s stated "
            "limit no longer holds")
