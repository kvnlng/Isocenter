"""A private element whose VR the file did not state is held as the file's bytes (#740).

A private data element reaches a reader with no VR in two ways: the file is
Implicit VR, which names none, or the file is Explicit VR and says `UN`,
which PS3.5 6.2.2 defines as "VR unknown". For a private creator pydicom's
private dictionary knows, pydicom's default `replace_un_with_known_vr`
relabels such an element on read with the dictionary's VR and decodes the
bytes under it. Measured at de5b26d9 on 3.12.14 and 3.14.7t:

- that VR was recorded and written by the Explicit VR export as though the
  file had stated it: `SIEMENS MR HEADER` (0019,xx08) `CS`, (0019,xx0A)
  `US`, (0019,xx0E) `FD`, and from an Explicit VR `UN` source `SIEMENS CSA
  HEADER` (0029,xx10) `OB`, which the #676 gate (Implicit VR, binary) missed;
- a value whose length does not fit the dictionary VR refused the whole
  file: six text bytes at (0019,xx12), `SL` in the dictionary, raised
  `BytesLengthException` out of ingest under either syntax.

Ruled (Q1 A, 2026-10-06): such an element is held as the source's bytes
and written `UN`, exactly as an unknown creator's is.

Wire VRs and bytes are read with `get_item`, which hands back the raw
element; `ds[tag]` would relabel them again.
"""
import json
import sqlite3
import struct

import numpy as np
import pydicom
import pytest
from pydicom.dataelem import RawDataElement
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import (ExplicitVRBigEndian, ExplicitVRLittleEndian,
                         ImplicitVRLittleEndian)

import isocenter.io_handlers as io_handlers
from isocenter.entities import Instance, PhiStatus
from isocenter.session import DicomSession

SC = "1.2.840.10008.5.1.4.1.1.7"
SOP = "1.2.826.0.1.740.1"
MR, CSA = "SIEMENS MR HEADER", "SIEMENS CSA HEADER"
RADWORKS = "Applicare/RadWorks/Version 5.0"     # (3109,xx0A) is DA
AGFA = "AGFA-AG_HPState"                        # (0071,xx18) is SQ

#: tag, pydicom's dictionary VR for `SIEMENS MR HEADER`, a value of it.
KNOWN = [
    ("0019,1008", "CS", "IMAGE NUM 4"),
    ("0019,1009", "LO", "1.0"),
    ("0019,100a", "US", 16),
    ("0019,100c", "IS", "1000"),
    ("0019,100e", "FD", [0.0, 0.5, -1.0]),
]
#: Six text bytes where pydicom's dictionary says `SL` (four bytes each).
MISMATCH = b"12.5 \x00"


def _tag(text):
    return pydicom.tag.Tag(int(text.replace(",", ""), 16))


def _source(folder, syntax, extra):
    folder.mkdir(parents=True, exist_ok=True)
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = SC
    meta.MediaStorageSOPInstanceUID = SOP
    meta.TransferSyntaxUID = syntax
    ds = Dataset()
    ds.file_meta = meta
    ds.SOPClassUID, ds.SOPInstanceUID = SC, SOP
    ds.PatientID, ds.PatientName = "P740", "Doe^John"
    ds.StudyInstanceUID = "1.2.826.0.1.740.2"
    ds.SeriesInstanceUID = "1.2.826.0.1.740.3"
    ds.Modality, ds.StudyDate, ds.StudyTime = "OT", "20200101", "120000"
    ds.SliceThickness = "1.500000"
    ds.Rows = ds.Columns = 4
    ds.BitsAllocated = ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelData = np.arange(16, dtype=np.uint16).tobytes()
    extra(ds)
    path = folder / "a.dcm"
    ds.save_as(str(path), enforce_file_format=True,
               implicit_vr=syntax == ImplicitVRLittleEndian,
               little_endian=syntax != ExplicitVRBigEndian)
    return path


def _mr_block(ds):
    """The `KNOWN` elements under the VR pydicom's dictionary names.

    Written under Implicit VR, no VR reaches the wire.
    """
    ds.add_new(0x00190010, "LO", MR)
    for tag, vr, value in KNOWN:
        ds.add_new(_tag(tag), vr, value)


def _raw(path, tag):
    """(wire VR, value bytes) of a top-level element as the file holds it."""
    raw = pydicom.dcmread(str(path)).get_item(_tag(tag), keep_deferred=True)
    return (None, None) if raw is None else (raw.VR, bytes(raw.value or b""))


