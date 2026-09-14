"""A configured rule on a sequence tag does what it says (#547).

Until 0.9.8 the configured-rule loop in `PhiInspector._scan_instance`
looked a tag up only for the keys of `item.attributes`. A sequence lives
in `item.sequences`, so a `REMOVE` or `EMPTY` on one raised no finding,
the instance read CLEARED, and the sequence reached the export intact
(measured: `0040,0275` under `REMOVE` and `0008,1110` under `EMPTY`, both
exported with their items). That was true of every user rule on a
sequence, and it would have been true of the 56 sequence rows PS3.15
Table E.1-1 gives the basic profile. Only the private-sequence sweep
(#167) ever asked about `sequences`.

What each rule now means on a sequence:

- `REMOVE` deletes it, through the arm #167 added for private sequences.
- `EMPTY` leaves it present with zero items. A zero-item sequence is how
  a Type 2 sequence says "no value", and it is what Annex E's `Z` means.
- `KEEP` does nothing, silently.
- `REPLACE`, `SHIFT` and `JITTER` have no meaning on a sequence: one
  warning per tag and action per inspector -- so per patient per audit,
  since each patient's scan task builds its own -- and no finding.
- A target gone between audit and anonymize declines, as `REMOVE` does.

Both findings join the deepest-first container list, for #167's reason:
a nested finding has to be remediated before its container goes, or its
audit row describes an item that is no longer in the graph.

Isocenter's own redaction note in Derivation Description `(0008,2111)` is
exempt from a rule on that tag, and only that exact value is. The table
removes the tag; without the exemption `export(check_burned_in=True)`
would skip every redacted instance.
"""
import logging
import os
import shutil
import sqlite3

import numpy as np
import pydicom
import pydicom.data
import pytest
from pydicom.dataset import Dataset
from pydicom.sequence import Sequence

from isocenter import Session
from isocenter.entities import (DicomItem, Equipment, Instance, Patient,
                                PhiStatus, Series, Study, iter_item_tree)
from isocenter.io_handlers import populate_attrs
from isocenter.privacy import PhiInspector
from isocenter.remediation import RemediationService

REQUEST_ATTRIBUTES = "0040,0275"        # X in Table E.1-1
REFERENCED_STUDY = "0008,1110"          # X/Z
VERIFYING_OBSERVER = "0040,a073"        # D (a sequence)
VERIFYING_OBSERVER_CODE = "0040,a088"   # Z, nested inside the one above
DERIVATION_DESCRIPTION = "0008,2111"


def _rule(action, name="Configured sequence"):
    return {"action": action, "name": name}


def _instance_from(ds, uid="1.2.826.0.1.547.1"):
    instance = Instance(uid, "1.2.840.10008.5.1.4.1.1.2", 1)
    populate_attrs(ds, instance)
    return instance


def _request_attributes_dataset():
    ds = Dataset()
    item = Dataset()
    item.add_new(0x00401001, "SH", "RP-777")      # Requested Procedure ID
    ds.add_new(0x00400275, "SQ", Sequence([item]))
    return ds


def _findings_for(findings, tag):
    return [f for f in findings if f.tag == tag]


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------

def test_a_configured_rule_on_a_sequence_raises_a_finding():
    """Red before: no finding at all, because the configured loop read
    only `item.attributes`.

    Kills: the configured-sequence loop deleted."""
    instance = _instance_from(_request_attributes_dataset())
    inspector = PhiInspector(
        config_tags={REQUEST_ATTRIBUTES: _rule("REMOVE")},
        remove_private_tags=False)

    found = _findings_for(inspector._scan_instance(instance, "P1"),
                          REQUEST_ATTRIBUTES)

    assert len(found) == 1, found
    proposal = found[0].remediation_proposal
    assert proposal.action_type == "REMOVE_TAG"
    assert proposal.target_attr == REQUEST_ATTRIBUTES
    assert found[0].entity is instance
    assert found[0].entity_path == ()


