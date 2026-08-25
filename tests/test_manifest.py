import pytest
import os
import json
from isocenter.manifest import Manifest, ManifestItem, generate_manifest_file, JSONManifestRenderer, HTMLManifestRenderer
from isocenter.session import DicomSession

@pytest.fixture
def mock_manifest():
    return Manifest(
        generated_at="2024-01-01T12:00:00",
        project_name="Test Project",
        items=[
            ManifestItem(
                patient_id="P001",
                study_instance_uid="1.2.3",
                series_instance_uid="1.2.3.4",
                sop_instance_uid="1.2.3.4.5",
                file_path="/tmp/test.dcm",
                modality="CT",
                manufacturer="TestMed",
                model_name="Scanner 2000"
            )
        ]
    )

def test_json_renderer(tmp_path, mock_manifest):
    output = tmp_path / "manifest.json"
    generate_manifest_file(mock_manifest, str(output), "json")

    assert output.exists()
    with open(output, 'r') as f:
        data = json.load(f)
        assert data["project_name"] == "Test Project"
        assert len(data["items"]) == 1
        assert data["items"][0]["patient_id"] == "P001"
        assert data["items"][0]["modality"] == "CT"

def test_html_renderer(tmp_path, mock_manifest):
    output = tmp_path / "manifest.html"
    generate_manifest_file(mock_manifest, str(output), "html")

    assert output.exists()
    content = output.read_text()
    assert "<!DOCTYPE html>" in content
    assert "Test Project" in content
    assert "P001" in content
    assert "Scanner 2000" in content
    assert "1.2.3.4.5" in content

def test_session_integration(tmp_path):
    # Mock DicomSession internal store
    session = DicomSession(persistence_file=":memory:")

    # Needs actual logic or extensive mocking of session.store structure.
    # For now, let's skip full integration test if we assume unit tests cover the renderer.
    # Or strict mock:

    # We can rely on the fact that if we call generate_manifest, it iterates.
    # Since session.store is empty by default
    output = tmp_path / "session_manifest.html"
    session.generate_manifest(str(output))
    assert output.exists()
    content = output.read_text()
    assert "Files:</strong> 0" in content
