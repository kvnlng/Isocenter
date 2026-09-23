"""An instance's copy of an owner-stamped tag is written only through its
owner (#624).

The export stamps Patient's Name and Patient ID from the `Patient` and
Study Date from the `Study` (`io_handlers.export_stamp_attributes`, #492),
so an instance's top-level copy of one of them is never what the file
carries. When the owner's own remediation wrote it in the pass, the
instance finding folds into that write (#496). When the owner declined --
its value was cleared or edited after `audit()` -- or its finding was not
handed to `anonymize()`, the instance finding used to run on its own: it
rewrote the copy with a value the file does not carry and stamped the
instance REMEDIATED. Measured at b7462cd3 (L8 PR C premises): a Study
Date cleared after the audit left the instance's copy shifted while the
file carried `''`; one edited to 2020-02-02 left it shifted while the file
carried `20200202`; a pass handed only the instance findings left the
copies reading `ANONYMIZED` and a shifted date while the file carried the
source name, Patient ID and date.

Now such a finding declines with its own row, after the copy is set to
what the export writes (owner ruling, 2026-09-22, Q-C1: the graph copy
equals the file, even when that is the original identifier; Q-C2: an
owner holding None gives `''`, the empty element the file carries, not an
absent copy). The instance ends the pass IDENTIFIED through the decline
demotion. A nested copy is the instance scan's to judge and is untouched
(#496 N4). Exported files do not change.
"""
import datetime
import sqlite3

import pydicom
import pytest

from isocenter import Session
from isocenter.entities import PhiStatus

from support.ct_small_files import study_uid, write_ct

OWNED = ("0010,0010", "0010,0020", "0008,0020")
REASON = ("is written by the export from the {owner}, whose value this pass "
          "did not write; the instance's copy holds the value the export writes")


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _declines(session):
    session.store_backend.flush_audit_queue()
    with sqlite3.connect(session.persistence_file) as conn:
        return conn.execute("SELECT entity_uid, details FROM audit_log "
                            "WHERE action_type='REMEDIATION_DECLINED'").fetchall()


def _owner_declines(rows):
    """The #624 rows: `{tag: owner}` for each decline carrying the reason."""
    found = {}
    for _uid, details in rows:
        for owner in ("Study", "Patient"):
            if REASON.format(owner=owner) in details:
                tag = next(t for t in OWNED if t in details)
                assert tag not in found, rows
                found[tag] = owner
    return found


def _run(tmp_path, suffix, between, handed="all", nested=False):
    """CT_small with ID `PID-624`, audited; `between(patient, study)` runs
    before `anonymize()`; returns the instance, both owners, the rows and
    the exported dataset."""
    path = write_ct(tmp_path / "in" / "a.dcm", "PID-624", suffix, name="Alpha^One")
    if nested:
        ds = pydicom.dcmread(str(path))
        item = pydicom.Dataset()
        item.ReferencedSOPClassUID = ds.SOPClassUID
        item.ReferencedSOPInstanceUID = ds.SOPInstanceUID + ".9"
        item.StudyDate = ds.StudyDate
        ds.ReferencedImageSequence = pydicom.Sequence([item])
        ds.save_as(str(path))
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        report = session.audit()
        [patient] = session.store.patients
        [study] = patient.studies
        [inst] = [i for se in study.series for i in se.instances]
        between(patient, study)
        findings = (report if handed == "all" else
                    [f for f in report.findings if f.entity_type == "Instance"])
        session.anonymize(findings)
        rows = _declines(session)
        session.export(str(tmp_path / "out"), use_compression=False)
        session.generate_report(str(tmp_path / "r.md"))
        grade = (tmp_path / "r.md").read_text(encoding="utf-8")
    [out] = list((tmp_path / "out").rglob("*.dcm"))
    return inst, patient, study, rows, pydicom.dcmread(str(out)), grade


