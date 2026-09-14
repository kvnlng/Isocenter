"""A lock refuses a token the session's key cannot open, instead of
replacing it (#617); and it creates no key where one could open nothing
(Q8).

Measured on 347ee93 (`probes-I/p_key.py wrongkey_relock`): the plan read
the existing token through the tolerant `recover_original_data`, which
answers `None` both for "no token" and for "this key cannot open it", so
a re-lock under a mistyped key path succeeded, created that key file,
logged one `Failed to recover data from <uid>: InvalidToken` line and
replaced a token the real key opened; recovery under the real key then
raised. Nothing was lost on that path only because the originals were
still in the clear; after `anonymize()` under a `KEEP` or `value:` rule
the same shape stashes pass output under a key nobody holds.

Now the plan reads every distinct token on the patient through a strict
read and refuses when a token **of ours** does not open. An Encrypted
Attributes Sequence that did not come from this library (no Fernet token
in `(0400,0510)`) is still replaced, as 0.9.4 released (#399): the two
are told apart by the token's own format -- base64url whose first
decoded byte is `0x80` -- so a truncated token of ours is a refusal, not
a silent replacement.

Two shapes of "the key cannot open it", two exact messages, both pinned:
a key file that exists and is the wrong key (refused per patient inside
the plan, numbered in a batch), and no key file at the path at all
(refused before any plan, once; and **no key is created**, where until
now the lock minted one that opened nothing at the path the message then
named -- the #539 debris shape, Q8).

**Why this file imports what it does.** `isocenter.session` and
`isocenter.reversibility` are named, so their probe rows are charged.
"""
import base64
import logging
import os
from datetime import date

import numpy as np
import pytest
from cryptography.fernet import Fernet

from isocenter.entities import DicomItem, Instance, Patient, Series, Study
from isocenter.reversibility import ReversibilityService
from isocenter.session import DicomSession

from support.ct_small_files import write_ct

PID_A, PID_B = "PAT-617-A", "PAT-617-B"
NAME_A, NAME_B = "Secret^A", "Secret^B"
TAGS = ["0010,0010", "0010,0020"]
SEQ, CONTENT, SYNTAX = "0400,0500", "0400,0510", "0400,0520"
FORMS = ("single", "batch", "report")
SHAPES = ("wrong_key", "no_key_file")


def wrong_key_refusal(path):
    """The #617 refusal for a key file that does not open the token."""
    return ("lock_identities: this patient carries an identity token that the "
            f"key at {path} does not decrypt, and this lock would replace it. "
            "Enable reversible anonymization with the key the identity was "
            "locked with; the token this call would have written is unchanged.")


def no_key_refusal(path):
    """The Q8 refusal: no key file, and a token of ours in the session."""
    return (f"lock_identities: there is no key file at {path}, and this session "
            "holds an identity token this library wrote, which a key created "
            "here could not open and a lock would replace. Enable reversible "
            "anonymization with the key the identities were locked with; no key "
            "was created, and the token this call would have written is unchanged.")


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _first(session, pid):
    patient = next(p for p in session.store.patients if p.patient_id == pid)
    return patient.studies[0].series[0].instances[0]


def _token(instance):
    seq = instance.sequences.get(SEQ)
    return bytes(seq.items[0].attributes[CONTENT]) if seq is not None and seq.items else None


@pytest.fixture(name="store")
def _store(tmp_path):
    """A saved store: PID_A locked under `real.key` before any pass, PID_B
    never locked. Returns (db, real key path, PID_A's token)."""
    write_ct(tmp_path / "in" / "a.dcm", PID_A, "6171", name=NAME_A)
    write_ct(tmp_path / "in" / "b.dcm", PID_B, "6172", name=NAME_B)
    db, real = str(tmp_path / "s.db"), str(tmp_path / "real.key")
    with DicomSession(db) as session:
        session.ingest(str(tmp_path / "in"))
        session.enable_reversible_anonymization(real)
        session.lock_identities(PID_A, tags_to_lock=TAGS, persist=True)
        token = _token(_first(session, PID_A))
        session.save(sync=True)
    return db, real, token


