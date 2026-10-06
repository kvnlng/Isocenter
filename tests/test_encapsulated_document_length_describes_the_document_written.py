"""Encapsulated Document Length describes the document the file carries (#757).

PS3.3 C.24.2 makes Encapsulated Document Length `(0042,0015)` Type 3: "The
length of the Encapsulated Document stream, not including any trailing
padding added for encapsulation as a DICOM object. If present, shall be
equal to the Value Length if even, or one less than the Value Length if
odd." `basic@2026c` replaces Encapsulated Document `(0042,0011)` (Table
E.1-1, D) with its two-byte dummy, and no row of the table touches the
length. Measured at de5b26d9 on 3.12.14 and 3.14.7t, live and reopened: a
1000-byte document under basic was exported as two bytes beside
`(0042,0015) = 1000`, and a 70000-byte one, dropped at ingest by the
binary retention limit, was exported with no document and
`(0042,0015) = 70000`.

Ruled (Q3 A, 2026-10-06): when present, the length is written as the
length of the document written, padding excluded; a conformant source
length is kept; it is removed when the file carries no document; one INFO
note either way; and this holds whatever a rule wrote on the tag.

Every outcome is read off the exported file and pinned to a literal, and
every test runs from the session that ingested and from one reopened.
"""
import logging

import pydicom
import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian

from isocenter.session import DicomSession

PDF = "1.2.840.10008.5.1.4.1.1.104.1"
SOP = "1.2.826.0.1.757.1"
DOCUMENT, LENGTH = "0042,0011", "0042,0015"
BASIC = "version: '2.0'\nprivacy_profile: basic\n"

REWRITTEN = (
    "(0042,0015) Encapsulated Document Length {declared} does not describe "
    "the {length}-byte Encapsulated Document (0042,0011) written; written as "
    "{length} (PS3.3 C.24.2).")
REMOVED = (
    "(0042,0015) Encapsulated Document Length {declared} is not written: the "
    "file carries no Encapsulated Document (0042,0011) for it to describe.")


def _document(n):
    head = b"%PDF-1.4\n% John Doe 1970-01-01\n"
    return (head + b"x" * n)[:n]


def _source(folder, n, *, length=True):
    folder.mkdir(parents=True, exist_ok=True)
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = PDF
    meta.MediaStorageSOPInstanceUID = SOP
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = Dataset()
    ds.file_meta = meta
    ds.SOPClassUID, ds.SOPInstanceUID = PDF, SOP
    ds.PatientID, ds.PatientName = "P757", "Doe^John"
    ds.StudyInstanceUID = "1.2.826.0.1.757.2"
    ds.SeriesInstanceUID = "1.2.826.0.1.757.3"
    ds.Modality, ds.StudyDate, ds.StudyTime = "DOC", "20200101", "120000"
    ds.InstanceNumber = "1"
    ds.add_new(0x00420010, "ST", "Report title")
    ds.add_new(0x00420011, "OB", _document(n))
    ds.add_new(0x00420012, "LO", "application/pdf")
    if length:
        ds.add_new(0x00420015, "UL", n)
    ds.save_as(str(folder / "a.dcm"), enforce_file_format=True,
               implicit_vr=False, little_endian=True)


def _run(tmp_path, caplog, n, *, reopen, config=None, mutate=None,
         length=True, compress=False):
    """Ingest, de-identify under `config`, mutate, export; the file and notes."""
    src, db, out = tmp_path / "src", tmp_path / "s.db", tmp_path / "out"
    _source(src, n, length=length)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="isocenter"):
        session = DicomSession(str(db))
        try:
            session.ingest(str(src))
            if config is not None:
                path = tmp_path / "config.yaml"
                path.write_text(config)
                session.load_config(str(path))
                session.anonymize(session.audit())
            (patient,) = session.store.patients
            if mutate is not None:
                mutate(patient.studies[0].series[0].instances[0])
            if reopen:
                session.save(sync=True)
                session.close()
                session = DicomSession(str(db))
            summary = session.export(str(out), use_compression=compress,
                                     show_progress=False)
            (patient,) = session.store.patients
            uid = patient.studies[0].series[0].instances[0].sop_instance_uid
            rows = [tuple(r) for r in session.store_backend.get_audit_errors()]
        finally:
            session.close()
    assert summary.written == 1, summary.failures
    (written,) = list(out.rglob("*.dcm"))
    prefix = f"{uid}: "
    notes = [r.getMessage()[len(prefix):] for r in caplog.records
             if r.levelno == logging.INFO
             and r.getMessage().startswith(prefix)]
    return pydicom.dcmread(str(written)), notes, rows


