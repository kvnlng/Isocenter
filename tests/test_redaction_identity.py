"""A redacted instance takes its new identity on every interpreter.

`execute_redaction_task` calls `inst.regenerate_uid()` in the worker and
hands the result back as `mutation["sop_uid"]`. The parent read that key
only as a fallback lookup key and never assigned it, so the regeneration
landed **only when the worker was the parent object**:

* threads -- 3.14t's default -- the worker mutates the parent's own
  `Instance`, so the new UID, the new `0008,0018` and `file_path = None`
  are already in place;
* processes -- 3.12's default -- the child mutates a copy, and the parent
  kept the source's SOP Instance UID and its `file_path` while picking up
  `ImageType = DERIVED`, the Derivation Code Sequence and the redaction
  hash from the same mutation.

Two different pixel sets under one SOP Instance UID is exactly the
collision `regenerate_uid()`'s docstring says it exists to prevent, and
because both DICOM write paths name files by that UID (#78) the same
input produced **different exported filenames on the two gate
interpreters**. Every test here is parametrised over the executor lever
rather than split per interpreter: the divergence is decided by the
executor, not by the build, and was measured on both builds under both
levers (#228).

The `monkeypatch.setenv` lever is the one
`test_a_failed_instance_is_left_as_it_was_found` already uses and is
legitimate for the same reason: `_resolve_strategy` reads these variables
**in the parent** when it picks `redact()`'s executor.
"""
import sqlite3

import numpy as np
import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian

from isocenter.services import RedactionService

SC_STORAGE = "1.2.840.10008.5.1.4.1.1.7"

#: Both spellings, always. The defect is one executor disagreeing with
#: the other, so a single leg cannot state the property.
LEVERS = ["ISOCENTER_FORCE_THREADS", "ISOCENTER_FORCE_PROCESSES"]

#: Inside a 32x32 image, so `apply_redaction_to_array` reports a change
#: and the worker reaches `regenerate_uid()`.
ZONE = [0, 8, 0, 8]

#: Entirely past the edge of a 32x32 image. Every zone is skipped,
#: `modified` stays False, `regenerate_uid()` is never called -- and a
#: mutation still comes back, which is what makes this a real gate.
OFF_EDGE_ZONE = [100, 108, 100, 108]


def _write_stale_source(path, uid):
    """A real DICOM file for the instance to be pointed at, then detached.

    `reloaded_redaction_session` builds its instance with
    `file_path = None`, so a test that only asserts `file_path is None`
    after `redact()` asserts what the fixture already supplied and stays
    green against an implementation that never touches the attribute.
    An ingested instance carries a `file_path` and the store persists it,
    so pointing the reloaded instance at a real file is the ordinary
    shape rather than a contrivance.

    It does not disturb the fixture's read-only-array premise:
    `Instance.get_pixel_data` tries `_pixel_loader` **before**
    `file_path`, and the reopened instance has one.
    """
    ds = Dataset()
    ds.file_meta = FileMetaDataset()
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.file_meta.MediaStorageSOPClassUID = SC_STORAGE
    ds.file_meta.MediaStorageSOPInstanceUID = uid
    ds.SOPClassUID = SC_STORAGE
    ds.SOPInstanceUID = uid
    ds.Rows = 32
    ds.Columns = 32
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = 8
    ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    ds.PixelData = np.full((32, 32), 200, dtype=np.uint8).tobytes()
    ds.save_as(str(path), enforce_file_format=True)


# --- T8 ---------------------------------------------------------------------

@pytest.mark.parametrize("lever", LEVERS)
def test_a_redacted_instance_takes_its_new_identity(
        reloaded_redaction_session, tmp_path, monkeypatch, lever):
    """The parent's own object carries the regenerated identity.

    All three assignments or none: a UID with a stale `0008,0018` is an
    instance whose object property and whose DICOM attribute disagree
    about what it is, and a `file_path` left pointing at the unredacted
    source after a successful redaction is half of what #228 is.

    On `dddb659` this is **red on the processes leg and green on the
    threads leg, on both gate interpreters**. That asymmetry is the
    defect, which is why the two legs are one parametrised test rather
    than one case per interpreter.
    """
    monkeypatch.setenv(lever, "1")
    session, inst = reloaded_redaction_session([ZONE], name=f"identity_{lever}")
    source_uid = inst.sop_instance_uid

    stale = tmp_path / f"stale_{lever}.dcm"
    _write_stale_source(stale, source_uid)
    inst.file_path = str(stale)
    assert inst.file_path is not None, (
        "the guard on the guard: with no file path to begin with, the "
        "`file_path is None` assertion below is the fixture's value and "
        "not the code's")

    session.redact(show_progress=False)

    assert inst.sop_instance_uid != source_uid, (
        "the redacted instance kept the SOP Instance UID of the data it "
        "was derived from")
    assert inst.attributes["0008,0018"] == inst.sop_instance_uid, (
        "the object property and the DICOM attribute disagree about the "
        "instance's identity")
    assert inst.file_path is None, (
        "the instance still points at the unredacted source file")


# --- T9 ---------------------------------------------------------------------

