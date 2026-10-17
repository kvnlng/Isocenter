"""A declared Planar Configuration under a compressed syntax is read as 0
(#1137, owner rulings R20 and R20a-c of 2026-10-16).

Under JPEG, JPEG-LS, JPEG 2000 and HTJ2K the compressed stream fixes how
colour components are laid out, and PS3.5 calls (0028,0006) irrelevant
there (§8.2.3, §8.2.4, §8.2.14; JPEG data "are always color-by-pixel",
§8.2.1). pydicom's Pillow plugin (JPEG Baseline and Extended, 8-bit JPEG
2000) returns interleaved samples and pydicom lays them out by the
element, so a declared 1 scrambled the colours. Every value but 0 is now
read as 0 under those 11 syntaxes (R20a), the instance or icon holds 0
and records `PLANAR_OVERRIDDEN_ATTR`, each export says so once at INFO,
and section 5 of the report counts them on a line of its own (R20b). No
row, no grade effect. Native syntaxes and RLE Lossless are unchanged.

Fixtures are built here, or bundled inside pydicom; the families are
#1134's, built declaring the value under test rather than none.
"""
import logging
import os
import re
import sqlite3

import imagecodecs
import numpy as np
import pydicom
import pytest
from pydicom.data import get_testdata_file
from pydicom.dataset import Dataset
from pydicom.sequence import Sequence

from isocenter import io_handlers, pixel_geometry
from isocenter.entities import Instance
from isocenter.io_handlers import _decode_pixels
from isocenter.session import DicomSession

from support.decode_doors import HTJ2K, J2K_LOSSLESS, JPEGLS
from support.decode_doors import dataset as encapsulated
from support.project_secret import load_fixed_secret

ICON = "0088,0200"
RLE = "1.2.840.10008.1.2.5"
LINE = "*   **Planar Configuration:** "
#: Both keys written out here; `test_the_keys_are_the_ones_named` holds
#: the module's constants to them.
PLANAR_OVERRIDDEN_ATTR = "_ISOCENTER_PLANAR_CONFIGURATION_OVERRIDDEN"
PLANAR_INFERRED_ATTR = "_ISOCENTER_PLANAR_CONFIGURATION_INFERRED"

RGB8 = ((np.arange(8 * 8 * 3) * 7) % 251).astype(np.uint8).reshape(8, 8, 3)
RGB16 = (np.arange(48, dtype=np.int64) * 1000).astype(np.uint16).reshape(4, 4, 3)
#: 4x5, every channel distinct, for the native control.
RGB = np.stack([np.arange(20).reshape(4, 5), 100 + np.arange(20).reshape(4, 5),
                200 + np.arange(20).reshape(4, 5)], axis=-1).astype(np.uint8)

#: The note, written out here and never read from the code.
NOTE = ("Planar Configuration (0028,0006) declared a value other than 0 "
        "in the source file, under a compressed transfer syntax whose "
        "stream fixes how colour components are laid out (PS3.5 8.2); "
        "read as 0, color-by-pixel, at ingest, and written as 0.")
INFERRED_NOTE_START = "Planar Configuration (0028,0006) was absent"


def report_line(parts):
    return (f"{LINE}{parts} read as 0 (color-by-pixel) over a declared "
            f"Planar Configuration (0028,0006), under a compressed transfer "
            f"syntax whose stream fixes the layout.\n")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _save(folder, name, ds):
    d = os.path.join(str(folder), name)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{name}.dcm")
    ds.save_as(path, enforce_file_format=True)
    return path


def _declare(ds, pc):
    """Set (0028,0006): a value, a list (multi-valued), "absent" or "empty"."""
    if pc == "absent":
        if "PlanarConfiguration" in ds:
            del ds.PlanarConfiguration
    elif pc == "empty":
        ds.add_new(0x00280006, "US", None)
    else:
        ds.add_new(0x00280006, "US", pc)


