"""A colour image with no Planar Configuration is read color-by-pixel (#1134).

Planar Configuration (0028,0006) is Type 1C: required when Samples per
Pixel is above 1. pydicom validates the header before any decoder runs, so
a colour file with the element absent, or present with no value, was
refused at ingest under every transfer syntax with
`Decompression Failed: AttributeError: Missing required element:
(0028,0006) 'Planar Configuration'`. Found on the MIDI-B Validation set
with 1.0.0rc20: 14 GE ultrasound images, each of which decodes with 0.

By the owner's rulings of 2026-10-16:

- such an image is read as interleaved (0) under every transfer syntax but
  RLE Lossless, with no plausibility check and no setting;
- the instance (or icon) holds 0 and a bookkeeping key saying the 0 was
  inferred (`pixel_geometry.PLANAR_INFERRED_ATTR`, `"absent"` or
  `"empty"`), and the export writes 0;
- each export logs one INFO note through `corrections`; no audit row is
  written and the grade does not move;
- section 5 of the report carries one count-only line when the count is
  above 0;
- an empty element is treated as absent; a colour icon is carried with 0.

Notes are read from the parent's INFO lines (`caplog` on the `isocenter`
logger, opened before the session as the #757 tests do). Rows are read
from `audit_log`. Every compressed fixture is built here or bundled inside
pydicom, never from pydicom-data, which CI does not fetch.
"""
import hashlib
import logging
import os
import sqlite3

import imagecodecs
import numpy as np
import pydicom
import pytest
from pydicom.data import get_testdata_file
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.encaps import encapsulate
from pydicom.sequence import Sequence
from pydicom.uid import ExplicitVRLittleEndian

from isocenter.entities import Instance
from isocenter.io_handlers import _decode_pixels
from isocenter import pixel_geometry
from isocenter.session import DicomSession

from support.decode_doors import HTJ2K, J2K_LOSSLESS, JPEGLS
from support.decode_doors import dataset as encapsulated

ROWS, COLS = 4, 5
SC = "1.2.840.10008.5.1.4.1.1.7"
RLE = "1.2.840.10008.1.2.5"
MPEG2 = "1.2.840.10008.1.2.4.100"
J2K_EXPORT = "1.2.840.10008.1.2.4.90"
ICON = "0088,0200"

#: Every channel distinct from the others, so a planar read of
#: interleaved bytes (or the reverse) cannot compare equal.
RGB = np.stack([np.arange(ROWS * COLS).reshape(ROWS, COLS),
                100 + np.arange(ROWS * COLS).reshape(ROWS, COLS),
                200 + np.arange(ROWS * COLS).reshape(ROWS, COLS) % 50],
               axis=-1).astype(np.uint8)
#: 8x8 RGB for the compressed families.
RGB8 = ((np.arange(8 * 8 * 3) * 7) % 251).astype(np.uint8).reshape(8, 8, 3)
RGB16 = (np.arange(48, dtype=np.int64) * 1000).astype(np.uint16).reshape(
    4, 4, 3)
ICON_RGB = np.arange(18, dtype=np.uint8).reshape(2, 3, 3)

LINE = "*   **Planar Configuration:** "
#: The bookkeeping key, written out here; `test_the_key_is_the_one_named`
#: holds the module's constant to it.
PLANAR_INFERRED_ATTR = "_ISOCENTER_PLANAR_CONFIGURATION_INFERRED"


def note(how):
    """The INFO note, written out here and never read from the code."""
    held = "was absent" if how == "absent" else "held no value"
    return (f"Planar Configuration (0028,0006) {held} in the source file, "
            f"where an image of more than one sample per pixel requires it "
            f"(PS3.3 C.7.6.3.1.3); read as 0, color-by-pixel, at ingest, "
            f"and written as 0. Nothing in the file confirmed that layout.")


