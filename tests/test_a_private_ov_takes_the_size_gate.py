"""A private `OV` value takes the binary size gate, as every other binary VR does (#735).

`populate_attrs` routes the wire VRs whose values are bulk bytes through
one size rule: kept at or below `BINARY_RETENTION_MAX_BYTES` (65534), dropped
with a `DATA_LOSS` row above it, so the outcome does not depend on the
source's transfer syntax (#151). `OV` was missing from that set. An Explicit
VR private `OV` of 65536 bytes was kept on the graph and exported, while its
Implicit VR twin -- the same bytes read as `UN` -- took the `UN` gate and was
dropped, and so was an `OD` of the same size beside it.
"""
from pathlib import Path

import numpy as np
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, ImplicitVRLittleEndian

from isocenter.session import DicomSession

SC = "1.2.840.10008.5.1.4.1.1.7"
SOP = "1.2.826.0.1.735.1"
OV_TAG, OD_TAG, SMALL_OV_TAG = 0x00091001, 0x00091002, 0x00091003


def _source(folder: Path, syntax, ov_bytes: int, extra=None) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = SC
    meta.MediaStorageSOPInstanceUID = SOP
    meta.TransferSyntaxUID = syntax
    ds = Dataset()
    ds.file_meta = meta
    ds.SOPClassUID, ds.SOPInstanceUID = SC, SOP
    ds.PatientID, ds.PatientName = "P735", "Doe^John"
    ds.StudyInstanceUID = "1.2.826.0.1.735.2"
    ds.SeriesInstanceUID = "1.2.826.0.1.735.3"
    ds.Modality, ds.StudyDate = "OT", "20200101"
    ds.add_new(0x00090010, "LO", "ACME")
    pattern = bytes(range(256))
    ds.add_new(OV_TAG, "OV", (pattern * (ov_bytes // 256 + 1))[:ov_bytes])
    ds.add_new(OD_TAG, "OD", pattern * 256)  # 65536 B: the control
    ds.add_new(SMALL_OV_TAG, "OV", bytes(range(32)))
    ds.Rows = ds.Columns = 4
    ds.BitsAllocated = ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelData = np.arange(16, dtype=np.uint8).tobytes()
    if extra is not None:
        extra(ds)
    ds.save_as(str(folder / "src.dcm"), enforce_file_format=True,
               little_endian=True,
               implicit_vr=syntax == ImplicitVRLittleEndian)


def _ingest(tmp_path, syntax, ov_bytes, extra=None):
    _source(tmp_path / "src", syntax, ov_bytes, extra)
    with DicomSession(str(tmp_path / "s.db")) as s:
        s.ingest(str(tmp_path / "src"))
        (p,) = s.store.patients
        inst = p.studies[0].series[0].instances[0]
        attrs = dict(inst.attributes)
        vrs = dict(inst.attribute_vrs)
        losses = s.store_backend.get_audit_losses()
    return attrs, vrs, losses


def _rows_for(losses, tag_text):
    return [row for row in losses if tag_text in row[2]]


def test_a_private_ov_over_the_threshold_is_dropped_as_od_is(tmp_path):
    attrs, _, losses = _ingest(tmp_path, ExplicitVRLittleEndian, 65536)

    assert "0009,1001" not in attrs
    ov_rows = _rows_for(losses, "0009,1001 (OV)")
    assert len(ov_rows) == 1, losses
    assert ov_rows[0][3] == "PRIVATE"
    # The control: the OD beside it was always dropped.
    assert len(_rows_for(losses, "0009,1002 (OD)")) == 1, losses
    # And a small OV is untouched.
    assert attrs["0009,1003"] == bytes(range(32))


def test_an_ov_at_the_threshold_is_kept(tmp_path):
    # 65528 is the largest whole number of 8-byte words <= 65534.
    attrs, vrs, losses = _ingest(tmp_path, ExplicitVRLittleEndian, 65528)

    assert isinstance(attrs["0009,1001"], bytes)
    assert len(attrs["0009,1001"]) == 65528
    assert vrs["0009,1001"] == "OV"
    assert _rows_for(losses, "0009,1001") == []


def test_the_ov_and_its_implicit_twin_agree(tmp_path):
    explicit = _ingest(tmp_path / "e", ExplicitVRLittleEndian, 65536)
    implicit = _ingest(tmp_path / "i", ImplicitVRLittleEndian, 65536)

    for attrs, _, losses in (explicit, implicit):
        assert "0009,1001" not in attrs
        assert "0009,1002" not in attrs
        assert len(_rows_for(losses, "0009,1001")) == 1, losses


SELECTOR_OV = 0x00720081  # Selector OV Value, the dictionary's one OV tag


def test_the_standard_ov_selector_value_takes_the_same_gate(tmp_path):
    """Selector OV Value (0072,0081) is OV in the dictionary, so #735's
    change reaches it too: over the threshold it is dropped with a
    STANDARD row, as an OD or OW of that size is (review of #900, F5)."""
    def big(ds):
        ds.add_new(SELECTOR_OV, "OV", bytes(range(256)) * 256)  # 65536 B

    attrs, _, losses = _ingest(tmp_path, ExplicitVRLittleEndian, 8, big)
    assert "0072,0081" not in attrs
    rows = _rows_for(losses, "0072,0081 (OV)")
    assert len(rows) == 1, losses
    assert rows[0][3] == "STANDARD"


def test_a_small_standard_ov_selector_value_is_kept(tmp_path):
    def small(ds):
        ds.add_new(SELECTOR_OV, "OV", bytes(range(16)))

    attrs, _, losses = _ingest(tmp_path, ExplicitVRLittleEndian, 8, small)
    assert attrs["0072,0081"] == bytes(range(16))
    assert _rows_for(losses, "0072,0081") == []
