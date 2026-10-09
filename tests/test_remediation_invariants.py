"""Behaviour in `remediation.py` that nothing was holding in place (#132).

Found by `scripts/mutation_probe.py` once #106 gave it operators that
reach straight-line code: 14 of 35 sampled mutations to the module that
*applies* de-identification survived. The code was right in every case
below; no test would have noticed it changing.

Each test here corresponds to a specific surviving mutant, named in its
docstring, so a future reader can tell what it is defending against
rather than guessing from the assertion.
"""
import dataclasses

import pytest

from isocenter.entities import (DicomItem, Instance, Patient, PhiStatus,
                                Series, Study)
from isocenter.privacy import PhiFinding, PhiRemediation
from isocenter.remediation import RemediationService

from support.project_secret import FIXED_A


def _finding(entity, action, tag, new_value=None, original=None, metadata=None):
    return PhiFinding(
        entity_uid=getattr(entity, "sop_instance_uid", "E1"),
        entity_type="Instance", field_name=tag, value=original,
        reason="test", tag=tag, entity=entity,
        remediation_proposal=PhiRemediation(
            action_type=action, target_attr=tag, new_value=new_value,
            original_value=original, metadata=metadata or {}))


def _saved_instance():
    inst = Instance("1.2.3", "1.2.840.10008.5.1.4.1.1.7", 1)
    inst.set_attr("0010,0010", "DOE^JOHN")
    inst.mark_persisted()
    assert not inst.has_unsaved_changes, "setup: starts saved"
    return inst


# --------------------------------------------------------------------
# PatientID resolution -- the input to deterministic date shifting
# --------------------------------------------------------------------

def test_patient_id_comes_from_the_proposal_metadata_first():
    """Mutant: `return entity.patient_id` -> `return None`, survived.

    Date jitter is deterministic *per patient* so intervals survive. The
    per-patient part is this ID. Returning None where an ID exists is how
    the jitter collapses to one shift for everybody -- the failure #104
    describes, reached from upstream. Nothing called this method.
    """
    inst = _saved_instance()
    resolved = RemediationService()._resolve_patient_id(
        inst, PhiRemediation(action_type="SHIFT_DATE", target_attr="0008,0020",
                             metadata={"patient_id": "PAT-42"}))

    assert resolved == "PAT-42"


def test_patient_id_falls_back_to_the_entity():
    patient = Patient(patient_name="DOE^JOHN", patient_id="PAT-7")
    resolved = RemediationService()._resolve_patient_id(
        patient, PhiRemediation(action_type="SHIFT_DATE",
                                target_attr="0008,0020"))

    assert resolved == "PAT-7"


def test_an_unresolvable_patient_id_is_none_rather_than_a_guess():
    """A wrong ID is worse than none: it would shift two patients as one."""
    resolved = RemediationService()._resolve_patient_id(
        DicomItem(), PhiRemediation(action_type="SHIFT_DATE",
                                    target_attr="0008,0020"))

    assert resolved is None


# --------------------------------------------------------------------
# "Remediated" must imply "needs saving"
# --------------------------------------------------------------------

@pytest.mark.parametrize("action,new_value", [
    ("REMOVE_TAG", None),
    ("REPLACE_TAG", "ANONYMIZED"),
])
def test_remediating_an_instance_leaves_it_needing_a_save(action, new_value):
    """The invariant behind the bug the line-206 comment records.

    `attributes` is a plain dict, so deleting from it bumps no revision.
    Without an explicit bump an already-saved instance reported no
    unsaved changes after its PHI was stripped, the next save skipped it,
    and the identifier stayed in the database.

    Two mechanisms now bump it -- `mark_modified()` and
    `record_phi_status()` -- so deleting either alone is invisible, which
    is why three such mutants survive (#132). This pins the invariant
    they jointly provide, so removing *both* fails here rather than
    shipping.
    """
    inst = _saved_instance()
    RemediationService().apply_remediation(
        [_finding(inst, action, "0010,0010",
                  new_value=new_value, original="DOE^JOHN")])

    assert inst.has_unsaved_changes, (
        f"{action} changed the instance but left it looking saved; the "
        "next save skips it and the identifier survives on disk")