def report_line(parts):
    return (f"{LINE}{parts} read with an inferred Planar Configuration "
            f"(0028,0006), as 0 (color-by-pixel), because the source file "
            f"stated none.\n")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _base(ts=ExplicitVRLittleEndian, patient="P1134"):
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = SC
    meta.MediaStorageSOPInstanceUID = pydicom.uid.generate_uid()
    meta.TransferSyntaxUID = ts
    ds = Dataset()
    ds.file_meta = meta
    ds.SOPClassUID = SC
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.PatientID, ds.PatientName = patient, "Doe^John"
    ds.StudyInstanceUID = pydicom.uid.generate_uid()
    ds.SeriesInstanceUID = pydicom.uid.generate_uid()
    ds.Modality, ds.StudyDate = "US", "20230101"
    ds.SeriesNumber, ds.InstanceNumber = 1, 1
    ds.DeviceSerialNumber = "SN1134"
    return ds


def _planar(ds, pc):
    """`"absent"`, `"empty"` (a zero-length element), or a value."""
    if pc == "empty":
        ds.add_new(0x00280006, "US", None)
    elif pc != "absent":
        ds.PlanarConfiguration = pc


def native(folder, name="one", *, pc="absent", spp=3, patient="P1134"):
    """A native Explicit VR LE 4x5 image; returns `(path, pixels)`."""
    ds = _base(patient=patient)
    ds.Rows, ds.Columns, ds.SamplesPerPixel = ROWS, COLS, spp
    ds.PhotometricInterpretation = "RGB" if spp == 3 else "MONOCHROME2"
    ds.BitsAllocated = ds.BitsStored = 8
    ds.HighBit, ds.PixelRepresentation = 7, 0
    arr = RGB if spp == 3 else RGB[..., 0].copy()
    if spp == 3:
        _planar(ds, pc)
    stored = arr.transpose(2, 0, 1) if pc == 1 else arr
    ds.PixelData = np.ascontiguousarray(stored).tobytes()
    return _save(folder, name, ds), arr


def with_icon(folder, name="icon", *, pc="absent", patient="P1134I"):
    """A MONOCHROME2 image carrying a 2x3 RGB icon with `pc`."""
    ds = _base(patient=patient)
    ds.Rows = ds.Columns = 4
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = ds.BitsStored = 8
    ds.HighBit, ds.PixelRepresentation = 7, 0
    ds.PixelData = np.arange(16, dtype=np.uint8).tobytes()
    icon = Dataset()
    icon.Rows, icon.Columns, icon.SamplesPerPixel = 2, 3, 3
    icon.PhotometricInterpretation = "RGB"
    icon.BitsAllocated = icon.BitsStored = 8
    icon.HighBit, icon.PixelRepresentation = 7, 0
    _planar(icon, pc)
    icon.PixelData = ICON_RGB.tobytes()
    ds.IconImageSequence = Sequence([icon])
    return _save(folder, name, ds)


def _save(folder, name, ds):
    d = os.path.join(str(folder), name)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{name}.dcm")
    ds.save_as(path, enforce_file_format=True)
    return path


def _bundled_without(folder, name, fixture):
    ds = pydicom.dcmread(get_testdata_file(fixture))
    del ds.PlanarConfiguration
    ds.PatientID = "P1134C"
    return _save(folder, name, ds)


def _encapsulated_without(folder, name, ts, codestream, **kwargs):
    ds = encapsulated(ts, [codestream], planar=None, **kwargs)
    return _save(folder, name, ds)


def _j2k(arr):
    return imagecodecs.jpeg2k_encode(arr, level=0, codecformat="J2K")