def _other_key(tmp_path, shape):
    """The key path the second session enables: a fresh valid key that is
    the wrong one, or a path with no file."""
    other = tmp_path / "other.key"
    if shape == "wrong_key":
        other.write_bytes(Fernet.generate_key())
    return str(other)


def _lock(session, form, *pids):
    if form == "single":
        return session.lock_identities(pids[0], tags_to_lock=TAGS)
    if form == "batch":
        return session.lock_identities(list(pids), tags_to_lock=TAGS)
    return session.lock_identities(session.audit(), tags_to_lock=TAGS)


def _refusal(shape, path, place=None):
    if shape == "no_key_file":
        return no_key_refusal(path)
    text = wrong_key_refusal(path)
    if place is None:
        return text
    n, m = place
    return (f"lock_identities: 1 of {m} patients cannot be locked as asked, so no "
            "patient was locked. Each is numbered by its place among the patients "
            "found, in Patient ID order. Lock the others without these, and each of "
            f"these as its message says:\n[{n} of {m}] {text}")


@pytest.mark.parametrize("form", FORMS)
@pytest.mark.parametrize("shape", SHAPES)
def test_a_relock_under_a_key_that_does_not_open_the_held_token_is_refused(
        store, tmp_path, form, shape, caplog):
    """T13. Under the wrong key (or none), every lock form is refused with
    its exact message, the token is unchanged, nobody is locked, no key
    file appears where there was none, and recovery under the real key
    then succeeds. Kills `is_one_of_ours` always False (M16: the token
    reads as foreign and is replaced) and the plan swallowing the strict
    read's raise (M17)."""
    db, real, token = store
    other = _other_key(tmp_path, shape)
    with DicomSession(db) as session:
        session.enable_reversible_anonymization(other)
        with caplog.at_level(logging.DEBUG, logger="isocenter"):
            with pytest.raises(RuntimeError) as caught:
                _lock(session, form, PID_A, PID_B)
        expected = _refusal(shape, other, (1, 2) if form != "single" else None)
        assert str(caught.value) == expected
        assert _token(_first(session, PID_A)) == token
        assert _token(_first(session, PID_B)) is None, "another patient was locked"
        assert os.path.exists(other) == (shape == "wrong_key"), "a key was created"
        assert not any("Failed to recover" in r.getMessage() for r in caplog.records)
    with DicomSession(db) as session:
        session.enable_reversible_anonymization(real)
        session.recover_patient_identity(PID_A, restore=False)
        assert session.reversibility_service.recover_original_data(
            _first(session, PID_A)) == {"0010,0010": NAME_A, "0010,0020": PID_A}


@pytest.mark.parametrize("shape", SHAPES)
def test_the_wrong_key_refusal_names_the_key_path_and_no_patient(store, tmp_path, shape):
    """T16 (P6). The key path is the caller's own argument and is named;
    the Patient ID, the name and the SOP UID are not; and the raise is
    `from None`, so no `InvalidToken` traceback carries a value. Kills a
    value interpolated (M14) and a bare re-raise."""
    db, _, _ = store
    other = _other_key(tmp_path, shape)
    with DicomSession(db) as session:
        session.enable_reversible_anonymization(other)
        uid = _first(session, PID_A).sop_instance_uid
        with pytest.raises(RuntimeError) as caught:
            session.lock_identities(PID_A, tags_to_lock=TAGS)
    message = str(caught.value)
    assert other in message
    for secret in (PID_A, NAME_A, uid):
        assert secret not in message, message
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def _hand_patient(session, pid=PID_A, name=NAME_A):
    patient = Patient(pid, name)
    study = Study("ST_1", date(2023, 1, 1))
    series = Series("SE_1", "CT", 1)
    inst = Instance("SOP_1", "1.2.840.10008.5.1.4.1.1.2", 1)
    inst.file_path = None
    inst.set_attr("0010,0010", name)
    inst.set_attr("0010,0020", pid)
    inst.set_pixel_data(np.zeros((8, 8), dtype=np.uint16))
    series.instances.append(inst)
    study.series.append(series)
    patient.studies.append(study)
    session.store.patients.append(patient)
    return inst


