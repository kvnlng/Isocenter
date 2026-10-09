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
                # A UID copy's row (#544) is `_uid_declines`'.
                tag = next((t for t in OWNED if t in details), None)
                if tag is None:
                    continue
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
    # The Series is an owner too since #544: the export stamps its Series
    # Instance UID, and its finding is borne by its instances.
    owners = [f for f in report.findings if f.entity_type in ("Patient", "Study", "Series")]
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
        # UNSCANNED, carrying the IDENTIFIED its scan left: the edit made
        # the status stale before the pass, and since #752 a pass keeps
        # such an entity stale rather than settling it. This line asserted
        # IDENTIFIED until then; either way the run cannot grade PASS.
        assert inst.phi_status is PhiStatus.UNSCANNED
        assert inst._phi_status is PhiStatus.IDENTIFIED


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


def test_a_copy_ingested_after_its_owners_wrote_is_synced_with_a_record(tmp_path):
    """Refinement 2's positive branch: the owners wrote in a full pass,
    then a second file of the same study is ingested carrying the source
    name, ID and date, and only its instance findings are handed in. Each
    copy is synced to the owner's value -- a remediation's output, which
    the first instance's record vouches for -- so the sync is recorded as
    one: the copy vouches for it, and the findings handed in again are
    satisfied (REMEDIATED). Unrecorded, the copy would read as an
    original, and every later hand-in would leave it unhandled. Kills MX1
    (the sibling rule never records)."""
    write_ct(tmp_path / "in1" / "a.dcm", "PID-624", "5694", name="Alpha^One")
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in1"))
        session.anonymize(session.audit())
        [patient] = [p for p in session.store.patients if p.studies]
        [study] = patient.studies
        ds = pydicom.dcmread(str(tmp_path / "in1" / "a.dcm"))
        ds.SOPInstanceUID = ds.SOPInstanceUID + ".7"
        (tmp_path / "in2").mkdir()
        ds.save_as(str(tmp_path / "in2" / "b.dcm"))
        session.ingest(str(tmp_path / "in2"))
        [late] = [i for se in study.series for i in se.instances
                  if i.sop_instance_uid.endswith(".7")]
        mine = [f for f in session.audit().findings if f.entity_type == "Instance"
                and f.entity_uid == late.sop_instance_uid]
        session.anonymize(mine)
        written = {"0010,0010": patient.patient_name,
                   "0010,0020": patient.patient_id,
                   "0008,0020": study.study_date.strftime("%Y%m%d")}
        for tag, value in written.items():
            assert late.attributes[tag] == value, tag
            assert late.remediation_vouches_for(tag, value), tag
        session.anonymize(mine)
        assert late.phi_status is PhiStatus.REMEDIATED
        assert _declines(session) == []


def test_a_date_is_recorded_only_on_the_word_of_its_own_study(tmp_path):
    """Refinement 2's scope for a Study Date: the Study, not the Patient.
    Study A's date is shifted in a full pass. Study B of the same patient
    is ingested afterwards carrying, as its *source* date, exactly A's
    shifted date; its instance copy is edited after the audit, and only
    its instance findings are handed in. The sync writes B's own value
    back -- a source date -- and A's instance, which vouches for the same
    string as a shift, must not make it read as a remediation. Kills MX6
    (the date's sibling scope widened to the whole patient)."""
    write_ct(tmp_path / "in1" / "a.dcm", "PID-624", "5695", name="Alpha^One")
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in1"))
        session.anonymize(session.audit())
        [patient] = [p for p in session.store.patients if p.studies]
        shifted = patient.studies[0].study_date.strftime("%Y%m%d")
        write_ct(tmp_path / "in2" / "b.dcm", patient.patient_id, "5696",
                 study_date=shifted, name=patient.patient_name)
        session.ingest(str(tmp_path / "in2"))
        [study_b] = [st for st in patient.studies
                     if st.study_instance_uid == study_uid("5696")]
        [late] = [i for se in study_b.series for i in se.instances]
        mine = [f for f in session.audit().findings if f.entity_type == "Instance"
                and f.entity_uid == late.sop_instance_uid]
        late.set_attr("0008,0020", "19990101")
        session.anonymize(mine)
        assert late.attributes["0008,0020"] == shifted
        assert not late.remediation_vouches_for("0008,0020", shifted)


# --- the owned UIDs (#544 over #624) -----------------------------------------
#
# Since #544 the Study and Series Instance UIDs are replaced, and the export
# stamps both from their owners (`0020,000d` from the Study, `0020,000e` from
# the Series). An instance's top-level copy of either follows its owner the
# way the three tags above do: never written as the replacement while the
# file carries the source, never stamped REMEDIATED over it.

OWNED_UIDS = {"0020,000d": "Study", "0020,000e": "Series"}


def _uid_declines(rows):
    """`{tag: owner}` for each #624 decline on an owned UID copy."""
    found = {}
    for _uid, details in rows:
        for tag, owner in OWNED_UIDS.items():
            if tag in details and REASON.format(owner=owner) in details:
                assert tag not in found, rows
                found[tag] = owner
    return found


def _everything_but(entity_type):
    def pick(report):
        return [f for f in report.findings if f.entity_type != entity_type]
    return pick


