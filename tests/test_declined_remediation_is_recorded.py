"""A remediation that declined to run leaves a readable trace (#301).

`RemediationService._apply_single_remediation` has six ways to reach a
`return` without changing anything, and the value it was pointed at stays
in the graph and reaches the exported file. Five of them wrote a log
line and nothing else -- no audit row, so no compliance-report entry, no
grade movement, and nothing durable for anyone reading the store
afterwards. The sixth wrote **nothing at all**: the `REMOVE_TAG`
fall-through leaves `action_type` at `""`, skips the bottom block and
returns, so an instruction to remove a tag the entity does not carry
under that spelling vanished without a word.

Each declined path now writes one `REMEDIATION_DECLINED` audit row whose
`details` names the reason, `SqliteStore.get_audit_declines()` reads them
back, the compliance report lists them, and one of them takes the run to
`REVIEW_REQUIRED` -- the same argument `open_gaps` makes: the value is
still in the graph.

**`REMEDIATION_DECLINED` must stay out of `REMEDIATION_ACTION_TYPES`.**
That frozenset is the ANONYMIZE evidence set `generate_report` checks
(#254): if a decline counted as evidence, a run in which *every*
remediation declined would satisfy the check that exists to catch a run
whose remediation rows went missing. Pinned below.

The tests drive `_apply_single_remediation` directly against a real
`SqliteStore`, the pattern `test_remediation_actions.py` and
`test_remediation_invariants.py` use, because the point is which call
sites emit rather than which findings the inspector raises.

**Since #626 the sixth path is no longer a decline.** A `REMOVE_TAG`
whose tag is already gone from the item is the end state the rule asks
for -- #567's satisfied shape -- and is stamped REMEDIATED with no row;
`test_a_remove_on_an_absent_tag_is_satisfied.py` holds that rule. The
tests here that used that shape as *the* decline use `REPLACE_TAG` on
the absent tag instead, which #547 declines.
"""
import pytest

from isocenter.entities import Instance, Patient, PhiStatus
from isocenter.persistence import SqliteStore
from isocenter.privacy import PhiFinding, PhiRemediation
from isocenter.remediation import RemediationService, _remediation_key

from support.project_secret import FIXED_A

# `REMEDIATION_DECLINED` is imported inside the one test that needs it,
# not here. A module-level import of a name the unfixed tree does not
# have turns the whole file into a collection error, and an ImportError
# is not evidence of the defect -- every other test in this file has to
# be able to fail on its own assertion.

INSTANCE_SOP_CLASS = "1.2.840.10008.5.1.4.1.1.7"


class _Bare:
    """An entity with no `set_attr`, no `attributes` and no useful names.

    Truthy, so it passes the `if not entity` gate, and then matches none
    of the arms below it. This is the shape a `PhiFinding` carries when
    the object graph and the proposal disagree about what the target is.
    """


def _finding(entity, action, tag, new_value=None, original=None,
             metadata=None, uid="1.2.3"):
    return PhiFinding(
        entity_uid=uid, entity_type="Instance", field_name=tag,
        value=original, reason="test", tag=tag, entity=entity,
        remediation_proposal=PhiRemediation(
            action_type=action, target_attr=tag, new_value=new_value,
            original_value=original, metadata=metadata or {}))


@pytest.fixture
def store(tmp_path):
    backend = SqliteStore(str(tmp_path / "declines.db"))
    yield backend
    backend.stop()


def _declines(store, finding):
    """Apply one finding through a service wired to `store`, read the rows."""
    RemediationService(store_backend=store, project_secret=FIXED_A)._apply_single_remediation(finding)
    return store.get_audit_declines()


def test_a_finding_whose_entity_could_not_be_resolved_is_recorded(store):
    """#57's `_live_target -> None` arrives here as `finding.entity is None`.

    That is the deliberate outcome for a nested finding whose
    `entity_path` no longer resolves: remediation skips it rather than
    writing the nested tag onto the instance and fabricating a decoy.
    Skipping is right; skipping *silently* is what this fixes -- the PHI
    is still inside the sequence and the report said nothing.
    """
    finding = _finding(None, "REPLACE_TAG", "0010,0010", new_value="ANON")

    rows = _declines(store, finding)

    assert len(rows) == 1
    assert "entity" in rows[0][2].lower()


