"""A read door that returns RGB samples says RGB, whichever decoder converted them (#482).

#464 made the imagecodecs arm of both read doors convert 8-bit YBR_FULL
JPEG-LS and relabel what it converted. Two other paths returned RGB
samples under the file's YBR label, which is #372's shape: a decoded
array under a label that is not true of it.

1. **The handler, under JPEG 2000 `YBR_RCT`/`YBR_ICT`.** `jpeg2k_decode`
   undoes the codestream's colour transform itself and returns RGB, at 8
   and 16 bits. `ingest()` has stored that under `RGB` since #448
   (`io_handlers._FALLBACK_DECODER_CONVERTS`). The handler returned the
   same RGB bytes and left `ds` saying `YBR_RCT`, and a labelled instance
   read through it kept `YBR_RCT` too. 16-bit reaches the handler from
   `Instance.get_pixel_data()`, because Pillow, pydicom's only J2K plugin
   here, refuses 16-bit multi-sample.
2. **`Instance.get_pixel_data()`'s pydicom arm, for any 8-bit YBR source
   pydicom decodes.** `ds.pixel_array` returns RGB by default
   (`as_rgb=True`) and leaves the dataset's label alone, so a labelled
   instance kept `YBR_FULL` over RGB bytes. The label pydicom gives its
   output is in the decoder's meta, which `pixel_array` discards; ingest
   reads it (`io_handlers._decode_pixels`).

**Why it mattered on disk.** Export writes the instance's label beside
the bytes the read returned. A hand-built graph over a native 8-bit
YBR_FULL file therefore wrote RGB samples under `YBR_FULL`, and pydicom,
reading the export, converted them a second time. After `ingest()` the
label is already `RGB`, so none of the session's own ingested paths
showed it; a hand-built labelled `Instance(file_path=...)` and a direct
handler call did.

**The rule, #464's:** relabel exactly when the decode converts, never one
without the other. A bare instance carries no label, so nothing on it is
false and nothing is written.
"""
import glob
import os

import imagecodecs
import numpy as np
import pydicom
import pytest
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.encaps import encapsulate, generate_frames
from pydicom.pixels import convert_color_space, get_decoder
from pydicom.uid import generate_uid

from isocenter import entities, imagecodecs_handler
from isocenter.entities import Equipment, Instance, Patient, Series, Study
from isocenter.io_handlers import (_FALLBACK_DECODER_CONVERTS,
                                   _FALLBACK_PHOTOMETRICS)
from isocenter.services import RedactionService
from isocenter.session import DicomSession
from support.decode_doors import through_the_fallback

EXPLICIT_LE = "1.2.840.10008.1.2.1"
JPEG_BASELINE = "1.2.840.10008.1.2.4.50"
J2K_LOSSLESS = "1.2.840.10008.1.2.4.90"
J2K = "1.2.840.10008.1.2.4.91"
SOP_CLASS = "1.2.840.10008.5.1.4.1.1.7"

#: Every sample differs from its neighbours in the pixel, so a plane swap
#: or a partial conversion would not compare equal.
RGB8 = (np.arange(48, dtype=np.int64) * 5).astype(np.uint8).reshape(4, 4, 3)
#: The YBR_FULL samples a native file stores for `RGB8`, and pydicom's own
#: conversion of them back: what pydicom's door returns for that file, and
#: what ingest stores.
YBR8 = convert_color_space(RGB8, "RGB", "YBR_FULL")
YBR8_AS_RGB = convert_color_space(YBR8, "YBR_FULL", "RGB")
RGB16 = (np.arange(48, dtype=np.int64) * 1000 + 7).astype(
    np.uint16).reshape(4, 4, 3)


def _j2k(arr, photometric):
    """A J2K codestream with the colour transform `photometric` names.

    `YBR_RCT` is the reversible transform, written into a lossless
    stream under .90; `YBR_ICT` the irreversible one, written into a lossy
    stream under .91.
    """
    reversible = photometric == "YBR_RCT"
    kwargs = {"level": 0} if reversible else {}
    return imagecodecs.jpeg2k_encode(arr, codecformat="J2K", mct=True,
                                     reversible=reversible, **kwargs)


