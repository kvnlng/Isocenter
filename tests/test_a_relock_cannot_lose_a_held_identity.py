"""A re-lock after `anonymize()` cannot stash a replacement, a blank or
nothing over a held identity (#537).

`lock_identities` refuses a value that reads as a replacement. Until
0.9.8 that was only ever `ANONYMIZED` or `ANON_...`, because the pipeline
replaced Patient's Name and Patient ID with nothing else whatever the
rule said. Once the rule governs them (#537), `anonymize()` can leave a
custom `value:`, an empty name, or no name at all, and a lock taken after
it -- the reverse of the documented order, or a re-lock -- would stash
that over the good token, report success, and recovery would restore it.
The lock tells what a pass left from the record the pass wrote on each
instance (`test_anonymize_records_what_it_left.py`), not from any policy
the session holds, which a reopen, a re-audit or `load_config()` changes.

**Why this file imports what it does.** `isocenter.session` and
`isocenter.privacy` are named, so their probe rows are charged.
"""
import sqlite3

import pydicom
import pytest
import yaml

from isocenter.privacy import PhiInspector  # noqa: F401  (probe row)
from isocenter.session import _DEFAULT_TAGS_TO_LOCK, DicomSession

from support.ct_small_files import write_ct

NAME = "Orig^Name"
PID = "P537L"
TAGS = ["0010,0010", "0010,0020"]


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")


def _session(tmp_path, tags, load=True, name=NAME, **source):
    """A reversible session over one CT. With `load` the rules go through
    `load_config`; without, the caller passes `_audit(session, ...)` the
    file, which is `audit(config_path=)`: the door that never assigns
    `configuration.phi_tags` (review of #574). `name=""` writes a source
    whose Patient's Name is present and empty (`write_ct` reads `""` as
    "the default name", so the file is rewritten). `source` sets further
    elements by keyword (`PatientBirthDate="19700101"`)."""
    path = write_ct(tmp_path / "in" / "a.dcm", PID, "5371", name=name or NAME)
    if not name or source:
        ds = pydicom.dcmread(path)
        if not name:
            ds.PatientName = ""
        for keyword, value in source.items():
            setattr(ds, keyword, value)
        ds.save_as(path)
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(yaml.safe_dump({"phi_tags": tags}), encoding="utf-8")
    session = DicomSession(str(tmp_path / "s.db"))
    session.enable_reversible_anonymization(str(tmp_path / "k.key"))
    session.ingest(str(tmp_path / "in"))
    session.cfg_path = None if load else str(cfg)
    if load:
        session.load_config(str(cfg))
    return session


def _audit(session):
    return session.audit(config_path=session.cfg_path) if session.cfg_path else session.audit()


def _token(instance):
    return instance.sequences["0400,0500"].items[0].attributes["0400,0520"]


DOORS = pytest.mark.parametrize("load", [True, False], ids=["load_config", "audit_config_path"])


@DOORS
def test_a_relock_after_a_custom_name_replacement_is_refused(tmp_path, load):
    """Kills the refusal left constant-only: `Project-X` is not
    `ANONYMIZED`, so the re-lock stashed it. Through both doors: the rule
    that wrote the name is the one the last `audit()` resolved, and
    `audit(config_path=)` never assigns `configuration.phi_tags`, so a
    refusal reading only that let the re-lock stash `Project-X` (review
    of #574, M-2)."""
    with _session(tmp_path, {"0010,0010": {"action": "REPLACE", "value": "Project-X"},
                             "0010,0020": {"action": "KEEP"}}, load=load) as session:
        instance = session.store.patients[0].studies[0].series[0].instances[0]
        session.lock_identities(PID, tags_to_lock=TAGS)
        held = session.reversibility_service.recover_original_data(instance)
        assert held == {"0010,0010": NAME, "0010,0020": PID}
        token = _token(instance)

        session.anonymize(_audit(session))
        assert instance.attributes["0010,0010"] == "Project-X"
        with pytest.raises(RuntimeError, match=r"0010,0010 \('Project-X'\)"):
            session.lock_identities(PID, tags_to_lock=TAGS)
        assert _token(instance) == token
        assert session.reversibility_service.recover_original_data(instance) == held


@pytest.mark.parametrize("action,said", [("EMPTY", "an empty value"),
                                         ("REMOVE", "nothing")])