def _bundled(fixture, pc, patient="P1137C"):
    """A bundled file declaring `pc`, under UIDs of its own, so two built
    from one fixture are two instances of two studies."""
    ds = pydicom.dcmread(get_testdata_file(fixture))
    _declare(ds, pc)
    ds.PatientID = patient
    ds.SOPInstanceUID = pydicom.uid.generate_uid()
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    ds.StudyInstanceUID = pydicom.uid.generate_uid()
    ds.SeriesInstanceUID = pydicom.uid.generate_uid()
    return ds


def _encaps(ts, stream, pc, **kwargs):
    ds = encapsulated(ts, [stream], planar=None, **kwargs)
    _declare(ds, pc)
    return ds


def _j2k(arr):
    return imagecodecs.jpeg2k_encode(arr, level=0, codecformat="J2K")


#: `(id, builder(pc) -> Dataset)`. JPEG Baseline and 8-bit JPEG 2000 are
#: decoded by Pillow, which read the element, so a declared 1 was
#: scrambled there; the others by imagecodecs, which never reads it.
FAMILIES = [
    ("jpeg-baseline-pillow", lambda pc: _bundled("SC_rgb_jpeg_dcmtk.dcm", pc)),
    ("j2k-8-pillow", lambda pc: _encaps(
        J2K_LOSSLESS, _j2k(RGB8), pc, rows=8, cols=8, samples=3, bits_allocated=8)),
    ("j2k-16-fallback", lambda pc: _encaps(
        J2K_LOSSLESS, _j2k(RGB16), pc, rows=4, cols=4, samples=3, bits_allocated=16)),
    ("htj2k", lambda pc: _encaps(
        HTJ2K, imagecodecs.htj2k_encode(RGB8, reversible=True), pc, rows=8,
        cols=8, samples=3, bits_allocated=8)),
    ("jpeg-ls", lambda pc: _encaps(
        JPEGLS, imagecodecs.jpegls_encode(RGB8), pc, rows=8, cols=8, samples=3,
        bits_allocated=8)),
    ("jpeg-ls-ybr-full", lambda pc: _encaps(
        JPEGLS, imagecodecs.jpegls_encode(RGB8), pc, rows=8, cols=8, samples=3,
        bits_allocated=8, photometric="YBR_FULL")),
    ("jpeg-lossless-sv1", lambda pc: _bundled("SC_rgb_jpeg_gdcm.dcm", pc)),
]
IDS = [f[0] for f in FAMILIES]
PILLOW = [f for f in FAMILIES if f[0].endswith("pillow")]


def _decoded(ds):
    arr, _label = _decode_pixels(ds)
    return arr


def _instances(session):
    return [i for p in session.store.patients for st in p.studies
            for se in st.series for i in se.instances]


def _rows(db):
    with sqlite3.connect(str(db)) as conn:
        return conn.execute("SELECT action_type, details FROM audit_log").fetchall()


_UIDISH = re.compile(r"\b\d+(?:\.\d+){3,}\b|ANON_\w+|\b[0-9a-f]{16,}\b")


def _rows_without_uids(db, run):
    """The rows, UIDs, pseudonyms and this run's own folder name spelled
    alike, so a declared-1 store and its declared-0 twin compare."""
    return sorted((action, _UIDISH.sub("<uid>", (details or "").replace(run, "<run>")))
                  for action, details in _rows(db))


def _notes(caplog, uid):
    prefix = f"{uid}: "
    return [r.getMessage()[len(prefix):] for r in caplog.records
            if r.levelno == logging.INFO and r.getMessage().startswith(prefix)
            and "Planar Configuration" in r.getMessage()]


def _export(session, out, compress):
    summary = session.export(str(out), use_compression=compress, show_progress=False)
    assert summary.written >= 1, summary.failures
    return {pydicom.dcmread(os.path.join(root, f)).SOPInstanceUID:
            pydicom.dcmread(os.path.join(root, f))
            for root, _d, files in os.walk(str(out)) for f in files
            if f.endswith(".dcm")}


def _grade(session, path):
    session.generate_report(str(path))
    text = path.read_text(encoding="utf-8")
    return [g for g in ("PASS", "REVIEW_REQUIRED", "FAIL")
            if f"| **Validation Status** | **{g}** |" in text]


