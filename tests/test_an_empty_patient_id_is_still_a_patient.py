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

**Since #584 ingest keys such a patient `NO_PATIENT_ID_PREFIX + <Study
UID>`, not `''`.** Every behaviour #581 fixed is kept here for that
synthetic patient, and the `''` filters stay exercised by a hand-built
`Patient("")`, which a pre-1.0 store or user code can still hold.
"""
import json
import re

import pydicom
import pytest

from isocenter import Session
from isocenter.builders import DicomBuilder
from isocenter.entities import NO_PATIENT_ID_PREFIX, PhiStatus, is_synthetic_patient_id

from support.ct_small_files import study_uid, write_ct

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


def _hand_built_empty_id(session):
    """A `Patient("")` as a pre-1.0 store or user code holds it."""
    builder = (DicomBuilder.start_patient("", NAME)
               .add_study(study_uid("5819"), None)
               .add_series(study_uid("5819") + ".1", "CT", 1))
    builder.add_instance(study_uid("5819") + ".1.1", "1.2.840.10008.5.1.4.1.1.2", 1)
    session.store.patients.append(builder.end_series().end_study().build())


@pytest.mark.parametrize("built", ["ingested", "hand-built"])
def test_an_empty_id_patient_with_a_name_finding_reads_identified(tmp_path, built):
    """(a) Kills `_record_scan_results` filtering on a truthy uid (the
    hand-built `''` case) and on a synthetic one (the ingested case)."""
    if built == "ingested":
        session = _session(tmp_path)
        expected_id = NO_PATIENT_ID_PREFIX + study_uid("5811")
    else:
        config = tmp_path / "none.yaml"
        config.write_text("privacy_profile: none\nremove_private_tags: false\n"
                          "phi_tags: {}\n", encoding="utf-8")
        session = Session(str(tmp_path / "s.db"))
        session.load_config(str(config))
        _hand_built_empty_id(session)
        expected_id = ""
    with session:
        [patient] = session.store.patients
        assert patient.patient_id == expected_id
        report = session.audit()
        assert [(f.entity_type, f.field_name, f.entity_uid) for f in report.findings] == [
            ("Patient", "patient_name", expected_id)]
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
        synthetic = NO_PATIENT_ID_PREFIX + study_uid("5821")
        assert sorted({f.patient_id for f in report.findings}) == ["P1", "P2", synthetic]
        session.lock_identities(report)
        assert _locked(session) == ["P1", "P2", synthetic]
        empty = next(p for p in session.store.patients if p.patient_id == synthetic)
        session.anonymize(report)
        assert empty.patient_name != "Secret^Empty"
        session.recover_patient_identity(empty.patient_id, restore=True)
        assert empty.patient_name == "Secret^Empty"
        # The restore keeps the key: the token's Patient ID is blank.
        assert empty.patient_id == synthetic


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
        # `P2` is second of three: the synthetic key of the ID-less
        # patient sorts after it (a backslash follows the letters).
        assert numbered == [("2", "3")], message
        found = sorted({f.patient_id for f in report.findings}
                       & {p.patient_id for p in session.store.patients})
        assert found[int(numbered[0][0]) - 1] == "P2"
        assert _locked(session) == []


@pytest.mark.parametrize("how", ["absent", "empty", "blank"])
def test_two_restored_id_less_subjects_are_not_merged(tmp_path, how):
    """T-B6's addition. A restore that wrote the token's blank Patient ID
    back would make both subjects `''`, and the next `audit()`'s shared-ID
    merge (#563) would collapse them into one patient. Kills M-B11. The
    blank case (`" \t "`, which pydicom reads back as `" \t"`: the lock
    stashes that copy) kills MR1, the guard's `.strip()` read as
    `restored_id == ""`: the second restore wrote `" \t"` over both keys
    and made one patient (review of this PR, finding 2)."""
    for suffix, name in (("5831", "Secret^One"), ("5832", "Secret^Two")):
        path = write_ct(tmp_path / "in" / f"{suffix}.dcm", "TMP", suffix, name=name)
        ds = pydicom.dcmread(path)
        if how == "absent":
            del ds.PatientID
        else:
            ds.PatientID = " \t " if how == "blank" else ""
        ds.save_as(path)
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        session.enable_reversible_anonymization(str(tmp_path / "k.key"))
        report = session.audit()
        session.lock_identities(report)
        session.anonymize(report)
        for patient in list(session.store.patients):
            session.recover_patient_identity(patient.patient_id, restore=True)
        session.audit()
        ids = sorted(p.patient_id for p in session.store.patients)
        names = sorted(p.patient_name for p in session.store.patients)
        copies = [i.attributes.get("0010,0020") for p in session.store.patients
                  for st in p.studies for se in st.series for i in se.instances]
    # The token stashed the ID the export writes, so the restore wrote a
    # blank copy back and not the key (M-B20: the lock's fallback reading
    # `patient_id` put the source Study UID into the token and the copy).
    assert all("no-patient-id" not in str(c) for c in copies), copies
    assert ids == [NO_PATIENT_ID_PREFIX + study_uid("5831"),
                   NO_PATIENT_ID_PREFIX + study_uid("5832")]
    assert all(is_synthetic_patient_id(pid) for pid in ids)
    assert names == ["Secret^One", "Secret^Two"]