def _session(tmp_path, src, *, reopen=False, config=None, name="s"):
    """An open session over `src`, de-identified under `config`, maybe reopened."""
    db = tmp_path / f"{name}.db"
    session = DicomSession(str(db))
    session.ingest(str(src.parent))
    if config is not None:
        path = tmp_path / f"{name}.yaml"   # JSON is YAML
        path.write_text(json.dumps(config), encoding="utf-8")
        session.load_config(str(path))
        session.anonymize(session.audit())
    if reopen:
        session.save(sync=True)
        session.close()
        session = DicomSession(str(db))
    return session


def _instance(session):
    (patient,) = session.store.patients
    return patient.studies[0].series[0].instances[0]


def _export(session, out, compress):
    summary = session.export(str(out), use_compression=compress,
                             show_progress=False)
    assert summary.written == 1, summary.failures
    (path,) = list(out.rglob("*.dcm"))
    return path


def _rows(session):
    return ([tuple(r) for r in session.store_backend.get_audit_errors()],
            [tuple(r) for r in session.store_backend.get_audit_losses()])


reopened = pytest.mark.parametrize("reopen", [False, True],
                                   ids=["live", "reopened"])


@reopened
def test_an_implicit_source_element_of_a_known_creator_is_held_and_written_un(
        tmp_path, reopen):
    """Spec test 1, through a real `ingest()`: nothing converts the element
    before `populate_attrs` reads it raw.

    Killing mutations: the `UN` substitution deleted; the element read
    (`_read_element`) before `get_item`.
    """
    src = _source(tmp_path / "src", ImplicitVRLittleEndian, _mr_block)
    source = {tag: _raw(src, tag) for tag, _, _ in KNOWN}
    assert all(vr is None for vr, _ in source.values()), "setup: no wire VR"

    session = _session(tmp_path, src, reopen=reopen)
    try:
        inst = _instance(session)
        for tag, _, _ in KNOWN:
            held = inst.attributes[tag]
            assert isinstance(held, bytes), (tag, type(held))
            assert held == source[tag][1], tag
            assert tag not in inst.attribute_vrs, (tag, inst.attribute_vrs)
        assert inst.attributes["0019,0010"] == MR
        assert inst.attribute_vrs["0019,0010"] == "LO"
        explicit = _export(session, tmp_path / "j2k", True)
        native = _export(session, tmp_path / "native", False)
        rows, losses = _rows(session)
    finally:
        session.close()

    assert not pydicom.dcmread(str(explicit)) \
        .file_meta.TransferSyntaxUID.is_implicit_VR
    for tag, _, _ in KNOWN:
        assert _raw(explicit, tag) == ("UN", source[tag][1]), tag
        # No VR on the wire, and the bytes the source had.
        assert _raw(native, tag) == (None, source[tag][1]), tag
    assert _raw(explicit, "0019,0010")[0] == "LO"
    assert not [r for r in rows + losses if "0019," in r[2]], (rows, losses)


@reopened
def test_an_explicit_un_is_no_statement_of_a_vr(tmp_path, reopen):
    """Spec test 2: wire `UN` for a known creator, binary included.

    Killing mutation: a predicate that reads Implicit VR only
    (`raw.VR is None`).
    """
    stated_un = [("0019,1008", b"IMAGE NUM 4 "),
                 ("0019,100a", struct.pack("<H", 16)),
                 ("0029,1010", b"SV10\x04\x03\x02\x01")]

    def extra(ds):
        ds.add_new(0x00190010, "LO", MR)
        ds.add_new(0x00290010, "LO", CSA)
        for tag, value in stated_un:
            ds.add_new(_tag(tag), "UN", value)

    src = _source(tmp_path / "src", ExplicitVRLittleEndian, extra)
    for tag, value in stated_un:
        assert _raw(src, tag) == ("UN", value), "setup: the wire says UN"

    session = _session(tmp_path, src, reopen=reopen)
    try:
        inst = _instance(session)
        for tag, value in stated_un:
            assert inst.attributes[tag] == value
            assert tag not in inst.attribute_vrs
        explicit = _export(session, tmp_path / "j2k", True)
        rows, losses = _rows(session)
    finally:
        session.close()

    for tag, value in stated_un:
        assert _raw(explicit, tag) == ("UN", value), tag
    assert not [r for r in rows + losses
                if "0019," in r[2] or "0029," in r[2]], (rows, losses)