#: `(id, builder)` for T9: one colour file per family with the element
#: deleted. JPEG Baseline and 8-bit JPEG 2000 are decoded by Pillow, which
#: reads the element; the others by imagecodecs, which does not.
FAMILIES = [
    ("jpeg-baseline-pillow",
     lambda f: _bundled_without(f, "jpeg", "SC_rgb_jpeg_dcmtk.dcm")),
    ("j2k-8-pillow", lambda f: _encapsulated_without(
        f, "j2k8", J2K_LOSSLESS, _j2k(RGB8), rows=8, cols=8, samples=3,
        bits_allocated=8)),
    ("j2k-16-fallback", lambda f: _encapsulated_without(
        f, "j2k16", J2K_LOSSLESS, _j2k(RGB16), rows=4, cols=4, samples=3,
        bits_allocated=16)),
    ("htj2k", lambda f: _encapsulated_without(
        f, "htj2k", HTJ2K, imagecodecs.htj2k_encode(RGB8, reversible=True),
        rows=8, cols=8, samples=3, bits_allocated=8)),
    ("jpeg-ls", lambda f: _encapsulated_without(
        f, "jls", JPEGLS, imagecodecs.jpegls_encode(RGB8), rows=8, cols=8,
        samples=3, bits_allocated=8)),
    # Moved here from test_one_decode_answer_per_file's refusal table,
    # where it was refused until #1134: 8-bit YBR_FULL, converted to RGB
    # by the fallback.
    ("jpeg-ls-ybr-full", lambda f: _encapsulated_without(
        f, "jlsybr", JPEGLS, imagecodecs.jpegls_encode(RGB8), rows=8,
        cols=8, samples=3, bits_allocated=8, photometric="YBR_FULL")),
    # imagecodecs cannot encode a colour JPEG Lossless stream; pydicom
    # bundles one.
    ("jpeg-lossless-sv1",
     lambda f: _bundled_without(f, "ljpeg", "SC_rgb_jpeg_gdcm.dcm")),
]


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

def _instances(session):
    return [i for p in session.store.patients for st in p.studies
            for se in st.series for i in se.instances]


def _rows(db):
    with sqlite3.connect(str(db)) as conn:
        return conn.execute(
            "SELECT action_type, details FROM audit_log").fetchall()


def _notes(caplog, uid):
    prefix = f"{uid}: "
    return [r.getMessage()[len(prefix):] for r in caplog.records
            if r.levelno == logging.INFO
            and r.getMessage().startswith(prefix)
            and "Planar Configuration" in r.getMessage()]


def _export(session, out, compress):
    summary = session.export(str(out), use_compression=compress,
                             show_progress=False)
    assert summary.written >= 1, summary.failures
    return {pydicom.dcmread(os.path.join(root, f)).SOPInstanceUID:
            pydicom.dcmread(os.path.join(root, f))
            for root, _d, files in os.walk(str(out)) for f in files
            if f.endswith(".dcm")}


def _no_planar_rows(rows):
    return [r for r in rows if "Planar Configuration" in (r[1] or "")
            or "0028,0006" in (r[1] or "")]


# ---------------------------------------------------------------------------
# T1, T2 -- the read, absent and empty
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("how", ["absent", "empty"])
def test_a_native_image_with_no_value_is_read_interleaved(tmp_path, how):
    """T1/T2: ingested, the graph holds 0 (never None) and the key."""
    path, arr = native(tmp_path / "src", pc=how)
    db = tmp_path / "s.db"
    with DicomSession(str(db)) as session:
        summary = session.ingest(os.path.dirname(path))
        assert summary.ingested == 1, summary.failures
        (inst,) = _instances(session)
        assert inst.attributes["0028,0006"] == 0
        assert inst.attributes[PLANAR_INFERRED_ATTR] == how
        assert inst.unload_pixel_data() is True
        assert np.array_equal(inst.get_pixel_data(), arr)
    rows = _rows(db)
    assert not [r for r in rows if r[0] in ("ERROR", "WARNING", "DATA_LOSS")
                ], rows


