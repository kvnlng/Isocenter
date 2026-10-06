"""The first-lock replacement refusal names the lock that works (#593,
owner ruling Q3 A, 2026-10-06).

A first `lock_identities()` after `anonymize()` is refused for a tag the
pass replaced: "Lock identities before anonymize()". After the pass has
run, the only way to follow that is to drop the tag, and the message named
one tag and gave no list. Measured on `main` at de5b26d9 (CT_small with an
Accession Number, the default `tags_to_lock`): under the floor it took
**four** calls to get a lock through -- refused for `0010,0010`, then for
`0010,0020`, then (the #574 refusal, which does give a list) for the
emptied `0008,0050` -- and the same under `basic@2026c`.

Now the refusal advises the caller's `tags_to_lock` less **every** tag
holding a replacement and **every** tag the pass emptied or removed, so
the advised call locks: two calls. Its opening sentence is unchanged, and
it still names no patient and no value but the replacement it found.

**A re-lock keeps the message it had.** Where the patient already carries
an identity token of this library, dropping the replaced tags is refused
by the loss check ("this lock would replace it with nothing"), so no list
works and the true advice is the old one: do not re-lock a patient after
`anonymize()`.

`isocenter.session` is named here, so its probe row is charged.
"""
import ast
import re

import pytest

from isocenter.entities import DicomItem
from isocenter.session import DicomSession, _DEFAULT_TAGS_TO_LOCK

from support.ct_small_files import write_ct

PID, NAME, ACCESSION = "PID-593", "Alpha^One", "ACC-593"
SEQ, SYNTAX, CONTENT = "0400,0500", "0400,0510", "0400,0520"

OPENING = ("lock_identities: this patient already carries a replacement in "
           "{tag} ({shown}), so there is no original identity left to stash. ")
ADVICE = ("Lock identities before anonymize(). To lock this patient now "
          "without {dropped}, call lock_identities(<its Patient ID>, "
          "tags_to_lock={rest!r}); the token this call would have written "
          "is unchanged.")
NOTHING_ELSE = ("Lock identities before anonymize(). tags_to_lock names no "
                "tag that still holds an original, so there is nothing else "
                "to lock; the token this call would have written is unchanged.")
#: The whole message as it has been since #492, kept for a re-lock.
RELOCK = ("Lock identities before anonymize(), and do not re-lock a patient "
          "after it; the token this call would have written is unchanged.")


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _session(tmp_path, config=None, lock_first=False, anonymize=True):
    """CT_small as `PID-593` with an Accession Number, under `config` (None
    is the floor), optionally locked first, then anonymized."""
    write_ct(tmp_path / "in" / "a.dcm", PID, "5930", name=NAME, accession=ACCESSION)
    session = DicomSession(str(tmp_path / "s.db"))
    session.enable_reversible_anonymization(str(tmp_path / "k.key"))
    if config is not None:
        path = tmp_path / "c.yaml"
        path.write_text(config, encoding="utf-8")
        session.load_config(str(path))
    session.ingest(str(tmp_path / "in"))
    if lock_first:
        assert len(session.lock_identities(PID)) == 1
    if anonymize:
        session.anonymize(session.audit())
    return session


def _only(session):
    [patient] = session.store.patients
    [inst] = [i for st in patient.studies for se in st.series for i in se.instances]
    return patient, inst


def _refusal(session, patient_id, **kwargs):
    with pytest.raises(RuntimeError) as raised:
        session.lock_identities(patient_id, **kwargs)
    return str(raised.value)


def _advised(message):
    found = re.search(r"lock_identities\(<its Patient ID>, tags_to_lock=(\[.*?\])\)",
                      message)
    assert found, message
    return ast.literal_eval(found.group(1))


def _value_free(message, *secrets):
    for secret in (PID, NAME, ACCESSION, *secrets):
        assert secret not in message, message


@pytest.mark.parametrize("config, dropped, rest", [
    (None, "0010,0010, 0010,0020, 0008,0050", ["0010,0030", "0010,0040"]),
    ("privacy_profile: basic\n", "0010,0010, 0010,0020, 0010,0040, 0008,0050",
     ["0010,0030"]),
], ids=["floor", "basic"])
def test_a_first_lock_after_anonymize_is_told_the_lock_that_works(
        tmp_path, config, dropped, rest):
    """The whole message, and then the advised call is made and locks.
    The second half is the point: a list that reads well and does not
    lock would pass the first. The floor keeps Patient's Sex and `basic`
    empties it, so the two lists differ: kills a hard-coded list, the
    first replaced tag named alone, and the emptied tags left in the
    advice (the advised call would then be refused)."""
    with _session(tmp_path, config) as session:
        patient, inst = _only(session)
        pseudonym = patient.patient_id
        message = _refusal(session, pseudonym)
        assert message == (OPENING.format(tag="0010,0010", shown="'ANONYMIZED'")
                           + ADVICE.format(dropped=dropped, rest=rest))
        _value_free(message, pseudonym)
        assert SEQ not in inst.sequences
        assert _advised(message) == rest
        assert len(session.lock_identities(pseudonym, tags_to_lock=_advised(message))) == 1
        assert session.reversibility_service.token_of_ours(inst) is not None


