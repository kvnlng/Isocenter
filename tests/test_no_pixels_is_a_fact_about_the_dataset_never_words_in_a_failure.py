"""`get_pixel_data()` answers None from the dataset, never from a message (#1093).

`Instance.get_pixel_data()` returns None for an instance with no pixels.
Until #1093 its file arm also decided that by looking for two words in a
failure's message:

    except Exception as e:
        if "no pixel data" in str(e).lower():
            return None

pydicom quotes header values in its errors, and an `OSError` carries the
path it failed on, so a file -- or the folder it sits in -- could write
the words itself. Measured on e7c05a63, 3.12.14 and 3.14.7t, pydicom
3.0.2, every one of these returned None, each beside a control that
raised:

    Photometric Interpretation `NO PIXEL DATA`, 32 bytes of Pixel Data held
    Transfer Syntax UID `NO PIXEL DATA`, 32 bytes of Pixel Data held
    a source path under a folder named `No Pixel Data` that cannot be read

A caller could not tell a file that failed to decode from one with
nothing to decode.

The other half is what must not move. No dataset without a pixel element
ever needed the words: pydicom refuses it with an `AttributeError`, which
the arm above the predicate answers by asking the dataset for
(7FE0,0010), (7FE0,0008) and (7FE0,0009). Each of those cases is pinned
here beside a control that holds pixels.

Names `isocenter.entities`, `isocenter.io_handlers` and
`isocenter.session`.
"""
import os
import sqlite3

import numpy as np
import pydicom
import pytest
from pydicom.data import get_testdata_file
from pydicom.dataset import Dataset
from pydicom.uid import generate_uid

from isocenter.entities import Instance
from isocenter.io_handlers import ExportError
from isocenter.services import RedactionError
from isocenter.session import DicomSession
from support.decode_doors import (  # pylint: disable=unused-import
    EXPLICIT_LE, J2K_LOSSLESS, JPEGLS, LJPEG_SV1, SOP_CLASS, dataset,
    pydicom_cannot)

IMPLICIT_LE = "1.2.840.10008.1.2"
JPEG_BASELINE = "1.2.840.10008.1.2.4.50"
RLE = "1.2.840.10008.1.2.5"
#: Two transfer syntaxes no decoder implements, and no word of the predicate.
PRIVATE_TS = "1.2.840.113619.5.2"
MPEG2 = "1.2.840.10008.1.2.4.100"
#: The words, in the two cases a header value can hold them.
WORDS = ["NO PIXEL DATA", "no pixel data"]
PIXEL_TAGS = (0x7FE00010, 0x7FE00008, 0x7FE00009)
SAMPLES = np.arange(16, dtype=np.uint16).reshape(4, 4)


def _image(ts=EXPLICIT_LE, label="MONOCHROME2", pixels=True):
    """A 4x4 16-bit image under `ts`; without its Pixel Data on request."""
    return dataset(ts, rows=4, cols=4, bits_allocated=16, photometric=label,
                   native=SAMPLES.tobytes(),
                   drop=() if pixels else ("PixelData",))


def _write(folder, ds, name="one.dcm"):
    """Write `ds` as little-endian bytes, explicit VR unless it says implicit.

    Not `decode_doors.write`: that lets pydicom choose the encoding from
    the Transfer Syntax UID, which it cannot do for a UID that is no
    transfer syntax.
    """
    os.makedirs(str(folder), exist_ok=True)
    path = os.path.join(str(folder), name)
    implicit = str(ds.file_meta.TransferSyntaxUID) == IMPLICIT_LE
    ds.save_as(path, implicit_vr=implicit, little_endian=True,
               enforce_file_format=False)
    return path


def _holds_a_pixel_element(path):
    ds = pydicom.dcmread(path, force=True)
    return any(tag in ds for tag in PIXEL_TAGS)


