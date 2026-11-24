"""A compressed export labels the reversible transform `YBR_RCT`, a `YBR_ICT` source too (#1080).

`_compress_j2k` encodes every 3-sample `RGB`, `YBR_RCT` and `YBR_ICT`
frame with the multiple-component transform at `level=0`, which is the
**reversible** transform. PS3.5 8.2.4 names that codestream `YBR_RCT`;
`YBR_ICT` names the irreversible one. An `RGB` source was relabelled
`YBR_RCT`, and a `YBR_ICT` source kept `YBR_ICT` over a reversibly
transformed codestream, with no row and a PASS: measured on 3e4ee91a,
3.12.14 and 3.14.7t, for a native three-sample source labelled `YBR_ICT`
and for an instance given that label with `set_attr`.

Now every frame the encoder transforms is labelled `YBR_RCT`, with an
INFO note where the declared label was `YBR_ICT` (owner ruling
2026-11-22, Q8 and Q9; no `CONFIG_VERSION` move, Q10). `YBR_ICT` leaves
the JPEG 2000 Lossless row of `_ADMISSIBLE_PHOTOMETRICS`, so the readback
refuses a `.90` file labelled `YBR_ICT`, which this exporter no longer
writes. Uncompressed, `YBR_ICT` keeps its WARNING, with a remedy that
says what compressing does to the label.

The transform is read from each codestream's `COD` segment, so no case is
satisfied by the label alone.

Names `isocenter.io_handlers`.
"""
import logging

import numpy as np
import pydicom
import pytest
from pydicom.encaps import encapsulate, generate_frames
from pydicom.uid import JPEG2000Lossless

from isocenter.io_handlers import (_compress_j2k, _readback_label_mismatch)
from test_export_photometric_admissibility import _export, _image
from test_j2k_mct_label_on_export import _cod_transform

NOTE = ("PhotometricInterpretation 'YBR_ICT' was written 'YBR_RCT': the "
        "JPEG 2000 codestream this export writes carries the reversible "
        "transform, which YBR_RCT names.")


def _codestream(path):
    ds = pydicom.dcmread(path)
    return ds, bytes(next(generate_frames(ds.PixelData, number_of_frames=1)))


def test_a_ybr_ict_instance_is_written_ybr_rct_with_a_note(tmp_path):
    """Case C: an instance given `YBR_ICT` with `set_attr`."""
    outcome = _export(tmp_path, _image("YBR_ICT"), compression="j2k")

    assert outcome.ok, outcome.error
    ds, stream = _codestream(outcome.output_path)
    assert ds.file_meta.TransferSyntaxUID == JPEG2000Lossless
    assert ds.PhotometricInterpretation == "YBR_RCT"
    assert _cod_transform(stream) == 1
    assert outcome.warnings == [], outcome.warnings
    assert NOTE in outcome.corrections


def test_a_ybr_rct_or_rgb_source_gets_no_note(tmp_path):
    for label in ("YBR_RCT", "RGB"):
        outcome = _export(tmp_path, _image(label), compression="j2k")
        assert outcome.ok, outcome.error
        ds, stream = _codestream(outcome.output_path)
        assert ds.PhotometricInterpretation == "YBR_RCT"
        assert _cod_transform(stream) == 1
        assert not [c for c in outcome.corrections if "YBR_ICT" in c]


