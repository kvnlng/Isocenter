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
import re

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


def _three_patients(tmp_path, unstashable_p2=False):
    """Patients `''`, `P1` and `P2`, each with a name the floor removes;
    `P2` optionally carrying an OB private value no token can hold."""
    for pid, suffix in (("", "5821"), ("P1", "5822"), ("P2", "5823")):
        path = write_ct(tmp_path / "in" / f"{pid or 'empty'}.dcm", "TMP", suffix,
                        name=f"Secret^{pid or 'Empty'}")
        ds = pydicom.dcmread(path)
        ds.PatientID = pid
        if unstashable_p2 and pid == "P2":
            ds.add_new(0x00291010, "LO", "PRIVCREATOR")
            ds.add_new(0x00291110, "OB", b"\x01\x02\x03\x04")
        ds.save_as(path)
    session = Session(str(tmp_path / "s.db"))
    session.ingest(str(tmp_path / "in"))
    session.enable_reversible_anonymization(str(tmp_path / "k.key"))
    return session


def _locked(session):
    return sorted(p.patient_id for p in session.store.patients
                  if "0400,0500" in p.studies[0].series[0].instances[0].sequences)


def test_a_report_lock_locks_an_empty_id_patient(tmp_path):
    """`lock_identities(report)` built its ID list with a truthy test, so
    the `''` patient the report names was skipped without a word, and after
    `anonymize()` its name was unrecoverable (review of #615, F-1). Kills
    `and item.patient_id` in the report normalisation."""
    with _three_patients(tmp_path) as session:
        report = session.audit()
        assert sorted({f.patient_id for f in report.findings}) == ["", "P1", "P2"]
        session.lock_identities(report)
        assert _locked(session) == ["", "P1", "P2"]
        empty = next(p for p in session.store.patients if p.patient_id == "")
        session.anonymize(report)
        assert empty.patient_name != "Secret^Empty"
        session.recover_patient_identity(empty.patient_id, restore=True)
        assert empty.patient_name == "Secret^Empty"


def test_a_report_lock_numbers_an_empty_id_patient_among_those_found(tmp_path):
    """With `''` counted, the documented recipe `sorted(found)[n - 1]` names
    the refused patient; skipped, `[2 of 2]` sent the caller to `P1`. Kills
    `and item.patient_id` in the report normalisation."""
    with _three_patients(tmp_path, unstashable_p2=True) as session:
        report = session.audit()
        with pytest.raises(RuntimeError) as caught:
            session.lock_identities(report, tags_to_lock=["0010,0010", "0010,0020", "0029,1110"])
        message = str(caught.value)
        assert message.startswith("lock_identities: 1 of 3 patients cannot be locked"), message
        numbered = re.findall(r"^\[(\d+) of (\d+)\] lock_identities: this patient holds a "
                              r"value in 0029,1110", message, flags=re.MULTILINE)
        assert numbered == [("3", "3")], message
        found = sorted({f.patient_id for f in report.findings}
                       & {p.patient_id for p in session.store.patients})
        assert found[int(numbered[0][0]) - 1] == "P2"
        assert _locked(session) == []