def _read(path):
    """`(uid, what get_pixel_data() returned or raised, the instance)`."""
    uid = generate_uid()
    inst = Instance(uid, SOP_CLASS, 1, file_path=path)
    try:
        return uid, inst.get_pixel_data(), inst
    except Exception as exc:  # pylint: disable=broad-except
        return uid, exc, inst


def _refused(got, uid, why):
    """`got` is the read door's whole sentence and nothing else."""
    assert isinstance(got, RuntimeError), repr(got)
    assert str(got) == f"Lazy load failed for instance {uid}: {why}", str(got)


# ---------------------------------------------------------------------------
# The words, written by the file: a refusal, as its control is
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("label", WORDS + ["NO PIXELS"])
def test_a_label_holding_the_words_is_refused_as_any_unknown_label_is(
        tmp_path, label):
    """The issue's reproducer and its control, in one sentence.

    Red on e7c05a63 for the two labels holding the words: the read
    returned None over 32 bytes of Pixel Data. `NO PIXELS` is the
    control and raised this sentence already.
    """
    path = _write(tmp_path, _image(label=label))
    assert _holds_a_pixel_element(path)
    with pytest.raises(ValueError) as theirs:
        _ = pydicom.dcmread(path).pixel_array
    words = (f"Unknown (0028,0004) 'Photometric Interpretation' value "
             f"'{label}'")
    assert str(theirs.value) == words, "the control: pydicom alone says this"

    uid, got, inst = _read(path)
    _refused(got, uid, f"ValueError: {words}")
    assert inst.pixel_array is None


@pytest.mark.parametrize("ts", ["NO PIXEL DATA", PRIVATE_TS])
def test_a_transfer_syntax_uid_holding_the_words_is_refused_as_any_unknown_one_is(
        tmp_path, ts):
    """pydicom quotes (0002,0010) too, and the file holds Pixel Data.

    Red on e7c05a63 for `NO PIXEL DATA`: None. The private syntax is the
    control and raised this sentence already.
    """
    path = _write(tmp_path, _image(ts=ts))
    assert _holds_a_pixel_element(path)
    uid, got, inst = _read(path)
    _refused(got, uid,
             f"NotImplementedError: Unable to decode the pixel data as a "
             f"(0002,0010) 'Transfer Syntax UID' value of '{ts}' is not "
             f"supported")
    assert inst.pixel_array is None


@pytest.mark.parametrize("keyword, value", [
    ("PixelData", b""),
    ("FloatPixelData", np.zeros(16, dtype=np.float32).tobytes()),
    ("DoubleFloatPixelData", np.zeros(16, dtype=np.float64).tobytes()),
], ids=["an-empty-pixel-data", "float-pixel-data", "double-float-pixel-data"])
def test_each_of_the_three_elements_is_a_pixel_element_and_presence_is_what_counts(
        tmp_path, keyword, value):
    """A file holding any one of the three is refused, never read as None.

    Under the private syntax, so the refusal arrives in the arm #1093
    changed. An empty Pixel Data is still a pixel element: the question
    is whether the file holds one, not whether it holds samples. Green
    on e7c05a63; red if the question drops an element or asks the value.
    """
    ds = _image(ts=PRIVATE_TS, pixels=False)
    setattr(ds, keyword, value)
    path = _write(tmp_path, ds)
    on_disk = pydicom.dcmread(path, force=True)
    assert [tag in on_disk for tag in PIXEL_TAGS].count(True) == 1
    uid, got, _inst = _read(path)
    _refused(got, uid,
             f"NotImplementedError: Unable to decode the pixel data as a "
             f"(0002,0010) 'Transfer Syntax UID' value of '{PRIVATE_TS}' is "
             f"not supported")


