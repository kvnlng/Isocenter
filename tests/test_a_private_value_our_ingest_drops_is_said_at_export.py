"""A private value this library's own ingest would drop is said at export (#921).

This library's ingest keeps a private binary value up to 65534 bytes and
drops a larger one with a PRIVATE `DATA_LOSS` row, which grades the run
`REVIEW_REQUIRED` (#151). A caller can put a larger one into the graph
with `set_attr`, and the export writes it. Measured at de5b26d9 on 3.12.14
and 3.14.7t:

- **Implicit VR** (`use_compression=False`) names no VR, so re-ingest reads
  every private element as `UN` bytes. A 40000-value list, a recorded `US`
  list of 40000, a 70000-character text and 70000 `bytes` were each written
  without a word, and each was dropped when that export was re-ingested.
- **Explicit VR**: a caller's 70000 `bytes` is written `UN`, a binary VR,
  and dropped on re-ingest, with no note either. #692's note covers only a
  value it relabels, and #901's only a private `LO`.

Ruled (Q5 A, 2026-10-06): one INFO note per element, under Implicit VR for
any private value over the limit and under Explicit VR for a binary-VR
private value over it. No audit row.

Each note is asserted whole, and the first test re-ingests the export and
finds the rows the notes foretold, so the notes are held to the truth.
"""
import logging
import shutil

import numpy as np
import pydicom
import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.filebase import DicomBytesIO
from pydicom.filewriter import write_dataset
from pydicom.sequence import Sequence
from pydicom.uid import ExplicitVRLittleEndian, ImplicitVRLittleEndian

from isocenter.session import DicomSession

SC = "1.2.840.10008.5.1.4.1.1.7"
SOP = "1.2.826.0.1.921.1"

IMPLICIT = (
    "{where}{tag} ({n} bytes) is written as it stands, under Implicit VR, "
    "which names no VR: this library's ingest reads a private element whose "
    "VR the file does not state as UN bytes, and drops one over 65534 bytes "
    "with a DATA_LOSS row.")
EXPLICIT = (
    "{where}{tag} ({vr}, {n} bytes) is written as it stands: this library's "
    "ingest drops a binary value over 65534 bytes with a DATA_LOSS row.")


def _source(folder, extra=None):
    folder.mkdir(parents=True, exist_ok=True)
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = SC
    meta.MediaStorageSOPInstanceUID = SOP
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = Dataset()
    ds.file_meta = meta
    ds.SOPClassUID, ds.SOPInstanceUID = SC, SOP
    ds.PatientID, ds.PatientName = "P921", "Doe^John"
    ds.StudyInstanceUID = "1.2.826.0.1.921.2"
    ds.SeriesInstanceUID = "1.2.826.0.1.921.3"
    ds.Modality, ds.StudyDate, ds.StudyTime = "OT", "20200101", "120000"
    ds.Rows = ds.Columns = 4
    ds.BitsAllocated = ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelData = np.arange(16, dtype=np.uint16).tobytes()
    ds.add_new(0x00090010, "LO", "C1PROBE")
    ds.add_new(0x00091002, "US", [1, 2])
    if extra is not None:
        extra(ds)
    ds.save_as(str(folder / "a.dcm"), enforce_file_format=True,
               implicit_vr=False, little_endian=True)


def _instance(session):
    (patient,) = session.store.patients
    return patient.studies[0].series[0].instances[0]


def _export(tmp_path, caplog, *, compress, mutate=None, extra=None,
            name="one"):
    """Ingest, mutate, export; the file, this instance's notes, the rows."""
    src = tmp_path / f"{name}-src"
    _source(src, extra)
    out = tmp_path / f"{name}-out"
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="isocenter"), \
            DicomSession(str(tmp_path / f"{name}.db")) as s:
        s.ingest(str(src))
        inst = _instance(s)
        if mutate is not None:
            mutate(inst)
        s.export(str(out), use_compression=compress, show_progress=False)
        rows = [tuple(r) for r in s.store_backend.get_audit_errors()]
        losses = [tuple(r) for r in s.store_backend.get_audit_losses()]
        prefix = f"{inst.sop_instance_uid}: "
    (written,) = list(out.rglob("*.dcm"))
    notes = [r.getMessage()[len(prefix):] for r in caplog.records
             if r.levelno == logging.INFO
             and r.getMessage().startswith(prefix)]
    return written, notes, rows, losses