def test_a_native_ybr_ict_source_through_a_session(tmp_path, caplog):
    """Case B: a three-sample native file labelled `YBR_ICT`, default export."""
    from pydicom.dataset import FileDataset, FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian, generate_uid

    from isocenter.session import DicomSession
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.7"
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    src = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    src.PatientID, src.PatientName = "P1080", "DOE^JANE"
    src.StudyInstanceUID, src.SeriesInstanceUID = generate_uid(), generate_uid()
    src.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    src.SOPClassUID = meta.MediaStorageSOPClassUID
    src.Modality, src.SeriesNumber, src.InstanceNumber = "OT", 1, 1
    src.StudyDate = "20230101"
    src.Rows = src.Columns = 8
    src.SamplesPerPixel, src.PlanarConfiguration = 3, 0
    src.PhotometricInterpretation = "YBR_ICT"
    src.BitsAllocated = src.BitsStored = 8
    src.HighBit, src.PixelRepresentation = 7, 0
    src.PixelData = np.full((8, 8, 3), (100, 123, 214), np.uint8).tobytes()
    (tmp_path / "in").mkdir()
    src.save_as(str(tmp_path / "in" / "one.dcm"), enforce_file_format=False)

    with DicomSession(persistence_file=str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        session.anonymize(session.audit())
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="isocenter"):
            summary = session.export(str(tmp_path / "out"),
                                     show_progress=False)
        assert summary.written == 1, summary.failures
    (written,) = list((tmp_path / "out").rglob("*.dcm"))
    ds, stream = _codestream(str(written))
    assert ds.PhotometricInterpretation == "YBR_RCT"
    assert _cod_transform(stream) == 1
    assert any(NOTE in r.getMessage() for r in caplog.records
               if r.levelno == logging.INFO)


def test_the_encoder_relabels_ybr_ict(tmp_path):
    ds = pydicom.Dataset()
    ds.file_meta = pydicom.dataset.FileMetaDataset()
    ds.Rows = ds.Columns = 8
    ds.SamplesPerPixel = 3
    ds.PhotometricInterpretation = "YBR_ICT"
    ds.BitsAllocated, ds.PixelRepresentation = 8, 0
    _compress_j2k(ds, np.full((8, 8, 3), 77, np.uint8))
    assert ds.PhotometricInterpretation == "YBR_RCT"


def test_one_sample_ybr_ict_is_not_transformed_or_relabelled():
    """No transform without three components, so no `YBR_RCT` either."""
    ds = pydicom.Dataset()
    ds.file_meta = pydicom.dataset.FileMetaDataset()
    ds.Rows = ds.Columns = 8
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "YBR_ICT"
    ds.BitsAllocated, ds.PixelRepresentation = 8, 0
    _compress_j2k(ds, np.full((8, 8), 77, np.uint8))
    assert ds.PhotometricInterpretation == "YBR_ICT"


def test_the_readback_refuses_a_jpeg_2000_lossless_file_labelled_ybr_ict():
    """Red while the `.90` row admits `YBR_ICT`."""
    ds = pydicom.Dataset()
    ds.file_meta = pydicom.dataset.FileMetaDataset()
    ds.file_meta.TransferSyntaxUID = JPEG2000Lossless
    ds.PhotometricInterpretation = "YBR_ICT"
    ds.PixelData = encapsulate([b"\xff\x4f\xff\x51"])
    reason = _readback_label_mismatch(ds)
    assert reason is not None and "YBR_ICT" in reason
    ds.PhotometricInterpretation = "YBR_RCT"
    assert _readback_label_mismatch(ds) is None


def test_uncompressed_ybr_ict_warns_with_the_compression_remedy(tmp_path):
    """Case D: the remedy says what compressing does to the label."""
    outcome = _export(tmp_path, _image("YBR_ICT"), compression=None)
    assert outcome.ok, outcome.error
    (warning,) = outcome.warnings
    assert ("Export with use_compression=True, where the reversible "
            "transform is applied and the file is labelled YBR_RCT, or "
            "declare the label these bytes have with "
            "set_attr(\"0028,0004\", ...).") in warning, warning


def test_uncompressed_ybr_rct_keeps_its_remedy(tmp_path):
    outcome = _export(tmp_path, _image("YBR_RCT"), compression=None)
    (warning,) = outcome.warnings
    assert ("Export with use_compression=True, where the transform is "
            "applied and the label is true of the codestream") in warning


@pytest.mark.parametrize("label", ["YBR_ICT", "YBR_RCT", "RGB"])
def test_no_warning_row_on_a_compressed_transform_export(tmp_path, label):
    """Red if the build half judges `YBR_ICT` against the JPEG 2000 row
    as declared, rather than as the encoder will write it."""
    outcome = _export(tmp_path, _image(label), compression="j2k")
    assert outcome.ok, outcome.error
    assert outcome.warnings == [], outcome.warnings
