
import pytest
from isocenter.entities import Patient, Study, Series, Instance
from isocenter.session import DicomSession
from isocenter.privacy import PhiInspector, PhiFinding
import json
import os

def test_configured_remove_action(tmp_path):
    # 1. Setup Session & Data
    session = DicomSession(persistence_file=":memory:")

    pat = Patient("P1", "Test")
    study = Study("S1", "20230101")
    series = Series("SE1", "OT", 1)
    inst = Instance("I1", "1.2.840.10008.5.1.4.1.1.2", 1)

    # Add a generic sensitive tag that we want to REMOVE (0008,0080)
    inst.set_attr("0008,0080", "Sensitive Hospital")

    series.instances.append(inst)
    study.series.append(series)
    pat.studies.append(study)
    session.store.patients.append(pat)

    # 2. Configure PHI Tags with REMOVE action
    config_path = tmp_path / "privacy_config.yaml"
    import yaml
    with open(config_path, "w") as f:
        yaml.dump({
            "version": "2.0",
            "phi_tags": {
                "0008,0080": {
                    "name": "Institution Name",
                    "action": "REMOVE"
                }
            },
            "machines": []
        }, f)

    session.load_config(str(config_path))

    # 3. Audit (Scan)
    report = session.audit()
    findings = [f for f in report.findings if f.field_name == "Institution Name"]

    assert len(findings) == 1
    f = findings[0]
    assert f.remediation_proposal.action_type == "REMOVE_TAG"

    # 4. Anonymize
    session.anonymize(findings)

    # 5. Verify In-Memory State
    # Tag should be GONE (None)
    current_val = inst.attributes.get("0008,0080")
    assert current_val is None, f"Expected None (Removed), got {current_val}"

    # 6. Verify via Scan
    report_v = session.audit()
    findings_v = [f for f in report_v.findings if f.field_name == "Institution Name"]
    assert len(findings_v) == 0

if __name__ == "__main__":
    test_configured_remove_action("test_repro_data")