def test_keep_on_a_sequence_raises_nothing(caplog):
    """KEEP is what the docs and the CHANGELOG tell a user to write to
    retain a sequence, so it must be silent as well as finding-free: a
    WARNING there would print "has no meaning on a sequence" for every
    patient of every audit, at the user who followed the advice.

    Kills: the KEEP exclusion dropped from the warn-once condition (the
    review's M10, which survived an assertion on findings alone)."""
    instance = _instance_from(_request_attributes_dataset())
    inspector = PhiInspector(
        config_tags={REQUEST_ATTRIBUTES: _rule("KEEP")},
        remove_private_tags=False)

    with caplog.at_level(logging.DEBUG, logger="isocenter"):
        findings = inspector._scan_instance(instance, "P1")

    assert not _findings_for(findings, REQUEST_ATTRIBUTES)
    warnings = [r.getMessage() for r in caplog.records
                if r.levelno >= logging.WARNING
                and REQUEST_ATTRIBUTES in r.getMessage()]
    assert warnings == []


@pytest.mark.parametrize("rule", [_rule("REPLACE"), _rule("JITTER"),
                                  _rule("SHIFT"), "Display name only"])
def test_replace_or_jitter_on_a_sequence_warns_and_raises_nothing(rule, caplog):
    """A value-writing action has no meaning on a sequence. Once per tag
    and action per inspector, however many instances carry it, so a large
    audit does not print the same sentence per instance. The string form
    is a display name and leaves the action at REPLACE, so it warns too.

    Kills: the fallthrough arm raising a `REPLACE_TAG` (which would write
    `ANONYMIZED` beside the untouched sequence), and the warning dropped
    or repeated per instance."""
    inspector = PhiInspector(config_tags={REQUEST_ATTRIBUTES: rule},
                             remove_private_tags=False)
    first = _instance_from(_request_attributes_dataset(), "1.2.826.0.1.547.1")
    second = _instance_from(_request_attributes_dataset(), "1.2.826.0.1.547.2")

    with caplog.at_level(logging.WARNING, logger="isocenter"):
        findings = (inspector._scan_instance(first, "P1")
                    + inspector._scan_instance(second, "P1"))

    assert not _findings_for(findings, REQUEST_ATTRIBUTES)
    warnings = [r for r in caplog.records
                if r.levelno == logging.WARNING and REQUEST_ATTRIBUTES in r.getMessage()]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert len(first.sequences[REQUEST_ATTRIBUTES].items) == 1


@pytest.mark.parametrize("remove_private", [True, False])
def test_private_sequence_rule_is_not_raised_twice(remove_private):
    """An odd-group sequence named in the policy is swept once when the
    private sweep is on, and raised by its rule when it is off.

    Kills: the odd-group `continue` removed (two findings under the
    sweep), and the `continue` made unconditional (none without it)."""
    ds = Dataset()
    ds.add_new(0x00090010, "LO", "ACME")
    item = Dataset()
    item.add_new(0x00091001, "LO", "vendor value")
    ds.add_new(0x00091010, "SQ", Sequence([item]))
    instance = _instance_from(ds)
    inspector = PhiInspector(config_tags={"0009,1010": _rule("REMOVE")},
                             remove_private_tags=remove_private)

    found = _findings_for(inspector._scan_instance(instance, "P1"), "0009,1010")

    assert len(found) == 1, found
    assert found[0].remediation_proposal.action_type == "REMOVE_TAG"


def _nested_observer_dataset():
    """Verifying Observer Identification Code Sequence inside Verifying
    Observer Sequence, with a name beside it."""
    ds = Dataset()
    code = Dataset()
    code.add_new(0x00080100, "SH", "12345")
    code.add_new(0x00080102, "SH", "LOCAL")
    code.add_new(0x00080104, "LO", "Dr Observer")
    observer = Dataset()
    observer.add_new(0x0040A075, "PN", "Observer^Verifying")
    observer.add_new(0x0040A088, "SQ", Sequence([code]))
    ds.add_new(0x0040A073, "SQ", Sequence([observer]))
    return ds


