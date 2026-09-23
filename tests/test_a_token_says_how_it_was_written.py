"""A token `lock_identities()` writes says it was captured per value-set
(#652).

Measured on 82e82386, and again on f54deaa1 (the base of this change), on
3.12 and 3.14t (`m2_reingest_token.py`): one patient, two studies whose
Accession Numbers are equal, so #583's lock writes **one** token over both.
Restored in the store that locked it, both studies take `ACC-SAME` back.
Exported and ingested into a new store under the same key, the restore
gave the second study `''` and logged the WARNING about tokens an earlier
release shared, because the store-local `__locked__` stamp that exempts
such a token never reaches a file, and nothing in the token said which
scheme wrote it.

The owner ruled (Q5, arm a): the scheme goes **inside the encrypted
plaintext**, `"__isocenter_token__": 2`. Bound to the token bytes,
authenticated by Fernet, readable by exactly who restores, and carried
through every export, re-ingest and store. What each test pins:

- **K1** the key is in the plaintext and nowhere in the clear;
- **K2, K6** no reader hands it out as a tag -- `open_token` strips it at
  the one door every reader goes through;
- **K3, K4** a marked token shared across studies is restored in full in
  any store; **K5** an unmarked one keeps #583's partial restore;
- **K7, K8** a re-lock over a marked, or an unmarked, token;
- **K9, K10** a later scheme is refused with its own sentence, and a
  plaintext of only the scheme holds no record;
- **K11** the restore still decrypts each distinct token once.

K4, K5, K7 and K11 are green before this change by construction -- the
first pins the correct half of #650's residual (iv), the others pin what
must not move -- and are here for the mutants they kill (named in each).

**Why this file imports what it does.** `isocenter.session` and
`isocenter.reversibility` are named, so both probe rows are charged.
"""
import json
import logging
import re
import sqlite3

import pydicom
import pytest

from isocenter.reversibility import ReversibilityService, _TokenHoldsNoRecord
from isocenter.session import DicomSession

from support.ct_small_files import write_ct
from test_one_token_per_value_set import (
    ACC, NAME, PID, _all, _anonymize_by_hand, _patient, _pre_098_token,
    _restore, _session, old_shared)

KEY = "__isocenter_token__"
TAG = re.compile(r"^[0-9a-f]{4},[0-9a-f]{4}$")


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def later(key_path):
    """The refusal for a token a later release wrote (K9)."""
    return (f"the key at {key_path} opens this patient's identity token, but "
            "it was written by a later release of this library, which "
            "recovering it needs")


def no_record(key_path):
    return (f"the key at {key_path} opens this patient's identity token, but "
            "it holds no identity record this library writes, so nothing can "
            "be recovered from it")


def _instances(session):
    return [i for p in session.store.patients for st in p.studies
            for se in st.series for i in se.instances]


def _accessions(session):
    return sorted(str(i.attributes.get(ACC)) for i in _instances(session))


# --- an export locked in one store, re-ingested in another (m2) --------------

def _locked_export(tmp_path, unmarked=False):
    """m2's store A: two studies of one patient, both `ACC-SAME`, locked
    with the default tags (one token over both, #583's rule), audited,
    anonymized, saved and exported. `unmarked`: the token a release before
    1.0 would have written over the same values, in place of the lock's.
    Returns (export folder, key path, pseudonym)."""
    for n, date in (("6521", "20050601"), ("6522", "20040601")):
        write_ct(tmp_path / "src" / n / "x.dcm", PID, n, study_date=date,
                 name=NAME, accession="ACC-SAME")
    key = str(tmp_path / "k.key")
    with DicomSession(str(tmp_path / "a.db")) as session:
        session.enable_reversible_anonymization(key)
        session.ingest(str(tmp_path / "src"))
        [patient] = session.store.patients
        if unmarked:
            record = session.reversibility_service.open_token(
                _one_token(session, lock=True))
            _pre_098_token(session, _instances(session), record)
        else:
            session.lock_identities(PID)
        assert len({session.reversibility_service.token_of_ours(i)
                    for i in _instances(session)}) == 1, "setup: one token"
        session.anonymize(session.audit())
        session.save(sync=True)
        pseudonym = patient.patient_id
        session.export(str(tmp_path / "exp"), use_compression=False,
                       show_progress=False)
    return tmp_path / "exp", key, pseudonym


