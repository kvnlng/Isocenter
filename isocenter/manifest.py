from dataclasses import dataclass, asdict
from typing import List, Optional, Protocol
import json
import os
import datetime
from .logger import get_logger


@dataclass
class ManifestItem:
    """
    Represents a single exported file or instance in the manifest.

    Attributes:
        patient_id (str): The Patient ID.
        study_instance_uid (str): The Study Instance UID.
        series_instance_uid (str): The Series Instance UID.
        sop_instance_uid (str): The SOP Instance UID.
        file_path (str): Relative or absolute path to the exported file.
        file_size_bytes (int): Size of the file in bytes.
        modality (str): Modality code (e.g. CT, MR).
        manufacturer (str): Manufacturer name.
        model_name (str): Model name.
        anonymized (bool): Status flag indicating if anonymization was applied.
    """
    patient_id: str
    study_instance_uid: str
    series_instance_uid: str
    sop_instance_uid: str

    # File details
    file_path: str = ""
    file_size_bytes: int = 0

    # Optional metadata
    modality: str = ""
    manufacturer: str = ""
    model_name: str = ""

    # Processing details
    anonymized: bool = True


@dataclass
class Manifest:
    """
    Collection of manifest items representing the entire export session.

    Attributes:
        generated_at (str): ISO timestamp of generation.
        items (List[ManifestItem]): The list of file entries.
        project_name (str): Name of the project/session.
        total_files (int): Total count of files.
        total_size_bytes (int): Total size in bytes.
    """
    generated_at: str
    items: List[ManifestItem]
    project_name: str = "Isocenter Session"
    total_files: int = 0
    total_size_bytes: int = 0

    def to_dict(self):
        """
        Converts the manifest to a dictionary for JSON serialization.
        """
        return {
            "generated_at": self.generated_at,
            "project_name": self.project_name,
            "total_files": len(self.items),
            "total_size_bytes": sum(i.file_size_bytes for i in self.items),
            "items": [asdict(i) for i in self.items]
        }


class ManifestRenderer(Protocol):
    """Protocol for a manifest renderer."""

    def render(self, manifest: Manifest, output_path: str) -> None:
        """
        Renders the manifest to the specified file.

        Args:
            manifest (Manifest): The manifest data.
            output_path (str): The destination file path.
        """
        ...


class JSONManifestRenderer:
    """Renders the manifest as a JSON file."""

    def render(self, manifest: Manifest, output_path: str) -> None:
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(manifest.to_dict(), f, indent=2)


class HTMLManifestRenderer:
    """Renders the manifest as a standalone HTML file."""

    def render(self, manifest: Manifest, output_path: str) -> None:
        # Basic accessible HTML table
        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Isocenter Manifest - {manifest.project_name}</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; margin: 2rem; color: #333; }}
        h1 {{ margin-bottom: 0.5rem; }}
        .meta {{ color: #666; margin-bottom: 2rem; }}
        table {{ width: 100%; border-collapse: collapse; font-size: 0.9rem; }}
        th, td {{ text-align: left; padding: 0.75rem; border-bottom: 1px solid #ddd; }}
        th {{ background-color: #f8f9fa; font-weight: 600; position: sticky; top: 0; }}
        tr:hover {{ background-color: #f5f5f5; }}
        .badge {{ padding: 0.25rem 0.5rem; border-radius: 4px; font-size: 0.8rem; font-weight: 600; }}
        .badge-success {{ background-color: #d4edda; color: #155724; }}
    </style>
</head>
<body>
    <h1>Data Manifest</h1>
    <div class="meta">
        <strong>Project:</strong> {manifest.project_name} &bull;
        <strong>Generated:</strong> {manifest.generated_at} &bull;
        <strong>Files:</strong> {len(manifest.items)}
    </div>

    <table>
        <thead>
            <tr>
                <th>Patient ID</th>
                <th>Study UID</th>
                <th>Series UID</th>
                <th>Modality</th>
                <th>Manufacturer</th>
                <th>Model</th>
                <th>SOP Instance UID</th>
                <th>File Path</th>
            </tr>
        </thead>
        <tbody>
"""
        for item in manifest.items:
            html += f"""
            <tr>
                <td>{item.patient_id}</td>
                <td>{item.study_instance_uid}</td>
                <td>{item.series_instance_uid}</td>
                <td>{item.modality}</td>
                <td>{item.manufacturer}</td>
                <td>{item.model_name}</td>
                <td>{item.sop_instance_uid}</td>
                <td><code>{item.file_path}</code></td>
            </tr>
"""
        html += """
        </tbody>
    </table>
</body>
</html>
"""
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(html)


def generate_manifest_file(manifest: Manifest, output_path: str, format: str = "html"):
    """
    Generates a manifest file in the requested format.

    Args:
        manifest (Manifest): The manifest object to export.
        output_path (str): The destination file path.
        format (str): 'json' or 'html'.

    Raises:
        ValueError: If format is unsupported.
    """
    if format.lower() == "json":
        renderer = JSONManifestRenderer()
    elif format.lower() == "html":
        renderer = HTMLManifestRenderer()
    else:
        raise ValueError(f"Unsupported manifest format: {format}")

    renderer.render(manifest, output_path)