@pytest.mark.parametrize("lever", LEVERS)
def test_the_exported_filename_is_the_new_uid_under_either_executor(
        reloaded_redaction_session, tmp_path, monkeypatch, lever):
    """Same input, same exported filename, whichever executor redacted.

    This is the user-visible half of #228. Both DICOM write paths name
    files by the SOP Instance UID (#78), so on `dddb659` the processes
    leg writes `<source uid>.dcm` and the threads leg writes a generated
    one -- measured, and it is what turned two commits red on 3.14t.

    The lever acts on `redact()` only. `session.export()` pins
    `maxtasksperchild=25` (`session.py`), and `_use_threads` gives
    worker recycling the last word, so the export runs in processes on
    every interpreter and under either lever (#185).

    `len(files) == 1` comes first and is not decoration: `export()` logs
    and writes nothing for an instance that fails module validation, so a
    filename assertion over an empty tree would pass while asserting
    nothing. The fixture supplies the elements that keep this instance
    exportable.
    """
    monkeypatch.setenv(lever, "1")
    session, inst = reloaded_redaction_session([ZONE], name=f"exported_{lever}")
    source_uid = inst.sop_instance_uid

    session.redact(show_progress=False)

    out = tmp_path / f"out_{lever}"
    session.export(str(out), format="dicom")

    files = sorted(p for p in out.rglob("*.dcm"))
    assert len(files) == 1, (
        f"expected exactly one exported instance, got {files}; an empty "
        "tree means the instance failed validation and the name assertion "
        "below would be vacuous")
    assert files[0].name == f"{inst.sop_instance_uid}.dcm"
    assert files[0].name != f"{source_uid}.dcm", (
        "the export is named for the source instance it was derived from")


# --- T10 --------------------------------------------------------------------

@pytest.mark.parametrize("lever", LEVERS)
def test_an_instance_nothing_was_applied_to_keeps_its_identity(
        reloaded_redaction_session, tmp_path, monkeypatch, lever):
    """The gate is UID inequality, not "a mutation came back".

    `execute_redaction_task` builds its mutation dict **outside** the
    `if modified:` block, so an instance whose every zone starts past the
    edge of the image returns a mutation and has attributes written onto
    it, having had nothing applied to its pixels. Handing that instance a
    new identity would be a false attestation -- an unmodified image
    claiming to be a new, derived SOP Instance.

    Because `regenerate_uid()` is called only inside `if modified:`,
    `sop_uid != original_sop_uid` is true exactly when the pixels
    changed, and that is the gate `_apply_redaction_outcomes` uses.

    **Selectivity guard with respect to #228** -- green on `dddb659` on
    both legs and both interpreters, so it is not evidence the
    divergence is fixed. It is detection with respect to the gate: it
    goes red the moment that condition is written `if new_uid:` or
    `if mutation:`.

    The `applied` count and the null-valued attributes this run writes
    are a separate defect, deliberately deferred so that its fix and this
    gate can be decided as one gate (#235). Nothing here asserts on
    either, so this test does not pin them.
    """
    monkeypatch.setenv(lever, "1")
    session, inst = reloaded_redaction_session(
        [OFF_EDGE_ZONE], name=f"offedge_{lever}")
    source_uid = inst.sop_instance_uid

    stale = tmp_path / f"stale_offedge_{lever}.dcm"
    _write_stale_source(stale, source_uid)
    inst.file_path = str(stale)

    # Non-vacuity, asked without depending on #235's count: the rule does
    # target this instance, so a worker really did run and really did
    # return a mutation for it.
    service = RedactionService(session.store, session.store_backend)
    tasks = service.prepare_redaction_tasks(session.configuration.rules[0])
    assert len(tasks) == 1, (
        "the rule matched no instance, so this test would pass without "
        "the gate ever being reached")

    session.redact(show_progress=False)

    assert inst.sop_instance_uid == source_uid, (
        "an instance whose zones all missed was given a new identity")
    assert inst.attributes["0008,0018"] == source_uid
    assert inst.file_path == str(stale), (
        "an instance whose zones all missed was detached from its file")


# --- T11 --------------------------------------------------------------------

@pytest.mark.parametrize("lever", LEVERS)
def test_the_store_holds_one_instance_and_one_pixel_blob(
        reloaded_redaction_session, monkeypatch, lever):
    """Applying the identity in the parent leaves no orphan behind.

    The worker regenerates the UID *before* `persist_pixel_data`, so on
    the processes path the blob went into `instance_blobs` under a UID no
    instance in the parent's graph carried, and the parent's own
    `save()` then wrote the pixels a second time under the source UID.
    Measured on `dddb659`, processes leg, both interpreters: one
    `instances` row under the **source** UID and **two** `instance_blobs`
    rows, 46 referenced bytes against the threads leg's 23. `compact()`
    collects exactly such rows, so it was dead space rather than a
    permanent leak -- and it grew with every redaction for anyone who
    never compacts.

    Read after `close()`. The persistence manager drains on a background
    thread, so a query issued before the close is a query about a
    half-written store.

    No `export()` here, deliberately: this test asks what `redact()` and
    `save()` leave in the store, and an export in between would put a
    second reader between the two.
    """
    monkeypatch.setenv(lever, "1")
    session, inst = reloaded_redaction_session([ZONE], name=f"store_{lever}")
    source_uid = inst.sop_instance_uid
    db_path = session.store_backend.db_path

    session.redact(show_progress=False)
    new_uid = inst.sop_instance_uid
    assert new_uid != source_uid, "nothing was redacted; the counts below are not the question"

    session.save()
    session.close()

    con = sqlite3.connect(db_path)
    try:
        instances = con.execute(
            "SELECT sop_instance_uid FROM instances").fetchall()
        blobs = con.execute(
            "SELECT instance_uid, kind FROM instance_blobs").fetchall()
    finally:
        con.close()

    assert instances == [(new_uid,)], (
        f"expected one instance row under the redacted UID, got {instances}")
    assert blobs == [(new_uid, "pixels")], (
        f"expected one pixel blob row under the redacted UID, got {blobs}; "
        "a row under any other UID is an orphan only `compact()` notices")