def test_a_lock_naming_only_replaced_tags_is_told_there_is_nothing_else(tmp_path):
    with _session(tmp_path) as session:
        patient, _inst = _only(session)
        message = _refusal(session, patient.patient_id, tags_to_lock=["0010,0020"])
        assert message == (
            OPENING.format(tag="0010,0020", shown="a replacement Patient ID")
            + NOTHING_ELSE)
        _value_free(message, patient.patient_id)


def test_a_relock_keeps_the_message_it_had(tmp_path):
    """Locked, then anonymized, then locked again: no list works (dropping
    the replaced tags is refused, "would replace it with nothing"), so the
    message is today's, whole, and advises none. Kills the advice given on
    a re-lock."""
    with _session(tmp_path, lock_first=True) as session:
        patient, _inst = _only(session)
        message = _refusal(session, patient.patient_id)
        assert message == (OPENING.format(tag="0010,0010", shown="'ANONYMIZED'")
                           + RELOCK)
        assert "tags_to_lock=" not in message
        # And the list the advice would have given is indeed refused.
        after = _refusal(session, patient.patient_id,
                         tags_to_lock=["0010,0030", "0010,0040"])
        assert "already has a locked identity holding" in after, after


def test_a_token_in_the_layout_before_1_0_counts_as_a_relock(tmp_path):
    """The patient's token is one this library wrote before 1.0, which 1.x
    does not read: a lock would replace it, so it is a re-lock, and the
    replacement refusal -- still raised first -- gives no list."""
    with _session(tmp_path, lock_first=True) as session:
        patient, inst = _only(session)
        [item] = inst.sequences[SEQ].items
        token, syntax = item.attributes[CONTENT], item.attributes[SYNTAX]
        item.attributes[SYNTAX], item.attributes[CONTENT] = token, syntax
        message = _refusal(session, patient.patient_id)
        assert message == (OPENING.format(tag="0010,0010", shown="'ANONYMIZED'")
                           + RELOCK)


def test_a_foreign_encrypted_attributes_item_is_still_a_first_lock(tmp_path):
    """An Encrypted Attributes item this library did not write is no token
    of ours, and a lock replaces it: a first lock, so the advice is given,
    and the advised call locks."""
    with _session(tmp_path) as session:
        patient, inst = _only(session)
        foreign = DicomItem()
        foreign.set_attr(SYNTAX, "1.2.840.10008.1.2")
        foreign.set_attr(CONTENT, b"0\x82\x01\x00CMS")
        inst.add_sequence_item(SEQ, foreign)
        assert session.reversibility_service.token_of_ours(inst) is None
        message = _refusal(session, patient.patient_id)
        assert message == (
            OPENING.format(tag="0010,0010", shown="'ANONYMIZED'")
            + ADVICE.format(dropped="0010,0010, 0010,0020, 0008,0050",
                            rest=["0010,0030", "0010,0040"]))
        assert len(session.lock_identities(patient.patient_id,
                                           tags_to_lock=_advised(message))) == 1
        assert session.reversibility_service.token_of_ours(inst) is not None


def test_a_replacement_on_a_later_study_is_dropped_from_the_advice(tmp_path):
    """Two studies: the first raw, the second holding the Accession Number
    a pass wrote (its record says so). `0008,0050` is dropped from the
    advice though the first value-set holds an original, and the advised
    call locks both instances. Kills the replaced tags read from the first
    value-set only (the advice would name `0008,0050`, and its call would
    be refused)."""
    write_ct(tmp_path / "in" / "a.dcm", PID, "5931", name=NAME, accession="ACC-ONE")
    write_ct(tmp_path / "in" / "b.dcm", PID, "5932", name=NAME, accession="ACC-TWO")
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.enable_reversible_anonymization(str(tmp_path / "k.key"))
        session.ingest(str(tmp_path / "in"))
        [patient] = session.store.patients
        instances = [i for st in patient.studies for se in st.series for i in se.instances]
        assert [i.attributes["0008,0050"] for i in instances] == ["ACC-ONE", "ACC-TWO"]
        instances[1].record_remediation("0008,0050", "PASS-OUTPUT")
        instances[1].set_attr("0008,0050", "PASS-OUTPUT")
        message = _refusal(session, PID)
        rest = [tag for tag in _DEFAULT_TAGS_TO_LOCK if tag != "0008,0050"]
        assert message == (
            OPENING.format(tag="0008,0050", shown="'PASS-OUTPUT'")
            + ADVICE.format(dropped="0008,0050", rest=rest))
        assert len(session.lock_identities(PID, tags_to_lock=_advised(message))) == 2
