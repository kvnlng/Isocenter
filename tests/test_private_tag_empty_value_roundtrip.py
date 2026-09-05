"""A zero-length private element must not reload as the word "None" (#339).

`save_vertical_attributes` renders every atom with `str()`, so a `None`
value -- which is what pydicom hands back for a zero-length element under
a *numeric* VR -- was stored as the four-character text `None` and
reloaded as that text. `"None"` is a conformant `LO` value, so the
reloaded export wrote it into the file as though the source had said it.

**The affected population is not the one the issue names.** #339 says
"a string VR with no value", and that is the one population that does
not reproduce: `pydicom.config.use_none_as_empty_text_VR_value` is
`False`, so a zero-length `LO`/`SH`/`UT` reads back as `''` and a `PN`
as `PersonName('')`, and those round-trip correctly today and after.
What reproduces is `DS`, `IS`, `US`, `UL`, `SS`, `FL`, `FD`, `AT` -- and
`UN`, whose zero-length value is `None` rather than `b''` and so misses
the binary arm that would have retained it as empty bytes.

**Fresh and reloaded disagreed in the audit trail as well as in the
file**, which is what picks the fix. Exported from the session that
ingested, the element is dropped with a `DATA_LOSS` row and the run
grades REVIEW_REQUIRED; exported after a save/close/reopen it was
written as `LO 'None'` with no row and a PASS. Skipping the tag at
`_split_core_and_private` -- the fix the issue proposes -- would have
made the *file* agree and left the *report* divergent: reloaded would
then drop the element in silence where fresh drops it loudly.
Preserving the `None` makes both halves agree, and forecloses nothing:
if the export encoder is ever taught to write a zero-length element,
both paths gain it at once, where a skip destroys the information for
good. The export writer already keeps a zero-length element rather
than dropping it, for the same reason:
`value = b""` at io_handlers.py line 867.

The file itself still omits the tag on both paths, which is #60's
ruling: absent beats fabricated.
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

#: The VRs whose zero-length value pydicom reads back as `None`. Every
#: one of them reached `str(None)` on the way into `value_text`.
EMPTY_NUMERIC = [
    (0x1005, 'DS'),
    (0x1006, 'US'),
    (0x1007, 'UL'),
    (0x1008, 'FL'),
    (0x1009, 'AT'),
    (0x100a, 'IS'),
    (0x100b, 'SS'),
    (0x100c, 'FD'),
    (0x100d, 'UN'),
]

#: The VRs whose zero-length value pydicom reads back as `''` (or as an
#: empty `PersonName`). These were never affected; see the module
#: docstring and `test_a_text_vr_private_element_was_never_affected`.
EMPTY_TEXT = [
    (0x1020, 'LO'),
    (0x1021, 'SH'),
    (0x1022, 'PN'),
]


def _write_src(folder):
    """One instance whose private block is entirely zero-length.

    Explicit VR on purpose: it is what puts a real VR on each private
    element in the source file, so the numeric and text populations can
    be told apart at all. Under Implicit VR every one of them arrives as
    `UN` and the question does not arise.

    The pixel data is there so the export takes its compressed branch,
    which writes an explicit-VR transfer syntax -- under the
    uncompressed branch's Implicit VR Little Endian no private element
    carries a VR in the file and the assertions below could not be made.
    """
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.7"
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian

    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientID, ds.PatientName = "PAT339", "DOE^JOHN"
    ds.StudyInstanceUID, ds.SeriesInstanceUID = generate_uid(), generate_uid()
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.SOPClassUID = meta.MediaStorageSOPClassUID
    ds.Modality, ds.SeriesNumber, ds.InstanceNumber = "OT", 1, 1
    ds.StudyDate = "20230101"

    ds.add_new(0x00090010, 'LO', 'ACME_HEADER')     # Private Creator
    for element, vr in EMPTY_NUMERIC + EMPTY_TEXT:
        ds.add_new(Tag(0x0009, element), vr, None)

    ds.Rows = ds.Columns = 4
    ds.BitsAllocated = ds.BitsStored = 8
    ds.HighBit = 7
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelRepresentation = 0
    ds.PixelData = np.zeros((4, 4), dtype=np.uint8).tobytes()

    path = os.path.join(folder, "one.dcm")
    ds.save_as(path, enforce_file_format=True)
    return ds.SOPInstanceUID


def _data_loss_tags(db_path):
    """The tags named by this store's `DATA_LOSS` rows."""
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT details FROM audit_log WHERE action_type='DATA_LOSS'"
        ).fetchall()
    return sorted({
        f"0009,{element:04x}"
        for element, _vr in EMPTY_NUMERIC + EMPTY_TEXT
        for (details,) in rows
        if f"0009,{element:04x}" in details})


