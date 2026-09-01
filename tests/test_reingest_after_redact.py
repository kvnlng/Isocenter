"""Re-ingesting a folder after `redact()` must not re-add the source (#238).

`Instance.regenerate_uid()` clears `file_path` -- it has to, because
`get_pixel_data()` falls back to it and a redacted instance still
pointing at its source would silently reload the un-redacted frame --
and `DicomStore.get_known_files()` built the incremental-ingest
de-duplication set out of that same field. So a redacted instance
stopped contributing its source path, and the next `ingest()` of the
same folder re-added the original: it keeps its SOP Instance UID while
the redacted copy carries a generated one, so no key in the store
relates the two.

Measured on 3.12.13 and 3.14.7t before the fix: two instances, two
exported files, the source's carrying its burned-in identifier intact,
under a compliance report that named nothing.

Two records answer it, and this file pins both:

* `Instance.source_path` -- the file an instance was *read* from, which
  redaction does not change -- keys the path gate;
* `_ISOCENTER_SOURCE_SOP_UID` -- the identity `regenerate_uid()` retired
  -- keys an identity gate that also catches the same file reached by
  another path (a copy, a move, a symlinked mount).

**Nothing here hands a fixture either value.** Every test writes a real
DICOM file and ingests it, because a `source_path` supplied by the test
would prove only that the dataclass accepts one.

`_ISOCENTER_SOURCE_SOP_UID` is spelled as a bare literal rather than
imported from `isocenter.entities`. A module-scope import of a name the
pre-fix tree does not have makes collection fail, which collapses all
nine detection tests into one ImportError and destroys the behavioural
red-before evidence T1/T4/T5/T7 exist to produce.
"""
import os
import shutil
import sqlite3

import numpy as np
import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian

from isocenter.session import DicomSession

SC_STORAGE = "1.2.840.10008.5.1.4.1.1.7"

#: See the module docstring: a literal on purpose.
SOURCE_SOP_UID_ATTR = "_ISOCENTER_SOURCE_SOP_UID"

#: Both spellings, wherever the executor can change the answer. The
#: identity record is written at two sites for the same reason #228's
#: `file_path = None` is, and a single leg cannot state that property.
LEVERS = ["ISOCENTER_FORCE_THREADS", "ISOCENTER_FORCE_PROCESSES"]

#: Inside a 32x32 image, so `apply_redaction_to_array` reports a change
#: and the worker reaches `regenerate_uid()`.
ZONE = [0, 8, 0, 8]

#: Entirely past the edge of a 32x32 image, as
#: `tests/test_redaction_identity.py` uses it: every zone is skipped and
#: no mutation comes back, so nothing is re-identified.
OFF_EDGE_ZONE = [100, 108, 100, 108]

RULES = [{"serial_number": "SN1", "redaction_zones": [ZONE]}]


def _write_source(path, uid, series_uid="1.2.3.se"):
    """One 32x32 MONOCHROME2 SC instance with a burned-in block.

    Rows and columns 0-7 hold value 200, which is what `ZONE` covers, so
    a redacted export sums to zero there and an un-redacted one does
    not. Carries the Type 2 elements `export()`'s module validation
    wants, so a file that reaches the exporter is actually written.
    """
    ds = Dataset()
    ds.file_meta = FileMetaDataset()
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.file_meta.MediaStorageSOPClassUID = SC_STORAGE
    ds.file_meta.MediaStorageSOPInstanceUID = uid
    ds.SOPClassUID = SC_STORAGE
    ds.SOPInstanceUID = uid
    ds.PatientID = "P1"
    ds.PatientName = "Test^Patient"
    ds.StudyInstanceUID = "1.2.3.st"
    ds.SeriesInstanceUID = series_uid
    ds.StudyDate = "20230101"
    ds.StudyTime = "120000"
    ds.Modality = "OT"
    ds.DeviceSerialNumber = "SN1"
    ds.Manufacturer = "Acme"
    ds.ManufacturerModelName = "Scanner"
    ds.InstanceNumber = 1
    ds.Rows = 32
    ds.Columns = 32
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = 8
    ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    arr = np.zeros((32, 32), dtype=np.uint8)
    arr[0:8, 0:8] = 200
    ds.PixelData = arr.tobytes()
    ds.save_as(str(path), enforce_file_format=True)
    return str(path)


