"""`recover_patient_identity()` returns the identity it recovered (#586).

It returned `None` under `restore=False` and `restore=True` alike: the
method had no `return` statement (measured on 63a64158). Since #539/#550
it prints nothing either, so the auditing call -- *who is `ANON_...`* --
had no answer short of `restore=True`, which writes the identity back
into the graph.

The owner ruled the shape (#586, Q1 (c)): a `dict` mapping the SOP
Instance UID of each instance carrying an identity token of ours to a
**copy** of the values that token holds, in study, series and instance
order, the same from both modes. A single dict would not do: since #583
a patient carries one token per distinct value-set, and a patient locked
with Accession Number over two studies carries two; the first token's
values would name study 1's accession for study 2.

What each test pins:

- The mapping itself, in graph order. Study 1 holds A, B, C with
  Accession Numbers `ACC-1`, `ACC-X`, `ACC-1`, so A and C share one token
  that **reappears after a different one**: grouped by token the order is
  A, C, B; graph order is A, B, C. Adjacent sharers would give the same
  order both ways.
- The two modes agree, and `restore=True` still restores.
- The values are copies: A and C share one token, and one dict object
  shared between them would carry an edit from one to the other.
- An instance carrying no token is absent.
- A pre-0.9.8 shared token is returned whole on every holder, while the
  restore writes only its group 0010 outside the first study: the return
  reports what the tokens hold, not what the restore wrote.

Measured while writing this file: `Instance.set_attr("0008,0018", ...)`
does not move `Instance.sop_instance_uid` (a dataclass field that only
`regenerate_uid()` assigns), so a restore of a locked SOP Instance UID
cannot change the keys whatever the order; no test is needed for it.

**Why this file imports what it does.** `isocenter.session` is named, so
its probe row is charged.
"""
from datetime import date

import numpy as np
import pytest

from isocenter.entities import Instance, Patient, Series, Study
from isocenter.session import DicomSession

PID, NAME = "PAT-586", "Secret^Eightysix"
PSEUDONYM = "ANON_586"
SEQ = "0400,0500"
ACC = "0008,0050"
TAGS = ["0010,0010", "0010,0020", ACC]
A, B, C, D = "SOP_586_A", "SOP_586_B", "SOP_586_C", "SOP_586_D"


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _session(tmp_path):
    session = DicomSession(str(tmp_path / "s.db"))
    session.enable_reversible_anonymization(str(tmp_path / "k.key"))
    return session


def _instance(uid, accession):
    inst = Instance(uid, "1.2.840.10008.5.1.4.1.1.2", 1)
    inst.file_path = None
    inst.set_attr("0010,0010", NAME)
    inst.set_attr("0010,0020", PID)
    inst.set_attr(ACC, accession)
    inst.set_pixel_data(np.zeros((8, 8), dtype=np.uint16))
    return inst


def _patient(session, studies):
    """`studies`: one list of `(uid, accession)` per study, one series
    each. Returns the patient and its instances in graph order."""
    patient = Patient(PID, NAME)
    instances = []
    for n, specs in enumerate(studies, start=1):
        study = Study(f"ST_586_{n}", date(2023, 1, n))
        series = Series(f"SE_586_{n}", "CT", n)
        for uid, accession in specs:
            inst = _instance(uid, accession)
            series.instances.append(inst)
            instances.append(inst)
        study.series.append(series)
        patient.studies.append(study)
    session.store.patients.append(patient)
    return patient, instances


def _anonymize_by_hand(patient, instances):
    """What a pass leaves on the identifiers, without running one."""
    for inst in instances:
        inst.set_attr("0010,0010", "ANONYMIZED")
        inst.set_attr("0010,0020", PSEUDONYM)
        inst.set_attr(ACC, "X")
    patient.patient_name, patient.patient_id = "ANONYMIZED", PSEUDONYM


def _locked(session):
    """A (ACC-1), B (ACC-X), C (ACC-1) in study 1, D (ACC-2) in study 2:
    three tokens, A and C sharing one. Locked, then anonymized by hand."""
    patient, instances = _patient(
        session, [[(A, "ACC-1"), (B, "ACC-X"), (C, "ACC-1")], [(D, "ACC-2")]])
    session.lock_identities(PID, tags_to_lock=TAGS)
    rs = session.reversibility_service
    tokens = [rs.token_of_ours(inst) for inst in instances]
    assert tokens[0] == tokens[2] and len(set(tokens)) == 3
    _anonymize_by_hand(patient, instances)
    return patient, instances