def _section_5(text):
    return text.split("\n## 5. Validation & Verification\n\n", 1)[1]


# ---------------------------------------------------------------------------
# U1, U2 -- the decode
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name, build", FAMILIES, ids=IDS)
def test_a_declared_1_decodes_as_its_0_twin(name, build):
    """U1. Red on main for the Pillow families: the colours were scrambled."""
    assert np.array_equal(_decoded(build(1)), _decoded(build(0))), name


@pytest.mark.parametrize("pc", [2, 7, [1, 1]], ids=["2", "7", "multi-valued"])
@pytest.mark.parametrize("name, build", FAMILIES, ids=IDS)
def test_any_declared_value_but_0_decodes_as_its_0_twin(name, build, pc):
    """U2 (R20a: every value but 0). Red on main: pydicom refused it, "must
    be 0 or 1"."""
    assert np.array_equal(_decoded(build(pc)), _decoded(build(0))), name


def test_the_decode_writes_0_on_the_dataset_only():
    """The one side effect, on the in-memory dataset; the file is untouched."""
    ds = FAMILIES[0][1](1)
    _decoded(ds)
    assert ds.PlanarConfiguration == 0


# ---------------------------------------------------------------------------
# U3 -- which syntaxes
# ---------------------------------------------------------------------------

def test_the_override_set_is_every_carried_compressed_syntax_but_rle():
    """U3. A syntax added to the carriable set goes red here until someone
    decides which side of the override it is on."""
    not_compressed_or_rle = {
        "1.2.840.10008.1.2", "1.2.840.10008.1.2.1", "1.2.840.10008.1.2.1.99",
        "1.2.840.10008.1.2.2", RLE}
    assert io_handlers._PLANAR_FIXED_BY_STREAM == (
        io_handlers._CARRIABLE_TRANSFER_SYNTAXES - not_compressed_or_rle)
    assert len(io_handlers._PLANAR_FIXED_BY_STREAM) == 11


# ---------------------------------------------------------------------------
# U4, U5 -- native and RLE are unchanged
# ---------------------------------------------------------------------------

def _native_planar(folder, pc=1):
    ds = encapsulated("1.2.840.10008.1.2.1", None, rows=4, cols=5, samples=3,
                      bits_allocated=8, planar=pc,
                      native=np.ascontiguousarray(RGB.transpose(2, 0, 1)).tobytes()
                      if pc == 1 else RGB.tobytes())
    return _save(folder, f"native{pc}", ds)


def test_a_native_declared_1_is_read_planar_with_no_note(tmp_path, caplog):
    """U4: under a native syntax a 1 means the bytes are color-by-plane."""
    path = _native_planar(tmp_path / "src")
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="isocenter"):
        with DicomSession(str(tmp_path / "s.db")) as session:
            assert session.ingest(os.path.dirname(path)).ingested == 1
            (inst,) = _instances(session)
            assert inst.attributes["0028,0006"] == 0
            assert PLANAR_OVERRIDDEN_ATTR not in inst.attributes
            assert np.array_equal(inst.get_pixel_data(), RGB)
            (written,) = _export(session, tmp_path / "out", False).values()
            assert np.array_equal(written.pixel_array, RGB)
            assert _notes(caplog, inst.sop_instance_uid) == []


def test_rle_is_not_overridden(tmp_path):
    """U5: RLE's decoder ignores the element (a 1 reads as a 0 did, with no
    key); a 2 is still refused in pydicom's words (the ruling's exclusion)."""
    declared = {}
    for pc in (0, 1):
        ds = _bundled("SC_rgb_rle.dcm", pc)
        declared[pc] = _decoded(ds)
        path = _save(tmp_path / f"src{pc}", "rle", _bundled("SC_rgb_rle.dcm", pc))
        with DicomSession(str(tmp_path / f"s{pc}.db")) as session:
            assert session.ingest(os.path.dirname(path)).ingested == 1
            (inst,) = _instances(session)
            assert PLANAR_OVERRIDDEN_ATTR not in inst.attributes
    assert np.array_equal(declared[0], declared[1])
    path = _save(tmp_path / "src2", "rle", _bundled("SC_rgb_rle.dcm", 2))
    with DicomSession(str(tmp_path / "s2.db")) as session:
        summary = session.ingest(os.path.dirname(path))
    assert summary.ingested == 0
    assert ("(0028,0006) 'Planar Configuration' value of '2' is invalid"
            in summary.failures[0][1]), summary.failures