def _instances(session):
    return [i for p in session.store.patients for st in p.studies
            for se in st.series for i in se.instances]


@pytest.fixture
def ingested(tmp_path):
    """A session holding one ingested instance, plus its source path.

    Yields `(session, source_dir, source_file)`. The session is a real
    `DicomSession`, so it carries a `store_backend` and a live
    `sidecar_manager` -- which T7 depends on, and which
    `DicomImporter.import_files` called directly does not have.
    """
    src = tmp_path / "src"
    src.mkdir()
    source_file = _write_source(src / "a.dcm", "1.2.3.phi")
    session = DicomSession(str(tmp_path / "s.db"))
    session.ingest(str(src))
    try:
        yield session, str(src), source_file
    finally:
        session.close()


# --- Detection --------------------------------------------------------------

# --- T1 ---------------------------------------------------------------------

@pytest.mark.parametrize("lever", LEVERS)
def test_reingesting_a_redacted_folder_adds_nothing(ingested, monkeypatch,
                                                    lever):
    """The whole defect, through the public API only.

    Red on `65dcb2e` on both gate interpreters and under both levers:
    the second `ingest()` re-added `1.2.3.phi` beside the redacted
    instance, so the store held two images of the same frame, one of
    them the un-redacted original.

    The lever acts on `redact()`, which is where the executor decides
    whether the worker mutated this process's object or a copy of it.
    """
    monkeypatch.setenv(lever, "1")
    session, src, source_file = ingested
    session.configuration.rules = RULES
    assert session.redact(show_progress=False) == 1, (
        "nothing was redacted, so this asserts the fixture and not the fix")

    session.ingest(src)

    live = _instances(session)
    assert len(live) == 1, (
        "re-ingesting the source folder after redaction added the "
        f"un-redacted original back: {[i.sop_instance_uid for i in live]}")
    assert live[0].sop_instance_uid != "1.2.3.phi", (
        "the surviving instance is the source, not the redacted copy")
    assert live[0].source_path == source_file


# --- T2 ---------------------------------------------------------------------

def test_redaction_keeps_the_source_path_and_still_drops_the_file_path(
        ingested):
    """Redaction detaches the file and keeps the provenance.

    The two halves are one assertion pair on purpose: `source_path`
    exists so `file_path` can keep being cleared. A fix that resurrected
    `file_path` would satisfy the de-duplication tests and reintroduce
    the reason `regenerate_uid()` clears it -- `get_pixel_data()` would
    reload the un-redacted frame off disk.

    Red on `65dcb2e` by `AttributeError: 'Instance' object has no
    attribute 'source_path'`, which is weaker evidence than T1's; it is
    here to pin a clause T1 cannot distinguish.
    """
    session, _, source_file = ingested
    inst = _instances(session)[0]
    assert inst.source_path == source_file, (
        "ingest did not record where the instance was read from")
    assert inst.file_path == source_file

    session.configuration.rules = RULES
    assert session.redact(show_progress=False) == 1

    inst = _instances(session)[0]
    assert inst.source_path == source_file, (
        "redaction cleared the provenance it must not touch")
    assert inst.file_path is None, (
        "the redacted instance still points at the un-redacted source "
        "file, which `get_pixel_data()` would read back")


# --- T3 ---------------------------------------------------------------------

def test_provenance_survives_a_save_and_reload(tmp_path):
    """The persistence round-trip: schema, upsert row, ALTER, both loads.

    `save_all` prunes the rows of instances that left the graph, so
    after a redaction the database holds exactly one instance and its
    `file_path` is NULL. The stored `source_path` is the only thing that
    can bring the origin back, and without it the field is memory-only:
    de-duplication is correct in the session that redacted and wrong in
    every session that reopens the store.
    """
    src = tmp_path / "src"
    src.mkdir()
    source_file = _write_source(src / "a.dcm", "1.2.3.phi")
    db = str(tmp_path / "s.db")

    session = DicomSession(db)
    session.ingest(str(src))
    session.configuration.rules = RULES
    assert session.redact(show_progress=False) == 1
    session.save()
    session.close()

    with sqlite3.connect(db) as conn:
        stored = conn.execute(
            "SELECT file_path, source_path FROM instances").fetchall()
    assert stored == [(None, source_file)], (
        f"the instances row did not carry the origin forward: {stored}")

    reopened = DicomSession(db)
    try:
        assert reopened.store.get_ingested_paths() == {
            os.path.abspath(source_file)}
        assert _instances(reopened)[0].source_path == source_file
        # The identity record is the *other* gate's key and rides
        # `attributes_json` rather than a column of its own. Asserted
        # here because nothing else does: T4 re-ingests the same folder
        # and is answered by the path gate, so gate 2 could return `{}`
        # on every reopened store -- silently accepting the copy T5
        # exists to refuse -- and the whole suite would stay green.
        assert _instances(reopened)[0].attributes[
            SOURCE_SOP_UID_ATTR] == "1.2.3.phi"
        assert reopened.store.get_superseded_uids(), (
            "the reopened store cannot recognise the pre-redaction "
            "identity it recorded, so a copy of the source at another "
            "path would be imported")
    finally:
        reopened.close()