@reopened
@pytest.mark.parametrize("syntax", [ImplicitVRLittleEndian,
                                    ExplicitVRLittleEndian],
                         ids=["implicit", "explicit-un"])
def test_bytes_that_do_not_fit_the_dictionary_vr_no_longer_refuse_the_file(
        tmp_path, syntax, reopen):
    """Spec test 3. On main the whole file was refused (`BytesLengthException`).

    Killing mutation: the `UN` substitution deleted.
    """
    def extra(ds):
        ds.add_new(0x00190010, "LO", MR)
        ds.add_new(0x00191012, "UN", MISMATCH)

    src = _source(tmp_path / "src", syntax, extra)
    session = _session(tmp_path, src, reopen=reopen)
    try:
        assert _instance(session).attributes["0019,1012"] == MISMATCH
        explicit = _export(session, tmp_path / "j2k", True)
        rows, losses = _rows(session)
    finally:
        session.close()

    assert _raw(explicit, "0019,1012") == ("UN", MISMATCH)
    assert not [r for r in rows if r[1] == "ERROR"], rows
    assert not [r for r in rows + losses if "0019," in r[2]], (rows, losses)


@reopened
def test_a_zero_length_element_is_held_the_same_way(tmp_path, reopen):
    """An empty value is still no statement of a VR.

    pydicom's `get_item` reads and converts a raw element whose value is
    None, which is every zero-length one, unless told to keep it; asked
    plainly, the empty element was relabelled like any other (and an
    empty LUT Data with no descriptor refused its file again). Killing
    mutation: `keep_deferred=True` dropped.
    """
    def extra(ds):
        ds.add_new(0x00190010, "LO", MR)
        ds.add_new(0x00191008, "CS", "")
        ds.add_new(0x0019100A, "US", None)
        ds.add_new(0x00110010, "LO", "ACME")
        ds.add_new(0x00111001, "LO", "")

    src = _source(tmp_path / "src", ImplicitVRLittleEndian, extra)
    assert _raw(src, "0019,1008") == (None, b"")

    session = _session(tmp_path, src, reopen=reopen)
    try:
        inst = _instance(session)
        # As an unknown creator's empty element is, and always was.
        assert "0011,1001" in inst.attributes
        for tag in ("0019,1008", "0019,100a"):
            assert tag in inst.attributes
            assert inst.attributes[tag] == inst.attributes["0011,1001"]
            assert inst.attributes[tag] is None, (tag, inst.attributes[tag])
            assert tag not in inst.attribute_vrs
        explicit = _export(session, tmp_path / "j2k", True)
    finally:
        session.close()

    assert _raw(explicit, "0019,1008") == ("UN", b"")
    assert _raw(explicit, "0019,100a") == ("UN", b"")


def test_an_unknown_creator_is_unchanged(tmp_path):
    """Spec test 4, control: what a known creator's element now matches."""
    def extra(ds):
        ds.add_new(0x00110010, "LO", "ACME")
        ds.add_new(0x00111001, "LO", "ACME TEXT")

    src = _source(tmp_path / "src", ImplicitVRLittleEndian, extra)
    session = _session(tmp_path, src)
    try:
        inst = _instance(session)
        assert inst.attributes["0011,1001"] == b"ACME TEXT "
        assert "0011,1001" not in inst.attribute_vrs
        explicit = _export(session, tmp_path / "j2k", True)
    finally:
        session.close()
    assert _raw(explicit, "0011,1001") == ("UN", b"ACME TEXT ")


@reopened
def test_a_vr_the_file_states_is_recorded_and_written(tmp_path, reopen):
    """Spec test 5, control: an Explicit VR source that says CS, US and OB.

    Killing mutation: the predicate without its wire-VR test (every private
    element held as `UN`).
    """
    def extra(ds):
        _mr_block(ds)
        ds.add_new(0x00290010, "LO", CSA)
        ds.add_new(0x00291010, "OB", b"SV10\x04\x03\x02\x01")

    src = _source(tmp_path / "src", ExplicitVRLittleEndian, extra)
    session = _session(tmp_path, src, reopen=reopen)
    try:
        inst = _instance(session)
        assert inst.attributes["0019,1008"] == "IMAGE NUM 4"
        assert int(inst.attributes["0019,100a"]) == 16
        assert {tag: inst.attribute_vrs[tag] for tag in
                ("0019,1008", "0019,100a", "0019,100e", "0029,1010")} == {
            "0019,1008": "CS", "0019,100a": "US", "0019,100e": "FD",
            "0029,1010": "OB"}
        explicit = _export(session, tmp_path / "j2k", True)
    finally:
        session.close()

    assert _raw(explicit, "0019,1008") == ("CS", b"IMAGE NUM 4 ")
    assert _raw(explicit, "0019,100a") == ("US", struct.pack("<H", 16))
    assert _raw(explicit, "0019,100e") == (
        "FD", struct.pack("<3d", 0.0, 0.5, -1.0))
    assert _raw(explicit, "0029,1010") == ("OB", b"SV10\x04\x03\x02\x01")