def test_a_replace_with_neither_a_setter_nor_the_attribute_is_recorded(store):
    """`REPLACE_TAG`'s `else` arm: the target has no way to take the value."""
    rows = _declines(store, _finding(_Bare(), "REPLACE_TAG", "patient_name",
                                     new_value="ANONYMIZED"))

    assert len(rows) == 1
    assert "patient_name" in rows[0][2]
    # The reason for a name the entity lacks, as distinct from the
    # reason for a slot holding None (#625): two reasons, one helper.
    assert "has no attribute or setter" in rows[0][2], rows[0][2]


def test_a_date_shift_with_no_resolvable_patient_id_is_recorded(store):
    """`SHIFT_DATE` needs a PatientID as the jitter seed, and had none.

    An `Instance` carries no `patient_id` and the proposal carries no
    metadata, so `_resolve_patient_id` returns None and the date is left
    exactly as it was -- unshifted, in a graph the report described as
    remediated.
    """
    inst = Instance("1.2.3", INSTANCE_SOP_CLASS, 1)
    inst.set_attr("0008,0020", "20230101")

    rows = _declines(store, _finding(inst, "SHIFT_DATE", "0008,0020",
                                     original="20230101"))

    assert len(rows) == 1
    assert "patientid" in rows[0][2].lower().replace(" ", "")


def test_an_unparseable_date_is_recorded(store):
    """`_shift_date_string` returned None over a non-empty value."""
    inst = Instance("1.2.3", INSTANCE_SOP_CLASS, 1)
    inst.set_attr("0008,0020", "not-a-date")

    rows = _declines(store, _finding(
        inst, "SHIFT_DATE", "0008,0020", original="not-a-date",
        metadata={"patient_id": "PAT-7"}))

    assert len(rows) == 1
    assert "not-a-date" in rows[0][2]


def test_a_remove_of_a_tag_the_instance_does_not_carry_is_satisfied(store):
    """The path that had no log line at all, and is now not a decline.

    `hasattr(entity, "attributes")` is True and the tag is in neither
    `attributes` nor `sequences`, so both inner arms fail, `action_type`
    stays `""` and the bottom `else` is reached. #301 made that a
    `matched no applicable arm` decline, so it was no longer silent; #626
    made it satisfied, because the tag being gone is the end state the
    rule asks for: no row, REMEDIATED, the key counted as handled,
    nothing applied.

    Asserted through `_remediation_key`, not a literal tuple:
    `entity_path` defaults to `()`, and a hardcoded tuple would pin the
    fixture's default rather than the contract.
    """
    inst = Instance("1.2.3", INSTANCE_SOP_CLASS, 1)
    inst.set_attr("0010,0010", "DOE^JOHN")
    inst.record_phi_status(PhiStatus.IDENTIFIED)
    finding = _finding(inst, "REMOVE_TAG", "0008,0080")
    service = RemediationService(store_backend=store, project_secret=FIXED_A)

    applied = service.apply_remediation([finding])
    store.flush_audit_queue()

    assert applied == 0
    assert store.get_audit_declines() == []
    assert inst.phi_status is PhiStatus.REMEDIATED
    assert _remediation_key(finding) in service._satisfied_keys


@pytest.mark.parametrize("new_value", ["", "ANONYMIZED"],
                         ids=["EMPTY", "REPLACE"])