def test_sequence_findings_come_innermost_first_without_the_private_sweep():
    """The deepest-first sort used to sit inside `if remove_private_tags`,
    where only private-sequence findings existed. A configured container
    inside a configured container needs the same order with the sweep off.

    Kills: the sort left inside the private block, or deleted."""
    instance = _instance_from(_nested_observer_dataset())
    inspector = PhiInspector(
        config_tags={VERIFYING_OBSERVER: _rule("REMOVE"),
                     VERIFYING_OBSERVER_CODE: _rule("REMOVE")},
        remove_private_tags=False)

    tags = [f.tag for f in inspector._scan_instance(instance, "P1")
            if f.tag in (VERIFYING_OBSERVER, VERIFYING_OBSERVER_CODE)]

    assert tags == [VERIFYING_OBSERVER_CODE, VERIFYING_OBSERVER]


def test_private_sequences_come_innermost_first_with_no_policy():
    """The other return. With an empty policy (`privacy_profile: none` and
    no tags) the scan returns before the configured loops, and the sweep's
    findings still need #167's order there.

    Kills: the sort dropped from the early return only."""
    ds = Dataset()
    ds.add_new(0x00090010, "LO", "ACME")
    inner = Dataset()
    inner.add_new(0x00091001, "LO", "vendor value")
    outer = Dataset()
    outer.add_new(0x00090010, "LO", "ACME")
    outer.add_new(0x00091011, "SQ", Sequence([inner]))
    ds.add_new(0x00091010, "SQ", Sequence([outer]))
    instance = _instance_from(ds)
    inspector = PhiInspector(config_tags={}, remove_private_tags=True)

    tags = [f.tag for f in inspector._scan_instance(instance, "P1")
            if f.tag in ("0009,1010", "0009,1011")]

    assert tags == ["0009,1011", "0009,1010"]


def test_empty_on_a_zero_item_sequence_raises_nothing():
    """The scan half of "a re-audit of a zeroed sequence is clear", at the
    unit level: the `EMPTY` arm tests for items the way the attribute arm
    tests `val != ""`.

    Kills: the items guard removed."""
    ds = Dataset()
    ds.add_new(0x00081110, "SQ", Sequence([]))
    instance = _instance_from(ds)
    assert REFERENCED_STUDY in instance.sequences, "setup: zero-item SQ ingested"
    inspector = PhiInspector(config_tags={REFERENCED_STUDY: _rule("EMPTY")},
                             remove_private_tags=False)

    assert not _findings_for(inspector._scan_instance(instance, "P1"),
                             REFERENCED_STUDY)


# ---------------------------------------------------------------------------
# Remediation
# ---------------------------------------------------------------------------

def test_clearing_a_sequence_advances_the_revision_only_when_it_clears():
    """`DicomItem.clear_sequence_items` is `add_sequence`'s rule applied
    the other way: a change the store must hold advances the revision, and
    a call that changes nothing does not.

    Kills: the `mark_modified()` in it deleted, and one made unconditional."""
    item = DicomItem()
    item.add_sequence_item(REFERENCED_STUDY, DicomItem())
    item.mark_persisted()

    assert item.clear_sequence_items(REFERENCED_STUDY) is True
    assert item.sequences[REFERENCED_STUDY].items == []
    assert item.has_unsaved_changes

    item.mark_persisted()
    assert item.clear_sequence_items(REFERENCED_STUDY) is False
    assert not item.has_unsaved_changes


def _empty_finding(entity, tag):
    from isocenter.privacy import PhiFinding, PhiRemediation
    return PhiFinding(
        entity_uid=entity.sop_instance_uid, entity_type="Instance",
        field_name=tag, value="<SEQUENCE>", reason="test", tag=tag,
        entity=entity,
        remediation_proposal=PhiRemediation(
            action_type="REPLACE_TAG", target_attr=tag, new_value=""))