@reopened
def test_a_nested_element_is_held_the_same_way(tmp_path, reopen):
    """Spec test 6: inside a standard sequence item of an Implicit VR file.

    Killing mutation: a top-level-only rule.
    """
    def extra(ds):
        item = Dataset()
        item.ReferencedSOPClassUID = SC
        item.ReferencedSOPInstanceUID = "1.2.826.0.1.740.9"
        item.add_new(0x00190010, "LO", MR)
        item.add_new(0x0019100A, "US", 16)
        ds.ReferencedImageSequence = Sequence([item])

    src = _source(tmp_path / "src", ImplicitVRLittleEndian, extra)
    session = _session(tmp_path, src, reopen=reopen)
    try:
        (item,) = _instance(session).sequences["0008,1140"].items
        assert item.attributes["0019,100a"] == struct.pack("<H", 16)
        assert "0019,100a" not in item.attribute_vrs
        assert item.attribute_vrs["0019,0010"] == "LO"
        explicit = _export(session, tmp_path / "j2k", True)
    finally:
        session.close()

    (written,) = pydicom.dcmread(str(explicit)).ReferencedImageSequence
    raw = written.get_item(0x0019100A)
    assert (raw.VR, bytes(raw.value)) == ("UN", struct.pack("<H", 16))


@reopened
@pytest.mark.parametrize("undefined", [True, False],
                         ids=["undefined-length", "defined-length"])
def test_a_known_creators_sequence_is_still_a_sequence(tmp_path, undefined,
                                                       reopen):
    """Spec test 7. An undefined length is a sequence on the wire and stays
    pydicom's to parse; a defined-length one takes the byte-exact re-parse
    an unknown creator's takes.

    Killing mutation: the undefined-length clause dropped.
    """
    def extra(ds):
        item = Dataset()
        item.PatientName = "NESTED^NAME"
        item.CodeValue = "X"
        ds.add_new(0x00710010, "LO", AGFA)
        ds.add_new(0x00711018, "SQ", Sequence([item]))
        ds[0x00711018].is_undefined_length = undefined

    src = _source(tmp_path / "src", ImplicitVRLittleEndian, extra)
    # pydicom parses an undefined-length element as a sequence while it
    # reads the file; a defined-length one is still raw.
    raw = pydicom.dcmread(str(src)).get_item(0x00711018)
    assert isinstance(raw, RawDataElement) is not undefined, "setup"

    session = _session(tmp_path, src, reopen=reopen)
    try:
        inst = _instance(session)
        assert "0071,1018" not in inst.attributes
        (item,) = inst.sequences["0071,1018"].items
        assert str(item.attributes["0010,0010"]) == "NESTED^NAME"
        assert item.attributes["0008,0100"] == "X"
        rows, losses = _rows(session)
        native = _export(session, tmp_path / "native", False)
    finally:
        session.close()

    assert not [r for r in rows + losses if "0071," in r[2]], (rows, losses)
    (written,) = pydicom.dcmread(str(native))[0x00711018].value
    assert str(written.PatientName) == "NESTED^NAME"