# ---------------------------------------------------------------------------
# T3, T5 -- the export, both arms; the note; no row; PASS; source unchanged
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("how", ["absent", "empty"])
def test_the_export_writes_0_and_says_so_once_at_info(tmp_path, caplog, how):
    path, arr = native(tmp_path / "src", pc=how)
    before = hashlib.sha256(open(path, "rb").read()).hexdigest()
    db = tmp_path / "s.db"
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="isocenter"):
        with DicomSession(str(db)) as session:
            session.ingest(os.path.dirname(path))
            session.anonymize(session.audit())
            (inst,) = _instances(session)
            uid = inst.sop_instance_uid
            for compress, syntax in ((False, ExplicitVRLittleEndian),
                                     (True, J2K_EXPORT)):
                caplog.clear()
                (written,) = _export(session, tmp_path / f"out{compress}",
                                     compress).values()
                assert written.file_meta.TransferSyntaxUID == syntax
                assert written.PlanarConfiguration == 0
                assert np.array_equal(written.pixel_array, arr)
                if not compress:
                    # T5's twin: the uncompressed file carries the source's
                    # Pixel Data bytes exactly.
                    assert written.PixelData == RGB.tobytes()
                assert _notes(caplog, uid) == [note(how)], caplog.text
            session.generate_report(str(tmp_path / "report.md"))
    assert not _no_planar_rows(_rows(db))
    report = (tmp_path / "report.md").read_text()
    assert "| **Validation Status** | **PASS** |" in report, report
    # T5: ingest and export leave the source file as it was.
    assert hashlib.sha256(open(path, "rb").read()).hexdigest() == before


# ---------------------------------------------------------------------------
# T4 -- a reopen keeps 0 and the key, and the note comes again
# ---------------------------------------------------------------------------

def test_a_reopened_store_keeps_0_and_the_key(tmp_path, caplog):
    path, arr = native(tmp_path / "src")
    db = tmp_path / "s.db"
    with DicomSession(str(db)) as session:
        session.ingest(os.path.dirname(path))
        (inst,) = _instances(session)
        assert inst.has_unsaved_changes is False
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="isocenter"):
        with DicomSession(str(db)) as session:
            (inst,) = _instances(session)
            assert inst.attributes["0028,0006"] == 0
            assert inst.attributes[PLANAR_INFERRED_ATTR] == "absent"
            assert inst.has_unsaved_changes is False
            (written,) = _export(session, tmp_path / "out", False).values()
            assert written.PlanarConfiguration == 0
            assert np.array_equal(written.pixel_array, arr)
            assert _notes(caplog, inst.sop_instance_uid) == [note("absent")]


# ---------------------------------------------------------------------------
# T6, T7 -- the file arm and `_decode_pixels` itself
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("how", ["absent", "empty"])
def test_the_file_arm_reads_it_and_logs_nothing(tmp_path, caplog, how):
    path, arr = native(tmp_path / "src", pc=how)
    inst = Instance(pydicom.uid.generate_uid(), SC, 1, file_path=path)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="isocenter"):
        got = inst.get_pixel_data()
    assert np.array_equal(got, arr)
    assert "Planar Configuration" not in caplog.text


@pytest.mark.parametrize("how", ["absent", "empty"])
def test_decode_pixels_supplies_the_element_in_memory(tmp_path, how):
    path, arr = native(tmp_path / "src", pc=how)
    ds = pydicom.dcmread(path)
    got, label = _decode_pixels(ds)
    assert np.array_equal(got, arr) and label == "RGB"
    assert ds["PlanarConfiguration"].value == 0
    again = pydicom.dcmread(path)
    assert ("PlanarConfiguration" not in again
            or again["PlanarConfiguration"].VM == 0)


# ---------------------------------------------------------------------------
# T8 -- an element on the dataset, not a decode option
# ---------------------------------------------------------------------------

def test_a_one_bit_excess_still_gets_its_own_sentence(tmp_path):
    """#669's sentence, which needs a header pydicom accepts.

    A decode option leaves the header incomplete for the two readers that
    build their own runner, and pydicom then fails in CPython's words
    (`slice indices must be integers`).
    """
    ds = _base()
    ds.Rows = ds.Columns = 3
    ds.SamplesPerPixel = 3
    ds.PhotometricInterpretation = "RGB"
    ds.BitsAllocated = ds.BitsStored = 1
    ds.HighBit, ds.PixelRepresentation = 0, 0
    ds.PixelData = bytes(range(16))
    path = _save(tmp_path / "src", "onebit", ds)
    with DicomSession(str(tmp_path / "s.db")) as session:
        summary = session.ingest(os.path.dirname(path))
    assert summary.ingested == 0
    reason = summary.failures[0][1]
    assert "Planar Configuration" not in reason, reason
    assert "slice indices" not in reason, reason
    from isocenter.imagecodecs_handler import bit_packed_excess_words  # pylint: disable=import-outside-toplevel
    filled = pydicom.dcmread(path)
    filled.PlanarConfiguration = 0
    assert bit_packed_excess_words(filled) in reason, reason


