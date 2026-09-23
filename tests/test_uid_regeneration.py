import pytest
from isocenter.entities import Instance

def test_regenerate_uid_functionality():
    """
    Verifies that regenerate_uid() correctly updates the SOP Instance UID,
    syncs the DICOM attributes, and detaches the instance from the file system.
    """
    # 1. Create a dummy instance
    original_uid = "1.2.3.4.5"
    inst = Instance(
        sop_instance_uid=original_uid,
        sop_class_uid="1.2.840.10008.5.1.4.1.1.2",
        instance_number=1,
        file_path="/tmp/dummy.dcm"
    )

    # 2. Verify initial state
    assert inst.sop_instance_uid == original_uid
    assert inst.attributes["0008,0018"] == original_uid
    assert inst.file_path == "/tmp/dummy.dcm"

    # 3. Regenerate UID
    # The caller supplies the UID since #544: `redact()` derives it under
    # the project secret (`services._redacted_uid_for`) rather than drawing
    # one here.
    inst.regenerate_uid("2.25.544")

    # 4. Verify post-regeneration state
    new_uid = inst.sop_instance_uid

    assert new_uid == "2.25.544", "UID should be the one given"

    # Attribute sync
    assert inst.attributes["0008,0018"] == new_uid, "DICOM Tag 0008,0018 should match new UID"

    # File detachment
    assert inst.file_path is None, "Instance should be detached from original file path"

    print(f"\nUID Regeneration Verified: {original_uid} -> {new_uid}")
