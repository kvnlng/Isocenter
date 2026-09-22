"""A kept private binary element is written under the VR it was read with (#676).

With `remove_private_tags: false`, a private `OB`, `OW`, `OL`, `OF`, `OD`
or `OV` element was written `UN` in every explicit-VR export, live and
reopened -- `OB` too, not only the word VRs the issue named. Three causes,
measured at 63a64158: `populate_attrs`' binary arm `continue`d before
`_record_private_vr`, which refused a bytes value anyway ("there is also
nowhere to keep it"); the root of `attributes_json` carried no `__vrs__`,
and a private bytes value is stored there rather than in the private-tag
table whose `value_rep` column holds the others' VRs; and
`_value_fits_vr` answered False for every bytes value.

The owner's ruling on #676: private binary is the user's to keep or to
remove, through `remove_private_tags`, and a kept element is written
faithfully -- under its recorded VR when its bytes are a whole number of
that VR's words (PS3.5 6.2), and otherwise `UN`, named in the instance's
one re-VR `WARNING` sentence (#571). pydicom writes a ragged `OL` or `OD`
without a word, so the gate refuses those lengths itself.

Only an explicit-VR source records a binary VR (Implicit VR states none,
whatever pydicom's private dictionary guesses), and only an explicit-VR
output shows one: here the JPEG 2000 arm of a
pixel-bearing instance.
"""
import itertools
import sqlite3
import json
from pathlib import Path

import numpy as np
import pydicom
import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import (ExplicitVRBigEndian, ExplicitVRLittleEndian,
                         ImplicitVRLittleEndian)

from isocenter.entities import Instance
from isocenter.io_handlers import (DicomExporter, ExportContext, _ReVr,
                                   _export_instance_worker, _value_fits_vr)
from isocenter.session import DicomSession

SC = "1.2.840.10008.5.1.4.1.1.7"
SOP = "1.2.826.0.1.676.1"

#: The root private binary elements: tag, VR, the value the source writes.
ROOT = {
    "OW": (0x00091001, bytes(range(8))),
    "OL": (0x00091002, bytes(range(8))),
    "OF": (0x00091003, bytes(range(8))),
    "OD": (0x00091004, bytes(range(16))),
    "OB": (0x00091005, bytes(range(5)) + b"\x00"),
    "OV": (0x00091006, bytes(range(16))),
}
UN_TAG = (0x00091007, bytes(range(6)))
NESTED_PRIVATE = (0x00091010, 0x00091008, "OW", bytes(range(4)))
NESTED_STANDARD = (0x00111001, "OF", bytes(range(8)))
#: The word width of each VR, for the Big Endian source's J7 conversion.
WIDTH = {"OB": 1, "OW": 2, "OL": 4, "OF": 4, "OD": 8, "OV": 8}


def _source(folder: Path, syntax=ExplicitVRLittleEndian, root=ROOT,
            extra=(), nested_extra=()) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = SC
    meta.MediaStorageSOPInstanceUID = SOP
    meta.TransferSyntaxUID = syntax
    ds = Dataset()
    ds.file_meta = meta
    ds.SOPClassUID, ds.SOPInstanceUID = SC, SOP
    ds.PatientID, ds.PatientName = "P676", "Doe^John"
    ds.StudyInstanceUID = "1.2.826.0.1.676.2"
    ds.SeriesInstanceUID = "1.2.826.0.1.676.3"
    ds.Modality, ds.StudyDate = "OT", "20200101"
    ds.add_new(0x00090010, "LO", "ACME")
    for vr, (tag, value) in root.items():
        ds.add_new(tag, vr, value)
    ds.add_new(UN_TAG[0], "UN", UN_TAG[1])
    for tag, vr, value in extra:
        ds.add_new(tag, vr, value)
    inner = Dataset()
    inner.add_new(0x00090010, "LO", "ACME")
    inner.add_new(NESTED_PRIVATE[1], NESTED_PRIVATE[2], NESTED_PRIVATE[3])
    ds.add_new(NESTED_PRIVATE[0], "SQ", Sequence([inner]))
    std = Dataset()
    std.ReferencedSOPInstanceUID = "1.2.826.0.1.676.9"
    std.add_new(0x00110010, "LO", "ACME")
    std.add_new(NESTED_STANDARD[0], NESTED_STANDARD[1], NESTED_STANDARD[2])
    for tag, vr, value in nested_extra:
        std.add_new(tag, vr, value)
    ds.ReferencedImageSequence = Sequence([std])
    ds.Rows = ds.Columns = 8
    ds.BitsAllocated = ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelData = np.arange(64, dtype=np.uint8).tobytes()
    path = folder / "src.dcm"
    ds.save_as(str(path), enforce_file_format=True,
               little_endian=syntax != ExplicitVRBigEndian,
               implicit_vr=syntax == ImplicitVRLittleEndian)
    return path


