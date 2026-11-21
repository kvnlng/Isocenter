"""A patient whose ID an earlier pass replaced is settled under the ID the
audit raised its findings under (#582, the L-1 comment).

`_settle_statuses` settles each resolved finding's live UID beside the
uid the finding names, so a hand-built finding filed under a wrong uid
cannot leave its entity REMEDIATED over what the audit raised under its
real one. A patient is read at the `patient_id` it held as the pass
began. After a first pass that replaced only the Patient ID, that is
this store's pseudonym, which the tally does not hold, so the tally had
no opinion: a second pass handed a patient finding under a wrong uid
stamped the patient REMEDIATED over its source name. (The study, the
instance, the manifest and the grade were already right.)

Now a pseudonym the tally does not hold is read back to the ID it was
minted from, among the IDs the audit raised, under the patient's own
scheme, keyed or legacy unkeyed.

**Why this file imports what it does.** The pipeline through
`isocenter.session`, hand-built findings through `isocenter.privacy`,
the status and the scheme through `isocenter.entities`: those rows of
the probe's `TARGETS`, with `remediation`'s.
"""
import pytest

from isocenter.entities import JITTER_SCHEME_UNKEYED, PhiStatus
from isocenter.privacy import PhiFinding, PhiRemediation
from isocenter.session import DicomSession
from support.ct_small_files import write_ct
from support.project_secret import FIXED_A, load_fixed_secret


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)


def _session(tmp_path):
    write_ct(tmp_path / "src" / "a.dcm", "PID-582L", 582, name="Alpha^One")
    cfg = tmp_path / "c.yaml"
    cfg.write_text("phi_tags:\n  '0010,0010': {action: REPLACE, value: ANONYMIZED}\n")
    session = DicomSession(str(tmp_path / "s.db"))
    load_fixed_secret(session, tmp_path, FIXED_A)
    session.load_config(str(cfg))
    session.ingest(str(tmp_path / "src"))
    return session


def _misnamed(patient):
    return PhiFinding(
        entity_uid="SOMEONE-ELSE", entity_type="Patient", field_name="Patient ID",
        value="PID-582L", reason="hand-built", entity=patient, tag="0010,0020",
        remediation_proposal=PhiRemediation(
            action_type="REPLACE_TAG", target_attr="patient_id",
            new_value=patient.patient_id, original_value="PID-582L"))


def _partial_pass(session):
    report = session.audit()
    patient = session.store.patients[0]
    ids = [f for f in report.findings if f.entity_type == "Patient"
           and f.remediation_proposal.target_attr == "patient_id"]
    assert len(ids) == 1
    session.anonymize(ids)
    assert patient.patient_id.startswith("ANON_")
    assert patient.patient_name == "Alpha^One"
    return patient


def test_a_misnamed_patient_finding_after_a_partial_pass_does_not_remediate_the_patient(
        tmp_path):
    with _session(tmp_path) as session:
        patient = _partial_pass(session)

        session.anonymize([_misnamed(patient)])

        assert patient.patient_name == "Alpha^One"
        assert patient.phi_status is PhiStatus.IDENTIFIED


def test_an_unkeyed_legacy_patients_pseudonym_is_read_back_too(tmp_path):
    """The legacy scheme mints its pseudonym without the secret; the read
    back mints under the patient's own scheme."""
    from isocenter.privacy import _unkeyed_replacement_id_for  # pylint: disable=import-outside-toplevel
    with _session(tmp_path) as session:
        report = session.audit()
        patient = session.store.patients[0]
        patient._jitter_scheme = JITTER_SCHEME_UNKEYED  # pylint: disable=protected-access
        legacy = _unkeyed_replacement_id_for("PID-582L")
        ids = [f for f in report.findings if f.entity_type == "Patient"
               and f.remediation_proposal.target_attr == "patient_id"]
        ids[0].remediation_proposal.new_value = legacy
        ids[0].remediation_proposal.metadata = dict(
            ids[0].remediation_proposal.metadata or {}, jitter_scheme=JITTER_SCHEME_UNKEYED)
        session.anonymize(ids)
        assert patient.patient_id == legacy

        session.anonymize([_misnamed(patient)])

        assert patient.phi_status is PhiStatus.IDENTIFIED

