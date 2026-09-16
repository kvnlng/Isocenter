"""A `REMOVE` on a `Patient` or `Study` field already `None` is satisfied
(#661).

The `REMOVE_TAG` Python-attribute arm tested `hasattr(entity,
target_attr)`, which is true of a slots field holding `None` -- #625's
trap one arm over, and #626's satisfied case missing on this one. So
`0010,0010: {action: REMOVE}` over a graph whose `patient_name` a pass
had already cleared filed `REMEDIATION_REMOVE ... removed from 0
instance copies` and counted as applied: a row for work nothing did, and
an applied count nobody can reconcile.

"Already gone" is all of: the field is one the exporter stamps
(`ENTITY_FIELD_TAGS`); it holds `None` (a `""` is a present, empty
value); no instance beneath the entity still holds the field's tag, so a
field cleared by hand while the copies survive is still a real removal;
and the absence is read on the object this session holds at the
finding's address, never on an entity handed in at another one (#626,
#644). These tests pin each of those four, and the three shapes that
must keep behaving exactly as they did.
"""
import json
import shutil
import sqlite3

from pydicom.data import get_testdata_file

from isocenter.entities import Patient, PhiStatus, Study
from isocenter.privacy import PhiFinding, PhiRemediation
from isocenter.remediation import RemediationService
from isocenter.session import DicomSession

REMOVE_RULES = {"0010,0010": {"name": "Patient Name", "action": "REMOVE"},
                "0008,0020": {"name": "Study Date", "action": "REMOVE"}}
STALE = "not the object this session holds at"