@pytest.mark.parametrize("folder", ["No Pixel Data", "Other"])
def test_a_path_holding_the_words_is_refused_as_any_unreadable_source_is(
        tmp_path, folder):
    """An `OSError` repeats the path in its message; the read's does not.

    Red on e7c05a63 for `No Pixel Data`: None, for a source that is
    there and cannot be read (a directory, which fails the same way on
    every platform and for every user). `Other` is the control.
    """
    path = os.path.join(str(tmp_path), folder, "one.dcm")
    os.makedirs(path)
    with pytest.raises(OSError) as theirs:
        pydicom.dcmread(path, force=True)
    assert folder in str(theirs.value), "the control: the path is in the message"

    uid, got, _inst = _read(path)
    _refused(got, uid, "IsADirectoryError: Is a directory")
    assert folder not in str(got)


@pytest.mark.parametrize("raised, reads_as_none", [
    (AttributeError("no dataset"), True),
    (TypeError("no dataset"), True),
    (ValueError("no dataset"), False),
    (ValueError("no pixel data"), False),
])
def test_a_read_that_fails_before_there_is_a_dataset_keeps_its_two_answers(
        tmp_path, monkeypatch, raised, reads_as_none):
    """`dcmread` itself failing: None for two classes, as before, by class.

    The one case that is not the dataset's answer, kept as it was: an
    `AttributeError` or `TypeError` out of `dcmread` reads as None. Every
    other class raises, whatever its message says. Patched, so that each
    class is asked alone and by class: no file is known to make
    `dcmread(force=True)` raise either class when read by its path
    (the review of #1096 read 18,548 single-byte changes to one of
    pydicom's bundled files, `image_dfl.dcm`: each of its 4637 offsets
    set to four values; none did), so the case is pinned by this patch
    only. The real `OSError` is the directory test above.
    """
    path = _write(tmp_path, _image())

    def _fails(*_args, **_kwargs):
        raise raised

    monkeypatch.setattr(pydicom, "dcmread", _fails)
    uid, got, _inst = _read(path)
    if reads_as_none:
        assert got is None
    else:
        _refused(got, uid, f"ValueError: {raised}")


def _export_after_the_source_became_unreadable(tmp_path, folder):
    """Ingest one pixel-less file under `folder`, lose it, export."""
    src = os.path.join(str(tmp_path), folder)
    path = _write(src, _image(pixels=False))
    db = os.path.join(str(tmp_path), "s.db")
    out = os.path.join(str(tmp_path), "out")
    raised = None
    with DicomSession(persistence_file=db) as session:
        summary = session.ingest(src)
        assert summary.ingested == 1 and not summary.failures
        os.remove(path)
        os.makedirs(path)
        try:
            session.export(out, format="dicom", show_progress=False)
        except ExportError as exc:
            raised = exc
    written = [name for _root, _dirs, names in os.walk(out) for name in names]
    with sqlite3.connect(db) as conn:
        errors = [row[0] for row in conn.execute(
            "SELECT details FROM audit_log WHERE action_type = 'ERROR'")]
    return raised, written, errors


@pytest.mark.parametrize("folder", ["no pixel data", "other"])
def test_an_export_whose_source_cannot_be_read_fails_whatever_its_folder_is_named(
        tmp_path, folder):
    """The one export outcome the words decided, and its control.

    An ingested instance with no pixel element has no frame in the
    store, so every export asks its source file again. Where that file
    is there and cannot be read, the export fails the instance with one
    `ERROR` row -- unless, on e7c05a63, a folder above it was named for
    the words: then the read returned None and the file was written.
    Red there for `no pixel data` (written, no row).
    """
    raised, written, errors = _export_after_the_source_became_unreadable(
        tmp_path, folder)
    assert isinstance(raised, ExportError)
    assert written == []
    assert len(errors) == 1, errors
    assert "Lazy load failed for instance" in errors[0]
    assert errors[0].endswith("IsADirectoryError: Is a directory"), errors[0]
    assert folder not in errors[0]


# ---------------------------------------------------------------------------
# No pixel element: None, by the dataset, as before
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ts", [IMPLICIT_LE, EXPLICIT_LE, JPEG_BASELINE,
                                LJPEG_SV1, JPEGLS, J2K_LOSSLESS, RLE])
