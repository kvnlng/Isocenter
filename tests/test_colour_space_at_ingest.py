"""A YBR colour source is stored and exported as the RGB it was decoded to (#372).

`ingest_worker` stores the bytes of pydicom's `pixel_array`, and at
pydicom 3 that property converts every 8-bit YBR family to RGB
(`as_rgb=True` is the default) **without touching**
`PhotometricInterpretation`. Ingest copied the declared label, the
sidecar held RGB, and the export-side rule from #186 -- correct only an
outright contradiction, leave a coherent label alone -- correctly left
it alone. So the exported file declared YBR over RGB bytes. Measured on
0.9.3 with a flat `(220, 40, 90)` source: a native `YBR_FULL` file
exported declaring `YBR_FULL` and a conformant reader showed
`(169, 255, 65)`; a `YBR_FULL_422` file (native or JPEG Baseline)
exported the same way, and with `use_compression=False` pydicom refused
the exported file outright (`ValueError: ... a third larger than
expected (12288 vs 8192 bytes) ... 'YBR_FULL_422' is incorrect`);
`YBR_ICT`/`YBR_RCT` JPEG 2000 sources read back correctly under a label
the transfer syntax does not permit.

**The population is not lossy sources**, as the issue framed it: it is
every 8-bit, 3-sample source declared `YBR_FULL` or `YBR_FULL_422` under
*any* transfer syntax -- ultrasound, native, uncompressed -- plus
`YBR_ICT`/`YBR_RCT` under JPEG 2000. Identical with Pillow alone and
with the pylibjpeg plugins installed.

The fix is where PlanarConfiguration is fixed: at ingest, from pydicom's
own decoder meta. `get_decoder(ts).as_array(ds)` is the call
`Dataset.pixel_array` makes and then discards the colour space of
(pydicom 3.0.2 `pixels/utils.py`, `arr, _ = decoder.as_array(...)`).
`0028,0004` is written only when the decoded colour space differs from
the declared one, so RGB, monochrome and palette sources bump no
revision.

Every fixture here is built with Pillow and pydicom only -- no encoder
plugin, no marker, no skip. The #183 spec's "no lossy-compressed fixture
of any family can be built in this venv" was wrong: Pillow is an
`install_requires`, and `Image.save(format="JPEG", subsampling=1)` and
`Image.save(format="JPEG2000", mct=1, no_jp2=True)` build every family
this needs.

Existing stores are **not** migrated (spec §0.2 B1): a store built by
0.9.3 or earlier from a YBR source still carries the source's label
over RGB bytes and exports the same file it did, because a load-time
relabel could not tell that row from a `set_pixel_data()` of genuine
YBR bytes. Re-ingest is the remedy.
"""
import io
import os
import sqlite3

import numpy as np
import pydicom
import pytest
from PIL import Image
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.encaps import encapsulate
from pydicom.uid import (ExplicitVRLittleEndian, ImplicitVRLittleEndian,
                         JPEG2000, JPEG2000Lossless, JPEGBaseline8Bit,
                         generate_uid)

from isocenter.entities import Instance
from isocenter.io_handlers import ingest_worker
from isocenter.session import DicomSession
from isocenter.sidecar import SidecarManager

#: One flat colour whose RGB and YBR triples are far apart, so a label
#: read wrong is unmistakable: `(220, 40, 90)` read as YBR_FULL shows
#: `(169, 255, 65)`.
RGB = (220, 40, 90)

#: 64x64: JPEG 2000 refuses 16x16 (too small for the default resolution
#: levels), and the packed 4:2:2 layout needs an even column count.
SIZE = 64

#: Per channel. Covers YBR round-trip rounding (`(221, 40, 91)` for the
#: native rows) and JPEG / irreversible-J2K quantisation at a flat colour.
TOL = 4

#: The five YBR kinds the fix covers and the three controls it must not
#: touch. `palette` is the one row the spec's own table did not measure
#: (§7 item 3): pydicom reports `PALETTE COLOR` back unchanged at 3.0.2
#: without `apply_color_lut`, and this pins that the write does not fire.
YBR_KINDS = ["native_ybr_full", "native_ybr_full_422", "jpeg_ybr_full_422",
             "j2k_ybr_ict", "j2k_ybr_rct"]
