"""A `REMOVE_TAG` whose tag is already gone is satisfied, not declined
(#626).

The end state REMOVE asks for -- the tag absent from the item -- is
#567's satisfied shape, which `EMPTY` on a sequence at zero items has
had since #577: stamped REMEDIATED, counted as handled by the scan
tally, no row, nothing counted as applied. Until now such a REMOVE fell
to the arm's bottom `else` and filed `Remediation declined for <uid>:
REMOVE_TAG on <tag> matched no applicable arm for Instance` (or `for
DicomItem`). `anonymize(report)` handed one report twice therefore wrote
one such row for every removal the first call made: measured on a67eb30,
CT_small and MR_small under the floor went 0 declines / PASS on the
first call to 196 declines / every instance IDENTIFIED / manifest
`anonymized` false / REVIEW_REQUIRED on the second, over a graph a
re-audit read as clean. The same happened on a first pass to a tag the
caller removed by hand between `audit()` and `anonymize()`.

**What still declines.** A REMOVE against an entity with no
`attributes` dict, a proposal whose action the arm does not implement,
and a hand-built tag spelled in upper case while the item holds it in
lower case: the REMOVE arms read the raw key, so such a tag falls past
them, and the satisfied test reads the canonical key so that a value
still there is never stamped over. Two findings that both decline still
write two rows (`test_declined_remediation_is_recorded.py`).

**Why this file imports what it does.** The pipeline through
`isocenter.session`, the arm through `isocenter.remediation`, the
hand-built findings through `isocenter.privacy` and the graph through
`isocenter.entities`, so it charges those four modules' probe rows; see
`test_mutation_probe_targets.py`.
"""
import json
import shutil
import sqlite3

import pytest
from pydicom.data import get_testdata_file

from isocenter.entities import DicomItem, Instance, PhiStatus
from isocenter.privacy import PhiFinding, PhiRemediation
from isocenter.remediation import RemediationService, _remediation_key
from isocenter.session import DicomSession
from support.project_secret import FIXED_A, load_fixed_secret

SOP_CLASS = "1.2.840.10008.5.1.4.1.1.7"
ABSENT = "0008,0080"
NO_ARM = "matched no applicable arm"

MODES = ["threads", "processes"]


