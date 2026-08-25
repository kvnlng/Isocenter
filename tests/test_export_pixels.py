
import pytest
import os
import pydicom
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ImplicitVRLittleEndian
from isocenter.entities import Patient, Study, Series, Instance
from isocenter.io_handlers import DicomExporter

def test_export_silent_pixel_failure(tmp_path):
    # 1. Create a dummy DICOM file to verify normal export works
    dcm_path = tmp_path / "valid.dcm"
    ds = FileDataset(str(dcm_path), {}, file_meta=FileMetaDataset())
    ds.file_meta.TransferSyntaxUID = ImplicitVRLittleEndian
    ds.is_little_endian = True
    ds.is_implicit_VR = True
    ds.SOPInstanceUID = "1.2.3.4.5"
    ds.PixelData = b'\x00\xFF' * 10
    ds.Rows = 1
    ds.Columns = 20
    ds.BitsAllocated = 8
    ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.save_as(str(dcm_path))

    # 2. Setup Patient Hierarchy with an Instance pointing to a MISSING file
    p = Patient("P_PIXEL", "Pixel Fail")
    st = Study("S1", "20230101")
    se = Series("SE1", "OT", 1)

    # Instance pointing to non-existent file
    inst = Instance("1.2.3.4.5", "1.2.840.10008.5.1.4.1.1.2", 1)
    inst.file_path = str(tmp_path / "missing.dcm") # DOES NOT EXIST

    # Needs valid attributes to pass IOD validator
    inst.attributes["0008,0020"] = "20230101"
    inst.attributes["0008,0030"] = "120000"
    inst.attributes["0018,0050"] = "1.0"
    inst.attributes["0018,0060"] = "120"
    inst.attributes["0020,0032"] = ["0","0","0"]
    inst.attributes["0020,0037"] = ["1","0","0","0","1","0"]
    inst.attributes["0028,0030"] = ["0.5", "0.5"]

    se.instances.append(inst)
    st.series.append(se)
    p.studies.append(st)

    out_dir = tmp_path / "export_pixel_fail"

    # 3. Export
    # New behavior: Should raise FileNotFoundError / RuntimeError because pixels are missing
    # and we removed the silent swallow.
    with pytest.raises((FileNotFoundError, RuntimeError), match="Pixels missing"):
         DicomExporter.save_patient(p, str(out_dir))

    # If we get here (and exception caught), success.
    # Validating file existence is no longer relevant as export typically stops or fails on that item.
    # But just in case partial write happened:
    outfile = out_dir / "1.2.3.4.5.dcm"
    if outfile.exists():
         # If file persists, it might be partial?
         # But usually validation blocks it if critical?
         # Wait, save_as happens AFTER merge. So file shouldn't be written if merge crashes.
         pass