CONTROL_KINDS = ["native_rgb", "mono2", "palette"]

#: What each kind's source file declares, so the tests can say "the
#: label is what the source declared" for a control without a second
#: table to drift from.
DECLARED = {
    "native_ybr_full": "YBR_FULL",
    "native_ybr_full_422": "YBR_FULL_422",
    "jpeg_ybr_full_422": "YBR_FULL_422",
    "j2k_ybr_ict": "YBR_ICT",
    "j2k_ybr_rct": "YBR_RCT",
    "native_rgb": "RGB",
    "mono2": "MONOCHROME2",
    "palette": "PALETTE COLOR",
}


def ybr_full(rgb):
    """PS3.3 C.7.6.3.1.2, the YBR_FULL equations, rounded to 8 bits."""
    r, g, b = rgb
    y = 0.299 * r + 0.587 * g + 0.114 * b
    cb = -0.1687 * r - 0.3313 * g + 0.5 * b + 128
    cr = 0.5 * r - 0.4187 * g - 0.0813 * b + 128
    return tuple(int(round(v)) for v in (y, cb, cr))


def _base(ts):
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.7"
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ts
    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.SOPClassUID = meta.MediaStorageSOPClassUID
    ds.PatientID, ds.PatientName = "PAT372", "DOE^JOHN"
    ds.StudyInstanceUID, ds.SeriesInstanceUID = generate_uid(), generate_uid()
    ds.Modality, ds.SeriesNumber, ds.InstanceNumber = "OT", 1, 1
    ds.ConversionType = "WSD"
    ds.StudyDate = "20230101"
    ds.Rows = ds.Columns = SIZE
    ds.BitsAllocated = ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    return ds


def _colour(ds, photometric):
    ds.SamplesPerPixel = 3
    ds.PlanarConfiguration = 0
    ds.PhotometricInterpretation = photometric


def _encapsulated(ds, frame, lossy):
    ds.PixelData = encapsulate([frame])
    ds["PixelData"].is_undefined_length = True
    if lossy:
        ds.LossyImageCompression = "01"


def _rgb_image():
    return Image.fromarray(np.full((SIZE, SIZE, 3), RGB, dtype=np.uint8), "RGB")


