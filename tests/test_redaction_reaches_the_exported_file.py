"""A redaction the parent process never saw, exported as if it had (#322).

Under the processes executor -- the default on any build with a GIL, and
what `ISOCENTER_FORCE_PROCESSES=1` pins everywhere else --
`RedactionService` redacts a *pickled copy* of the instance. The copy's
zeroed frame is written to the sidecar and a `SidecarPixelLoader` for it
comes back in the mutation; `Session._apply_redaction_outcomes` rebinds
that loader onto the parent's instance. What it never touched was
`instance.pixel_array`, so a parent holding a resident array kept the
**pre-redaction** pixels in memory beside a loader pointing at the
redacted ones -- two answers to "what are this instance's pixels", and
every consumer that asks the instance rather than the loader gets the
wrong one.

The suite could not see it because `tests/conftest.py`'s
`reloaded_redaction_session` ends with `inst.unload_pixel_data()`: the
fixture drops the resident array, which is precisely the state the bug
needs. Every redaction test inherited that mitigation. These tests undo
it -- one line, `inst.get_pixel_data()` -- which is what a caller who
previewed the image, ran OCR over it, or replaced it with
`set_pixel_data()` is holding when they call `redact()`.
"""
import numpy as np
import pydicom
import pytest

from isocenter.session import DicomSession

#: The zone `test_export_redaction_hash_warning.py` uses, for the same
#: reason: it lands inside the fixture's 32x32 image, so the redaction is
#: applied rather than skipped.
IN_IMAGE_ZONE = [0, 8, 0, 8]

#: The fixture's fill. Every pixel is this before redaction and 0 inside
#: the zone after it, so one pixel read distinguishes the two frames.
FILL = 200


def _resident_pre_redaction_array(inst):
    """Undo the fixture's trailing `unload_pixel_data()`.

    That line is the mitigation this bug hides behind. A caller who
    previewed the image, ran OCR, or called `set_pixel_data()` has a
    resident array at this point, and so must these tests.
    """
    arr = inst.get_pixel_data()
    assert arr is not None and int(arr[0, 0]) == FILL, (
        "fixture drift: the instance no longer starts with the fill this "
        "file distinguishes the two frames by")
    return arr


def test_a_processes_path_redaction_exports_the_redacted_pixels(
        reloaded_redaction_session, tmp_path, monkeypatch):
    """The deliverable: wrong pixels in a file, under a redaction attestation.

    Not "a flag is set" -- an exported `.dcm` whose Pixel Data is the
    pre-redaction frame while the same file carries
    `_ISOCENTER_REDACTION_HASH`, Image Type `DERIVED`, Burned In
    Annotation `NO` and a Derivation Code Sequence saying the pixels were
    modified. Every attestation the redaction writes, over the pixels it
    did not remove.

    **The export runs from a second session, and that is not decoration.**
    `_export_instance_worker` re-applies `ctx.redaction_zones`
    (io_handlers.py, "APPLY REDACTION (Fix for Export Compression Bug)"),
    and those zones come from `self.configuration.rules` -- so exporting
    from the *same* session that just redacted zeroes the region a second
    time and hands back a clean file over corrupt pixels. That second
    pass is why the plan's single-session version of this test passed
    against the unfixed code: it is not the redaction being verified, it
    is a different redaction being applied to the wrong array. The moment
    the export runs anywhere the rule does not follow -- a reopened
    store, a delivery session, a config edited between the two steps --
    the cover is gone and the stale frame is what ships. Do not
    "simplify" this back into one session.

    `use_compression=False` is load-bearing: the JPEG2000 path needs a
    codec this suite cannot assume, and a skip here is a silent pass on
    the most important test in the change.

    The `assert written` line is equally load-bearing. `session.export()`
    logs and writes nothing when an instance fails module validation, so
    a fixture that drifted into being unexportable leaves an empty tree
    and `sorted(...rglob(...))` iterates nothing -- the vacuous pass this
    file exists to make impossible.
    """
    monkeypatch.setenv("ISOCENTER_FORCE_PROCESSES", "1")
    session, inst = reloaded_redaction_session(
        [IN_IMAGE_ZONE], fill=FILL, name="exported")
    _resident_pre_redaction_array(inst)

    assert session.redact(show_progress=False) == 1
    # The documented order -- redact, save, export. Synchronous so the
    # save that reads this instance's pixels is ordered against the
    # export rather than racing `release_memory()` inside it.
    session.save(sync=True)
    session.close()

    delivery = DicomSession(str(tmp_path / "exported.db"))
    out = tmp_path / "out"
    try:
        delivery.export(str(out), use_compression=False, show_progress=False)
    finally:
        delivery.close()

    written = sorted(out.rglob("*.dcm"))
    assert written, "the export produced no files; the fixture went vacuous"
    ds = pydicom.dcmread(str(written[0]))
    assert "DERIVED" in list(ds.ImageType), (
        "fixture drift: the exported file no longer carries the redaction "
        "attestation this test is about")
    assert int(ds.pixel_array[0, 0]) == 0, (
        "the exported file carries pre-redaction pixels under a redaction "
        "attestation")