def test_a_file_with_no_pixel_element_reads_as_none_under_every_decoded_syntax(
        tmp_path, ts):
    """Native, pydicom's plugins and the fallback's syntaxes alike.

    The descriptors are all there (Rows, Columns, BitsAllocated, a
    label); only the element is not. pydicom refuses with an
    `AttributeError` before any decoder is chosen, and the dataset
    answers. Green on e7c05a63: the selectivity guard for the fix.
    """
    path = _write(tmp_path, _image(ts=ts, pixels=False))
    assert not _holds_a_pixel_element(path)
    with pytest.raises(AttributeError):
        _ = pydicom.dcmread(path).pixel_array
    _uid, got, inst = _read(path)
    assert got is None
    assert inst.pixel_array is None


@pytest.mark.parametrize("ts", [MPEG2, PRIVATE_TS, "NO PIXEL DATA"])
def test_a_file_with_no_pixel_element_reads_as_none_under_a_syntax_no_decoder_reads(
        tmp_path, ts):
    """The dataset answers in the other arm too (owner ruling, option A).

    Under a transfer syntax no decoder implements pydicom raises
    `NotImplementedError`, not `AttributeError`, before it looks for the
    element. On e7c05a63 that read raised `Lazy load failed ...: Unable
    to decode the pixel data ...` about pixels the file does not hold --
    red there for MPEG2 and the private syntax -- unless the UID spelled
    the words, which returned None then and does now, by the dataset.
    The control is the same syntax over a file that holds Pixel Data:
    `test_a_transfer_syntax_uid_holding_the_words_is_refused_as_any_unknown_one_is`.
    """
    path = _write(tmp_path, _image(ts=ts, pixels=False))
    assert not _holds_a_pixel_element(path)
    with pytest.raises(NotImplementedError):
        _ = pydicom.dcmread(path).pixel_array
    _uid, got, inst = _read(path)
    assert got is None
    assert inst.pixel_array is None


@pytest.mark.parametrize("compressed", [True, False])
@pytest.mark.parametrize("ts", [MPEG2, PRIVATE_TS])
def test_an_instance_with_no_pixels_exports_whatever_syntax_its_source_named(
        tmp_path, ts, compressed):
    """A report is not failed for a decoder its pixels would have needed.

    Ingest admits the file: it holds nothing to decode. On e7c05a63 its
    export then failed, `0 of 1`, with one `ERROR` row ending `Unable to
    decode the pixel data as a (0002,0010) 'Transfer Syntax UID' value
    of '...' is not supported`. Red there. It is written as every file
    with no pixels is, Implicit VR Little Endian, and what was written
    is ingested again.
    """
    src = os.path.join(str(tmp_path), "src")
    _write(src, _image(ts=ts, pixels=False))
    db = os.path.join(str(tmp_path), "s.db")
    out = os.path.join(str(tmp_path), "out")
    with DicomSession(persistence_file=db) as session:
        summary = session.ingest(src)
        assert summary.ingested == 1 and not summary.failures
        session.export(out, format="dicom", use_compression=compressed,
                       show_progress=False)
    written = [os.path.join(root, name)
               for root, _dirs, names in os.walk(out) for name in names]
    assert len(written) == 1, written
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT details FROM audit_log WHERE "
                            "action_type = 'ERROR'").fetchall() == []
    exported = pydicom.dcmread(written[0])
    assert str(exported.file_meta.TransferSyntaxUID) == IMPLICIT_LE
    assert not any(tag in exported for tag in PIXEL_TAGS)
    assert exported.Modality == "OT"

    with DicomSession(persistence_file=os.path.join(
            str(tmp_path), "again.db")) as session:
        summary = session.ingest(out)
        assert summary.ingested == 1 and not summary.failures


def test_the_same_file_with_its_pixel_element_reads_an_array(tmp_path):
    """The control for the test above: the element is what decides."""
    path = _write(tmp_path, _image())
    assert _holds_a_pixel_element(path)
    _uid, got, _inst = _read(path)
    assert isinstance(got, np.ndarray) and got.tolist() == SAMPLES.tolist()


