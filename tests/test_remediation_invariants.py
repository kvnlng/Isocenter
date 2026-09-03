"""Behaviour in `remediation.py` that nothing was holding in place (#132).

Found by `scripts/mutation_probe.py` once #106 gave it operators that
reach straight-line code: 14 of 35 sampled mutations to the module that
*applies* de-identification survived. The code was right in every case
below; no test would have noticed it changing.

Each test here corresponds to a specific surviving mutant, named in its
docstring, so a future reader can tell what it is defending against
rather than guessing from the assertion.
"""
import pytest

from isocenter.entities import DicomItem, Instance, Patient, PhiStatus, Study
from isocenter.privacy import PhiFinding, PhiRemediation
from isocenter.remediation import RemediationService


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
# The de-identification method code sequence (0012,0064)
# --------------------------------------------------------------------

def test_the_deid_method_code_carries_its_coding_scheme():
    """Mutant: `item.set_attr("0008,0102", "DCM")` deleted, and survived.

    A Code Sequence item is a triple: Code Value, Coding Scheme
    Designator, Code Meaning. Drop the designator and `113100` names
    nothing -- code values are only unique within a scheme, so a reader
    cannot tell "Basic Application Confidentiality Profile" from any
    other registry's 113100.

    This is the 0.8.1 family exactly: the exported artefact asserting
    something a consumer has no way to check. Here the assertion is the
    de-identification conformance claim itself.
    """
    inst = _saved_instance()
    RemediationService().add_global_deid_tags(inst)

    item = inst.sequences["0012,0064"].items[0]
    assert item.attributes.get("0008,0100") == "113100"
    assert item.attributes.get("0008,0102") == "DCM", \
        "the code value has no scheme, so it identifies nothing"
    assert item.attributes.get("0008,0104") == \
        "Basic Application Confidentiality Profile"


def test_stamping_twice_does_not_duplicate_the_code_item():
    """The dedup check reads `0008,0100`; nothing pinned that it works."""
    inst = _saved_instance()
    service = RemediationService()
    service.add_global_deid_tags(inst)
    service.add_global_deid_tags(inst)

    codes = [i.attributes.get("0008,0100")
             for i in inst.sequences["0012,0064"].items]
    assert codes == ["113100"], codes


def test_the_deid_method_string_is_not_repeated():
    inst = _saved_instance()
    service = RemediationService()
    service.add_global_deid_tags(inst)
    service.add_global_deid_tags(inst)

    assert inst.attributes["0012,0063"] == ["Isocenter Privacy Profile"]


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
# The five `mark_modified()` calls, one test each (#173, #132)
# --------------------------------------------------------------------
#
# On a *first* remediation each of these calls is redundant:
# `record_phi_status(REMEDIATED)` on the shared success path also
# advances the revision, so deleting the bump alone stays green -- that
# is what the tests above say, honestly, in their docstrings. After a
# reload it is the only thing left. A hydrated entity already reads
# REMEDIATED, so `record_phi_status(REMEDIATED)` short-circuits (the
# guard reads the `phi_status` property, which still returns REMEDIATED
# because nothing moved the revision) and no bump happens. The PHI is
# stripped from memory, the entity reports nothing to save, the next
# save skips it, and the identifier stays in the database (#173).
#
# Each test below drives exactly one of the five arms and names it, so a
# deletion anywhere in the cluster turns exactly one test red.


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
    """Pins `entity.mark_modified()` at remediation.py line 148.

    That is the `REPLACE_TAG` Python-attribute arm -- the one a
    `Patient` takes, having no `set_attr`.
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


def test_shifting_a_study_date_after_a_reload_still_needs_a_save():
    """Pins `entity.mark_modified()` at remediation.py line 181.

    That is the `SHIFT_DATE` `setattr` arm, and it is not a corner: the
    inspector's study scan raises `SHIFT_DATE` against `study_date` on a
    `Study`, which has no `set_attr`, so every flagged study date goes
    through this line.

    The setup arrives at REMEDIATED for *something else* rather than by
    shifting the same date twice: the study scan returns early once
    `date_shifted` is set, so a study cannot be re-flagged for its own
    date. A hydrated study already remediated for any reason is the
    reachable state.
    """
    study = _as_reloaded(Study("S1", "20230101"))

    RemediationService().apply_remediation(
        [_finding(study, "SHIFT_DATE", "study_date",
                  original="20230101", metadata={"patient_id": "PAT-7"})])

    assert study.study_date != "20230101"
    assert study.has_unsaved_changes, (
        "the entity reports no unsaved changes after its PHI was "
        "stripped, so the next save skips it and the value stays in "
        "the database")


def test_removing_a_second_tag_after_a_reload_still_needs_a_save():
    """Pins `entity.mark_modified()` at remediation.py line 218.

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
    """Pins `entity.mark_modified()` at remediation.py line 239.

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


def test_clearing_a_patient_attribute_after_a_reload_still_needs_a_save():
    """Pins `entity.mark_modified()` at remediation.py line 247.

    That is the `REMOVE_TAG` Python-attribute arm, which sets the
    attribute to None.
    """
    patient = _as_reloaded(Patient(patient_name="DOE^JOHN", patient_id="PAT-7"))

    RemediationService().apply_remediation(
        [_finding(patient, "REMOVE_TAG", "patient_id", original="PAT-7")])

    assert patient.patient_id is None
    assert patient.has_unsaved_changes, (
        "the entity reports no unsaved changes after its PHI was "
        "stripped, so the next save skips it and the value stays in "
        "the database")