def test_emptying_a_sequence_after_a_reload_still_needs_a_save():
    """The `_as_reloaded` shape from `tests/test_remediation_invariants.py`:
    an instance already REMEDIATED and saved, so the status recorded at the
    end of this remediation short-circuits and the clear is the only thing
    left to advance the revision.

    Kills: the REPLACE_TAG sequence branch deleted (`set_attr` writes a
    `""` attribute beside the untouched sequence), and
    `clear_sequence_items` not advancing the revision."""
    inst = Instance("1.2.3", "1.2.840.10008.5.1.4.1.1.7", 1)
    inst.add_sequence_item(REFERENCED_STUDY, DicomItem())
    inst.record_phi_status(PhiStatus.REMEDIATED)
    inst.mark_persisted()
    assert not inst.has_unsaved_changes, "setup: starts saved"

    applied = RemediationService().apply_remediation(
        [_empty_finding(inst, REFERENCED_STUDY)])

    assert applied == 1
    assert inst.sequences[REFERENCED_STUDY].items == []
    assert REFERENCED_STUDY not in inst.attributes
    assert inst.has_unsaved_changes, (
        "the sequence was emptied in memory but the instance still looks "
        "saved, so the next save skips it and the items stay in the store")


# ---------------------------------------------------------------------------
# Through a session, to the written file
# ---------------------------------------------------------------------------

def _ct_small_with(folder, mutate):
    ds = pydicom.dcmread(pydicom.data.get_testdata_file("CT_small.dcm"))
    mutate(ds)
    os.makedirs(folder, exist_ok=True)
    ds.save_as(os.path.join(folder, "ct.dcm"))
    return ds


def _written(folder):
    paths = [os.path.join(root, name) for root, _, names in os.walk(folder)
             for name in names if name.endswith(".dcm")]
    assert len(paths) == 1, paths
    return pydicom.dcmread(paths[0])


def _rows(db_path, action_type):
    with sqlite3.connect(db_path) as conn:
        return [row[0] for row in conn.execute(
            "SELECT details FROM audit_log WHERE action_type = ?",
            (action_type,))]


def test_a_removed_sequence_is_absent_from_the_export(tmp_path):
    """Red before: the sequence exported with its item.

    Kills: the configured-sequence loop deleted, and the sequence branch
    of the `REMOVE_TAG` arm deleted (the finding is filed and declined,
    and the exporter writes the sequence)."""
    def add(ds):
        ds.update(_request_attributes_dataset())
    _ct_small_with(str(tmp_path / "in"), add)
    db = str(tmp_path / "s.db")

    with Session(db) as session:
        session.configuration.phi_tags[REQUEST_ATTRIBUTES] = _rule(
            "REMOVE", "Request Attributes Sequence")
        session.ingest(str(tmp_path / "in"))
        session.anonymize(session.audit())
        summary = session.export(str(tmp_path / "out"), use_compression=False)
        session.save(sync=True)

    assert summary.written == 1, summary.failures
    assert (0x0040, 0x0275) not in _written(str(tmp_path / "out"))
    assert [d for d in _rows(db, "REMEDIATION_REMOVE")
            if f"Removed Sequence {REQUEST_ATTRIBUTES}" in d], \
        _rows(db, "REMEDIATION_REMOVE")


def test_a_configured_sequence_inside_a_configured_sequence_is_removed_innermost_first(
        tmp_path, monkeypatch):
    """Every removal's entity is still reachable from its instance at the
    moment it is applied -- the claim #167's ordering exists to keep --
    and with the private sweep off, where the old sort did not run.

    Kills: the sort left inside `if remove_private_tags` (the outer
    sequence is removed first and the inner removal acts on a detached
    item)."""
    def add(ds):
        ds.update(_nested_observer_dataset())
    _ct_small_with(str(tmp_path / "in"), add)

    unreachable = []
    original = RemediationService._apply_single_remediation

    def spy(self, finding, audit_buffer=None):
        if finding.tag in (VERIFYING_OBSERVER, VERIFYING_OBSERVER_CODE):
            instance = session.store.patients[0].studies[0].series[0].instances[0]
            if not any(item is finding.entity
                       for item, _ in iter_item_tree(instance)):
                unreachable.append(finding.tag)
        return original(self, finding, audit_buffer)

    monkeypatch.setattr(RemediationService, "_apply_single_remediation", spy)

    with Session(str(tmp_path / "s.db")) as session:
        session.configuration.remove_private_tags = False
        session.configuration.phi_tags[VERIFYING_OBSERVER] = _rule("REMOVE")
        session.configuration.phi_tags[VERIFYING_OBSERVER_CODE] = _rule("REMOVE")
        session.ingest(str(tmp_path / "in"))
        session.anonymize(session.audit())
        instance = session.store.patients[0].studies[0].series[0].instances[0]
        assert VERIFYING_OBSERVER not in instance.sequences

    assert unreachable == []


