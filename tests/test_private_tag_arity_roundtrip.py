"""The private-tag tier records how many values there were (#328).

`instance_attributes` is one row per value atom, ordered by
`atom_index`. It records the value and -- since #154 -- the VR, and it
recorded no **arity**: reassembly returned a list when there was more
than one row and a scalar otherwise, so `['SIEMENS']` went in and
`'SIEMENS'` came out, and `[]` went in and the tag came back absent.

**Two reds, at two different levels, and the difference is measured.**
For the one-element case the harm stops at the graph: `['SIEMENS']` and
`'SIEMENS'` serialize to byte-identical DICOM (verified -- both written
to a file and the bytes compared, equal), because pydicom accepts either
and writes the same element. So its test is a graph-level assertion, and
it is honest about that. The **empty list** is file-observable:
`_fallback_multivalue([])` returns `('LO', [])`, which pydicom writes as
a present, zero-length element, while the reloaded path wrote no rows at
all and the tag vanished with no `DATA_LOSS` row to say so. That second
red is the capability the container length buys over a boolean "was a
sequence" flag, which is why the column stores the length.

**Neither shape arises from ingesting a conformant file.** pydicom
returns a scalar for VM 1 and `''` for a zero-length text element, so
`['ONE']` and `[]` enter the graph only through `set_attr` or a
remediation write. The tests plant them that way on purpose; the issue's
`["SIEMENS"]` example should not be read as an ingest path.

**The collision with #339.** A NULL `value_text` means the atom was
`None`; a `value_count` of `0` means the element was an empty container
and its single placeholder row carries no atom at all. `value_count`
resolves first. Getting these the other way round makes an empty list
reload as `[None]`, which is why one test here asserts exactly that
distinction.
"""
import glob
import os
import sqlite3

import numpy as np
import pydicom
import pytest
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.tag import Tag
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from isocenter.persistence import SqliteStore
from isocenter.session import DicomSession

ONE_ELEMENT = "0009,1030"
EMPTY = "0009,1031"
THREE = "0009,1032"


def _write_src(folder):
    """One instance with a private creator and pixel data.

    The pixel data forces the compressed export branch, which writes an
    explicit-VR transfer syntax; under the uncompressed branch's Implicit
    VR Little Endian a private element carries no VR in the file and the
    two exports could not be compared element by element.
    """
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.7"
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian

    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientID, ds.PatientName = "PAT328", "DOE^JOHN"
    ds.StudyInstanceUID, ds.SeriesInstanceUID = generate_uid(), generate_uid()
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.SOPClassUID = meta.MediaStorageSOPClassUID
    ds.Modality, ds.SeriesNumber, ds.InstanceNumber = "OT", 1, 1
    ds.StudyDate = "20230101"
    ds.add_new(0x00090010, 'LO', 'ACME_HEADER')     # Private Creator

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


def _plant(session):
    """The three arities, written the only way they can enter a graph."""
    for patient in session.store.patients:
        for study in patient.studies:
            for series in study.series:
                for instance in series.instances:
                    instance.set_attr(ONE_ELEMENT, ["ONE"])
                    instance.set_attr(EMPTY, [])
                    instance.set_attr(THREE, ["A", "B", "C"])


def _sole_instance(session):
    return session.store.patients[0].studies[0].series[0].instances[0]


def _read_only_written(out):
    written = glob.glob(os.path.join(str(out), "**", "*.dcm"), recursive=True)
    assert written, "export produced no .dcm files"
    return pydicom.dcmread(written[0])


def _ingest_plant_save(tmp_path, db):
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    _write_src(str(src))

    session = DicomSession(persistence_file=str(db))
    try:
        session.ingest(str(src))
        _plant(session)
        session.save(sync=True)
    finally:
        session.close()


@pytest.fixture
def reloaded(tmp_path):
    """A fresh session opened over a database that already holds the block."""
    db = tmp_path / "arity.db"
    _ingest_plant_save(tmp_path, db)

    session = DicomSession(persistence_file=str(db))
    yield session
    session.close()