# ---------------------------------------------------------------------------
# U6, U6b, U7 -- ingest, export, reopen
# ---------------------------------------------------------------------------

def _ingest_one(folder, db, ds, name):
    path = _save(folder, name, ds)
    session = DicomSession(str(db))
    # One secret for a store and its twin, so their date offsets agree.
    load_fixed_secret(session)
    summary = session.ingest(os.path.dirname(path))
    assert summary.ingested == 1, summary.failures
    return session


@pytest.mark.parametrize("name, build", FAMILIES, ids=IDS)
def test_ingest_and_export_read_and_write_0(tmp_path, caplog, name, build):
    """U6: the key and 0 on the graph; each export writes 0, the pixels of
    the declared-0 twin's export, and exactly one INFO note; the rows and
    the grade are the twin's."""
    out = {}
    for pc in (1, 0):
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="isocenter"):
            with _ingest_one(tmp_path / f"src{pc}", tmp_path / f"s{pc}.db",
                             build(pc), name) as session:
                (inst,) = _instances(session)
                assert inst.attributes["0028,0006"] == 0
                if pc == 1:
                    assert inst.attributes[PLANAR_OVERRIDDEN_ATTR] == "declared"
                else:
                    assert PLANAR_OVERRIDDEN_ATTR not in inst.attributes
                assert PLANAR_INFERRED_ATTR not in inst.attributes
                session.anonymize(session.audit())
                for compress in (False, True):
                    caplog.clear()
                    (written,) = _export(session,
                                         tmp_path / f"run{pc}" / f"o{compress}",
                                         compress).values()
                    assert written.PlanarConfiguration == 0
                    # Through the library's own decode: pydicom alone cannot
                    # read a 16-bit colour JPEG 2000 export back (Pillow).
                    out[(pc, compress)] = _decoded(written)
                    assert _notes(caplog, inst.sop_instance_uid) == (
                        [NOTE] if pc == 1 else [])
                out[(pc, "grade")] = _grade(session, tmp_path / f"r{pc}.md")
        out[(pc, "rows")] = _rows_without_uids(tmp_path / f"s{pc}.db", f"run{pc}")
    for compress in (False, True):
        assert np.array_equal(out[(1, compress)], out[(0, compress)]), compress
    assert out[(1, "rows")] == out[(0, "rows")]
    assert out[(1, "grade")] == out[(0, "grade")] == ["PASS"]


@pytest.mark.parametrize("name, build", [FAMILIES[0], FAMILIES[4]],
                         ids=["jpeg-baseline-pillow", "jpeg-ls"])
def test_a_declared_2_is_held_as_0(tmp_path, name, build):
    """U6b (R20a): refused before; the graph holds 0 and the export writes 0."""
    with _ingest_one(tmp_path / "src", tmp_path / "s.db", build(2), name) as session:
        (inst,) = _instances(session)
        assert inst.attributes["0028,0006"] == 0
        assert inst.attributes[PLANAR_OVERRIDDEN_ATTR] == "declared"
        (written,) = _export(session, tmp_path / "out", False).values()
        assert written.PlanarConfiguration == 0
        assert np.array_equal(written.pixel_array, _decoded(build(0)))


def test_the_note_survives_a_reopen(tmp_path, caplog):
    """U7: the key is stored, so a reopened store's export still says so."""
    with _ingest_one(tmp_path / "src", tmp_path / "s.db", FAMILIES[0][1](1),
                     "jpeg") as session:
        session.save(sync=True)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="isocenter"):
        with DicomSession(str(tmp_path / "s.db")) as session:
            (inst,) = _instances(session)
            assert inst.attributes[PLANAR_OVERRIDDEN_ATTR] == "declared"
            _export(session, tmp_path / "out", False)
            assert _notes(caplog, inst.sop_instance_uid) == [NOTE]