@pytest.mark.parametrize("group, element, vr, length, unstated", [
    (0x0019, 0x1008, None, 12, True),
    (0x0019, 0x1008, "UN", 12, True),
    (0x0019, 0x1008, "CS", 12, False),          # the file's own statement
    (0x0019, 0x0010, None, 18, False),          # a private creator
    (0x0019, 0x0fff, None, 2, False),
    (0x0019, 0x1000, None, 12, True),           # the first data element
    (0x0018, 0x1008, None, 12, False),          # a standard tag
    (0x0071, 0x1018, None, 0xFFFFFFFF, False),  # a sequence on the wire
    (0x0071, 0x1018, "UN", 0xFFFFFFFF, False),
])
def test_which_raw_elements_are_held_as_un(group, element, vr, length,
                                           unstated):
    """The predicate, clause by clause; pydicom parses an undefined-length
    element itself, so only a direct call reaches that clause.

    Killing mutations: each clause dropped.
    """
    raw = RawDataElement(pydicom.tag.Tag(group, element), vr, length,
                         b"IMAGE NUM 4 ", 0, vr is None, True)
    assert io_handlers._vr_unstated_private(raw) is unstated
    converted = pydicom.dataelem.DataElement(
        pydicom.tag.Tag(group, element), vr or "UN", b"IMAGE NUM 4 ")
    assert io_handlers._vr_unstated_private(converted) is False


def test_a_zero_length_raw_element_is_one_whose_length_is_zero():
    """Zero length is read off the length, never off a value of None.

    A raw element pydicom has deferred also has a value of None, with the
    length the file gave it; it is not in hand, and is left to pydicom to
    read. Killing mutation: the deferred clause dropped.
    """
    tag = pydicom.tag.Tag(0x0019, 0x1008)
    empty = RawDataElement(tag, None, 0, None, 0, True, True)
    deferred = RawDataElement(tag, None, 12, None, 0, True, True)
    assert io_handlers._vr_unstated_private(empty) is True
    assert io_handlers._vr_unstated_private(deferred) is False


def test_a_deferred_element_is_not_taken_for_an_empty_one(tmp_path):
    """No read in this library defers a value; one that did must not lose it.

    `get_item(keep_deferred=True)` hands back a deferred raw element with
    a value of None. Read as zero-length, a long private value would be
    held as empty with no row. Killing mutation: zero length keyed on
    `raw.value is None`.
    """
    def extra(ds):
        ds.add_new(0x00190010, "LO", MR)
        ds.add_new(0x00191009, "LO", "A LONG PRIVATE VALUE")
        ds.add_new(0x00191008, "CS", "")

    src = _source(tmp_path / "src", ImplicitVRLittleEndian, extra)
    ds = pydicom.dcmread(str(src), defer_size=8)
    raw = ds.get_item(0x00191009, keep_deferred=True)
    assert isinstance(raw, RawDataElement) and raw.value is None \
        and raw.length == 20, "setup: the value is deferred"

    item = Instance(SOP, SC, 1)
    io_handlers.populate_attrs(ds, item, [])

    held = item.attributes["0019,1009"]
    assert held is not None
    assert (held.decode() if isinstance(held, bytes) else str(held)).strip() \
        == "A LONG PRIVATE VALUE"
    # The empty one beside it is still held as empty.
    assert item.attributes["0019,1008"] is None


def test_a_known_creators_value_over_the_limit_is_dropped_as_any_un_is(
        tmp_path):
    """Spec test 8: 70000 bytes at (0051,xx19), `LO` in the dictionary.

    On main it was kept as text and written `UT` with a re-VR `WARNING`
    row. Killing mutation: the `UN` substitution deleted.
    """
    def extra(ds):
        ds.add_new(0x00510010, "LO", MR)
        ds.add_new(0x00511019, "LT", "y" * 70000)

    src = _source(tmp_path / "src", ImplicitVRLittleEndian, extra)
    assert _raw(src, "0051,1019") == (None, b"y" * 70000)

    session = _session(tmp_path, src)
    try:
        assert "0051,1019" not in _instance(session).attributes
        _export(session, tmp_path / "j2k", True)
        rows, losses = _rows(session)
    finally:
        session.close()

    named = [r for r in losses if "0051,1019" in r[2]]
    assert len(named) == 1, losses
    assert named[0][3] == "PRIVATE"
    assert "65534-byte retention threshold" in named[0][2]
    assert not [r for r in rows if "0051,1019" in r[2]], rows


@reopened
def test_a_private_creator_keeps_its_lo(tmp_path, reopen):
    """Spec test 9: (gggg,00xx) is read as pydicom reads it, never `UN`.

    Killing mutation: the rule widened below element 0x1000.
    """
    src = _source(tmp_path / "src", ImplicitVRLittleEndian, _mr_block)
    session = _session(tmp_path, src, reopen=reopen)
    try:
        inst = _instance(session)
        assert inst.attributes["0019,0010"] == MR
        assert isinstance(inst.attributes["0019,0010"], str)
        assert inst.attribute_vrs["0019,0010"] == "LO"
        explicit = _export(session, tmp_path / "j2k", True)
    finally:
        session.close()
    assert _raw(explicit, "0019,0010") == ("LO", MR.encode() + b" ")