def _dataset(ts, *, photometric, bits=8, native=None, frames=None,
             study=None, series=None, number=1):
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = SOP_CLASS
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ts
    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientID, ds.PatientName = "PAT482", "DOE^JANE"
    ds.StudyInstanceUID = study or generate_uid()
    ds.SeriesInstanceUID = series or generate_uid()
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.SOPClassUID = SOP_CLASS
    ds.Modality, ds.SeriesNumber, ds.InstanceNumber = "OT", 1, number
    ds.StudyDate = "20230101"
    ds.Rows, ds.Columns = 4, 4
    ds.SamplesPerPixel, ds.PlanarConfiguration = 3, 0
    ds.PhotometricInterpretation = photometric
    ds.BitsAllocated = ds.BitsStored = bits
    ds.HighBit = bits - 1
    ds.PixelRepresentation = 0
    if native is not None:
        ds.PixelData = native.tobytes()
    else:
        ds.PixelData = encapsulate(frames, has_bot=True)
        ds["PixelData"].is_undefined_length = True
    return ds


def _j2k_dataset(photometric, arr, **kwargs):
    ts = J2K_LOSSLESS if photometric == "YBR_RCT" else J2K
    bits = arr.dtype.itemsize * 8
    return _dataset(ts, photometric=photometric, bits=bits,
                    frames=[_j2k(arr, photometric)], **kwargs)


def _write(tmp_path, ds, name="one"):
    src = tmp_path / f"src_{name}"
    os.makedirs(src, exist_ok=True)
    path = str(src / f"{ds.SOPInstanceUID}.dcm")
    ds.save_as(path, enforce_file_format=True)
    return path


def _instance(path, label=None):
    inst = Instance(generate_uid(), SOP_CLASS, 1, file_path=path)
    if label is not None:
        inst.attributes["0028,0004"] = label
    return inst


def _read_instance(path, label):
    """What the Instance door returns, the label after, the revision moved."""
    inst = _instance(path, label)
    before = inst._revision  # pylint: disable=protected-access
    try:
        got = inst.get_pixel_data()
    except Exception as exc:  # pylint: disable=broad-except
        got = exc
    return (got, inst.attributes.get("0028,0004"),
            inst._revision - before)  # pylint: disable=protected-access


def _same(arr, want):
    return (isinstance(arr, np.ndarray) and arr.dtype == want.dtype
            and arr.shape == want.shape and arr.tolist() == want.tolist())


def _pydicom_cannot_decode(path):
    with pytest.raises(RuntimeError):
        _ = pydicom.dcmread(path).pixel_array


# ---------------------------------------------------------------------------
# J1 -- the handler relabels the colour transform jpeg2k_decode undid
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("photometric", ["YBR_RCT", "YBR_ICT"])
@pytest.mark.parametrize("source", [RGB8, RGB16], ids=["8-bit", "16-bit"])
def test_the_handler_says_rgb_for_a_ybr_rct_or_ict_j2k_stream(
        tmp_path, photometric, source):
    """J1: RGB bytes, labelled RGB, at 8 and 16 bits.

    `jpeg2k_decode` returns the codestream's components after undoing its
    colour transform, so the array is the RGB the encoder was given
    (exactly under RCT, within the lossy stream's error under ICT). Before
    #482 the handler returned it with `ds` still saying `YBR_RCT` or
    `YBR_ICT`. The label now follows the decoder's answer, which is
    `_FALLBACK_PHOTOMETRICS`' answer for ingest.

    Asked of `_decode_pixels` with pydicom unable to decode since #453
    deleted the handler's `get_pixel_data` (Q10). It returns the label
    rather than writing it onto `ds`.
    """
    ds = _j2k_dataset(photometric, source)
    codestream = next(generate_frames(ds.PixelData, number_of_frames=1))
    want = imagecodecs.jpeg2k_decode(codestream)
    assert np.abs(want.astype(int) - source.astype(int)).max() <= 1
    arr, label = through_the_fallback(ds)
    assert _same(arr, want), arr
    assert label == "RGB"