@pytest.mark.parametrize("ts", [LJPEG_SV1, JPEGLS, J2K_LOSSLESS])
def test_a_file_with_no_pixel_element_reads_as_none_where_pydicom_has_no_plugin(
        tmp_path, pydicom_cannot, ts):
    """The imagecodecs route: the header is validated before the fallback.

    `pydicom_cannot` makes pydicom's decoder raise the `RuntimeError` of
    an installation with no plugin, so `_decode_pixels` goes on to
    validate the header as pydicom would and to ask imagecodecs. The
    validation refuses a dataset with no pixel element with pydicom's
    own `AttributeError`, and the dataset answers.
    """
    path = _write(tmp_path, _image(ts=ts, pixels=False))
    _uid, got, _inst = _read(path)
    assert pydicom_cannot["n"] >= 1, "pydicom was never asked"
    assert got is None


@pytest.mark.parametrize("name", ["waveform_ecg.dcm", "test-SR.dcm",
                                  "rtplan.dcm", "reportsi.dcm"])
def test_a_waveform_a_report_and_a_plan_read_as_none(name):
    """Real objects with no pixel element, as pydicom bundles them."""
    path = get_testdata_file(name)
    assert not _holds_a_pixel_element(path)
    _uid, got, _inst = _read(path)
    assert got is None


def test_a_file_with_no_file_meta_and_no_pixel_element_reads_as_none(tmp_path):
    """A raw dump: forced at ingest, so forced here, and it holds no pixels."""
    ds = Dataset()
    ds.PatientID, ds.SOPInstanceUID = "P1093", generate_uid()
    ds.Rows, ds.Columns = 4, 4
    path = os.path.join(str(tmp_path), "raw.dcm")
    ds.save_as(path, implicit_vr=True, little_endian=True)
    assert not _holds_a_pixel_element(path)
    _uid, got, _inst = _read(path)
    assert got is None


def test_a_file_that_is_not_dicom_reads_as_none(tmp_path):
    """The documented exception: a forced read parses it to nothing."""
    path = os.path.join(str(tmp_path), "notes.dcm")
    with open(path, "wb") as handle:
        handle.write(b"not a DICOM file at all, only some text\n" * 4)
    _uid, got, _inst = _read(path)
    assert got is None


def test_an_ingested_instance_with_no_pixel_element_reads_as_none_and_exports(
        tmp_path):
    """Through a session, live and reopened: no loader, the source file asked.

    The control is the image ingested beside it, which reads its array
    from the store. Both are written by the export.
    """
    src = os.path.join(str(tmp_path), "src")
    _write(src, _image(pixels=False), "report.dcm")
    _write(src, _image(), "image.dcm")
    db = os.path.join(str(tmp_path), "s.db")
    out = os.path.join(str(tmp_path), "out")

    def _by_pixels(session):
        found = {}
        for patient in session.store.patients:
            for study in patient.studies:
                for series in study.series:
                    for inst in series.instances:
                        found[inst._pixel_loader is not None] = inst  # pylint: disable=protected-access
        return found[False], found[True]

    with DicomSession(persistence_file=db) as session:
        assert session.ingest(src).ingested == 2
        report, image = _by_pixels(session)
        assert report.file_path and report.get_pixel_data() is None
        assert image.unload_pixel_data() is True
        assert image.get_pixel_data().tolist() == SAMPLES.tolist()
        session.export(out, format="dicom", show_progress=False)
    written = [os.path.join(root, name)
               for root, _dirs, names in os.walk(out) for name in names]
    assert len(written) == 2
    assert sorted(_holds_a_pixel_element(path) for path in written) == [
        False, True]

    with DicomSession(persistence_file=db) as session:
        report, image = _by_pixels(session)
        assert report.get_pixel_data() is None
        assert image.get_pixel_data().tolist() == SAMPLES.tolist()