def test_a_one_element_private_list_reloads_as_a_list(reloaded):
    """The graph-level red, and it is graph-level for a measured reason.

    `['SIEMENS']` and `'SIEMENS'` write byte-identical DICOM -- both
    saved to a file and the bytes compared, equal -- so this collapse is
    invisible in the exported artefact and cannot be asserted there. The
    harm is confined to in-memory callers: one that iterates the value
    gets three characters where it expected one string, and one that
    reads `len()` gets the wrong answer. That is a smaller blast radius
    than the empty-list case below, and it is still a value that came
    back as a different thing from the one that was stored.
    """
    assert _sole_instance(reloaded).attributes.get(ONE_ELEMENT) == ["ONE"], (
        "a one-element list reloaded as %r; the tier records the atoms "
        "and, until #328, not how many the element had"
        % (_sole_instance(reloaded).attributes.get(ONE_ELEMENT),))


def test_a_multi_valued_private_tag_still_reloads_as_a_list(reloaded):
    """The regression guard: the existing branch was not eaten.

    Green on both sides of #328. It is here because the new resolution
    reads `value_count` before falling back to the row count, and a
    three-atom element is the case that already worked -- if the column
    is ever written wrong for the ordinary path, this is what says so.
    """
    assert _sole_instance(reloaded).attributes.get(THREE) == ["A", "B", "C"]


def test_an_empty_private_list_survives_a_reload_into_the_exported_file(
        tmp_path):
    """The end-to-end red, and the one a boolean flag could not turn green.

    An empty container has no atom to hang a count on, so it needs a
    placeholder row -- which is only worth writing if the column can say
    "zero values" rather than merely "this was a sequence". The fresh
    export writes a present, zero-length `LO` element for `[]`
    (`_fallback_multivalue`'s `if not atoms: return 'LO', []` arm, whose
    own comment calls it "a legal element saying the tag was present
    with no value"). The reloaded export wrote nothing, and no
    `DATA_LOSS` row either -- the tag simply was not there any more.
    """
    src = tmp_path / "src"
    src.mkdir()
    _write_src(str(src))

    fresh_out = tmp_path / "fresh"
    session = DicomSession(persistence_file=str(tmp_path / "fresh.db"))
    try:
        session.ingest(str(src))
        _plant(session)
        session.export(str(fresh_out), format="dicom", show_progress=False)
    finally:
        session.close()

    db = tmp_path / "reloaded.db"
    _ingest_plant_save(tmp_path, db)
    reloaded_out = tmp_path / "reloaded"
    session = DicomSession(persistence_file=str(db))
    try:
        session.export(str(reloaded_out), format="dicom", show_progress=False)
    finally:
        session.close()

    fresh = _read_only_written(fresh_out)
    reloaded_ds = _read_only_written(reloaded_out)
    tag = Tag(0x0009, 0x1031)

    assert tag in fresh, (
        "the fresh export no longer writes a zero-length element for an "
        "empty list; the comparison below would then be comparing the "
        "wrong thing")
    assert tag in reloaded_ds, (
        "an empty private list did not survive the reload: the fresh "
        "export writes a present zero-length element and the reloaded "
        "one omits the tag entirely, with nothing saying so (#328)")
    assert reloaded_ds[tag].VR == fresh[tag].VR
    assert reloaded_ds[tag].value == fresh[tag].value


def test_an_empty_list_and_a_list_holding_none_are_told_apart():
    """The precedence rule between #328 and #339, asserted rather than
    described.

    `[]` writes one placeholder row whose `value_text` is NULL and whose
    `value_count` is `0`. `[None]` writes one atom row whose
    `value_text` is also NULL, with a `value_count` of `1`. The two rows
    differ in the count and in nothing else, so the count has to be read
    first: a reader that asked "is `value_text` NULL?" first would
    reload the empty list as `[None]`.
    """
    store = SqliteStore(":memory:")
    try:
        store.save_vertical_attributes(
            "I328", {("0029", "1040"): [], ("0029", "1041"): [None]})
        loaded = store.load_vertical_attributes("I328")
    finally:
        store.stop()

    assert loaded[("0029", "1040")] == [], (
        "an empty container came back as %r"
        % (loaded[("0029", "1040")],))
    assert loaded[("0029", "1041")] == [None], (
        "a one-element list holding None came back as %r"
        % (loaded[("0029", "1041")],))