def test_a_replace_of_a_tag_the_instance_no_longer_carries_is_recorded(
        store, new_value):
    """`REMOVE_TAG`'s decline, for the value-writing half (#547).

    The audit saw the tag; it was gone before the remediation ran. Red
    before: `set_attr` wrote the replacement anyway, fabricating an
    element the graph did not hold -- #57's decoy at the top level -- and
    the row said `REMEDIATION_REPLACE`. #547 moved 13 basic rules from
    `REMOVE`, which declined here, to `EMPTY`, which did not, and added
    141 more `EMPTY` rules. `REPLACE` shares the arm and the defect.

    Kills: the absent-target decline removed from the `REPLACE_TAG` arm.
    """
    inst = Instance("1.2.3", INSTANCE_SOP_CLASS, 1)
    inst.set_attr("0010,0010", "DOE^JOHN")

    changed = RemediationService(
        store_backend=store, project_secret=FIXED_A)._apply_single_remediation(
            _finding(inst, "REPLACE_TAG", "0008,1010", new_value=new_value,
                     original="STATION-7"))
    rows = store.get_audit_declines()

    assert changed is False
    assert "0008,1010" not in inst.attributes
    assert len(rows) == 1
    assert "0008,1010" in rows[0][2]


def test_a_value_aimed_at_a_sequence_is_recorded(store):
    """A hand-built `REPLACE_TAG` with a value, aimed at a tag the item
    holds as a sequence. The scan never proposes one -- it warns instead
    (#547) -- and there is no value to write into a sequence, so it
    declines rather than setting an attribute beside it.

    Kills: the sequence case of the absent-target decline writing the
    value anyway."""
    from isocenter.entities import DicomItem
    inst = Instance("1.2.3", INSTANCE_SOP_CLASS, 1)
    inst.add_sequence_item("0040,0275", DicomItem())

    changed = RemediationService(
        store_backend=store, project_secret=FIXED_A)._apply_single_remediation(
            _finding(inst, "REPLACE_TAG", "0040,0275", new_value="ANONYMIZED"))
    rows = store.get_audit_declines()

    assert changed is False
    assert "0040,0275" not in inst.attributes
    assert len(inst.sequences["0040,0275"].items) == 1
    assert len(rows) == 1
    assert "sequence" in rows[0][2]


def test_a_remove_against_an_entity_with_no_attributes_dict_is_recorded(store):
    """The second `REMOVE_TAG` fall-through shape, which the plan missed.

    The Python-attribute arm is an `elif` on the outer `if hasattr(entity,
    "attributes")`, so an entity with *no* `attributes` dict **and** no
    matching Python attribute falls past both and reaches the same silent
    return. One `else` on the bottom `if action_type:` covers both shapes;
    an `else` nested inside the `attributes` arm would have covered only
    the first.
    """
    rows = _declines(store, _finding(_Bare(), "REMOVE_TAG", "patient_id"))

    assert len(rows) == 1
    assert "patient_id" in rows[0][2]
    # Through `apply_remediation` a dropped `attributes`-dict guard in
    # the satisfied test (#626) turns this row into `REMOVE_TAG on
    # patient_id raised TypeError ...`, which also names the attribute;
    # the wording is what tells the two apart.
    assert "matched no applicable arm" in rows[0][2], rows[0][2]


def test_an_empty_date_is_not_a_decline(store):
    """The one non-success path that deliberately writes no row.

    An empty value is not retained PHI: there is nothing there to shift
    and nothing left behind. A row here would be the cry-wolf shape --
    `REVIEW_REQUIRED` over a graph with nothing wrong in it.
    """
    inst = Instance("1.2.3", INSTANCE_SOP_CLASS, 1)

    rows = _declines(store, _finding(
        inst, "SHIFT_DATE", "0008,0020", original="",
        metadata={"patient_id": "PAT-7"}))

    assert rows == []


def test_two_findings_sharing_a_target_write_two_declines(store):
    """Accepted, and stated so it reads as decided rather than as drift.

    `apply_remediation`'s `processed_entities` set is added to only on
    success, so two findings with the same dedup key both reach
    `_apply_single_remediation` and both decline. Two findings are two
    declines. Folding them would mean adding the key on failure too,
    which would let a successful second attempt be skipped because a
    first one declined.
    """
    inst = Instance("1.2.3", INSTANCE_SOP_CLASS, 1)
    findings = [_finding(inst, "REPLACE_TAG", "0008,0080", new_value="ANONYMIZED"),
                _finding(inst, "REPLACE_TAG", "0008,0080", new_value="ANONYMIZED")]

    applied = RemediationService(store_backend=store)\
        .apply_remediation(findings)

    assert len(store.get_audit_declines()) == 2
    # The other half of the same bool, and it needs its own assertion:
    # the row count above catches a decline that returned True by way of
    # the dedup key, but `apply_remediation`'s return is what
    # `anonymize()` prints ("Anonymized N tags according to policy") and
    # what its docstring calls "a count of what actually changed". A
    # decline changed nothing.
    assert applied == 0, (
        "a declined remediation was counted as applied, so anonymize() "
        "reports removing a value that is still in the graph")