# ---------------------------------------------------------------------------
# U8 -- an icon
# ---------------------------------------------------------------------------

def _host_with_icon(pc):
    """SC_rgb_jpeg_dcmtk.dcm (declaring 0) whose Icon Image Sequence item is
    its own stream declaring `pc`."""
    ds = _bundled("SC_rgb_jpeg_dcmtk.dcm", 0, patient="P1137I")
    icon = Dataset()
    for keyword in ("Rows", "Columns", "SamplesPerPixel", "PhotometricInterpretation",
                    "BitsAllocated", "BitsStored", "HighBit", "PixelRepresentation"):
        setattr(icon, keyword, getattr(ds, keyword))
    _declare(icon, pc)
    icon.add_new(0x7FE00010, "OB", ds.PixelData)
    icon["PixelData"].is_undefined_length = True
    ds.IconImageSequence = Sequence([icon])
    return ds


def _icon_item(inst):
    return inst.sequences[ICON].items[0]


@pytest.mark.parametrize("declared", [1, 2])
def test_an_icon_declaring_1_is_carried_as_0(tmp_path, caplog, declared):
    """U8, and an icon declaring 2 (R20a), whose graph 0 is this change's
    own write: #1134's rewrite of a declared 1 does not reach it."""
    out = {}
    for pc in (declared, 0):
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="isocenter"):
            with _ingest_one(tmp_path / f"src{pc}", tmp_path / f"s{pc}.db",
                             _host_with_icon(pc), "icon") as session:
                (inst,) = _instances(session)
                item = _icon_item(inst)
                assert item.attributes["0028,0006"] == 0
                assert (item.attributes.get(PLANAR_OVERRIDDEN_ATTR)
                        == ("declared" if pc else None))
                assert PLANAR_OVERRIDDEN_ATTR not in inst.attributes
                (written,) = _export(session, tmp_path / f"o{pc}", False).values()
                icon = written.IconImageSequence[0]
                assert icon.PlanarConfiguration == 0
                out[pc] = icon.PixelData
                assert _notes(caplog, inst.sop_instance_uid) == (
                    [f"At {ICON}[0]: {NOTE}"] if pc else [])
    assert out[declared] == out[0]


# ---------------------------------------------------------------------------
# U9, U10 -- guards
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("pc, inferred, overridden", [
    ("absent", "absent", None), ("empty", "empty", None),
    (1, None, "declared"), (0, None, None)])
def test_the_two_keys_are_disjoint(tmp_path, caplog, pc, inferred, overridden):
    """U9: #1134's inference and this override never meet on one image."""
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="isocenter"):
        with _ingest_one(tmp_path / "src", tmp_path / "s.db", FAMILIES[4][1](pc),
                         "jls") as session:
            (inst,) = _instances(session)
            assert inst.attributes.get(PLANAR_INFERRED_ATTR) == inferred
            assert inst.attributes.get(PLANAR_OVERRIDDEN_ATTR) == overridden
            _export(session, tmp_path / "out", False)
            notes = _notes(caplog, inst.sop_instance_uid)
    assert (NOTE in notes) == (overridden is not None)
    assert any(n.startswith("Planar Configuration (0028,0006) "
                            + ("was absent" if pc == "absent" else "held no value"))
               for n in notes) == (inferred is not None)
    assert len(notes) == (inferred is not None) + (overridden is not None)


def test_no_pixels_or_one_sample_gets_no_key(tmp_path, caplog):
    """U10: a .50 colour header declaring 1 with no PixelData, and a
    monochrome .50 file declaring 1."""
    header = FAMILIES[0][1](1)
    del header.PixelData
    mono = _bundled("JPEG-lossy.dcm", 1, patient="P1137M")
    assert mono.SamplesPerPixel == 1
    assert mono.file_meta.TransferSyntaxUID in io_handlers._PLANAR_FIXED_BY_STREAM
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="isocenter"):
        for name, ds in (("nopix", header), ("mono", mono)):
            with _ingest_one(tmp_path / name, tmp_path / f"{name}.db", ds,
                             name) as session:
                (inst,) = _instances(session)
                assert PLANAR_OVERRIDDEN_ATTR not in inst.attributes, name
                _export(session, tmp_path / f"o{name}", False)
    assert NOTE not in caplog.text