# --- T4 ---------------------------------------------------------------------

def test_reingesting_after_a_reload_adds_nothing(tmp_path):
    """The field is not memory-only.

    T1 passes against an implementation that records `source_path` on
    the object and never writes it to the store -- the shape #84 warns
    about, where an index built once at ingest is never rebuilt on load.
    This is the leg that says so: red on `65dcb2e` behaviourally, two
    instances after the reopened session re-ingests its own source.
    """
    src = tmp_path / "src"
    src.mkdir()
    _write_source(src / "a.dcm", "1.2.3.phi")
    db = str(tmp_path / "s.db")

    session = DicomSession(db)
    session.ingest(str(src))
    session.configuration.rules = RULES
    assert session.redact(show_progress=False) == 1
    session.save()
    session.close()

    reopened = DicomSession(db)
    try:
        reopened.ingest(str(src))
        live = _instances(reopened)
        assert len(live) == 1, (
            "a reopened store re-imported its own source folder: "
            f"{[i.sop_instance_uid for i in live]}")
        assert live[0].sop_instance_uid != "1.2.3.phi"
    finally:
        reopened.close()


# --- T5 ---------------------------------------------------------------------

def test_a_copy_of_the_source_at_another_path_is_declined(ingested, tmp_path):
    """The identity gate: the same image, reached by a path nothing knows.

    The copy must live in a **second directory**. Left in the source
    folder, the path gate answers first and this passes without gate 2
    ever running -- green for the wrong reason, and blind to the copy,
    move and symlinked-mount cases the identity gate exists for.

    Red on `65dcb2e` behaviourally: two instances, the second the
    un-redacted original.
    """
    session, _, source_file = ingested
    session.configuration.rules = RULES
    assert session.redact(show_progress=False) == 1
    redacted_uid = _instances(session)[0].sop_instance_uid

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    shutil.copy(source_file, elsewhere / "copy.dcm")

    session.ingest(str(elsewhere))

    live = _instances(session)
    assert len(live) == 1, (
        "a copy of the un-redacted source at another path was imported: "
        f"{[i.sop_instance_uid for i in live]}")

    errors = session.store_backend.get_audit_errors()
    warnings = [e for e in errors if e[1] == "WARNING"]
    assert len(warnings) == 1, f"expected one WARNING row, got {errors}"
    details = warnings[0][2]
    assert "1.2.3.phi" in details and redacted_uid in details, (
        "the audit row names neither the declined identity nor the "
        f"instance that supersedes it: {details}")


# --- T6 ---------------------------------------------------------------------

def test_the_declined_import_reaches_the_compliance_report(ingested, tmp_path):
    """`WARNING`, not a row the report only counts.

    `get_audit_errors()` selects `action_type IN ('ERROR','WARNING')`,
    which is what puts the row in section 4 and takes the grade to
    `REVIEW_REQUIRED` with no renderer change. `RISK` would be counted
    in section 2 and never graded -- a declined re-import the operator
    never sees is the failure this issue is filed under.
    """
    session, _, source_file = ingested
    session.configuration.rules = RULES
    assert session.redact(show_progress=False) == 1

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    shutil.copy(source_file, elsewhere / "copy.dcm")
    session.ingest(str(elsewhere))

    report = tmp_path / "report.md"
    session.generate_report(str(report))
    text = report.read_text()

    section4 = text.split("## 4. Exceptions & Errors", 1)[1].split("## 5.", 1)[0]
    assert "1.2.3.phi" in section4, (
        f"the declined identity is not named in section 4: {section4}")
    assert "| **REVIEW_REQUIRED** |" in text, (
        "a run that declined an un-redacted original graded as clean")