@pytest.mark.parametrize("photometric", ["YBR_RCT", "YBR_ICT"])
def test_a_labelled_instance_over_16_bit_ybr_rct_or_ict_j2k_reads_as_rgb(
        tmp_path, photometric):
    """J2: the Instance door follows the handler's relabel, at 16 bits.

    Pillow refuses 16-bit multi-sample, so this file reaches the
    handler through `Instance.get_pixel_data()`'s fallback (asserted
    first). The door already relabels the instance whenever the handler
    changed `ds`'s label (#464), so the handler's relabel is the whole
    fix at this door. The bare instance gains no label.
    """
    path = _write(tmp_path, _j2k_dataset(photometric, RGB16))
    _pydicom_cannot_decode(path)
    arr, label, moved = _read_instance(path, photometric)
    assert isinstance(arr, np.ndarray) and arr.dtype == np.uint16, arr
    assert np.abs(arr.astype(int) - RGB16.astype(int)).max() <= 1
    assert (label, moved) == ("RGB", 1)
    arr, label, moved = _read_instance(path, None)
    assert isinstance(arr, np.ndarray), arr
    assert (label, moved) == (None, 0)


def test_a_j2k_decode_that_fails_changes_no_label(tmp_path):
    """J3: the relabel follows a decode that happened, never precedes it.

    The stream is cut two bytes short, so OpenJPEG reads the header and
    fails the decode (measured: `opj_decode or opj_end_decompress
    failed`). A door that relabelled before decoding would leave `RGB`
    on an instance nothing was decoded from, which is the other half of
    #372's defect: a label without its conversion. (`_decode_pixels`
    never writes to its dataset, so the handler's dataset-label half of
    this test went with the handler's `get_pixel_data`, #453.)
    """
    ds = _j2k_dataset("YBR_RCT", RGB16)
    whole = _j2k(RGB16, "YBR_RCT")
    ds.PixelData = encapsulate([whole[:-2]], has_bot=True)
    ds["PixelData"].is_undefined_length = True
    path = _write(tmp_path, ds)
    with pytest.raises(RuntimeError,
                       match="imagecodecs could not decode it either"):
        through_the_fallback(pydicom.dcmread(path))
    exc, label, moved = _read_instance(path, "YBR_RCT")
    assert isinstance(exc, RuntimeError), exc
    assert (label, moved) == ("YBR_RCT", 0)


def test_the_handler_relabels_exactly_the_rows_ingest_relabels_without_converting():
    """J4: one table of label-only relabels, held to ingest's.

    `_FALLBACK_PHOTOMETRICS` says which declared label ingest stores
    under which, and `_FALLBACK_DECODER_CONVERTS` names the syntaxes
    whose decoder has already converted, so a relabel there changes the
    label only. The handler's `DECODER_RELABELS` must hold exactly those
    rows. A row in one table alone is a label one door gives and the
    other does not, which is this issue again. (#464's R5 holds the
    conversion rows the same way.)
    """
    from_table = {ts: {declared: stored
                       for declared, stored in labels.items()
                       if declared != stored}
                  for ts, labels in _FALLBACK_PHOTOMETRICS.items()
                  if ts in _FALLBACK_DECODER_CONVERTS}
    assert from_table == {
        ts: dict(rows)
        for ts, rows in imagecodecs_handler.DECODER_RELABELS.items()}
    assert not set(imagecodecs_handler.DECODER_RELABELS) & set(
        imagecodecs_handler.CONVERTS_TO)


# ---------------------------------------------------------------------------
# P1 -- the pydicom arm relabels from the decoder's own answer
# ---------------------------------------------------------------------------

