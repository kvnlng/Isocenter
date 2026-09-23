"""A findings list that leaves an identifier behind does not read REMEDIATED (#553).

`anonymize(findings=...)` applies whatever it is handed, and the pass used
to stamp every entity it touched `REMEDIATED` on the strength of the
proposals it was given, whatever else the audit had raised against that
entity. Measured on ac33641:

- one of a CT instance's 202 top-level findings handed in: the instance
  read `REMEDIATED`, with 21 of its tags still holding their original
  values;
- the nested findings only: `REMEDIATED`, with the top-level identifiers
  still in place;
- the patient ID finding only: the patient `REMEDIATED`, its name still
  the original.

The manifest's `anonymized` is documented as "left no identifier
unremediated", and it reads the status, so the status has to know what the
audit raised.

**The scan tally.** `audit()` records, for each scan-time `entity_uid`, how
many distinct remediation keys it raised and their 64-bit hash-sum -- two
ints per entity. Each pass settles each uid it touched: complete when the
keys handled (applied, folded, or already satisfied), across passes on the
same audit, are exactly the raised set; otherwise every entity the pass
left `REMEDIATED` under that uid is recorded `IDENTIFIED`. Without an audit
behind the session -- hand-built findings, a reopened store -- there is no
tally and the pass accounts for itself as it always did.

What this does **not** do (owner ruling Q7, #573): move the grade. The
report grades audit rows, and a finding the caller did not hand in writes
none.
"""
import dataclasses
import json

import pydicom
import pytest
from pydicom.dataset import Dataset
from pydicom.sequence import Sequence

from isocenter.entities import PhiStatus
from isocenter.privacy import PhiFinding, PhiRemediation
from isocenter.session import DicomSession

from support.ct_small_files import write_ct

MODES = ["threads", "processes"]

#: Other Patient IDs Sequence: CT_small carries two items, each holding a
#: Patient ID the scan raises -- the nested findings this file hands in.
OTHER_PATIENT_IDS = "0010,1002"


@pytest.fixture
def mode(request, monkeypatch):
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)
    if request.param == "threads":
        monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
        monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    else:
        monkeypatch.setenv("ISOCENTER_FORCE_PROCESSES", "1")
        monkeypatch.delenv("ISOCENTER_FORCE_THREADS", raising=False)
    return request.param


