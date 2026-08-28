"""Private tags are written to the EAV tier and never read back (#158).

`_split_core_and_private` routes odd-group values to the
`instance_attributes` table, and `SqliteStore.load_all` -- which
`DicomSession.__init__` calls on every open of an existing database --
never SELECTed from it. `load_vertical_attributes` existed, worked, was
unit-tested, and had no production caller. So `remove_private_tags:
False` was honoured for exactly as long as the session stayed in memory:
save, close, reopen, and the vendor block was gone from the graph and
gone from the export.

The suite was green because no test crossed that boundary.
`tests/test_private_tag_export.py` never reloads, and
`test_save_all_contract.py::test_private_tags_are_split_out_into_the_vertical_table`
asserts the write half only. Every test in this file reloads.

Two things this file deliberately pins rather than fixes:

* **Types are not restored.** `save_vertical_attributes` writes
  `str(val)`, so a private `5` comes back as `"5"`. The table stores no
  VR -- `value_rep` is hardcoded to `"UN"` and never read -- so there is
  nothing to reconstruct a type from, and inventing one from the value
  would be a storage-shape decision. That is #154, and it is the repo
  owner's call. `test_a_numeric_private_value_reloads_as_a_string`
  states the current shape so a later fix has to change a test on
  purpose.
* **The table stores no arity either.** A one-element list saves as a
  single row and reloads as a scalar, for the same reason. Same root as
  #154; not invented around here.
"""
import contextlib
import glob
import os
import sqlite3

import numpy as np
import pydicom
import pytest
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.multival import MultiValue
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from isocenter.entities import Patient, Study, Series, Instance
from isocenter.persistence import SqliteStore
from isocenter.session import DicomSession


# --------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------

def _write_src(folder):
    """A single-instance study carrying a private block.

    Explicit VR on purpose: it is what makes pydicom hand back `str`
    values for the LO elements, which is what puts them on the EAV tier.
    Under implicit VR the same tags resolve to `UN`/`bytes` and stay
    inline in `attributes_json` -- the tier that already round-tripped,
    and the reason the loss looked partial rather than total (#151).
    """
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.7"
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian

    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientID, ds.PatientName = "PAT1", "DOE^JOHN"
    ds.StudyInstanceUID, ds.SeriesInstanceUID = generate_uid(), generate_uid()
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.SOPClassUID = meta.MediaStorageSOPClassUID
    ds.Modality, ds.SeriesNumber, ds.InstanceNumber = "OT", 1, 1
    ds.StudyDate = "20230101"

    ds.add_new(0x00090010, 'LO', 'ACME_HEADER')             # Private Creator
    ds.add_new(0x00091001, 'LO', 'acquisition-v7')
    ds.add_new(0x00091002, 'LO', ['alpha', 'beta', 'gamma'])  # VM = 3
    ds.add_new(0x00091003, 'IS', 5)                          # numeric, see #154

    # Pixel data so the export takes its compressed branch, which writes an
    # explicit-VR transfer syntax. The uncompressed branch writes Implicit
    # VR Little Endian, under which a private element carries no VR in the
    # file and pydicom reads every one of them back as `UN`/`bytes` -- a
    # property of the exporter and of #154, not of the reload, and not
    # something this file should be measuring.
    ds.Rows = ds.Columns = 4
    ds.BitsAllocated = ds.BitsStored = 8
    ds.HighBit = 7
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelRepresentation = 0
    ds.PixelData = np.zeros((4, 4), dtype=np.uint8).tobytes()

    path = os.path.join(folder, "one.dcm")
    ds.save_as(path, enforce_file_format=True)
    return path


def _ingest_save_close(tmp_path, db, remove_private=None):
    """Ingest the private-block study, optionally anonymize, save, close."""
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    _write_src(str(src))

    session = DicomSession(persistence_file=str(db))
    try:
        session.ingest(str(src))
        if remove_private is not None:
            session.configuration.remove_private_tags = remove_private
            session.audit()
            session.anonymize()
        session.save(sync=True)
    finally:
        session.close()


def _sole_instance(session):
    return session.store.patients[0].studies[0].series[0].instances[0]


def _private(attributes):
    """The odd-group subset of a `{"gggg,eeee": value}` mapping."""
    return {tag: val for tag, val in attributes.items()
            if int(tag.split(',')[0], 16) % 2 == 1}