def _pydicom_sources():
    yield "native YBR_FULL", _dataset(
        EXPLICIT_LE, photometric="YBR_FULL", native=YBR8)
    yield "JPEG Baseline YBR_FULL_422", _dataset(
        JPEG_BASELINE, photometric="YBR_FULL_422",
        frames=[imagecodecs.jpeg8_encode(RGB8, level=100,
                                         subsampling="422")])
    yield "J2K 8-bit YBR_RCT", _j2k_dataset("YBR_RCT", RGB8)
    yield "J2K 8-bit YBR_ICT", _j2k_dataset("YBR_ICT", RGB8)


@pytest.mark.parametrize("name,ds", list(_pydicom_sources()),
                         ids=[n for n, _ in _pydicom_sources()])
def test_a_labelled_instance_pydicom_decodes_to_rgb_is_relabelled_rgb(
        tmp_path, name, ds):
    """P1: the pydicom arm's RGB bytes come under an RGB label.

    pydicom decodes all four here and returns RGB (its default
    `as_rgb=True`), saying so in the decoder's meta while the dataset
    keeps its YBR label (asserted first: the premise, measured). Before
    #482 the labelled instance kept the YBR label. Its bytes do not
    change; only the label, and the revision with it. The bare instance
    gets no label.
    """
    path = _write(tmp_path, ds)
    probe = pydicom.dcmread(path)
    want, meta = get_decoder(probe.file_meta.TransferSyntaxUID).as_array(
        probe)
    assert meta["photometric_interpretation"] == "RGB", name
    assert str(probe.PhotometricInterpretation) != "RGB", name
    if name == "native YBR_FULL":
        assert _same(want, YBR8_AS_RGB)
    arr, label, moved = _read_instance(path, str(ds.PhotometricInterpretation))
    assert _same(arr, want), arr
    assert (label, moved) == ("RGB", 1)
    arr, label, moved = _read_instance(path, None)
    assert _same(arr, want), arr
    assert (label, moved) == (None, 0)


@pytest.mark.parametrize("stored,label", [
    ("RGB", "YBR_FULL"),
    ("MONOCHROME2", "MONOCHROME1"),
], ids=["RGB file under a YBR_FULL instance",
        "MONOCHROME2 file under a MONOCHROME1 instance"])
def test_the_pydicom_arm_relabels_from_the_decode_not_from_the_file(
        tmp_path, stored, label):
    """P2: a hand-built label the decode did not change is left alone.

    pydicom converts nothing here, so its meta repeats the file's own
    label. The door has made no statement about colour, so the instance's
    label (wrong, but not this read's to correct) and its revision stay
    put. A door that compared the decoder's label with the instance's,
    rather than with the file's, would write the file's label here.
    """
    if stored == "RGB":
        ds = _dataset(EXPLICIT_LE, photometric="RGB", native=RGB8)
    else:
        ds = _dataset(EXPLICIT_LE, photometric="RGB", native=RGB8)
        ds.SamplesPerPixel = 1
        del ds.PlanarConfiguration
        ds.PhotometricInterpretation = "MONOCHROME2"
        ds.Rows, ds.Columns = 4, 12
    path = _write(tmp_path, ds)
    arr, got_label, moved = _read_instance(path, label)
    assert isinstance(arr, np.ndarray), arr
    assert (got_label, moved) == (label, 0)


