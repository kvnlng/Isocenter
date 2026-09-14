"""A re-lock after `anonymize()` cannot stash a replacement, a blank or
nothing over a held identity (#537).

`lock_identities` refuses a value that reads as a replacement. Until
0.9.8 that was only ever `ANONYMIZED` or `ANON_...`, because the pipeline
replaced Patient's Name and Patient ID with nothing else whatever the
rule said. Once the rule governs them (#537), `anonymize()` can leave a
custom `value:`, an empty name, or no name at all, and a lock taken after
it -- the reverse of the documented order, or a re-lock -- would stash
that over the good token, report success, and recovery would restore it.

**Why this file imports what it does.** `isocenter.session` and
`isocenter.privacy` are named, so their probe rows are charged.
"""
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


def _session(tmp_path, tags, load=True, name=NAME):
    """A reversible session over one CT. With `load` the rules go through
    `load_config`; without, the caller passes `_audit(session, ...)` the
    file, which is `audit(config_path=)`: the door that never assigns
    `configuration.phi_tags` (review of #574). `name=""` writes a source
    whose Patient's Name is present and empty (`write_ct` reads `""` as
    "the default name", so the file is rewritten)."""
    path = write_ct(tmp_path / "in" / "a.dcm", PID, "5371", name=name or NAME)
    if not name:
        ds = pydicom.dcmread(path)
        ds.PatientName = ""
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
    return instance.sequences["0400,0500"].items[0].attributes["0400,0510"]


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
            f"lock_identities: patient {PID!r} already has a locked identity "
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
            f"lock_identities: patient {PID!r} already has a locked identity "
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
    """The F-1 message, whose advice names `tags_to_lock` without the name."""
    return (f"lock_identities: patient {PID!r} holds no value in 0010,0010 "
            f"under a rule of {action} on it, which cannot be told apart from "
            "an original that anonymize() removed, and a token without that "
            "original would lose a recoverable identity. To lock this patient "
            f"without the name, call lock_identities({PID!r}, tags_to_lock={rest}); "
            "the token this call would have written is unchanged.")


@EMPTYING
@DOORS
def test_a_first_lock_after_anonymize_of_an_emptied_name_is_refused(tmp_path, action, load):
    """No token yet, so there is nothing held to lose, but the lock would
    report success over a token without the identity it exists to hold:
    #399's "a lock that silently kept nothing" (review of #574, F-1). On
    ac33641 the name read `ANONYMIZED` here and the lock was refused.
    A blank name under an EMPTY or REMOVE rule is refused, and no token
    is written."""
    with _session(tmp_path, {"0010,0010": {"action": action},
                             "0010,0020": {"action": "KEEP"}}, load=load) as session:
        instance = session.store.patients[0].studies[0].series[0].instances[0]
        session.anonymize(_audit(session))
        with pytest.raises(RuntimeError) as caught:
            session.lock_identities(PID, tags_to_lock=TAGS)
        assert str(caught.value) == _blank_name_refusal(action)
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
        assert str(caught.value) == _blank_name_refusal(action)
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
        assert str(caught.value) == _blank_name_refusal(action)
        assert "0400,0500" not in instance.sequences


@EMPTYING
@DOORS
def test_a_lock_before_anonymize_of_a_source_with_an_empty_name_is_refused(
        tmp_path, action, load):
    """The one call that worked on 0.9.7 and now raises. Before
    `anonymize()` the empty name is the source's own, but nothing stored
    tells it from a name the pass emptied or removed (the re-audit test
    above), so it is refused the same way rather than risk a token that
    loses a recoverable name (owner ruling on review of #574)."""
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
            "a recoverable identity. tags_to_lock names no other tag, so there "
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