@pytest.fixture
def reloaded(tmp_path):
    """A fresh session opened over a database that already has the block."""
    db = tmp_path / "reload.db"
    _ingest_save_close(tmp_path, db)

    session = DicomSession(persistence_file=str(db))
    yield session
    session.close()


# --------------------------------------------------------------------
# The round trip
# --------------------------------------------------------------------

def test_a_private_tag_survives_a_session_reload(reloaded):
    """The bug in one line: saved to the database, never read back."""
    assert _sole_instance(reloaded).attributes.get("0009,1001") == "acquisition-v7"


def test_the_private_creator_survives_a_session_reload(reloaded):
    """Without (gggg,0010) the surviving values cannot be attributed."""
    assert _sole_instance(reloaded).attributes.get("0009,0010") == "ACME_HEADER"


def test_a_multi_valued_private_tag_reloads_as_a_list(reloaded):
    """VM > 1 is what `atom_index` is for; one row per atom, reassembled.

    pydicom hands multi-valued elements back as a `MultiValue`, which is
    a `MutableSequence` and *not* a `list`, so the write side's
    `isinstance(val, list)` gate used to miss it and store
    `"['alpha', 'beta', 'gamma']"` in a single row. The reload would then
    be a string that looks like a list, which is worse than a loss
    because it survives a glance.
    """
    assert _sole_instance(reloaded).attributes.get("0009,1002") == [
        "alpha", "beta", "gamma"]


def test_a_numeric_private_value_reloads_as_a_string(reloaded):
    """Types are NOT restored, and that is deliberate here (#154).

    `save_vertical_attributes` writes `str(val)` and the table carries no
    usable VR, so there is nothing to reconstruct a type from. Guessing
    one from the value would be a storage-shape decision, which is the
    repo owner's to make. Pinned so a later fix changes it on purpose.
    """
    assert _sole_instance(reloaded).attributes.get("0009,1003") == "5"


def test_an_export_from_a_reloaded_session_carries_the_private_block(tmp_path):
    """The consequence a site actually sees.

    A store can be inspected and its private tags are visibly there. The
    export that runs after a reload had none of them.

    (0009,1002) is asserted on since #165. It was not when this file was
    written: `_fallback_encoding` returned None for any sequence value,
    so a multi-valued private tag had never reached an exported file, and
    #158 made that gap *more* reachable rather than less -- the EAV
    reassembles VM > 1 as a list of strings, where in-memory it was a
    `MultiValue` that fell down the same hole. Both shapes are handled
    now, and the list is the shape only a reload produces, so this is the
    end-to-end evidence for that half.
    """
    db = tmp_path / "export.db"
    _ingest_save_close(tmp_path, db)

    out = tmp_path / "out"
    session = DicomSession(persistence_file=str(db))
    try:
        session.export(str(out), format="dicom", show_progress=False)
    finally:
        session.close()

    written = glob.glob(str(out / "**" / "*.dcm"), recursive=True)
    assert written, "export produced no .dcm files"

    kept = {f"{el.tag.group:04x},{el.tag.element:04x}": el.value
            for el in pydicom.dcmread(written[0]) if el.tag.group % 2 == 1}
    assert kept.get("0009,0010") == "ACME_HEADER", kept
    assert kept.get("0009,1001") == "acquisition-v7", kept
    assert list(kept.get("0009,1002", [])) == ["alpha", "beta", "gamma"], kept