def _only(folder: Path) -> Path:
    (path,) = list(folder.rglob("*.dcm"))
    return path


def _little_endian(vr, value):
    """The source's words in little-endian order: what J7 stores."""
    width = WIDTH[vr]
    if width == 1:
        return value
    return np.frombuffer(value, dtype=f">u{width}").astype(f"<u{width}").tobytes()


def _warnings(db):
    with sqlite3.connect(db) as conn:
        return [d for (d,) in conn.execute(
            "SELECT details FROM audit_log WHERE action_type = 'WARNING'")]


def _written(tmp_path, syntax=ExplicitVRLittleEndian, reopen=False, **kw):
    _source(tmp_path / "src", syntax, **kw)
    db = str(tmp_path / "s.db")
    with DicomSession(db) as session:
        session.ingest(str(tmp_path / "src"))
        if not reopen:
            session.export(str(tmp_path / "out"), use_compression=True)
        session.save(sync=True)
    if reopen:
        with DicomSession(db) as session:
            session.export(str(tmp_path / "out"), use_compression=True)
    out = pydicom.dcmread(str(_only(tmp_path / "out")))
    assert out.file_meta.TransferSyntaxUID.is_implicit_VR is False
    return out, db


@pytest.mark.parametrize("syntax", [ExplicitVRLittleEndian, ExplicitVRBigEndian],
                         ids=["LE", "BE"])
@pytest.mark.parametrize("vr", sorted(ROOT))
def test_each_private_binary_vr_is_written_as_read(tmp_path, vr, syntax):
    """Killing mutations: `_record_private_vr` still refusing bytes; the
    binary arm not calling it (then `OV`, which takes the generic arm,
    passes and the other five fail); `_value_fits_vr` refusing bytes; a
    wrong width (with the ragged test)."""
    out, db = _written(tmp_path, syntax)
    tag, value = ROOT[vr]
    element = out[tag]
    assert element.VR == vr
    expected = value if syntax == ExplicitVRLittleEndian else _little_endian(vr, value)
    assert bytes(element.value) == expected
    # No re-VR sentence. The Big Endian source draws one other row, at
    # ingest, for its `UN` element, whose byte order nothing can know.
    assert [w for w in _warnings(db) if w.startswith("Private element")] == []


def test_the_vr_survives_a_save_and_a_reopen(tmp_path):
    """At the root, inside a private SQ, inside a standard SQ item.
    Killing mutations: the root `__vrs__` not written (live passes, the
    reopened export writes `UN`); written but not applied on load."""
    out, db = _written(tmp_path, reopen=True)
    for vr, (tag, value) in ROOT.items():
        assert (out[tag].VR, bytes(out[tag].value)) == (vr, value), vr
    (inner,) = out[NESTED_PRIVATE[0]].value
    assert inner[NESTED_PRIVATE[1]].VR == NESTED_PRIVATE[2]
    (std,) = out.ReferencedImageSequence
    assert std[NESTED_STANDARD[0]].VR == NESTED_STANDARD[1]
    assert out[UN_TAG[0]].VR == "UN"
    assert _warnings(db) == []


def _hand_built(extra, vrs):
    inst = Instance("1.2.826.0.1.676.5", SC, 1)
    inst.file_path = None
    for tag, value in (("0008,0020", "20230101"), ("0008,0060", "OT"),
                       ("0028,0002", 1), ("0028,0004", "MONOCHROME2"),
                       ("0009,0010", "ACME")):
        inst.set_attr(tag, value)
    inst.record_attr_vr("0009,0010", "LO")
    for tag, value in extra.items():
        inst.set_attr(tag, value)
    for tag, vr in vrs.items():
        inst.record_attr_vr(tag, vr)
    inst.set_pixel_data(np.arange(64, dtype=np.uint16).reshape(8, 8))
    return inst