def test_removing_a_private_sequence_leaves_the_instance_needing_a_save():
    """The same invariant, on the arm the parametrization never reaches.

    `test_remediating_an_instance_leaves_it_needing_a_save` drives
    REMOVE_TAG and REPLACE_TAG at `0010,0010`, which lives in
    `attributes`, so it stops at the first branch. The private-sequence
    arm (added by #167) is a separate `elif` that `del`s from
    `entity.sequences` -- and nothing exercised it, so the invariant it
    is supposed to hold was prose there.

    **This kills no surviving mutant from the #132 run, and the honest
    reading matters.** Deleting this arm's `entity.mark_modified()`
    alone stays green, exactly as it does on the attribute arm, because
    `record_phi_status()` on the shared success path also advances the
    revision. Red is demonstrated the same joint way the sibling test's
    docstring describes: delete BOTH the `mark_modified()` in this arm
    and the `record_phi_status()` below it, and this fails. What it adds
    is a third parametrization of an invariant over an arm nothing was
    running at all -- not a survivor-killer.
    """
    inst = _saved_instance()
    inst.add_sequence_item("0009,1001", DicomItem())
    inst.mark_persisted()
    assert not inst.has_unsaved_changes, "setup: starts saved"

    RemediationService().apply_remediation(
        [_finding(inst, "REMOVE_TAG", "0009,1001")])

    assert "0009,1001" not in inst.sequences, (
        "the private sequence survived a REMOVE_TAG that the report "
        "records as having removed it")
    assert inst.has_unsaved_changes, (
        "the sequence was stripped in memory but the instance looks "
        "saved; the next save skips it and the store keeps the block")


def _saved_patient():
    """`Patient`/`Study`/`Series` are `TrackedEntity` but not `DicomItem`.

    They have no `set_attr`, so remediation reaches them through the
    `elif hasattr(entity, target_attr)` branch that writes the Python
    attribute directly -- a separate code path from the tag-dict one an
    `Instance` takes, with its own revision bookkeeping.
    """
    patient = Patient(patient_name="DOE^JOHN", patient_id="PAT-7")
    patient.mark_persisted()
    assert not patient.has_unsaved_changes, "setup: starts saved"
    return patient


def test_replacing_a_patient_attribute_leaves_it_needing_a_save():
    """Same invariant as the instance case, on the branch `Patient` takes.

    An `Instance` never reaches this code -- it has `set_attr`, so it
    stops at the first branch. Every test that drove remediation through
    an instance therefore left this path unexercised, which is why a
    mutation here survived the whole 608-test suite (#132).
    """
    patient = _saved_patient()
    RemediationService().apply_remediation(
        [_finding(patient, "REPLACE_TAG", "patient_name",
                  new_value="ANONYMIZED", original="DOE^JOHN")])

    assert patient.patient_name == "ANONYMIZED"
    assert patient.has_unsaved_changes, (
        "the patient name was replaced in memory but the patient still "
        "looks saved, so the next save skips it and the name survives")


def test_clearing_a_patient_attribute_leaves_it_needing_a_save():
    patient = _saved_patient()
    RemediationService().apply_remediation(
        [_finding(patient, "REMOVE_TAG", "patient_name", original="DOE^JOHN")])

    assert patient.patient_name is None
    assert patient.has_unsaved_changes