def _four(inst):
    inst.set_attr("0009,1001", list(range(40000)))
    inst.set_attr("0009,1002", list(range(40000)))
    inst.set_attr("0009,1003", "x" * 70000)
    inst.set_attr("0009,1004", b"\x01" * 70000)
    inst.set_attr("0009,1005", "short")


def _reingest_losses(tmp_path, written, name="again"):
    folder = tmp_path / f"{name}-src"
    folder.mkdir()
    shutil.copy(written, folder / "a.dcm")
    with DicomSession(str(tmp_path / f"{name}.db")) as s:
        s.ingest(str(folder))
        held = dict(_instance(s).attributes)
        losses = [tuple(r) for r in s.store_backend.get_audit_losses()]
    return held, losses


def test_an_implicit_export_says_each_value_re_ingest_drops(tmp_path, caplog):
    """Four values over the limit, four notes, and re-ingest drops those four.

    Killing mutations: the call deleted (no notes); the notes raised under
    Explicit VR only.
    """
    written, notes, rows, losses = _export(
        tmp_path, caplog, compress=False, mutate=_four)

    assert str(pydicom.dcmread(str(written)).file_meta.TransferSyntaxUID) \
        == ImplicitVRLittleEndian
    assert notes == [
        IMPLICIT.format(where="", tag="0009,1001", n=228890),
        IMPLICIT.format(where="", tag="0009,1002", n=80000),
        IMPLICIT.format(where="", tag="0009,1003", n=70000),
        IMPLICIT.format(where="", tag="0009,1004", n=70000)]
    # A note, never a row: the file is written as asked.
    assert not [r for r in rows if "0009," in r[2]], rows
    assert not [r for r in losses if "0009," in r[2]], losses

    held, again = _reingest_losses(tmp_path, written)
    dropped = sorted(r[2] for r in again if "0009," in r[2])
    assert len(dropped) == 4, again
    for tag, text in zip(("0009,1001", "0009,1002", "0009,1003", "0009,1004"),
                         dropped):
        assert tag in text and "65534-byte retention threshold" in text, text
        assert tag not in held
    assert all(r[3] == "PRIVATE" for r in again if "0009," in r[2]), again
    # The one under the limit comes back.
    assert "0009,1005" in held


def test_an_explicit_export_says_a_binary_value_re_ingest_drops(
        tmp_path, caplog):
    """A caller's 70000 `bytes` is written `UN`; said, and dropped on re-ingest.

    Killing mutation: the Implicit VR arm alone (the issue as filed, Q5 B).
    """
    written, notes, rows, _losses = _export(
        tmp_path, caplog, compress=True,
        mutate=lambda i: i.set_attr("0009,1004", b"\x01" * 70000))

    ds = pydicom.dcmread(str(written))
    assert not ds.file_meta.TransferSyntaxUID.is_implicit_VR
    assert ds.get_item(0x00091004).VR == "UN"
    assert notes == [EXPLICIT.format(where="", tag="0009,1004", vr="UN",
                                     n=70000)]
    assert not [r for r in rows if "0009," in r[2]], rows

    held, again = _reingest_losses(tmp_path, written)
    assert "0009,1004" not in held
    assert len([r for r in again if "0009,1004" in r[2]]) == 1, again


