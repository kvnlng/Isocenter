import json
import os

import pytest

jsonschema = pytest.importorskip("jsonschema")

from gantry.entities import DicomItem
from gantry.io_handlers import populate_attrs
from gantry.murmur import build_annotations, write_annotations, SCHEMA_VERSION
from gantry.waveform import Waveform
from scripts.generate_waveform_test_data import build_ecg_dataset, add_annotation


SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "fixtures",
                           "annotations.schema.json")


def _instance_from(ds):
    """Build a Gantry Instance-like item carrying ds's sequences."""
    from gantry.entities import Instance
    inst = Instance(str(ds.SOPInstanceUID), str(ds.SOPClassUID), 1)
    populate_attrs(ds, inst, inst.text_index)
    return inst


def _waveform_from(ds):
    item = DicomItem()
    populate_attrs(ds.WaveformSequence[0], item)
    return Waveform.from_dicom_item(item)


def test_point_annotation_maps_to_a_point_finding():
    ds = build_ecg_dataset(num_samples=1000)
    add_annotation(ds, start_sample=101)
    doc = build_annotations(_instance_from(ds), _waveform_from(ds), "gantry/test")

    assert doc["schemaVersion"] == SCHEMA_VERSION
    assert len(doc["findings"]) == 1
    finding = doc["findings"][0]
    assert finding["kind"] == "point"
    # DICOM sample positions are 1-based; Murmur's are 0-based.
    assert finding["startSample"] == 100


def test_segment_annotation_maps_to_a_range_finding():
    ds = build_ecg_dataset(num_samples=1000)
    add_annotation(ds, start_sample=101, end_sample=301)
    doc = build_annotations(_instance_from(ds), _waveform_from(ds), "gantry/test")

    finding = doc["findings"][0]
    assert finding["kind"] == "range"
    assert finding["startSample"] == 100
    assert finding["endSample"] == 300


def test_category_is_the_scheme_qualified_code_and_label_is_the_meaning():
    ds = build_ecg_dataset(num_samples=500)
    add_annotation(ds, code_value="164889003",
                   code_meaning="Atrial fibrillation", scheme="SCT")
    finding = build_annotations(_instance_from(ds), _waveform_from(ds),
                                "gantry/test")["findings"][0]

    assert finding["category"] == "SCT:164889003"
    assert finding["label"] == "Atrial fibrillation"


def test_lead_comes_from_the_coded_channel_source():
    ds = build_ecg_dataset(num_samples=500)
    add_annotation(ds, channel=2)
    finding = build_annotations(_instance_from(ds), _waveform_from(ds),
                                "gantry/test")["findings"][0]
    assert finding["lead"] == "MDC_ECG_LEAD_II"


def test_free_text_becomes_the_note():
    ds = build_ecg_dataset(num_samples=500)
    add_annotation(ds, text="Onset preceded by R-on-T")
    finding = build_annotations(_instance_from(ds), _waveform_from(ds),
                                "gantry/test")["findings"][0]
    assert finding["note"] == "Onset preceded by R-on-T"


def test_absolute_time_is_never_emitted():
    ds = build_ecg_dataset(num_samples=500)
    add_annotation(ds, start_sample=10)
    finding = build_annotations(_instance_from(ds), _waveform_from(ds),
                                "gantry/test")["findings"][0]
    assert "startUnixMS" not in finding
    assert "endUnixMS" not in finding


def test_no_annotation_sequence_yields_no_findings():
    ds = build_ecg_dataset(num_samples=500)
    doc = build_annotations(_instance_from(ds), _waveform_from(ds), "gantry/test")
    assert doc["findings"] == []


def test_write_annotations_skips_when_there_are_no_findings(tmp_path):
    """An annotation-free instance must produce NO `.annotations.json`
    file -- `test_no_annotation_sequence_yields_no_findings` above only
    covers `build_annotations` (the document shape); nothing previously
    asserted `write_annotations`'s own empty-document skip. Mutating
    `if not document.get("findings"): return None` to `if False:` left
    every existing test green: an exported record would gain an
    `annotations.json` with `"findings": []` and nothing would notice.
    """
    path = str(tmp_path / "rec.annotations.json")
    document = {"schemaVersion": SCHEMA_VERSION, "source": "gantry/test", "findings": []}

    result = write_annotations(path, document)

    assert result is None, "empty-findings document should not be written"
    assert not os.path.exists(path), (
        "write_annotations wrote a file for a document with no findings")