# ---------------------------------------------------------------------------
# T9 -- every compressed family but RLE
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name, build", FAMILIES,
                         ids=[f[0] for f in FAMILIES])
def test_a_compressed_family_is_read_and_written_0(tmp_path, caplog, name,
                                                   build):
    path = build(tmp_path / "src")
    want_ds = pydicom.dcmread(path)
    want_ds.PlanarConfiguration = 0
    want, _label = _decode_pixels(want_ds)
    db = tmp_path / "s.db"
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="isocenter"):
        with DicomSession(str(db)) as session:
            summary = session.ingest(os.path.dirname(path))
            assert summary.ingested == 1, summary.failures
            (inst,) = _instances(session)
            assert inst.attributes["0028,0006"] == 0
            assert inst.attributes[PLANAR_INFERRED_ATTR] == "absent"
            assert inst.unload_pixel_data() is True
            assert np.array_equal(inst.get_pixel_data(), want)
            for compress in (False, True):
                caplog.clear()
                (written,) = _export(session, tmp_path / f"o{compress}",
                                     compress).values()
                assert written.PlanarConfiguration == 0
                assert _notes(caplog, inst.sop_instance_uid) == [
                    note("absent")]
    assert not _no_planar_rows(_rows(db))


# ---------------------------------------------------------------------------
# T10 -- a colour icon is carried
# ---------------------------------------------------------------------------

def _icon_item(inst):
    return inst.sequences[ICON].items[0]


@pytest.mark.parametrize("how", ["absent", "empty"])
def test_a_colour_icon_with_no_value_is_carried(tmp_path, caplog, how):
    path = with_icon(tmp_path / "src", pc=how)
    db = tmp_path / "s.db"
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="isocenter"):
        with DicomSession(str(db)) as session:
            session.ingest(os.path.dirname(path))
            (inst,) = _instances(session)
            item = _icon_item(inst)
            assert item.attributes["0028,0006"] == 0
            assert item.attributes[PLANAR_INFERRED_ATTR] == how
            assert PLANAR_INFERRED_ATTR not in inst.attributes
            session.anonymize(session.audit())
            (inst,) = _instances(session)
            assert _icon_item(inst).attributes[PLANAR_INFERRED_ATTR] == how
            session.save(sync=True)
        with DicomSession(str(db)) as session:
            (inst,) = _instances(session)
            assert _icon_item(inst).attributes[PLANAR_INFERRED_ATTR] == how
            uid = inst.sop_instance_uid
            for compress in (False, True):
                caplog.clear()
                (written,) = _export(session, tmp_path / f"o{compress}",
                                     compress).values()
                icon = written.IconImageSequence[0]
                assert "PixelData" in icon
                assert icon.PlanarConfiguration == 0
                assert icon.PixelData == ICON_RGB.tobytes()
                # The key never reaches a file.
                assert "PlanarConfiguration" not in written
                assert _notes(caplog, uid) == [
                    f"At {ICON}[0]: {note(how)}"], caplog.text
    rows = _rows(db)
    assert not [r for r in rows if r[0] == "DATA_LOSS"], rows
    assert not _no_planar_rows(rows)


# ---------------------------------------------------------------------------
# T11 -- redaction reads the interleaved array and writes no descriptor
# ---------------------------------------------------------------------------

