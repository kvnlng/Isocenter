"""A second `lock_identities()` is the one recovery answers with (#399).

`ReversibilityService.embed_identity_token` appended an item to the
Encrypted Attributes Sequence `(0400,0500)` on every call while
`recover_original_data` read item **0**, so the *first* capture won
forever: a re-lock was accepted, reported as success, persisted and
exported, and then ignored at the one moment the token is read. Every
stale capture shipped in the file, encrypted with the same key as the
live one.

The four tests below that exercise the embed are red before the fix.
The fifth is green on both trees and is the freeze made executable --
see its own docstring, which says why deleting it would be silent.

Every graph here is built by hand rather than ingested. That is
deliberate and it is what test 3 turns on: an ingested instance is
dirty for reasons of its own between two locks, which would make
`has_unsaved_changes` True without the `mark_modified()` the fix adds
and leave the assertion measuring nothing.
"""
from datetime import date

import numpy as np

from isocenter.entities import DicomItem, Instance, Patient, Series, Study
from isocenter.session import DicomSession

SEQ = "0400,0500"
CONTENT = "0400,0510"
SYNTAX = "0400,0520"
PID = "REV_399"


def _build_patient(session, patient_id=PID, name="Original^Name"):
    """One patient, one study, one series, one instance, no file behind it."""
    patient = Patient(patient_id, name)
    study = Study("ST_1", date(2023, 1, 1))
    study.study_time = "120000"
    series = Series("SE_1", "CT", 1)
    inst = Instance("SOP_1", "1.2.840.10008.5.1.4.1.1.2", 1)
    inst.file_path = None
    inst.set_attr("0010,0010", name)
    inst.set_attr("0010,0020", patient_id)
    inst.set_pixel_data(np.zeros((8, 8), dtype=np.uint16))
    series.instances.append(inst)
    study.series.append(series)
    patient.studies.append(study)
    session.store.patients.append(patient)
    return inst


def _items(instance):
    sequence = instance.sequences.get(SEQ)
    return list(sequence.items) if sequence is not None else []


def test_a_second_lock_leaves_one_token_item(tmp_path):
    """Two locks, one item.

    Asserted as a count and separately from what recovery answers,
    because a fix that inserts the new token at index 0 without
    clearing the rest satisfies every recovery assertion in this file
    and still grows the sequence -- and every stale item still ships in
    the exported file.
    """
    with DicomSession(str(tmp_path / "relock_count.db")) as session:
        session.enable_reversible_anonymization(str(tmp_path / "isocenter.key"))
        inst = _build_patient(session)

        session.lock_identities(PID, tags_to_lock=["0010,0010"])
        assert len(_items(inst)) == 1

        session.lock_identities(PID, tags_to_lock=["0010,0010"])
        assert len(_items(inst)) == 1


def test_a_re_lock_is_what_recovery_answers_with(tmp_path):
    """The second capture wins, asserted by the value that differs.

    The first capture's keys are a strict subset of the second's, so
    `"0010,0010" in recovered` is true of both captures and pins
    nothing. The tag set *and* the value change between the two locks,
    and the assertion is full dict equality.
    """
    with DicomSession(str(tmp_path / "relock_recover.db")) as session:
        session.enable_reversible_anonymization(str(tmp_path / "isocenter.key"))
        inst = _build_patient(session)

        session.lock_identities(PID, tags_to_lock=["0010,0010"])
        assert session.reversibility_service.recover_original_data(inst) == {
            "0010,0010": "Original^Name"}

        inst.set_attr("0010,0010", "CHANGED^Value")
        session.lock_identities(PID, tags_to_lock=["0010,0010", "0010,0020"])

        assert session.reversibility_service.recover_original_data(inst) == {
            "0010,0010": "CHANGED^Value", "0010,0020": PID}


def test_a_re_lock_reaches_the_store(tmp_path):
    """The re-lock tells the store it happened.

    This is the only test that sees the `mark_modified()` line in
    `embed_identity_token`, and it sees it only because there is **no
    other mutation between the two locks**: any `set_attr` in between
    dirties the instance for its own reasons and hides the hole.
    `add_sequence()` marks modified only when it *creates*, so a fix
    that reaches into `items` in place advances no revision on the
    second lock, the next `save()` skips the instance, and memory and
    the store disagree about the identity with nothing saying so.

    It saves through `save(sync=True)` and never `persist=True`:
    `SqliteStore.update_attributes()` writes every instance handed to
    it with no dirty check at all, so `persist=True` would write the
    new token whether or not the revision moved and would mask exactly
    the trap this test exists for.
    """
    db_path = str(tmp_path / "relock_store.db")
    key_path = str(tmp_path / "isocenter.key")

    with DicomSession(db_path) as session:
        session.enable_reversible_anonymization(key_path)
        inst = _build_patient(session)

        session.lock_identities(PID, tags_to_lock=["0010,0010"])
        session.save(sync=True)
        assert not inst.has_unsaved_changes, (
            "precondition: the first lock must be flushed before the second, "
            "or the dirty flag below is left over from it")

        session.lock_identities(PID, tags_to_lock=["0010,0020"])
        assert inst.has_unsaved_changes, (
            "the re-lock replaced the token in memory without telling the "
            "store; the next save() will skip this instance (#399)")
        session.save(sync=True)

    with DicomSession(db_path) as reloaded:
        reloaded.enable_reversible_anonymization(key_path)
        stored = reloaded.store.patients[0].studies[0].series[0].instances[0]
        assert len(_items(stored)) == 1
        assert reloaded.reversibility_service.recover_original_data(stored) == {
            "0010,0020": PID}