def test_a_set_landing_during_the_pydicom_read_keeps_its_own_label(
        tmp_path, monkeypatch):
    """P3: the pydicom arm's relabel goes through the pixel-state lock too.

    The same interleaving as #464's R6, at the other arm: a
    `set_pixel_data()` of a grey frame lands inside the decode, and
    corrects the label to MONOCHROME2 and SamplesPerPixel to 1. A relabel
    made outside `Instance._relabel_to_decoded_colour` -- straight
    through `set_attr` -- would then write RGB beside a SamplesPerPixel of
    1, a label no array can carry. And since #465 the set keeps its
    pixels too: the read publishes only into an empty slot, and returns
    the set's array here.
    """
    path = _write(tmp_path, _dataset(
        EXPLICIT_LE, photometric="YBR_FULL", native=YBR8))
    inst = _instance(path, "YBR_FULL")
    # The decode the door makes is `io_handlers._decode_pixels`' since
    # #453, so its `get_decoder` is the one wrapped. `fired` says the set
    # really landed inside it: a wrap in a module the door no longer
    # decodes through would leave every assertion below about a read
    # with no set in it.
    from isocenter import io_handlers
    real = io_handlers.get_decoder
    fired = []
    grey = np.zeros((4, 4), dtype=np.uint8)

    class SetDuringRead:
        def __init__(self, decoder):
            self._decoder = decoder

        def as_array(self, *args, **kwargs):
            got = self._decoder.as_array(*args, **kwargs)
            fired.append(1)
            inst.set_pixel_data(grey)
            return got

    monkeypatch.setattr(io_handlers, "get_decoder",
                        lambda ts: SetDuringRead(real(ts)))
    got = inst.get_pixel_data()
    assert fired == [1], "the set never ran inside the decode"
    assert inst.attributes["0028,0004"] == "MONOCHROME2"
    assert int(inst.attributes["0028,0002"]) == 1
    assert got is inst.pixel_array
    assert got.shape == grey.shape and np.array_equal(got, grey)
    assert inst._pixel_array_unwritten  # pylint: disable=protected-access


# ---------------------------------------------------------------------------
# E1 -- the exported file: its label matches its bytes
# ---------------------------------------------------------------------------

def _hand_built(session, sources, serial=None):
    """One patient, study and series holding an instance per source file,
    labelled and described as each file is, as a hand-built graph would be.

    `serial` puts the series on a scanner with that serial number, which
    is what a redaction rule matches on. Returns the instances.
    """
    first = sources[0][1]
    patient = Patient("PAT482", "DOE^JANE")
    study = Study(first.StudyInstanceUID, "20230101")
    series = Series(first.SeriesInstanceUID, "OT", 1)
    if serial is not None:
        series.equipment = Equipment("Acme", "Model", serial)
    for path, ds in sources:
        inst = Instance(ds.SOPInstanceUID, SOP_CLASS, int(ds.InstanceNumber),
                        file_path=path)
        for tag, value in (
                ("0028,0004", str(ds.PhotometricInterpretation)),
                ("0028,0002", 3), ("0028,0006", 0),
                ("0028,0010", ds.Rows), ("0028,0011", ds.Columns),
                ("0028,0100", ds.BitsAllocated),
                ("0028,0101", ds.BitsStored), ("0028,0102", ds.HighBit),
                ("0028,0103", 0), ("0008,0020", "20230101")):
            inst.attributes[tag] = value
        series.instances.append(inst)
    study.series.append(series)
    patient.studies.append(study)
    session.store.patients.append(patient)
    return list(series.instances)


def _written_label(use_compression):
    """The label an RGB 3-sample instance's exported file carries.

    `RGB` natively. Under compression `YBR_RCT` since #490: an RGB source
    is encoded with the multiple-component transform, and PS3.5 8.2.4
    gives that codestream `YBR_RCT` under a reversible encode. What this
    module is about -- the label telling the truth about the bytes beside
    it -- is unchanged either way; only which truth it tells changes with
    the encode. Before #482 the same file said `YBR_FULL` or `YBR_RCT`
    over untransformed RGB samples, which was true of neither.
    """
    return "YBR_RCT" if use_compression else "RGB"


