"""A finding raised under the policy and never acted on costs the run its PASS (#573).

Condition 7 of the grade (`docs/analytics.md`, "How the grade is
decided"). Before it, the grade read only the audit log, and `audit()`
writes no audit row for a finding. So a finding nobody passed to
`anonymize()` left no trace the grade could see. Measured at 03fcdaac on
3.12, CT_small under the floor policy, every one of these graded PASS:

- (b) `ingest` -> `audit` -> `export`, no `anonymize()`: every entity
  IDENTIFIED, the file carrying the original name, ID and date;
- (d) `anonymize()` handed only the patient's findings: study and instance
  IDENTIFIED, the Study Date unshifted in the file;
- (e) `anonymize()` handed only the instance's findings: patient, study
  and instance IDENTIFIED -- the instance's copies of the name, ID and
  date follow their owners, which the pass never touched, so those
  findings are left unhandled (#624) -- and the export stamped the
  original name, ID and date.

The term reads `phi_status`, the per-entity result of the last scan, which
is persisted per entity. So it holds in a store reopened by a session that
never ran the scan, like every other store-wide term (owner ruling Q2,
2026-09-21). An entity never scanned, or edited since, reads UNSCANNED and
does not grade (Q3): section 5 counts those instances instead.
"""
import re

import pytest

from isocenter import Session
from isocenter.privacy import PhiReport

from support.ct_small_files import write_ct

MODES = ["threads", "processes"]