def test_locking_over_a_foreign_encrypted_attributes_sequence_recovers(tmp_path):
    """An instance whose source already carried `(0400,0500)` is recoverable.

    Before #399 it was not, at all: the foreign item sat at index 0,
    the token was appended at index 1, `recover_original_data` handed
    the foreign blob to the engine, logged a failure and returned
    `None`. The foreign item is replaced rather than kept -- that is a
    source element this library overwrites, recorded in the CHANGELOG
    and filed as an owner question, not an oversight.

    The instance must carry the tag being locked, or `original_attrs`
    is empty, `embed_identity_token` returns on `if not token`, and the
    arm under test is never entered -- which would leave this test red
    on the fixed tree too.
    """
    with DicomSession(str(tmp_path / "relock_foreign.db")) as session:
        session.enable_reversible_anonymization(str(tmp_path / "isocenter.key"))
        inst = _build_patient(session)

        foreign = DicomItem()
        foreign.set_attr(CONTENT, b"NOT-OUR-TOKEN")
        foreign.set_attr(SYNTAX, "1.2.840.10008.1.2")
        inst.add_sequence_item(SEQ, foreign)
        assert len(_items(inst)) == 1
        assert session.reversibility_service.recover_original_data(inst) is None

        session.lock_identities(PID, tags_to_lock=["0010,0010"])

        recovered = session.reversibility_service.recover_original_data(inst)
        assert recovered is not None, (
            "the foreign item is still at index 0 and recovery cannot read "
            "past it (#399)")
        assert recovered == {"0010,0010": "Original^Name"}
        assert len(_items(inst)) == 1


def test_a_legacy_three_item_sequence_is_still_read_at_item_zero(tmp_path):
    """A file written before #399 stays recoverable at item 0.

    Green before the fix and green after it, and it is not decoration.
    `docs/api/stability.md` promises that a file exported with
    reversible anonymization is recoverable by every 1.x with its key.
    After #399 every sequence this library writes holds exactly one
    item, so `items[0]`, `items[-1]` and `items[len(items) // 2]` are
    the same expression on every file it will ever write again -- and a
    later reader "simplifying" recovery to the most recent item would
    be green on the whole suite while breaking every multi-item file
    0.9.4 shipped. This test is the only thing in the tree that can
    tell those spellings apart, and the three items must therefore
    encrypt three *distinct* identities.
    """
    with DicomSession(str(tmp_path / "relock_legacy.db")) as session:
        session.enable_reversible_anonymization(str(tmp_path / "isocenter.key"))
        # Since #539 enable creates no key and the first lock does; this
        # test drives the service directly, so it creates the key as the
        # lock would.
        session.key_manager.load_or_generate_key()
        inst = _build_patient(session)
        service = session.reversibility_service

        captures = [
            {"0010,0010": "First^Capture"},
            {"0010,0010": "Second^Capture"},
            {"0010,0010": "Third^Capture"},
        ]
        for capture in captures:
            item = DicomItem()
            item.set_attr(CONTENT, service.generate_identity_token(capture))
            item.set_attr(SYNTAX, service.PAYLOAD_TRANSFER_SYNTAX)
            inst.add_sequence_item(SEQ, item)

        assert len(_items(inst)) == 3
        assert service.recover_original_data(inst) == captures[0]


def test_the_token_item_names_its_payload_transfer_syntax(tmp_path):
    """Each token item carries `(0400,0520)`, and a re-lock keeps it (#439).

    Recovery never reads the Transfer Syntax UID, so deleting the
    `set_attr` that writes it left every recovery test green -- yet the
    item is exported, and PS3.15's Encrypted Attributes item carries it
    to say how the decrypted payload is encoded. Asserted after the
    second lock too, because the re-lock replaces the item (#399): a
    replacement built without it would lose it from then on.
    """
    with DicomSession(str(tmp_path / "relock_syntax.db")) as session:
        session.enable_reversible_anonymization(str(tmp_path / "isocenter.key"))
        inst = _build_patient(session)

        session.lock_identities(PID, tags_to_lock=["0010,0010"])
        items = _items(inst)
        assert len(items) == 1
        assert items[0].attributes.get(SYNTAX) == "1.2.840.10008.1.2"

        session.lock_identities(PID, tags_to_lock=["0010,0010"])
        items = _items(inst)
        assert len(items) == 1
        assert items[0].attributes.get(SYNTAX) == "1.2.840.10008.1.2"
