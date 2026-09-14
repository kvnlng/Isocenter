"""`lock_identities(report)` locks every patient or none (#537).

The batch planned and locked one patient at a time, in the order of a set,
so a refusal part-way left an arbitrary subset locked before `anonymize()`
and nothing said which: measured on the round-2 review of #574, one of six.
It now plans every patient first, and a refusal of any raises one
`RuntimeError` naming each refused patient with its own message, before
any token is written.

**Why this file imports what it does.** `isocenter.session` is named, so
its probe row is charged.
"""
import sqlite3
from unittest.mock import MagicMock

import pydicom
import pytest
import yaml

import isocenter.session as session_module
from isocenter.session import _DEFAULT_TAGS_TO_LOCK, DicomSession

from support.ct_small_files import write_ct

PATIENTS = [f"P{i}" for i in range(6)]
REST = [tag for tag in _DEFAULT_TAGS_TO_LOCK if tag != "0010,0010"]


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")


def _session(tmp_path, blank):
    """Six patients, each one CT; those in `blank` have an empty source
    name, which the `EMPTY` rule on the name refuses to lock."""
    for i, pid in enumerate(PATIENTS):
        path = write_ct(tmp_path / "in" / f"{pid}.dcm", pid, f"80{i}", name=f"N^{i}")
        if pid in blank:
            ds = pydicom.dcmread(path)
            ds.PatientName = ""
            ds.save_as(path)
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(yaml.safe_dump({"phi_tags": {"0010,0010": {"action": "EMPTY"}}}),
                   encoding="utf-8")
    session = DicomSession(str(tmp_path / "s.db"))
    session.enable_reversible_anonymization(str(tmp_path / "k.key"))
    session.ingest(str(tmp_path / "in"))
    session.load_config(str(cfg))
    return session


def _refusal():
    return ("lock_identities: this patient holds no value in 0010,0010 under a "
            "rule of EMPTY on it, and a blank Patient's Name is not locked under a "
            "rule that blanks it. To lock this patient without the name, call "
            f"lock_identities(<its Patient ID>, tags_to_lock={REST!r}); the token this call "
            "would have written is unchanged.")


def _locked(session):
    return sorted(patient.patient_id for patient in session.store.patients
                  for study in patient.studies for series in study.series
                  for instance in series.instances if "0400,0500" in instance.sequences)


def _stored_tokens(tmp_path):
    with sqlite3.connect(str(tmp_path / "s.db")) as conn:
        (count,) = conn.execute("SELECT count(*) FROM instances "
                                "WHERE attributes_json LIKE '%0400,0500%'").fetchone()
    return count


@pytest.mark.parametrize("persist,chunk", [(False, 0), (True, 0), (False, 1)],
                         ids=["in_memory", "persist", "chunked"])
def test_one_refused_patient_leaves_every_patient_unlocked(tmp_path, persist, chunk):
    """P3's refusal leaves P0-P5 without a token, in memory and in the
    store, whether each patient is persisted as it is locked or in chunks.
    Kills the batch locking as it plans, and one that skips the refused
    patient and locks the rest."""
    with _session(tmp_path, {"P3"}) as session:
        report = session.audit()
        with pytest.raises(RuntimeError, match="1 of 6 patients cannot be locked"):
            if chunk:
                session.lock_identities_batch(report, auto_persist_chunk_size=chunk)
            else:
                session.lock_identities(report, persist=persist)
        assert _locked(session) == []
        session.save(sync=True)
    assert _stored_tokens(tmp_path) == 0
    with _session(tmp_path / "control", set()) as session:
        session.lock_identities(session.audit(), persist=True)
        assert _locked(session) == PATIENTS


def test_every_refused_patient_is_named(tmp_path):
    """Both refused patients, in Patient ID order, each with the message its
    own lock would raise, numbered by its place among the six found (P6: no
    message names a patient). Kills a raise at the first refusal."""
    with _session(tmp_path, {"P3", "P1"}) as session:
        with pytest.raises(RuntimeError) as caught:
            session.lock_identities(list(reversed(PATIENTS)))
    assert str(caught.value) == (
        "lock_identities: 2 of 6 patients cannot be locked as asked, so no patient "
        "was locked. Each is numbered by its place among the patients found, in "
        "Patient ID order. Lock the others without these, and each of these as its "
        "message says:\n[2 of 6] " + _refusal() + "\n[4 of 6] " + _refusal())