def test_a_reloaded_export_matches_an_in_memory_one(tmp_path):
    """The #158/#165 interaction, as one comparison rather than two.

    The same value reaches `_fallback_encoding` as two different Python
    types depending on whether a save and reopen happened in between: a
    `MultiValue` straight from pydicom, a `list` of strings out of the
    EAV table. A fix that handled one and not the other would make the
    exported file depend on session lifetime, which is the shape of #158
    itself.

    They converge because the EAV writes `str(atom)` and the encoder
    stringifies each value by the same scalar rules, so the headline
    assertion is an equality rather than two expected values -- there is
    nothing to keep in step. Both arms are then anchored against the
    literal as well: consistency alone is satisfied by two exports that
    are wrong in the same way.
    """
    src = tmp_path / "src"
    src.mkdir()
    _write_src(str(src))

    def _read(out):
        written = glob.glob(str(out / "**" / "*.dcm"), recursive=True)
        assert written, "export produced no .dcm files"
        return _private(
            {f"{el.tag.group:04x},{el.tag.element:04x}": el.value
             for el in pydicom.dcmread(written[0])})

    # One session, ingest straight to export: the private values are the
    # `MultiValue` objects pydicom produced and have never been through
    # the EAV table. Reopening here instead would hand this arm the same
    # reassembled strings as the other and the comparison would be
    # between a path and itself.
    out_mem = tmp_path / "out_mem"
    session = DicomSession(persistence_file=str(tmp_path / "mem.db"))
    try:
        session.ingest(str(src))
        session.export(str(out_mem), format="dicom", show_progress=False)
        assert isinstance(
            _sole_instance(session).attributes["0009,1002"], MultiValue), (
                "this arm is only the in-memory path while the value is "
                "still the MultiValue pydicom handed back")
    finally:
        session.close()
    in_memory = _read(out_mem)

    out_saved = tmp_path / "out_saved"
    db = tmp_path / "saved.db"
    _ingest_save_close(tmp_path, db)
    session = DicomSession(persistence_file=str(db))
    try:
        assert _sole_instance(session).attributes["0009,1002"] == [
            "alpha", "beta", "gamma"], "this arm is the EAV reassembly"
        session.export(str(out_saved), format="dicom", show_progress=False)
    finally:
        session.close()
    reloaded_out = _read(out_saved)

    # Equality first, because agreeing is the property under test -- and
    # then both arms against the literal, because two exports that are
    # wrong in the same way would agree just as well.
    assert in_memory == reloaded_out, (in_memory, reloaded_out)
    assert list(in_memory["0009,1002"]) == ["alpha", "beta", "gamma"]
    assert list(reloaded_out["0009,1002"]) == ["alpha", "beta", "gamma"]


# --------------------------------------------------------------------
# The half a read path can break: removals must stay removed
# --------------------------------------------------------------------

def test_removed_private_tags_do_not_come_back_on_a_reload(tmp_path):
    """The mirror of the fix, and the reason it is not read-side only.

    `REMOVE_TAG` deletes the key from `inst.attributes`, so after
    `remove_private_tags=True` the instance's private set is empty. The
    write side used to delete only the keys it was about to re-insert,
    and skipped the call entirely when there was nothing to insert -- so
    the stripped rows stayed in `instance_attributes` untouched. Inert
    while nothing read them; the moment hydration does, a reload puts the
    vendor block back into a graph that was de-identified on purpose.
    """
    db = tmp_path / "stripped.db"
    _ingest_save_close(tmp_path, db, remove_private=True)

    session = DicomSession(persistence_file=str(db))
    try:
        kept = _private(_sole_instance(session).attributes)
    finally:
        session.close()

    assert not kept, f"stripped private tags reappeared on reload: {kept}"


def test_a_private_tag_that_shrinks_to_one_value_does_not_keep_its_old_atoms(
        tmp_path):
    """VM 3 -> VM 1 must not leave atoms 1 and 2 in the table."""
    store = SqliteStore(str(tmp_path / "shrink.db"))
    patient = _hand_built_patient(private={"0009,1002": ["a", "b", "c"]})
    store.save_all([patient])

    inst = patient.studies[0].series[0].instances[0]
    inst.set_attr("0009,1002", "a")
    store.save_all([patient])

    fresh = SqliteStore(str(tmp_path / "shrink.db"))
    loaded = fresh.load_all()[0].studies[0].series[0].instances[0]
    assert loaded.attributes.get("0009,1002") == "a"


# --------------------------------------------------------------------
# Hydration must not look like an edit, and must not go per-instance
# --------------------------------------------------------------------

def _hand_built_patient(pid="P1", n_instances=1, private=None):
    """A graph with private tags, built without a session behind it."""
    patient = Patient(pid, "Test^Patient")
    study = Study(f"{pid}.STUDY", "20230101")
    series = Series(f"{pid}.SERIES", "CT", 1)
    for i in range(n_instances):
        inst = Instance(f"{pid}.INST.{i}", "/tmp/x.dcm")
        inst.sop_class_uid = "1.2.840.10008.5.1.4.1.1.2"
        inst.instance_number = i + 1
        inst.set_attr("0010,0020", pid)
        for tag, val in (private or {"0009,1001": "vendor"}).items():
            inst.set_attr(tag, val)
        series.instances.append(inst)
    study.series.append(series)
    patient.studies.append(study)
    return patient