def _ybr_fixture(folder, kind, frames=1):
    """One source file of `kind`; returns its path.

    The native 4:2:2 layout is `Y0 Y1 Cb Cr` per horizontal pixel pair
    (PS3.3 C.7.6.3.1.2), two bytes per pixel -- 8192 bytes for 64x64,
    against 12288 for the RGB that pydicom decodes it to, which is the
    "a third larger than expected" of the reader's refusal. J2K is
    written as a raw codestream (`no_jp2=True`): Pillow's default wraps
    it in a JP2 box, which is not what a DICOM fragment carries.
    """
    y, cb, cr = ybr_full(RGB)
    if kind == "native_ybr_full":
        ds = _base(ExplicitVRLittleEndian)
        _colour(ds, "YBR_FULL")
        frame = np.full((SIZE, SIZE, 3), (y, cb, cr), dtype=np.uint8).tobytes()
        if frames > 1:
            ds.NumberOfFrames = frames
        ds.PixelData = frame * frames
    elif kind == "native_ybr_full_422":
        assert frames == 1
        ds = _base(ExplicitVRLittleEndian)
        _colour(ds, "YBR_FULL_422")
        ds.PixelData = bytes([y, y, cb, cr]) * (SIZE * SIZE // 2)
    elif kind == "jpeg_ybr_full_422":
        assert frames == 1
        ds = _base(JPEGBaseline8Bit)
        _colour(ds, "YBR_FULL_422")
        buf = io.BytesIO()
        _rgb_image().save(buf, format="JPEG", subsampling=1, quality=95)
        _encapsulated(ds, buf.getvalue(), lossy=True)
    elif kind in ("j2k_ybr_ict", "j2k_ybr_rct"):
        assert frames == 1
        lossy = kind == "j2k_ybr_ict"
        ds = _base(JPEG2000 if lossy else JPEG2000Lossless)
        _colour(ds, "YBR_ICT" if lossy else "YBR_RCT")
        buf = io.BytesIO()
        _rgb_image().save(buf, format="JPEG2000", irreversible=lossy,
                          mct=1, no_jp2=True)
        _encapsulated(ds, buf.getvalue(), lossy=lossy)
    elif kind == "native_rgb":
        assert frames == 1
        ds = _base(ExplicitVRLittleEndian)
        _colour(ds, "RGB")
        ds.PixelData = np.full((SIZE, SIZE, 3), RGB, dtype=np.uint8).tobytes()
    elif kind == "mono2":
        assert frames == 1
        ds = _base(ExplicitVRLittleEndian)
        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = "MONOCHROME2"
        ds.PixelData = np.full((SIZE, SIZE), 7, dtype=np.uint8).tobytes()
    elif kind == "palette":
        assert frames == 1
        ds = _base(ExplicitVRLittleEndian)
        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = "PALETTE COLOR"
        lut = bytes(range(256))
        for colour in ("Red", "Green", "Blue"):
            setattr(ds, f"{colour}PaletteColorLookupTableDescriptor", [256, 0, 8])
            setattr(ds, f"{colour}PaletteColorLookupTableData", lut)
        ds.PixelData = np.full((SIZE, SIZE), 7, dtype=np.uint8).tobytes()
    else:
        raise ValueError(kind)

    path = os.path.join(str(folder), f"{kind}.dcm")
    ds.save_as(path, enforce_file_format=True)
    return path


def _close(actual, expected=RGB, tol=TOL):
    return all(abs(int(a) - int(e)) <= tol for a, e in zip(actual, expected))


def _assert_fixture_reads_as_source(path):
    """The fixture itself, read by pydicom, is the source colour.

    Before ingesting: a packed 4:2:2 written `Y Cb Cr Y` instead of
    `Y0 Y1 Cb Cr` would make the sidecar assertion a test of pydicom's
    tolerance rather than of Isocenter's correction (spec §7 item 4).
    """
    ds = pydicom.dcmread(path)
    px = ds.pixel_array[0, 0]
    assert _close(px), (
        "the %s fixture reads back as %r, not the source colour %r; the "
        "fixture is wrong, not the code under test"
        % (os.path.basename(path), tuple(px), RGB))


def _only_instance(session):
    return session.store.patients[0].studies[0].series[0].instances[0]


def _sidecar_first_triple(inst):
    """The first three bytes on disk, through the loader's own offset."""
    loader = inst._pixel_loader
    assert loader is not None, "the instance has no sidecar loader"
    raw = SidecarManager(loader.sidecar_path).read_frame(
        loader.offset, loader.length, loader.alg)
    return tuple(raw[:3]), len(raw)


def _exported(out):
    written = [os.path.join(r, f) for r, _d, files in os.walk(str(out))
               for f in files if f.endswith(".dcm")]
    assert len(written) == 1, written
    return pydicom.dcmread(written[0])


def _error_rows(db_path):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT entity_uid, details FROM audit_log "
            "WHERE action_type='ERROR'").fetchall()


# --- 1. The five YBR kinds ------------------------------------------------