def test_a_standard_element_of_an_implicit_source_is_still_decoded(tmp_path):
    """Spec test 10, control. Killing mutation: the odd-group test dropped."""
    src = _source(tmp_path / "src", ImplicitVRLittleEndian, _mr_block)
    session = _session(tmp_path, src)
    try:
        inst = _instance(session)
        held = inst.attributes["0018,0050"]
        assert not isinstance(held, bytes), type(held)
        assert float(held) == 1.5
        assert inst.attributes["0008,0060"] == "OT"
        explicit = _export(session, tmp_path / "j2k", True)
    finally:
        session.close()
    assert _raw(explicit, "0018,0050") == ("DS", b"1.500000")


# --- What the ruling costs, pinned so that it is seen (Q2) ------------------

def _private_date(ds):
    ds.add_new(0x31090010, "LO", RADWORKS)
    ds.add_new(0x3109100A, "DA", "20200115")


KEEP_PRIVATE = {"remove_private_tags": False}


@pytest.mark.parametrize("syntax, temporal", [
    # The file stated DA: the date is read, found as it was, and the marker
    # is withheld.
    (ExplicitVRLittleEndian, None),
    # The file stated no VR: the date is `UN` bytes and is not read.
    (ImplicitVRLittleEndian, "MODIFIED"),
], ids=["stated-da", "unstated"])
def test_a_private_date_whose_vr_was_not_stated_does_not_stop_modified(
        tmp_path, syntax, temporal):
    """Spec test 11. **This pins a documented weakening; review it as one.**

    `(0028,0303)` reads a private date only when its VR is recorded. A
    known creator's date from an Implicit VR source used to be recorded
    under pydicom's dictionary VR and so withheld `MODIFIED`; it is now
    `UN` bytes, as an unknown creator's always was, and `MODIFIED` is
    written beside a private date left as found. `docs/configuration.md`
    says so, and it is reachable only with `remove_private_tags: false`.
    """
    src = _source(tmp_path / "src", syntax, _private_date)
    session = _session(tmp_path, src, config=KEEP_PRIVATE)
    try:
        native = _export(session, tmp_path / "native", False)
    finally:
        session.close()

    ds = pydicom.dcmread(str(native))
    assert ds.StudyDate != "20200101", "setup: the Study Date was shifted"
    assert bytes(ds.get_item(0x3109100A).value) == b"20200115"
    assert ds.get("LongitudinalTemporalInformationModified") == temporal


def test_a_shift_rule_on_such_a_private_key_declines(tmp_path):
    """Spec test 12, the fact behind Q2: the rule fails closed.

    A `JITTER` on a private key of a known creator shifted the date pydicom
    decoded from an Implicit VR source. It now meets `UN` bytes: the
    proposal is declined, the instance stays IDENTIFIED, and the run grades
    REVIEW_REQUIRED. The date is exported as found, and said.
    """
    src = _source(tmp_path / "src", ImplicitVRLittleEndian, _private_date)
    config = {**KEEP_PRIVATE, "phi_tags": {
        "3109,100a": {"action": "JITTER", "name": "Receive Date"}}}
    report = tmp_path / "report.md"
    session = _session(tmp_path, src, config=config)
    try:
        inst = _instance(session)
        assert inst.attributes["3109,100a"] == b"20200115"
        assert inst.phi_status is PhiStatus.IDENTIFIED
        _export(session, tmp_path / "native", False)
        session.generate_report(str(report))
        rows, _losses = _rows(session)
    finally:
        session.close()

    with sqlite3.connect(str(tmp_path / "s.db")) as conn:
        log = [tuple(r) for r in conn.execute(
            "SELECT action_type, details FROM audit_log")]
    declined = [r for r in log if "3109,100a" in r[1]]
    assert [r[0] for r in declined] == ["REMEDIATION_DECLINED"], declined
    grade = [line for line in report.read_text().splitlines()
             if "Grade Basis" in line]
    assert grade and "REVIEW_REQUIRED" in grade[0], grade


def _private_date_said_un(ds):
    ds.add_new(0x31090010, "LO", RADWORKS)
    ds.add_new(0x3109100A, "UN", b"20200115")