def _sources(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    for name in ("CT_small.dcm", "MR_small.dcm"):
        shutil.copy(get_testdata_file(name), src / name)
    return str(src)


def _session(tmp_path):
    session = DicomSession(str(tmp_path / "m.db"))
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(json.dumps({"phi_tags": REMOVE_RULES}))
    session.load_config(str(cfg))
    session.ingest(_sources(tmp_path))
    session.save(sync=True)
    return session


def _rows(session):
    session.store_backend.flush_audit_queue()
    with sqlite3.connect(session.store_backend.db_path) as conn:
        return list(conn.execute(
            "SELECT action_type, details FROM audit_log ORDER BY rowid"))


def _owner_rows(session, before):
    """The rows a pass wrote about the two owner fields under test, and
    every decline whatever it names.

    A decline names no field: `Remediation declined for 1CT1: no entity
    reference; the finding could not be resolved against the live graph`
    holds neither `patient_name` nor `study_date`, so a filter on those
    two read a pass that declined every finding as a pass with nothing
    to do -- and the mutant that drops the pseudonym lookups
    `test_a_saved_and_reopened_store_reads_its_owner_removals_as_done`
    exists to catch survived it (review of #661). Kept by action type,
    not by substring, for that reason.
    """
    return [(a, d) for a, d in _rows(session)[before:]
            if a == "REMEDIATION_DECLINED"
            or "patient_name" in (d or "") or "study_date" in (d or "")]


def _finding(entity, entity_type, uid, attr):
    return PhiFinding(
        entity_uid=uid, entity_type=entity_type, field_name=attr, value="x",
        reason="r", entity=entity,
        remediation_proposal=PhiRemediation(action_type="REMOVE_TAG",
                                            target_attr=attr,
                                            original_value="x"))


def test_a_reused_report_reads_its_own_owner_removals_as_done(tmp_path):
    """One report, handed over twice in one session: the second call has
    nothing to remove from the fields the first cleared."""
    with _session(tmp_path) as session:
        report = session.audit()
        assert session.anonymize(report) > 0
        before = len(_rows(session))

        session.anonymize(report)

        assert _owner_rows(session, before) == []
        assert all(p.phi_status is PhiStatus.REMEDIATED
                   for p in session.store.patients)


def test_a_saved_and_reopened_store_reads_its_owner_removals_as_done(tmp_path):
    """The issue's flow: the pass is saved, the store reopened, and the
    same report applied to the graph the first pass already cleaned. The
    patient's ID is this store's pseudonym by then, so the address is
    read under the pseudonym as `_live_findings` reads it (#644).

    The count is asserted, not just the rows: read under the pseudonym
    the four owner removals are satisfied and the twenty `REPLACE`s are
    re-applied, which is the CHANGELOG's 24 -> 20. Lose the pseudonym
    lookup and the pass does not become quiet -- it declines every
    finding, which `_owner_rows` now sees and this count would fail on
    either way.
    """
    with _session(tmp_path) as session:
        report = session.audit()
        session.anonymize(report)
        session.save(sync=True)
        assert all(str(p.patient_id).startswith("ANON_")
                   for p in session.store.patients)

    with DicomSession(str(tmp_path / "m.db")) as second:
        second.load_config(str(tmp_path / "cfg.yaml"))
        before = len(_rows(second))

        assert second.anonymize(report) == 20

        rows = _rows(second)[before:]
        assert [a for a, _ in rows].count("REMEDIATION_DECLINED") == 0, rows
        assert _owner_rows(second, before) == []


def test_an_owner_attribute_still_holding_a_value_is_removed(tmp_path):
    """The pin: a field that holds a value is removed, with its row."""
    with _session(tmp_path) as session:
        report = session.audit()
        before = len(_rows(session))

        session.anonymize(report)

        rows = _owner_rows(session, before)
        assert [a for a, _ in rows] == ["REMEDIATION_REMOVE"] * 4, rows
        assert [p.patient_name for p in session.store.patients] == [None, None]


def test_a_cleared_attribute_whose_instance_copies_survive_is_still_removed(tmp_path):
    """Where something is still there to write, write it: a field cleared
    by hand while the instances keep their own copies of the tag is not
    gone, and the removal still takes the copies away."""
    with _session(tmp_path) as session:
        report = session.audit()
        for patient in session.store.patients:
            patient.patient_name = None
        before = len(_rows(session))

        session.anonymize(report)

        rows = [d for a, d in _owner_rows(session, before)
                if "patient_name" in d]
        assert len(rows) == 2, rows
        assert all("removed from 1 instance copy" in d for d in rows), rows
        assert all("0010,0010" not in i.attributes
                   for p in session.store.patients for st in p.studies
                   for se in st.series for i in se.instances)


def test_a_study_whose_own_instances_still_hold_the_date_is_removed(tmp_path):
    """The Study half of the instance-copy walk.

    A `Study` has no `studies` attribute, so `_owner_field_gone` walks
    its own `series` through the fallback; a `Patient` finding never
    reaches that line, which is why the test above cannot see it change.
    Handed over **alone** -- not inside a report, whose instance-level
    `0008,0020` findings take the copies away themselves and leave both
    readings in the same graph state -- one `REMOVE_TAG` on a Study
    whose field was cleared by hand while its own instance still carries
    the tag is a real removal, and writes its row (review of #661).
    """
    with _session(tmp_path) as session:
        study = session.store.patients[0].studies[0]
        instances = [i for series in study.series for i in series.instances]
        assert len(instances) == 1, instances
        assert "0008,0020" in instances[0].attributes
        study.study_date = None
        finding = _finding(study, "Study", study.study_instance_uid,
                           "study_date")
        before = len(_rows(session))

        assert session.anonymize([finding]) == 1

        rows = _owner_rows(session, before)
        assert [a for a, _ in rows] == ["REMEDIATION_REMOVE"], rows
        assert "removed from 1 instance copy" in rows[0][1], rows
        assert "0008,0020" not in instances[0].attributes


def test_a_removal_filed_at_another_patients_address_declines(tmp_path):
    """Absence is read at the finding's address (#626, #644).

    The fixture is deliberately asymmetric: the patient the finding
    *addresses* still holds its name, and the patient the finding carries
    is the clean one. Read on the entity, that is a success row naming a
    patient nothing touched; read at the address, the absence is no
    evidence about the element and the removal declines.
    """
    with _session(tmp_path) as session:
        first, second = session.store.patients
        assert first.patient_name is not None
        second.patient_name = None
        for study in second.studies:
            for series in study.series:
                for instance in series.instances:
                    instance.attributes.pop("0010,0010", None)
        finding = _finding(second, "Patient", first.patient_id, "patient_name")
        before = len(_rows(session))

        assert session.anonymize([finding]) == 0

        rows = _rows(session)[before:]
        assert [a for a, _ in rows] == ["REMEDIATION_DECLINED"], rows
        assert STALE in rows[0][1]
        assert second.phi_status is not PhiStatus.REMEDIATED


def test_a_removal_filed_at_a_clean_patients_address_is_satisfied_there(tmp_path):
    """The other half of reading at the address: where the object at the
    finding's address is itself clean, the absence is evidence and the
    removal is satisfied -- no row, nothing applied -- as the instance
    path resolves a UID only one object holds (#626). Both patients are
    cleared here, which is what separates this from the decline above.
    """
    with _session(tmp_path) as session:
        first, second = session.store.patients
        for patient in (first, second):
            patient.patient_name = None
            for study in patient.studies:
                for series in study.series:
                    for instance in series.instances:
                        instance.attributes.pop("0010,0010", None)
        finding = _finding(second, "Patient", first.patient_id, "patient_name")
        before = len(_rows(session))

        assert session.anonymize([finding]) == 0

        assert _rows(session)[before:] == []
        assert second.phi_status is PhiStatus.REMEDIATED


def test_without_a_session_absence_is_read_on_the_entity():
    """A service used without a session has no graph to resolve an
    address in and reads the entity it was handed, as it always did."""
    gone = Patient(patient_id="P1", patient_name=None)
    finding = _finding(gone, "Patient", "P1", "patient_name")
    assert RemediationService().apply_remediation([finding]) == 0
    assert gone.phi_status is PhiStatus.REMEDIATED

    held = Patient(patient_id="P2", patient_name="DOE^JOHN")
    assert RemediationService().apply_remediation(
        [_finding(held, "Patient", "P2", "patient_name")]) == 1
    assert held.patient_name is None

    study = Study(study_instance_uid="1.2.3", study_date=None)
    assert RemediationService().apply_remediation(
        [_finding(study, "Study", "1.2.3", "study_date")]) == 0


def test_an_attribute_that_is_not_an_exported_field_is_untouched_by_the_rule(tmp_path):
    """The gate is `ENTITY_FIELD_TAGS`, as #626's is well-formed tags:
    absence under a name the export never writes is no evidence about an
    element. A hand-built REMOVE on such a name behaves exactly as it
    did -- the boundary of the rule, not an endorsement of the arm's
    looseness, which is #679.
    """
    with _session(tmp_path) as session:
        series = session.store.patients[0].studies[0].series[0]
        series.modality = None
        finding = _finding(series, "Series", series.series_instance_uid,
                           "modality")

        assert session.anonymize([finding]) == 1