def _run_uids(tmp_path, suffix, between=_nothing, pick=None):
    path = write_ct(tmp_path / "in" / "a.dcm", "PID-624", suffix, name="Alpha^One")
    source = pydicom.dcmread(str(path))
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        report = session.audit()
        [patient] = session.store.patients
        [study] = patient.studies
        [inst] = [i for se in study.series for i in se.instances]
        between(patient, study)
        session.anonymize(report if pick is None else pick(report))
        rows = _declines(session)
        session.export(str(tmp_path / "out"), use_compression=False)
        session.generate_report(str(tmp_path / "r.md"))
        grade = (tmp_path / "r.md").read_text(encoding="utf-8")
    [out] = list((tmp_path / "out").rglob("*.dcm"))
    return inst, source, rows, pydicom.dcmread(str(out)), grade


def test_t_u1_a_report_from_another_store_leaves_the_uid_copies_as_the_file(tmp_path):
    """T-U1 (T-C1 and Q-C4 for UIDs). Store B handed store A's report over
    the same file: B's Study and Series refuse A's replacements (the UID
    analogue of #644), so the owners keep their source UIDs, and the
    instance's copies of both follow their refusing owners -- each holds
    what B's export writes, with one row carrying the owner's reason,
    which wins for a stamped copy (#624, Q-C4). No UID A minted reaches
    B's graph or file; the instance reads IDENTIFIED. Kills the UID tags
    missing from `_owner_stamps_copy`, and the owners' refusal."""
    from isocenter.privacy import _replacement_uid_for
    from support.project_secret import FIXED_A, FIXED_B, load_fixed_secret

    path = write_ct(tmp_path / "in" / "a.dcm", "PID-624", "5640", name="Alpha^One")
    src = pydicom.dcmread(str(path))
    with Session(str(tmp_path / "a.db")) as store_a:
        load_fixed_secret(store_a, secret=FIXED_A)
        store_a.ingest(str(tmp_path / "in"))
        report = store_a.audit()
    with Session(str(tmp_path / "b.db")) as store_b:
        load_fixed_secret(store_b, secret=FIXED_B)
        store_b.ingest(str(tmp_path / "in"))
        [inst] = [i for p in store_b.store.patients for st in p.studies
                  for se in st.series for i in se.instances]
        store_b.anonymize(report)
        rows = _declines(store_b)
        store_b.export(str(tmp_path / "out"), use_compression=False)
    [out] = list((tmp_path / "out").rglob("*.dcm"))
    ds = pydicom.dcmread(str(out))
    for tag, keyword in (("0020,000d", "StudyInstanceUID"),
                         ("0020,000e", "SeriesInstanceUID")):
        assert inst.attributes[tag] == _exported(ds, tag) == str(getattr(src, keyword)), tag
    assert _uid_declines(rows) == OWNED_UIDS, rows
    minted_by_a = {_replacement_uid_for(str(getattr(src, k)), FIXED_A)
                   for k in ("SOPInstanceUID", "StudyInstanceUID", "SeriesInstanceUID",
                             "FrameOfReferenceUID")}
    assert not [el for el in ds.iterall() if str(el.value) in minted_by_a]
    assert inst.phi_status is PhiStatus.IDENTIFIED


def test_t_u2_a_pass_given_only_the_instance_findings_leaves_the_uid_copies_as_the_file(
        tmp_path):
    """T-U2 (T-C4). Only the instance findings: the Study and Series keep
    their source UIDs, which the file carries, and the copies equal them
    -- not the replacements a copy-only write left beside a file carrying
    the source -- with no row (the owners were not handed in, Q-C5), the
    instance IDENTIFIED, and REVIEW_REQUIRED. The instance's own SOP
    Instance UID is still replaced: it is not stamped from an owner."""
    inst, src, rows, ds, grade = _run_uids(tmp_path, "5641",
                                           pick=_everything_but_owners)
    for tag, keyword in (("0020,000d", "StudyInstanceUID"),
                         ("0020,000e", "SeriesInstanceUID")):
        assert inst.attributes[tag] == _exported(ds, tag) == str(getattr(src, keyword)), tag
    assert str(ds.SOPInstanceUID) != str(src.SOPInstanceUID)
    assert inst.phi_status is PhiStatus.IDENTIFIED
    assert rows == []
    assert "**REVIEW_REQUIRED**" in grade and "**PASS**" not in grade


def _everything_but_owners(report):
    return [f for f in report.findings if f.entity_type == "Instance"]


def test_t_u3_a_series_not_handed_in_costs_pass(tmp_path):
    """T-U3. Patient, Study and instance findings handed in, the Series'
    not: the file carries the source Series Instance UID, the instance's
    copy equals it (no row, Q-C5), and the run grades REVIEW_REQUIRED --
    the Series' instances bear its finding (review of #544, finding 2)."""
    inst, src, rows, ds, grade = _run_uids(tmp_path, "5642",
                                           pick=_everything_but("Series"))
    assert inst.attributes["0020,000e"] == _exported(ds, "0020,000e") == str(src.SeriesInstanceUID)
    assert _exported(ds, "0020,000d") != str(src.StudyInstanceUID)
    assert inst.phi_status is PhiStatus.IDENTIFIED
    assert rows == []
    assert "**REVIEW_REQUIRED**" in grade and "**PASS**" not in grade