def _exported(ds, tag):
    return str(ds[tag.replace(",", "")].value) if tag.replace(",", "") in ds else None


def _clear_date(patient, study):
    study.study_date = None


def _new_date(patient, study):
    study.study_date = datetime.date(2020, 2, 2)


def _clear_name(patient, study):
    patient.patient_name = None


def _nothing(patient, study):
    pass


def test_a_study_date_cleared_after_the_audit_leaves_the_copy_empty(tmp_path):
    """T-C1, `study_none`. The Study declines ("no longer on the Study");
    the instance's copy was shifted by its own finding and read REMEDIATED
    while the file carried `''`. Now the copy is `''` exactly (Q-C2), the
    instance is IDENTIFIED, and its row names the Study. Kills M-C1 (the
    helper not called from `_shift_target_moved`)."""
    inst, _p, _s, rows, ds, _g = _run(tmp_path, "5624", _clear_date)
    assert inst.attributes["0008,0020"] == ""
    assert _exported(ds, "0008,0020") == ""
    assert inst.phi_status is PhiStatus.IDENTIFIED
    assert _owner_declines(rows) == {"0008,0020": "Study"}, rows


def test_a_study_date_edited_after_the_audit_is_the_copy_the_file_carries(tmp_path):
    """T-C2, `study_new`. The copy holds `20200202`, the unshifted date the
    export writes, not a shift of the audited date. Kills M-C2 (declines
    without syncing the copy)."""
    inst, _p, _s, rows, ds, _g = _run(tmp_path, "5625", _new_date)
    assert inst.attributes["0008,0020"] == "20200202" == _exported(ds, "0008,0020")
    assert inst.phi_status is PhiStatus.IDENTIFIED
    assert _owner_declines(rows) == {"0008,0020": "Study"}, rows


def test_a_patient_name_cleared_after_the_audit_leaves_the_copy_empty(tmp_path):
    """T-C3, `patient_name_cleared`. The copy read `ANONYMIZED` while the
    file carried `''`; now it is `''` exactly (Q-C2). Kills M-C3 (the
    helper not called from `_replace_on_item`)."""
    inst, _p, _s, rows, ds, _g = _run(tmp_path, "5626", _clear_name)
    assert inst.attributes["0010,0010"] == "" == _exported(ds, "0010,0010")
    assert inst.phi_status is PhiStatus.IDENTIFIED
    assert _owner_declines(rows) == {"0010,0010": "Patient"}, rows


def test_a_pass_given_only_the_instance_findings_leaves_the_copies_as_the_file(tmp_path):
    """T-C4, `instance_only_partial`. Every copy of the three tags equals
    the exported value -- the source name, Patient ID and date, since the
    owners were not in the pass (Q-C1: the graph copy equals the file) --
    no decline row, since no owner declined (Q-C5: the findings are left
    unhandled instead), and REVIEW_REQUIRED through #573's condition 7,
    never PASS. In 0.9.8 this graded PASS. Kills M-C4 ("owner not in the
    pass" read as folded) and the no-row sentinel read as a decline."""
    inst, _p, _s, rows, ds, grade = _run(tmp_path, "5627", _nothing, handed="instance")
    for tag in OWNED:
        assert inst.attributes[tag] == _exported(ds, tag), tag
    assert (_exported(ds, "0010,0010"), _exported(ds, "0010,0020")) == ("Alpha^One", "PID-624")
    assert inst.phi_status is PhiStatus.IDENTIFIED
    assert rows == []
    assert "**REVIEW_REQUIRED**" in grade and "**PASS**" not in grade


def test_a_full_pass_folds_and_declines_nothing(tmp_path):
    """T-C5 (control). The owners write, every instance finding folds as
    before (#496): no decline row, the copies equal the file, REMEDIATED,
    PASS. Kills M-C5 (declines when the owner did write)."""
    inst, _p, _s, rows, ds, grade = _run(tmp_path, "5628", _nothing)
    assert rows == []
    for tag in OWNED:
        assert inst.attributes[tag] == _exported(ds, tag), tag
    assert inst.phi_status is PhiStatus.REMEDIATED
    assert "**PASS**" in grade


