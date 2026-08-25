
import pytest
import os
import json
from isocenter.session import DicomSession

def test_scaffold_generates_basic_profile(tmp_path):
    # 1. Setup Session (Mock Store not needed for scaffold structure check, hopefully)
    # Actually, scaffold_config accesses self.store.get_unique_equipment().
    # We need a session, but it can be empty for this specific check if we ignore machines.

    session = DicomSession(":memory:")

    # 2. Run Scaffolding
    output_path = tmp_path / "scaffold_profile.yaml"
    session.create_config(str(output_path))

    # 3. Verify Output
    assert output_path.exists()

    with open(output_path, "r") as f:
        import yaml
        data = yaml.safe_load(f)

    # Check Profile
    assert data.get("privacy_profile") == "basic"

    # Check PHI Tags (Should ONLY be overrides, not the full list)
    tags = data.get("phi_tags", {})

    # Should contain research overrides like Age or Sex being KEPT
    assert "0010,0040" in tags # Patient Sex
    assert tags["0010,0040"]["action"] == "KEEP"

    # Should NOT contain standard removals like Patient Name (handled by profile)
    # Note: 0010,0010 is usually REMOVE in basic profile.
    assert "0010,0010" not in tags