def _read_only_written(out):
    written = glob.glob(os.path.join(str(out), "**", "*.dcm"), recursive=True)
    assert written, "export produced no .dcm files"
    return pydicom.dcmread(written[0])


def _export_fresh(tmp_path):
    """Export from the session that ingested, with no store round trip."""
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    _write_src(str(src))
    out = tmp_path / "out"
    db = str(tmp_path / "fresh.db")

    session = DicomSession(persistence_file=db)
    try:
        session.ingest(str(src))
        session.export(str(out), format="dicom", show_progress=False)
    finally:
        session.close()

    return _read_only_written(out), _data_loss_tags(db)


def _export_reloaded(tmp_path):
    """Export from a session that opened an existing database."""
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    _write_src(str(src))
    db = str(tmp_path / "reloaded.db")
    out = tmp_path / "out"

    session = DicomSession(persistence_file=db)
    try:
        session.ingest(str(src))
        session.save()
    finally:
        session.close()

    session = DicomSession(persistence_file=db)
    try:
        session.export(str(out), format="dicom", show_progress=False)
    finally:
        session.close()

    return _read_only_written(out), _data_loss_tags(db)


@pytest.fixture(scope="module")
def fresh_export(tmp_path_factory):
    return _export_fresh(tmp_path_factory.mktemp("fresh339"))


@pytest.fixture(scope="module")
def reloaded_export(tmp_path_factory):
    return _export_reloaded(tmp_path_factory.mktemp("reloaded339"))


def test_a_zero_length_private_element_is_not_reloaded_as_the_word_none():
    """The store-method red, measured exactly as #339 reports it.

    `str(None)` is `'None'`, and there is no arm in `_vertical_atom_text`
    that does not go through `str()` -- the `AT` arm reaches the same
    place by a longer route, because `int(None)` raises `TypeError` and
    the handler documented for "a value that is no longer a tag" catches
    it and returns `str(atom)`.
    """
    store = SqliteStore(":memory:")
    try:
        store.save_vertical_attributes("I339", {("0029", "1010"): None})
        loaded = store.load_vertical_attributes("I339")
    finally:
        store.stop()

    assert loaded == {("0029", "1010"): None}, (
        "a value that was None came back as %r; the store invented "
        "content for an element that had none (#339)"
        % (loaded.get(("0029", "1010")),))


def test_the_at_arm_reaches_the_same_answer_as_every_other_vr():
    """`AT` is the one arm with a branch of its own, so it gets a case.

    Its `str()` is the display spelling `'(0010,0010)'`, so it stores the
    decimal integer instead -- and `int(None)` raises, which drops a
    `None` into the `except` arm whose comment is about a value that is
    no longer a tag. Guarding `None` before that arm is what keeps the
    normal path out of an exception handler documented for something
    else.
    """
    store = SqliteStore(":memory:")
    try:
        store.save_vertical_attributes(
            "I339AT", {("0029", "1011"): None}, vrs={("0029", "1011"): "AT"})
        loaded = store.load_vertical_attributes("I339AT")
    finally:
        store.stop()

    assert loaded[("0029", "1011")] is None


