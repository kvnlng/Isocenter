"""A store written before 1.0 keeps its statuses, with no policy (#555).

A 0.9.x store recorded a PHI status per patient, study and instance and
never which configuration the scan ran under, so that fact cannot be
known. Opening such a store adds `phi_policy` and `phi_policy_base` as
NULL and **back-fills nothing**: an `UPDATE ... SET phi_policy = <the
policy in force>` would fabricate exactly the fact #555 is about. So:

- a status is restored as recorded, and `phi_status_policy` is None;
- an `export()` of such instances writes the `WARNING` row, counting them as
  having no recorded policy (None never matches);
- `audit()` under the policy in force re-records every status with its
  policy, even when the status itself does not change, and `save()` writes
  it, after which the row stops;
- each load logs one line counting them, and writes no audit row (owner's
  ruling Q3): the export row already grades the run that writes them.

**How the fixture is built.** A store written by this release, then aged
to exactly what a 0.9.8 store lacks: the two columns dropped on all three
tables, and every item `__phi__` key removed from `attributes_json`.

**Why this file imports what it does.** The migration and hydration live
in `isocenter.persistence`, driven through `isocenter.session`, and the
policy on `isocenter.entities`; see `test_mutation_probe_targets.py`.
"""
import json
import logging
import shutil
import sqlite3

import pytest
from pydicom.data import get_testdata_file

from isocenter.entities import PhiStatus
from isocenter.session import DicomSession

NOTICE = "recorded under a policy other than the one in force"
LEGACY = "with no recorded policy"
TABLES = ("patients", "studies", "instances")


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _strip_phi(node):
    if isinstance(node, dict):
        node.pop("__phi__", None)
        for value in node.values():
            _strip_phi(value)
    elif isinstance(node, list):
        for value in node:
            _strip_phi(value)


def _age(db):
    """Make a store written by this release look like a 0.9.8 store."""
    with sqlite3.connect(db) as conn:
        for table in TABLES:
            conn.execute(f"ALTER TABLE {table} DROP COLUMN phi_policy")
            conn.execute(f"ALTER TABLE {table} DROP COLUMN phi_policy_base")
        for uid, text in conn.execute(
                "SELECT sop_instance_uid, attributes_json FROM instances"
        ).fetchall():
            data = json.loads(text)
            _strip_phi(data)
            conn.execute("UPDATE instances SET attributes_json = ? "
                         "WHERE sop_instance_uid = ?", (json.dumps(data), uid))


def _columns(db, table):
    with sqlite3.connect(db) as conn:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _policies(db):
    with sqlite3.connect(db) as conn:
        return {table: conn.execute(
            f"SELECT phi_status, phi_policy, phi_policy_base FROM {table}"
        ).fetchall() for table in TABLES}


def _input(tmp_path):
    folder = tmp_path / "in"
    folder.mkdir(exist_ok=True)
    shutil.copy(get_testdata_file("CT_small.dcm"), folder / "a.dcm")
    return str(folder)


def _instance(session):
    [patient] = session.store.patients
    return patient.studies[0].series[0].instances[0]


def _notices(session):
    return [d for _, _, d in session.store_backend.get_audit_errors()
            if NOTICE in d]


def _legacy_store(tmp_path):
    db = str(tmp_path / "old.db")
    with DicomSession(db) as session:
        session.ingest(_input(tmp_path))
        session.anonymize(session.audit())
        assert _instance(session).phi_status is PhiStatus.REMEDIATED
        session.save(sync=True)
    _age(db)
    assert "phi_policy" not in _columns(db, "instances")
    return db


def test_a_0_9_x_status_is_restored_without_a_policy(tmp_path):
    """Kills: a back-fill sweep; reading a legacy row as UNSCANNED."""
    db = _legacy_store(tmp_path)
    with DicomSession(db) as session:
        inst = _instance(session)
        assert inst.phi_status is PhiStatus.REMEDIATED
        assert inst.phi_status_policy is None
        [patient] = session.store.patients
        assert patient.phi_status is not PhiStatus.UNSCANNED
        assert patient.phi_status_policy is None
        for table in TABLES:
            assert {"phi_policy", "phi_policy_base"} <= _columns(db, table)
        rows = _policies(db)
    assert {r[1:] for table in TABLES for r in rows[table]} == {(None, None)}


def test_its_export_names_it(tmp_path):
    """Kills: `None` treated as matching the policy in force."""
    db = _legacy_store(tmp_path)
    with DicomSession(db) as session:
        session.export(str(tmp_path / "out"))
        [notice] = _notices(session)
        assert " writes 1 instance(s) whose" in notice
        assert f"1 {LEGACY}" in notice, notice


def test_an_audit_under_the_policy_in_force_retires_it(tmp_path):
    """A legacy row the re-audit finds exactly as recorded: only the policy
    changes. Kills: a short-circuit that treats None as matching any
    policy, so the re-stamp is never recorded or saved."""
    db = str(tmp_path / "clean.db")
    with DicomSession(db) as session:
        session.ingest(_input(tmp_path))
        session.anonymize(session.audit())
        session.audit()
        before = {id(e): e.phi_status for e in _all(session)}
        assert set(before.values()) == {PhiStatus.CLEARED}, (
            "setup: the anonymized CT re-audits clean")
        session.save(sync=True)
    _age(db)
    with DicomSession(db) as session:
        assert {e.phi_status for e in _all(session)} == {PhiStatus.CLEARED}
        assert {e.phi_status_policy for e in _all(session)} == {None}
        session.audit()
        policy = session.configuration._scan_policy()
        assert {e.phi_status for e in _all(session)} == {PhiStatus.CLEARED}
        assert [e for e in _all(session) if not e.has_unsaved_changes] == []
        session.save(sync=True)
    rows = _policies(db)
    assert {r for table in TABLES for r in rows[table]} == {
        ("cleared", policy.fingerprint, policy.base)}
    with DicomSession(db) as session:
        session.export(str(tmp_path / "out"))
        assert _notices(session) == []


def _all(session):
    for patient in session.store.patients:
        yield patient
        for study in patient.studies:
            yield study
            for series in study.series:
                yield from series.instances


def test_the_load_says_it_once(tmp_path, caplog):
    """Q3: a log line per load, no audit row. Kills: one record per entity;
    an audit row."""
    db = _legacy_store(tmp_path)
    with caplog.at_level(logging.WARNING, logger="isocenter"):
        with DicomSession(db) as session:
            said = [r for r in caplog.records if "no recorded policy" in r.getMessage()]
            assert len(said) == 1, [r.getMessage() for r in said]
            assert said[0].levelno == logging.WARNING
            # One patient, one study, one instance: three statuses.
            assert "3 PHI statuses" in said[0].getMessage()
            assert [d for _, _, d in session.store_backend.get_audit_errors()
                    if "no recorded policy" in d] == []