def test_redaction_over_an_inferred_image(tmp_path):
    path, arr = native(tmp_path / "src")
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(os.path.dirname(path))
        session.configuration.add_rule("SN1134",
                                       redaction_zones=[[0, 2, 0, 2]])
        assert session.redact(show_progress=False) == 1
        (inst,) = _instances(session)
        assert inst.attributes["0028,0006"] == 0
        want = arr.copy()
        want[0:2, 0:2] = 0
        assert np.array_equal(inst.get_pixel_data(), want)
        (written,) = _export(session, tmp_path / "out", False).values()
        assert written.PlanarConfiguration == 0
        assert np.array_equal(written.pixel_array, want)


# ---------------------------------------------------------------------------
# T12 -- Pillow consults the element, so 0 is the value that reads it right
# ---------------------------------------------------------------------------

def test_pillow_lays_out_jpeg_by_the_element():
    """A forced 1 now decodes as the declared 0 does: under JPEG the
    element is read as 0 whatever it declares (#1137). Until #1137 this
    asserted the two differed, which was that defect, pinned."""
    source = pydicom.dcmread(get_testdata_file("SC_rgb_jpeg_dcmtk.dcm"))
    declared, _ = _decode_pixels(pydicom.dcmread(source.filename))
    supplied = pydicom.dcmread(source.filename)
    del supplied.PlanarConfiguration
    inferred, _ = _decode_pixels(supplied)
    forced = pydicom.dcmread(source.filename)
    forced.PlanarConfiguration = 1
    planar, _ = _decode_pixels(forced)
    assert source.PlanarConfiguration == 0
    assert np.array_equal(inferred, declared)
    assert np.array_equal(planar, declared)


# ---------------------------------------------------------------------------
# T13, T14 -- the report's count line
# ---------------------------------------------------------------------------

def _section_5(report):
    return report.split("\n## 5. Validation & Verification\n\n", 1)[1]


def test_the_report_counts_images_and_icons_before_any_export(tmp_path):
    src = tmp_path / "src"
    native(src, "a", patient="P1")
    native(src, "b", pc="empty", patient="P2")
    with_icon(src, "c", patient="P3")
    db = tmp_path / "s.db"
    want = report_line("2 image(s) and 1 icon(s)")
    with DicomSession(str(db)) as session:
        summary = session.ingest(str(src))
        assert summary.ingested == 3, summary.failures
        session.generate_report(str(tmp_path / "r1.md"))
        section = _section_5((tmp_path / "r1.md").read_text())
        assert want in section, section
        # In the burned-in text bullet's place's wake: no pass ran, so that
        # bullet is "" and the line sits between the Pixel Scan bullet and
        # Methodology, directly.
        lines = section.splitlines(keepends=True)
        at = lines.index(want)
        assert lines[at - 1].startswith("*   **Pixel Scan"), lines[at - 1]
        assert lines[at + 1].startswith("*   **Methodology:**"), lines[at + 1]
        session.anonymize(session.audit())
        session.generate_report(str(tmp_path / "r2.md"))
        r2 = (tmp_path / "r2.md").read_text()
        assert "| **Validation Status** | **PASS** |" in r2, r2
        assert want in _section_5(r2)
    with DicomSession(str(db)) as session:
        session.generate_report(str(tmp_path / "r3.md"))
    assert want in _section_5((tmp_path / "r3.md").read_text())


def test_the_report_object_holds_the_two_counts(tmp_path, monkeypatch):
    src = tmp_path / "src"
    native(src, "a", patient="P1")
    native(src, "b", patient="P2")
    with_icon(src, "c", patient="P3")
    from isocenter import reporting  # pylint: disable=import-outside-toplevel
    seen = []
    real = reporting.MarkdownRenderer.render
    monkeypatch.setattr(reporting.MarkdownRenderer, "render",
                        lambda self, report, path: (seen.append(report),
                                                    real(self, report, path)))
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(str(src))
        session.generate_report(str(tmp_path / "r.md"))
    assert seen[-1].planar_configuration_inferred == {"images": 2, "icons": 1}


