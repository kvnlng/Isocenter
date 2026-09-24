"""`REPLACE` writes its `value:`, and `set_phi_tag(value=)` stores it
there (#538).

Measured on ac33641: `{action: REPLACE, value: Project-X}` on Institution
Name loaded, kept the `value` key, and exported `ANONYMIZED`; the arm
had the constant. `set_phi_tag("0008,0080", "REPLACE",
replacement="RESEARCH STUDY")` stored a `replacement` key that nothing
read, `save()` wrote it to YAML, and the export read `ANONYMIZED`.

**Why this file imports what it does.** `PhiInspector` through
`isocenter.privacy` and `RemediationService` through
`isocenter.remediation`, so both modules' probe rows are charged.
"""
import pydicom
import pytest
import yaml

from isocenter.entities import (DicomItem, DicomSequence, Instance, Patient,
                                Series, Study)
from isocenter.privacy import PhiInspector
from isocenter.remediation import RemediationService
from isocenter.session import DicomSession

from support.ct_small_files import write_ct
from support.project_secret import FIXED_A, load_fixed_secret

INSTITUTION = "0008,0080"


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")


def _export_one(session, out):
    summary = session.export(str(out), use_compression=False)
    assert summary.written == 1, summary.failures
    (path,) = list(out.rglob("*.dcm"))
    return pydicom.dcmread(str(path))


def test_a_replace_value_is_written(tmp_path):
    """Kills the constant restored in the arm, and the already-replaced
    test left comparing against `ANONYMIZED` (the re-audit then raises the
    value the rule wrote)."""
    write_ct(tmp_path / "in" / "a.dcm", "P538", "538")
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(yaml.safe_dump({"privacy_profile": "none", "phi_tags": {
        INSTITUTION: {"action": "REPLACE", "value": "Project-X"}}}), encoding="utf-8")
    with DicomSession(str(tmp_path / "s.db")) as session:
        load_fixed_secret(session, tmp_path, FIXED_A)
        session.ingest(str(tmp_path / "in"))
        session.load_config(str(cfg))
        session.anonymize(session.audit())
        instance = session.store.patients[0].studies[0].series[0].instances[0]
        assert instance.attributes[INSTITUTION] == "Project-X"
        assert [f for f in session.audit() if f.tag == INSTITUTION] == []
        ds = _export_one(session, tmp_path / "out")
    assert ds.InstitutionName == "Project-X"


def test_set_phi_tag_replacement_is_the_value(tmp_path):
    """The stored key is `value`, the saved YAML has `value:` and no
    `replacement:`, and a fresh session that loads the saved file writes
    it. Kills `val["replacement"]` kept."""
    write_ct(tmp_path / "in" / "a.dcm", "P538", "538")
    saved = tmp_path / "saved.yaml"
    with DicomSession(str(tmp_path / "first.db")) as session:
        session.configuration.config_path = str(saved)
        session.configuration.set_phi_tag(INSTITUTION, "REPLACE",
                                          value="RESEARCH STUDY")
        session.configuration.save()
        assert session.configuration.phi_tags[INSTITUTION] == {
            "name": "Custom Tag", "action": "REPLACE", "value": "RESEARCH STUDY"}
    rule = yaml.safe_load(saved.read_text(encoding="utf-8"))["phi_tags"][INSTITUTION]
    assert rule == {"name": "Custom Tag", "action": "REPLACE", "value": "RESEARCH STUDY"}

    with DicomSession(str(tmp_path / "second.db")) as session:
        load_fixed_secret(session, tmp_path, FIXED_A)
        session.ingest(str(tmp_path / "in"))
        session.load_config(str(saved))
        session.anonymize(session.audit())
        ds = _export_one(session, tmp_path / "out")
    assert ds.InstitutionName == "RESEARCH STUDY"


def test_a_nested_replace_value_is_written():
    """The value inside a sequence item, as at the top level. Kills the
    value read only on the top-level path."""
    patient = Patient("P538", "Orig^Name")
    study = Study("1.2.826.0.1.538", "20240101")
    series = Series("1.2.826.0.1.538.1", "OT", 1)
    instance = Instance("1.2.826.0.1.538.1.1", "1.2.840.10008.5.1.4.1.1.7", 1)
    instance.set_attr(INSTITUTION, "Top Hospital")
    item = DicomItem()
    item.set_attr(INSTITUTION, "Nested Hospital")
    instance.sequences["0008,1110"] = DicomSequence(tag="0008,1110", items=[item])
    series.instances.append(instance)
    study.series.append(series)
    patient.studies.append(study)

    policy = {INSTITUTION: {"action": "REPLACE", "value": "Project-X"}}
    findings = PhiInspector(config_tags=policy, project_secret=FIXED_A).scan_patient(patient)
    RemediationService(project_secret=FIXED_A).apply_remediation(findings)

    assert instance.attributes[INSTITUTION] == "Project-X"
    assert item.attributes[INSTITUTION] == "Project-X"
    again = PhiInspector(config_tags=policy, project_secret=FIXED_A).scan_patient(patient)
    assert [f for f in again if f.tag == INSTITUTION] == []