def test_a_none_atom_inside_a_list_keeps_its_place():
    """One `None` among siblings is stored and reloaded in position.

    The export then reports the whole element as loss -- siblings
    included -- because `_fallback_multivalue` returns `None` for the
    element as soon as one atom has no text encoding. That is that
    function's documented rule and it is the same on the fresh path, so
    the two agree here too.
    """
    store = SqliteStore(":memory:")
    try:
        store.save_vertical_attributes(
            "I339L", {("0029", "1012"): [None, "B"]})
        loaded = store.load_vertical_attributes("I339L")
    finally:
        store.stop()

    assert loaded[("0029", "1012")] == [None, "B"], (
        "a None atom inside a multi-valued element must keep its place "
        "rather than becoming the text 'None'")


@pytest.mark.parametrize("element, vr", EMPTY_NUMERIC)
def test_a_reloaded_export_does_not_invent_a_value_for_an_empty_element(
        reloaded_export, element, vr):
    """The end-to-end red: a fabricated `LO 'None'` in the exported file.

    The fresh export of the same source omits the tag. This asserts the
    reloaded one does too -- absent, which is #60's ruling, rather than
    present and wrong.
    """
    exported, _losses = reloaded_export
    assert Tag(0x0009, element) not in exported, (
        "(0009,%04x) was zero-length %s in the source and the reloaded "
        "export wrote %r for it; the fresh export omits it (#339)"
        % (element, vr, exported[Tag(0x0009, element)].value))


def test_the_fresh_and_reloaded_exports_report_the_same_loss(
        fresh_export, reloaded_export):
    """The audit half, and the reason the fix preserves rather than skips.

    A skip at `_split_core_and_private` would make the two files agree
    and leave this red: the reloaded path would have no value to fail on
    and would drop the element in silence, where the fresh path drops it
    with a `DATA_LOSS` row and a REVIEW_REQUIRED grade. Preserving the
    `None` is what makes the same element fail the same way on both
    paths.
    """
    _fresh_ds, fresh_losses = fresh_export
    _reloaded_ds, reloaded_losses = reloaded_export

    assert fresh_losses == sorted(
        f"0009,{element:04x}" for element, _vr in EMPTY_NUMERIC), (
        "the fresh export's losses are not the zero-length numeric block; "
        "the two-path comparison below would then be comparing the wrong "
        "thing")
    assert reloaded_losses == fresh_losses, (
        "the reloaded export reported %r and the fresh one %r for the "
        "same source file: an element dropped loudly on one path and "
        "silently on the other (#339)" % (reloaded_losses, fresh_losses))


@pytest.mark.parametrize("element, vr", EMPTY_TEXT)
def test_a_text_vr_private_element_was_never_affected(
        fresh_export, reloaded_export, element, vr):
    """A characterization test: what #339's own text got wrong.

    `pydicom.config.use_none_as_empty_text_VR_value` is `False`, so a
    zero-length `LO`/`SH` reads back as `''` and a zero-length `PN` as an
    empty `PersonName`. An empty string has a text encoding, so it
    survives the store as `''` and is written as a present, zero-length
    element on both paths -- which is what the file said. Nothing here
    changed; it is held still because a flip of that pydicom setting in
    some consumer's process would turn the whole text block into the
    population #339 describes, and this is where that would be caught
    rather than discovered.
    """
    for exported, _losses in (fresh_export, reloaded_export):
        element_out = exported[Tag(0x0009, element)]
        assert str(element_out.value) == "", (
            "(0009,%04x) was a zero-length %s in the source and came out "
            "as %r" % (element, vr, element_out.value))