def _one_token(session, lock=False):
    if lock:
        session.lock_identities(PID)
    [token] = {session.reversibility_service.token_of_ours(i)
               for i in _instances(session)}
    return token


def _restored_elsewhere(tmp_path, caplog, folder, key, pseudonym):
    with DicomSession(str(tmp_path / "b.db")) as session:
        session.enable_reversible_anonymization(key)
        session.ingest(str(folder))
        assert not any(i.identity_token_is_this_stores(
            session.reversibility_service.token_of_ours(i))
            for i in _instances(session)), "setup: store B stamped nothing"
        warnings = _restore(session, caplog, pseudonym)
        return warnings, _accessions(session), session


def test_a_new_token_carries_its_scheme_inside_the_encryption(tmp_path):
    """K1. Kills: the marker written outside the token (a tag beside it, or
    an unencrypted field), or not written."""
    folder, key, _ = _locked_export(tmp_path)
    with DicomSession(str(tmp_path / "check.db")) as session:
        session.enable_reversible_anonymization(key)
        session.ingest(str(folder))
        rs = session.reversibility_service
        tokens = {rs.token_of_ours(i) for i in _instances(session)}
        assert len(tokens) == 1
        plaintext = json.loads(rs.engine.decrypt(tokens.pop()))
    assert plaintext[KEY] == 2
    assert sorted(plaintext) == ["0008,0050", "0010,0010", "0010,0020",
                                 "0010,0030", "0010,0040", KEY]
    for path in folder.rglob("*.dcm"):
        ds = pydicom.dcmread(str(path))
        content = ds[0x04000500].value[0][0x04000510].value
        assert KEY not in (content if isinstance(content, str)
                           else content.decode("latin-1"))
        assert KEY.encode() not in path.read_bytes()


def test_a_re_ingested_1_0_export_restores_every_study(tmp_path, caplog):
    """K3, §1.2 exactly. Store B never stamped the token, but the token
    says it was captured per value-set, so both studies take `ACC-SAME`
    back and no #583 WARNING fires. Red on f54deaa1: `['', 'ACC-SAME']`
    and the WARNING. Kills: the restore ignoring the scheme."""
    folder, key, pseudonym = _locked_export(tmp_path)
    warnings, accessions, _ = _restored_elsewhere(tmp_path, caplog, folder,
                                                  key, pseudonym)
    assert warnings == []
    assert accessions == ["ACC-SAME", "ACC-SAME"]


def test_one_study_of_a_1_0_export_alone_is_restored_in_full(tmp_path, caplog):
    """K4. One study's folder of a 1.0 export, ingested alone: restored in
    full, with no WARNING. Green before this change (shared across no
    study, #650's residual iv); pinned because it is now correct for a
    reason, not by accident. Kills: a marked token held to the
    partial restore when alone."""
    folder, key, pseudonym = _locked_export(tmp_path)
    [study] = [d for d in folder.glob("*/*") if d.is_dir()][:1]
    warnings, accessions, _ = _restored_elsewhere(tmp_path, caplog, study,
                                                  key, pseudonym)
    assert warnings == []
    assert accessions == ["ACC-SAME"]


def test_an_unmarked_shared_token_still_gets_the_partial_restore(tmp_path, caplog):
    """K5. The same values under a token a release before 1.0 wrote (no
    scheme key): unchanged, #583's b2 restore and its WARNING. Kills: every
    token exempted (the scheme read as present when absent), and
    `_pre_098_token` left marking its token (which would turn every
    pre-0.9.8 test in `test_one_token_per_value_set.py` into a test of a
    marked token)."""
    folder, key, pseudonym = _locked_export(tmp_path, unmarked=True)
    warnings, accessions, _ = _restored_elsewhere(tmp_path, caplog, folder,
                                                  key, pseudonym)
    assert warnings == [old_shared(1, 2)]
    assert accessions == ["", "ACC-SAME"]