def test_a_nested_copy_is_shifted_by_its_own_finding(tmp_path):
    """T-C6. `0008,1140[0] > 0008,0020` is the instance scan's to judge:
    the stamp reaches the dataset root only (#496 N4). With the owners not
    in the pass, the top-level copy is left to its owner (no row, Q-C5) and
    the nested one is still shifted by its own finding. Kills M-C6 (the
    helper applied to nested items): the nested copy would keep its source
    date."""
    source = str(pydicom.dcmread(str(write_ct(
        tmp_path / "src.dcm", "PID-624", "5629", name="Alpha^One"))).StudyDate)
    inst, _p, _s, rows, ds, _g = _run(tmp_path, "5629", _nothing,
                                      handed="instance", nested=True)
    [item] = inst.sequences["0008,1140"].items
    assert item.attributes["0008,0020"] not in (source, None, "")
    assert _exported(ds, "0008,0020") == source
    [nested] = ds.ReferencedImageSequence
    assert str(nested.StudyDate) == item.attributes["0008,0020"]
    assert rows == []


def test_a_report_from_another_store_declines_the_copy_under_the_owners_reason(tmp_path):
    """Q-C4 and the spec's attack point 7. Store B anonymizes with store
    A's report over the same file: B's Patient refuses A's pseudonym
    (#644), so the Patient's ID is not written, and the instance's
    0010,0020 REPLACE, carrying A's pseudonym too, does not fold. Its row
    carries the #624 reason -- the owner's reason wins for a stamped tag
    (coordinator ruling) -- and the copy holds B's Patient ID, which is
    what B's export writes."""
    write_ct(tmp_path / "in" / "a.dcm", "PID-624", "5630", name="Alpha^One")
    with Session(str(tmp_path / "a.db")) as store_a:
        store_a.ingest(str(tmp_path / "in"))
        report = store_a.audit()
    with Session(str(tmp_path / "b.db")) as store_b:
        store_b.ingest(str(tmp_path / "in"))
        [patient] = store_b.store.patients
        [inst] = [i for st in patient.studies for se in st.series for i in se.instances]
        store_b.anonymize(report)
        rows = _declines(store_b)
        store_b.export(str(tmp_path / "out"), use_compression=False)
    [out] = list((tmp_path / "out").rglob("*.dcm"))
    exported_id = str(pydicom.dcmread(str(out)).PatientID)
    assert inst.attributes["0010,0020"] == exported_id == patient.patient_id
    assert _owner_declines(rows).get("0010,0020") == "Patient", rows
    instance_rows = [d for uid, d in rows
                     if uid == study_uid("5630") + ".1.1" and "0010,0020" in d]
    assert len(instance_rows) == 1 and REASON.format(owner="Patient") in instance_rows[0], rows


def _session_with(tmp_path, suffix):
    write_ct(tmp_path / "in" / "a.dcm", "PID-624", suffix, name="Alpha^One")
    session = Session(str(tmp_path / "s.db"))
    session.ingest(str(tmp_path / "in"))
    report = session.audit()
    [patient] = session.store.patients
    [inst] = [i for st in patient.studies for se in st.series for i in se.instances]
    owners = [f for f in report.findings if f.entity_type in ("Patient", "Study")]
    instance = [f for f in report.findings if f.entity_type == "Instance"]
    return session, patient, inst, owners, instance