def test_a_none_written_by_hand_onto_a_text_vr_private_tag_is_not_invented(
        tmp_path):
    """The one route left by which `'None'` could still be fabricated.

    `_value_fits_vr('None', 'LO')` is `True`, so a text-VR private tag
    whose value was set to `None` in the graph -- by a `set_attr`, an
    anonymisation or a remediation rather than by the source file --
    would have reloaded under its own recorded VR carrying the word.
    The store no longer produces the string, so it takes the same
    fallback the fresh path takes and is reported instead.
    """
    src = tmp_path / "src"
    src.mkdir()
    _write_src(str(src))
    db = str(tmp_path / "byhand.db")
    out = tmp_path / "out"

    session = DicomSession(persistence_file=db)
    try:
        session.ingest(str(src))
        for patient in session.store.patients:
            for study in patient.studies:
                for series in study.series:
                    for instance in series.instances:
                        instance.set_attr("0009,1020", None)
        session.save()
    finally:
        session.close()

    session = DicomSession(persistence_file=db)
    try:
        session.export(str(out), format="dicom", show_progress=False)
    finally:
        session.close()

    exported = _read_only_written(out)
    assert Tag(0x0009, 0x1020) not in exported, (
        "a hand-set None on a text-VR private tag came back as %r"
        % (exported[Tag(0x0009, 0x1020)].value,))


def _loss_details(db_path):
    with sqlite3.connect(db_path) as conn:
        return [row[0] for row in conn.execute(
            "SELECT details FROM audit_log WHERE action_type='DATA_LOSS'")]


def test_a_none_among_siblings_is_the_same_loud_loss_on_both_paths(tmp_path):
    """The export half of the `[None, 'B']` edge, not only the store half.

    `_fallback_multivalue` returns `None` for the whole element as soon
    as one atom has no text encoding, and reports it as loss with its
    siblings -- "there is no half-written element in DICOM", which is
    that function's own documented rule and not something this fix
    changes. What matters here is that the reloaded path now reaches
    that rule at all: before, the `None` came back as the text `'None'`,
    every atom encoded, and the element was written as a two-value
    string with a word the source never said in it.
    """
    tag = "0009,1050"
    src = tmp_path / "src"
    src.mkdir()
    _write_src(str(src))

    fresh_db = str(tmp_path / "siblings_fresh.db")
    session = DicomSession(persistence_file=fresh_db)
    try:
        session.ingest(str(src))
        for patient in session.store.patients:
            for study in patient.studies:
                for series in study.series:
                    for instance in series.instances:
                        instance.set_attr(tag, [None, "B"])
        session.export(str(tmp_path / "fresh_out"), format="dicom",
                       show_progress=False)
    finally:
        session.close()

    reloaded_db = str(tmp_path / "siblings_reloaded.db")
    session = DicomSession(persistence_file=reloaded_db)
    try:
        session.ingest(str(src))
        for patient in session.store.patients:
            for study in patient.studies:
                for series in study.series:
                    for instance in series.instances:
                        instance.set_attr(tag, [None, "B"])
        session.save(sync=True)
    finally:
        session.close()

    session = DicomSession(persistence_file=reloaded_db)
    try:
        exported = session.store.patients[0].studies[0].series[0].instances[0]
        assert exported.attributes.get(tag) == [None, "B"], (
            "the reloaded graph did not carry the None atom, so the "
            "export comparison below would not be about this edge")
        session.export(str(tmp_path / "reloaded_out"), format="dicom",
                       show_progress=False)
    finally:
        session.close()

    fresh_losses = [d for d in _loss_details(fresh_db) if tag in d]
    reloaded_losses = [d for d in _loss_details(reloaded_db) if tag in d]

    assert len(fresh_losses) == 1, fresh_losses
    assert len(reloaded_losses) == 1, (
        "the reloaded export reported %d losses for %s where the fresh "
        "one reported 1" % (len(reloaded_losses), tag))
    assert fresh_losses == reloaded_losses, (
        "the two paths report the element differently: %r vs %r"
        % (fresh_losses, reloaded_losses))