def test_a_restore_writes_no_scheme_attribute(tmp_path, caplog):
    """K6. The scheme key is the token's, never an attribute: a restore
    writes each value of the token onto the instance, and the key must not
    be one of them, in memory or in the store. Kills: the strip missing
    from the restore's read."""
    folder, key, pseudonym = _locked_export(tmp_path)
    with DicomSession(str(tmp_path / "b.db")) as session:
        session.enable_reversible_anonymization(key)
        session.ingest(str(folder))
        _restore(session, caplog, pseudonym)
        assert not [k for i in _instances(session) for k in i.attributes
                    if k.startswith("__isocenter")]
        session.save(sync=True)
    with sqlite3.connect(str(tmp_path / "b.db")) as conn:
        blobs = [b for (b,) in conn.execute("SELECT attributes_json FROM instances")]
    assert blobs and not any(KEY in b for b in blobs)


# --- the readers --------------------------------------------------------------

def test_no_reader_hands_the_scheme_out_as_a_tag(tmp_path):
    """K2. Every door from a decrypt to a caller returns tags only:
    `open_token`, `held_identity`, `recover_or_raise`, the tolerant
    `recover_original_data`, and what `recover_patient_identity()` returns
    (#586). Kills: the strip missing from any one of them."""
    with _session(tmp_path) as session:
        _, by_study = _patient(session, [[{"0008_0050": "ACC-ONE"}]])
        session.lock_identities(PID, tags_to_lock=["0010,0010", "0010,0020", ACC])
        [inst] = _all(by_study)
        rs = session.reversibility_service
        token = rs.token_of_ours(inst)
        assert KEY in json.loads(rs.engine.decrypt(token)), "setup: marked"
        reads = {"open_token": rs.open_token(token),
                 "held_identity": rs.held_identity(inst)[1],
                 "recover_or_raise": rs.recover_or_raise(inst),
                 "recover_original_data": rs.recover_original_data(inst)}
        returned = session.recover_patient_identity(PID)
    for name, values in reads.items():
        assert values == {"0010,0010": NAME, "0010,0020": PID,
                          ACC: "ACC-ONE"}, name
    assert [sorted(v) for v in returned.values()] == [[ACC, "0010,0010", "0010,0020"]]
    assert all(TAG.match(k) for v in returned.values() for k in v)


def test_a_relock_over_a_marked_token_is_not_refused_for_the_marker(tmp_path):
    """K7. A re-lock reads every tag the existing token holds and refuses
    to drop one `tags_to_lock` does not name (#537); the scheme key is not
    one. Green before this change (no token was marked). Kills: the strip
    missing from the lock's read, which would refuse every re-lock with
    "`__isocenter_token__` ... tags_to_lock does not name it"."""
    with _session(tmp_path) as session:
        _, by_study = _patient(session, [[{"0008_0050": "ACC-ONE"}]])
        tags = ["0010,0010", "0010,0020", ACC]
        session.lock_identities(PID, tags_to_lock=tags)
        session.lock_identities(PID, tags_to_lock=tags)
        rs = session.reversibility_service
        [inst] = _all(by_study)
        assert json.loads(rs.engine.decrypt(rs.token_of_ours(inst)))[KEY] == 2


def test_what_a_relock_over_an_unmarked_token_leaves(tmp_path):
    """K8. A re-lock over a token a release before 1.0 wrote, on raw data
    holding the same values, captures from the instances (never from the
    old plaintext) and writes a new, marked token. Measured at this
    change. Kills: a marker certifying an old shared record (the old
    plaintext re-encrypted with the key added)."""
    with _session(tmp_path) as session:
        _, by_study = _patient(session, [[{"0008_0050": "ACC-ONE"}],
                                         [{"0008_0050": "ACC-TWO"}]])
        instances = _all(by_study)
        old = _pre_098_token(session, instances,
                             {"0010,0010": NAME, "0010,0020": PID, ACC: "ACC-ONE"})
        session.lock_identities(PID, tags_to_lock=["0010,0010", "0010,0020", ACC])
        rs = session.reversibility_service
        tokens = [rs.token_of_ours(i) for i in instances]
        assert old not in tokens
        held = [json.loads(rs.engine.decrypt(t)) for t in tokens]
    assert [h[ACC] for h in held] == ["ACC-ONE", "ACC-TWO"]
    assert [h[KEY] for h in held] == [2, 2]


