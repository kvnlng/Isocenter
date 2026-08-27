import os
import pydicom
import numpy as np
from unittest.mock import patch
from isocenter.entities import Instance, Study, Series, Patient
from isocenter.builders import DicomBuilder
from isocenter.io_handlers import DicomExporter, DicomImporter, DicomStore


def test_export_import_roundtrip(tmp_path, dummy_patient):
    # 1. Export
    export_dir = tmp_path / "export_test"
    DicomExporter.write_tree(dummy_patient, str(export_dir))

    DicomExporter.write_tree(dummy_patient, str(export_dir))

    files = list(export_dir.rglob("*.dcm"))
    assert len(files) == 1

    # 2. Import into new store
    store = DicomStore()
    DicomImporter.import_files([str(f) for f in files], store)

    assert len(store.patients) == 1
    imported_inst = store.patients[0].studies[0].series[0].instances[0]

    # Check if file path was captured (for lazy loading)
    assert imported_inst.file_path is not None
    assert str(export_dir) in imported_inst.file_path

def test_persistence_priority(tmp_path):
    """
    Ensure that explicit Object Model fields (e.g. StudyDate) take precedence
    over legacy attributes in DicomExporter.
    """
    # 1. Create dummy instance
    inst = Instance("1.2.3.4", "1.2.840.10008.5.1.4.1.1.2", 1)
    inst.attributes["0008,0020"] = "20000101" # Stale date
    inst.set_pixel_data(np.zeros((10,10), dtype=np.uint8)) # Valid pixels

    study = Study("1.2.3.99", "20230101") # New/Remediated date
    series = Series("1.2.3.98", "OT", 1)

    study.series.append(series)
    series.instances.append(inst)

    pat = Patient("P1", "Test")
    pat.studies.append(study)

    # 2. Export
    out_dir = tmp_path / "export_prio"

    # Mock validator to accept sparse dummy data
    # AND Mock run_parallel to run synchronously so the patch applies!
    with patch('isocenter.validation.IODValidator.validate', return_value=[]), \
         patch('isocenter.io_handlers.run_parallel', side_effect=lambda func, items, *a, **k: [func(i) for i in items]):
        DicomExporter.write_tree(pat, str(out_dir))

    # 3. Read back
    exported_files = list(out_dir.rglob("*.dcm"))
    assert len(exported_files) > 0
    exported_file = exported_files[0]
    ds = pydicom.dcmread(exported_file)

    # 4. Assert
    assert ds.StudyDate == "20230101", "Export should prioritize Study object field over attributes dict"