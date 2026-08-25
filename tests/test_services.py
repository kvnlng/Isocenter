from isocenter.services import MachinePixelIndex, RedactionService
from isocenter.io_handlers import DicomStore
import numpy as np


def test_machine_index(dummy_patient):
    store = DicomStore()
    store.patients.append(dummy_patient)

    index = MachinePixelIndex()
    index.index_store(store)

    results = index.get_by_machine("SN-999")
    assert len(results) == 1
    assert results[0].sop_instance_uid == "1.2.840.111.1.1.1"


def test_redaction_service(dummy_patient):
    store = DicomStore()
    store.patients.append(dummy_patient)

    svc = RedactionService(store)

    # Original pixel check (top left is 0)
    inst = store.patients[0].studies[0].series[0].instances[0]
    # Set a value to verify it gets cleared
    inst.pixel_array[20, 20] = 500

    # Act: Redact region 10-50
    svc.redact_machine_instances("SN-999", [(10, 50, 10, 50)])

    # Assert
    # Assert
    assert inst.pixel_array[20, 20] == 0

    # Verify Redaction Flags
    # 1. UID should change
    assert inst.sop_instance_uid != "1.2.840.111.1.1.1"

    # 2. Image Type
    img_type = inst.attributes.get("0008,0008")
    assert "DERIVED" in img_type

    # 3. Description
    desc = inst.attributes.get("0008,2111")
    assert "Isocenter Pixel Redaction" in desc

    # 4. Burned In Annotation
    assert inst.attributes.get("0028,0301") == "NO"

    # 5. Code Sequence
    seq = inst.sequences.get("0008,9215")
    assert seq is not None
    assert seq.items[0].attributes["0008,0104"] == "Pixel Data modification"