def test_a_same_session_export_re_redacts_and_so_hides_this_entirely(
        reloaded_redaction_session, tmp_path, monkeypatch):
    """Why the test above must stay cross-session, as a test and not prose.

    `_export_instance_worker` applies `ctx.redaction_zones` to whatever
    array it is handed, and those zones are resolved from
    `session.configuration.rules`. So an export run from the session that
    just redacted zeroes the region a *second* time -- over the stale
    array -- and produces a clean file whatever state the instance is in.
    That is not the redaction being verified; it is a different redaction
    covering for it.

    **This passes on both sides of the fix**, deliberately. It is the
    guard that stops the deliverable above being "simplified" back into
    one session, where it would go green against the unfixed code and
    measure nothing. If this ever goes red, the export's second pass has
    changed and the deliverable's construction has to be revisited with
    it.
    """
    monkeypatch.setenv("ISOCENTER_FORCE_PROCESSES", "1")
    session, inst = reloaded_redaction_session(
        [IN_IMAGE_ZONE], fill=FILL, name="samesession")
    _resident_pre_redaction_array(inst)

    assert session.redact(show_progress=False) == 1
    session.save(sync=True)

    out = tmp_path / "same"
    session.export(str(out), use_compression=False, show_progress=False)

    written = sorted(out.rglob("*.dcm"))
    assert written, "the export produced no files; the fixture went vacuous"
    assert int(pydicom.dcmread(str(written[0])).pixel_array[0, 0]) == 0, (
        "the export no longer re-applies the session's zones, so the "
        "cross-session construction above needs revisiting")


def test_the_parent_instance_holds_no_pre_redaction_array_after_redact(
        reloaded_redaction_session, monkeypatch):
    """The fast pin: nothing is left in memory that disagrees with the loader.

    Needed beside the export test rather than instead of it. This one
    alone would pass against a fix that nulled the array in the wrong
    place -- outside the pixel-swap lock, or on the `pixel_hash`-only
    mutation that carries no loader to reload from -- and the export
    test alone would pass against a read-time patch that left the
    divergence in place.

    Both executors leave identical state here. On the threads path the
    worker *is* this object, so its own `set_pixel_data()` already
    replaced the array; the null is a no-op rather than a second
    behaviour.
    """
    monkeypatch.setenv("ISOCENTER_FORCE_PROCESSES", "1")
    session, inst = reloaded_redaction_session([IN_IMAGE_ZONE], fill=FILL)
    _resident_pre_redaction_array(inst)

    assert session.redact(show_progress=False) == 1

    assert inst.pixel_array is None, (
        "the instance still holds the array the worker redacted a copy "
        "of; it will serve those pixels to anything that asks")
    assert int(inst.get_pixel_data()[0, 0]) == 0, (
        "reading through the rebound loader must give the redacted frame")