def _foreign_item(content):
    item = DicomItem()
    item.set_attr(CONTENT, content)
    item.set_attr(SYNTAX, "1.2.840.10008.1.2")
    return item


def test_a_foreign_encrypted_attributes_sequence_is_still_replaced(tmp_path):
    """T14. #399's released behaviour: a `(0400,0500)` from a source file,
    holding no Fernet token, is replaced, and -- with no token of ours
    anywhere -- the first lock still creates the key. Kills
    `is_one_of_ours` always True (M18) and the Q8 sniff counting a
    foreign blob."""
    key = tmp_path / "k.key"
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.enable_reversible_anonymization(str(key))
        inst = _hand_patient(session)
        inst.add_sequence_item(SEQ, _foreign_item(b"NOT-OUR-TOKEN"))
        assert not key.exists()
        session.lock_identities(PID_A, tags_to_lock=["0010,0010"])
        assert key.exists()
        assert session.reversibility_service.recover_original_data(inst) == {
            "0010,0010": NAME_A}
        assert len(inst.sequences[SEQ].items) == 1


def test_a_truncated_token_of_ours_is_refused_not_replaced(tmp_path):
    """T15. Our token cut to 41 characters is still shaped like ours (the
    sniff reads the first 12), so it is a refusal under the key that
    wrote it, not a replacement. 41 and not 40: a whole-string base64
    check fails on 41 with "incorrect padding" and would call it foreign
    (M19)."""
    key = tmp_path / "k.key"
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.enable_reversible_anonymization(str(key))
        inst = _hand_patient(session)
        session.lock_identities(PID_A, tags_to_lock=TAGS)
        cut = _token(inst)[:41]
        inst.sequences[SEQ].items[0].set_attr(CONTENT, cut)
        with pytest.raises(RuntimeError) as caught:
            session.lock_identities(PID_A, tags_to_lock=TAGS)
        assert str(caught.value) == wrong_key_refusal(str(key))
        assert _token(inst) == cut


@pytest.mark.parametrize("content,ours", [
    (b"NOT-OUR-TOKEN", False),
    (b"", False),
    (b"gAAAAAB", False),                       # shorter than the 12 the sniff reads
    (b"gAAAAABxxxxx", True),                    # ours-shaped, whatever follows
    ("gAAAAABxxxxxxxxxxxxx", True),             # a str spelling
    (bytearray(b"gAAAAABxxxxxxxxxxxxx"), True),
    (b"\x30\x82\x01\x0a\x06\x09\x2a\x86\x48\x86\xf7\x0d", False),   # CMS-shaped DER
    (base64.urlsafe_b64encode(b"\x81" + b"\x00" * 20), False),      # version byte 0x81
    (b"////////////////", False),               # not base64url
    (None, False), (7, False),
], ids=["foreign", "empty", "short", "ours", "str", "bytearray", "der", "v81",
        "not_urlsafe", "none", "int"])
def test_the_sniff_reads_the_fernet_version_byte(content, ours):
    """`is_one_of_ours` decodes the first 12 characters and tests the first
    decoded byte for Fernet's version `0x80`; nothing else, and never the
    whole string."""
    assert ReversibilityService.is_one_of_ours(content) is ours


def test_a_real_token_is_ours_and_one_flipped_character_still_is(tmp_path):
    """The sniff is a shape test: a token this library wrote passes it, and
    so does one with a character flipped in its body -- which the read
    then refuses under the key, rather than replacing."""
    from isocenter.crypto import KeyManager
    manager = KeyManager(str(tmp_path / "k.key"))
    manager.load_or_generate_key()
    service = ReversibilityService(manager)
    token = service.generate_identity_token({"0010,0010": NAME_A})
    assert token.startswith(b"gAAAAAB")
    assert service.is_one_of_ours(token)
    body = bytearray(token)
    body[30] = ord("A") if body[30] != ord("A") else ord("B")
    flipped = bytes(body)
    assert service.is_one_of_ours(flipped)
    inst = Instance("SOP", "1.2.840.10008.5.1.4.1.1.2", 1)
    inst.add_sequence_item(SEQ, _foreign_item(flipped))
    with pytest.raises(RuntimeError, match="does not decrypt"):
        service.held_identity(inst)
    inst.sequences[SEQ].items[0].set_attr(CONTENT, token)
    assert service.held_identity(inst) == (token, {"0010,0010": NAME_A})