def test_the_owners_in_an_earlier_pass_leave_the_copies_satisfied(tmp_path):
    """The owners handed in one pass and the instance findings in the next
    (#553's complementary passes): the owners' writes already put the
    export's values on the copies, and a remediation vouches for each. So
    the instance findings on them are satisfied, not declined -- a decline
    would be REVIEW_REQUIRED over a graph equal to its file -- and the
    instance ends REMEDIATED. Kills M-C7 (the satisfied check dropped)."""
    session, _patient, inst, owners, instance = _session_with(tmp_path, "5631")
    with session:
        session.anonymize(owners)
        session.anonymize(instance)
        assert _owner_declines(_declines(session)) == {}
        assert inst.phi_status is PhiStatus.REMEDIATED


def test_a_restored_original_is_not_recorded_as_a_remediation(tmp_path):
    """The copy is set to the owner's value, and when that is the source
    value -- the owner not handed in -- it is not recorded as a
    remediation's output: the next lock would read the original as a
    replacement, and the next pass would read the copy as already
    remediated. Handed the instance findings twice, neither pass writes a
    row (Q-C5) and the instance stays IDENTIFIED. Kills M-C8 (the sync
    always recorded)."""
    session, _patient, inst, _owners, instance = _session_with(tmp_path, "5632")
    with session:
        session.anonymize(instance)
        assert not inst.remediation_vouches_for("0010,0020", "PID-624")
        assert not inst.remediation_vouches_for("0010,0010", "Alpha^One")
        session.anonymize(instance)
        assert not inst.remediation_vouches_for("0010,0020", "PID-624")
        assert inst.phi_status is PhiStatus.IDENTIFIED
        assert _declines(session) == []