@DOORS
def test_a_relock_that_would_blank_a_held_value_is_refused(tmp_path, action, said, load):
    """EMPTY leaves `""` on the copy and the patient; REMOVE leaves no copy
    and None on the patient, so nothing is stashed for the name. Either
    way the held name would be lost. Kills the held-value check deleted."""
    with _session(tmp_path, {"0010,0010": {"action": action},
                             "0010,0020": {"action": "KEEP"}}, load=load) as session:
        instance = session.store.patients[0].studies[0].series[0].instances[0]
        session.lock_identities(PID, tags_to_lock=TAGS)
        held = session.reversibility_service.recover_original_data(instance)
        token = _token(instance)

        session.anonymize(_audit(session))
        with pytest.raises(RuntimeError) as caught:
            session.lock_identities(PID, tags_to_lock=TAGS)
        assert str(caught.value) == (
            "lock_identities: this patient already has a locked identity "
            f"holding 0010,0010, and this lock would replace it with {said}; "
            "lock identities before anonymize(), and do not re-lock a patient "
            "after it; the token this call would have written is unchanged.")
        assert _token(instance) == token
        assert session.reversibility_service.recover_original_data(instance) == held


def test_a_narrower_relock_after_anonymize_cannot_drop_a_held_name(tmp_path):
    """A re-lock that does not name the held name, after `anonymize()`
    emptied it, writes a token holding only the ID over one that held
    both (review of #574, F-7). The held token is read, not
    `tags_to_lock`. Kills the loss check looping over `tags_to_lock`."""
    with _session(tmp_path, {"0010,0010": {"action": "EMPTY"},
                             "0010,0020": {"action": "KEEP"}}) as session:
        instance = session.store.patients[0].studies[0].series[0].instances[0]
        session.lock_identities(PID, tags_to_lock=TAGS)
        held = session.reversibility_service.recover_original_data(instance)
        token = _token(instance)
        session.anonymize(session.audit())
        with pytest.raises(RuntimeError) as caught:
            session.lock_identities(PID, tags_to_lock=["0010,0020"])
        assert str(caught.value) == (
            "lock_identities: this patient already has a locked identity "
            "holding 0010,0010, and this lock would replace it with nothing "
            "(tags_to_lock does not name it); lock identities before "
            "anonymize(), and do not re-lock a patient after it; the token "
            "this call would have written is unchanged.")
        assert _token(instance) == token
        assert session.reversibility_service.recover_original_data(instance) == held


@DOORS
def test_a_narrower_relock_cannot_drop_a_held_name_now_replaced(tmp_path, load):
    """The same drop where the name now reads as its rule's replacement
    rather than blank: not named, not blank, and no original left. Kills
    the loss check judging an unnamed tag's blankness only."""
    with _session(tmp_path, {"0010,0010": {"action": "REPLACE", "value": "Project-X"},
                             "0010,0020": {"action": "KEEP"}}, load=load) as session:
        instance = session.store.patients[0].studies[0].series[0].instances[0]
        session.lock_identities(PID, tags_to_lock=TAGS)
        held = session.reversibility_service.recover_original_data(instance)
        session.anonymize(_audit(session))
        with pytest.raises(RuntimeError, match=r"holding 0010,0010, and this lock would "
                                               r"replace it with nothing \(tags_to_lock"):
            session.lock_identities(PID, tags_to_lock=["0010,0020"])
        assert session.reversibility_service.recover_original_data(instance) == held


def test_a_narrower_relock_before_anonymize_is_unchanged(tmp_path):
    """#399's rule: a re-lock of still-original values replaces the token,
    narrower or not (`test_relock_identity_token.py` pins it on a
    hand-built graph). The held name still reads as the original, so
    nothing is lost that the file does not still carry."""
    with _session(tmp_path, {"0010,0010": {"action": "EMPTY"},
                             "0010,0020": {"action": "KEEP"}}) as session:
        instance = session.store.patients[0].studies[0].series[0].instances[0]
        session.lock_identities(PID, tags_to_lock=TAGS)
        session.lock_identities(PID, tags_to_lock=["0010,0020"])
        assert session.reversibility_service.recover_original_data(instance) == {
            "0010,0020": PID}


EMPTYING = pytest.mark.parametrize("action", ["EMPTY", "REMOVE"])


def _blank_name_refusal(action, rest="['0010,0020']"):
    """The F-1 message, whose advice names `tags_to_lock` without the name.
    It fires only where no record says a pass blanked the name."""
    return ("lock_identities: this patient holds no value in 0010,0010 "
            f"under a rule of {action} on it, and a blank Patient's Name is not "
            "locked under a rule that blanks it. To lock this patient "
            f"without the name, call lock_identities(<its Patient ID>, tags_to_lock={rest}); "
            "the token this call would have written is unchanged.")