# ---------------------------------------------------------------------------
# Pixels that are elsewhere are not "no pixels" (owner ruling, 2026-10-12)
# ---------------------------------------------------------------------------
#
# A JPIP Referenced image holds no pixel element: its pixels are on a
# server, named by (0028,7FE0) Pixel Data Provider URL. pydicom has no
# decoder for its transfer syntax and raises `NotImplementedError`, which
# is the arm #1093 opened. Read as "no pixels" the image would be
# exported without its pixels, with the URL, graded PASS, and `redact()`
# would return 0 over an image whose zones were never applied. It keeps
# the refusal it had: no door, row, grade or byte moves for a dataset
# that holds the URL or is under a JPIP Referenced syntax.

JPIP = ["1.2.840.10008.1.2.4.94", "1.2.840.10008.1.2.4.95",
        "1.2.840.10008.1.2.4.204", "1.2.840.10008.1.2.4.205"]
URL = "http://jpip.example.test/server?target=T-1093&fsiz=4,4"
SERIAL = "SN-1093"


def _elsewhere(ts, url=True):
    """A 4x4 image header with no pixel element, and the URL on request."""
    ds = _image(ts=ts, pixels=False)
    ds.Manufacturer, ds.ManufacturerModelName = "Acme", "Elsewhere"
    ds.DeviceSerialNumber = SERIAL
    if url:
        ds.PixelDataProviderURL = URL
    return ds


def _not_supported(ts):
    """pydicom's sentence for a transfer syntax it has no decoder for."""
    return (f"NotImplementedError: Unable to decode the pixel data as a "
            f"(0002,0010) 'Transfer Syntax UID' value of "
            f"'{pydicom.uid.UID(ts).name}' is not supported")


#: `(transfer syntax, URL held)`: each half of the condition alone, and both.
ELSEWHERE = ([(ts, True) for ts in JPIP] + [(ts, False) for ts in JPIP]
             + [(MPEG2, True), (PRIVATE_TS, True)])


@pytest.mark.parametrize("ts, url", ELSEWHERE)
def test_a_file_whose_pixels_are_elsewhere_is_refused_as_it_was(
        tmp_path, ts, url):
    """The URL alone, a JPIP Referenced syntax alone, and both.

    Red on 0a5bc8ac, where every one of these read as None. The control
    is the same header under MPEG2 or the private syntax with no URL,
    which reads as None:
    `test_a_file_with_no_pixel_element_reads_as_none_under_a_syntax_no_decoder_reads`.
    """
    path = _write(tmp_path, _elsewhere(ts, url))
    assert not _holds_a_pixel_element(path)
    uid, got, inst = _read(path)
    _refused(got, uid, _not_supported(ts))
    assert inst.pixel_array is None


@pytest.mark.parametrize("ts", [MPEG2, PRIVATE_TS])
def test_an_empty_provider_url_still_says_the_pixels_are_elsewhere(
        tmp_path, ts):
    """Presence of (0028,7FE0) is what is asked, never its value.

    As for the pixel elements: a file that holds the element with
    nothing in it is refused, as it was before #1093. Without this a
    rule that asked for a URL that is not empty passed every test here.
    """
    ds = _elsewhere(ts, url=False)
    ds.PixelDataProviderURL = ""
    path = _write(tmp_path, ds)
    held = pydicom.dcmread(path, force=True)
    assert 0x00287FE0 in held and not held.PixelDataProviderURL
    uid, got, _inst = _read(path)
    _refused(got, uid, _not_supported(ts))


def _url_in_an_item_only(ts):
    """The header with no URL of its own and one inside a sequence item."""
    ds = _elsewhere(ts, url=False)
    item = Dataset()
    item.ReferencedSOPClassUID = SOP_CLASS
    item.ReferencedSOPInstanceUID = generate_uid()
    item.PixelDataProviderURL = URL
    ds.SourceImageSequence = [item]
    return ds