def test_the_redacted_frame_is_what_a_reopened_store_holds(
        reloaded_redaction_session, tmp_path, monkeypatch):
    """The durable arm: the corruption reached the sidecar, not just a read.

    `_persist_pixels` hashes the *resident* array. Its dedup guard is
    `inst._pixel_hash == digest`, and after a processes-path redaction
    `_pixel_hash` is the redacted digest while the resident array is the
    stale one -- so the guard misses, the stale frame is appended to the
    sidecar and the loader is rewired to it. A read-time-only fix leaves
    that on disk, and this test is what says so.
    """
    monkeypatch.setenv("ISOCENTER_FORCE_PROCESSES", "1")
    session, inst = reloaded_redaction_session(
        [IN_IMAGE_ZONE], fill=FILL, name="durable")
    _resident_pre_redaction_array(inst)

    assert session.redact(show_progress=False) == 1
    uid = inst.sop_instance_uid
    session.save(sync=True)
    session.close()

    reopened = DicomSession(str(tmp_path / "durable.db"))
    try:
        stored = next(i
                      for p in reopened.store.patients
                      for st in p.studies
                      for se in st.series
                      for i in se.instances
                      if i.sop_instance_uid == uid)
        assert int(stored.get_pixel_data()[0, 0]) == 0, (
            "the store holds the pre-redaction frame; every later session "
            "over it reads and exports unredacted pixels")
    finally:
        reopened.close()


def test_a_replaced_array_redacted_without_a_save_stays_freeable(
        reloaded_redaction_session, monkeypatch):
    """The state `session.py`'s deleted flag-clearing line was written for.

    `set_pixel_data()` sets `_pixel_array_unwritten`, and that line
    cleared it so `release_memory()` would not refuse the instance for
    the rest of the session. Its own comment called the sequence
    "REACHABLE BUT UNTESTED ... the test is filed, not written here" --
    so the line is deleted here on the argument that nulling the array
    makes the flag unreachable, and this is the test that argument owes:
    `unload_pixel_data()` returns True on a `None` array *before* it
    consults the flag, and `get_pixel_data()`'s loader arm clears it.

    Construct it exactly: replace the pixels, redact with no save in
    between, then sweep and read back.

    **This test passes on both sides of the fix**, and says so rather
    than being counted as evidence: before it, the deleted line clears
    the flag; after it, the null makes the flag unreachable. It is the
    guard the deletion owes, not a demonstration of the defect.
    """
    monkeypatch.setenv("ISOCENTER_FORCE_PROCESSES", "1")
    session, inst = reloaded_redaction_session(
        [IN_IMAGE_ZONE], fill=FILL, name="unwritten")

    inst.set_pixel_data(np.full((32, 32), 77, dtype=np.uint8))
    assert inst._pixel_array_unwritten is True, (
        "fixture drift: set_pixel_data no longer records the divergence "
        "this test is about")

    assert session.redact(show_progress=False) == 1

    session.release_memory()
    assert inst.pixel_array is None, (
        "release_memory() refused this instance and only logged a count; "
        "it is unfreeable for the rest of the session (#293)")
    assert int(inst.get_pixel_data()[0, 0]) == 0, (
        "the replaced pixels were redacted, so the frame read back must "
        "be the redacted one")


@pytest.mark.parametrize("force", ["ISOCENTER_FORCE_PROCESSES",
                                   "ISOCENTER_FORCE_THREADS"])
def test_both_executors_leave_the_same_pixel_state(
        reloaded_redaction_session, monkeypatch, force):
    """Convergence, which is the shape #228 asked for.

    The parent's view of an instance must not depend on which executor
    ran the pass. Under threads the worker mutated this very object and
    `execute_redaction_task`'s `finally` already dropped the array from
    it, so the threads case is the *correct* side and passes on both
    sides of the fix -- it is the reference the processes case is graded
    against, and only the processes case is evidence of the defect.
    """
    monkeypatch.setenv(force, "1")
    other = ("ISOCENTER_FORCE_THREADS"
             if force == "ISOCENTER_FORCE_PROCESSES"
             else "ISOCENTER_FORCE_PROCESSES")
    monkeypatch.delenv(other, raising=False)

    session, inst = reloaded_redaction_session([IN_IMAGE_ZONE], fill=FILL)
    _resident_pre_redaction_array(inst)

    assert session.redact(show_progress=False) == 1

    assert inst.pixel_array is None
    assert int(inst.get_pixel_data()[0, 0]) == 0