@pytest.mark.parametrize("action", ["SHIFT", "JITTER"])
@pytest.mark.parametrize("syntax, extra", [
    (ImplicitVRLittleEndian, _private_date),
    (ExplicitVRLittleEndian, _private_date_said_un),
], ids=["implicit", "explicit-un"])
def test_a_declined_shift_exports_the_date_the_source_held(
        tmp_path, syntax, extra, action):
    """**What the declined shift writes, in both export arms.**

    The 1.0.0rc13 `**Output:**` line for #740 did not say this. With
    `remove_private_tags: false` and a `SHIFT` or `JITTER` rule on a known
    creator's private date whose VR the source did not state, `main` before
    #740 shifted the date pydicom decoded and exported the shifted one
    (`DA` under Explicit VR) with a `REMEDIATION_SHIFT_DATE` row, under
    PASS. Now the rule meets `UN` bytes and is declined: both arms carry
    the source's own date, beside a Study Date that *was* shifted, with one
    `REMEDIATION_DECLINED` row and no shift row, and the run grades
    REVIEW_REQUIRED.

    Killing mutation: the `UN` substitution deleted (the date is shifted
    in both arms, a shift row, PASS).
    """
    src = _source(tmp_path / "src", syntax, extra)
    config = {**KEEP_PRIVATE, "phi_tags": {
        "3109,100a": {"action": action, "name": "Receive Date"}}}
    session = _session(tmp_path, src, config=config)
    try:
        native = _export(session, tmp_path / "native", False)
        explicit = _export(session, tmp_path / "j2k", True)
        grade = _grade(session, tmp_path)
    finally:
        session.close()

    # The exported value first: it is the statement the CHANGELOG makes.
    assert _raw(native, "3109,100a") == (None, b"20200115")
    assert _raw(explicit, "3109,100a") == ("UN", b"20200115")
    for path in (native, explicit):
        assert pydicom.dcmread(str(path)).StudyDate != "20200101", (
            "the Study Date beside it is shifted")
    with sqlite3.connect(str(tmp_path / "s.db")) as conn:
        log = [tuple(r) for r in conn.execute(
            "SELECT action_type, details FROM audit_log")]
    assert [r[0] for r in log if "3109,100a" in r[1]] == [
        "REMEDIATION_DECLINED"], log
    assert grade == "REVIEW_REQUIRED"


@pytest.mark.parametrize("action", ["SHIFT", "JITTER"])
def test_a_private_date_whose_vr_the_file_states_is_still_shifted(
        tmp_path, action):
    """The control, and the way out the CHANGELOG names: the same rule on
    the same element from an Explicit VR source that says `DA` shifts it,
    as before, and the run grades PASS."""
    src = _source(tmp_path / "src", ExplicitVRLittleEndian, _private_date)
    config = {**KEEP_PRIVATE, "phi_tags": {
        "3109,100a": {"action": action, "name": "Receive Date"}}}
    session = _session(tmp_path, src, config=config)
    try:
        explicit = _export(session, tmp_path / "j2k", True)
        grade = _grade(session, tmp_path)
    finally:
        session.close()

    vr, value = _raw(explicit, "3109,100a")
    assert vr == "DA" and len(value) == 8 and value != b"20200115"
    with sqlite3.connect(str(tmp_path / "s.db")) as conn:
        log = [tuple(r) for r in conn.execute(
            "SELECT action_type, details FROM audit_log")]
    assert [r[0] for r in log if "3109,100a" in r[1]] == [
        "REMEDIATION_SHIFT_DATE"], log
    assert grade == "PASS"


# --- What the ruling costs at ingest, under every configuration (Q-R1 A) ----
#
# The size gate and the byte-order row are ingest's, written before any
# configuration exists, and a PRIVATE loss or a WARNING row grades whatever
# the sweep then removes. The grade is read from the report file.

def _grade(session, tmp_path):
    report = tmp_path / "report.md"
    session.generate_report(str(report))
    (line,) = [line for line in report.read_text().splitlines()
               if "Grade Basis" in line]
    return "REVIEW_REQUIRED" if "REVIEW_REQUIRED" in line else (
        "PASS" if "PASS" in line else line)


def _long_text(ds):
    ds.add_new(0x00510010, "LO", MR)
    ds.add_new(0x00511019, "LT", "y" * 70000)       # dictionary: LO


def _long_numbers(ds):
    ds.add_new(0x00190010, "LO", MR)
    ds.add_new(0x0019100E, "FD", [0.25] * 9000)      # 72000 bytes