def _graded(session, tmp_path):
    session.export(str(tmp_path / "out"), use_compression=False)
    session.generate_report(str(tmp_path / "r.md"))
    return (tmp_path / "r.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("then", ["the_instance_findings_again", "a_reaudit"])
def test_the_owners_in_a_later_pass_grade_pass(tmp_path, then):
    """Q-C5, the reverse split: instance findings first, owners in a later
    pass, then either the instance findings again or a re-audit and a pass
    over its report. Both grade PASS with no decline row. A row for the
    first pass's copies -- the owners were not handed in, so nothing
    declined -- would be counted by the grade forever and hold this at
    REVIEW_REQUIRED (it did, before the ruling). Handed again, the copies
    are satisfied by the owners' writes and the instance reads REMEDIATED;
    after the re-audit, which finds nothing, every entity reads CLEARED,
    the status a clean scan records."""
    session, patient, inst, owners, instance = _session_with(tmp_path, "5633")
    with session:
        session.anonymize(instance)
        assert inst.phi_status is PhiStatus.IDENTIFIED
        session.anonymize(owners)
        if then == "a_reaudit":
            report = session.audit()
            assert list(report.findings) == []
            session.anonymize(report)
            assert {e.phi_status for e in (inst, patient)} == {PhiStatus.CLEARED}
        else:
            session.anonymize(instance)
            assert inst.phi_status is PhiStatus.REMEDIATED
        assert _declines(session) == []
        grade = _graded(session, tmp_path)
    assert "**PASS**" in grade and "**REVIEW_REQUIRED**" not in grade, grade


def test_the_instances_alone_never_grade_pass(tmp_path):
    """Q-C5, the other half: the instance findings handed in, twice, and a
    re-audit, with the owners never handed in. No row is written, and the
    run grades REVIEW_REQUIRED, never PASS, because the owners' findings
    stay raised and unacted on (#573 condition 7)."""
    session, _patient, inst, _owners, instance = _session_with(tmp_path, "5634")
    with session:
        session.anonymize(instance)
        report = session.audit()
        session.anonymize([f for f in report.findings if f.entity_type == "Instance"])
        assert _declines(session) == []
        assert inst.phi_status is not PhiStatus.REMEDIATED
        grade = _graded(session, tmp_path)
    assert "**REVIEW_REQUIRED**" in grade and "**PASS**" not in grade, grade


@pytest.mark.parametrize("owner_handed", [False, True])
def test_a_sync_that_is_the_instances_only_write_keeps_its_status(tmp_path, owner_handed):
    """The Study Date edited after the audit and the pass handed only the
    instance's top-level Study Date finding (with or without the Study's):
    the sync to `20200202` is the only write the instance receives. The
    write moves its revision, and a status left at the old revision reads
    UNSCANNED, which the pass-end demotion does not take to IDENTIFIED --
    in the full-report tests another of the instance's findings stamps it
    afterwards and hides this. Kills M-C9 (the status not re-recorded
    across the sync)."""
    session, patient, inst, owners, instance = _session_with(tmp_path, "5691")
    with session:
        patient.studies[0].study_date = datetime.date(2020, 2, 2)
        only = [f for f in instance if f.tag == "0008,0020" and not f.entity_path]
        assert len(only) == 1
        study = [f for f in owners if f.entity_type == "Study"] if owner_handed else []
        session.anonymize(only + study)
        assert inst.attributes["0008,0020"] == "20200202"
        assert inst.phi_status is PhiStatus.IDENTIFIED
        assert _owner_declines(_declines(session)) == (
            {"0008,0020": "Study"} if owner_handed else {})


def test_a_copy_edited_after_the_audit_is_synced_back_without_a_record(tmp_path):
    """The instance's name copy edited after the audit, the owners not
    handed in: the sync writes the Patient's name back, the source value,
    and records no remediation for it -- a record would make the next
    `lock_identities()` stash the source name as a replacement, and the
    next pass read it as already remediated. Kills M-C8 (the sync always
    recorded): the full-report tests never sync a source value, because
    there the copy already holds it."""
    session, _patient, inst, _owners, instance = _session_with(tmp_path, "5692")
    with session:
        inst.set_attr("0010,0010", "Edited^Copy")
        session.anonymize(instance)
        assert inst.attributes["0010,0010"] == "Alpha^One"
        assert not inst.remediation_vouches_for("0010,0010", "Alpha^One")
        assert inst.phi_status is PhiStatus.IDENTIFIED


def test_a_study_date_no_study_holds_is_satisfied_by_the_empty_copy(tmp_path):
    """Q-C8, the fingerprint's `color-pl`/`ExplVR_BigEnd` shape: the source
    Study Date `1994.11.05` does not parse, so the Study holds none and
    raises no finding; only the instance's copy is raised. The export has
    always stamped it `''`. The copy is synced to `''`, nothing is left in
    the graph or the file, and the finding is satisfied: the file is
    written by the safe export, the instance reads REMEDIATED, no decline
    row, and the run grades PASS. Before #624 the instance's own shift
    declined on the date format and `check_burned_in=True` withheld a file
    that carried no date. Kills M-C17 (the empty copy left unhandled) and
    M-C18 (the sentinel written as a decline)."""
    path = write_ct(tmp_path / "in" / "a.dcm", "PID-624", "5693", name="Alpha^One")
    ds = pydicom.dcmread(str(path))
    ds.StudyDate = "1994.11.05"
    ds.save_as(str(path))
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        report = session.audit()
        [patient] = session.store.patients
        [study] = patient.studies
        [inst] = [i for se in study.series for i in se.instances]
        assert study.study_date is None
        assert [f.entity_type for f in report.findings if f.tag == "0008,0020"
                and not f.entity_path] == ["Instance"]
        session.anonymize(report)
        assert inst.attributes["0008,0020"] == ""
        assert inst.phi_status is PhiStatus.REMEDIATED
        assert _declines(session) == []
        session.export(str(tmp_path / "out"), use_compression=False, check_burned_in=True)
        session.generate_report(str(tmp_path / "r.md"))
        grade = (tmp_path / "r.md").read_text(encoding="utf-8")
    [out] = list((tmp_path / "out").rglob("*.dcm"))
    assert _exported(pydicom.dcmread(str(out)), "0008,0020") == ""
    assert "**PASS**" in grade and "**REVIEW_REQUIRED**" not in grade, grade