def _wire_length(ds):
    """The document's value length as the file holds it, padding included."""
    raw = ds.get_item(0x00420011)
    return None if raw is None else len(raw.value)


pytestmark = pytest.mark.parametrize("reopen", [False, True],
                                     ids=["live", "reopened"])


def test_the_length_follows_the_dummy_basic_writes(tmp_path, caplog, reopen):
    """The issue: two bytes of document beside a declared 1000.

    Killing mutation: the call deleted (1000, as on main).
    """
    ds, notes, rows = _run(tmp_path, caplog, 1000, reopen=reopen,
                           config=BASIC)

    assert bytes(ds[0x00420011].value) == b"\x00\x00"
    assert ds.get("EncapsulatedDocumentLength") == 2
    assert ds[0x00420015].VR == "UL"
    assert notes == [REWRITTEN.format(declared=1000, length=2)]
    # An exact derivation: a note, never a row.
    assert not [r for r in rows if "0042,0015" in r[2]], rows


def test_a_source_length_that_agrees_is_kept(tmp_path, caplog, reopen):
    """Control: an even-length document, no rule, no note."""
    ds, notes, _rows = _run(tmp_path, caplog, 1000, reopen=reopen)

    assert _wire_length(ds) == 1000
    assert ds.get("EncapsulatedDocumentLength") == 1000
    assert notes == []


def test_a_conformant_odd_length_is_kept(tmp_path, caplog, reopen):
    """1001 bytes are padded to 1002, and the length says 1001: conformant.

    Killing mutation: a predicate with no padding clause (rewritten 1002).
    """
    ds, notes, _rows = _run(tmp_path, caplog, 1001, reopen=reopen)

    assert _wire_length(ds) == 1002
    assert ds.get("EncapsulatedDocumentLength") == 1001
    assert notes == []


def test_one_less_than_the_value_is_kept_only_over_a_pad_byte(
        tmp_path, caplog, reopen):
    """1002 bytes ending in a document byte under a declared 1001: rewritten.

    Killing mutation: the padding clause without its NUL test.
    """
    whole = _document(1001) + b"x"
    ds, notes, _rows = _run(
        tmp_path, caplog, 1001, reopen=reopen,
        mutate=lambda inst: inst.set_attr(DOCUMENT, whole))

    assert bytes(ds[0x00420011].value) == whole
    assert ds.get("EncapsulatedDocumentLength") == 1002
    assert notes == [REWRITTEN.format(declared=1001, length=1002)]


def test_an_odd_length_document_set_by_a_caller_is_described_unpadded(
        tmp_path, caplog, reopen):
    """35 bytes are written as 36 with a pad; the length excludes it.

    Killing mutation: the length taken from the padded value (36).
    """
    whole = b"%PDF" * 8 + b"EOF"
    ds, notes, _rows = _run(
        tmp_path, caplog, 1000, reopen=reopen,
        mutate=lambda inst: inst.set_attr(DOCUMENT, whole))

    assert _wire_length(ds) == 36
    assert ds.get("EncapsulatedDocumentLength") == 35
    assert notes == [REWRITTEN.format(declared=1000, length=35)]