def test_empty_on_a_sequence_writes_a_zero_item_sequence_and_no_attribute(tmp_path):
    """Red before: no finding, and the sequence exported with its item.

    The written file is read back, because nothing else checks that the
    exporter writes a present, zero-item SQ for an emptied one.

    Kills: the `REPLACE_TAG` sequence branch deleted (a `""` attribute is
    written beside the untouched sequence)."""
    def add(ds):
        ref = Dataset()
        ref.add_new(0x00081150, "UI", "1.2.840.10008.3.1.2.3.1")
        ref.add_new(0x00081155, "UI", "1.2.826.0.1.547.99")
        ds.add_new(0x00081110, "SQ", Sequence([ref]))
    _ct_small_with(str(tmp_path / "in"), add)

    with Session(str(tmp_path / "s.db")) as session:
        session.configuration.phi_tags[REFERENCED_STUDY] = _rule(
            "EMPTY", "Referenced Study Sequence")
        session.ingest(str(tmp_path / "in"))
        session.anonymize(session.audit())
        instance = session.store.patients[0].studies[0].series[0].instances[0]
        assert instance.sequences[REFERENCED_STUDY].items == []
        assert REFERENCED_STUDY not in instance.attributes
        summary = session.export(str(tmp_path / "out"), use_compression=False)

    assert summary.written == 1, summary.failures
    out = _written(str(tmp_path / "out"))
    assert (0x0008, 0x1110) in out
    assert out[0x0008, 0x1110].VR == "SQ"
    assert len(out[0x0008, 0x1110].value) == 0


def test_a_nested_emptied_sequence_reaches_the_store_after_a_reload(tmp_path):
    """The nested form of the reload test above, through a real store: an
    instance a first pass left REMEDIATED and saved is reopened, audited
    under an EMPTY rule on a sequence *inside* a sequence item, saved,
    anonymized, saved and reopened. The clear moves only the nested item,
    and the instance's REMEDIATED stamp short-circuits, so the instance
    must be marked modified by the remediation's owner hook (#494) or the
    save skips it and the items come back on reopen.

    Red on #547 alone (one item back after reopen; the review measured the
    same) and green with #561's owner hook. Here the owner's REMEDIATED
    stamp is a status change -- the audit left the instance IDENTIFIED --
    so the stamp alone advances the revision; the hook's
    `mark_modified()` for an owner already REMEDIATED is #561's tests' to
    hold.

    Kills: the owner hook reverted (`_instance_owners` never consulted)."""
    def add(ds):
        ds.update(_nested_observer_dataset())
    _ct_small_with(str(tmp_path / "in"), add)
    db = str(tmp_path / "s.db")
    owners = {"0010,0010": _rule("REMOVE"), "0010,0020": _rule("REMOVE"),
              "0008,0020": _rule("REMOVE")}

    with Session(db) as session:
        session.configuration.remove_private_tags = False
        session.configuration.phi_tags = dict(owners)
        session.ingest(str(tmp_path / "in"))
        session.anonymize(session.audit())
        session.save(sync=True)

    with Session(db) as session:
        session.configuration.remove_private_tags = False
        session.configuration.phi_tags = {
            **owners, VERIFYING_OBSERVER_CODE: _rule("EMPTY")}
        report = session.audit()
        assert _findings_for(report, VERIFYING_OBSERVER_CODE), "setup: raised"
        session.save(sync=True)
        session.anonymize(report)
        observer = (session.store.patients[0].studies[0].series[0]
                    .instances[0].sequences[VERIFYING_OBSERVER].items[0])
        assert observer.sequences[VERIFYING_OBSERVER_CODE].items == [], \
            "setup: cleared in memory"
        session.save(sync=True)

    with Session(db) as session:
        observer = (session.store.patients[0].studies[0].series[0]
                    .instances[0].sequences[VERIFYING_OBSERVER].items[0])
        assert observer.sequences[VERIFYING_OBSERVER_CODE].items == [], (
            "the nested sequence was emptied in memory and came back from "
            "the store: the instance was never saved")