def _emptied_refusal(blanked, rest):
    """The refusal of a tag `anonymize()` emptied or removed, whose advice
    names `tags_to_lock` without those tags. No Patient ID (P6)."""
    advice = (f"To lock this patient without {'it' if len(blanked) == 1 else 'them'}, "
              f"call lock_identities(<its Patient ID>, tags_to_lock={rest!r})" if rest else
              "tags_to_lock names no other tag, so there is nothing else to lock")
    return (f"lock_identities: this patient holds no value in {', '.join(blanked)}, "
            "which anonymize() emptied or removed, so there is no original left to "
            f"stash. {advice}; the token this call would have written is unchanged.")


@EMPTYING
@DOORS
def test_a_first_lock_after_anonymize_of_an_emptied_name_is_refused(tmp_path, action, load):
    """No token yet, so there is nothing held to lose, but the lock would
    report success over a token without the identity it exists to hold:
    #399's "a lock that silently kept nothing" (review of #574, F-1). On
    ac33641 the name read `ANONYMIZED` here and the lock was refused.
    The record says the pass blanked the name, so the refusal says that
    and names no rule; no token is written. Kills the rule's refusal
    placed before the record's."""
    with _session(tmp_path, {"0010,0010": {"action": action},
                             "0010,0020": {"action": "KEEP"}}, load=load) as session:
        instance = session.store.patients[0].studies[0].series[0].instances[0]
        session.anonymize(_audit(session))
        with pytest.raises(RuntimeError) as caught:
            session.lock_identities(PID, tags_to_lock=TAGS)
        assert str(caught.value) == _emptied_refusal(["0010,0010"], ["0010,0020"])
        assert "0400,0500" not in instance.sequences


@EMPTYING
@DOORS
def test_a_first_lock_after_anonymize_and_a_reaudit_is_refused(tmp_path, action, load):
    """A re-audit records CLEARED over the patient's REMEDIATED, which is
    also what an audited source with an empty name reads. A refusal gated
    on REMEDIATED let this sequence write a token without the original
    name, where ac33641 refused it (owner ruling on review of #574). Kills
    the refusal gated on the patient's status."""
    with _session(tmp_path, {"0010,0010": {"action": action},
                             "0010,0020": {"action": "KEEP"}}, load=load) as session:
        instance = session.store.patients[0].studies[0].series[0].instances[0]
        session.anonymize(_audit(session))
        _audit(session)
        assert session.store.patients[0].phi_status.name == "CLEARED"
        with pytest.raises(RuntimeError) as caught:
            session.lock_identities(PID, tags_to_lock=TAGS)
        assert str(caught.value) == _emptied_refusal(["0010,0010"], ["0010,0020"])
        assert "0400,0500" not in instance.sequences


@EMPTYING
@pytest.mark.parametrize("reaudit", [False, True], ids=["anonymize", "anonymize_audit"])
def test_a_first_lock_after_anonymize_and_a_reload_is_refused(tmp_path, action, reaudit):
    """The refusal reads the graph as reloaded, whatever status was saved:
    REMEDIATED after a pass, CLEARED after a pass and a re-audit."""
    with _session(tmp_path, {"0010,0010": {"action": action},
                             "0010,0020": {"action": "KEEP"}}) as session:
        session.anonymize(session.audit())
        if reaudit:
            session.audit()
        session.save(sync=True)
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.enable_reversible_anonymization(str(tmp_path / "k.key"))
        session.load_config(str(tmp_path / "cfg.yaml"))
        instance = session.store.patients[0].studies[0].series[0].instances[0]
        assert session.store.patients[0].phi_status.name == (
            "CLEARED" if reaudit else "REMEDIATED")
        with pytest.raises(RuntimeError) as caught:
            session.lock_identities(PID, tags_to_lock=TAGS)
        assert str(caught.value) == _emptied_refusal(["0010,0010"], ["0010,0020"])
        assert "0400,0500" not in instance.sequences


@EMPTYING
@DOORS
def test_a_lock_before_anonymize_of_a_source_with_an_empty_name_is_refused(
        tmp_path, action, load):
    """The one call that worked on 0.9.7 and now raises. Before
    `anonymize()` the empty name is the source's own, and no record says a
    pass blanked it. The refusal is kept as ruled on review of #574, and it
    is also what stands where a store older than the record holds a blank
    a pass wrote (`test_a_store_written_without_the_record_locks_as_before`)."""
    with _session(tmp_path, {"0010,0010": {"action": action},
                             "0010,0020": {"action": "KEEP"}},
                  load=load, name="") as session:
        instance = session.store.patients[0].studies[0].series[0].instances[0]
        _audit(session)
        with pytest.raises(RuntimeError) as caught:
            session.lock_identities(PID, tags_to_lock=TAGS)
        assert str(caught.value) == _blank_name_refusal(action)
        assert "0400,0500" not in instance.sequences