# ---------------------------------------------------------------------------
# U11 -- the report
# ---------------------------------------------------------------------------

def test_the_report_counts_overrides_apart(tmp_path, monkeypatch):
    """U11 (R20b): one inferred image (#1134), two overridden images and one
    overridden icon, before any export and after a reopen: #1134's line
    and this one, in that order, between Pixel Scan and Methodology."""
    src = tmp_path / "src"
    _save(src, "inferred", _bundled("SC_rgb_jpeg_dcmtk.dcm", "absent", "P1"))
    _save(src, "over1", _bundled("SC_rgb_jpeg_dcmtk.dcm", 1, "P2"))
    _save(src, "over2", FAMILIES[4][1](2))
    _save(src, "icon", _host_with_icon(1))
    from isocenter import reporting  # pylint: disable=import-outside-toplevel
    seen = []
    real = reporting.MarkdownRenderer.render
    monkeypatch.setattr(reporting.MarkdownRenderer, "render",
                        lambda self, report, path: (seen.append(report),
                                                    real(self, report, path)))
    inferred_line = (f"{LINE}1 image(s) read with an inferred Planar "
                     f"Configuration (0028,0006), as 0 (color-by-pixel), "
                     f"because the source file stated none.\n")
    want = report_line("2 image(s) and 1 icon(s)")
    db = tmp_path / "s.db"
    with DicomSession(str(db)) as session:
        assert session.ingest(str(src)).ingested == 4
        session.generate_report(str(tmp_path / "r1.md"))
        session.save(sync=True)
    with DicomSession(str(db)) as session:
        session.generate_report(str(tmp_path / "r2.md"))
    for report in ("r1.md", "r2.md"):
        lines = _section_5((tmp_path / report).read_text()).splitlines(keepends=True)
        at = lines.index(inferred_line)
        assert lines[at + 1] == want, lines
        assert lines[at - 1].startswith("*   **Pixel Scan"), lines[at - 1]
        assert lines[at + 2].startswith("*   **Methodology:**"), lines[at + 2]
    assert seen[-1].planar_configuration_overridden == {"images": 2, "icons": 1}
    assert seen[-1].planar_configuration_inferred == {"images": 1, "icons": 0}


@pytest.mark.parametrize("images, icons, parts", [
    (1, 0, "1 image(s)"), (0, 1, "1 icon(s)"), (1, 1, "1 image(s) and 1 icon(s)")],
    ids=["images", "icons", "both"])
def test_each_form_of_the_line(tmp_path, images, icons, parts):
    src = tmp_path / "src"
    if images:
        _save(src, "over", _bundled("SC_rgb_jpeg_dcmtk.dcm", 1, "P2"))
    if icons:
        _save(src, "icon", _host_with_icon(1))
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(str(src))
        session.generate_report(str(tmp_path / "r.md"))
    section = _section_5((tmp_path / "r.md").read_text())
    assert [line + "\n" for line in section.splitlines()
            if line.startswith(LINE)] == [report_line(parts)], section


# ---------------------------------------------------------------------------
# U12 -- the file arm
# ---------------------------------------------------------------------------

def test_the_file_arm_reads_a_declared_1_right(tmp_path):
    """U12: `Instance.get_pixel_data()` on a file-backed instance."""
    path = _save(tmp_path / "src", "jpeg", FAMILIES[0][1](1))
    inst = Instance(pydicom.uid.generate_uid(), "1.2.840.10008.5.1.4.1.1.7", 1,
                    file_path=path)
    assert np.array_equal(inst.get_pixel_data(), _decoded(FAMILIES[0][1](0)))


def test_the_keys_are_the_ones_named():
    assert pixel_geometry.PLANAR_OVERRIDDEN_ATTR == PLANAR_OVERRIDDEN_ATTR
    assert pixel_geometry.PLANAR_INFERRED_ATTR == PLANAR_INFERRED_ATTR