def test_empty_on_a_sequence_gone_before_anonymize_declines(tmp_path):
    """The audit sees the sequence; it is gone before the remediation
    runs. Red before: the sequence arm was gated on the tag being in
    `sequences`, so control fell to `set_attr` and wrote `""` under the
    SQ key -- the exporter then wrote a zero-item SQ that was not in the
    graph, and the audit row said `REMEDIATION_REPLACE`. `REMOVE` on the
    same vanished sequence declines, and `EMPTY` now does too.

    Kills: the absent-target decline removed from the `REPLACE_TAG` arm."""
    def add(ds):
        ds.add_new(0x00081110, "SQ", Sequence([Dataset()]))
    _ct_small_with(str(tmp_path / "in"), add)
    db = str(tmp_path / "s.db")

    with Session(db) as session:
        session.configuration.phi_tags[REFERENCED_STUDY] = _rule("EMPTY")
        session.ingest(str(tmp_path / "in"))
        report = session.audit()
        instance = session.store.patients[0].studies[0].series[0].instances[0]
        assert _findings_for(report, REFERENCED_STUDY), "setup: raised"
        del instance.sequences[REFERENCED_STUDY]
        session.anonymize(report)

        assert REFERENCED_STUDY not in instance.attributes
        assert REFERENCED_STUDY not in instance.sequences
        summary = session.export(str(tmp_path / "out"), use_compression=False)
        session.save(sync=True)

    assert summary.written == 1, summary.failures
    assert (0x0008, 0x1110) not in _written(str(tmp_path / "out"))
    assert [d for d in _rows(db, "REMEDIATION_DECLINED")
            if REFERENCED_STUDY in d], _rows(db, "REMEDIATION_DECLINED")
    assert not [d for d in _rows(db, "REMEDIATION_REPLACE")
                if REFERENCED_STUDY in d]


def test_empty_on_a_sequence_already_emptied_writes_no_row(tmp_path):
    """The items went between audit and anonymize, and the sequence is
    still there. Nothing is left behind -- a zero-item sequence is what
    the rule asks for -- so no decline row; and nothing changed, so no
    `Emptied Sequence` row either and nothing counted as applied. Red
    before #547: the ignored return of `clear_sequence_items` filed a
    `REMEDIATION_REPLACE` for a no-op.

    **Stamped REMEDIATED since #567.** The rule's end state is in the
    graph, so the status says so; before, the instance kept the status
    the audit gave it -- IDENTIFIED over an instance with nothing left
    in it, beside a PASS grade. The stamp is a status change, and a
    status change advances the revision (CLAUDE.md, "Object graph and
    dirty tracking": a new status is a change the store should hold), so
    the instance now has something to save, and the save writes the
    zero-item sequence. No `mark_modified()` is involved: nothing else
    changed.

    Kills: `clear_sequence_items`' return value ignored (a success row);
    `_satisfied` not called (the status stays UNSCANNED)."""
    from isocenter.persistence import SqliteStore

    inst = Instance("1.2.3", "1.2.840.10008.5.1.4.1.1.7", 1)
    inst.add_sequence(REFERENCED_STUDY)
    inst.mark_persisted()
    store = SqliteStore(str(tmp_path / "rows.db"))
    buffer = []
    try:
        changed = RemediationService(
            store_backend=store)._apply_single_remediation(
                _empty_finding(inst, REFERENCED_STUDY), buffer)
    finally:
        store.stop()

    assert changed is False
    assert buffer == [], buffer
    assert inst.phi_status is PhiStatus.REMEDIATED
    assert inst.sequences[REFERENCED_STUDY].items == []
    assert REFERENCED_STUDY not in inst.attributes
    assert inst.has_unsaved_changes, (
        "the REMEDIATED stamp is a status change and must move the revision")