def _sequence_shaped(n=70000):
    """Bytes over the limit that re-encode byte for byte as a sequence."""
    item = Dataset()
    item.add_new(0x00091010, "UN", b"\x02" * n)
    buffer = DicomBytesIO()
    buffer.is_little_endian, buffer.is_implicit_VR = True, True
    holder = Dataset()
    holder.add_new(0x00091004, "SQ", Sequence([item]))
    write_dataset(buffer, holder)
    # Tag (4) and length (4), then the items.
    encoded = buffer.getvalue()[8:]
    assert len(encoded) > n
    return encoded


@pytest.mark.parametrize("vr", ["OB", "OW", "OF", "OD", "OL", "OV"])
@pytest.mark.parametrize("shaped", [False, True],
                         ids=["plain", "sequence-shaped"])
def test_an_explicit_export_says_a_value_under_a_stated_binary_vr(
        tmp_path, caplog, vr, shaped):
    """A private value the source stated as a binary VR, grown past the
    limit by a caller: the commonest real case (a vendor header blob).

    Ingest drops such a value by size alone, with no sequence re-parse
    (that is for a VR the file left unknown), so sequence-shaped bytes
    are said too. Killing mutations: the Explicit VR set cut to `OW` and
    `UN`; the sequence exemption applied to a stated binary VR.
    """
    def recorded(ds):
        ds.add_new(0x00091006, vr, b"\x01" * 8)

    # 70000 is a whole number of every binary VR's words (1, 2, 4, 8).
    value = _sequence_shaped() if shaped else b"\x01" * 70000
    value += b"\x00" * (-len(value) % 8)
    written, notes, rows, _losses = _export(
        tmp_path, caplog, compress=True, extra=recorded,
        mutate=lambda i: i.set_attr("0009,1006", value))

    assert pydicom.dcmread(str(written)).get_item(0x00091006).VR == vr
    assert notes == [EXPLICIT.format(where="", tag="0009,1006", vr=vr,
                                     n=len(value))]
    assert not [r for r in rows if "0009," in r[2]], rows

    held, again = _reingest_losses(tmp_path, written)
    assert "0009,1006" not in held
    dropped = [r for r in again if "0009,1006" in r[2]]
    assert len(dropped) == 1 and dropped[0][3] == "PRIVATE", again


def test_an_explicit_export_does_not_say_it_twice(tmp_path, caplog):
    """A value #692 relabels or #901 writes `UC` keeps that one note.

    The 40000-value list is `LO` when this note looks, and `UC` afterwards
    (kept on re-ingest); the recorded `US` list is `US`, then #692's `UN`,
    whose own note already says it is dropped. Killing mutations: the
    call placed after the relabel (it would see `UN` twice); the binary-VR
    restriction dropped under Explicit VR.
    """
    def lists(inst):
        inst.set_attr("0009,1001", list(range(40000)))
        inst.set_attr("0009,1002", list(range(40000)))
        inst.set_attr("0009,1003", "x" * 70000)

    written, notes, _rows, _losses = _export(
        tmp_path, caplog, compress=True, mutate=lists)

    ds = pydicom.dcmread(str(written))
    assert (ds.get_item(0x00091001).VR, ds.get_item(0x00091002).VR,
            ds.get_item(0x00091003).VR) == ("UC", "UN", "UT")
    assert notes == [
        "0009,1001 (LO, 228890 bytes) is written as UC: an Explicit VR LO "
        "element can hold at most 65535 bytes (PS3.5 6.2.2), and UC holds "
        "the same values with a 4-byte length; this library reads them "
        "back as UC.",
        "0009,1002 (US, 80000 bytes) is written as UN: an Explicit VR US "
        "element can hold at most 65535 bytes (PS3.5 6.2.2). The bytes are "
        "the value's own, in Implicit VR Little Endian encoding, and this "
        "library holds them as UN bytes on re-ingest, which drops a UN "
        "over 65534 bytes with a DATA_LOSS row."]


@pytest.mark.parametrize("compress", [False, True])
@pytest.mark.parametrize("length, written_length", [
    (65534, None), (65535, 65536), (65536, 65536)])
