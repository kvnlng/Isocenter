
import pytest
import os
from isocenter.entities import Patient, Study, Series, Instance
from isocenter.io_handlers import DicomExporter

def test_export_command_set_error(tmp_path):
    # Setup Patient Hierarchy
    p = Patient("P_CMD", "Command Set Test")
    st = Study("S1", None) # None date handles by exporter logic
    se = Series("SE1", "OT", 1)

    # Create instance and inject illegal 0000 group tag
    inst = Instance("I1", "1.2.840.10008.5.1.4.1.1.2", 1)
    # 0000,0010 is CommandGroupLength, definitely illegal for file write
    inst.attributes["0000,0010"] = 100

    # Add Mandatory IOD Tags to pass validation
    inst.attributes["0008,0020"] = "20230101" # Study Date
    inst.attributes["0008,0030"] = "120000"   # Study Time
    inst.attributes["0018,0050"] = "1.0"      # Slice Thickness
    inst.attributes["0018,0060"] = "120"      # KVP
    inst.attributes["0020,0032"] = ["0","0","0"] # Position
    inst.attributes["0020,0037"] = ["1","0","0","0","1","0"] # Orientation
    inst.attributes["0020,0037"] = ["1","0","0","0","1","0"] # Orientation
    inst.attributes["0028,0030"] = ["0.5", "0.5"] # Pixel Spacing

    # NEW: Add dummy pixels to satisfy strict export check
    import numpy as np
    inst.set_pixel_data(np.zeros((10,10), dtype=np.uint8))

    se.instances.append(inst)
    st.series.append(se)
    p.studies.append(st)

    out_dir = tmp_path / "export_bad"

    # Act: This should NOW SUCCEED (Command tags ignored)
    from unittest.mock import patch
    import concurrent.futures
    # We must patch ProcessPoolExecutor to ThreadPoolExecutor because 'spawn'ed processes
    # (default on macOS/Py3.14) do NOT see the mocked IODValidator.
    with patch("isocenter.validation.IODValidator.validate", return_value=[]), \
         patch("concurrent.futures.ProcessPoolExecutor", side_effect=concurrent.futures.ThreadPoolExecutor):
        DicomExporter.write_tree(p, str(out_dir))

    # Verify file exists (recursive search)
    files = list(out_dir.rglob("*.dcm"))
    assert len(files) == 1
    # The SOP Instance UID, not InstanceNumber. This asserted
    # "0001.dcm" until #50: this path named files by InstanceNumber
    # (0020,0013) while session.export() used the SOP UID, so the
    # same instance landed under two names. InstanceNumber also is
    # not unique and collides silently within a series.
    assert files[0].name == f"{p.studies[0].series[0].instances[0].sop_instance_uid}.dcm"