@pytest.mark.parametrize("images, icons, parts", [
    (1, 0, "1 image(s)"),
    (0, 1, "1 icon(s)"),
    (1, 1, "1 image(s) and 1 icon(s)"),
], ids=["images", "icons", "both"])
def test_each_form_of_the_line(tmp_path, images, icons, parts):
    src = tmp_path / "src"
    if images:
        native(src, "a", patient="P1")
    if icons:
        with_icon(src, "c", patient="P3")
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(str(src))
        session.generate_report(str(tmp_path / "r.md"))
    section = _section_5((tmp_path / "r.md").read_text())
    assert [line + "\n" for line in section.splitlines()
            if line.startswith(LINE)] == [report_line(parts)], section


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

def test_one_sample_gets_no_element_and_no_note(tmp_path, caplog):
    """G1."""
    path, _arr = native(tmp_path / "src", spp=1)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="isocenter"):
        with DicomSession(str(tmp_path / "s.db")) as session:
            session.ingest(os.path.dirname(path))
            (inst,) = _instances(session)
            assert "0028,0006" not in inst.attributes
            assert PLANAR_INFERRED_ATTR not in inst.attributes
            (written,) = _export(session, tmp_path / "out", False).values()
            assert "PlanarConfiguration" not in written
    assert "Planar Configuration (0028,0006)" not in caplog.text


@pytest.mark.parametrize("pc", [0, 1], ids=["declared-0", "declared-1"])
def test_a_declared_value_gets_no_key_and_no_note(tmp_path, caplog, pc):
    """G2, G3: a declared 1 is read planar and rewritten 0, as before."""
    path, arr = native(tmp_path / "src", pc=pc)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="isocenter"):
        with DicomSession(str(tmp_path / "s.db")) as session:
            session.ingest(os.path.dirname(path))
            (inst,) = _instances(session)
            assert inst.attributes["0028,0006"] == 0
            assert PLANAR_INFERRED_ATTR not in inst.attributes
            assert np.array_equal(inst.get_pixel_data(), arr)
            (written,) = _export(session, tmp_path / "out", False).values()
            assert written.PlanarConfiguration == 0
            assert np.array_equal(written.pixel_array, arr)
    assert "Planar Configuration (0028,0006)" not in caplog.text


def test_a_value_of_2_is_still_refused_in_pydicoms_words(tmp_path):
    """G4."""
    path, _arr = native(tmp_path / "src", pc=2)
    with DicomSession(str(tmp_path / "s.db")) as session:
        summary = session.ingest(os.path.dirname(path))
    assert summary.ingested == 0
    assert ("(0028,0006) 'Planar Configuration' value of '2' is invalid"
            in summary.failures[0][1]), summary.failures


def test_rle_with_no_value_is_still_refused(tmp_path):
    """G5: the RLE exclusion, at ingest and at the file arm."""
    path = _bundled_without(tmp_path / "src", "rle", "SC_rgb_rle.dcm")
    assert pydicom.dcmread(path).file_meta.TransferSyntaxUID == RLE
    words = "Missing required element: (0028,0006) 'Planar Configuration'"
    with DicomSession(str(tmp_path / "s.db")) as session:
        summary = session.ingest(os.path.dirname(path))
    assert summary.ingested == 0
    assert words in summary.failures[0][1], summary.failures
    inst = Instance(pydicom.uid.generate_uid(), SC, 1, file_path=path)
    with pytest.raises(RuntimeError) as caught:
        inst.get_pixel_data()
    assert words in str(caught.value), str(caught.value)
    ds = pydicom.dcmread(path)
    with pytest.raises(AttributeError):
        _decode_pixels(ds)
    assert "PlanarConfiguration" not in ds


def test_no_file_meta_still_raises_on_file_meta():
    """G7: the write sits below the transfer syntax read."""
    ds = Dataset()
    ds.Rows, ds.Columns, ds.SamplesPerPixel = ROWS, COLS, 3
    ds.PhotometricInterpretation = "RGB"
    ds.BitsAllocated = ds.BitsStored = 8
    ds.HighBit, ds.PixelRepresentation = 7, 0
    ds.PixelData = RGB.tobytes()
    with pytest.raises(AttributeError) as caught:
        _decode_pixels(ds)
    assert "file_meta" in str(caught.value), str(caught.value)
    assert "PlanarConfiguration" not in ds