def _held(accession):
    return {"0010,0010": NAME, "0010,0020": PID, ACC: accession}


def test_restore_false_returns_each_instances_token(tmp_path):
    """Each instance's own token's values, keyed by its SOP Instance UID,
    in graph order, and nothing written. Kills M586-1 (`None` returned),
    M586-2 (every key given the first token's values: B and D would read
    `ACC-1`) and M586-3 (keys in the order tokens were first found, which
    puts C before B)."""
    with _session(tmp_path) as session:
        patient, instances = _locked(session)
        result = session.recover_patient_identity(PSEUDONYM, restore=False)
        assert result == {A: _held("ACC-1"), B: _held("ACC-X"),
                          C: _held("ACC-1"), D: _held("ACC-2")}
        assert list(result) == [A, B, C, D]
        assert patient.patient_id == PSEUDONYM
        assert instances[0].attributes["0010,0020"] == PSEUDONYM
        # The patient-level answer, as the docstring gives it.
        assert next(iter(result.values()))["0010,0020"] == PID


def test_restore_true_returns_the_same_mapping(tmp_path):
    """`restore=True` returns what `restore=False` returns, and restores.
    Kills M586-4 (the return only on the `restore=False` path, or before
    the restore)."""
    with _session(tmp_path) as session:
        patient, instances = _locked(session)
        read = session.recover_patient_identity(PSEUDONYM, restore=False)
        restored = session.recover_patient_identity(PSEUDONYM, restore=True)
        assert restored == read
        assert list(restored) == [A, B, C, D]
        assert patient.patient_id == PID
        assert [inst.attributes[ACC] for inst in instances] == [
            "ACC-1", "ACC-X", "ACC-1", "ACC-2"]


def test_the_result_is_a_copy(tmp_path):
    """A and C carry one token. An edit to A's entry reaches neither C's
    entry nor the next call. Kills M586-6 (the opened record returned
    uncopied: one dict object behind A and C, and behind every later call
    in this one)."""
    with _session(tmp_path) as session:
        _locked(session)
        result = session.recover_patient_identity(PSEUDONYM, restore=False)
        result[A]["0010,0020"] = "X"
        assert result[C]["0010,0020"] == PID
        again = session.recover_patient_identity(PSEUDONYM, restore=False)
        assert again[A]["0010,0020"] == PID


def test_an_instance_without_a_token_is_absent(tmp_path):
    """D's token is removed. D is absent from the result under both modes,
    while the restore still gives it group 0010 of the first token (#583,
    unchanged). Kills M586-7 (tokenless instances included, with the
    group-0010 fallback or anything else)."""
    with _session(tmp_path) as session:
        patient, instances = _locked(session)
        instances[3].sequences.pop(SEQ)
        read = session.recover_patient_identity(PSEUDONYM, restore=False)
        assert list(read) == [A, B, C]
        restored = session.recover_patient_identity(PSEUDONYM, restore=True)
        assert restored == read
        assert patient.patient_id == PID
        assert instances[3].attributes["0010,0020"] == PID
        assert instances[3].attributes[ACC] == "X"


def test_a_pre_0_9_8_shared_token_is_returned_whole(tmp_path):
    """One unstamped token, holding study 1's `ACC-1`, shared across two
    studies: the 0.9.7 shape. The restore writes it in full on study 1 and
    only its group 0010 on study 2, which keeps the pass's `X`. The return
    gives the full record for both holders. Kills M586-5 (the mapping read
    from the instances' attributes after the restore: study 2 would read
    `X`) and M586-8 (the return mirroring restore policy through
    `patient_level`: study 2 would lack `0008,0050`). Both differ from the
    fix only on this layout.

    M586-5 has one more form, the mapping built from the opened tokens
    after the restore loop rather than before it. That form is
    **equivalent**: the opened tokens do not change, and no restore can
    move a key (see the module docstring). No test kills it and none
    claims to."""
    with _session(tmp_path) as session:
        patient, instances = _patient(session, [[(A, "ACC-1")], [(D, "ACC-2")]])
        rs = session.reversibility_service
        session._key_for_locking()
        token = rs.generate_identity_token(_held("ACC-1"))
        for inst in instances:
            rs.embed_identity_token(inst, token)
            inst._locked_token = None
        _anonymize_by_hand(patient, instances)

        result = session.recover_patient_identity(PSEUDONYM, restore=True)
        assert result == {A: _held("ACC-1"), D: _held("ACC-1")}
        assert [inst.attributes[ACC] for inst in instances] == ["ACC-1", "X"]
        assert instances[1].attributes["0010,0020"] == PID