def test_a_lock_of_another_patient_creates_no_key_while_a_token_of_ours_is_unopenable(
        tmp_path):
    """Q8 as ruled, session scope: with no key file and one patient carrying
    a token this library wrote (under a key nobody here holds), a lock of
    a *different*, never-locked patient is refused before any plan and
    creates no key. Pinned so the choice is visible; the owner call on
    whether this over-refuses is in the PR."""
    key = tmp_path / "k.key"
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.enable_reversible_anonymization(str(key))
        locked = _hand_patient(session)
        _hand_patient(session, pid=PID_B, name=NAME_B)
        elsewhere = Fernet(Fernet.generate_key())
        locked.add_sequence_item(SEQ, _foreign_item(elsewhere.encrypt(b'{"0010,0010": "x"}')))
        with pytest.raises(RuntimeError) as caught:
            session.lock_identities(PID_B, tags_to_lock=TAGS)
        assert str(caught.value) == no_key_refusal(str(key))
        assert not key.exists()
        assert _token(_first(session, PID_B)) is None


def test_a_batch_mixing_both_refusals_locks_nobody(tmp_path):
    """Attack 9: one patient's token does not open under the key (a token
    this library wrote elsewhere), another's is unstamped and would lose
    a held value (#607). One `RuntimeError`, both numbered, no token
    written for anyone, under `persist=True` and a chunk size alike."""
    import sqlite3
    write_ct(tmp_path / "in" / "a.dcm", PID_A, "6173", name=NAME_A)
    write_ct(tmp_path / "in" / "b.dcm", PID_B, "6174", name=NAME_B)
    db, key = str(tmp_path / "s.db"), str(tmp_path / "k.key")
    with DicomSession(db) as session:
        session.ingest(str(tmp_path / "in"))
        session.enable_reversible_anonymization(key)
        session.lock_identities([PID_A, PID_B], tags_to_lock=TAGS)
        session.save(sync=True)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE instances SET attributes_json = "
                     "json_remove(attributes_json, '$.__locked__')")
    elsewhere = Fernet(Fernet.generate_key()).encrypt(b'{"0010,0010": "x"}')
    for persist, chunk in ((True, 0), (False, 2), (True, 2)):
        with DicomSession(db) as session:
            session.enable_reversible_anonymization(key)
            a, b = _first(session, PID_A), _first(session, PID_B)
            a.sequences[SEQ].items[0].set_attr(CONTENT, elsewhere)
            b.set_attr("0010,0010", "CHANGED^Value")
            token_b = _token(b)
            with pytest.raises(RuntimeError) as caught:
                session.lock_identities_batch([PID_A, PID_B], persist=persist,
                                              auto_persist_chunk_size=chunk,
                                              tags_to_lock=TAGS)
            lines = str(caught.value).split("\n")
            assert lines[0].startswith("lock_identities: 2 of 2 patients cannot be locked")
            assert lines[1] == f"[1 of 2] {wrong_key_refusal(key)}"
            assert lines[2] == ("[2 of 2] lock_identities: this patient's identity token "
                                "did not come from this store, so the value it holds in "
                                "0010,0010 cannot be told from what anonymize() left, and "
                                "this lock would replace it with a different one. "
                                "recover_patient_identity(<its Patient ID>, restore=True) "
                                "puts the held values back, and a lock after that is "
                                "accepted; the token this call would have written is "
                                "unchanged.")
            assert _token(a) == elsewhere and _token(b) == token_b