def test_t_u4_a_full_pass_folds_the_uid_copies(tmp_path):
    """T-U4 (T-C5, control). The owners write their replacements and every
    instance copy folds: no row, copies equal the file, REMEDIATED, PASS."""
    inst, src, rows, ds, grade = _run_uids(tmp_path, "5643")
    assert rows == []
    for tag in OWNED_UIDS:
        assert inst.attributes[tag] == _exported(ds, tag), tag
    assert _exported(ds, "0020,000e") != str(src.SeriesInstanceUID)
    assert inst.phi_status is PhiStatus.REMEDIATED
    assert "**PASS**" in grade


# --- REMOVE on an owner-stamped copy (#764, the REMOVE half of #624) ---------
#
# #624 covered REPLACE and SHIFT. An instance REMOVE on such a copy still ran
# on its own: it removed the copy, wrote `REMEDIATION_REMOVE Removed Tag ...`
# and stamped the instance REMEDIATED, while the export went on stamping the
# owner's value into the file. Measured at de5b26d9 and a7f8aeb9.
#
# **Every session test here but the last two runs `privacy_profile: none`
# plus the one REMOVE rule** (the no-session control has no policy). Under the floor the same pass leaves the instance
# IDENTIFIED on main too, for another reason (the floor also raises Patient ID
# and the two UIDs on the instance, #624 leaves those unhandled, and the tally
# demotes it), so a floor test of the status passes with the fix deleted. And
# every test asserts the copy's *value* against the exported value, not only
# a status.
#
# The last two run under the floor because they need its owner REPLACE:
# `test_a_remove_on_a_copy_ingested_after_its_owner_wrote_is_left_to_the_owner`
# and `test_an_instance_remove_beside_an_owner_replace_declines`. In both, the
# row, the value and the vouch record carry the test; their IDENTIFIED
# assertion alone would pass with the fix deleted, for the reason above.
#
# Owner rulings (2026-10-06): Q1 A, an owner holding no value leaves the copy
# `''`, as Q-C2 does; Q2 A, an instance REMOVE beside an owner REPLACE on the
# same tag declines with a row. Q1 A was reversed on 2026-10-08 (Q-D1-1 A,
# #948): under an owner holding no value the copy is removed, with its row,
# because the scan raises a REMOVE on anything present and the `''` copy was
# raised again by every audit.

REMOVE_REASON = (
    "{tag} is written by the export from the {owner}, which still holds a "
    "value, so removing the instance's copy would not remove it from the "
    "file; the copy holds the value the export writes")
NAME_RULE = "  '0010,0010': {action: REMOVE, name: Name}\n"
DATE_RULE = "  '0008,0020': {action: REMOVE, name: Date}\n"
#: `(rule, tag, owner type, owner field, CT_small's source value)`.
REMOVED = {
    "name": (NAME_RULE, "0010,0010", "Patient", "patient_name", "Alpha^One"),
    "date": (DATE_RULE, "0008,0020", "Study", "study_date", "20040119"),
}


def _all_rows(session):
    session.store_backend.flush_audit_queue()
    with sqlite3.connect(session.persistence_file) as conn:
        return conn.execute(
            "SELECT action_type, entity_uid, details FROM audit_log").fetchall()


def _remove_session(tmp_path, suffix, rules, nested=False, source=None):
    """CT_small as `PID-624`/`Alpha^One` under `privacy_profile: none` plus
    `rules`, audited. `source`, when given, edits the dataset before it is
    ingested. Returns the open session, the report, and the patient, study
    and instance."""
    path = write_ct(tmp_path / "in" / "a.dcm", "PID-624", suffix, name="Alpha^One")
    if source is not None:
        ds = pydicom.dcmread(str(path))
        source(ds)
        ds.save_as(str(path))
    if nested:
        ds = pydicom.dcmread(str(path))
        item = pydicom.Dataset()
        item.ReferencedSOPClassUID = ds.SOPClassUID
        item.ReferencedSOPInstanceUID = ds.SOPInstanceUID + ".9"
        item.StudyDate = ds.StudyDate
        ds.ReferencedImageSequence = pydicom.Sequence([item])
        ds.save_as(str(path))
    config = tmp_path / "c.yaml"
    config.write_text("privacy_profile: none\nphi_tags:\n" + rules, encoding="utf-8")
    session = Session(str(tmp_path / "s.db"))
    session.load_config(str(config))
    session.ingest(str(tmp_path / "in"))
    report = session.audit()
    [patient] = session.store.patients
    [study] = patient.studies
    [inst] = [i for se in study.series for i in se.instances]
    return session, report, patient, study, inst


def _instance_findings(report):
    return [f for f in report.findings if f.entity_type == "Instance"]


def _owner_findings(report):
    return [f for f in report.findings if f.entity_type != "Instance"]


def _exported_file(session, tmp_path, name="out"):
    session.export(str(tmp_path / name), use_compression=False)
    [out] = list((tmp_path / name).rglob("*.dcm"))
    return pydicom.dcmread(str(out))


def _grade_of(session, tmp_path):
    session.generate_report(str(tmp_path / "r.md"))
    text = (tmp_path / "r.md").read_text(encoding="utf-8")
    return [g for g in ("PASS", "REVIEW_REQUIRED", "FAIL") if f"**{g}**" in text]


def _removal_rows(rows, inst, tag):
    """The `REMEDIATION_REMOVE` rows that say `tag` was removed from the
    instance itself."""
    return [details for action, uid, details in rows
            if action == "REMEDIATION_REMOVE" and uid == inst.sop_instance_uid
            and tag in details]