def _exported(folder):
    """`{sop_uid: (label, stored samples, dataset)}` for every file written.

    The stored samples are read without any colour conversion: pydicom's
    `as_rgb=False` for a native file, the codestream through
    `jpeg2k_decode` for a compressed one (which undoes only the
    codestream's own transform, as every J2K decoder does).
    """
    out = {}
    for path in glob.glob(os.path.join(folder, "**", "*.dcm"),
                          recursive=True):
        ds = pydicom.dcmread(path)
        ts = ds.file_meta.TransferSyntaxUID
        if ts.is_encapsulated:
            stored = imagecodecs.jpeg2k_decode(
                next(generate_frames(ds.PixelData, number_of_frames=1)))
        else:
            stored = get_decoder(ts).as_array(ds, as_rgb=False)[0]
        out[str(ds.SOPInstanceUID)] = (
            str(ds.PhotometricInterpretation), stored, ds)
    return out


@pytest.mark.parametrize("use_compression", [True, False],
                         ids=["compressed", "native"])
def test_a_hand_built_export_writes_rgb_bytes_under_an_rgb_label(
        tmp_path, use_compression):
    """E1: the exported file's label matches its bytes, and pydicom reads
    the source colours back without converting them a second time.

    The graph is hand-built over three files -- a native 8-bit YBR_FULL
    file, which reaches the pydicom arm, and 16-bit J2K YBR_RCT and
    YBR_ICT files, which reach the handler -- and labelled as the files
    are. Export writes the label the instance holds after the read, so the
    read doors' labels are the exported file's.

    Before #482: the YBR_FULL file was written as RGB samples under
    `YBR_FULL`, compressed or not, and pydicom converted them again
    (measured, max difference 255 from the source). The J2K files were
    written under `YBR_RCT`/`YBR_ICT`, which a native file cannot carry at
    all; pydicom reads the native one's samples as they are, so there the
    label is the only wrong thing, and it is asserted.

    Each file is also the file the *ingested* graph exports for the same
    source -- same label, same stored samples -- so a hand-built graph and
    an ingested one give one answer for one file.

    pydicom's pixel read of the compressed 16-bit files is not asked:
    Pillow, its only J2K plugin here, refuses 16-bit multi-sample.
    """
    study, series = generate_uid(), generate_uid()
    datasets = [
        ("YBR_FULL", _dataset(EXPLICIT_LE, photometric="YBR_FULL",
                              native=YBR8, study=study, series=series,
                              number=1)),
        ("YBR_RCT", _j2k_dataset("YBR_RCT", RGB16, study=study,
                                 series=series, number=2)),
        ("YBR_ICT", _j2k_dataset("YBR_ICT", RGB16, study=study,
                                 series=series, number=3)),
    ]
    sources = [(_write(tmp_path, ds), ds) for _, ds in datasets]
    colours = {}
    for (name, ds), (path, _) in zip(datasets, sources):
        if name == "YBR_FULL":
            colours[str(ds.SOPInstanceUID)] = YBR8_AS_RGB
        else:
            colours[str(ds.SOPInstanceUID)] = through_the_fallback(
                pydicom.dcmread(path))[0]

    hand = DicomSession(persistence_file=str(tmp_path / "hand.db"))
    ingested = DicomSession(persistence_file=str(tmp_path / "ingested.db"))
    try:
        _hand_built(hand, sources)
        hand_summary = hand.export(str(tmp_path / "hand"),
                                   use_compression=use_compression)
        assert (hand_summary.written, hand_summary.failures) == (3, [])
        summary = ingested.ingest(str(tmp_path / "src_one"))
        assert (summary.ingested, summary.failures) == (3, [])
        ingested.export(str(tmp_path / "ingested"),
                        use_compression=use_compression)
    finally:
        hand.close()
        ingested.close()

    from_hand = _exported(str(tmp_path / "hand"))
    from_ingest = _exported(str(tmp_path / "ingested"))
    assert set(from_hand) == set(from_ingest) == set(colours)
    for uid, want in colours.items():
        label, stored, ds = from_hand[uid]
        assert label == _written_label(use_compression), (uid, label)
        assert _same(stored, want), (uid, stored)
        assert from_ingest[uid][0] == label
        assert _same(from_ingest[uid][1], stored)
        if want.dtype == np.uint8 or not use_compression:
            # pydicom's own reading of the exported file: the source
            # colours, converted once, not twice.
            assert _same(ds.pixel_array, want), (uid, ds.pixel_array)


