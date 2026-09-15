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
`attributes` dict; a proposal whose action the arm does not implement; a
hand-built tag spelled in upper case while the item holds it in lower
case (the REMOVE arms read the raw key, so such a tag falls past them,
and the satisfied test reads the lower-cased key, which is held); and
any key that is not a well-formed `gggg,eeee` tag once lower-cased --
`00080080`, `(0008,0080)`, `InstitutionName`, `patient_id` -- because
its absence from `attributes` is no evidence the element it seems to
name is gone (review of this change, M1). Two findings that both decline still
write two rows (`test_declined_remediation_is_recorded.py`).

**Why this file imports what it does.** The pipeline through
`isocenter.session`, the arm through `isocenter.remediation`, the
hand-built findings through `isocenter.privacy` and the graph through
`isocenter.entities`, so it charges those four modules' probe rows; see
`test_mutation_probe_targets.py`.
"""
import json
import logging
import re
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


def _finding(entity, action, tag, uid="1.2.3", path=None, new_value=None):
    return PhiFinding(
        entity_uid=uid, entity_type="Instance", field_name=tag, value=None,
        reason="test", tag=tag, entity=entity, entity_path=path,
        remediation_proposal=PhiRemediation(
            action_type=action, target_attr=tag, new_value=new_value,
            metadata={}))


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
    """The grade token of every `**Grade Basis:**` line, read whole.

    A token, not a substring: `"PASS" in line` would also pass a
    REVIEW_REQUIRED line whose reasons happened to quote the word."""
    path = tmp_path / "report.md"
    session.generate_report(str(path))
    return [re.search(r"\*\*Grade Basis:\*\* ([A-Z_]+)", line).group(1)
            for line in path.read_text(encoding="utf-8").splitlines()
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
        assert _grade(session, tmp_path) == ["PASS"]


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
        assert _grade(session, tmp_path) == ["PASS"]


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
    # Nothing to still be there; what can be checked is that nothing was
    # set on it either.
    bare = _Bare()
    return bare, "REMOVE_TAG", "patient_id", lambda: vars(bare) == {}


def _bare_with_a_tag():
    # The same entity under a well-formed tag. `patient_id` above now
    # declines at the well-formed check before the `attributes` guard is
    # read, so without this the guard is pinned by nothing: dropped, a
    # well-formed tag reaches `tag not in None`.
    bare = _Bare()
    return bare, "REMOVE_TAG", "0010,0020", lambda: vars(bare) == {}


def _unknown_action():
    # An absent tag, so a predicate that forgot to read the action type
    # would call this satisfied.
    inst = _instance()
    return inst, "REDACT_REGION", ABSENT, lambda: ABSENT not in inst.attributes


HELD = "JFK IMAGING CENTER"


def _misspelled(spelling, held_tag=ABSENT):
    """A REMOVE keyed by a spelling of a tag the item holds that is not
    its `gggg,eeee` key: every one of these lower-cases onto a key the
    item does not have, so a predicate that only lower-cased read the
    tag as gone and stamped the value it was pointed at REMEDIATED."""
    def build():
        inst = _instance()
        inst.set_attr(held_tag, HELD)
        return (inst, "REMOVE_TAG", spelling,
                lambda: inst.attributes.get(held_tag) == HELD)
    return build


@pytest.mark.parametrize("build", [
    pytest.param(_bare, id="bare"),
    pytest.param(_bare_with_a_tag, id="bare_with_a_tag"),
    pytest.param(_unknown_action, id="unknown_action"),
    pytest.param(_uppercase_attribute, id="uppercase_attribute"),
    pytest.param(_uppercase_sequence, id="uppercase_sequence"),
    pytest.param(_misspelled("00080080"), id="no_comma"),
    pytest.param(_misspelled("(0008,0080)"), id="parenthesised"),
    pytest.param(_misspelled("0008, 0080"), id="inner_space"),
    pytest.param(_misspelled(" 0008,0080"), id="leading_space"),
    # Nine characters, so a check that only measured the length would
    # pass it; every other spelling here has a length of its own.
    pytest.param(_misspelled("0008.0080"), id="nine_characters"),
    # Nine characters with the comma in place and a letter O for a zero,
    # so only the hex half of the check refuses it (review of #639 r2, F2).
    pytest.param(_misspelled("0O08,0080"), id="letter_o"),
    pytest.param(_misspelled("InstitutionName"), id="keyword"),
    pytest.param(_misspelled("patient_id", held_tag="0010,0020"), id="python_name"),
])
def test_a_remove_that_matched_no_arm_still_declines(build):
    """The other ways to the bottom `else` stay declines: no `attributes`
    dict, an action the arm does not implement, a raw key the arms did
    not match while the canonical one is still held -- an attribute or a
    sequence -- and any key that is not a well-formed `gggg,eeee` tag
    once lower-cased. In each the value is still there and is not stamped
    over.

    The malformed spellings are the review of #626 (M1): measured on
    c6d0112, each was satisfied -- no row, REMEDIATED, the value kept --
    and through a session the run graded PASS with the value in the
    exported file. At a67eb30 each was a decline. Only a well-formed tag
    can be said to be absent: `InstitutionName` or `patient_id` absent
    from `attributes` says nothing about whether the item holds the
    element they name.

    Kills: the action-type check dropped; the `attributes` guard dropped
    (`bare_with_a_tag`); the canonical key read raw; the sequences half
    of the predicate dropped; the well-formed-tag check dropped, or
    weakened to a comma or to a length of nine (`nine_characters`), or
    to a length of nine and a comma with no hex check (`letter_o`)."""
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


@pytest.mark.parametrize("mode", MODES, indirect=True)
def test_a_misspelled_remove_through_the_session_grades_review_required(
        tmp_path, mode):
    """The review's session probe for M1: CT_small, no `audit()`, a
    hand-built `REMOVE_TAG` on Institution Name spelled `00080080`
    handed to the public `anonymize(findings)` beside a real removal of
    Patient's Birth Date, so the pass has one success. Red on c6d0112:
    no decline, the instance REMEDIATED and grade PASS, with `JFK IMAGING
    CENTER` still in the graph and so in the exported file. Now the one
    decline row, IDENTIFIED and REVIEW_REQUIRED: the file still carries
    the value, and the session says so. (The manifest read false on
    c6d0112 too -- no `audit()`, so nothing vouches for the instance --
    and is asserted as the story, not as the red.)"""
    session = _session(tmp_path, ["CT_small.dcm"])
    with session:
        (inst,) = _instances(session)
        held = inst.attributes.get(ABSENT)
        # Non-vacuity: the fixture carries both tags.
        assert held == HELD, held
        assert "0010,0030" in inst.attributes
        uid = inst.sop_instance_uid
        misspelled = _finding(inst, "REMOVE_TAG", "00080080", uid=uid)
        companion = _finding(inst, "REMOVE_TAG", "0010,0030", uid=uid)

        assert session.anonymize([misspelled, companion]) == 1

        assert "0010,0030" not in inst.attributes
        assert inst.attributes.get(ABSENT) == HELD
        declines = _declined(session)
        assert len(declines) == 1, declines
        assert "REMOVE_TAG on 00080080 " + NO_ARM in declines[0], declines
        assert inst.phi_status is PhiStatus.IDENTIFIED, mode
        assert _manifest(session, tmp_path) == [False]
        assert _grade(session, tmp_path) == ["REVIEW_REQUIRED"]


# ---------------------------------------------------------------------------
# A satisfied removal does not mask a decline on the same instance
# ---------------------------------------------------------------------------

def _replace_on_absent(inst):
    # #547: a value aimed at a tag the item no longer holds. A different
    # tag from the satisfied removal's, so nothing here leans on how two
    # proposals on one key are deduplicated (#636).
    return _finding(inst, "REPLACE_TAG", "0008,1010", new_value="ANONYMIZED")


def _unknown_action_on(inst):
    return _finding(inst, "REDACT_REGION", "0008,1010")


@pytest.mark.parametrize("order", ["decline_first", "satisfied_first"])
@pytest.mark.parametrize("decline", [
    pytest.param(_replace_on_absent, id="replace_on_absent"),
    pytest.param(_unknown_action_on, id="unknown_action"),
])
def test_a_satisfied_remove_beside_a_decline_on_the_same_instance_leaves_it_identified(
        decline, order):
    """The satisfied stamp is REMEDIATED, as a success's is; the pass-end
    demotion (#486) must still take an instance that also declined back
    to IDENTIFIED, whichever came first -- the satisfied analogue of
    `test_one_value_per_owned_tag.py`'s
    `test_a_fold_beside_a_decline_on_the_same_instance_leaves_it_identified`.

    `_instance()` starts IDENTIFIED, so "ends IDENTIFIED" means something
    only because the satisfied removal would have left it REMEDIATED
    (U1): the satisfied key is asserted to be counted, and the one row
    to be the decline's.

    Kills: a satisfied removal that takes its entity out of
    `_declined_entities` (the review's `r1`, which survived the whole
    `remediation.py` row on c6d0112). That mutant bites only in
    `decline_first`; `satisfied_first` is its guard in the other
    direction."""
    inst = _instance()
    rows = _Rows()
    service = _service(rows)
    satisfied = _finding(inst, "REMOVE_TAG", ABSENT)
    declined = decline(inst)
    findings = ([declined, satisfied] if order == "decline_first"
                else [satisfied, declined])

    applied = service.apply_remediation(findings)

    assert applied == 0
    assert _remediation_key(satisfied) in service._satisfied_keys
    assert [a for a, *_ in rows.rows] == ["REMEDIATION_DECLINED"], rows.rows
    assert "0008,1010" in rows.rows[0][2], rows.rows
    assert inst.phi_status is PhiStatus.IDENTIFIED, order


# ---------------------------------------------------------------------------
# The INFO line a satisfied removal logs
# ---------------------------------------------------------------------------

def test_a_satisfied_remove_logs_one_info_line_naming_the_tag_and_uid_only(caplog):
    """The CHANGELOG promises one INFO line per satisfied removal, `<tag>
    is not on <uid>; nothing to remove`, the level the satisfied `EMPTY`
    logs at. The line names the tag and the instance UID -- the log
    convention `_log_subject` and `_log_line` state -- and nothing else:
    no value the item holds, and for a finding filed under a Patient not
    the Patient ID, which `_log_subject` renders as `a patient`.

    Kills: the INFO line deleted (the review's `m14`, which survived the
    whole row on c6d0112); the line logged at another level; a subject
    that prints a Patient ID."""
    inst = _instance()
    other = _instance()
    rows = _Rows()
    by_instance = _finding(inst, "REMOVE_TAG", ABSENT, uid="1.2.3")
    by_patient = PhiFinding(
        entity_uid="PAT-626", entity_type="Patient", field_name=ABSENT,
        value=None, reason="test", tag="0008,1010", entity=other,
        remediation_proposal=PhiRemediation(
            action_type="REMOVE_TAG", target_attr="0008,1010", metadata={}))

    with caplog.at_level(logging.DEBUG, logger="isocenter"):
        applied = _service(rows).apply_remediation([by_instance, by_patient])

    assert applied == 0 and rows.rows == []
    said = [(r.levelname, r.getMessage()) for r in caplog.records
            if "nothing to remove" in r.getMessage()]
    assert said == [
        ("INFO", f"{ABSENT} is not on 1.2.3; nothing to remove"),
        ("INFO", "0008,1010 is not on a patient; nothing to remove"),
    ], said
    for _, message in said:
        assert "PAT-626" not in message and "DOE^JOHN" not in message
        assert "/" not in message, message


# ---------------------------------------------------------------------------
# Absence is read on the live object at the finding's address
# ---------------------------------------------------------------------------
#
# Review of #639 r2 (M1, F1). `anonymize(findings)` does not rehydrate a
# finding's `entity`, so a report kept across `close()` and a reopen still
# points at the first session's objects, which its first pass cleaned. Read
# there, every REMOVE was "already gone" while the live graph -- the one
# `export()` writes -- still held each value: measured on 063ed59, 0
# declines, PASS, Institution Name and Patient's Name exported. The same
# class reaches a hand-built finding whose `entity` is not the object at its
# `entity_uid`/`entity_path`. The session now resolves that address and the
# satisfied test reads the object it finds; a UID that names no single
# instance satisfies nothing, and a nested path that breaks is the next
# section's question. REPLACE and SHIFT on a stale report are #644.

HELD_VALUES = ("JFK IMAGING CENTER", "TOSHIBA", "CompressedSamples", "CT01")
STALE = "not the object this session holds at"


def _exported(session, tmp_path, name="out"):
    import pydicom  # pylint: disable=import-outside-toplevel
    out = tmp_path / name
    session.export(str(out), use_compression=False)
    return [pydicom.dcmread(str(p), stop_before_pixels=True)
            for p in sorted(out.rglob("*.dcm"))]


def _removes(report):
    return [f for f in report.findings if f.remediation_proposal
            and f.remediation_proposal.action_type == "REMOVE_TAG"]


def _first_session_passes(tmp_path, save_after_pass):
    """Session 1 of the review's `stale_report` flow: ingest, save, audit,
    anonymize, close -- saving the pass only when asked. Returns the
    report, still in hand as it would be in a notebook."""
    session = _session(tmp_path, ["CT_small.dcm", "MR_small.dcm"])
    with session:
        session.save(sync=True)
        report = session.audit()
        session.anonymize(report)
        assert _declined(session) == []
        if save_after_pass:
            session.save(sync=True)
    return report


@pytest.mark.parametrize("mode", MODES, indirect=True)
def test_a_report_kept_across_a_reopen_of_an_unsaved_pass_declines_its_removals(
        tmp_path, mode):
    """The review's `stale_report`: session 1's pass was never saved, so
    the reopened graph still holds every value the report removed. Each
    REMOVE declines, naming no value, and the run grades REVIEW_REQUIRED
    over a file that still carries them. Red on 063ed59: 0 declines and
    PASS, with `JFK IMAGING CENTER` exported.

    Kills: absence read on `finding.entity` rather than the live object;
    the session not handing the service its live objects."""
    report = _first_session_passes(tmp_path, save_after_pass=False)
    removes = _removes(report)
    assert len(removes) > 100, len(removes)

    session = DicomSession(str(tmp_path / "m.db"))
    with session:
        live = _instances(session)
        # Non-vacuity: the live graph is not the one the report points at,
        # and it still holds what the first pass removed.
        assert not {id(f.entity) for f in removes} & {id(i) for i in live}
        assert "JFK IMAGING CENTER" in [i.attributes.get(ABSENT) for i in live]

        session.anonymize(report)

        declines = _declined(session)
        assert len(declines) == len(removes), (len(declines), len(removes))
        for row in declines:
            assert not any(v in row for v in HELD_VALUES), row
        assert all(STALE in row for row in declines), declines[:3]
        assert _grade(session, tmp_path) == ["REVIEW_REQUIRED"]
        ct = [ds for ds in _exported(session, tmp_path) if ds.Modality == "CT"]
        assert [ds.InstitutionName for ds in ct] == ["JFK IMAGING CENTER"]


@pytest.mark.parametrize("mode", MODES, indirect=True)
def test_a_report_kept_across_a_reopen_of_a_saved_pass_still_grades_pass(
        tmp_path, mode):
    """The review's `stale_report_saved`: the pass was saved, so the
    reopened graph is clean and each REMOVE's end state holds on the live
    object at its address -- satisfied, as #626 asks, and the exported
    files carry none of the values. A guard, green before: reading
    liveness as "`finding.entity` is the live object" would decline all
    of them here (a67eb30's REVIEW_REQUIRED), where nothing is wrong."""
    report = _first_session_passes(tmp_path, save_after_pass=True)
    removes = _removes(report)
    assert len(removes) > 100, len(removes)

    session = DicomSession(str(tmp_path / "m.db"))
    with session:
        live = _instances(session)
        assert not {id(f.entity) for f in removes} & {id(i) for i in live}

        session.anonymize(report)

        assert _declined(session) == [], mode
        assert _grade(session, tmp_path) == ["PASS"]
        exported = _exported(session, tmp_path)
        assert len(exported) == 2
        removed = {f.remediation_proposal.target_attr for f in removes
                   if not f.entity_path}
        # Per element, not a text search: MR_small's Manufacturer is
        # `TOSHIBA_MEC`, which is not the Institution Name and stays.
        assert sorted(ds.get("InstitutionName", "") for ds in exported) == ["", ""]
        for ds in exported:
            assert "CompressedSamples" not in str(ds.get("PatientName", "")), ds.PatientName
            assert (0x0009, 0x1002) not in ds
            for tag in removed:
                group, element = (int(x, 16) for x in tag.split(","))
                if group % 2:
                    assert (group, element) not in ds, (tag, ds.Modality)


def _ct_and_mr(session):
    by_modality = {se.modality: se.instances[0] for p in session.store.patients
                   for st in p.studies for se in st.series}
    return by_modality["CT"], by_modality["MR"]


def _wrong_uid(session):
    """`entity` the CT instance, with Institution Name deleted from it;
    `entity_uid` the MR's, which holds `TOSHIBA`."""
    ct, mr = _ct_and_mr(session)
    del ct.attributes[ABSENT]
    finding = _finding(ct, "REMOVE_TAG", ABSENT, uid=mr.sop_instance_uid)
    return finding, ct, lambda: mr.attributes.get(ABSENT) == "TOSHIBA"


def _nested_mismatch(session):
    """`entity` the CT instance, `entity_path` an item under Referenced
    Image Sequence that holds Other Patient IDs; the top level does not.
    The finding's key is exactly the scan's key for the nested element."""
    ct, _ = _ct_and_mr(session)
    item = DicomItem()
    item.set_attr("0010,1000", "OTHER-PID-123")
    ct.add_sequence_item("0008,1140", item)
    assert "0010,1000" not in ct.attributes
    finding = _finding(ct, "REMOVE_TAG", "0010,1000", uid=ct.sop_instance_uid,
                       path=(("0008,1140", 0),))
    return finding, ct, lambda: item.attributes.get("0010,1000") == "OTHER-PID-123"


def _no_such_uid(session):
    """An address no instance in the session has."""
    ct, _ = _ct_and_mr(session)
    del ct.attributes[ABSENT]
    finding = _finding(ct, "REMOVE_TAG", ABSENT, uid="1.2.3.4.5.6.7.8.9")
    return finding, ct, lambda: True


def _shorter_sequence_still_held(session):
    """The right instance, a path to index 1 of a sequence that now holds
    one item, and that one item still holds the tag -- the value a shift
    moved, or one the finding never covered. The path breaks at the
    sequence, and a remaining item holds the tag, so nothing says the
    element is gone. The instance's own top level lacks the tag, so reading
    it instead (review of #639 r3, `w1`) would call this satisfied."""
    ct, _ = _ct_and_mr(session)
    held = DicomItem()
    held.set_attr("0010,1000", "SHIFTED-PHI")
    ct.add_sequence_item("0008,1140", held)
    assert "0010,1000" not in ct.attributes
    finding = _finding(DicomItem(), "REMOVE_TAG", "0010,1000",
                       uid=ct.sop_instance_uid, path=(("0008,1140", 1),))
    return finding, ct, lambda: held.attributes.get("0010,1000") == "SHIFTED-PHI"


def _study_sharing_the_instance_uid(session):
    """A Study whose Study Instance UID is its instance's SOP Instance UID
    (hand-built), with Study Date deleted from the instance, and a
    tag-keyed REMOVE filed against the Study. A Study has no `attributes`,
    so a removal on it is never satisfied -- unless its address is looked
    up among the instances, where the colliding instance lacks the tag
    (review of #639 r3, F1: `n9`, `w2`)."""
    ct, _ = _ct_and_mr(session)
    study = next(st for p in session.store.patients for st in p.studies
                 if ct in st.series[0].instances)
    study.study_instance_uid = ct.sop_instance_uid
    del ct.attributes["0008,0020"]
    finding = PhiFinding(
        entity_uid=ct.sop_instance_uid, entity_type="Study",
        field_name="0008,0020", value=None, reason="test", tag="0008,0020",
        entity=study, remediation_proposal=PhiRemediation(
            action_type="REMOVE_TAG", target_attr="0008,0020", metadata={}))
    return finding, ct, lambda: study.phi_status is not PhiStatus.REMEDIATED


def _shared_uid(session):
    """Two instances holding one UID (hand-built, `docs/api/stability.md`),
    neither holding Institution Name, and a finding whose entity is
    neither: the address is ambiguous, so it names nothing -- guessing the
    first would read a clean instance the finding may not mean."""
    ct, mr = _ct_and_mr(session)
    mr.sop_instance_uid = ct.sop_instance_uid
    for inst in (ct, mr):
        inst.attributes.pop(ABSENT, None)
    finding = _finding(DicomItem(), "REMOVE_TAG", ABSENT, uid=ct.sop_instance_uid)
    return finding, ct, lambda: True


@pytest.mark.parametrize("build", [
    pytest.param(_wrong_uid, id="wrong_uid"),
    pytest.param(_nested_mismatch, id="nested_mismatch"),
    pytest.param(_no_such_uid, id="no_such_uid"),
    pytest.param(_shorter_sequence_still_held, id="shorter_sequence_still_held"),
    pytest.param(_shared_uid, id="shared_uid"),
    pytest.param(_study_sharing_the_instance_uid, id="study_sharing_the_instance_uid"),
])
def test_a_remove_whose_entity_is_not_at_its_address_declines(tmp_path, build, caplog):
    """Hand-built, through `anonymize(findings)`, beside a real removal of
    Patient's Birth Date on the CT so the pass has one success. Absence
    on an object the finding does not address is no evidence the element
    is gone. Red on 063ed59: 0 declines and PASS -- for `wrong_uid` and
    `nested_mismatch` with `TOSHIBA` and `OTHER-PID-123` exported.

    Every finding carries a sentinel `value`, which neither the row nor
    the log may repeat (review of #639 r3, `w5`). The Study's decline is
    the arm's own (`matched no applicable arm`), not the address's.

    Kills: absence read on `finding.entity`; an address that resolves to
    nothing read as satisfied; liveness by UID alone, ignoring the path;
    an ambiguous UID resolved to its first instance (`shared_uid`); a
    broken path read on the instance's top level, or read satisfied though
    a remaining item holds the tag (`shorter_sequence_still_held`); a
    non-Instance finding resolved among the instances
    (`study_sharing_the_instance_uid`); a value in the decline."""
    session = _session(tmp_path, ["CT_small.dcm", "MR_small.dcm"])
    with session:
        finding, ct, still_there = build(session)
        finding.value = VALUE_SENTINEL
        assert "0010,0030" in ct.attributes
        companion = _finding(ct, "REMOVE_TAG", "0010,0030",
                             uid=ct.sop_instance_uid)

        with caplog.at_level(logging.DEBUG, logger="isocenter"):
            assert session.anonymize([finding, companion]) == 1

        assert "0010,0030" not in ct.attributes
        assert still_there()
        declines = _declined(session)
        assert len(declines) == 1, declines
        expected = NO_ARM if finding.entity_type == "Study" else STALE
        assert expected in declines[0], declines
        assert not any(v in declines[0] for v in (
            "TOSHIBA", "OTHER-PID-123", "SHIFTED-PHI", VALUE_SENTINEL)), declines
        assert VALUE_SENTINEL not in caplog.text
        assert _grade(session, tmp_path) == ["REVIEW_REQUIRED"]


VALUE_SENTINEL = "VALUE-SENTINEL-626"


def test_a_shared_uid_resolves_to_the_instance_the_finding_names(tmp_path):
    """Two instances holding one UID: A lacks Institution Name, B holds
    it. A finding whose entity is A is at its address -- A's path leads to
    A -- so its removal is satisfied on A, and B keeps its value, which
    the finding never named. The ambiguous case, an entity at neither, is
    `shared_uid` above. A guard, green before.

    Kills: the identity match dropped, so a shared UID resolves only when
    one instance holds it (review of #639 r3, `w3`)."""
    session = _session(tmp_path, ["CT_small.dcm", "MR_small.dcm"])
    with session:
        a, b = _ct_and_mr(session)
        b.sop_instance_uid = a.sop_instance_uid
        del a.attributes[ABSENT]
        assert b.attributes.get(ABSENT) == "TOSHIBA"

        session.anonymize([_finding(a, "REMOVE_TAG", ABSENT, uid=a.sop_instance_uid)])

        assert _declined(session) == []
        assert a.phi_status is PhiStatus.REMEDIATED
        assert b.attributes.get(ABSENT) == "TOSHIBA"


# ---------------------------------------------------------------------------
# A path the first pass broke
# ---------------------------------------------------------------------------
#
# Review of #639 r3 (M1). Reading "nothing at the address" as a decline
# regressed a clean reuse: a nested removal whose sequence the first pass
# removed or emptied names an address that no longer resolves, and
# OBXXXX1A.dcm's second pass wrote 42 declines and graded REVIEW_REQUIRED
# on d7a2a3d, where 063ed59 graded PASS. The path is now walked as deep as
# it resolves; a break at a sequence is satisfied when that sequence is
# gone from the deepest live parent, or when no item it still holds holds
# the tag -- and never by reading the instance's own top level.


@pytest.mark.parametrize("mode", MODES, indirect=True)
def test_obxxxx1a_reused_writes_no_decline_and_grades_pass(tmp_path, mode):
    """The review's fixture: pydicom's `OBXXXX1A.dcm`, whose private
    sequences the default floor removes whole, `anonymize(report)` twice.
    Red on d7a2a3d: 42 declines on the second call, REVIEW_REQUIRED."""
    session = _session(tmp_path, ["OBXXXX1A.dcm"])
    with session:
        report = session.audit()
        nested = [f for f in _removes(report) if f.entity_path]
        assert len(nested) > 10, len(nested)
        session.anonymize(report)
        assert _declined(session) == [], mode

        session.anonymize(report)

        assert _declined(session) == [], mode
        assert _grade(session, tmp_path) == ["PASS"]


def _private_sequence(session, tmp_path):
    """A private sequence whose item holds private tags: the floor's sweep
    removes the nested tags, then the sequence."""
    (inst,) = _instances(session)
    item = DicomItem()
    item.set_attr("0029,0010", "PRIVCREATOR")
    item.set_attr("0029,1051", "PRIVATE-PHI-51")
    inst.add_sequence_item("0029,1050", item)
    return lambda: "0029,1050" not in inst.sequences


def _emptied_sequence(session, tmp_path):
    """`0008,1140: EMPTY` over an item holding `0010,1000` under
    `REMOVE`: the pass removes the nested tag, then empties the sequence,
    so the path's index is past a sequence of zero items."""
    (inst,) = _instances(session)
    item = DicomItem()
    item.set_attr("0008,1150", SOP_CLASS)
    item.set_attr("0008,1155", "1.2.3.4.5")
    item.set_attr("0010,1000", "OTHER-PID-123")
    inst.add_sequence_item("0008,1140", item)
    path = tmp_path / "cfg.yaml"
    path.write_text(json.dumps({"phi_tags": {
        "0008,1140": {"name": "r", "action": "EMPTY"},
        "0010,1000": {"name": "o", "action": "REMOVE"}}}), encoding="utf-8")
    session.load_config(str(path))
    return lambda: len(inst.sequences["0008,1140"].items) == 0


@pytest.mark.parametrize("build", [
    pytest.param(_private_sequence, id="private_sequence_removed"),
    pytest.param(_emptied_sequence, id="sequence_emptied"),
])
@pytest.mark.parametrize("mode", MODES, indirect=True)
def test_a_sequence_the_first_pass_detached_reuses_cleanly(tmp_path, build, mode):
    """The review's minimal shapes. Red on d7a2a3d: 2 declines on the
    private sequence, 1 on the emptied one, REVIEW_REQUIRED.

    Kills: a broken path read as a decline whatever the live parent holds."""
    session = _session(tmp_path, ["CT_small.dcm"])
    with session:
        detached = build(session, tmp_path)
        report = session.audit()
        assert [f for f in _removes(report) if f.entity_path], report.findings
        session.anonymize(report)
        assert detached()
        assert _declined(session) == [], mode

        session.anonymize(report)

        assert _declined(session) == [], mode
        assert _grade(session, tmp_path) == ["PASS"]


def test_a_nested_remove_never_reads_the_instance_top_level(tmp_path):
    """A nested finding under a sequence the instance no longer holds is
    satisfied -- nothing is at its address -- and the instance's own
    top-level element under the same tag is neither read nor removed: the
    finding never named it. Top-level Institution Name is held here, so
    reading the instance for the broken path would decline instead.

    Kills: a broken path read on the instance (review of #639 r3, `w1`, in
    the direction the shorter-sequence decline does not reach)."""
    session = _session(tmp_path, ["CT_small.dcm", "MR_small.dcm"])
    with session:
        ct, _ = _ct_and_mr(session)
        assert "0008,1140" not in ct.sequences
        assert ct.attributes.get(ABSENT) == "JFK IMAGING CENTER"
        finding = _finding(DicomItem(), "REMOVE_TAG", ABSENT,
                           uid=ct.sop_instance_uid, path=(("0008,1140", 0),))

        session.anonymize([finding])

        assert _declined(session) == []
        assert ct.attributes.get(ABSENT) == "JFK IMAGING CENTER"


def test_without_the_session_absence_is_read_on_the_finding_entity():
    """A service used without a session has no graph to resolve an
    address in, and reads the entity it was handed, as it always did --
    the direct tests above rest on this. Given the session's map, the
    object the map names is what is read: one still holding the tag, or
    no object at all, declines; a clean one is satisfied.

    Kills: the map's default meaning "resolved nothing" rather than "no
    session"; an unresolved address read as satisfied; the map ignored;
    the address's reason written on a decline the address did not cause."""
    rows = _Rows()
    alone = _instance()
    assert _service(rows).apply_remediation(
        [_finding(alone, "REMOVE_TAG", ABSENT)]) == 0
    assert rows.rows == [] and alone.phi_status is PhiStatus.REMEDIATED

    holding = _instance()
    holding.set_attr(ABSENT, "HOSP")
    for target in (holding, None):
        entity = _instance()
        finding = _finding(entity, "REMOVE_TAG", ABSENT)
        rows = _Rows()
        service = _service(rows)
        service._use_removal_targets({id(finding): target})
        assert service.apply_remediation([finding]) == 0
        assert [a for a, *_ in rows.rows] == ["REMEDIATION_DECLINED"], rows.rows
        assert STALE in rows.rows[0][2], rows.rows
        assert "HOSP" not in rows.rows[0][2]
        assert entity.phi_status is PhiStatus.IDENTIFIED
    assert holding.attributes[ABSENT] == "HOSP"

    # An entity that would decline on its own keeps its own reason: the
    # address did not change the answer, so the row does not blame it.
    finding = _finding(_Bare(), "REMOVE_TAG", ABSENT)
    rows = _Rows()
    service = _service(rows)
    service._use_removal_targets({id(finding): None})
    assert service.apply_remediation([finding]) == 0
    assert [a for a, *_ in rows.rows] == ["REMEDIATION_DECLINED"], rows.rows
    assert NO_ARM in rows.rows[0][2] and STALE not in rows.rows[0][2], rows.rows

    finding = _finding(_instance(), "REMOVE_TAG", ABSENT)
    rows = _Rows()
    service = _service(rows)
    service._use_removal_targets({id(finding): _instance()})
    assert service.apply_remediation([finding]) == 0
    assert rows.rows == []