class _Explodes:
    """An entity whose setter raises, so its finding reaches the `except`."""

    def set_attr(self, *_args, **_kwargs):
        raise RuntimeError("entity is read-only")


def test_the_failure_warning_counts_declines_among_the_attempts(store,
                                                                caplog):
    """The failure warning divides by what was tried, declines included.

    The warning used to read `failures + len(processed_entities)`, which
    equalled the attempt count only while a decline was added to
    `processed_entities`. Moving the key to the success arm made that
    sum stop counting declines, so a run that tried two and failed one
    reported "1 of 1" -- the operator's only summary line, saying every
    remediation it attempted had failed. The attempts are counted
    directly now.
    """
    inst = Instance("1.2.3", INSTANCE_SOP_CLASS, 1)
    findings = [_finding(_Explodes(), "REPLACE_TAG", "0010,0010",
                         new_value="ANON", uid="1.2.4"),
                _finding(inst, "REPLACE_TAG", "0008,0080", new_value="ANONYMIZED")]

    with caplog.at_level("WARNING"):
        RemediationService(store_backend=store).apply_remediation(findings)

    summary = [r.message for r in caplog.records
               if "remediations failed" in r.message]
    assert summary, "the run failed one remediation and said nothing"
    assert "1 of 2 remediations failed" in summary[0], summary[0]


def test_declines_are_not_anonymisation_evidence():
    """`REMEDIATION_DECLINED` must never satisfy #254's evidence check.

    `generate_report` asks whether the audit log heard about what this
    session *did*: an ANONYMIZE verb must find one of
    `REMEDIATION_ACTION_TYPES` in the summary. A decline is the opposite
    of evidence -- it says the value is still there -- so a run whose
    every remediation declined must not grade PASS on the strength of its
    decline rows.
    """
    from isocenter.remediation import (REMEDIATION_ACTION_TYPES,
                                       REMEDIATION_DECLINED)

    assert REMEDIATION_DECLINED not in REMEDIATION_ACTION_TYPES


def test_a_declined_session_reports_the_decline_and_grades_review_required(
        tmp_path):
    """End to end: the row reaches the report, and the report reaches a grade.

    Built with a `Patient` whose `patient_name` is remediated
    successfully -- so `audit_summary` is non-empty and the ANONYMIZE
    evidence check is satisfied -- beside a finding that declines. Without
    the success the report would grade `REVIEW_REQUIRED` on an empty
    audit summary and the test would pass without the fix.
    """
    from isocenter.session import DicomSession

    session = DicomSession(str(tmp_path / "report.db"))
    try:
        patient = Patient("PAT-7", "DOE^JOHN")
        session.store.patients.append(patient)

        inst = Instance("1.2.3", INSTANCE_SOP_CLASS, 1)
        service = RemediationService(store_backend=session.store_backend)
        service.apply_remediation([
            _finding(patient, "REPLACE_TAG", "patient_name",
                     new_value="ANONYMIZED", original="DOE^JOHN"),
            _finding(inst, "REPLACE_TAG", "0008,0080", new_value="ANONYMIZED"),
        ])
        session._actions_performed.add("ANONYMIZE")

        out = tmp_path / "report.md"
        session.generate_report(str(out))
        content = out.read_text(encoding="utf-8")
    finally:
        session.close()

    assert "Declined Remediations" in content
    assert "0008,0080" in content.split("Declined Remediations", 1)[1]
    assert "**REVIEW_REQUIRED**" in content, (
        "a value the pipeline was told to remove and did not remove is "
        "still in the graph, and the report graded the run PASS")


