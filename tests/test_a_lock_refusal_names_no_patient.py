"""A `lock_identities` refusal carries no Patient ID (P6, v0.9.8 bunch E2).

The refusals #574 added opened `lock_identities: patient '<id>' ...` and
advised `lock_identities('<id>', tags_to_lock=[...])`. Before `anonymize()`
that ID is the original; after it, the pseudonym, which #550 kept off the
console for the same reason. The replacement refusal also quoted the value
it found, and on Patient ID that value is the pseudonym: the validator
refuses any literal there, so a replaced `0010,0020` is always the
patient's own current Patient ID.

So a single-patient message says "this patient" and spells the advice
`lock_identities(<its Patient ID>, tags_to_lock=[...])` -- the caller holds
the ID it passed. A replaced Patient ID is described, never quoted; every
other replacement is still quoted, because `ANONYMIZED`, a rule's `value:`
and a shifted date are not IDs and are what tells the caller which pass
wrote them. The batch numbers each refused patient by its place among the
patients it found, in Patient ID order, which is the order it plans and
locks in, so `sorted(found)[n - 1]` names it. Only the text changed: every
refusal still refuses (the exact-text tests in
`test_a_relock_cannot_lose_a_held_identity.py`,
`test_a_batch_lock_locks_every_patient_or_none.py`, `test_floor_policy.py`
and `test_patient_level_remediation_reaches_instances.py` pin each site).
"""
import ast
import re

import pydicom
import pytest
import yaml

from isocenter import Session

from support.ct_small_files import write_ct

PID = "PAT-P6"


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def test_a_replaced_patient_id_is_described_not_quoted(tmp_path):
    """The floor replaces the ID with the pseudonym; a lock naming only the
    ID after it is refused without quoting it. Kills the value quoted for
    `0010,0020`."""
    write_ct(tmp_path / "in" / "a.dcm", PID, "6061")
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        session.enable_reversible_anonymization(str(tmp_path / "k.key"))
        session.anonymize(session.audit())
        pseudonym = session.store.patients[0].patient_id
        assert pseudonym.startswith("ANON_")
        with pytest.raises(RuntimeError) as caught:
            session.lock_identities(pseudonym, tags_to_lock=["0010,0020"])
    message = str(caught.value)
    assert message.startswith(
        "lock_identities: this patient already carries a replacement in 0010,0020 "
        "(a replacement Patient ID), so there is no original identity left to stash.")
    assert pseudonym not in message and PID not in message


def test_the_batch_number_names_the_refused_patient(tmp_path):
    """The recipe the message gives -- the patients found, in Patient ID
    order -- recovers each refused patient, and the lock its message
    advises then succeeds. An ID that matched no patient is not counted.
    Kills the numbering taken over the IDs given (unmatched included) and
    over the refusals alone."""
    given = ["P9", "NO-SUCH-ID", "P2", "P5"]
    for n, pid in enumerate(["P2", "P5", "P9"]):
        path = write_ct(tmp_path / "in" / f"{pid}.dcm", pid, f"607{n}", name=f"N^{n}")
        if pid in ("P5", "P9"):
            ds = pydicom.dcmread(path)
            ds.PatientName = ""
            ds.save_as(path)
    config = tmp_path / "c.yaml"
    config.write_text(yaml.safe_dump({"phi_tags": {"0010,0010": {"action": "EMPTY"}}}),
                      encoding="utf-8")
    with Session(str(tmp_path / "s.db")) as session:
        session.enable_reversible_anonymization(str(tmp_path / "k.key"))
        session.ingest(str(tmp_path / "in"))
        session.load_config(str(config))
        with pytest.raises(RuntimeError) as caught:
            session.lock_identities(given)
        message = str(caught.value)
        for pid in given:
            assert pid not in message, message
        found = sorted(p for p in set(given)
                       if p in {x.patient_id for x in session.store.patients})
        numbered = re.findall(r"^\[(\d+) of (\d+)\] lock_identities: this patient", message,
                              flags=re.MULTILINE)
        assert numbered == [("2", "3"), ("3", "3")], message
        assert "2 of 3 patients cannot be locked" in message
        refused = [found[int(n) - 1] for n, _ in numbered]
        assert refused == ["P5", "P9"]
        advice = re.search(r"lock_identities\(<its Patient ID>, tags_to_lock=(\[.*?\])\)",
                           message)
        for pid in refused:
            session.lock_identities(pid, tags_to_lock=ast.literal_eval(advice.group(1)))
        session.lock_identities("P2")


def test_a_name_that_is_the_patient_id_is_described_not_quoted(tmp_path):
    """A source pseudonymised upstream as `PatientName == PatientID ==
    'ANON_123'`: the name reads as a replacement, and quoting it would quote
    the Patient ID. So any replacement equal to the Patient ID is described,
    on any tag. Kills dropping `or str(val) == patient_id` (review of #615,
    P-5)."""
    write_ct(tmp_path / "in" / "a.dcm", "ANON_123", "6081", name="ANON_123")
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        session.enable_reversible_anonymization(str(tmp_path / "k.key"))
        with pytest.raises(RuntimeError) as caught:
            session.lock_identities("ANON_123", tags_to_lock=["0010,0010"])
        assert "0400,0500" not in session.store.patients[0].studies[0].series[0] \
            .instances[0].sequences
    message = str(caught.value)
    assert message.startswith(
        "lock_identities: this patient already carries a replacement in 0010,0010 "
        "(a replacement Patient ID), so there is no original identity left to stash."), message
    assert "ANON_123" not in message