@pytest.mark.parametrize("kind", YBR_KINDS)
def test_a_ybr_source_is_stored_and_exported_as_rgb(tmp_path, kind):
    """Label `RGB` in the graph, RGB on disk, and a conformant reader sees
    the source colour in the exported file.

    The *file's* label is `YBR_RCT` rather than `RGB` since #490: the
    default export compresses, and RGB samples encoded with the
    multiple-component transform are what PS3.5 8.2.4 gives that label
    to. The graph's label, the sidecar's bytes and the colour a reader
    sees are all unchanged, and they are what #372 is about.

    The sidecar assertion reads the bytes through the loader's
    offset/length, not `get_pixel_data()`, so it is about what is on
    disk. Killed by deleting the conditional `set_attr` in
    `ingest_worker`: the label stays YBR and the reader sees
    `(169, 255, 65)` or raises -- measured on every row before the fix.
    """
    src = tmp_path / "src"
    src.mkdir()
    path = _ybr_fixture(src, kind)
    _assert_fixture_reads_as_source(path)
    out = tmp_path / "out"

    with DicomSession(str(tmp_path / f"{kind}.db")) as session:
        session.ingest(str(src))
        inst = _only_instance(session)
        assert inst.attributes.get("0028,0004") == "RGB", (
            "a %s source ingested with PhotometricInterpretation %r over "
            "sidecar bytes pydicom decoded to RGB (#372)"
            % (kind, inst.attributes.get("0028,0004")))
        triple, length = _sidecar_first_triple(inst)
        assert _close(triple), (
            "the sidecar's first triple is %r, not RGB %r" % (triple, RGB))
        assert length == SIZE * SIZE * 3, length
        session.export(str(out), show_progress=False)

    exported = _exported(out)
    # `YBR_RCT`, not `RGB`, since #490: `export()` compresses by default
    # and an RGB source is encoded with the multiple-component transform,
    # which PS3.5 8.2.4 gives exactly that label under a reversible
    # encode. What #372 is about is unchanged and is what the colour
    # assertion below holds: the label names the transform the codestream
    # carries over RGB samples, where it used to name a colour space the
    # samples were not in.
    assert exported.PhotometricInterpretation == "YBR_RCT", (
        "the exported file declares %r over RGB samples carried through "
        "the multiple-component transform; a conformant reader shows the "
        "wrong colours or refuses the file (#372, #490)"
        % exported.PhotometricInterpretation)
    px = exported.pixel_array[0, 0]
    assert _close(px), (
        "a conformant reader sees %r where the source was %r" % (tuple(px), RGB))


# --- 2. The raw-export reader error --------------------------------------

def test_a_native_422_source_exports_raw_without_a_reader_error(tmp_path):
    """`use_compression=False` on a 4:2:2 source: the reader does not raise.

    Before the fix pydicom refused the exported file: `ValueError: ...
    a third larger than expected (12288 vs 8192 bytes) ... 'YBR_FULL_422'
    is incorrect`. The 12288 bytes are the RGB the sidecar holds; the
    8192 are what a `YBR_FULL_422` label promises.
    """
    src = tmp_path / "src"
    src.mkdir()
    _assert_fixture_reads_as_source(_ybr_fixture(src, "native_ybr_full_422"))
    out = tmp_path / "out"

    with DicomSession(str(tmp_path / "raw422.db")) as session:
        session.ingest(str(src))
        session.export(str(out), use_compression=False, show_progress=False)

    exported = _exported(out)
    assert exported.file_meta.TransferSyntaxUID == ImplicitVRLittleEndian
    assert exported.PhotometricInterpretation == "RGB"
    px = exported.pixel_array[0, 0]     # raised ValueError before #372
    assert _close(px), tuple(px)


# --- 3. Multi-frame --------------------------------------------------------

def test_a_two_frame_ybr_source_is_corrected_once(tmp_path):
    """Both frames are decoded and both are the source colour.

    Killed by a helper that decodes `index=0` only (the second frame is
    missing or garbage), or one that reads the label from `arr[0]`'s
    shape rather than from the decoder's meta.
    """
    src = tmp_path / "src"
    src.mkdir()
    _ybr_fixture(src, "native_ybr_full", frames=2)

    with DicomSession(str(tmp_path / "two.db")) as session:
        session.ingest(str(src))
        inst = _only_instance(session)
        assert inst.attributes.get("0028,0004") == "RGB"
        arr = inst.get_pixel_data()
        assert arr.shape == (2, SIZE, SIZE, 3), arr.shape
        for frame in range(2):
            assert _close(arr[frame, 0, 0]), (
                "frame %d is %r" % (frame, tuple(arr[frame, 0, 0])))
            assert _close(arr[frame, -1, -1]), (
                "frame %d ends %r" % (frame, tuple(arr[frame, -1, -1])))


# --- 4. The write fires only when the label changes ----------------------