def test_a_report_with_no_declines_renders_no_decline_section(tmp_path):
    """The empty case renders nothing, not an empty header.

    Unlike 3.1 and 3.2, whose empty prose is load-bearing for the
    bounded slices two other test files take. Nothing slices on 3.3, and
    a header asserting "the following remediations declined" over no rows
    is a claim about a run that had none.
    """
    from isocenter.session import DicomSession

    session = DicomSession(str(tmp_path / "clean.db"))
    try:
        patient = Patient("PAT-7", "DOE^JOHN")
        session.store.patients.append(patient)
        RemediationService(store_backend=session.store_backend)\
            .apply_remediation([
                _finding(patient, "REPLACE_TAG", "patient_name",
                         new_value="ANONYMIZED", original="DOE^JOHN")])

        out = tmp_path / "report.md"
        session.generate_report(str(out))
        content = out.read_text(encoding="utf-8")
    finally:
        session.close()

    assert "Declined Remediations" not in content
    assert "## 4. Exceptions & Errors" in content, (
        "section 4 must keep its number; three test files assert it")


def test_a_decline_is_not_a_phi_status_change(store):
    """A declined entity is not `REMEDIATED`, and must not claim to be.

    `record_phi_status(PhiStatus.REMEDIATED)` lives inside the success
    block. The decline path must not reach it: the value is still there,
    so the entity's own status has to keep saying so.
    """
    inst = Instance("1.2.3", INSTANCE_SOP_CLASS, 1)
    before = inst.phi_status

    _declines(store, _finding(inst, "REPLACE_TAG", "0008,0080",
                              new_value="ANONYMIZED"))

    assert inst.phi_status is before


def test_a_pass_that_only_declined_leaves_the_status_alone(store):
    """Through `apply_remediation`, where the test above drives
    `_apply_single_remediation` directly and so never reaches the pass end.

    The pass-end demotion (#486, from the review of #491) re-records
    IDENTIFIED on an entity that declined and still ended the pass
    REMEDIATED. An entity that only declined ended it as it began and is
    not touched: recording IDENTIFIED over UNSCANNED here would call a
    never-scanned instance identified on the strength of a proposal that
    did not run.

    Kills: the demotion applied to every entity that declined, whatever
    status it ended the pass with.
    """
    inst = Instance("1.2.3", INSTANCE_SOP_CLASS, 1)
    before = inst.phi_status

    RemediationService(store_backend=store).apply_remediation(
        [_finding(inst, "REPLACE_TAG", "0008,0080", new_value="ANONYMIZED")])
    store.flush_audit_queue()

    assert len(store.get_audit_declines()) == 1
    assert inst.phi_status is before


# ---------------------------------------------------------------------------
# #553: a proposal that raised is a decline
# ---------------------------------------------------------------------------

class _ExplodesWithAPipe:
    """A setter whose exception text would break a markdown table row."""

    def set_attr(self, *_args, **_kwargs):
        raise RuntimeError("a\n|b")


def test_a_proposal_that_raised_writes_a_declined_row(store):
    """The `except` arm used to count and log, and write nothing durable.

    A raise may have left the value unchanged or partly written, and
    either way the pipeline was told to remove it and cannot say it did:
    the same claim a decline makes, so the same row (owner ruling Q2).
    The details are flattened and pipe-escaped because section 3.3 of the
    report renders them into a table cell.

    Kills: the `except` arm without `_record_decline`; the flattening
    dropped.
    """
    inst = Instance("1.2.3", INSTANCE_SOP_CLASS, 1)
    inst.attributes["0008,0080"] = "St Elsewhere"
    findings = [_finding(_ExplodesWithAPipe(), "REPLACE_TAG", "0010,0010",
                         new_value="ANON", uid="1.2.4"),
                _finding(inst, "REMOVE_TAG", "0008,0080")]

    applied = RemediationService(store_backend=store).apply_remediation(
        findings)
    declines = store.get_audit_declines()

    assert applied == 1
    assert len(declines) == 1, declines
    _ts, uid, details = declines[0]
    assert uid == "1.2.4"
    assert "REPLACE_TAG on 0010,0010 raised" in details, details
    assert "RuntimeError" in details, details
    assert "unchanged or partly written" in details, details
    assert "\n" not in details, details
    assert "|" not in details.replace("\\|", ""), details