def test_a_url_inside_a_sequence_item_is_not_the_data_sets_own(tmp_path):
    """The URL is asked for at the top level only (owner ruling, 2026-10-12).

    PS3.5 8.2 puts the provider URL in the top-level data set, and the
    three pixel elements are asked for there too. A URL inside an item
    says nothing of this data set's pixels: under MPEG2 the file has no
    pixels and is written, the item and its URL with it. It raised
    before #1093, when every such file did. The controls: the same URL
    at the top level is refused
    (`test_a_file_whose_pixels_are_elsewhere_is_refused_as_it_was`), and
    the same file under a JPIP Referenced syntax is refused by the
    syntax.
    """
    path = _write(tmp_path, _url_in_an_item_only(MPEG2))
    held = pydicom.dcmread(path, force=True)
    assert 0x00287FE0 not in held
    assert held.SourceImageSequence[0].PixelDataProviderURL == URL
    _uid, got, _inst = _read(path)
    assert got is None

    src = os.path.join(str(tmp_path), "src")
    _write(src, _url_in_an_item_only(MPEG2))
    out = os.path.join(str(tmp_path), "out")
    with DicomSession(persistence_file=os.path.join(str(tmp_path), "s.db")) as session:
        assert session.ingest(src).ingested == 1
        session.export(out, format="dicom", show_progress=False)
    written = [os.path.join(root, name)
               for root, _dirs, names in os.walk(out) for name in names
               if name.endswith(".dcm")]
    assert len(written) == 1
    back = pydicom.dcmread(written[0])
    assert 0x00287FE0 not in back and not _holds_a_pixel_element(written[0])
    assert back.SourceImageSequence[0].PixelDataProviderURL == URL

    jpip = _write(tmp_path, _url_in_an_item_only(JPIP[0]), "jpip.dcm")
    uid, got, _inst = _read(jpip)
    _refused(got, uid, _not_supported(JPIP[0]))


def test_the_one_file_holding_the_url_that_moves_spelled_the_words(tmp_path):
    """Transfer Syntax UID `NO PIXEL DATA`, the URL held, no pixel element.

    The one shape holding (0028,7FE0) that is not as it was (accepted by
    the owner, 2026-10-12). Before #1093 it read as None by the words in
    pydicom's refusal of that UID, and was exported with its URL and no
    pixels; it is refused now, as the same file is under any other UID no
    decoder reads. The control is the same file without the URL, which
    has no pixels and reads as None, before by the words and now by the
    dataset.
    """
    words = "NO PIXEL DATA"
    path = _write(tmp_path, _elsewhere(words))
    uid, got, _inst = _read(path)
    _refused(got, uid, _not_supported(words))
    _uid, got, _inst = _read(
        _write(tmp_path, _elsewhere(words, url=False), "control.dcm"))
    assert got is None


@pytest.mark.parametrize("ts", JPIP[:2] + [MPEG2])
def test_an_image_whose_pixels_are_elsewhere_is_not_exported_without_them(
        tmp_path, ts):
    """`export()` fails the instance, with its row, and the grade says so.

    Red on 0a5bc8ac: the file was written with the URL and no pixels,
    `(0012,0062) YES`, no row, graded PASS. For MPEG2 it is the URL
    that decides; the same file without it is written
    (`test_an_instance_with_no_pixels_exports_whatever_syntax_its_source_named`).
    """
    src = os.path.join(str(tmp_path), "src")
    _write(src, _elsewhere(ts))
    db = os.path.join(str(tmp_path), "s.db")
    out = os.path.join(str(tmp_path), "out")
    report = os.path.join(str(tmp_path), "report.md")
    with DicomSession(persistence_file=db) as session:
        summary = session.ingest(src)
        assert summary.ingested == 1 and not summary.failures
        session.anonymize(session.audit())
        with pytest.raises(ExportError):
            session.export(out, format="dicom", show_progress=False)
        session.generate_report(report)
    assert [name for _r, _d, names in os.walk(out) for name in names
            if name.endswith(".dcm")] == []
    with sqlite3.connect(db) as conn:
        errors = [row[0] for row in conn.execute(
            "SELECT details FROM audit_log WHERE action_type = 'ERROR'")]
    assert len(errors) == 1, errors
    assert errors[0].startswith("Export failed for instance ")
    assert errors[0].endswith(_not_supported(ts)), errors[0]
    with open(report, encoding="utf-8") as handle:
        assert "| **Validation Status** | **REVIEW_REQUIRED** |" in handle.read()


