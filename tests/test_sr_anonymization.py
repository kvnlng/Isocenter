
import pytest
import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.sequence import Sequence
from isocenter import Session
from isocenter.entities import Instance
from isocenter.io_handlers import populate_attrs, process_sequence
from isocenter.privacy import PhiInspector, PhiFinding

def test_sr_recursive_indexing():
    """
    Verifies that nested text within a Sequence is correctly indexed during ingestion logic.
    """
    # 1. Manually simulate ingestion of a dataset with Sequence
    ds = Dataset()
    ds.PatientName = "Test^Patient"
    ds.PatientID = "123456"

    # Create a Sequence
    # (0040,A730) Content Sequence
    seq_item = Dataset()
    seq_item.ValueType = "TEXT"
    seq_item.TextValue = "Patient states pain in leg." # Sensitive!

    # Nested Sequence
    nested_item = Dataset()
    nested_item.PersonName = "Dr. Smeagol" # Sensitive!

    nested_seq = Sequence([nested_item])
    seq_item.ContentSequence = nested_seq

    ds.ContentSequence = Sequence([seq_item])

    # Check VR manually for pseudo-dataset
    # pydicom should handle VRs for standard tags automatically.
    # We only ensure we aren't getting UN (Unknown) if implicit.
    # But populate_attrs uses elem.VR.

    # ds[0x0040, 0xa730].VR = 'SQ'
    # ds[0x0040, 0xa730][0][0x0040, 0xa160].VR = 'UT' # TextValue
    # ds[0x0040, 0xa730][0][0x0040, 0xa730].VR = 'SQ'
    # ds[0x0040, 0xa730][0][0x0040, 0xa730][0][0x0010, 0x0010].VR = 'PN'
    # ds[0x0010, 0x0010].VR = 'PN' # Top PatientName
    # ds[0x0010, 0x0020].VR = 'LO' # Top PatientID

    # 2. Create Isocenter Instance
    inst = Instance("1.2.3", "1.2.840.10008.5.1.4.1.1.88.33", 1)

    # 3. Populate
    populate_attrs(ds, inst, inst.text_index)

    # 4. Verify Index Size
    # Expected:
    # 1. PatientName (Top)
    # 2. TextValue (Level 1)
    # 3. PersonName (Level 2)
    # 4. PatientID (Top)

    print("Index Contents:")
    for item, tag in inst.text_index:
        val = item.attributes.get(tag)
        print(f" - {tag}: {val}")

    tags = [t for _, t in inst.text_index]

    assert "0010,0010" in tags # PatientName
    assert "0040,a160" in tags # TextValue
    assert "0010,0010" in tags # Nested PersonName (Tag reuse)

    # Let's check values to be sure where they came from
    values = [str(i.attributes.get(t)) for i, t in inst.text_index]
    assert "Test^Patient" in values
    assert "Patient states pain in leg." in values
    assert "Dr. Smeagol" in values

def test_phi_inspector_deep_scan():
    """
    Verifies that PhiInspector finds PHI nested in a sequence.

    This used to attach `deep_item` to nothing and hand-append it to
    `inst.text_index`, so it pinned the index as the mechanism. The index
    is built once at ingest and is neither rebuilt when a session loads
    from the store nor carried into the worker copies `session.audit()`
    scans -- so it was empty on every real path, and the deep scan this
    test proved was working never once ran in production (#57). The scan
    now walks the item graph, so the item has to actually be in one.
    """
    # 1. Setup Instance with a nested sequence item
    inst = Instance("1.2.3", "class", 1)

    from isocenter.entities import DicomItem
    deep_item = DicomItem()
    deep_item.set_attr("0040,a160", "Patient has history of diabetes.")
    inst.add_sequence_item("0040,a730", deep_item)

    # 2. Setup Inspector with rule for TextValue (0040,A160)
    # We pretend 0040,A160 is flagged as PHI (it usually is or should be cleaned)
    config = {
        "0040,a160": {"name": "Text Value", "action": "REPLACE"}
    }

    inspector = PhiInspector(config_tags=config)

    # 3. Scan
    findings = inspector._scan_instance(inst, "PAT_123")

    # 4. Verify
    assert len(findings) == 1
    f = findings[0]
    assert f.tag == "0040,a160"
    assert f.value == "Patient has history of diabetes."
    assert f.remediation_proposal.new_value == "ANONYMIZED"
    assert f.entity is deep_item  # Crucial: Point to deep item, not root instance
    assert f.entity_path == (("0040,a730", 0),), (
        "the finding must record where the item sits, or it cannot be "
        "found again after crossing a process boundary")