def test_a_stored_count_that_disagrees_with_the_rows_does_not_truncate():
    """`value_count` decides the shape, never the contents.

    A hand-edited or corrupt store can hold a count that disagrees with
    the number of rows. The read path trusts the rows for the values and
    uses the count only to decide list-versus-scalar: it does not
    truncate to the count and does not pad. Truncating would make a
    wrong number in one column into a silent loss of values that are
    sitting right there in the table.

    Both directions are asserted, because they fail differently. An
    over-count is harmless under any implementation -- there is nothing
    to slice away. An **under**-count is the dangerous one, and `0` is
    the extreme of it: a reader that took `count == 0` as "empty
    container" on its own returned `[]` over two real atoms and dropped
    them. The empty container is recognised by its placeholder row's
    shape instead -- one atom, NULL text -- so `0` written over real
    values hands the values back.
    """
    store = SqliteStore(":memory:")
    try:
        store.save_vertical_attributes(
            "I328T", {("0029", "1042"): ["A", "B"],
                      ("0029", "1043"): ["C", "D"]})
        with store._get_connection() as conn:
            conn.execute(
                "UPDATE instance_attributes SET value_count = 5"
                " WHERE instance_uid = 'I328T' AND element_id = '1042'")
            conn.execute(
                "UPDATE instance_attributes SET value_count = 0"
                " WHERE instance_uid = 'I328T' AND element_id = '1043'")
        loaded = store.load_vertical_attributes("I328T")
    finally:
        store.stop()

    assert loaded[("0029", "1042")] == ["A", "B"]
    assert loaded[("0029", "1043")] == ["C", "D"], (
        "a count of 0 over two real atoms reloaded as %r; the rows are "
        "the values, and a wrong number in one column must not delete "
        "them" % (loaded[("0029", "1043")],))


def test_a_store_created_before_the_arity_column_still_opens(tmp_path):
    """Upgrading must not require rebuilding the session.

    `CREATE TABLE IF NOT EXISTS` leaves an existing table untouched, so
    the column never appears in a store an earlier version created. The
    guarded ALTER adds it, and rows predating it read NULL -- which
    hydrates by the old rule: more than one atom is a list, one atom is a
    scalar. That is not a loss. The old schema never recorded arity, so
    a pre-column one-atom row genuinely does not know whether it was
    `['X']` or `'X'`, and answering `['X']` would fabricate the one
    thing the column exists to stop being guessed at.
    """
    db = tmp_path / "legacy.db"
    _ingest_plant_save(tmp_path, db)

    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "ALTER TABLE instance_attributes DROP COLUMN value_count")

    session = DicomSession(persistence_file=str(db))
    try:
        with session.store_backend._get_connection() as conn:
            columns = {row[1] for row in conn.execute(
                "PRAGMA table_info(instance_attributes)").fetchall()}
        assert "value_count" in columns, (
            "the guarded ALTER did not re-add the column, so every save "
            "after this upgrade would fail on the INSERT")

        attributes = _sole_instance(session).attributes
        assert attributes.get(ONE_ELEMENT) == "ONE", (
            "a row that predates the column was hydrated as %r; the old "
            "schema never recorded arity and inventing a list for it is "
            "the fabrication the NULL rule exists to prevent"
            % (attributes.get(ONE_ELEMENT),))
        assert attributes.get(THREE) == ["A", "B", "C"], (
            "a pre-column multi-atom row must still reload as a list")
    finally:
        session.close()