@pytest.fixture(autouse=True)
def _threads_by_default(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


@pytest.fixture
def mode(request, monkeypatch):
    """The scan's two parallel paths: findings rehydrated from a thread's
    copy or from a process's pickle. The status is recorded on the live
    graph after either."""
    if request.param == "processes":
        monkeypatch.setenv("ISOCENTER_FORCE_PROCESSES", "1")
        monkeypatch.delenv("ISOCENTER_FORCE_THREADS", raising=False)
    return request.param


def _ingested(tmp_path):
    write_ct(tmp_path / "in" / "a.dcm", "PA", "5731", name="Alpha^One")
    session = Session(str(tmp_path / "s.db"))
    session.ingest(str(tmp_path / "in"))
    return session


def _section_5(session, tmp_path):
    path = tmp_path / "report.md"
    session.generate_report(str(path))
    text = path.read_text(encoding="utf-8")
    return text.split("## 5. Validation & Verification", 1)[1]


def _reasons(section_5):
    """The Grade Basis lines, one per reason; [] for PASS."""
    basis = section_5.split("*   **Metadata Remediation:**", 1)[0]
    if "**Grade Basis:** PASS" in basis:
        return []
    return re.findall(r"^    \*   (.*)$", basis, flags=re.M)


def _unacted(reasons):
    return [r for r in reasons if "read IDENTIFIED" in r]


def _unscanned_line(section_5):
    [line] = [l for l in section_5.splitlines() if "**PHI Scan (`audit()`):**" in l]
    return line


def _only(findings, entity_type):
    return PhiReport([f for f in findings if f.entity_type == entity_type])


@pytest.mark.parametrize("mode", MODES, indirect=True)
def test_an_audit_nobody_acted_on_grades_review_required(tmp_path, mode):
    """T-A1, scenario (b). Kills M-A1 (the term dropped) and M-A2 (the walk
    skips instances: the per-level counts expose it)."""
    with _ingested(tmp_path) as session:
        report = session.audit()
        assert report.findings
        session.export(str(tmp_path / "out"), use_compression=False)
        section_5 = _section_5(session, tmp_path)

    reasons = _reasons(section_5)
    # The only reason: nothing else in this run is wrong.
    assert reasons == [
        "3 entities read IDENTIFIED: the last PHI scan raised a finding "
        "under the policy it ran with, and no `anonymize()` pass since "
        "acted on it (patients 1, studies 1, instances 1)"], section_5


@pytest.mark.parametrize("mode", MODES, indirect=True)
def test_a_partial_pass_grades_in_a_reopened_store(tmp_path, mode):
    """T-A2, scenario (d), reported by a session that never scanned.

    Kills M-A3 (the term reads the session's memory of its scan rather than
    `phi_status`: the reopening session has none) and M-A4 (REMEDIATED
    counted: the patient is REMEDIATED and must read 0)."""
    with _ingested(tmp_path) as session:
        report = session.audit()
        session.anonymize(_only(report.findings, "Patient"))
        session.export(str(tmp_path / "out"), use_compression=False)

    with Session(str(tmp_path / "s.db")) as reopened:
        section_5 = _section_5(reopened, tmp_path)

    assert _unacted(_reasons(section_5)) == [
        "2 entities read IDENTIFIED: the last PHI scan raised a finding "
        "under the policy it ran with, and no `anonymize()` pass since "
        "acted on it (patients 0, studies 1, instances 1)"], section_5


def test_the_count_is_over_the_whole_store_not_the_export(tmp_path):
    """Owner ruling Q2: counted store-wide, like every other condition, not
    over the exported instances. `PA` is passed its patient findings only
    and exported alone, leaving its study and instance; `PB`, two studies,
    is scanned and never acted on, and still grades. Both patients hold
    uncounted-if-skipped entities, so the result does not depend on which
    one `store.patients` lists first. Kills M-A10 (the walk narrowed to
    the first patient), M-A11 (to each patient's first study) and any
    narrowing to what was exported."""
    write_ct(tmp_path / "in" / "a.dcm", "PA", "5732", name="Alpha^One")
    write_ct(tmp_path / "in" / "b1.dcm", "PB", "5733", name="Beta^Two")
    write_ct(tmp_path / "in" / "b2.dcm", "PB", "5734", name="Beta^Two")
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        report = session.audit()
        session.anonymize(PhiReport([f for f in report.findings
                                     if f.patient_id == "PA"
                                     and f.entity_type == "Patient"]))
        [pa_sop] = [i.sop_instance_uid for p in session.store.patients
                    if p.patient_id != "PB" for st in p.studies
                    for se in st.series for i in se.instances]
        session.export(str(tmp_path / "out"), use_compression=False,
                       subset=[pa_sop])
        section_5 = _section_5(session, tmp_path)

    assert _unacted(_reasons(section_5)) == [
        "7 entities read IDENTIFIED: the last PHI scan raised a finding "
        "under the policy it ran with, and no `anonymize()` pass since "
        "acted on it (patients 1, studies 3, instances 3)"], section_5
    assert _unscanned_line(section_5) == (
        "*   **PHI Scan (`audit()`):** 0 of 3 instance(s) have no PHI scan "
        "at their current revision.")


def test_an_instance_only_pass_grades(tmp_path):
    """Scenario (e): the file carries the original name, ID and date,
    because the export stamps them from the owners the pass never touched.
    The instance counts too: its copies of those three are written only
    through their owners (#624), so its findings on them are left
    unhandled rather than written as `ANONYMIZED` and a shift no file
    carries (coordinator ruling Q-C5)."""
    with _ingested(tmp_path) as session:
        report = session.audit()
        session.anonymize(_only(report.findings, "Instance"))
        session.export(str(tmp_path / "out"), use_compression=False)
        section_5 = _section_5(session, tmp_path)

    assert _unacted(_reasons(section_5)) == [
        "3 entities read IDENTIFIED: the last PHI scan raised a finding "
        "under the policy it ran with, and no `anonymize()` pass since "
        "acted on it (patients 1, studies 1, instances 1)"], section_5


def test_a_full_pass_grades_pass(tmp_path):
    """T-A3, scenario (c). Kills M-A6 (series counted: a series is never
    scanned, so it would count here as UNSCANNED or fail the PASS)."""
    with _ingested(tmp_path) as session:
        session.anonymize(session.audit())
        session.export(str(tmp_path / "out"), use_compression=False)
        section_5 = _section_5(session, tmp_path)

    assert _reasons(section_5) == [], section_5
    assert "**Grade Basis:** PASS" in section_5, section_5
    assert _unscanned_line(section_5) == (
        "*   **PHI Scan (`audit()`):** 0 of 1 instance(s) have no PHI scan "
        "at their current revision.")


def test_an_export_nothing_scanned_grades_pass_and_says_so(tmp_path):
    """T-A3, scenario (a), Q3: data no scan has seen is not a finding, so it
    does not grade, and section 5 says how much of it there is. Kills M-A5
    (UNSCANNED counted as not acted on)."""
    with _ingested(tmp_path) as session:
        session.export(str(tmp_path / "out"), use_compression=False)
        section_5 = _section_5(session, tmp_path)

    assert _reasons(section_5) == [], section_5
    assert "**Grade Basis:** PASS" in section_5, section_5
    assert _unscanned_line(section_5).startswith(
        "*   **PHI Scan (`audit()`):** 1 of 1 instance(s) have no PHI scan "
        "at their current revision."), section_5


def test_an_entity_edited_after_its_scan_does_not_grade(tmp_path):
    """T-A4: the documented rule that an entity edited after its scan reads
    UNSCANNED and does not grade under condition 7, pinned.

    The instance is IDENTIFIED by the scan and then edited; its patient and
    study are not edited and still count. Kills M-A8 (the status read
    without its revision check, which counts the instance's stale
    IDENTIFIED)."""
    with _ingested(tmp_path) as session:
        session.audit()
        [patient] = session.store.patients
        instance = patient.studies[0].series[0].instances[0]
        instance.set_attr("0008,103e", "an edit after the scan")
        session.export(str(tmp_path / "out"), use_compression=False)
        section_5 = _section_5(session, tmp_path)

    assert _unacted(_reasons(section_5)) == [
        "2 entities read IDENTIFIED: the last PHI scan raised a finding "
        "under the policy it ran with, and no `anonymize()` pass since "
        "acted on it (patients 1, studies 1, instances 0)"], section_5
    assert "1 of 1 instance(s) have no PHI scan" in _unscanned_line(section_5)