@EMPTYING
@DOORS
def test_the_lock_the_refusal_names_writes_an_id_only_token(tmp_path, action, load):
    """The workaround the message gives: the same patient, locked without
    the name, writes a token holding the ID, and `anonymize()` then runs."""
    with _session(tmp_path, {"0010,0010": {"action": action},
                             "0010,0020": {"action": "KEEP"}},
                  load=load, name="") as session:
        instance = session.store.patients[0].studies[0].series[0].instances[0]
        report = _audit(session)
        session.lock_identities(PID, tags_to_lock=["0010,0020"])
        assert session.reversibility_service.recover_original_data(instance) == {
            "0010,0020": PID}
        session.anonymize(report)
        assert session.reversibility_service.recover_original_data(instance) == {
            "0010,0020": PID}


def test_the_refusal_names_the_lock_without_the_name_from_the_tags_asked_for(tmp_path):
    """The advice is `tags_to_lock` less the name, so the default lock is
    told to keep birth date and sex, and a lock of the name alone is told
    there is nothing else. Kills the advice hard-coded to the ID."""
    with _session(tmp_path, {"0010,0010": {"action": "EMPTY"},
                             "0010,0020": {"action": "KEEP"}}, name="") as session:
        with pytest.raises(RuntimeError) as caught:
            session.lock_identities(PID)
        rest = [tag for tag in _DEFAULT_TAGS_TO_LOCK if tag != "0010,0010"]
        assert str(caught.value) == _blank_name_refusal("EMPTY", rest=repr(rest))
        with pytest.raises(RuntimeError) as caught:
            session.lock_identities(PID, tags_to_lock=["0010,0010"])
        assert str(caught.value).endswith(
            "a rule that blanks it. tags_to_lock names no other tag, so there "
            "is nothing else to lock; the token this call would have written "
            "is unchanged.")


def test_a_tag_the_held_token_does_not_carry_never_refuses(tmp_path):
    """The loss check reads what the existing token holds: a first lock of
    the ID alone holds no name, so re-locking the ID after the name was
    emptied loses nothing and is not refused."""
    with _session(tmp_path, {"0010,0010": {"action": "EMPTY"},
                             "0010,0020": {"action": "KEEP"}}) as session:
        instance = session.store.patients[0].studies[0].series[0].instances[0]
        session.lock_identities(PID, tags_to_lock=["0010,0020"])
        session.anonymize(session.audit())
        session.lock_identities(PID, tags_to_lock=["0010,0020"])
        assert session.reversibility_service.recover_original_data(instance) == {
            "0010,0020": PID}


# --- the record, not the policy (review of #574, M-3) ----------------------
#
# Every predicate that asked "which rule wrote this value?" read session
# state -- the audited policy, `configuration.phi_tags`, a status -- and
# none of that survives a reopen, a re-audit or a config change. The lock
# now asks the instance what a remediation left there.

BIRTH = "0010,0030"
ACCESSION = "0008,0050"
KEEP_BOTH = {"0010,0010": {"action": "KEEP"}, "0010,0020": {"action": "KEEP"}}
PROJECT_X = {"0010,0010": {"action": "REPLACE", "value": "Project-X"},
             "0010,0020": {"action": "KEEP"}}
SOURCE = {"PatientBirthDate": "19700101", "AccessionNumber": "ACC123"}


def _instance(session):
    return session.store.patients[0].studies[0].series[0].instances[0]


def _reopened(tmp_path, config=False):
    """The store `_session` wrote, opened again: the same key, and the rules
    only when `config`."""
    session = DicomSession(str(tmp_path / "s.db"))
    session.enable_reversible_anonymization(str(tmp_path / "k.key"))
    if config:
        session.load_config(str(tmp_path / "cfg.yaml"))
    return session


def test_a_relock_after_the_policy_changed_is_refused(tmp_path):
    """S1: the pass ran under `value: Project-X`, and a re-audit under a
    `KEEP` rule replaced the policy the session holds. The record still
    says the pass wrote `Project-X`. Kills the record's read removed, with
    only the policies left to judge."""
    keep = tmp_path / "keep.yaml"
    keep.write_text(yaml.safe_dump({"phi_tags": KEEP_BOTH}), encoding="utf-8")
    with _session(tmp_path, PROJECT_X, load=False) as session:
        instance = _instance(session)
        session.lock_identities(PID, tags_to_lock=TAGS)
        held = session.reversibility_service.recover_original_data(instance)
        token = _token(instance)
        session.anonymize(_audit(session))
        session.audit(config_path=str(keep))
        with pytest.raises(RuntimeError, match=r"0010,0010 \('Project-X'\)"):
            session.lock_identities(PID, tags_to_lock=TAGS)
        assert _token(instance) == token
        assert session.reversibility_service.recover_original_data(instance) == held