def _worker_export(tmp_path, inst):
    return _export_instance_worker(ExportContext(
        instance=inst,
        output_path=str(tmp_path / "out" / f"{inst.sop_instance_uid}.dcm"),
        patient_attributes={"0010,0010": "ANON", "0010,0020": "PAT1"},
        study_attributes={"0020,000d": "1.2.826.0.2.1"},
        series_attributes={"0020,000e": "1.2.826.0.3.1"},
        compression="j2k"))


RAGGED = [("OW", 7), ("OL", 6), ("OF", 6), ("OD", 12), ("OV", 12)]


@pytest.mark.parametrize("vr,length", RAGGED)
def test_a_value_that_is_not_whole_words_is_written_un_and_said(tmp_path, vr, length):
    """Bytes that are no whole number of the recorded VR's words: `UN`, the
    bytes unchanged, one re-VR sentence naming the tag and both VRs.
    Killing mutations: a width table accepting any length (pydicom writes a
    ragged `OL` without a word); the fallback taken with no `_ReVr`."""
    value = bytes(range(length))
    outcome = _worker_export(tmp_path, _hand_built({"0009,1002": value},
                                                   {"0009,1002": vr}))
    assert outcome.ok, outcome.error
    out = pydicom.dcmread(outcome.output_path)
    assert out[0x00091002].VR == "UN"
    assert bytes(out[0x00091002].value).rstrip(b"\0") == value.rstrip(b"\0")
    sentences = [w for w in outcome.warnings if w.startswith("Private element")]
    assert len(sentences) == 1
    assert f"(0009,1002) recorded {vr}, written UN" in sentences[0]


def test_a_ragged_source_value_draws_one_warning_row(tmp_path):
    """The same through a session: an `OL` of six bytes, which pydicom
    writes into the source without padding, reaches the store's audit log
    as exactly one `WARNING` row."""
    ragged = dict(ROOT, OL=(0x00091002, bytes(range(6))))
    out, db = _written(tmp_path, root=ragged)
    assert out[0x00091002].VR == "UN"
    (row,) = _warnings(db)
    assert "(0009,1002) recorded OL, written UN" in row


#: Private creators pydicom's private dictionary knows, so that under
#: Implicit VR its default `replace_un_with_known_vr` hands back the
#: dictionary's VR instead of `UN` -- `OB` for a Siemens CSA header, `OF`
#: for this Toshiba element (six bytes: not whole `OF` words).
KNOWN_CREATORS = [(0x00290010, "LO", "SIEMENS CSA HEADER"),
                  (0x00291010, "OB", bytes(range(10))),
                  (0x700D0010, "LO", "TOSHIBA_MEC_MR3"),
                  (0x700D1090, "OB", bytes(range(6)))]


def test_an_implicit_source_records_no_vr_and_writes_un(tmp_path, monkeypatch):
    """Implicit VR carries no VR, so no binary VR is recorded and every
    private binary value is written `UN`, with no row -- including for a
    creator pydicom's private dictionary knows, whose element pydicom reads
    back as the dictionary's `OB` or `OF` rather than `UN` (review of #739).
    That is pydicom's guess, not the file's statement: recorded, the CSA
    header was written `OB` and the six-byte Toshiba element drew a re-VR
    `WARNING` ("recorded OF, written UN") over a VR the file never declared.
    The output is read with the relabel off, or pydicom would guess again on
    the way back in. Killing mutations: recording `UN`; guessing a VR from a
    length; recording a binary VR from an Implicit VR dataset, at the root
    or in a sequence item."""
    out_dir = tmp_path / "out"
    _source(tmp_path / "src", ImplicitVRLittleEndian, extra=KNOWN_CREATORS,
            nested_extra=KNOWN_CREATORS[:2])
    db = str(tmp_path / "s.db")
    with DicomSession(db) as session:
        session.ingest(str(tmp_path / "src"))
        (patient,) = session.store.patients
        inst = patient.studies[0].series[0].instances[0]
        recorded = dict(inst.attribute_vrs)
        (std,) = inst.sequences["0008,1140"].items
        nested = dict(std.attribute_vrs)
        session.export(str(out_dir), use_compression=True)
    monkeypatch.setattr(pydicom.config, "replace_un_with_known_vr", False)
    out = pydicom.dcmread(str(_only(out_dir)))
    assert out.file_meta.TransferSyntaxUID.is_implicit_VR is False
    binary = [tag for tag, _ in ROOT.values()] + [0x00291010, 0x700D1090]
    for tag in binary:
        key = f"{tag >> 16:04x},{tag & 0xFFFF:04x}"
        assert key not in recorded, key
        assert out[tag].VR == "UN", key
    assert "0029,1010" not in nested
    (item,) = out.ReferencedImageSequence
    assert item[0x00291010].VR == "UN"
    assert _warnings(db) == []


