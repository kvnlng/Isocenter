"""`export(check_reversibility=...)` must actually check something (#76).

The parameter was accepted, documented and inert: it defaulted to True,
so every caller believed a safety check was running, and someone passing
it explicitly was asking for a behaviour and being told nothing.

What it checks: whether the files about to be written still carry the
encrypted originals that `lock_identities()` embeds. Those are recoverable
by anyone holding `isocenter.key`, which is a property a cohort's
recipient has no way to see and every reason to be told about.
"""
import logging
import sqlite3
from datetime import date

import numpy as np
import pytest

from isocenter.entities import Instance, Patient, Series, Study
from isocenter.session import DicomSession


def _session_with_locked_identity(tmp_path, lock=True):
    session = DicomSession(str(tmp_path / "rev.db"))
    session.enable_reversible_anonymization(str(tmp_path / "test.key"))

    pid = "REV_123"
    p = Patient(pid, "Original Name")
    st = Study("ST_1", date(2023, 1, 1))
    st.study_time = "120000"
    se = Series("SE_1", "CT", 1)
    inst = Instance("SOP_1", "1.2.840.10008.5.1.4.1.1.2", 1)
    inst.file_path = None
    inst.set_attr("0010,0010", "Original Name")
    inst.set_attr("0010,0020", pid)
    for tag, val in (("0018,0050", "1.0"), ("0018,0060", "120"),
                     ("0020,0032", ["0", "0", "0"]),
                     ("0020,0037", ["1", "0", "0", "0", "1", "0"]),
                     ("0028,0030", ["0.5", "0.5"])):
        inst.set_attr(tag, val)
    inst.set_pixel_data(np.zeros((10, 10), dtype=np.uint16))

    se.instances.append(inst)
    st.series.append(se)
    p.studies.append(st)
    session.store.patients.append(p)
    session.save()

    if lock:
        session.lock_identities(pid)
        session.save()
        assert "0400,0500" in inst.sequences, "fixture did not embed a token"

    return session


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records
            if r.levelno >= logging.WARNING]


def test_export_warns_when_the_written_files_carry_recoverable_identities(
        tmp_path, caplog):
    session = _session_with_locked_identity(tmp_path)
    try:
        with caplog.at_level(logging.WARNING):
            session.export(str(tmp_path / "out"), show_progress=False)
    finally:
        session.close()

    msgs = _warnings(caplog)
    assert any("recover" in m.lower() for m in msgs), msgs


def test_the_warning_names_how_many_instances_are_affected(tmp_path, caplog):
    session = _session_with_locked_identity(tmp_path)
    try:
        with caplog.at_level(logging.WARNING):
            session.export(str(tmp_path / "out"), show_progress=False)
    finally:
        session.close()

    assert any("1 of 1" in m for m in _warnings(caplog)), _warnings(caplog)


def test_the_disclosure_reaches_the_audit_log(tmp_path):
    """A warning goes to a log the recipient of the cohort never sees."""
    session = _session_with_locked_identity(tmp_path)
    try:
        session.export(str(tmp_path / "out"), show_progress=False)
        db_path = session.store_backend.db_path
    finally:
        session.close()

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT details FROM audit_log WHERE action_type='REVERSIBLE_EXPORT'"
        ).fetchall()

    assert len(rows) == 1, rows


def test_passing_false_accepts_the_risk_without_the_warning(tmp_path, caplog):
    """The flag is the caller's acknowledgement, so it must silence it."""
    session = _session_with_locked_identity(tmp_path)
    try:
        with caplog.at_level(logging.WARNING):
            session.export(str(tmp_path / "out"), show_progress=False,
                           check_reversibility=False)
    finally:
        session.close()

    assert not any("recover" in m.lower() for m in _warnings(caplog))


def test_an_export_with_no_embedded_identities_says_nothing(tmp_path, caplog):
    """The check must stay quiet on the ordinary path, or it is noise."""
    session = _session_with_locked_identity(tmp_path, lock=False)
    try:
        with caplog.at_level(logging.WARNING):
            session.export(str(tmp_path / "out"), show_progress=False)
    finally:
        session.close()

    assert not any("recover" in m.lower() for m in _warnings(caplog))