@pytest.mark.parametrize("kind", ["native_ybr_full"] + CONTROL_KINDS)
def test_the_label_is_written_only_when_it_changes(tmp_path, monkeypatch, kind):
    """One correcting write for a YBR source; none for RGB, mono, palette.

    `ingest_worker` is called directly, in this process, so the
    monkeypatch on `Instance.set_attr` reaches it. The spec proposed
    `ISOCENTER_FORCE_THREADS=1` for that, and it does not work here:
    `Session.ingest()` hands `run_parallel` the session's own
    `ProcessPoolExecutor`, and `_run_on_shared_executor` uses whatever
    executor it is given -- the variable decides the strategy only when
    no executor is passed. The worker is a module-scope function by
    design (it has to pickle), so calling it is the honest shape.

    `populate_attrs` writes every tag through `set_attr`, including the
    declared `0028,0004`, so the count is what discriminates: a control
    sees exactly one write (the copy, equal to what it declared) and the
    YBR row sees two (the copy, then `RGB`). Killed by an unconditional
    write (controls see two) and by a write keyed on `samples >= 3`
    rather than the meta (`native_rgb` sees two, and the #186 line is
    back). `palette` is here because it is the one row the spec's table
    did not measure: pydicom reports `PALETTE COLOR` unchanged, so the
    write must not fire on it.
    """
    src = tmp_path / "src"
    src.mkdir()
    path = _ybr_fixture(src, kind)

    writes = []
    original = Instance.set_attr

    def recording(self, tag, value):
        if tag == "0028,0004":
            writes.append(value)
        return original(self, tag, value)

    monkeypatch.setattr(Instance, "set_attr", recording)
    _meta, inst, p_bytes, _h, _alg, _w, _wh, error = ingest_worker(path)
    assert error is None, error
    assert p_bytes, "no pixel bytes came back"

    declared = DECLARED[kind]
    if kind in YBR_KINDS:
        assert writes == [declared, "RGB"], writes
        assert inst.attributes["0028,0004"] == "RGB"
    else:
        assert writes == [declared], (
            "a %s source saw %r written to 0028,0004; the label must be "
            "written only when the decoded colour space differs from the "
            "declared one, so a control bumps no revision (#372)"
            % (kind, writes))
        assert inst.attributes["0028,0004"] == declared


# --- 8. Parity: the no-file_meta population still takes its refusal row --

def test_a_file_with_no_file_meta_still_takes_the_decompression_failed_row(
        tmp_path):
    """#281's population: a bare dataset with no preamble and no file_meta.

    `pixel_array` raised `AttributeError: Unable to decode the pixel data
    as the dataset's 'file_meta' has no (0002,0010) 'Transfer Syntax
    UID' element`; the helper's `ds.file_meta.TransferSyntaxUID` raises
    `AttributeError` too -- same type, same `except`, same row. What
    this pins is the row, not its text. Killed by a helper that reads
    the transfer syntax with a `.get()` default and decodes as Explicit
    VR LE: the file would ingest with garbage geometry and no row.
    """
    src = tmp_path / "src"
    src.mkdir()
    ds = Dataset()
    ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.7"
    ds.SOPInstanceUID = generate_uid()
    ds.PatientID, ds.PatientName = "PAT281", "DOE^JOHN"
    ds.StudyInstanceUID, ds.SeriesInstanceUID = generate_uid(), generate_uid()
    ds.Modality, ds.StudyDate = "OT", "20230101"
    ds.Rows = ds.Columns = 4
    ds.SamplesPerPixel = 1
    ds.BitsAllocated = ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelData = bytes(16)
    ds.is_little_endian = True
    ds.is_implicit_VR = True
    path = src / "bare.dcm"
    pydicom.dcmwrite(str(path), ds, enforce_file_format=False)
    assert pydicom.dcmread(str(path), force=True).preamble is None

    with DicomSession(str(tmp_path / "bare.db")) as session:
        summary = session.ingest(str(src))
        n_instances = sum(len(se.instances) for p in session.store.patients
                          for st in p.studies for se in st.series)
        db_path = session.store_backend.db_path

    assert summary.failed == 1 and summary.ingested == 0, summary
    assert n_instances == 0
    rows = _error_rows(db_path)
    assert len(rows) == 1, rows
    uid, details = rows[0]
    assert uid == str(path)
    assert "Decompression Failed" in details, details