@pytest.mark.parametrize("rules,tags,written", [
    (PROJECT_X, TAGS, "0010,0010 ('Project-X')"),
    ({BIRTH: {"action": "REPLACE", "value": "19000101"}}, [BIRTH], "0010,0030 ('19000101')"),
], ids=["name-project-x", "birth-date-replace"])
def test_a_relock_after_a_reopen_with_no_config_is_refused(tmp_path, rules, tags, written):
    """R1 and O2: after a reopen the session holds no policy at all, and
    the stored record is what refuses. The birth date runs under the floor,
    where no rule on the name or ID is at play. Kills the record not
    stored or not hydrated, end to end."""
    with _session(tmp_path, rules, **SOURCE) as session:
        session.lock_identities(PID, tags_to_lock=tags)
        held = session.reversibility_service.recover_original_data(_instance(session))
        session.anonymize(session.audit())
        session.save(sync=True)
    with _reopened(tmp_path) as session:
        instance = _instance(session)
        token = _token(instance)
        with pytest.raises(RuntimeError) as caught:
            session.lock_identities(session.store.patients[0].patient_id, tags_to_lock=tags)
        assert f"already carries a replacement in {written}" in str(caught.value)
        assert _token(instance) == token
        assert session.reversibility_service.recover_original_data(instance) == held


@EMPTYING
def test_a_first_lock_after_a_reopen_with_no_config_is_refused(tmp_path, action):
    """R3: no token, no policy, and a name the pass emptied (a copy holding
    `""`) or removed (no copy, and none on the patient). Kills the
    emptied-tag check deleted, and a blank vouch that forgets removals
    (the REMOVE case)."""
    with _session(tmp_path, {"0010,0010": {"action": action},
                             "0010,0020": {"action": "KEEP"}}) as session:
        session.anonymize(session.audit())
        session.save(sync=True)
    with _reopened(tmp_path) as session:
        instance = _instance(session)
        with pytest.raises(RuntimeError) as caught:
            session.lock_identities(PID, tags_to_lock=TAGS)
        assert str(caught.value) == _emptied_refusal(["0010,0010"], ["0010,0020"])
        assert "0400,0500" not in instance.sequences


@pytest.mark.parametrize("reopen", [False, True], ids=["in_session", "reopen"])
def test_a_relock_after_a_date_shift_is_refused(tmp_path, reopen):
    """H1: a shifted birth date is not an original. `__shifted__` vouches
    for it, in the session and after a reopen. Kills the date-shift record
    left out of what a pass wrote."""
    tags = TAGS + [BIRTH]

    def relock(session):
        instance = _instance(session)
        token = _token(instance)
        with pytest.raises(RuntimeError, match=rf"0010,0030 \('{shifted}'\)"):
            session.lock_identities(PID, tags_to_lock=tags)
        assert _token(instance) == token
        assert session.reversibility_service.recover_original_data(instance) == held

    with _session(tmp_path, {**KEEP_BOTH, BIRTH: {"action": "SHIFT"}}, **SOURCE) as session:
        session.lock_identities(PID, tags_to_lock=tags)
        held = session.reversibility_service.recover_original_data(_instance(session))
        session.anonymize(session.audit())
        shifted = _instance(session).attributes[BIRTH]
        assert shifted != "19700101"
        if not reopen:
            relock(session)
            return
        session.save(sync=True)
    with _reopened(tmp_path) as session:
        relock(session)


def test_a_relock_naming_study_date_after_the_floor_shift_is_refused(tmp_path):
    """H2: the floor shifts Study Date on the study and writes the shifted
    value onto the instance's copy, which the date-shift record never
    covered. The copy's write is recorded. Kills the copy writer recording
    only the name and ID."""
    tags = TAGS + ["0008,0020"]
    with _session(tmp_path, KEEP_BOTH) as session:
        instance = _instance(session)
        session.lock_identities(PID, tags_to_lock=tags)
        held = session.reversibility_service.recover_original_data(instance)
        session.anonymize(session.audit())
        shifted = instance.attributes["0008,0020"]
        assert shifted != held["0008,0020"]
        with pytest.raises(RuntimeError, match=rf"0008,0020 \('{shifted}'\)"):
            session.lock_identities(PID, tags_to_lock=tags)
        assert session.reversibility_service.recover_original_data(instance) == held