# --------------------------------------------------------------------
# The four `mark_modified()` calls, one test each (#173, #132, #961)
# --------------------------------------------------------------------
#
# Five until #961, which found that three of them were pinned by nothing:
# with the call replaced by `pass`, this file and every other test in
# `remediation.py`'s probe row stayed green (measured on 3.12 and 3.14t,
# each alone and all three together). The reason is #767: assigning a
# tracked field of a Patient, Study or Series is itself an edit
# (`entities._assign_tracked_field` marks the entity when the value
# changes), so the explicit call after a `setattr` matters only where the
# value written does not change, or the field is not tracked. What #961
# did with each:
#
# * the REPLACE Python-attribute arm's call stays, pinned on the one path
#   that needs it, a stored field the entity does not track
#   (`test_replacing_a_field_the_entity_does_not_track_still_needs_a_save`);
# * the SHIFT arm's call stays, pinned on a Study that already holds the
#   shifted date and carries no record of it
#   (`test_a_shift_onto_a_date_already_there_still_saves_its_record`);
# * the REMOVE Python-attribute arm's call is deleted: #679 restricts that
#   arm to the fields the exporter stamps, every one of them tracked, and
#   `test_every_field_the_remove_arm_may_clear_is_a_tracked_field` pins
#   that they stay tracked.
#
# The two item arms `del` from a plain dict, which bumps no revision, and
# were always pinned.
#
# On a *first* remediation each of these calls is redundant:
# `record_phi_status(REMEDIATED)` on the shared success path also
# advances the revision, so deleting the bump alone stays green -- that
# is what the tests above say, honestly, in their docstrings. After a
# reload it is the only thing left. A graph loaded from the store
# carries whatever status was stored for each entity, so one already
# remediated once comes back at REMEDIATED and the
# `record_phi_status(REMEDIATED)` ending a second remediation
# short-circuits (the guard reads the `phi_status` property, which
# still returns REMEDIATED because nothing moved the revision) and no
# bump happens. The PHI is
# stripped from memory, the entity reports nothing to save, the next
# save skips it, and the identifier stays in the database (#173).
#
# Each of the four tests that cites a line drives exactly one arm on the
# path its call bears, so a deletion anywhere in the cluster turns exactly
# one test red. The tests beside them that drive a tracked field through
# the same arms pin `_assign_tracked_field`, and say so.


def _as_reloaded(entity):
    """Puts an entity in the state a load from the store leaves it in.

    Two lines, because that is what hydration is: `load_all` records the
    stored conclusion and then marks the subtree persisted (see the
    `stored_statuses` loop in persistence.py). Reached directly rather
    than by remediating through another arm first -- routing the setup
    through a second `mark_modified()` site would make each test kill
    two lines and pin neither.
    """
    entity.record_phi_status(PhiStatus.REMEDIATED)
    entity.mark_persisted()
    assert entity.phi_status is PhiStatus.REMEDIATED, \
        "setup: a hydrated entity carries the conclusion the store held"
    assert not entity.has_unsaved_changes, "setup: starts saved"
    return entity


def test_replacing_a_second_patient_attribute_after_a_reload_still_needs_a_save():
    """The `REPLACE_TAG` Python-attribute arm -- the one a `Patient`
    takes, having no `set_attr` -- on a tracked field whose value changes.

    This claimed to pin the arm's `mark_modified()` until #961. It does
    not: `patient_name` is a tracked field, so the assignment marks the
    patient itself (`entities._assign_tracked_field`, #767), and the test
    is green with the call deleted. What it pins is that assignment.
    """
    patient = _as_reloaded(Patient(patient_name="DOE^JOHN", patient_id="PAT-7"))

    RemediationService().apply_remediation(
        [_finding(patient, "REPLACE_TAG", "patient_name",
                  new_value="ANONYMIZED", original="DOE^JOHN")])

    assert patient.patient_name == "ANONYMIZED"
    assert patient.has_unsaved_changes, (
        "the entity reports no unsaved changes after its PHI was "
        "stripped, so the next save skips it and the value stays in "
        "the database")


def test_replacing_a_field_the_entity_does_not_track_still_needs_a_save():
    """Pins `entity.mark_modified()` at remediation.py line 299.

    That is the `REPLACE_TAG` Python-attribute arm, on the one path its
    call bears: a stored field outside the entity's `_TRACKED_FIELDS`.
    `Study.date_shifted` is one (a `studies` column, written by
    remediation beside its own `mark_modified()`), so assigning it marks
    nothing, and without the call the flag is `True` in memory while the
    next save skips the study's row.

    The path is hand-built only: no scan raises a REPLACE on
    `date_shifted`. The arm allows it because `_replace_attr_refused` is
    generic over every entity field; whether REPLACE should take #679's
    allowlist, as REMOVE did, is an open question, and if it does this
    test's subject goes with it. Until then the call is load-bearing here.
    """
    study = _as_reloaded(Study("S1", "20230101"))
    assert study.date_shifted is False

    RemediationService().apply_remediation(
        [_finding(study, "REPLACE_TAG", "date_shifted",
                  new_value=True, original=False)])

    assert study.date_shifted is True
    assert study.has_unsaved_changes, (
        "the flag was written in memory but the study still looks saved, "
        "so the next save skips its row and the store keeps the old flag")