# --- T7 ---------------------------------------------------------------------

def test_a_declined_import_writes_nothing_to_the_sidecar(ingested, tmp_path):
    """Gate placement: above the sidecar write, not below it.

    Moving the `continue` under the pixel write leaves T5 green -- the
    instance is still never linked into the graph -- and leaves this
    red. The frame would be appended to the sidecar and registered in
    `instance_blobs` under a UID no instance carries: un-redacted pixels
    resident in the store, invisible to the graph, and stranded for
    compaction (#235).

    The size assertion needs the session's own `sidecar_manager`: the
    guarded write is `if p_bytes and sidecar_manager:`, and
    `session.ingest()` is what passes `self.store_backend.sidecar`.
    Calling `DicomImporter.import_files` directly without one, as
    several other tests do, would make this vacuous rather than red.
    """
    session, _, source_file = ingested
    session.configuration.rules = RULES
    assert session.redact(show_progress=False) == 1
    # `sync=True` so the store is quiesced before the baseline is read.
    # The default drains on the persistence thread, and a read that
    # races it sees the pre-redaction blob row still present and the
    # new one already written -- a baseline of 2 that the next save
    # prunes to 1, which reads as the gate having deleted something.
    session.save(sync=True)

    sidecar_path = session.store_backend.sidecar.filepath
    size_before = os.path.getsize(sidecar_path)
    with sqlite3.connect(session.store_backend.db_path) as conn:
        blobs_before = conn.execute(
            "SELECT COUNT(*) FROM instance_blobs").fetchone()[0]
    assert blobs_before > 0, (
        "no blob rows at all, so the count assertion below would hold "
        "against an ingest that wrote nothing anywhere")

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    shutil.copy(source_file, elsewhere / "copy.dcm")
    session.ingest(str(elsewhere))

    assert os.path.getsize(sidecar_path) == size_before, (
        "the declined file's un-redacted frame was appended to the sidecar")
    with sqlite3.connect(session.store_backend.db_path) as conn:
        blobs_after = conn.execute(
            "SELECT COUNT(*) FROM instance_blobs").fetchone()[0]
    assert blobs_after == blobs_before, (
        "a blob row was registered for a file that was never imported")


# --- T8 ---------------------------------------------------------------------

@pytest.mark.parametrize("lever", LEVERS)
def test_the_pre_redaction_identity_is_recorded_under_either_executor(
        ingested, monkeypatch, lever):
    """Two assignment sites, and both are needed -- the #228 shape.

    Under threads the worker *is* the parent's object, so
    `regenerate_uid()` has already written the record; under processes
    the worker wrote it on a copy that is discarded, and
    `_apply_redaction_outcomes` is this process's own authority for the
    same fact. Deleting either line reddens exactly one leg.
    """
    monkeypatch.setenv(lever, "1")
    session, _, _ = ingested
    inst = _instances(session)[0]
    source_uid = inst.sop_instance_uid

    session.configuration.rules = RULES
    assert session.redact(show_progress=False) == 1

    inst = _instances(session)[0]
    assert inst.sop_instance_uid != source_uid
    assert inst.attributes[SOURCE_SOP_UID_ATTR] == source_uid, (
        "the redacted instance did not record the identity it retired, "
        "so a re-offered copy of its source cannot be recognised")


# --- T9 ---------------------------------------------------------------------

def test_a_forced_second_redaction_keeps_the_first_identity(ingested):
    """Write-once: the first identity, the one a file can carry.

    `redact(force=True)` (#237) re-redacts and takes another generated
    UID. An unconditional assignment would record that one -- a UID that
    exists in no file on disk -- and the ingest gate would stop matching
    the real source, which is the whole point of the record.
    """
    session, _, _ = ingested
    source_uid = _instances(session)[0].sop_instance_uid

    session.configuration.rules = RULES
    assert session.redact(show_progress=False) == 1
    first_pass_uid = _instances(session)[0].sop_instance_uid

    session.redact(show_progress=False, force=True)
    inst = _instances(session)[0]

    assert inst.attributes[SOURCE_SOP_UID_ATTR] == source_uid, (
        "a second redaction overwrote the recorded origin with a "
        f"generated UID ({inst.attributes[SOURCE_SOP_UID_ATTR]!r}); "
        f"the first pass produced {first_pass_uid!r}, which no source "
        "file carries")