@pytest.mark.parametrize("rules,blanked", [
    ({BIRTH: {"action": "EMPTY"}, ACCESSION: {"action": "KEEP"}}, BIRTH),
    ({BIRTH: {"action": "KEEP"}, ACCESSION: {"action": "REMOVE"}}, ACCESSION),
], ids=["birth-date-empty", "accession-remove"])
def test_a_first_lock_naming_a_tag_anonymize_emptied_or_removed_is_refused(
        tmp_path, rules, blanked):
    """H5 and H8, on the default lock: a birth date the pass emptied and an
    accession it removed, neither the name. The advice is the tags asked
    for, less that one. Kills the emptied-tag check held to the name, and
    advice that does not come from `tags_to_lock`."""
    rules = {**KEEP_BOTH, "0010,0040": {"action": "KEEP"}, **rules}
    with _session(tmp_path, rules, **SOURCE) as session:
        instance = _instance(session)
        session.anonymize(session.audit())
        with pytest.raises(RuntimeError) as caught:
            session.lock_identities(PID)
        rest = [tag for tag in _DEFAULT_TAGS_TO_LOCK if tag != blanked]
        assert str(caught.value) == _emptied_refusal([blanked], rest)
        assert "0400,0500" not in instance.sequences


def test_a_first_lock_of_a_custom_name_after_a_reopen_is_refused(tmp_path):
    """H6: no token and no policy, and the name reads `Project-X`, which no
    constant catches. The stored record does."""
    with _session(tmp_path, PROJECT_X) as session:
        session.anonymize(session.audit())
        session.save(sync=True)
    with _reopened(tmp_path) as session:
        instance = _instance(session)
        with pytest.raises(RuntimeError, match=r"0010,0010 \('Project-X'\)"):
            session.lock_identities(PID, tags_to_lock=TAGS)
        assert "0400,0500" not in instance.sequences


def test_a_first_lock_of_an_emptied_name_under_a_pseudonym_after_a_reopen_is_refused(tmp_path):
    """H7: the floor pseudonymized the ID and the rule emptied the name, so
    the patient is found by its pseudonym and nothing keyed on the original
    ID can speak for it. The instance's own record does. Kills a check
    that reads state keyed on the Patient ID."""
    with _session(tmp_path, {"0010,0010": {"action": "EMPTY"}}) as session:
        session.anonymize(session.audit())
        session.save(sync=True)
    with _reopened(tmp_path) as session:
        pseudonym = session.store.patients[0].patient_id
        assert pseudonym.startswith("ANON_")
        with pytest.raises(RuntimeError) as caught:
            session.lock_identities(pseudonym, tags_to_lock=["0010,0010"])
        assert str(caught.value) == _emptied_refusal(["0010,0010"], [])
        assert pseudonym not in str(caught.value)


def test_a_relock_after_recovery_restores_the_originals_succeeds(tmp_path):
    """H3c: a restore puts the originals back, which no pass wrote, so the
    record vouches for none of them and the re-lock succeeds. A second pass
    writes the replacements again, and the lock after it is refused.
    Kills a vouch that reads the tag's presence in the record only."""
    tags = TAGS + [BIRTH]
    rules = {**PROJECT_X, BIRTH: {"action": "REPLACE", "value": "19000101"}}
    with _session(tmp_path, rules, **SOURCE) as session:
        instance = _instance(session)
        session.lock_identities(PID, tags_to_lock=tags)
        held = session.reversibility_service.recover_original_data(instance)
        session.anonymize(session.audit())
        session.recover_patient_identity(PID, restore=True)
        session.lock_identities(PID, tags_to_lock=tags)
        assert session.reversibility_service.recover_original_data(instance) == held
        session.anonymize(session.audit())
        with pytest.raises(RuntimeError, match=r"0010,0010 \('Project-X'\)"):
            session.lock_identities(PID, tags_to_lock=tags)
        assert session.reversibility_service.recover_original_data(instance) == held


def test_a_source_value_equal_to_a_rule_value_locks_before_anonymize(tmp_path):
    """A source name that already reads as its rule's `value:` is the
    source's own: no pass wrote it, the scan raises nothing on it, and the
    file ships that very value, so stashing it loses nothing. It was
    refused while the lock judged a value by the rule. Kills the rule's
    replacement test put back into the lock."""
    with _session(tmp_path, PROJECT_X, name="Project-X") as session:
        instance = _instance(session)
        session.lock_identities(PID, tags_to_lock=TAGS)
        assert session.reversibility_service.recover_original_data(instance) == {
            "0010,0010": "Project-X", "0010,0020": PID}