def test_the_root_vrs_key_holds_only_tags_stored_beside_it(tmp_path):
    """One home per tag: a private bytes value's VR in the root `__vrs__`
    beside it in `attributes_json`, every other private tag's in
    `instance_attributes.value_rep`. Killing mutation: a root `__vrs__`
    copying all of `attribute_vrs` -- a second home that can disagree with
    `value_rep` after a partial write."""
    _source(tmp_path / "src", extra=[(0x00091020, "LO", "vendor"),
                                     (0x00091021, "DS", "3.000")])
    db = str(tmp_path / "s.db")
    with DicomSession(db) as session:
        session.ingest(str(tmp_path / "src"))
        session.save(sync=True)
    with sqlite3.connect(db) as conn:
        (blob,) = conn.execute("SELECT attributes_json FROM instances").fetchone()
        rows = dict(((g, e), vr) for g, e, vr in conn.execute(
            "SELECT group_id, element_id, value_rep FROM instance_attributes"))
    stored = json.loads(blob)["__vrs__"]
    assert stored == {f"{tag >> 16:04x},{tag & 0xFFFF:04x}": vr
                      for vr, (tag, _) in ROOT.items()}
    assert rows[("0009", "1020")] == "LO"
    assert rows[("0009", "1021")] == "DS"
    assert rows[("0009", "0010")] == "LO"


_CONFIG = """\
version: '2.0'
privacy_profile: basic
remove_private_tags: {remove}
"""


@pytest.mark.parametrize("remove", [True, False])
def test_remove_private_tags_decides_both_ways(tmp_path, remove):
    """`remove_private_tags` is the one switch: `true` removes every private
    element, binary included, each with its `REMEDIATION_REMOVE` row;
    `false` keeps them all, under their source VRs."""
    _source(tmp_path / "src")
    config = tmp_path / "config.yaml"
    config.write_text(_CONFIG.format(remove=str(remove).lower()), encoding="utf-8")
    db = str(tmp_path / "s.db")
    with DicomSession(db) as session:
        session.ingest(str(tmp_path / "src"))
        session.load_config(str(config))
        session.audit()
        session.anonymize()
        session.export(str(tmp_path / "out"), use_compression=True)
    out = pydicom.dcmread(str(_only(tmp_path / "out")))
    root_private = [e.tag for e in out if e.tag.group % 2 == 1]
    with sqlite3.connect(db) as conn:
        removed = [d for (d,) in conn.execute(
            "SELECT details FROM audit_log WHERE action_type = 'REMEDIATION_REMOVE'")]
    if remove:
        assert root_private == []
        for tag, _ in ROOT.values():
            key = f"{tag >> 16:04x},{tag & 0xFFFF:04x}"
            assert len([d for d in removed if key in d]) == 1, key
    else:
        for vr, (tag, _) in ROOT.items():
            assert out[tag].VR == vr
        assert removed == []


def test_a_replaced_private_binary_value_is_written_under_a_vr_that_holds_it():
    """Text under a recorded `OW` -- a REPLACE rule's `ANON` -- does not fit
    and takes the fallback, named. Killing mutation: the gate accepting a
    `str` under a binary VR."""
    ds, revrs = Dataset(), []
    DicomExporter._merge(ds, {"0009,1001": "ANON"}, vrs={"0009,1001": "OW"},
                         revrs=revrs)
    assert ds[0x00091001].VR == "LO"
    assert revrs == [_ReVr(tag="0009,1001", within="", recorded="OW", written="LO")]


#: PS3.5 6.2: the word width each binary VR's value is a whole number of.
FITS = {"OB": 1, "OW": 2, "OL": 4, "OF": 4, "OD": 8, "OV": 8}


@pytest.mark.parametrize("vr,n", list(itertools.product(
    ["OB", "OW", "OL", "OF", "OD", "OV", "UN", "LO"], [0, 1, 2, 4, 6, 8, 12, 16])))
def test_the_fit_table(vr, n):
    """True exactly where the bytes are whole words of the VR; a length of
    0 fits every binary VR; `UN` (never recorded) and every non-binary VR
    never fit bytes."""
    expected = vr in FITS and n % FITS[vr] == 0
    assert _value_fits_vr(b"\0" * n, vr) is expected