BASIC = {"privacy_profile": "basic"}


@pytest.mark.parametrize("extra, tag", [
    (_long_text, "0051,1019"), (_long_numbers, "0019,100e")],
    ids=["text", "numbers"])
@pytest.mark.parametrize("config", [
    BASIC, {**BASIC, "remove_private_tags": False}],
    ids=["default-sweep", "private-kept"])
def test_a_known_creators_value_over_the_limit_costs_the_run_its_pass(
        tmp_path, extra, tag, config):
    """**The accepted cost (Q-R1 A), pinned so it is seen.**

    A known creator's value over 65534 bytes, from a source that stated
    no VR for it, is dropped at ingest with a PRIVATE `DATA_LOSS` row, as
    an unknown creator's is. On `main` it was held under pydicom's VR: the
    default sweep removed it and the run graded PASS, and with private tags
    kept the number list was exported under PASS (the text was written
    `UT` with a `WARNING` row). Now every one of the four grades
    REVIEW_REQUIRED, and with private tags kept the value is not exported.

    Killing mutation: the `UN` substitution deleted (three of the four go
    back to PASS).
    """
    src = _source(tmp_path / "src", ImplicitVRLittleEndian, extra)
    session = _session(tmp_path, src, config=config)
    try:
        held = tag in _instance(session).attributes
        explicit = _export(session, tmp_path / "j2k", True)
        _rows_, losses = _rows(session)
        grade = _grade(session, tmp_path)
    finally:
        session.close()

    # The grade first: it is the statement the CHANGELOG makes.
    assert grade == "REVIEW_REQUIRED"
    named = [r for r in losses if tag in r[2]]
    assert len(named) == 1 and named[0][3] == "PRIVATE", losses
    assert "65534-byte retention threshold" in named[0][2]
    assert not held
    assert _raw(explicit, tag) == (None, None)


def test_a_big_endian_source_that_says_un_draws_the_byte_order_row(tmp_path):
    """An Explicit VR Big Endian `UN` of a known creator is `UN` bytes whose
    word size nothing states, so ingest cannot convert them and says so
    with one `WARNING` row per element, as for an unknown creator. On
    `main` pydicom decoded them under its dictionary VR and no row was
    written. The run grades REVIEW_REQUIRED under any configuration.

    Killing mutation: the `UN` substitution deleted (no row, PASS).
    """
    def extra(ds):
        ds.add_new(0x00190010, "LO", MR)
        ds.add_new(0x0019100A, "UN", struct.pack(">H", 16))
        ds.add_new(0x00191008, "UN", b"IMAGE NUM 4 ")

    src = _source(tmp_path / "src", ExplicitVRBigEndian, extra)
    session = _session(tmp_path, src, config=BASIC)
    try:
        rows, _losses = _rows(session)
        grade = _grade(session, tmp_path)
    finally:
        session.close()

    assert grade == "REVIEW_REQUIRED"
    named = sorted(r[2] for r in rows if "0019,10" in r[2])
    assert len(named) == 2, rows
    assert "0019,1008" in named[0] and "0019,100a" in named[1]
    assert all("byte order" in text for text in named), named
    assert all(r[1] == "WARNING" for r in rows if "0019,10" in r[2]), rows


@pytest.mark.parametrize("rule, wire", [
    ({"action": "REPLACE", "value": "SITE"}, ("LO", b"SITE")),
    ({"action": "EMPTY"}, ("UN", b"")),
], ids=["valued-replace", "empty"])
def test_a_rule_on_such_a_key_writes_the_vr_of_an_unstated_element(
        tmp_path, rule, wire):
    """With private tags kept, a valued `REPLACE` on such a key is written
    `LO` (the exporter's fallback for text with no recorded VR) and an
    `EMPTY` as a zero-length `UN`. On `main` both went out under the
    dictionary's `CS`, which the file had never stated.

    Killing mutation: the `UN` substitution deleted (`CS`).
    """
    src = _source(tmp_path / "src", ImplicitVRLittleEndian, _mr_block)
    config = {**BASIC, "remove_private_tags": False, "phi_tags": {
        "0019,1008": {**rule, "name": "Private text"}}}
    session = _session(tmp_path, src, config=config)
    try:
        explicit = _export(session, tmp_path / "j2k", True)
    finally:
        session.close()

    assert _raw(explicit, "0019,1008") == wire