@pytest.fixture(autouse=True)
def _threads_by_default(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _session(tmp_path, name="s", files=(("PAT-553", "1"),)):
    src = tmp_path / f"{name}_src"
    for index, (patient_id, suffix) in enumerate(files):
        write_ct(str(src / f"{index}.dcm"), patient_id, suffix)
    session = DicomSession(persistence_file=str(tmp_path / f"{name}.db"))
    session.ingest(str(src))
    return session


def _instances(session):
    return [i for p in session.store.patients for st in p.studies
            for se in st.series for i in se.instances]


def _entities(session):
    out = []
    for patient in session.store.patients:
        out.append(patient)
        for study in patient.studies:
            out.append(study)
            out.extend(i for se in study.series for i in se.instances)
    return out


def _manifest(session, tmp_path):
    path = tmp_path / "manifest.json"
    session.generate_manifest(str(path), format="json")
    return [item["anonymized"]
            for item in json.loads(path.read_text(encoding="utf-8"))["items"]]


def _grade(session, tmp_path):
    path = tmp_path / "report.md"
    session.generate_report(str(path))
    return [line for line in path.read_text(encoding="utf-8").splitlines()
            if line.startswith("| **Validation Status**")]


def _top_level(report, entity):
    return [f for f in report if f.entity is entity and not f.entity_path]


@pytest.mark.parametrize("mode", MODES, indirect=True)
def test_a_partial_top_level_list_leaves_the_instance_identified(
        tmp_path, mode):
    """s1: one of the instance's top-level findings, handed alone."""
    session = _session(tmp_path)
    try:
        report = session.audit()
        instance = _instances(session)[0]
        handed = [f for f in _top_level(report, instance)
                  if f.tag == "0008,0080"]
        assert len(handed) == 1 and len(_top_level(report, instance)) > 1

        applied = session.anonymize(findings=handed)

        assert applied == 1, "the handed finding is still applied"
        assert instance.phi_status is PhiStatus.IDENTIFIED
    finally:
        session.close()


@pytest.mark.parametrize("mode", MODES, indirect=True)
def test_a_nested_only_list_leaves_the_owner_identified(tmp_path, mode):
    """s2: the findings inside sequences, and none of the top-level ones."""
    session = _session(tmp_path)
    try:
        report = session.audit()
        instance = _instances(session)[0]
        nested = [f for f in report if f.entity_path
                  and f.entity_uid == instance.sop_instance_uid]
        assert nested, "setup: CT_small carries nested identifiers"

        assert session.anonymize(findings=nested) == len(nested)

        assert instance.phi_status is PhiStatus.IDENTIFIED
    finally:
        session.close()


def test_a_partial_patient_list_leaves_the_patient_identified(tmp_path):
    """s7: the Patient ID alone. The ID the tally was keyed on changes
    during the pass, so a tally keyed on the live `patient_id` would miss
    it."""
    session = _session(tmp_path)
    try:
        report = session.audit()
        patient = session.store.patients[0]
        handed = [f for f in report
                  if f.entity is patient and f.field_name == "patient_id"]
        assert len(handed) == 1

        assert session.anonymize(findings=handed) == 1

        assert patient.patient_id.startswith("ANON_"), "setup: replaced"
        assert patient.phi_status is PhiStatus.IDENTIFIED
    finally:
        session.close()


def _icon(value):
    """A 2x2 MONOCHROME2 icon item, complete enough to decode."""
    item = Dataset()
    item.Rows = item.Columns = 2
    item.BitsAllocated = item.BitsStored = 8
    item.HighBit = 7
    item.SamplesPerPixel = 1
    item.PhotometricInterpretation = "MONOCHROME2"
    item.PixelRepresentation = 0
    item.add_new(0x7FE00010, "OB", bytes([value]) * 4)
    return item


def _icon_session(tmp_path):
    """CT_small carrying its own icon and a referenced image's icon: the
    depth-1 and depth-2 nested shapes the tally premise was measured on.

    Built here rather than imported from `test_nested_pixel_carriage`:
    no test module here imports another, and that one's helpers carry
    `io_handlers` into this file's import graph for nothing.
    """
    path = tmp_path / "icons_src" / "icons.dcm"
    write_ct(str(path), "PAT-553-I", "9")
    ds = pydicom.dcmread(str(path))
    ds.IconImageSequence = Sequence([_icon(7)])
    referenced = Dataset()
    referenced.ReferencedSOPClassUID = ds.SOPClassUID
    referenced.ReferencedSOPInstanceUID = ds.SOPInstanceUID + ".2"
    referenced.IconImageSequence = Sequence([_icon(9)])
    ds.ReferencedImageSequence = Sequence([referenced])
    ds.save_as(str(path))
    session = DicomSession(persistence_file=str(tmp_path / "icons.db"))
    session.ingest(str(path.parent))
    return session


@pytest.mark.parametrize("mode", MODES, indirect=True)
@pytest.mark.parametrize("shape", ["ct", "two_cts_one_patient", "icons"])
def test_a_full_report_still_remediates_everything(tmp_path, mode, shape):
    """The ordinary path: every key the audit raised is handled.

    CT_small raises 207 findings for 206 applied remediations -- the gap is
    folds and duplicates -- so a tally that counted findings rather than
    distinct keys, or that did not count folded keys as handled, would
    demote here.
    """
    if shape == "icons":
        session = _icon_session(tmp_path)
    elif shape == "two_cts_one_patient":
        session = _session(tmp_path, files=(("PAT-553", "1"),
                                            ("PAT-553", "2")))
    else:
        session = _session(tmp_path)
    try:
        report = session.audit()
        assert len(report) > 0
        session.anonymize(report)

        statuses = {e.phi_status for e in _entities(session)}
        assert statuses == {PhiStatus.REMEDIATED}, statuses
        # Every raised uid settled complete and was dropped: the tally
        # holds nothing once the ordinary path is done with it.
        assert session._scan_tally._raised == {}
        assert session._scan_tally._partial == {}
        assert set(_manifest(session, tmp_path)) == {True}
        assert _grade(session, tmp_path) == [
            "| **Validation Status** | **PASS** |"]
    finally:
        session.close()


def _halves(report, instance):
    """The instance's findings split in two; everything else in the first."""
    mine = [f for f in report if f.entity_uid == instance.sop_instance_uid]
    others = [f for f in report if f.entity_uid != instance.sop_instance_uid]
    half = len(mine) // 2
    return others + mine[:half], mine[half:]


def test_two_complementary_partial_passes_end_remediated(tmp_path):
    """Keys handled by earlier passes on the same audit count.

    The two lists overlap by one `REPLACE_TAG` finding, which applies
    cleanly twice: handled in both passes, it is one key of the raised
    set, not two.

    Kills: the handled keys not merged across passes; merged as a list,
    so a key handled twice counts twice.
    """
    session = _session(tmp_path)
    try:
        report = session.audit()
        instance = _instances(session)[0]
        first, second = _halves(report, instance)
        overlap = next(f for f in _top_level(report, instance)
                       if f.remediation_proposal.action_type == "REPLACE_TAG")
        first += [] if overlap in first else [overlap]
        second += [] if overlap in second else [overlap]

        session.anonymize(findings=first)
        assert instance.phi_status is PhiStatus.IDENTIFIED

        session.anonymize(findings=second)
        assert instance.phi_status is PhiStatus.REMEDIATED
    finally:
        session.close()


def test_handing_the_same_partial_list_again_stays_identified(tmp_path):
    """A count reached by repetition is not the raised set.

    Only the instance's top-level `REPLACE_TAG` findings, which apply
    again cleanly on every pass, so no decline demotes the instance and
    the tally is the only thing that can. Handed as many times as it
    takes for the passes together to have applied at least as many
    remediations as the audit raised against the instance.

    Kills: a running count of handled keys in place of the merged set.
    """
    session = _session(tmp_path)
    try:
        report = session.audit()
        instance = _instances(session)[0]
        mine = [f for f in report if f.entity_uid == instance.sop_instance_uid]
        raised = {(f.entity_path, f.remediation_proposal.target_attr)
                  for f in mine}
        # Not the copies the export stamps from an owner: with the owner
        # not handed in, those decline (#624), and this test is about the
        # tally being the only thing that demotes.
        replaces = list({f.tag: f for f in _top_level(report, instance)
                         if f.remediation_proposal.action_type
                         == "REPLACE_TAG"
                         and f.tag not in ("0010,0010", "0010,0020",
                                           # the owned UIDs, since #544
                                           "0020,000d", "0020,000e")}.values())
        assert 0 < len(replaces) < len(raised), (len(replaces), len(raised))

        for _ in range(-(-len(raised) // len(replaces))):
            assert session.anonymize(findings=replaces) == len(replaces)

        assert session.store_backend.get_audit_declines() == []
        assert instance.phi_status is PhiStatus.IDENTIFIED
    finally:
        session.close()


def test_a_hand_built_extra_key_cannot_complete_an_entity(tmp_path):
    """The raised keys less one, plus a key the audit never raised.

    The count matches; the set does not. A hand-built finding under a
    scanned uid can only ever demote.
    """
    session = _session(tmp_path)
    try:
        report = session.audit()
        instance = _instances(session)[0]
        mine = [f for f in report if f.entity_uid == instance.sop_instance_uid]
        dropped = next(f for f in mine if f.tag == "0008,0080")
        handed = [f for f in mine if f is not dropped]
        assert "0008,0060" in instance.attributes
        assert not any(f.tag == "0008,0060" for f in mine)
        handed.append(PhiFinding(
            entity_uid=instance.sop_instance_uid, entity_type="Instance",
            field_name="Modality", value=instance.attributes["0008,0060"],
            reason="hand-built", tag="0008,0060", entity=instance,
            remediation_proposal=PhiRemediation(
                action_type="REPLACE_TAG", target_attr="0008,0060",
                new_value="OT",
                original_value=instance.attributes["0008,0060"])))

        session.anonymize(findings=handed)

        assert instance.attributes["0008,0060"] == "OT", "setup: applied"
        assert instance.phi_status is PhiStatus.IDENTIFIED
    finally:
        session.close()


def _misnamed_setup(session, report, level):
    """The entity at `level`, its uid, and the one report finding to re-file.

    `patient_name` and `patient_id` both re-file a patient finding: the
    first leaves the patient's live ID the original through the pass, the
    second replaces it, so the patient is settled under the ID it held
    when the pass began, not the one it ends with.
    """
    instance = _instances(session)[0]
    if level in ("patient_name", "patient_id"):
        patient = session.store.patients[0]
        return patient, patient.patient_id, next(
            f for f in report if f.entity is patient and f.field_name == level)
    if level == "study":
        study = session.store.patients[0].studies[0]
        return study, study.study_instance_uid, next(
            f for f in report if f.entity is study)
    return instance, instance.sop_instance_uid, next(
        f for f in _top_level(report, instance) if f.tag == "0008,0080")


@pytest.mark.parametrize("uid", [None, "not-a-scanned-uid"])
@pytest.mark.parametrize("level", ["instance", "study", "patient_name",
                                   "patient_id"])
def test_a_misnamed_finding_does_not_vouch_for_its_entity(
        tmp_path, level, uid):
    """The report less every finding under the entity's UID, plus one of
    those findings re-filed under a uid the audit never raised under --
    or none.

    The tally is asked about the uid a finding *names*, so a wrong or
    missing one got no opinion, and the success stamped the entity
    REMEDIATED over everything the pass was never handed: manifest
    `true`, PASS, 18 of the instance's identifiers still original. The
    entity's own UID is settled as well, and nothing was handled under
    it.
    """
    session = _session(tmp_path)
    try:
        report = session.audit()
        entity, real_uid, original = _misnamed_setup(session, report, level)
        rest = [f for f in report if f.entity_uid != real_uid]
        assert len(rest) < len(report), "setup"

        session.anonymize(findings=rest + [
            dataclasses.replace(original, entity_uid=uid)])

        assert entity.phi_status is PhiStatus.IDENTIFIED
        assert _manifest(session, tmp_path) == [False]
    finally:
        session.close()


@pytest.mark.parametrize("level", ["instance", "study", "patient_name",
                                   "patient_id"])
def test_a_misnamed_finding_beside_the_full_report_stays_remediated(
        tmp_path, level):
    """The same re-filed finding, handed with the whole report.

    Every key the audit raised under the entity's UID is handled, so
    settling that UID completes it; a stray name on one extra finding
    demotes nothing.
    """
    session = _session(tmp_path)
    try:
        report = session.audit()
        _entity, _uid, original = _misnamed_setup(session, report, level)

        session.anonymize(findings=list(report) + [
            dataclasses.replace(original, entity_uid="not-a-scanned-uid")])

        assert {e.phi_status for e in _entities(session)} == {
            PhiStatus.REMEDIATED}
        assert set(_manifest(session, tmp_path)) == {True}
    finally:
        session.close()


def test_hand_built_findings_without_an_audit_keep_pass_accounting(tmp_path):
    """No audit, no tally: the pass speaks for the findings it was given."""
    session = _session(tmp_path)
    try:
        instance = _instances(session)[0]
        finding = PhiFinding(
            entity_uid=instance.sop_instance_uid, entity_type="Instance",
            field_name="Institution Name",
            value=instance.attributes["0008,0080"], reason="hand-built",
            tag="0008,0080", entity=instance,
            remediation_proposal=PhiRemediation(
                action_type="REPLACE_TAG", target_attr="0008,0080",
                new_value="", original_value=instance.attributes["0008,0080"]))

        assert session.anonymize(findings=[finding]) == 1

        assert instance.phi_status is PhiStatus.REMEDIATED
    finally:
        session.close()


def test_a_pass_after_the_audit_is_settled_keeps_pass_accounting(tmp_path):
    """A uid whose raised keys were all handled leaves the tally, so a
    later hand-built finding on it is judged as one with no audit behind
    it: applied, and REMEDIATED.

    Kills: `settle` answering False, rather than no opinion, for a uid
    the tally does not hold.
    """
    session = _session(tmp_path)
    try:
        session.anonymize(session.audit())
        instance = _instances(session)[0]
        assert instance.phi_status is PhiStatus.REMEDIATED, "setup"
        instance.set_attr("0008,0081", "1 Main St")
        finding = PhiFinding(
            entity_uid=instance.sop_instance_uid, entity_type="Instance",
            field_name="Institution Address", value="1 Main St",
            reason="hand-built", tag="0008,0081", entity=instance,
            remediation_proposal=PhiRemediation(
                action_type="REPLACE_TAG", target_attr="0008,0081",
                new_value="", original_value="1 Main St"))

        assert session.anonymize(findings=[finding]) == 1

        assert instance.phi_status is PhiStatus.REMEDIATED
    finally:
        session.close()


NARROW_CONFIG = (
    "privacy_profile: none\n"
    "remove_private_tags: false\n"
    "phi_tags:\n"
    "  '0008,0080':\n"
    "    name: Institution Name\n"
    "    action: EMPTY\n")


@pytest.mark.parametrize("between", ["narrower_config", "partial_then_audit"])
def test_an_earlier_report_is_settled_against_the_latest_audit(
        tmp_path, between):
    """Pinned as it stands, and a known cost of the design (#582).

    The tally is the **most recent** `audit()`'s. A report from an earlier
    audit -- or from one run under another config -- carries keys the
    latest one did not raise, so the instance's handled set is a strict
    superset of the raised one, and count plus hash-sum cannot tell a
    superset from a set with a key missing without holding the keys.
    The instance reads IDENTIFIED with nothing left on it, until the next
    `audit()` scans it clean. The safe direction: the manifest says
    `false` for a clean instance, never `true` for one with identifiers.

    - `narrower_config`: `r1 = audit()`, `audit(config_path=narrow)`,
      `anonymize(r1)`.
    - `partial_then_audit`: `r1 = audit()`, a `REPLACE_TAG`-only pass
      over r1, `audit()`, `anonymize(r1)`.

    If #582 changes this, this test is the one to change with it.
    """
    session = _session(tmp_path)
    try:
        first = session.audit()
        instance = _instances(session)[0]
        if between == "narrower_config":
            narrow = tmp_path / "narrow.yaml"
            narrow.write_text(NARROW_CONFIG, encoding="utf-8")
            assert 0 < len(session.audit(config_path=str(narrow))) < len(first)
        else:
            session.anonymize(findings=[
                f for f in first
                if f.remediation_proposal.action_type == "REPLACE_TAG"])
            assert 0 < len(session.audit()) < len(first)

        session.anonymize(first)

        assert instance.phi_status is PhiStatus.IDENTIFIED
        assert _manifest(session, tmp_path) == [False]

        assert len(session.audit()) == 0
        assert instance.phi_status is PhiStatus.CLEARED
    finally:
        session.close()


def test_a_new_audit_replaces_the_tally(tmp_path):
    """Partial pass, a hand edit, then a new audit and a full pass over
    its report.

    The edit adds an identifier the first audit never raised, so the
    second report carries a key the first tally does not hold: a tally
    kept from the first audit reads that key as a hand-built extra and
    never completes the instance.

    Kills: the tally built once per session rather than per audit.
    """
    session = _session(tmp_path)
    try:
        report = session.audit()
        instance = _instances(session)[0]
        assert not [f for f in report if f.tag == "0008,0081"], "setup"
        first, _second = _halves(report, instance)
        session.anonymize(findings=first)
        assert instance.phi_status is PhiStatus.IDENTIFIED
        instance.set_attr("0008,0081", "1 Main St")

        session.anonymize(session.audit())

        assert instance.phi_status is PhiStatus.REMEDIATED
    finally:
        session.close()


@pytest.mark.parametrize("mode", MODES, indirect=True)
def test_an_unresolvable_nested_finding_keeps_its_instance_identified(
        tmp_path, mode):
    """#57's shape: the item a nested finding named is gone by the pass.

    The finding rehydrates with no entity and records a "no entity
    reference" decline, but it names no entity to demote. Before the
    tally, the rest of the report succeeding on the same instance left
    it `REMEDIATED` beside the decline. The whole report is handed, so
    the unresolved keys are the only ones the pass leaves unhandled.

    Kills: an unresolved finding's key counted as handled.
    """
    session = _session(tmp_path)
    try:
        report = session.audit()
        instance = _instances(session)[0]
        nested = [f for f in report if f.entity_path
                  and f.entity_uid == instance.sop_instance_uid
                  and f.entity_path[0][0] == OTHER_PATIENT_IDS]
        assert nested, "setup"
        instance.sequences[OTHER_PATIENT_IDS].items.clear()
        session._rehydrate_findings(nested)
        assert all(f.entity is None for f in nested), "setup: unresolvable"

        session.anonymize(findings=report)
        session.store_backend.flush_audit_queue()
        declines = [d for _t, _u, d in session.store_backend.get_audit_declines()
                    if "no entity reference" in d]

        assert instance.phi_status is PhiStatus.IDENTIFIED
        assert len(declines) == len(nested), declines
    finally:
        session.close()


def _finding(uid, attr, path=None):
    return PhiFinding(
        entity_uid=uid, entity_type="Instance", field_name=attr, value="x",
        reason="t", tag=attr, entity=None, entity_path=path,
        remediation_proposal=PhiRemediation(
            action_type="REPLACE_TAG", target_attr=attr, new_value=""))


def test_a_complete_entity_leaves_nothing_behind_in_the_tally():
    """Two ints per raised uid, and nothing held once it is complete.

    Imported here, not at module scope: a name the unfixed tree does not
    have would turn the whole file into a collection error, and every
    other test here has to be able to fail on its own assertion.

    Kills: the tally retaining key sets for complete entities; counting
    findings rather than distinct keys.
    """
    from isocenter.remediation import _ScanTally, _remediation_key

    raised = [_finding("u1", "0008,0080"), _finding("u1", "0008,0081"),
              _finding("u1", "0008,0080"), _finding("u2", "0010,0010")]
    tally = _ScanTally(raised)

    stored = tally._raised["u1"]
    assert isinstance(stored, tuple) and len(stored) == 2
    assert all(isinstance(value, int) for value in stored)
    assert stored[0] == 2, "distinct keys, not findings"

    assert tally.settle("u1", {_remediation_key(raised[0])}) is False
    assert tally._partial, "an incomplete uid keeps what it handled"
    assert tally.settle("u1", {_remediation_key(raised[1])}) is True
    assert "u1" not in tally._raised
    assert "u1" not in tally._partial
    assert tally.settle("nobody", set()) is None


def test_an_empty_uid_is_tallied_like_any_other():
    """An empty Patient ID is a uid the scan files findings under (#581).

    The tally skipped a falsy `entity_uid`, so a pass over a `''` patient
    got None -- no opinion -- and the success stamp stood over whatever
    the pass was not handed. Only a hand-built graph reaches this end to
    end: the shipped inspector raises no ID finding for an empty ID, so
    such a patient carries one key, and one key is either handled or never
    settled. So this is the killer for the `_ScanTally` filter
    (`tests/test_an_empty_patient_id_is_still_a_patient.py` covers the
    other two).
    """
    from isocenter.remediation import _ScanTally, _remediation_key

    first, second = _finding("", "0010,0010"), _finding("", "0010,1000")
    tally = _ScanTally([first, second])
    assert tally.settle("", {_remediation_key(first)}) is False
    assert tally.settle("", {_remediation_key(second)}) is True