def _shift(study, original):
    return _finding(study, "SHIFT_DATE", "study_date", original=original,
                    metadata={"patient_id": "PAT-7"})


def test_a_shift_onto_a_date_already_there_still_saves_its_record():
    """Pins `entity.mark_modified()` at remediation.py line 376.

    That is the `SHIFT_DATE` `setattr` arm, which every flagged study
    date goes through (a `Study` has no `set_attr`), on the path its call
    bears: the Study already holds the shifted date and carries no record
    of the shift. The date does not change, so the assignment marks
    nothing; the arm also writes `_shifted_study_date` and `date_shifted`,
    neither a tracked field ("which only remediation writes, beside its
    own `mark_modified()`", `entities.py`). Without the call both are set
    in memory and never written, and the next load reads the shifted date
    as an original, which the scan raises again.

    The shifted date is computed, not written as a literal: the offset is
    keyed. A first pass over a second `Study` under the same secret and
    patient gives it.
    """
    service = RemediationService(project_secret=FIXED_A)
    first = Study("S2", "20230101")
    service.apply_remediation([_shift(first, "20230101")])
    shifted = first.study_date
    record = first._shifted_study_date
    assert record and first.date_shifted, "setup: the first pass shifted"

    study = _as_reloaded(Study("S1", shifted))
    assert study._shifted_study_date is None and study.date_shifted is False

    assert service.apply_remediation([_shift(study, "20230101")]) == 1

    assert study.study_date == shifted, "setup: the date did not change"
    assert study._shifted_study_date == record
    assert study.date_shifted is True
    assert study.has_unsaved_changes, (
        "the shift record and the flag were set in memory but the study "
        "still looks saved, so the next save skips its row and the next "
        "load reads the shifted date as an original")


def test_shifting_a_study_date_after_a_reload_still_needs_a_save():
    """The `SHIFT_DATE` `setattr` arm on a date that changes.

    This claimed to pin the arm's `mark_modified()` until #961. It does
    not: `study_date` is a tracked field, so the assignment of a new date
    marks the study itself (#767), and the test is green with the call
    deleted. `test_a_shift_onto_a_date_already_there_still_saves_its_record`
    pins the call. This is not a corner all the same: the
    inspector's study scan raises `SHIFT_DATE` against `study_date` on a
    `Study`, which has no `set_attr`, so every flagged study date goes
    through this arm.

    The setup arrives at REMEDIATED for *something else* rather than by
    shifting the same date twice: the study scan returns early once
    `date_shifted` is set, so a study cannot be re-flagged for its own
    date. A hydrated study already remediated for any reason is the
    reachable state.
    """
    study = _as_reloaded(Study("S1", "20230101"))

    RemediationService(project_secret=FIXED_A).apply_remediation(
        [_finding(study, "SHIFT_DATE", "study_date",
                  original="20230101", metadata={"patient_id": "PAT-7"})])

    assert study.study_date != "20230101"
    assert study.has_unsaved_changes, (
        "the entity reports no unsaved changes after its PHI was "
        "stripped, so the next save skips it and the value stays in "
        "the database")


def test_removing_a_second_tag_after_a_reload_still_needs_a_save():
    """Pins `entity.mark_modified()` at remediation.py line 427.

    That is the `REMOVE_TAG` arm that `del`s from `attributes` -- a
    plain dict, so the deletion bumps no revision by itself.
    """
    inst = Instance("1.2.3", "1.2.840.10008.5.1.4.1.1.7", 1)
    inst.set_attr("0008,0080", "MERCY GENERAL")
    _as_reloaded(inst)

    RemediationService().apply_remediation(
        [_finding(inst, "REMOVE_TAG", "0008,0080", original="MERCY GENERAL")])

    assert "0008,0080" not in inst.attributes
    assert inst.has_unsaved_changes, (
        "the entity reports no unsaved changes after its PHI was "
        "stripped, so the next save skips it and the value stays in "
        "the database")