def _two_instances(tmp_path, rules):
    """One series of two CTs; the first has lost its Patient's Name copy, so
    the lock reads the name from the patient."""
    first = write_ct(tmp_path / "in" / "a.dcm", PID, "5371", name=NAME)
    ds = pydicom.dcmread(first)
    ds.SOPInstanceUID = ds.file_meta.MediaStorageSOPInstanceUID = f"{ds.SOPInstanceUID}.2"
    ds.InstanceNumber = 2
    ds.save_as(str(tmp_path / "in" / "b.dcm"))
    session = _session(tmp_path, rules)
    series = session.store.patients[0].studies[0].series[0]
    assert len(series.instances) == 2
    del series.instances[0].attributes["0010,0010"]
    series.instances[0].mark_modified()
    return session


def _nameless_first(session):
    """Put the instance without a name copy first, and say so: the test is
    about the lock reading the patient, and must not quietly read the
    other instance instead."""
    series = session.store.patients[0].studies[0].series[0]
    series.instances.sort(key=lambda instance: "0010,0010" in instance.attributes)
    assert "0010,0010" not in series.instances[0].attributes
    assert "0010,0010" in series.instances[1].attributes
    return series.instances


@pytest.mark.parametrize("case", ["non_blank_in_session", "blank_after_reopen"])
def test_a_name_read_from_the_patient_is_judged_by_every_instance(tmp_path, case):
    """The patient's name is not on the first instance, so only the copies
    the patient's write reached can say the pass wrote it: `Project-X` in
    the session, and an emptied name after a reopen, where no policy is
    left to refuse it. Kills the record read on the first instance only."""
    rules = PROJECT_X if case == "non_blank_in_session" else {
        "0010,0010": {"action": "EMPTY"}, "0010,0020": {"action": "KEEP"}}
    with _two_instances(tmp_path, rules) as session:
        _nameless_first(session)
        session.anonymize(session.audit())
        session.save(sync=True)
        if case == "non_blank_in_session":
            first, _ = _nameless_first(session)
            assert session.store.patients[0].patient_name == "Project-X"
            with pytest.raises(RuntimeError, match=r"0010,0010 \('Project-X'\)"):
                session.lock_identities(PID, tags_to_lock=TAGS)
            assert "0400,0500" not in first.sequences
            return
    with _reopened(tmp_path) as session:
        first, _ = _nameless_first(session)
        with pytest.raises(RuntimeError) as caught:
            session.lock_identities(PID, tags_to_lock=TAGS)
        assert str(caught.value) == _emptied_refusal(["0010,0010"], ["0010,0020"])
        assert "0400,0500" not in first.sequences


def test_a_narrower_relock_before_anonymize_keeps_nothing_it_does_not_name(tmp_path):
    """F-8: before `anonymize()` a narrower re-lock replaces the token (#399),
    because every tag it drops still holds its original on the instance.
    Kills the loss check reading every unnamed tag as lost."""
    with _session(tmp_path, KEEP_BOTH, **SOURCE) as session:
        instance = _instance(session)
        session.lock_identities(PID, tags_to_lock=TAGS + [BIRTH])
        session.lock_identities(PID, tags_to_lock=["0010,0020"])
        assert session.reversibility_service.recover_original_data(instance) == {
            "0010,0020": PID}


def test_a_lock_meeting_a_replacement_and_a_blank_names_the_replacement(tmp_path):
    """Order pin: a lock that meets a written value and an emptied tag gets
    the replacement's message, which has always come first. Kills the two
    checks swapped."""
    rules = {**PROJECT_X, BIRTH: {"action": "EMPTY"}}
    with _session(tmp_path, rules, **SOURCE) as session:
        session.anonymize(session.audit())
        with pytest.raises(RuntimeError, match=r"already carries a replacement in "
                                               r"0010,0010 \('Project-X'\)"):
            session.lock_identities(PID, tags_to_lock=TAGS + [BIRTH])