@pytest.mark.parametrize("ts, raises", [(JPIP[0], True), (JPIP[1], True),
                                        (MPEG2, False)])
def test_redact_does_not_pass_over_an_image_whose_pixels_are_elsewhere(
        tmp_path, ts, raises):
    """A matching zone that was never applied is a failure, not a 0.

    Red on 0a5bc8ac for the two JPIP syntaxes: `redact()` returned 0
    with no row. The control is the MPEG2 header with no URL, which has
    no pixels to redact and returns 0.
    """
    src = os.path.join(str(tmp_path), "src")
    _write(src, _elsewhere(ts, url=raises))
    db = os.path.join(str(tmp_path), "s.db")
    with DicomSession(persistence_file=db) as session:
        assert session.ingest(src).ingested == 1
        # The rule below matches this machine: without that the control's
        # 0 would be "no rule applied", not "nothing to redact".
        assert [series.equipment.device_serial_number
                for patient in session.store.patients
                for study in patient.studies
                for series in study.series] == [SERIAL]
        session.configuration.rules = [
            {"serial_number": SERIAL, "redaction_zones": [[0, 2, 0, 2]]}]
        if raises:
            with pytest.raises(RedactionError):
                session.redact(show_progress=False)
        else:
            assert session.redact(show_progress=False) == 0
    with sqlite3.connect(db) as conn:
        errors = [row[0] for row in conn.execute(
            "SELECT details FROM audit_log WHERE action_type = 'ERROR'")]
    assert len(errors) == (1 if raises else 0), errors
    if raises:
        assert _not_supported(ts) in errors[0], errors[0]


def test_a_url_under_a_syntax_pydicom_decodes_reads_as_it_did(tmp_path):
    """Not this change's: the arm above answers it, as on main.

    Under Explicit VR Little Endian pydicom raises `AttributeError` for a
    dataset with no pixel element whether or not it names a provider, and
    that arm read it as None before #1093 and does now. Pinned so that
    the exception above is not widened into it unnoticed; whether that
    answer is right is another question, and not this file's.
    """
    path = _write(tmp_path, _elsewhere(EXPLICIT_LE))
    with pytest.raises(AttributeError):
        _ = pydicom.dcmread(path).pixel_array
    _uid, got, _inst = _read(path)
    assert got is None


# ---------------------------------------------------------------------------
# "Whatever the decode raised", for a class no file is known to reach
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raised", [ValueError("x"), RuntimeError("x"),
                                    KeyError("x"), OSError("x")])
def test_the_dataset_answers_whatever_class_the_decode_raised(
        tmp_path, monkeypatch, raised):
    """The rule is any class, and every real file arrives with one.

    A file with no pixel element reaches the second arm only through
    `NotImplementedError`, so a rule narrowed to that class would pass
    every other test here. Patched for the rest: None for a dataset with
    no pixel element, a raise for one whose pixels are elsewhere.
    """
    from isocenter import entities  # pylint: disable=import-outside-toplevel

    def _fails(_ds):
        raise raised

    monkeypatch.setattr(entities, "_decode_from_file", _fails)
    _uid, got, _inst = _read(_write(tmp_path, _image(pixels=False)))
    assert got is None
    uid, got, _inst = _read(_write(tmp_path, _elsewhere(EXPLICIT_LE), "u.dcm"))
    assert isinstance(got, RuntimeError)
    assert str(got).startswith(f"Lazy load failed for instance {uid}: "
                               f"{type(raised).__name__}")