def test_an_odd_length_document_ending_in_nul_is_not_taken_for_padded(
        tmp_path, caplog, reopen):
    """37 bytes whose last is NUL, under a declared 36: the NUL is the
    document's own. A pad byte makes a value even, so an odd-length value
    holds none.

    Killing mutation: the padding clause without its even-length test
    (kept at 36).
    """
    whole = b"%PDF" * 9 + b"\x00"
    ds, notes, _rows = _run(
        tmp_path, caplog, 36, reopen=reopen,
        mutate=lambda inst: inst.set_attr(DOCUMENT, whole))

    assert _wire_length(ds) == 38
    assert ds.get("EncapsulatedDocumentLength") == 37
    assert notes == [REWRITTEN.format(declared=36, length=37)]


def test_a_length_with_no_document_is_not_written(tmp_path, caplog, reopen):
    """A 70000-byte document is dropped at ingest; its length goes too.

    Killing mutation: the absent-document arm deleted (70000, as on main).
    """
    ds, notes, _rows = _run(tmp_path, caplog, 70000, reopen=reopen,
                            config=BASIC)

    assert 0x00420011 not in ds
    assert 0x00420015 not in ds
    assert notes == [REMOVED.format(declared=70000)]


def test_a_length_the_source_never_had_is_not_added(tmp_path, caplog, reopen):
    """The element is Type 3: absent stays absent.

    Killing mutation: the "not in ds" guard dropped (always written).
    """
    ds, notes, _rows = _run(tmp_path, caplog, 1000, reopen=reopen,
                            config=BASIC, length=False)

    assert bytes(ds[0x00420011].value) == b"\x00\x00"
    assert 0x00420015 not in ds
    assert notes == []


def test_a_kept_document_a_caller_replaced_is_described(tmp_path, caplog,
                                                        reopen):
    """A KEEP override keeps the document, and a caller's own is measured."""
    config = (BASIC + "phi_tags:\n  '0042,0011':\n    action: KEEP\n"
              "    name: Encapsulated Document\n")
    whole = b"%PDF" * 9
    ds, notes, _rows = _run(
        tmp_path, caplog, 1000, reopen=reopen, config=config,
        mutate=lambda inst: inst.set_attr(DOCUMENT, whole))

    assert bytes(ds[0x00420011].value) == whole
    assert ds.get("EncapsulatedDocumentLength") == 36
    assert notes == [REWRITTEN.format(declared=1000, length=36)]


def test_a_compressed_export_says_the_same(tmp_path, caplog, reopen):
    """A pixel-less file stays native under `use_compression=True`."""
    ds, notes, _rows = _run(tmp_path, caplog, 1000, reopen=reopen,
                            config=BASIC, compress=True)

    assert ds.get("EncapsulatedDocumentLength") == 2
    assert notes == [REWRITTEN.format(declared=1000, length=2)]


@pytest.mark.parametrize("rule, written, note", [
    # Removed by the rule: the element is Type 3 and is never added back.
    ("action: REMOVE", None, None),
    # Emptied by the rule: present, so it says the document's length.
    ("action: EMPTY", 2,
     "(0042,0015) Encapsulated Document Length, which holds no value, does "
     "not describe the 2-byte Encapsulated Document (0042,0011) written; "
     "written as 2 (PS3.3 C.24.2)."),
])
def test_a_rule_on_the_length_does_not_outrank_the_document(
        tmp_path, caplog, reopen, rule, written, note):
    """Q3 A: the length is the document's, whatever a rule wrote on it.

    Killing mutation: a configured rule on (0042,0015) suppressing the
    derivation (Q3 B).
    """
    config = (BASIC + f"phi_tags:\n  '0042,0015':\n    {rule}\n"
              "    name: Encapsulated Document Length\n")
    ds, notes, _rows = _run(tmp_path, caplog, 1000, reopen=reopen,
                            config=config)

    assert bytes(ds[0x00420011].value) == b"\x00\x00"
    assert ds.get("EncapsulatedDocumentLength") == written
    assert (0x00420015 in ds) is (written is not None)
    assert notes == ([] if note is None else [note])