def test_hydrating_private_tags_does_not_mark_the_graph_unsaved(tmp_path):
    """Hydration speaks for the whole graph with `mark_subtree_persisted`.

    Writing the private values back in after that call -- or with
    `set_attr`, which advances `_revision` -- would leave every reloaded
    instance claiming unsaved changes, and the next save would rewrite
    the entire store.
    """
    db = str(tmp_path / "clean.db")
    SqliteStore(db).save_all([_hand_built_patient(n_instances=3)])

    patients = SqliteStore(db).load_all()
    instances = patients[0].studies[0].series[0].instances

    assert all(i.attributes.get("0009,1001") == "vendor" for i in instances)
    assert not any(i.has_unsaved_changes for i in instances)
    assert not patients[0].has_unsaved_changes


def test_a_reloaded_instance_keeps_its_stored_phi_status(tmp_path):
    """A reload must not cost an instance what the store knew about it.

    An entity edited since its scan reports `UNSCANNED`, structurally --
    so anything hydration does that counts as an edit throws away the
    conclusion stored in the same row it was just read from. This asserts
    the outcome. What guarantees it is `_apply_vertical_attributes`
    assigning directly rather than through `set_attr`, which is pinned
    where it is written by
    `test_applying_a_loaded_private_tag_is_not_an_edit` -- this test
    passes either way, because both callers apply the values before the
    status loop.
    """
    from isocenter.entities import PhiStatus

    db = str(tmp_path / "status.db")
    patient = _hand_built_patient()
    inst = patient.studies[0].series[0].instances[0]
    inst.record_phi_status(PhiStatus.CLEARED)
    SqliteStore(db).save_all([patient])

    loaded = SqliteStore(db).load_all()[0].studies[0].series[0].instances[0]
    assert loaded.attributes.get("0009,1001") == "vendor"
    assert loaded.phi_status == PhiStatus.CLEARED


@contextlib.contextmanager
def _traced(store, sink):
    """Records every SQL statement `store` executes while active."""
    real = store._get_connection

    @contextlib.contextmanager
    def traced():
        with real() as conn:
            conn.set_trace_callback(sink.append)
            try:
                yield conn
            finally:
                conn.set_trace_callback(None)

    store._get_connection = traced
    try:
        yield
    finally:
        store._get_connection = real


def test_hydration_reads_the_vertical_table_in_bulk(tmp_path):
    """The whole reason the tier exists is that it is not queried per row.

    `load_vertical_attributes` takes one SOP Instance UID. Calling it in
    a loop would reintroduce exactly the 10k-instances-10k-queries shape
    the storage split was built to avoid, and it would do it on the
    default path of every session open.
    """
    db = str(tmp_path / "bulk.db")
    SqliteStore(db).save_all([_hand_built_patient(n_instances=40)])

    store = SqliteStore(db)
    statements = []
    with _traced(store, statements):
        patients = store.load_all()

    reads = [s for s in statements
             if "instance_attributes" in s and s.lstrip().upper().startswith("SELECT")]
    assert len(patients[0].studies[0].series[0].instances) == 40
    assert len(reads) == 1, f"{len(reads)} vertical reads for 40 instances"


def test_the_scan_worker_rehydrates_private_tags_too(tmp_path):
    """`load_patient` is the multiprocessing scan worker's entry point.

    `_scan_patient_worker` (session.py) rebuilds a patient from the
    database in the subprocess. Left unfixed, an out-of-process PHI scan
    would see a graph with no private tags at all while an in-process
    scan sees them -- the same defect, in the path where it is hardest
    to notice.
    """
    db = str(tmp_path / "worker.db")
    SqliteStore(db).save_all([_hand_built_patient(n_instances=2)])

    patient = SqliteStore(db).load_patient("P1")
    instances = patient.studies[0].series[0].instances

    assert all(i.attributes.get("0009,1001") == "vendor" for i in instances)
    assert not any(i.has_unsaved_changes for i in instances)


def test_bytes_private_values_are_not_loaded_twice(tmp_path):
    """`bytes` never enters the EAV, so the read path cannot duplicate it.

    `_split_core_and_private` keeps every `bytes` value inline because
    the vertical table's column is TEXT. That is what made the loss
    partial rather than total, and it also means hydration has exactly
    one source for such a value. Pinned so the split cannot quietly start
    sending bytes to both tiers.
    """
    db = str(tmp_path / "bytes.db")
    SqliteStore(db).save_all(
        [_hand_built_patient(private={"0009,1004": b"\x01\x02\x03"})])

    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT COUNT(*) FROM instance_attributes WHERE element_id='1004'"
        ).fetchone()[0]
    assert rows == 0, "a bytes value reached the vertical table"

    loaded = SqliteStore(db).load_all()[0].studies[0].series[0].instances[0]
    assert loaded.attributes.get("0009,1004") == b"\x01\x02\x03"


