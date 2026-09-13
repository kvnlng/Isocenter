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
import pytest
import yaml

from isocenter.privacy import PhiInspector  # noqa: F401  (probe row)
from isocenter.session import DicomSession

from support.ct_small_files import write_ct

NAME = "Orig^Name"
PID = "P537L"
TAGS = ["0010,0010", "0010,0020"]


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")


def _session(tmp_path, tags):
    write_ct(tmp_path / "in" / "a.dcm", PID, "5371", name=NAME)
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(yaml.safe_dump({"phi_tags": tags}), encoding="utf-8")
    session = DicomSession(str(tmp_path / "s.db"))
    session.enable_reversible_anonymization(str(tmp_path / "k.key"))
    session.ingest(str(tmp_path / "in"))
    session.load_config(str(cfg))
    return session


def _token(instance):
    return instance.sequences["0400,0500"].items[0].attributes["0400,0510"]


def test_a_relock_after_a_custom_name_replacement_is_refused(tmp_path):
    """Kills the refusal left constant-only: `Project-X` is not
    `ANONYMIZED`, so the re-lock stashed it."""
    with _session(tmp_path, {"0010,0010": {"action": "REPLACE", "value": "Project-X"},
                             "0010,0020": {"action": "KEEP"}}) as session:
        instance = session.store.patients[0].studies[0].series[0].instances[0]
        session.lock_identities(PID, tags_to_lock=TAGS)
        held = session.reversibility_service.recover_original_data(instance)
        assert held == {"0010,0010": NAME, "0010,0020": PID}
        token = _token(instance)

        session.anonymize(session.audit())
        assert instance.attributes["0010,0010"] == "Project-X"
        with pytest.raises(RuntimeError, match=r"0010,0010 \('Project-X'\)"):
            session.lock_identities(PID, tags_to_lock=TAGS)
        assert _token(instance) == token
        assert session.reversibility_service.recover_original_data(instance) == held


@pytest.mark.parametrize("action,said", [("EMPTY", "an empty value"),
                                         ("REMOVE", "nothing")])
def test_a_relock_that_would_blank_a_held_value_is_refused(tmp_path, action, said):
    """EMPTY leaves `""` on the copy and the patient; REMOVE leaves no copy
    and None on the patient, so nothing is stashed for the name. Either
    way the held name would be lost. Kills the held-value check deleted."""
    with _session(tmp_path, {"0010,0010": {"action": action},
                             "0010,0020": {"action": "KEEP"}}) as session:
        instance = session.store.patients[0].studies[0].series[0].instances[0]
        session.lock_identities(PID, tags_to_lock=TAGS)
        held = session.reversibility_service.recover_original_data(instance)
        token = _token(instance)

        session.anonymize(session.audit())
        with pytest.raises(RuntimeError) as caught:
            session.lock_identities(PID, tags_to_lock=TAGS)
        assert str(caught.value) == (
            f"lock_identities: patient {PID!r} already has a locked identity "
            f"holding 0010,0010, and this lock would replace it with {said}; "
            "lock identities before anonymize(), and do not re-lock a patient "
            "after it; the token this call would have written is unchanged.")
        assert _token(instance) == token
        assert session.reversibility_service.recover_original_data(instance) == held


def test_a_tag_the_held_token_does_not_carry_never_refuses(tmp_path):
    """The check reads what the existing token holds: a first lock of the
    ID alone holds no name, so a later lock whose name is empty loses
    nothing and is not refused. Kills the check reading `tags_to_lock`
    rather than the held token."""
    with _session(tmp_path, {"0010,0010": {"action": "EMPTY"},
                             "0010,0020": {"action": "KEEP"}}) as session:
        instance = session.store.patients[0].studies[0].series[0].instances[0]
        session.lock_identities(PID, tags_to_lock=["0010,0020"])
        session.anonymize(session.audit())
        session.lock_identities(PID, tags_to_lock=TAGS)
        assert session.reversibility_service.recover_original_data(instance) == {
            "0010,0010": "", "0010,0020": PID}