@pytest.mark.parametrize("mode", ["threads", "processes"])
def test_a_proposal_that_raised_demotes_its_entity(tmp_path, monkeypatch,
                                                   mode):
    """s3: one finding of a full report raises; the instance is not REMEDIATED.

    Measured on ac33641: the instance read REMEDIATED with the raised
    tag still holding its value, zero rows of any kind, and PASS.

    Kills: the raised entity not named for the pass-end demotion.
    """
    import json

    from isocenter.entities import PhiStatus
    from isocenter.session import DicomSession
    from support.ct_small_files import write_ct

    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)
    if mode == "threads":
        monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
        monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    else:
        monkeypatch.setenv("ISOCENTER_FORCE_PROCESSES", "1")
        monkeypatch.delenv("ISOCENTER_FORCE_THREADS", raising=False)

    src = tmp_path / "src"
    write_ct(str(src / "ct.dcm"), "PAT-553", "1")
    session = DicomSession(persistence_file=str(tmp_path / "raise.db"))
    try:
        session.ingest(str(src))
        report = session.audit()
        instance = session.store.patients[0].studies[0].series[0].instances[0]
        value = instance.attributes["0008,0080"]

        real = RemediationService._apply_single_remediation

        def _raise_on_one(self, finding, audit_buffer=None):
            if finding.entity is instance and finding.tag == "0008,0080":
                raise RuntimeError("disk full")
            return real(self, finding, audit_buffer)

        monkeypatch.setattr(RemediationService, "_apply_single_remediation",
                            _raise_on_one)
        session.anonymize(report)

        manifest = tmp_path / "manifest.json"
        session.generate_manifest(str(manifest), format="json")
        items = json.loads(manifest.read_text(encoding="utf-8"))["items"]
        out = tmp_path / "report.md"
        session.generate_report(str(out))
        content = out.read_text(encoding="utf-8")
    finally:
        session.close()

    assert instance.attributes["0008,0080"] == value, "setup: it raised"
    assert instance.phi_status is PhiStatus.IDENTIFIED
    assert [item["anonymized"] for item in items] == [False], items
    assert "**REVIEW_REQUIRED**" in content, content
    declined = content.split("Declined Remediations", 1)[1]
    assert "raised RuntimeError" in declined, declined


def test_a_proposal_that_raised_demotes_its_entity_with_no_audit(store):
    """The same without a session: no scan tally, so the decline alone
    has to take the instance back from REMEDIATED.

    Kills: the raised entity not named for the pass-end demotion (with an
    audit behind the pass, the tally would demote it anyway).
    """
    from isocenter.entities import PhiStatus

    class _RaisesOnName(Instance):
        __slots__ = ()

        def set_attr(self, tag, value):
            if tag == "0008,0080":
                raise RuntimeError("disk full")
            super().set_attr(tag, value)

    inst = _RaisesOnName("1.2.3", INSTANCE_SOP_CLASS, 1)
    inst.attributes["0008,0080"] = "St Elsewhere"
    inst.attributes["0008,0090"] = "Dr Who"
    findings = [_finding(inst, "REPLACE_TAG", "0008,0090", new_value=""),
                _finding(inst, "REPLACE_TAG", "0008,0080", new_value="")]

    applied = RemediationService(store_backend=store).apply_remediation(
        findings)

    assert applied == 1
    assert inst.attributes["0008,0080"] == "St Elsewhere", "setup: it raised"
    assert inst.phi_status is PhiStatus.IDENTIFIED


def test_a_raise_on_an_entity_without_status_does_not_crash_the_pass(store):
    """`_Explodes` has no `phi_status`; naming it for demotion must not raise.

    Kills: the settle reading `entity.phi_status` without a guard.
    """
    applied = RemediationService(store_backend=store).apply_remediation(
        [_finding(_Explodes(), "REPLACE_TAG", "0010,0010", new_value="ANON")])

    assert applied == 0
    assert len(store.get_audit_declines()) == 1