def test_the_batch_still_logs_missing_ids_when_it_refuses(tmp_path, monkeypatch):
    """An ID that matches no patient is logged as before, and before the
    refusal raises; `m` counts the patients found. Kills the missing-ID
    count moved after the raise."""
    fake = MagicMock()
    monkeypatch.setattr(session_module, "get_logger", lambda: fake)
    with _session(tmp_path, {"P3"}) as session:
        with pytest.raises(RuntimeError, match="1 of 6 patients cannot be locked"):
            session.lock_identities(PATIENTS + ["NO-SUCH-ID"])
    errors = [str(call) for call in fake.error.call_args_list]
    assert [e for e in errors if "1 Patient ID given matched no patient" in e], errors


# A value no token can hold (review of #574, round 3, F-1). The token is
# `json.dumps` of the values, and an OB element is ingested as `bytes`, so
# the lock raised `TypeError` from inside the write, after every patient
# before it in the batch was locked and persisted: measured, one of two.
PRIVATE = "0029,1110"


def _bytes_session(tmp_path):
    """PA and PB, one CT each; PB's file carries an OB private element."""
    write_ct(tmp_path / "in" / "a.dcm", "PA", "6001", name="A^A")
    path = write_ct(tmp_path / "in" / "b.dcm", "PB", "6002", name="B^B")
    ds = pydicom.dcmread(path)
    ds.private_block(0x0029, "R3PROBE", create=True).add_new(0x10, "OB", b"\x01\x02\x03\x04")
    ds.save_as(path)
    session = DicomSession(str(tmp_path / "s.db"))
    session.enable_reversible_anonymization(str(tmp_path / "k.key"))
    session.ingest(str(tmp_path / "in"))
    pb = next(p for p in session.store.patients if p.patient_id == "PB")
    assert pb.studies[0].series[0].instances[0].attributes[PRIVATE] == b"\x01\x02\x03\x04"
    return session


def _unstashable():
    return (f"lock_identities: this patient holds a value in {PRIVATE} that no "
            "token can hold (bytes), so there is nothing to stash for it. To lock "
            "this patient without it, call lock_identities(<its Patient ID>, "
            "tags_to_lock=['0010,0020']); the token this call would have written "
            "is unchanged.")


@pytest.mark.parametrize("chunk", [0, 1], ids=["per_patient", "chunked"])
def test_a_value_no_token_can_hold_leaves_every_patient_unlocked(tmp_path, chunk):
    """PB's bytes refuse the batch before PA is locked, in memory and in the
    store, with PB's own message. Kills the token built in the write half,
    and the planning loop not catching the token's failure."""
    with _bytes_session(tmp_path) as session:
        with pytest.raises(RuntimeError) as caught:
            session.lock_identities_batch(["PA", "PB"], auto_persist_chunk_size=chunk,
                                          tags_to_lock=["0010,0020", PRIVATE],
                                          persist=True)
        assert str(caught.value) == (
            "lock_identities: 1 of 2 patients cannot be locked as asked, so no "
            "patient was locked. Each is numbered by its place among the patients "
            "found, in Patient ID order. Lock the others without these, and each of "
            "these as its message says:\n[2 of 2] " + _unstashable())
        assert _stored_tokens(tmp_path) == 0
        assert _locked(session) == []
        session.save(sync=True)
    assert _stored_tokens(tmp_path) == 0


def test_a_single_lock_of_a_value_no_token_can_hold_says_the_same(tmp_path):
    """One patient gets the refusal the batch names, not `json`'s
    `TypeError`, and the lock the message gives then succeeds."""
    with _bytes_session(tmp_path) as session:
        with pytest.raises(RuntimeError) as caught:
            session.lock_identities("PB", tags_to_lock=["0010,0020", PRIVATE])
        assert str(caught.value) == _unstashable()
        assert _locked(session) == []
        session.lock_identities("PB", tags_to_lock=["0010,0020"])
        assert _locked(session) == ["PB"]