def _declined(rows):
    return [(uid, details) for action, uid, details in rows
            if action == "REMEDIATION_DECLINED"]


@pytest.mark.parametrize("which", ["name", "date"])
def test_a_remove_given_only_the_instance_findings_leaves_the_copy_as_the_file(
        tmp_path, which):
    """The owner not handed in: the copy stays, holding what the file
    carries (Q-C1), no row is written (Q-C5), and the instance reads
    IDENTIFIED through the tally. On main the copy was removed, the row
    said `Removed Tag`, the instance read REMEDIATED and the file carried
    the source value. Handed the same findings again, nothing moves."""
    rules, tag, _owner, _field, source = REMOVED[which]
    session, report, _patient, _study, inst = _remove_session(tmp_path, "7641", rules)
    with session:
        sop = inst.sop_instance_uid
        session.anonymize(_instance_findings(report))
        once = (dict(inst.attributes), inst.phi_status, _all_rows(session))
        assert inst.attributes[tag] == source
        assert inst.phi_status is PhiStatus.IDENTIFIED
        assert _declined(once[2]) == []
        assert _removal_rows(once[2], inst, tag) == []
        assert not inst.remediation_vouches_for(tag, source)
        session.anonymize(_instance_findings(report))
        assert (dict(inst.attributes), inst.phi_status, _all_rows(session)) == once
        assert inst.sop_instance_uid == sop
        assert _exported(_exported_file(session, tmp_path), tag) == source
        assert _grade_of(session, tmp_path) == ["REVIEW_REQUIRED"]


@pytest.mark.parametrize("which", ["name", "date"])
def test_a_remove_whose_owner_declined_writes_one_row_and_keeps_the_copy(
        tmp_path, which):
    """The owner's finding handed in and declined (it names an entity the
    graph does not hold): the instance's REMOVE declines with its own row,
    asserted whole, and never with `matched no applicable arm`. On main
    there was no row for the instance and it read REMEDIATED."""
    import copy

    rules, tag, owner, _field, source = REMOVED[which]
    session, report, _patient, _study, inst = _remove_session(tmp_path, "7642", rules)
    with session:
        handed = []
        for finding in report.findings:
            if finding.entity_type == owner:
                finding = copy.copy(finding)
                finding.entity, finding.entity_uid = None, "NO-SUCH-ENTITY"
            handed.append(finding)
        session.anonymize(handed)
        rows = _all_rows(session)
        mine = [details for uid, details in _declined(rows)
                if uid == inst.sop_instance_uid]
        assert mine == [f"Remediation declined for {inst.sop_instance_uid}: "
                        + REMOVE_REASON.format(tag=tag, owner=owner)]
        assert not [d for _uid, d in _declined(rows) if "matched no applicable arm" in d]
        assert _removal_rows(rows, inst, tag) == []
        assert inst.attributes[tag] == source
        assert inst.phi_status is PhiStatus.IDENTIFIED
        assert _exported(_exported_file(session, tmp_path), tag) == source
        assert _grade_of(session, tmp_path) == ["REVIEW_REQUIRED"]


def test_an_absent_copy_is_not_read_as_removed_while_the_owner_holds_a_value(tmp_path):
    """A report kept from the first audit. The owners' pass removes the
    copy and clears the Patient; the name is then set back on the Patient
    by hand; then **every** instance finding of the first report is handed
    in. The copy is not re-created (#624's rule), and its absence says
    nothing about the file, which carries the name: the instance reads
    IDENTIFIED, with no row. On main the absence was read as the REMOVE's
    end state and the instance read REMEDIATED.

    Two things keep this honest. The second pass is handed every instance
    finding: handed the one alone, the instance is IDENTIFIED on main too,
    over the rest. And only the instance is asserted: the hand edit leaves
    the Patient UNSCANNED (#767) on both trees."""
    rules, tag, _owner, _field, source = REMOVED["name"]
    session, report, patient, _study, inst = _remove_session(tmp_path, "7643", rules)
    with session:
        session.anonymize(_owner_findings(report))
        assert tag not in inst.attributes and patient.patient_name is None
        patient.patient_name = source
        before = _all_rows(session)
        session.anonymize(_instance_findings(report))
        assert tag not in inst.attributes
        assert inst.phi_status is PhiStatus.IDENTIFIED
        new = _all_rows(session)[len(before):]
        assert not [row for row in new if tag in row[2]], new
        assert _exported(_exported_file(session, tmp_path), tag) == source


@pytest.mark.parametrize("which, empty", [("name", None), ("name", ""), ("date", None)],
                         ids=["name-none", "name-blank", "date-none"])