@pytest.fixture(autouse=True)
def _threads_by_default(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


@pytest.fixture
def mode(request, monkeypatch):
    if request.param == "processes":
        monkeypatch.setenv("ISOCENTER_FORCE_PROCESSES", "1")
        monkeypatch.delenv("ISOCENTER_FORCE_THREADS", raising=False)
    return request.param


class _Rows:
    """A store stand-in that keeps the audit rows and nothing else."""

    def __init__(self):
        self.rows = []

    def log_audit_batch(self, rows):
        self.rows.extend(rows)

    def log_audit(self, *row):
        self.rows.append(row)


class _Bare:
    """No `set_attr`, no `attributes`, no useful names."""


def _finding(entity, action, tag, uid="1.2.3", path=None):
    return PhiFinding(
        entity_uid=uid, entity_type="Instance", field_name=tag, value=None,
        reason="test", tag=tag, entity=entity, entity_path=path,
        remediation_proposal=PhiRemediation(
            action_type=action, target_attr=tag, metadata={}))


def _instance():
    inst = Instance("1.2.3", SOP_CLASS, 1)
    inst.set_attr("0010,0010", "DOE^JOHN")
    inst.record_phi_status(PhiStatus.IDENTIFIED)
    return inst


def _service(rows, owners=None):
    service = RemediationService(store_backend=rows, project_secret=FIXED_A)
    if owners:
        service._use_instance_owners(owners)
    return service


def _session(tmp_path, names):
    src = tmp_path / "src"
    src.mkdir()
    for n, name in enumerate(names):
        shutil.copy(get_testdata_file(name), str(src / f"{n}.dcm"))
    session = DicomSession(str(tmp_path / "m.db"))
    load_fixed_secret(session, tmp_path, FIXED_A)
    session.ingest(str(src))
    return session


def _instances(session):
    return [i for p in session.store.patients for st in p.studies
            for se in st.series for i in se.instances]


def _declined(session):
    session.store_backend.flush_audit_queue()
    with sqlite3.connect(session.store_backend.db_path) as conn:
        return [d for (d,) in conn.execute(
            "SELECT details FROM audit_log WHERE action_type='REMEDIATION_DECLINED'")]


def _audit_text(session):
    session.store_backend.flush_audit_queue()
    with sqlite3.connect(session.store_backend.db_path) as conn:
        return "\n".join(d or "" for (d,) in conn.execute(
            "SELECT details FROM audit_log"))


def _manifest(session, tmp_path):
    path = tmp_path / "manifest.json"
    session.generate_manifest(str(path), format="json")
    return [item["anonymized"]
            for item in json.loads(path.read_text(encoding="utf-8"))["items"]]


def _grade(session, tmp_path):
    path = tmp_path / "report.md"
    session.generate_report(str(path))
    return [line for line in path.read_text(encoding="utf-8").splitlines()
            if "**Grade Basis:**" in line]


# ---------------------------------------------------------------------------
# U1, U2: the satisfied shape, hand-built
# ---------------------------------------------------------------------------

def test_a_remove_on_an_absent_tag_is_satisfied():
    """No row, REMEDIATED, the key counted as handled, nothing applied.
    Red before: one `matched no applicable arm` row, IDENTIFIED.

    The key is asserted through `_remediation_key`, not as a literal
    tuple: `entity_path` defaults to `()`, and a hardcoded tuple would
    pin the fixture's default rather than the contract."""
    inst = _instance()
    rows = _Rows()
    service = _service(rows)
    finding = _finding(inst, "REMOVE_TAG", ABSENT)

    applied = service.apply_remediation([finding])

    assert applied == 0
    assert rows.rows == []
    assert inst.phi_status is PhiStatus.REMEDIATED
    assert _remediation_key(finding) in service._satisfied_keys


def test_a_nested_remove_on_an_absent_tag_stamps_its_owner():
    """Inside a sequence, the item and the instance holding it are both
    stamped, as a nested success stamps both (#494). Red before: a
    `for DicomItem` row and the owner left IDENTIFIED."""
    inst = _instance()
    item = DicomItem()
    item.set_attr("0010,0010", "DOE^JOHN")
    inst.add_sequence_item("0008,1140", item)
    rows = _Rows()
    service = _service(rows, {id(item): inst})

    applied = service.apply_remediation(
        [_finding(item, "REMOVE_TAG", ABSENT, path="0008,1140[0]")])

    assert applied == 0
    assert rows.rows == []
    assert item.phi_status is PhiStatus.REMEDIATED
    assert inst.phi_status is PhiStatus.REMEDIATED


# ---------------------------------------------------------------------------
# U3, U4: through the session
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", MODES, indirect=True)
def test_one_report_applied_twice_writes_no_decline_and_grades_pass(
        tmp_path, mode):
    """The issue as filed: CT_small and MR_small under the floor,
    `anonymize(report)` twice. Red before: 196 declined rows, both
    instances IDENTIFIED, manifest false, REVIEW_REQUIRED."""
    session = _session(tmp_path, ["CT_small.dcm", "MR_small.dcm"])
    with session:
        report = session.audit()
        # Non-vacuity: the first call has removals to make.
        removes = [f for f in report.findings if f.remediation_proposal
                   and f.remediation_proposal.action_type == "REMOVE_TAG"]
        assert len(removes) > 100, len(removes)
        session.anonymize(report)
        assert _declined(session) == [], mode
        session.anonymize(report)

        assert _declined(session) == [], mode
        assert NO_ARM not in _audit_text(session)
        assert [i.phi_status for i in _instances(session)] == [
            PhiStatus.REMEDIATED, PhiStatus.REMEDIATED]
        assert _manifest(session, tmp_path) == [True, True]
        grade = _grade(session, tmp_path)
        assert len(grade) == 1 and "PASS" in grade[0], grade


@pytest.mark.parametrize("mode", MODES, indirect=True)
def test_a_tag_removed_by_hand_before_anonymize_reads_remediated(
        tmp_path, mode):
    """A first pass under the scan tally, with five of the tags the audit
    asked to remove deleted by hand in between: each is satisfied and
    counted as handled, so the tally reads the instance complete. Red
    before: five rows, IDENTIFIED, manifest false, REVIEW_REQUIRED.

    Kills: a satisfied REMOVE that is not added to `_satisfied_keys` --
    the tally then reads the uid incomplete and demotes it, and the
    manifest reads false while the grade stays PASS, so both are
    asserted."""
    session = _session(tmp_path, ["CT_small.dcm"])
    with session:
        report = session.audit()
        (inst,) = _instances(session)
        targets = [f.remediation_proposal.target_attr for f in report.findings
                   if f.remediation_proposal
                   and f.remediation_proposal.action_type == "REMOVE_TAG"
                   and f.entity is inst and not f.entity_path]
        gone = [t for t in targets if t in inst.attributes][:5]
        assert len(gone) == 5, gone
        for tag in gone:
            del inst.attributes[tag]
        session.anonymize(report)

        assert _declined(session) == [], mode
        assert inst.phi_status is PhiStatus.REMEDIATED
        assert _manifest(session, tmp_path) == [True]
        grade = _grade(session, tmp_path)
        assert len(grade) == 1 and "PASS" in grade[0], grade


# ---------------------------------------------------------------------------
# U5, U6: the boundaries
# ---------------------------------------------------------------------------

def _uppercase_attribute():
    inst = _instance()
    inst.set_attr("0008,00a0", "SECRET")
    return inst, "REMOVE_TAG", "0008,00A0", lambda: inst.attributes.get("0008,00a0") == "SECRET"


def _uppercase_sequence():
    inst = _instance()
    inst.add_sequence_item("0040,a730", DicomItem())
    return inst, "REMOVE_TAG", "0040,A730", lambda: len(inst.sequences["0040,a730"].items) == 1


def _bare():
    return _Bare(), "REMOVE_TAG", "patient_id", lambda: True


def _unknown_action():
    # An absent tag, so a predicate that forgot to read the action type
    # would call this satisfied.
    inst = _instance()
    return inst, "REDACT_REGION", ABSENT, lambda: ABSENT not in inst.attributes


@pytest.mark.parametrize("build", [
    pytest.param(_bare, id="bare"),
    pytest.param(_unknown_action, id="unknown_action"),
    pytest.param(_uppercase_attribute, id="uppercase_attribute"),
    pytest.param(_uppercase_sequence, id="uppercase_sequence"),
])
def test_a_remove_that_matched_no_arm_still_declines(build):
    """The other ways to the bottom `else` stay declines: no `attributes`
    dict, an action the arm does not implement, and a raw key the arms
    did not match while the canonical one is still held -- an attribute
    or a sequence -- so the value is still there and is not stamped over.

    Kills: the action-type check dropped; the canonical key read raw; the
    sequences half of the predicate dropped."""
    entity, action, tag, still_there = build()
    before = getattr(entity, "phi_status", None)
    rows = _Rows()

    applied = _service(rows).apply_remediation([_finding(entity, action, tag)])

    assert applied == 0
    assert still_there()
    assert [a for a, *_ in rows.rows] == ["REMEDIATION_DECLINED"], rows.rows
    assert NO_ARM in rows.rows[0][2], rows.rows
    assert getattr(entity, "phi_status", None) is before


def test_a_key_holding_none_is_still_removed():
    """Present with None is not absent: `set_attr(tag, None)` leaves the
    key, the REMOVE arm finds it and deletes it with a
    `REMEDIATION_REMOVE` row, as before."""
    inst = _instance()
    inst.set_attr(ABSENT, None)
    rows = _Rows()

    applied = _service(rows).apply_remediation([_finding(inst, "REMOVE_TAG", ABSENT)])

    assert applied == 1
    assert ABSENT not in inst.attributes
    assert [a for a, *_ in rows.rows] == ["REMEDIATION_REMOVE"], rows.rows
