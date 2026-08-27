
import pytest
from isocenter.entities import Patient, Study, Series, Instance
from isocenter.io_handlers import DicomExporter

def test_export_string_date_error(tmp_path):
    # Setup
    p = Patient("P_DATE_ERR", "Date Error")
    # Inject string date explicitly
    st = Study("S1", "20230101")
    se = Series("SE1", "OT", 1)
    inst = Instance("I1", "1.2.3", 1)

    # Needs valid attributes to pass IOD validator
    inst.attributes["0008,0020"] = "20230101"
    inst.attributes["0008,0030"] = "120000"
    inst.attributes["0018,0050"] = "1.0"
    inst.attributes["0018,0060"] = "120"
    inst.attributes["0020,0032"] = ["0","0","0"]
    inst.attributes["0020,0037"] = ["1","0","0","0","1","0"]
    inst.attributes["0028,0030"] = ["0.5", "0.5"]

    # NEW: Add dummy pixels to satisfy strict export check
    import numpy as np
    inst.set_pixel_data(np.zeros((10,10), dtype=np.uint8))

    se.instances.append(inst)
    st.series.append(se)
    p.studies.append(st)

    out_dir = tmp_path / "export_date_err"

    # Act
    # This should now SUCCEED
    DicomExporter.write_tree(p, str(out_dir))

    # Assert using rglob to support structured export
    files = list(out_dir.rglob("*.dcm"))
    assert len(files) == 1
    # The SOP Instance UID, not InstanceNumber. This asserted
    # "0001.dcm" until #50: this path named files by InstanceNumber
    # (0020,0013) while session.export() used the SOP UID, so the
    # same instance landed under two names. InstanceNumber also is
    # not unique and collides silently within a series.
    assert files[0].name == f"{p.studies[0].series[0].instances[0].sop_instance_uid}.dcm"