def test_removing_a_private_sequence_after_a_reload_still_needs_a_save():
    """Pins `entity.mark_modified()` at remediation.py line 448.

    That is the private-sequence arm added by #167, which `del`s from
    `sequences`.
    """
    inst = Instance("1.2.3", "1.2.840.10008.5.1.4.1.1.7", 1)
    inst.add_sequence_item("0009,1010", DicomItem())
    _as_reloaded(inst)

    RemediationService().apply_remediation(
        [_finding(inst, "REMOVE_TAG", "0009,1010")])

    assert "0009,1010" not in inst.sequences
    assert inst.has_unsaved_changes, (
        "the entity reports no unsaved changes after its PHI was "
        "stripped, so the next save skips it and the value stays in "
        "the database")


def _owner_holding(field):
    """A Patient, Study or Series holding a value in `field`, one of the
    fields the `REMOVE_TAG` Python-attribute arm may clear."""
    if field in ("patient_name", "patient_id"):
        return Patient(patient_name="DOE^JOHN", patient_id="PAT-7")
    if field in ("study_date", "study_instance_uid"):
        return Study("1.2.3", "20230101")
    return Series("1.2.3.4", "CT", 1)


def _class_declaring(field):
    """The one entity class whose dataclass fields include `field`."""
    [cls] = [cls for cls in (Patient, Study, Series)
             if field in {f.name for f in dataclasses.fields(cls)}]
    return cls


def test_every_field_the_remove_arm_may_clear_is_a_tracked_field():
    """Stands where the pin of the `REMOVE_TAG` Python-attribute arm's
    `mark_modified()` stood. #961 deleted that call: the arm is
    `setattr(entity, attr, None)`, #679 restricts it to
    `ENTITY_FIELD_TAGS`, and assigning a tracked field marks the entity
    itself (#767), so the call bore no path. That is true only while
    every field in the table is tracked by the class that declares it.
    Untrack one, or add an untracked field to the table, and this is red
    before a cleared field goes unsaved.

    Since #949 the arm declines the three keys the store holds a row by
    (`_STORE_KEY_FIELDS`); they stay in the table, which the stamping
    rules read, so they are still checked here.
    """
    fields = set(RemediationService.ENTITY_FIELD_TAGS)
    assert fields == {"patient_name", "patient_id", "study_date",
                      "study_instance_uid", "series_instance_uid"}
    for field in sorted(fields):
        cls = _class_declaring(field)
        assert field in cls._TRACKED_FIELDS, (
            f"{cls.__name__}.{field} can be cleared by a REMOVE but an "
            "assignment to it does not mark the entity, so the cleared "
            "field would not be saved")


@pytest.mark.parametrize("field", sorted(
    set(RemediationService.ENTITY_FIELD_TAGS)
    - RemediationService._STORE_KEY_FIELDS))
def test_clearing_an_owner_field_after_a_reload_still_needs_a_save(field):
    """The `REMOVE_TAG` Python-attribute arm, which sets the attribute to
    None, over every field it may clear. Behaviour, and no line: what
    marks the entity is `entities._assign_tracked_field`, since #961
    deleted the arm's own `mark_modified()`.

    Not the three keys the store holds a row by (`patient_id` and the
    two owned UIDs): since #949 the arm declines those, which
    `tests/test_declined_remediation_is_recorded.py` pins.
    """
    owner = _as_reloaded(_owner_holding(field))
    assert getattr(owner, field) is not None

    assert RemediationService().apply_remediation(
        [_finding(owner, "REMOVE_TAG", field)]) == 1

    assert getattr(owner, field) is None
    assert owner.has_unsaved_changes, (
        "the entity reports no unsaved changes after its PHI was "
        "stripped, so the next save skips it and the value stays in "
        "the database")