# --- Selectivity guards -----------------------------------------------------

# --- S1 ---------------------------------------------------------------------

def test_a_genuinely_new_file_is_still_ingested_after_a_redaction(
        ingested, tmp_path):
    """Selectivity guard -- green before and after.

    Neither gate may match too broadly. A `get_superseded_uids()` that
    answered "every UID in the store", or a path gate that matched a
    directory rather than a file, passes the entire detection set above
    while refusing files this session has never seen.
    """
    session, _, _ = ingested
    session.configuration.rules = RULES
    assert session.redact(show_progress=False) == 1

    more = tmp_path / "more"
    more.mkdir()
    _write_source(more / "b.dcm", "1.2.3.other", series_uid="1.2.3.se2")

    session.ingest(str(more))

    uids = {i.sop_instance_uid for i in _instances(session)}
    assert len(uids) == 2, f"a new, unrelated file was refused: {uids}"
    assert "1.2.3.other" in uids


# --- S2 ---------------------------------------------------------------------

def test_an_instance_no_zone_touched_records_no_provenance_change(ingested):
    """Selectivity guard -- vacuous in the clauses that can be measured.

    Recording a retired identity for an instance nothing was applied to
    would make that instance's own source file permanently
    un-re-ingestable -- a false refusal, and the milestone's defect with
    its polarity reversed. That is what this guards, and it is not
    evidence the fix works: on `65dcb2e` the key never exists, so the
    first two assertions hold there for want of anything to violate
    them.

    The third assertion is not vacuous and is not detection either: it
    names `source_path`, so on `65dcb2e` this test errors with
    `AttributeError: 'Instance' object has no attribute 'source_path'`
    rather than passing. Measured, and recorded here so a reader is not
    misled into counting it as one of the red-before legs.
    """
    session, _, source_file = ingested
    session.configuration.rules = [
        {"serial_number": "SN1", "redaction_zones": [OFF_EDGE_ZONE]}]
    assert session.redact(show_progress=False) == 0, (
        "the off-edge zone was applied, so this no longer describes an "
        "instance nothing happened to")

    inst = _instances(session)[0]
    assert SOURCE_SOP_UID_ATTR not in inst.attributes, (
        "an instance nothing was applied to recorded a retired identity")
    assert inst.file_path == source_file, (
        "an instance nothing was applied to was detached from a file it "
        "still byte-for-byte matches")
    assert inst.source_path == inst.file_path


# --- S3 ---------------------------------------------------------------------

def test_the_recorded_identity_does_not_reach_the_exported_file(
        ingested, tmp_path):
    """Selectivity guard -- vacuously green on `65dcb2e`.

    `DicomExporter._merge` skips keys that start with `_` or carry no
    comma, which is the route `_ISOCENTER_REDACTION_HASH` already takes.
    The record is bookkeeping, and a key that is neither a tag nor a
    DICOM keyword in a written file would be a conformance defect.
    """
    import pydicom

    session, _, _ = ingested
    session.configuration.rules = RULES
    assert session.redact(show_progress=False) == 1

    out = tmp_path / "out"
    session.export(str(out), format="dicom")
    written = sorted(out.rglob("*.dcm"))
    assert len(written) == 1, (
        f"expected one exported file, got {written}; an empty tree would "
        "make the byte assertion below vacuous")

    assert SOURCE_SOP_UID_ATTR.encode() not in written[0].read_bytes(), (
        "the internal provenance record was written into the export")
    pydicom.dcmread(str(written[0]))


# --- S4 ---------------------------------------------------------------------

def test_ingesting_an_unredacted_folder_twice_is_still_silent(ingested):
    """Selectivity guard -- green before and after.

    Ordinary incremental ingest re-offers a folder constantly. If gate 2
    fired there, every session that re-scans its inbox would be stamped
    `REVIEW_REQUIRED` -- and the grade would stop meaning anything.
    """
    session, src, _ = ingested

    session.ingest(src)

    assert len(_instances(session)) == 1
    assert session.store_backend.get_audit_errors() == [], (
        "an ordinary second ingest of an unredacted folder wrote an "
        "audit error or warning")