@pytest.mark.parametrize("mode", ["threads", "processes"])
def test_a_sequence_cleared_between_audit_and_anonymize_reads_remediated(
        tmp_path, monkeypatch, mode):
    """#567, end to end: the audit raised `EMPTY` on a sequence whose items
    were then cleared by hand before `anonymize()`.

    Measured on ac33641: the instance read IDENTIFIED, with no row for
    the tag, a PASS grade and a manifest reading `anonymized: false` --
    three records of one run disagreeing. The rule's end state is in the
    graph, so the instance is REMEDIATED, and the scan tally (#553)
    counts the satisfied key as handled rather than demoting it.

    Kills: `_satisfied_keys` not in the handled set the tally settles
    against."""
    import json

    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)
    if mode == "threads":
        monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
        monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    else:
        monkeypatch.setenv("ISOCENTER_FORCE_PROCESSES", "1")
        monkeypatch.delenv("ISOCENTER_FORCE_THREADS", raising=False)

    def add(ds):
        ds.add_new(0x00081110, "SQ", Sequence([Dataset()]))
    _ct_small_with(str(tmp_path / "in"), add)
    db = str(tmp_path / "s.db")

    with Session(db) as session:
        session.configuration.remove_private_tags = False
        session.configuration.phi_tags[REFERENCED_STUDY] = _rule("EMPTY")
        session.ingest(str(tmp_path / "in"))
        report = session.audit()
        instance = session.store.patients[0].studies[0].series[0].instances[0]
        assert _findings_for(report, REFERENCED_STUDY), "setup: raised"
        instance.sequences[REFERENCED_STUDY].items.clear()

        session.anonymize(report)

        manifest = tmp_path / "manifest.json"
        session.generate_manifest(str(manifest), format="json")
        items = json.loads(manifest.read_text(encoding="utf-8"))["items"]
        out = tmp_path / "report.md"
        session.generate_report(str(out))
        content = out.read_text(encoding="utf-8")
        status = instance.phi_status

    assert status is PhiStatus.REMEDIATED
    assert [item["anonymized"] for item in items] == [True], items
    assert "| **Validation Status** | **PASS** |" in content, content
    rows = [d for action in ("REMEDIATION_REPLACE", "REMEDIATION_DECLINED")
            for d in _rows(db, action) if REFERENCED_STUDY in d]
    assert rows == [], rows


def test_a_nested_satisfied_empty_stamps_its_owner():
    """An `EMPTY` on a sequence inside an item, already at zero items: the
    instance holding the item reads REMEDIATED too, as a nested success
    stamps it (#494).

    Kills: the owner stamp dropped from `_satisfied`."""
    inst = Instance("1.2.3", "1.2.840.10008.5.1.4.1.1.7", 1)
    observer = DicomItem()
    observer.add_sequence(VERIFYING_OBSERVER_CODE)
    inst.add_sequence_item(VERIFYING_OBSERVER, observer)
    finding = _empty_finding(inst, VERIFYING_OBSERVER_CODE)
    finding.entity = observer
    finding.entity_path = ((VERIFYING_OBSERVER, 0),)
    service = RemediationService()
    service._use_instance_owners({id(observer): inst})

    changed = service._apply_single_remediation(finding)

    assert changed is False
    assert observer.sequences[VERIFYING_OBSERVER_CODE].items == []
    assert observer.phi_status is PhiStatus.REMEDIATED
    assert inst.phi_status is PhiStatus.REMEDIATED


def test_a_declined_empty_is_not_stamped_as_satisfied():
    """The sequence is gone, so the `EMPTY` declines: `_satisfied` must
    not stamp REMEDIATED or count the key as handled on a decline.

    Driven through `_apply_single_remediation` directly, because through
    a pass the pass-end demotion takes a wrongly stamped instance back to
    IDENTIFIED and would hide the stamp.

    Kills: `_satisfied` stamping whatever `declined` says."""
    inst = Instance("1.2.3", "1.2.840.10008.5.1.4.1.1.7", 1)
    service = RemediationService()

    changed = service._apply_single_remediation(
        _empty_finding(inst, REFERENCED_STUDY))

    assert changed is False
    assert inst.phi_status is not PhiStatus.REMEDIATED
    assert not service._satisfied_keys
    assert service._declined_entities == [inst]


