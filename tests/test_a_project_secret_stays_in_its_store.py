"""A project secret stays in the store that generated it (#716).

Pseudonyms and date offsets derive from the store's project secret
(0.9.7), not from the configuration. 0.9.7 also shipped a carry,
`write_project_secret(path)` and `load_project_secret(path)`, that wrote
the secret to a file and adopted it into another store. The owner's #716
ruling is that 1.0 offers no way to carry one to a new store, so both are
deleted before the freeze (their absence is pinned in
`test_api_coherence.py`, the house list of deleted spellings).

What these hold is what the documentation now states (Configuration ->
What to keep): a copy of the store file is the same store, secret
included, and nothing detects or refuses a copy; the same configuration
over a new store gives new pseudonyms; the refusal for a store that lost
its secret names no carry; and a store that took a secret unverified
under 0.9.7 or 0.9.8 keeps saying so, because the read side is a store
format and the load was only one way to reach it.

**Why this file imports what it does.** The secret, the refusal and the
unverified notice live in `isocenter.persistence`, the wiring in
`isocenter.session`.
"""
import os
import shutil
import sqlite3
from datetime import date

import pytest

from isocenter.entities import Instance, Patient, Series, Study
from isocenter.persistence import SqliteStore
from isocenter.session import DicomSession

from support.ct_small_files import write_ct

SC_SOP_CLASS = "1.2.840.10008.5.1.4.1.1.7"


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")


def _pseudonym_after_anonymize(db, source):
    with DicomSession(db) as session:
        session.ingest(str(source))
        session.anonymize(session.audit())
        (patient,) = session.store.patients
        return patient.patient_id


def test_a_copy_of_a_store_is_the_same_project(tmp_path):
    """A store generates its secret at its first audit; a copy of the file
    made after that holds the same secret, and gives the same patient the
    same pseudonym. Pins the fact the docs state, not an enforcement:
    nothing detects a copy. Kills a secret keyed to the file's path or to
    a per-open nonce."""
    source = tmp_path / "in"
    write_ct(source / "a.dcm", "MRN716", "716")
    original = str(tmp_path / "a.db")
    with DicomSession(original) as session:
        session.audit()
    copy = str(tmp_path / "b.db")
    # The store has ingested nothing, so there is no sidecar content to
    # carry; the SQLite side files are copied if the close left any.
    for src, dst in ((original, copy), (original + "-wal", copy + "-wal"),
                     (original + "-shm", copy + "-shm")):
        if os.path.exists(src):
            shutil.copyfile(src, dst)

    first = _pseudonym_after_anonymize(original, source)
    second = _pseudonym_after_anonymize(copy, source)
    assert first.startswith("ANON_")
    assert first == second


def test_the_same_config_over_a_new_store_gives_new_pseudonyms(tmp_path):
    """Two fresh stores, one source, one (the bare session's) policy: two
    pseudonyms. Pseudonyms only, because two offsets drawn from a 365-day
    range can collide. Kills a secret derived from the configuration, and
    a fixed one."""
    source = tmp_path / "in"
    write_ct(source / "a.dcm", "MRN716", "716")
    one = _pseudonym_after_anonymize(str(tmp_path / "one.db"), source)
    two = _pseudonym_after_anonymize(str(tmp_path / "two.db"), source)
    assert one.startswith("ANON_") and two.startswith("ANON_")
    assert one != two


def _shifted_store(db):
    """A store whose one patient was pseudonymised and date-shifted."""
    with DicomSession(db) as session:
        patient = Patient("P1", "Orig^Name")
        study = Study("1.2.826.0.1.716.1", date(2023, 1, 1))
        series = Series("1.2.826.0.1.716.1.1", "OT", 1)
        series.instances.append(Instance("1.2.826.0.1.716.1.1.0", SC_SOP_CLASS, 1))
        study.series.append(series)
        patient.studies.append(study)
        session.store.patients.append(patient)
        session.anonymize(session.audit())
        session.save(sync=True)


def test_the_lost_secret_refusal_names_no_carry(tmp_path):
    """Nothing in the library deletes the `project_secret` row; a store
    whose row was deleted by hand refuses, as before, and its advice is to
    re-ingest, since the secret cannot be restored from outside the store.
    Kills the message left prescribing a deleted method."""
    db = str(tmp_path / "lost.db")
    _shifted_store(db)
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM project_secret")
    with DicomSession(db) as session:
        with pytest.raises(RuntimeError) as refused:
            session.audit()
    message = str(refused.value)
    assert "no longer has" in message and "re-ingest" in message, message
    assert "load_project_secret" not in message, message


def _warnings(db):
    with sqlite3.connect(db) as conn:
        return [r[0] for r in conn.execute(
            "SELECT details FROM audit_log WHERE action_type = 'WARNING'")]


def test_a_store_that_took_a_secret_unverified_still_says_so(tmp_path):
    """A 0.9.7 or 0.9.8 store whose row says `loaded-unverified` still
    writes the lasting WARNING at every audit, and grades
    REVIEW_REQUIRED: the reason it warns is recorded in the store, and
    deleting the load does not remove it. The row is written by SQL, as
    that release's load left it. Kills the read side deleted along with
    the load."""
    db = str(tmp_path / "unverified.db")
    _shifted_store(db)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE project_secret SET origin = 'loaded-unverified'")
    before = len(_warnings(db))
    with DicomSession(db) as session:
        session.audit()
        session.store_backend.flush_audit_queue()
        report = tmp_path / "report.md"
        session.generate_report(str(report))
    new = _warnings(db)[before:]
    assert any("could not be verified when it was loaded" in w for w in new), new
    assert "REVIEW_REQUIRED" in report.read_text(encoding="utf-8")


def test_the_store_class_offers_no_secret_file_format():
    """The file format went with the carry. Kills a prefix constant or a
    parser left behind for a format nothing writes."""
    for name in ("_SECRET_FILE_PREFIX", "_parse_secret_file",
                 "_SECRET_ALREADY_HELD", "_store_recorded_an_unverified_secret"):
        assert not hasattr(SqliteStore, name), name