def test_write_annotations_writes_when_findings_are_present(tmp_path):
    """Positive counterpart to the skip test above: a document that DOES
    carry findings must actually be written, so the skip test above
    cannot be satisfied by a `write_annotations` that always returns
    None.
    """
    path = str(tmp_path / "rec.annotations.json")
    document = {
        "schemaVersion": SCHEMA_VERSION,
        "source": "gantry/test",
        "findings": [{"kind": "point", "startSample": 0, "category": "AFib"}],
    }

    result = write_annotations(path, document)

    assert result == path
    assert os.path.exists(path)
    with open(path, encoding="utf-8") as f:
        written = json.load(f)
    assert written == document


def test_output_validates_against_murmurs_published_schema():
    ds = build_ecg_dataset(num_samples=1000)
    add_annotation(ds, start_sample=101, end_sample=301,
                   text="Range finding")
    add_annotation(ds, start_sample=500, channel=3)
    doc = build_annotations(_instance_from(ds), _waveform_from(ds), "gantry/1.0")

    # Guard against a trivially-passing validation: an empty findings list
    # validates fine against the schema, so a broken mapping that silently
    # drops every finding would pass jsonschema.validate() undetected.
    assert len(doc["findings"]) == 2

    with open(SCHEMA_PATH, encoding="utf-8") as f:
        schema = json.load(f)

    jsonschema.validate(instance=doc, schema=schema)


def test_schema_rejects_an_unknown_kind():
    """Prove the validator can actually fail: kind must be point/range."""
    doc = {
        "schemaVersion": 1,
        "findings": [
            {"kind": "banana", "category": "AFib", "startSample": 0},
        ],
    }
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        schema = json.load(f)

    with pytest.raises(jsonschema.exceptions.ValidationError):
        jsonschema.validate(instance=doc, schema=schema)


def test_schema_rejects_a_finding_with_no_time_anchor():
    """A finding needs startSample or startUnixMS; neither must fail."""
    doc = {
        "schemaVersion": 1,
        "findings": [
            {"kind": "point", "category": "AFib"},
        ],
    }
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        schema = json.load(f)

    with pytest.raises(jsonschema.exceptions.ValidationError):
        jsonschema.validate(instance=doc, schema=schema)


def test_schema_rejects_an_unknown_top_level_key():
    """additionalProperties: false at the top level must be enforced."""
    doc = {
        "schemaVersion": 1,
        "findings": [],
        "annotations": [],  # the old/wrong key name -- must not be accepted
    }
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        schema = json.load(f)

    with pytest.raises(jsonschema.exceptions.ValidationError):
        jsonschema.validate(instance=doc, schema=schema)


def test_annotations_file_lands_beside_the_header(tmp_path):
    import pydicom
    from gantry.session import DicomSession
    from scripts.generate_waveform_test_data import build_ecg_dataset, add_annotation

    src = tmp_path / "src"
    src.mkdir()
    ds = add_annotation(build_ecg_dataset(num_samples=500), start_sample=101)
    pydicom.dcmwrite(str(src / "ecg.dcm"), ds, write_like_original=False)

    session = DicomSession(persistence_file=str(tmp_path / "ann.db"))
    try:
        session.ingest(str(src))
        paths = session.export(str(tmp_path / "out"), format="wfdb")
    finally:
        session.close()

    base = os.path.splitext(paths[0])[0]
    ann_path = f"{base}.annotations.json"
    assert os.path.exists(ann_path)

    with open(ann_path, encoding="utf-8") as f:
        doc = json.load(f)
    assert doc["schemaVersion"] == 1
    assert doc["findings"][0]["startSample"] == 100
    assert doc["source"].startswith("gantry/")