def test_a_zeroed_sequence_is_clear_on_re_audit(tmp_path):
    """A second audit of an emptied sequence raises nothing, so the
    instance does not flip back to IDENTIFIED and a second `anonymize()`
    has nothing to do.

    Kills: the items guard in the `EMPTY` arm removed."""
    def add(ds):
        ds.add_new(0x00081110, "SQ", Sequence([Dataset()]))
    _ct_small_with(str(tmp_path / "in"), add)

    with Session(str(tmp_path / "s.db")) as session:
        session.configuration.phi_tags[REFERENCED_STUDY] = _rule("EMPTY")
        session.ingest(str(tmp_path / "in"))
        session.anonymize(session.audit())
        again = session.audit()
        instance = session.store.patients[0].studies[0].series[0].instances[0]

        assert not _findings_for(again, REFERENCED_STUDY)
        assert instance.phi_status in (PhiStatus.CLEARED, PhiStatus.REMEDIATED)


# ---------------------------------------------------------------------------
# The redaction note in Derivation Description
# ---------------------------------------------------------------------------

def test_redaction_attestation_is_not_a_finding():
    """Red before: `REMOVE_TAG` on Isocenter's own note. An operator's text
    in the same tag is still removed, so the exemption is the value and
    not the tag.

    Kills: the exemption removed, and the exemption keyed on the tag alone."""
    from isocenter.services import _REDACTION_DERIVATION_DESCRIPTION
    inspector = PhiInspector(
        config_tags={DERIVATION_DESCRIPTION: _rule("REMOVE")},
        remove_private_tags=False)

    ours = Dataset()
    ours.add_new(0x00082111, "ST", _REDACTION_DERIVATION_DESCRIPTION)
    theirs = Dataset()
    theirs.add_new(0x00082111, "ST", "Cropped by Dr Smith at St Elsewhere")

    assert not _findings_for(
        inspector._scan_instance(_instance_from(ours), "P1"),
        DERIVATION_DESCRIPTION)
    operator = _findings_for(
        inspector._scan_instance(_instance_from(theirs), "P1"),
        DERIVATION_DESCRIPTION)
    assert [f.remediation_proposal.action_type for f in operator] == ["REMOVE_TAG"]


def test_safe_export_writes_a_redacted_instance(tmp_path, monkeypatch):
    """The one that matters to users: under a policy that removes
    Derivation Description, as the basic profile does, a redacted
    instance carries Isocenter's note, and `export(check_burned_in=True)`
    re-audits and skips every entity with a finding. Without the
    exemption no redacted instance is ever written by safe export.

    Kills: the exemption removed."""
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    serial = "SN-547"
    session = Session(str(tmp_path / "redact.db"))
    patient = Patient("P547", "Original^Name")
    study = Study("1.2.826.0.1.547", "20230101")
    series = Series("1.2.826.0.1.547.1", "OT", 1)
    series.equipment = Equipment("Acme", "Model", serial)
    instance = Instance("1.2.826.0.1.547.1.0", "1.2.840.10008.5.1.4.1.1.7", 1)
    instance.file_path = None
    instance.set_pixel_data(np.full((16, 16), 200, dtype=np.uint8))
    series.instances.append(instance)
    study.series.append(series)
    patient.studies.append(study)
    session.store.patients.append(patient)
    session.configuration.rules = [{"serial_number": serial,
                                    "redaction_zones": [[0, 8, 0, 8]]}]
    session.configuration.phi_tags[DERIVATION_DESCRIPTION] = _rule(
        "REMOVE", "Derivation Description")

    with session:
        session.anonymize(session.audit())
        session.redact()
        assert DERIVATION_DESCRIPTION in instance.attributes, \
            "setup: redaction wrote its note"
        summary = session.export(str(tmp_path / "out"), use_compression=False,
                                 check_burned_in=True)

    assert summary.written == 1, summary.failures