# ---------------------------------------------------------------------------
# R1 -- redact() then export(): the label comes back with the redacted frame
# ---------------------------------------------------------------------------

SERIAL = "SN482"
#: Rows 0-1, columns 0-1: a corner, so the rest of each frame is left to
#: compare with its source.
ZONE = (0, 2, 0, 2)


@pytest.fixture(params=["processes", "threads", "serial"])
def redaction_arm(request, monkeypatch):
    """Each place a redaction reads and replaces a frame from.

    `processes` and `threads` are `Session.redact()`'s pool, forced either
    way on either interpreter: `parallel._resolve_execution_choice` reads
    both variables at call time. `serial` is
    `RedactionService.redact_machine_instances`, which runs in the
    caller's process on the live instance, so the variables do not reach
    it.
    """
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)
    if request.param == "processes":
        monkeypatch.delenv("ISOCENTER_FORCE_THREADS", raising=False)
        monkeypatch.setenv("ISOCENTER_FORCE_PROCESSES", "1")
    else:
        monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
        monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    return request.param


@pytest.mark.parametrize("use_compression", [False, True],
                         ids=["native", "compressed"])
def test_a_redacted_hand_built_graph_exports_rgb_bytes_under_an_rgb_label(
        tmp_path, redaction_arm, use_compression):
    """R1: `redact()` then `export()` writes the label of the frame it wrote.

    The pipeline order, over a hand-built graph: a native 8-bit YBR_FULL
    file (the pydicom arm) and a 16-bit J2K YBR_RCT file (the handler),
    on one scanner, with a one-zone rule for it.

    The redaction reads each file, and that read relabels the instance
    that did the reading. Under threads, and on the serial path, that
    instance is the caller's. Under processes it is the worker's copy,
    and before this the result the worker sent back carried the flags,
    the hash and the new identity but not the label. So the caller's
    instance kept `YBR_FULL`/`YBR_RCT` over the worker's RGB frame, with
    no file left to read again, and export wrote that label beside RGB
    samples. #493's review measured it on 168fdd6, 3.12.14: the native
    YBR_FULL export read back through pydicom up to 116 away from the
    source, and `verify_readback` said nothing. Red on this branch before
    the fix: the two `processes` cases, on 3.12.14 and 3.14.7t, at the
    first assertion below. The `threads` and `serial` cases were green,
    and stay here to hold the arms that were already right.

    Asserted in order: the caller's graph says RGB after `redact()`,
    which is the seam itself; the file says RGB; its stored samples are
    the source's RGB outside the zone and zero inside it; and pydicom,
    reading the file as any consumer would, gives the source colours
    back, converted once. pydicom is not asked to read the compressed
    16-bit file, for the reason E1 gives.
    """
    study, series = generate_uid(), generate_uid()
    datasets = [
        _dataset(EXPLICIT_LE, photometric="YBR_FULL", native=YBR8,
                 study=study, series=series, number=1),
        _j2k_dataset("YBR_RCT", RGB16, study=study, series=series, number=2),
    ]
    sources = [(_write(tmp_path, ds), ds) for ds in datasets]
    colours = {1: YBR8_AS_RGB,
               2: through_the_fallback(
                   pydicom.dcmread(sources[1][0]))[0]}

    session = DicomSession(persistence_file=str(tmp_path / "redact.db"))
    try:
        instances = _hand_built(session, sources, serial=SERIAL)
        if redaction_arm == "serial":
            RedactionService(
                session.store, session.store_backend).redact_machine_instances(
                    SERIAL, [ZONE], targets=instances, show_progress=False)
        else:
            session.configuration.rules = [
                {"serial_number": SERIAL, "redaction_zones": [list(ZONE)]}]
            session.redact(show_progress=False)
        # Redacted, so each took a new identity and left its file behind:
        # there is nothing to read again that could correct the label.
        assert [inst.file_path for inst in instances] == [None, None]
        assert [inst.attributes["0028,0004"] for inst in instances] == [
            "RGB", "RGB"]
        summary = session.export(str(tmp_path / "out"),
                                 use_compression=use_compression)
        assert (summary.written, summary.failures) == (2, [])
    finally:
        session.close()

    outside = np.ones((4, 4), dtype=bool)
    outside[ZONE[0]:ZONE[1], ZONE[2]:ZONE[3]] = False
    written = _exported(str(tmp_path / "out"))
    assert sorted(int(ds.InstanceNumber) for _, _, ds in written.values()) == [
        1, 2]
    for label, stored, ds in written.values():
        want = colours[int(ds.InstanceNumber)]
        assert label == _written_label(use_compression), (
            int(ds.InstanceNumber), label)
        assert (stored.dtype, stored.shape) == (want.dtype, want.shape)
        assert stored[outside].tolist() == want[outside].tolist()
        assert not stored[~outside].any()
        if want.dtype == np.uint8 or not use_compression:
            back = ds.pixel_array
            assert back[outside].tolist() == want[outside].tolist(), (
                int(ds.InstanceNumber), back)