def test_a_remove_under_an_owner_holding_no_value_removes_the_copy(
        tmp_path, which, empty):
    """Q-D1-1 A (#948), which reverses C3's Q1 A for this case. The owner
    cleared by hand and not handed in: the export writes the element empty
    whatever the copy holds, so removing the copy is true of the file, and
    the arm removes it: absent, one `REMEDIATION_REMOVE` row, REMEDIATED,
    something to save. Absent after a save and a reopen, and a re-audit
    raises nothing on the tag. Under Q1 A the copy was set to `''` with no
    row, which every later audit raised again (a REMOVE is raised on
    anything present). Kills an owner holding None read as "not handed in"
    (the copy would keep the source value), and, in the `name-blank` cell,
    a predicate that reads only None as no value: a Patient's Name of `''`
    is what ingest gives a source with an empty one. (A Study holds a date
    or None, so the date has no such cell.)"""
    rules, tag, _owner, field, _source = REMOVED[which]
    session, report, patient, study, inst = _remove_session(tmp_path, "7644", rules)
    with session:
        owner = patient if which == "name" else study
        setattr(owner, field, empty)
        assert session.anonymize(_instance_findings(report)) == len(
            _instance_findings(report))
        rows = _all_rows(session)
        assert tag not in inst.attributes
        assert inst.phi_status is PhiStatus.REMEDIATED
        assert inst.has_unsaved_changes
        assert _declined(rows) == []
        assert _removal_rows(rows, inst, tag) == [
            f"Removed Tag {tag} from {inst.sop_instance_uid}"]
        assert _exported(_exported_file(session, tmp_path), tag) == ""
        session.save(sync=True)
    with Session(str(tmp_path / "s.db")) as reopened:
        reopened.load_config(str(tmp_path / "c.yaml"))
        [again] = [i for p in reopened.store.patients for st in p.studies
                   for se in st.series for i in se.instances]
        assert tag not in again.attributes
        # The instance's, not the owner's: a Patient holding `''` raises
        # its own REMOVE (the `name-blank` cell), which is not this copy.
        assert [f for f in reopened.audit().findings
                if f.entity_type == "Instance"
                and tag in (f.tag, f.remediation_proposal.target_attr)] == []


def _blank_date(ds):
    ds.StudyDate = ""


def _dotted_date(ds):
    # The ACR-NEMA spelling: present, not empty, and no Study holds it.
    ds[0x00080020] = pydicom.DataElement(
        0x00080020, "DA", "1994.11.05", validation_mode=pydicom.config.IGNORE)


def _on_the_date(findings):
    return [(f.entity_type, f.remediation_proposal.action_type) for f in findings
            if f.remediation_proposal.target_attr in ("0008,0020", "study_date")]


@pytest.mark.parametrize("source, held", [(_blank_date, ""), (_dotted_date, "1994.11.05")],
                         ids=["empty", "dotted"])
def test_a_source_date_no_study_holds_settles_under_remove_in_one_pass(
        tmp_path, source, held):
    """#948, with no hand edit: a regression of #958 shipped in 1.0.0rc14
    and rc15. A source whose Study Date is present and empty, or spelled
    `1994.11.05`, gives a Study holding no date and an instance copy
    holding the source's text. Under `0008,0020: REMOVE`, whole reports
    only: one pass removes the copy with its `REMEDIATION_REMOVE` row and
    counts it, and the next audit raises nothing, reads the instance
    CLEARED and grades PASS. In rc14 and rc15 the copy was set to `''`
    (the dotted date blanked with no row and not counted), and every later
    audit raised the same instance finding: IDENTIFIED, REVIEW_REQUIRED.

    Under `privacy_profile: none` on purpose: under the floor the instance
    is IDENTIFIED for other findings on round one. Three rounds, so a fix
    that settles on round two only is red: no row is added after the
    first, and every later audit raises nothing on the date."""
    session, report, _patient, study, inst = _remove_session(
        tmp_path, "9481", DATE_RULE, source=source)
    with session:
        assert study.study_date is None and inst.attributes["0008,0020"] == held
        assert _on_the_date(report.findings) == [("Instance", "REMOVE_TAG")]
        assert session.anonymize(report) == len(report.findings)
        rows = _all_rows(session)
        assert "0008,0020" not in inst.attributes
        assert _removal_rows(rows, inst, "0008,0020") == [
            f"Removed Tag 0008,0020 from {inst.sop_instance_uid}"]
        assert _declined(rows) == []
        for _round in (2, 3):
            again = session.audit()
            assert _on_the_date(again.findings) == []
            assert inst.phi_status is PhiStatus.CLEARED
            assert _grade_of(session, tmp_path) == ["PASS"]
            session.anonymize(again)
            assert "0008,0020" not in inst.attributes
            assert _removal_rows(_all_rows(session), inst, "0008,0020") == [
                f"Removed Tag 0008,0020 from {inst.sop_instance_uid}"]
        assert _exported(_exported_file(session, tmp_path), "0008,0020") == ""
        last = session.audit()
        assert _on_the_date(last.findings) == []
        assert inst.phi_status is PhiStatus.CLEARED
        assert _grade_of(session, tmp_path) == ["PASS"]


def _blank_name(ds):
    ds.PatientName = ""


@pytest.mark.parametrize("rules, source, tag", [
    (NAME_RULE, _blank_name, "0010,0010"),
    ("  '0008,0020': {action: JITTER, name: Date}\n", _blank_date, "0008,0020"),
], ids=["an-empty-name-under-remove", "an-empty-date-under-jitter"])
def test_the_empty_sources_that_always_settled_still_do(tmp_path, rules, source, tag):
    """Controls for #948, green before it. An empty Patient's Name is held
    by the Patient as `''`, not None, so the owner's own REMOVE is raised
    and takes the copies; a JITTER on an empty date raises nothing (the
    scan's shift arm skips a blank). Both settle: the audit after one pass
    raises nothing on the tag and grades PASS."""
    session, report, _patient, _study, inst = _remove_session(
        tmp_path, "9482", rules, source=source)
    with session:
        session.anonymize(report)
        again = session.audit()
        assert [f for f in again.findings
                if tag in (f.tag, f.remediation_proposal.target_attr)] == []
        assert inst.phi_status is PhiStatus.CLEARED
        assert _grade_of(session, tmp_path) == ["PASS"]


