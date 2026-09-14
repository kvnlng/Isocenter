"""A patient whose Patient ID element is empty is scanned as a patient (#581).

Ingest keeps an *empty* PatientID as `''` (an *absent* one becomes
`UnknownPatient`, `io_handlers.py`), and the scan files the patient's
findings under that `''`. Three filters dropped every finding whose
`entity_uid` was falsy, so `''` never marked its patient:

- `_record_scan_results` stamped the patient CLEARED with its name
  finding outstanding, and the manifest called it anonymized;
- `_scan_before_export` left the patient out of the identifying set, so
  `export(check_burned_in=True)` wrote its instances with the original
  name;
- `_ScanTally.__init__` held nothing for `''`, so a pass over it had no
  opinion.

Measured on 57400d1, 3.12 and 3.14t (`probes-E/p581.py`, `p581c.py`):
`privacy_profile: none`, PatientID `''`, no Study Date -- CLEARED after
`audit()`, manifest `[True]`, and the safe export wrote one file carrying
`Secret^Name`. With the Patient ID `PID-X` instead it wrote none.
"""
import json

import pydicom
import pytest

from isocenter import Session
from isocenter.entities import PhiStatus

from support.ct_small_files import write_ct

NAME = "Secret^Name"


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _session(tmp_path, patient_id=""):
    path = write_ct(tmp_path / "in" / "a.dcm", "TMP", "5811", study_date=None, name=NAME)
    ds = pydicom.dcmread(path)
    ds.PatientID = patient_id
    ds.save_as(path)
    config = tmp_path / "none.yaml"
    config.write_text("privacy_profile: none\nremove_private_tags: false\nphi_tags: {}\n",
                      encoding="utf-8")
    session = Session(str(tmp_path / "s.db"))
    session.load_config(str(config))
    session.ingest(str(tmp_path / "in"))
    return session


def test_an_empty_id_patient_with_a_name_finding_reads_identified(tmp_path):
    """(a) Kills `_record_scan_results` filtering on a truthy uid."""
    with _session(tmp_path) as session:
        [patient] = session.store.patients
        assert patient.patient_id == ""
        report = session.audit()
        assert [(f.entity_type, f.field_name, f.entity_uid) for f in report.findings] == [
            ("Patient", "patient_name", "")]
        assert patient.phi_status is PhiStatus.IDENTIFIED
        manifest = tmp_path / "m.json"
        session.generate_manifest(str(manifest), format="json")
        items = json.loads(manifest.read_text(encoding="utf-8"))
        items = items if isinstance(items, list) else items.get("items", items.get("instances"))
        assert [item["anonymized"] for item in items] == [False]


@pytest.mark.parametrize("patient_id", ["", "PID-X"], ids=["empty", "control"])
def test_the_safe_export_withholds_an_empty_id_patient(tmp_path, patient_id):
    """(c) Kills `_scan_before_export` filtering on a truthy uid. The
    `PID-X` control is the same file with a Patient ID, which was always
    withheld."""
    with _session(tmp_path, patient_id) as session:
        summary = session.export(str(tmp_path / "out"), use_compression=False,
                                 check_burned_in=True)
        assert summary.written == 0
    names = [str(pydicom.dcmread(str(p)).PatientName) for p in (tmp_path / "out").rglob("*.dcm")]
    assert NAME not in names
    assert names == []