def test_the_limit_is_the_ingest_gates_own(tmp_path, caplog, compress, length,
                                           written_length):
    """65534 bytes is kept on re-ingest and not noted; one more is both.

    An odd length is padded to an even one on the wire, so 65535 bytes is
    written as 65536 and dropped. Killing mutations: `>` for `>=` at the
    limit; the length weighed unpadded.
    """
    written, notes, _rows, _losses = _export(
        tmp_path, caplog, compress=compress,
        mutate=lambda i: i.set_attr("0009,1004", b"\x01" * length))

    held, again = _reingest_losses(tmp_path, written)
    dropped = [r for r in again if "0009,1004" in r[2]]
    if written_length is None:
        assert notes == []
        assert not dropped and len(held["0009,1004"]) == 65534
    else:
        template = EXPLICIT if compress else IMPLICIT
        assert notes == [template.format(where="", tag="0009,1004", vr="UN",
                                         n=written_length)]
        assert len(dropped) == 1 and "0009,1004" not in held


@pytest.mark.parametrize("compress", [False, True])
def test_a_small_value_and_the_creator_are_not_noted(tmp_path, caplog,
                                                     compress):
    """Control: nothing under the limit, and never the private creator."""
    def small(inst):
        inst.set_attr("0009,1005", "short")
        inst.set_attr("0009,1004", b"\x01" * 100)

    _written, notes, _rows, _losses = _export(
        tmp_path, caplog, compress=compress, mutate=small)

    assert notes == []


@pytest.mark.parametrize("compress", [False, True])
def test_a_standard_value_is_not_this_notes_subject(tmp_path, caplog,
                                                    compress):
    """Control: an even-group value keeps the notes it already has.

    A standard element has a dictionary VR to be read back under.
    Killing mutation: the odd-group test dropped.
    """
    _written, notes, _rows, _losses = _export(
        tmp_path, caplog, compress=compress,
        mutate=lambda i: i.set_attr("0040,9212", [0.25] * 9000))

    assert not [n for n in notes if "is written as it stands" in n], notes


@pytest.mark.parametrize("compress", [False, True])
def test_a_nested_private_value_is_said_with_its_path(tmp_path, caplog,
                                                      compress):
    """An item's private value over the limit is found, and placed.

    Killing mutation: a top-level-only walk.
    """
    def nested(ds):
        item = Dataset()
        item.ReferencedSOPClassUID = SC
        item.ReferencedSOPInstanceUID = "1.2.826.0.1.921.9"
        item.add_new(0x00090010, "LO", "C1PROBE")
        ds.ReferencedImageSequence = Sequence([item])

    def mutate(inst):
        (item,) = inst.sequences["0008,1140"].items
        item.set_attr("0009,1004", b"\x01" * 70000)

    _written, notes, _rows, _losses = _export(
        tmp_path, caplog, compress=compress, extra=nested, mutate=mutate)

    template = EXPLICIT if compress else IMPLICIT
    assert notes == [template.format(where="(0008,1140) item 0 > ",
                                     tag="0009,1004", vr="UN", n=70000)]


@pytest.mark.parametrize("compress", [False, True])
def test_bytes_our_ingest_reads_back_as_a_sequence_are_not_said_to_drop(
        tmp_path, caplog, compress):
    """A `UN` value that re-parses byte-exactly as a sequence is kept whole.

    Ingest restores such a value as a sequence before any size gate, so
    saying it is dropped would be false. Killing mutation: the note raised
    on size alone.
    """
    encoded = _sequence_shaped()

    written, notes, _rows, _losses = _export(
        tmp_path, caplog, compress=compress,
        mutate=lambda i: i.set_attr("0009,1004", encoded))

    assert notes == []
    with DicomSession(str(tmp_path / "again.db")) as s:
        folder = tmp_path / "again-src"
        folder.mkdir()
        shutil.copy(written, folder / "a.dcm")
        s.ingest(str(folder))
        inst = _instance(s)
        assert "0009,1004" in inst.sequences
        assert "0009,1004" not in inst.attributes