@pytest.mark.parametrize("which", ["name", "date"])
def test_the_owners_removal_in_an_earlier_pass_satisfies_the_instances(
        tmp_path, which):
    """The owners first: their removal takes the copy, and the instance
    findings in the next pass find nothing left in the graph or the file.
    No row, REMEDIATED, PASS."""
    rules, tag, _owner, _field, _source = REMOVED[which]
    session, report, _patient, _study, inst = _remove_session(tmp_path, "7645", rules)
    with session:
        session.anonymize(_owner_findings(report))
        session.anonymize(_instance_findings(report))
        assert tag not in inst.attributes
        assert inst.phi_status is PhiStatus.REMEDIATED
        assert _declined(_all_rows(session)) == []
        assert _exported(_exported_file(session, tmp_path), tag) == ""
        assert _grade_of(session, tmp_path) == ["PASS"]


@pytest.mark.parametrize("which", ["name", "date"])
def test_the_owners_removal_in_a_later_pass_needs_a_reaudit(tmp_path, which):
    """Q-C5's consequence, now for REMOVE as for REPLACE: instance findings
    first, owners later. The owners' pass removes the copy, and the
    instance stays IDENTIFIED until a re-audit, which clears it; no row is
    ever written, so the run then grades PASS. On main the instance read
    REMEDIATED after the first pass. Kills a row written for the
    not-handed case, which would hold the grade at REVIEW_REQUIRED for
    good."""
    rules, tag, _owner, _field, _source = REMOVED[which]
    session, report, patient, _study, inst = _remove_session(tmp_path, "7646", rules)
    with session:
        session.anonymize(_instance_findings(report))
        assert inst.phi_status is PhiStatus.IDENTIFIED
        session.anonymize(_owner_findings(report))
        assert tag not in inst.attributes
        assert inst.phi_status is PhiStatus.IDENTIFIED
        assert _declined(_all_rows(session)) == []
        session.anonymize(session.audit())
        assert {inst.phi_status, patient.phi_status} == {PhiStatus.CLEARED}
        assert _declined(_all_rows(session)) == []
        assert _exported(_exported_file(session, tmp_path), tag) == ""
        assert _grade_of(session, tmp_path) == ["PASS"]


@pytest.mark.parametrize("times", [1, 2], ids=["once", "twice"])
@pytest.mark.parametrize("which", ["name", "date"])
def test_a_full_report_folds_the_remove_and_declines_nothing(tmp_path, which, times):
    """Control. With the whole report the owner's removal reaches the copy
    and the instance finding folds into it, as before: the owner's row
    names the fold, the copy is gone, PASS. Handed the same report a
    second time, the removal is already there: no row names the tag or
    the field, and it is still PASS (#567). Kills a decline written when
    the owner holds no value."""
    rules, tag, _owner, field, _source = REMOVED[which]
    session, report, _patient, _study, inst = _remove_session(tmp_path, "7647", rules)
    with session:
        session.anonymize(report)
        rows = _all_rows(session)
        [owners] = [d for a, _u, d in rows
                    if a == "REMEDIATION_REMOVE" and f"Cleared Attribute {field} " in d]
        assert owners.endswith("removed from 1 instance copy; "
                               "1 instance-level finding on this tag folded into it")
        if times == 2:
            session.anonymize(report)
            again = _all_rows(session)[len(rows):]
            assert not [row for row in again if tag in row[2] or field in row[2]], again
        assert tag not in inst.attributes
        assert inst.phi_status is PhiStatus.REMEDIATED
        assert _declined(_all_rows(session)) == []
        assert _exported(_exported_file(session, tmp_path), tag) == ""
        assert _grade_of(session, tmp_path) == ["PASS"]


def test_a_nested_copy_is_removed_by_its_own_finding(tmp_path):
    """Control. `0008,1140[0] > 0008,0020` is not stamped from the Study:
    with the owners not handed in the top-level copy stays and the nested
    one is removed by its own finding. Kills a gate keyed on the tag
    alone."""
    session, report, _patient, _study, inst = _remove_session(
        tmp_path, "7648", DATE_RULE, nested=True)
    with session:
        [item] = inst.sequences["0008,1140"].items
        assert item.attributes["0008,0020"] == "20040119"
        session.anonymize(_instance_findings(report))
        assert "0008,0020" not in item.attributes
        assert inst.attributes["0008,0020"] == "20040119"
        ds = _exported_file(session, tmp_path)
        [nested] = ds.ReferencedImageSequence
        assert "StudyDate" not in nested
        assert _exported(ds, "0008,0020") == "20040119"


def test_a_remove_on_a_tag_no_owner_stamps_still_removes_it(tmp_path):
    """Control. Institution Name beside the name rule, in the same pass
    and on the same instance: removed, with its `REMEDIATION_REMOVE` row.
    Kills a gate that is true of every tag."""
    session, report, _patient, _study, inst = _remove_session(
        tmp_path, "7649", NAME_RULE + "  '0008,0080': {action: REMOVE, name: Inst}\n")
    with session:
        assert inst.attributes["0008,0080"]
        session.anonymize(_instance_findings(report))
        assert "0008,0080" not in inst.attributes
        assert _removal_rows(_all_rows(session), inst, "0008,0080") == [
            f"Removed Tag 0008,0080 from {inst.sop_instance_uid}"]
        assert inst.attributes["0010,0010"] == "Alpha^One"
        assert "InstitutionName" not in _exported_file(session, tmp_path)