def _hand_token(session, instances, payload):
    """A token of ours holding exactly `payload`, embedded on `instances`."""
    rs = session.reversibility_service
    session._key_for_locking()
    token = rs.engine.encrypt(json.dumps(payload).encode("utf-8"))
    for inst in instances:
        rs.embed_identity_token(inst, token)
    return token


#: `True` is an int to Python and equals 1, so without the bool check it
#: would pass as scheme 1 (review of L12, N2); 0 is below every scheme.
@pytest.mark.parametrize("scheme", [3, "x", True, 0])
def test_a_token_of_a_later_scheme_is_refused_not_guessed(tmp_path, scheme, caplog):
    """K9. A token whose scheme this release does not know holds a record,
    so "holds no identity record" would be false: refused with its own
    sentence, on every read -- the restore (nothing written), the lock's
    plan, and the tolerant read (None). Kills: a later scheme restored
    under this release's rules; the no-record sentence reused."""
    with _session(tmp_path) as session:
        patient, by_study = _patient(session, [[{}]])
        [inst] = _all(by_study)
        token = _hand_token(session, [inst], {"0010,0010": NAME,
                                              "0010,0020": PID, KEY: scheme})
        path = session.key_manager.key_path
        with pytest.raises(_TokenHoldsNoRecord) as raised:
            session.reversibility_service.open_token(token)
        assert str(raised.value) == later(path)
        assert session.reversibility_service.recover_original_data(inst) is None
        with pytest.raises(RuntimeError) as raised:
            session.lock_identities(PID)
        assert "written by a later release of this library" in str(raised.value)
        assert "holds no identity record" not in str(raised.value)
        assert session.reversibility_service.token_of_ours(inst) == token, \
            "the lock replaced nothing"
        _anonymize_by_hand(patient, [inst])
        with pytest.raises(RuntimeError) as raised:
            session.recover_patient_identity("ANON_583", restore=True)
        assert later(path) in str(raised.value)
        assert inst.attributes["0010,0010"] == "ANONYMIZED", "nothing written"


def test_a_token_holding_only_the_scheme_holds_no_record(tmp_path):
    """K10. `{"__isocenter_token__": 2}` alone is an empty record, which no
    lock writes (`generate_identity_token` returns `b""` for one). Kills:
    the empty check run before the strip."""
    with _session(tmp_path) as session:
        _, by_study = _patient(session, [[{}]])
        [inst] = _all(by_study)
        token = _hand_token(session, [inst], {KEY: 2})
        with pytest.raises(_TokenHoldsNoRecord) as raised:
            session.reversibility_service.open_token(token)
        assert str(raised.value) == no_record(session.key_manager.key_path)
        assert session.reversibility_service.generate_identity_token({}) == b""


def test_a_restore_decrypts_each_token_once(tmp_path, caplog, monkeypatch):
    """K11. The scheme comes from the same decrypt as the values: one per
    distinct token, as #583 made it. Green before this change. Kills: a
    second decrypt added to read the scheme."""
    with _session(tmp_path) as session:
        patient, by_study = _patient(session, [[{"0008_0050": "ACC-ONE"}] * 2,
                                               [{"0008_0050": "ACC-TWO"}]])
        session.lock_identities(PID, tags_to_lock=["0010,0010", "0010,0020", ACC])
        _anonymize_by_hand(patient, _all(by_study), **{"0008_0050": "X"})
        engine = session.reversibility_service.engine
        calls = []
        real = engine.decrypt
        monkeypatch.setattr(engine, "decrypt",
                            lambda content: calls.append(1) or real(content))
        assert _restore(session, caplog) == []
    assert len(calls) == 2
    assert [i.attributes[ACC] for i in _all(by_study)] == ["ACC-ONE", "ACC-ONE", "ACC-TWO"]


def test_the_scheme_key_is_spelled_once():
    """The two names the marker is read and written through."""
    assert ReversibilityService.TOKEN_SCHEME_KEY == KEY
    assert ReversibilityService.TOKEN_SCHEME == 2