def test_a_colour_header_with_no_pixel_element_gets_nothing(tmp_path, caplog):
    """The predicate's `"PixelData" not in ds` arm (review of #1138).

    SamplesPerPixel 3, no Planar Configuration and no pixel element at
    all: there are no samples to lay out, so nothing is inferred. Without
    the arm the graph gained 0 and the key, the export wrote 0, and the
    report counted an image.
    """
    ds = _base()
    ds.Rows = ds.Columns = 2
    ds.SamplesPerPixel = 3
    ds.PhotometricInterpretation = "RGB"
    ds.BitsAllocated = ds.BitsStored = 8
    ds.HighBit, ds.PixelRepresentation = 7, 0
    path = _save(tmp_path / "src", "nopix", ds)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="isocenter"):
        with DicomSession(str(tmp_path / "s.db")) as session:
            summary = session.ingest(os.path.dirname(path))
            assert summary.ingested == 1, summary.failures
            (inst,) = _instances(session)
            assert PLANAR_INFERRED_ATTR not in inst.attributes
            assert "0028,0006" not in inst.attributes
            (written,) = _export(session, tmp_path / "out", False).values()
            assert "PixelData" not in written
            assert "PlanarConfiguration" not in written
            session.generate_report(str(tmp_path / "r.md"))
    assert LINE not in (tmp_path / "r.md").read_text()
    assert "Planar Configuration (0028,0006)" not in caplog.text


def test_a_syntax_with_no_decoder_is_refused_as_before(tmp_path):
    """G8: the in-memory element changes no outcome where nothing decodes."""
    reasons = {}
    for pc in ("absent", 0):
        ds = _base(ts=MPEG2)
        ds.Rows, ds.Columns, ds.SamplesPerPixel = ROWS, COLS, 3
        ds.PhotometricInterpretation = "RGB"
        ds.BitsAllocated = ds.BitsStored = 8
        ds.HighBit, ds.PixelRepresentation = 7, 0
        _planar(ds, pc)
        ds.PixelData = encapsulate([b"\x00\x00\x01\xb3" + bytes(60)])
        path = _save(tmp_path / f"src{pc}", "mpeg", ds)
        with DicomSession(str(tmp_path / f"s{pc}.db")) as session:
            summary = session.ingest(os.path.dirname(path))
        assert summary.ingested == 0, pc
        reasons[pc] = summary.failures[0][1]
    assert reasons["absent"] == reasons[0], reasons


def test_no_inferred_element_no_line(tmp_path, monkeypatch):
    """G9: ordinary colour and monochrome files, and a refused RLE file."""
    src = tmp_path / "src"
    native(src, "zero", pc=0, patient="P1")
    native(src, "one", pc=1, patient="P2")
    native(src, "mono", spp=1, patient="P3")
    _bundled_without(src, "rle", "SC_rgb_rle.dcm")
    from isocenter import reporting  # pylint: disable=import-outside-toplevel
    seen = []
    real = reporting.MarkdownRenderer.render

    def render(self, report, path):
        seen.append(report)
        return real(self, report, path)

    with DicomSession(str(tmp_path / "s.db")) as session:
        summary = session.ingest(str(src))
        assert summary.ingested == 3, summary.failures
        monkeypatch.setattr(reporting.MarkdownRenderer, "render", render)
        session.generate_report(str(tmp_path / "r.md"))
    assert LINE not in (tmp_path / "r.md").read_text()
    assert not any(seen[-1].planar_configuration_inferred.values())


def test_the_key_is_the_one_named():
    """Underscored, so `_merge` never writes it and `set_attr` is not used."""
    assert getattr(pixel_geometry, "PLANAR_INFERRED_ATTR", None) == \
        PLANAR_INFERRED_ATTR