def test_without_a_session_a_remove_on_the_name_still_removes_it():
    """Control. A service with no session has no owners to stamp from
    (`write_tree()`'s case, and the direct tests'): the removal runs.
    Kills a gate that does not read the session's owners."""
    from isocenter.entities import Instance
    from isocenter.privacy import PhiFinding, PhiRemediation
    from isocenter.remediation import RemediationService

    inst = Instance("1.2.3.764", "1.2.840.10008.5.1.4.1.1.2", 1)
    inst.set_attr("0010,0010", "Alpha^One")
    finding = PhiFinding(
        entity_uid=inst.sop_instance_uid, entity_type="Instance",
        field_name="0010,0010", value="Alpha^One", reason="test", tag="0010,0010",
        entity=inst, remediation_proposal=PhiRemediation(
            action_type="REMOVE_TAG", target_attr="0010,0010", metadata={}))
    assert RemediationService().apply_remediation([finding]) == 1
    assert "0010,0010" not in inst.attributes
    assert inst.phi_status is PhiStatus.REMEDIATED


def test_a_remove_on_a_copy_ingested_after_its_owner_wrote_is_left_to_the_owner(tmp_path):
    """#894's shape under a REMOVE. The owners wrote in a full pass under
    the floor; a second file of the study is then ingested carrying the
    source name, and its instance findings are handed in with the name's
    turned into a REMOVE. The copy is synced to `ANONYMIZED`, which its
    sibling vouches for -- the end state a REPLACE reads as already there
    -- and for a REMOVE that is still a file carrying the value: the
    finding is left to the owner, which this pass was not handed, with no
    row. Kills the vouched sync read as satisfied for a removal, which
    wrote a `REMEDIATION_DECLINED` row with an empty reason."""
    import copy

    from isocenter.privacy import PhiRemediation

    write_ct(tmp_path / "in1" / "a.dcm", "PID-624", "7651", name="Alpha^One")
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in1"))
        session.anonymize(session.audit())
        [patient] = [p for p in session.store.patients if p.studies]
        [study] = patient.studies
        ds = pydicom.dcmread(str(tmp_path / "in1" / "a.dcm"))
        ds.SOPInstanceUID = ds.SOPInstanceUID + ".7"
        (tmp_path / "in2").mkdir()
        ds.save_as(str(tmp_path / "in2" / "b.dcm"))
        session.ingest(str(tmp_path / "in2"))
        [late] = [i for se in study.series for i in se.instances
                  if i.sop_instance_uid.endswith(".7")]
        assert late.attributes["0010,0010"] == "Alpha^One"
        mine, swapped = [], 0
        for finding in session.audit().findings:
            if (finding.entity_type != "Instance"
                    or finding.entity_uid != late.sop_instance_uid):
                continue
            proposal = finding.remediation_proposal
            if not finding.entity_path and proposal.target_attr == "0010,0010":
                finding = copy.copy(finding)
                finding.remediation_proposal = PhiRemediation(
                    "REMOVE_TAG", "0010,0010", original_value=proposal.original_value)
                swapped += 1
            mine.append(finding)
        assert swapped == 1
        before = _all_rows(session)
        session.anonymize(mine)
        new = _all_rows(session)[len(before):]
        assert late.attributes["0010,0010"] == "ANONYMIZED" == patient.patient_name
        assert late.remediation_vouches_for("0010,0010", "ANONYMIZED")
        assert _declined(new) == []
        assert _removal_rows(new, late, "0010,0010") == []
        assert late.phi_status is PhiStatus.IDENTIFIED


def test_an_instance_remove_beside_an_owner_replace_declines(tmp_path):
    """Q2 A. A hand-built list under the floor: the Patient's REPLACE
    writes `ANONYMIZED` onto the copy, and the instance's finding on the
    same copy is a REMOVE. The file carries `ANONYMIZED`, so the REMOVE
    declines with its row, the copy keeps the owner's value, and the run
    grades REVIEW_REQUIRED. On main the copy was removed, the instance
    read REMEDIATED and the run graded PASS beside a file carrying the
    value. Kills the owner's vouched value read as the REMOVE's end
    state."""
    import copy

    from isocenter.privacy import PhiRemediation

    write_ct(tmp_path / "in" / "a.dcm", "PID-624", "7650", name="Alpha^One")
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        report = session.audit()
        [inst] = [i for p in session.store.patients for st in p.studies
                  for se in st.series for i in se.instances]
        sop = inst.sop_instance_uid
        handed, swapped = [], 0
        for finding in report.findings:
            proposal = finding.remediation_proposal
            if (finding.entity_type == "Instance" and not finding.entity_path
                    and proposal and proposal.target_attr == "0010,0010"):
                finding = copy.copy(finding)
                finding.remediation_proposal = PhiRemediation(
                    "REMOVE_TAG", "0010,0010", original_value=proposal.original_value)
                swapped += 1
            handed.append(finding)
        assert swapped == 1
        session.anonymize(handed)
        rows = _all_rows(session)
        assert inst.attributes["0010,0010"] == "ANONYMIZED"
        # The instance's row is filed under the UID the scan saw.
        assert [d for _uid, d in _declined(rows) if "0010,0010" in d] == [
            f"Remediation declined for {sop}: "
            + REMOVE_REASON.format(tag="0010,0010", owner="Patient")]
        assert inst.phi_status is PhiStatus.IDENTIFIED
        assert _exported(_exported_file(session, tmp_path), "0010,0010") == "ANONYMIZED"
        assert _grade_of(session, tmp_path) == ["REVIEW_REQUIRED"]