# ---------------------------------------------------------------------------
# J5 -- the handler relabels only what the codec converted
# ---------------------------------------------------------------------------

MONO8 = (np.arange(16, dtype=np.int64) * 13 + 3).astype(np.uint8).reshape(4, 4)
MONO16 = (np.arange(16, dtype=np.int64) * 1000 + 7).astype(
    np.uint16).reshape(4, 4)


@pytest.mark.parametrize("ts", [J2K_LOSSLESS, J2K], ids=[".90", ".91"])
@pytest.mark.parametrize("photometric,source", [
    ("MONOCHROME2", MONO8), ("MONOCHROME2", MONO16),
    ("RGB", RGB8), ("RGB", RGB16),
], ids=["MONOCHROME2 8-bit", "MONOCHROME2 16-bit", "RGB 8-bit", "RGB 16-bit"])
def test_a_j2k_stream_with_no_colour_transform_keeps_its_label(
        tmp_path, ts, photometric, source):
    """J5, P2's twin at the handler: no transform undone, no label written.

    `DECODER_RELABELS` names the labels whose transform the codec undoes.
    A J2K stream under any other label -- MONOCHROME2, or RGB written
    without the multiple component transform -- decodes to exactly what
    was encoded, so the handler has converted nothing and says nothing:
    `ds` keeps its label. A lookup that defaulted to RGB for every J2K
    syntax would relabel the MONOCHROME2 stream, a label no one-sample
    array can carry.

    The instance half: a labelled instance over each file keeps its label
    and its revision. **It does not reach the handler for MONOCHROME2**:
    pydicom's Pillow plugin decodes that at both depths, and only the
    16-bit RGB file falls through to the handler (measured). So the
    fallback calls on MONOCHROME2 are the ones that pin the lookup; the
    instance half pins the door around it. The fallback is
    `_decode_pixels` with pydicom unable to decode since #453 deleted the
    handler's `get_pixel_data` (Q10), and its label is the one returned.
    """
    frame = imagecodecs.jpeg2k_encode(source, level=0, codecformat="J2K",
                                      mct=False, reversible=True)
    ds = _dataset(ts, photometric=photometric,
                  bits=source.dtype.itemsize * 8, frames=[frame])
    if source.ndim == 2:
        ds.SamplesPerPixel = 1
        del ds.PlanarConfiguration
    path = _write(tmp_path, ds)
    arr, decoded_label = through_the_fallback(pydicom.dcmread(path))
    assert _same(arr, source), arr
    assert decoded_label == photometric
    arr, label, moved = _read_instance(path, photometric)
    assert _same(arr, source), arr
    assert (label, moved) == (photometric, 0)