def test_a_row_left_behind_by_an_older_version_is_loaded_onto_the_graph(tmp_path):
    """The upgrade case, pinned rather than decided.

    Before this fix the write path deleted only the keys it was about to
    re-insert and skipped the call entirely for an instance whose private
    set had gone empty, so a store de-identified with
    `remove_private_tags: true` and saved by an earlier version still has
    the stripped vendor block sitting in `instance_attributes`. Reading
    the tier back puts those rows on the graph the first time such a
    store is opened after upgrading, and an export taken from that
    session carries them.

    This is not fixable from inside the library. A stale row and a
    legitimate one are byte-identical: nothing in the database records
    which private tags were deleted from the graph, so a migration would
    either drop every site's vendor block -- undoing the fix -- or keep
    every stripped one. Which it is depends on what the site ran, which
    only the site knows. Filed as #172; this test states what happens
    today so the decision is visible instead of latent.
    """
    db = str(tmp_path / "legacy.db")
    SqliteStore(db).save_all([_hand_built_patient()])

    # What the old writer left behind: a row for a tag the graph does not
    # have. Inserted directly, because the fixed writer cannot produce it.
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO instance_attributes (instance_uid, group_id,"
            " element_id, atom_index, value_rep, value_text)"
            " VALUES (?, '0009', '1009', 0, 'UN', 'stale-vendor')",
            ("P1.INST.0",))

    loaded = SqliteStore(db).load_all()[0].studies[0].series[0].instances[0]
    assert loaded.attributes.get("0009,1009") == "stale-vendor", (
        "if this fails the upgrade behaviour changed; #172 decided it, or "
        "something else did")


def test_a_private_tag_holding_an_empty_list_does_not_survive(tmp_path):
    """The one exception to "private tags survive a reload".

    An empty value has no atom to write, so it produces no rows, and the
    tier records no arity to tell "no rows" from "no such tag" apart --
    the same gap that turns a saved one-element list back into a scalar.
    It did not survive before this fix either, along with everything else
    on the tier. Stated here so a reader auditing their vendor block after
    an upgrade finds it written down rather than discovering it.
    """
    db = str(tmp_path / "empty.db")
    SqliteStore(db).save_all([_hand_built_patient(
        private={"0009,1001": "kept", "0009,1006": []})])

    loaded = SqliteStore(db).load_all()[0].studies[0].series[0].instances[0]
    assert loaded.attributes.get("0009,1001") == "kept"
    assert "0009,1006" not in loaded.attributes


def test_applying_a_loaded_private_tag_is_not_an_edit():
    """The one invariant `_apply_vertical_attributes` has to hold.

    It assigns into `attributes` directly, as `_deserialize_into` does,
    and must never reach for `set_attr` -- which advances `_revision`, and
    a status recorded at a revision the entity has since left reads back
    as `UNSCANNED` by design. A reloaded instance would then report that
    nothing is known about its PHI, having just been built from the row
    that said otherwise.

    Asserted on the helper rather than through a reload, because through a
    reload it is invisible. `load_all` and `load_patient` both apply these
    values before the `record_phi_status` loop and before
    `mark_subtree_persisted()`, and either call absorbs the bump, so
    swapping this body for `set_attr` leaves every round-trip test in this
    file green. That ordering is worth keeping -- it is what makes the
    mistake survivable -- but it is defence, not the rule, and the rule
    needs a test where it is written.
    """
    from isocenter.entities import PhiStatus

    inst = Instance("1.2.3.4", "1.2.840.10008.5.1.4.1.1.2")
    inst.record_phi_status(PhiStatus.CLEARED)
    revision = inst._revision

    SqliteStore._apply_vertical_attributes(inst, {("0009", "1001"): "vendor"})

    assert inst.attributes["0009,1001"] == "vendor"
    assert inst._revision == revision, "hydration advanced the revision"
    assert inst.phi_status == PhiStatus.CLEARED, (
        "the stored status was invalidated by loading the row it was "
        "stored with")