# -- what the exported file carries while the instance reads IDENTIFIED ------
#
# The 1.0.0rc14 `**Output:**` line for #764 said "none. No exported byte
# changes". The owner's value in the file is unchanged, but the three
# de-identification markers go only on an instance whose own status is
# REMEDIATED or CLEARED, so the two sequences #764 leaves IDENTIFIED export a
# file without them where they exported one with them. Measured against
# v1.0.0rc13 on 3.12 and 3.14t; the tests below read the exported files.

MARKERS = ((0x0012, 0x0062), (0x0012, 0x0063), (0x0028, 0x0303))


def _markers_in_both_exports(session, tmp_path, name):
    """Which of `(0012,0062)`, `(0012,0063)`, `(0028,0303)` the one exported
    file carries, from the Implicit VR export and from the JPEG 2000 one,
    which names its VRs on the wire. Also the files."""
    present, files = [], []
    for arm, compress in (("native", False), ("j2k", True)):
        folder = tmp_path / f"{name}-{arm}"
        session.export(str(folder), use_compression=compress)
        [path] = list(folder.rglob("*.dcm"))
        ds = pydicom.dcmread(str(path))
        files.append(ds)
        present.append([tag in ds for tag in MARKERS])
    return present, files


@pytest.mark.parametrize("paired", [True, False], ids=["pairing", "whole-report"])
def test_the_replace_and_remove_pairing_exports_no_marker(tmp_path, paired):
    """Q2 A's pairing under the floor: the file carries `ANONYMIZED` either
    way. With the hand-built instance REMOVE the instance reads IDENTIFIED,
    so the file carries none of the three markers and the run grades
    REVIEW_REQUIRED; at v1.0.0rc13 it carried all three and graded PASS.
    The whole report, unpaired, is the control: all three, and PASS. Kills
    the REMOVE running on a copy its owner stamps."""
    import copy

    from isocenter.privacy import PhiRemediation

    write_ct(tmp_path / "in" / "a.dcm", "PID-624", "7652", name="Alpha^One")
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        handed = []
        for finding in session.audit().findings:
            proposal = finding.remediation_proposal
            if (paired and finding.entity_type == "Instance"
                    and not finding.entity_path
                    and proposal and proposal.target_attr == "0010,0010"):
                finding = copy.copy(finding)
                finding.remediation_proposal = PhiRemediation(
                    "REMOVE_TAG", "0010,0010", original_value=proposal.original_value)
            handed.append(finding)
        session.anonymize(handed)
        present, files = _markers_in_both_exports(session, tmp_path, "out")
        assert [_exported(ds, "0010,0010") for ds in files] == ["ANONYMIZED"] * 2
        assert present == [[not paired] * 3] * 2
        assert _grade_of(session, tmp_path) == [
            "REVIEW_REQUIRED" if paired else "PASS"]
        if not paired:
            assert [str(ds[0x0012, 0x0062].value) for ds in files] == ["YES"] * 2


@pytest.mark.parametrize("which", ["name", "date"])
def test_the_owners_removal_in_a_later_pass_exports_no_marker_until_a_reaudit(
        tmp_path, which):
    """Instance findings first, owners later, under a REMOVE rule: the
    element is written empty either way. Until a re-audit the instance
    reads IDENTIFIED, the file carries neither `(0012,0062)` nor
    `(0012,0063)` and the run grades REVIEW_REQUIRED; at v1.0.0rc13 it
    carried both at once and graded PASS. After `anonymize(audit())` both
    are back, and PASS. `(0028,0303)`: the file keeps Series Date and the
    other dates as the source held them. Under the name rule Study Date
    is shifted all the same, and since #978 a shift this store wrote is
    always marked, so the third is `MODIFIED` there (it was absent until
    #978, which is what the 1.0.0rc15 record says). Under the date rule
    nothing is shifted and dates are as found, so it is absent: the
    control that the helper does not read every tag as present."""
    rules, tag, _owner, _field, _source = REMOVED[which]
    session, report, _patient, _study, inst = _remove_session(tmp_path, "7653", rules)
    with session:
        session.anonymize(_instance_findings(report))
        session.anonymize(_owner_findings(report))
        present, files = _markers_in_both_exports(session, tmp_path, "before")
        assert [_exported(ds, tag) for ds in files] == [""] * 2
        # The file first: it is what the line under correction is about.
        assert present == [[False, False, False]] * 2
        assert _grade_of(session, tmp_path) == ["REVIEW_REQUIRED"]
        assert inst.phi_status is PhiStatus.IDENTIFIED

        session.anonymize(session.audit())
        present, files = _markers_in_both_exports(session, tmp_path, "after")
        assert [_exported(ds, tag) for ds in files] == [""] * 2
        assert present == [[True, True, which == "name"]] * 2
        if which == "name":
            assert [str(ds[0x0028, 0x0303].value) for ds in files] == ["MODIFIED"] * 2
        assert _grade_of(session, tmp_path) == ["PASS"]