@pytest.mark.parametrize("rules,config", [
    ({}, False), ({"0010,0010": {"action": "EMPTY"}, "0010,0020": {"action": "KEEP"}}, True),
    ({"0010,0010": {"action": "EMPTY"}, "0010,0020": {"action": "KEEP"}}, False),
], ids=["floor", "name-empty-rules-loaded", "name-empty-no-rules"])
def test_a_store_written_without_the_record_locks_as_before(tmp_path, rules, config):
    """A store from before the record, made here by deleting it from the
    stored JSON. Measured on stores 0.9.6 and 0.9.7 wrote: every name a
    pass left is `ANONYMIZED` on the patient and on each copy it did not
    remove, so the constants refuse, as the floor case shows. 0.9.5 wrote
    `ANONYMIZED` on the patient but `""` on each copy under an `EMPTY`
    rule, which the name-empty cases reproduce: with the rule loaded the
    blank-name refusal stands; with no rules nothing on the store says a
    pass wrote the blank, and the lock stashes `""` exactly as 0.9.5 did
    over its own store. That last case is the residual the CHANGELOG
    names, pinned so a change to it is seen. One field differs from a
    real 0.9.5 store: the patient's own name reads `""` here (this
    release honours the rule on the patient) where 0.9.5 left
    `ANONYMIZED`; each copy and the ID match. The outcome is the same
    because the lock stashes the first instance's copy and reads the
    patient only where that copy is absent (measured on a v0.9.5 store,
    review of #574 round 3, P-4)."""
    with _session(tmp_path, rules) as session:
        session.anonymize(session.audit())
        session.save(sync=True)
    with sqlite3.connect(str(tmp_path / "s.db")) as conn:
        conn.execute("UPDATE instances SET attributes_json = "
                     "json_remove(attributes_json, '$.__remediated__')")
    with _reopened(tmp_path, config=config) as session:
        instance = _instance(session)
        assert instance._remediated_values is None and instance._remediated_blank is None
        if not rules:
            assert instance.attributes["0010,0010"] == "ANONYMIZED"
            with pytest.raises(RuntimeError, match=r"0010,0010 \('ANONYMIZED'\)"):
                session.lock_identities(session.store.patients[0].patient_id,
                                        tags_to_lock=TAGS)
            return
        assert instance.attributes["0010,0010"] == ""
        if config:
            with pytest.raises(RuntimeError) as caught:
                session.lock_identities(PID, tags_to_lock=TAGS)
            assert str(caught.value) == _blank_name_refusal("EMPTY")
            assert "0400,0500" not in instance.sequences
            return
        session.lock_identities(PID, tags_to_lock=TAGS)
        assert session.reversibility_service.recover_original_data(instance) == {
            "0010,0010": "", "0010,0020": PID}


@pytest.mark.parametrize("rules,tags,refused", [
    (None, None, r"already carries a replacement in 0010,0010 \('ANONYMIZED'\)"),
    (None, [BIRTH], r"already has a locked identity holding 0010,0030, and this lock "
                    r"would replace it with an empty value"),
    (PROJECT_X, TAGS, r"identity token did not come from this store, so the value it "
                      r"holds in 0010,0010 cannot be told from what anonymize\(\) left"),
    ({**KEEP_BOTH, BIRTH: {"action": "SHIFT"}}, TAGS + [BIRTH],
     r"identity token did not come from this store, so the value it holds in "
     r"0010,0030 cannot be told from what anonymize\(\) left"),
], ids=["floor", "floor_birth_only", "project_x", "keep_shift"])
def test_a_relock_of_a_patient_reingested_from_its_own_export(tmp_path, rules, tags, refused):
    """Lock, `anonymize()`, `export()`; a new store with the same key
    ingests the export and locks again. The file carries the token and the
    pass's values but no record, which is never a written byte (review of
    #574, round 3, F-2).

    The floor's constants refuse (`floor`), and the held birth date is not
    lost to the blank the floor left (`floor_birth_only`; e475ab7 stashed
    `""` over it). Under a `value:` or a `KEEP` rule nothing on the file
    says a pass wrote what it holds, and until #607 the re-lock stashed it
    over the held original (`project_x`, `keep_shift`): the residual the
    CHANGELOG then named, pinned so a fix would be seen. The fix is not
    "refuse a held value that differs from the current one", which #399's
    `test_a_re_lock_is_what_recovery_answers_with` forbids; it is that
    the token arrived in a file and nothing on this store vouches for it
    (`tests/test_a_token_this_store_did_not_write_is_not_replaced.py`),
    so a re-lock that would change a value it holds is refused, and the
    refusal names the first such tag. The held token is unchanged in
    every case."""
    with _session(tmp_path, rules or {}, load=rules is not None,
                  PatientBirthDate="19700101") as session:
        session.lock_identities(PID, tags_to_lock=tags)
        held = session.reversibility_service.recover_original_data(_instance(session))
        session.anonymize(session.audit())
        session.export(str(tmp_path / "out"))
    with DicomSession(str(tmp_path / "again.db")) as session:
        session.enable_reversible_anonymization(str(tmp_path / "k.key"))
        session.ingest(str(tmp_path / "out"))
        instance = _instance(session)
        assert instance._remediated_values is None and instance._remediated_blank is None
        assert instance._locked_token is None
        assert session.reversibility_service.recover_original_data(instance) == held
        pid = session.store.patients[0].patient_id
        token = _token(instance)
        with pytest.raises(RuntimeError, match=refused):
            session.lock_identities(pid, tags_to_lock=tags)
        assert _token(instance) == token
        assert session.reversibility_service.recover_original_data(instance) == held
