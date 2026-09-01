"""`export(check_reversibility=...)` must actually check something (#76),
and must not describe files that were never written (#187).

The parameter was accepted, documented and inert: it defaulted to True,
so every caller believed a safety check was running, and someone passing
it explicitly was asking for a behaviour and being told nothing.

What it checks: whether the files about to be written still carry the
encrypted originals that `lock_identities()` embeds. Those are recoverable
by anyone holding `isocenter.key`, which is a property a cohort's
recipient has no way to see and every reason to be told about.

The disclosure was raised from the export *plan*, before the batch ran,
and never reconciled with what the batch delivered -- so an export that
wrote nothing still put "3 of 3 exported instances carry encrypted
original identities ... treat the export as re-identifiable by any
holder of it" into the compliance report. The tests at the bottom of
this file force the write to fail from two structurally different
origins, because the row has to be keyed on the outcome and not on any
one way of failing.
"""
import logging
import os
import sqlite3
from datetime import date

import numpy as np
import pytest

from isocenter.entities import Instance, Patient, Series, Study
from isocenter.session import DicomSession

#: Pixel Spacing, Type 1 for a CT image. An instance without it fails
#: `IODValidator` inside the worker -- a failure made of data alone, no
#: filesystem and no monkeypatch, which is the only kind that survives
#: the process boundary `session.export()` always crosses. It fails
#: *before* `save_as` is reached, so nothing is created on disk.
DROPPED_TAG = "0028,0030"

#: Overlay Rows, VR `US`. A string here encodes fine into the object
#: graph and raises inside `save_as`, at group `6000` -- which is past
#: (0400,0500), so the token is already serialized when the write gives
#: up. Before #199 that left a readable partial carrying the encrypted
#: originals under the real name (#198); the worker now writes to a
#: temp name and renames only on success, so this arm must deliver
#: nothing at all.
LATE_FAILURE_TAG = "6000,0010"


def _session_with_locked_identity(tmp_path, lock=True, instances=1,
                                  break_instances=(), truncate_instances=()):
    session = DicomSession(str(tmp_path / "rev.db"))
    session.enable_reversible_anonymization(str(tmp_path / "test.key"))

    pid = "REV_123"
    p = Patient(pid, "Original Name")
    st = Study("ST_1", date(2023, 1, 1))
    st.study_time = "120000"
    se = Series("SE_1", "CT", 1)
    for n in range(instances):
        inst = Instance(f"SOP_{n}", "1.2.840.10008.5.1.4.1.1.2", n + 1)
        inst.file_path = None
        inst.set_attr("0010,0010", "Original Name")
        inst.set_attr("0010,0020", pid)
        for tag, val in (("0018,0050", "1.0"), ("0018,0060", "120"),
                         ("0020,0032", ["0", "0", "0"]),
                         ("0020,0037", ["1", "0", "0", "0", "1", "0"]),
                         ("0028,0030", ["0.5", "0.5"])):
            if n in break_instances and tag == DROPPED_TAG:
                continue
            inst.set_attr(tag, val)
        if n in truncate_instances:
            inst.set_attr(LATE_FAILURE_TAG, "not-a-number")
        inst.set_pixel_data(np.zeros((10, 10), dtype=np.uint16))
        se.instances.append(inst)

    st.series.append(se)
    p.studies.append(st)
    session.store.patients.append(p)
    session.save()

    if lock:
        session.lock_identities(pid)
        session.save()
        assert all("0400,0500" in i.sequences for i in se.instances), (
            "fixture did not embed a token")

    return session


def _disclosures(db_path):
    with sqlite3.connect(db_path) as conn:
        return [d for (d,) in conn.execute(
            "SELECT details FROM audit_log "
            "WHERE action_type='REVERSIBLE_EXPORT'").fetchall()]


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


def test_a_failed_export_discloses_nothing_because_nothing_was_released(
        tmp_path, caplog):
    """Zero files on disk cannot be re-identifiable by their holder.

    The row was written from the export plan, so an export into a
    directory the process cannot write to still asserted in the
    compliance report that three re-identifiable files had gone out. A
    site reading that starts a disclosure process for an export that
    never happened.
    """
    if os.geteuid() == 0:
        pytest.skip("root ignores the directory mode this test relies on")

    session = _session_with_locked_identity(tmp_path, instances=3)
    out = tmp_path / "out"
    out.mkdir()
    report = tmp_path / "report.md"
    try:
        os.chmod(out, 0o500)
        with caplog.at_level(logging.WARNING):
            try:
                session.export(str(out), show_progress=False)
            finally:
                os.chmod(out, 0o700)
        session.generate_report(str(report))
        db_path = session.store_backend.db_path
    finally:
        session.close()

    assert list(out.rglob("*.dcm")) == [], "the arm did not fail the writes"
    assert _disclosures(db_path) == []
    content = report.read_text(encoding="utf-8")
    assert "REVERSIBLE_EXPORT" not in content, content
    assert not any("recover" in m.lower() for m in _warnings(caplog))


def test_a_partial_export_discloses_the_instances_that_were_written(
        tmp_path, caplog):
    """The claim is about delivered files, so it counts delivered files.

    All three instances carry a token and one of them fails validation
    inside the worker. The disclosure has to read "2 of 2" -- the two
    that exist -- rather than "3 of 3", which describes a file the
    recipient will never hold, and rather than "2 of 3", which invites
    the reader to look for a third.
    """
    session = _session_with_locked_identity(tmp_path, instances=3,
                                            break_instances=(1,))
    out = tmp_path / "out"
    try:
        with caplog.at_level(logging.WARNING):
            session.export(str(out), show_progress=False)
        db_path = session.store_backend.db_path
    finally:
        session.close()

    written = sorted(p.stem for p in out.rglob("*.dcm"))
    assert written == ["SOP_0", "SOP_2"], written

    rows = _disclosures(db_path)
    assert len(rows) == 1, rows
    assert "2 of 2 exported instances" in rows[0], rows[0]
    assert any("2 of 2" in m for m in _warnings(caplog)), _warnings(caplog)


def test_a_write_that_fails_late_delivers_nothing_to_disclose(tmp_path):
    """A failure past (0400,0500) used to release the token anyway (#198).

    `save_as` creates the file and streams elements in ascending tag
    order, so a write that gave up after group `0400` -- an `ENOSPC`
    part-way through Pixel Data, (7FE0,0010), being the ordinary way --
    left a short file that `dcmread` accepted and that carried the
    encrypted originals in full, while the disclosure keyed on the
    worker's verdict counted two of the three files on disk.

    #199 closed that at the source: the worker writes to a temporary
    name and renames only on success, so `ok=False` now does mean
    nothing reached disk under the real name, and the disclosure counts
    the two files that exist. This arm pins that the late-failure
    origin -- the one that used to leak the token -- and the disclosure
    now agree.
    """
    session = _session_with_locked_identity(tmp_path, instances=3,
                                            truncate_instances=(1,))
    out = tmp_path / "out"
    try:
        session.export(str(out), show_progress=False)
        db_path = session.store_backend.db_path
        errors = len(session.store_backend.get_audit_errors())
    finally:
        session.close()

    on_disk = sorted(p.name for p in out.rglob("*") if p.is_file())
    assert on_disk == ["SOP_0.dcm", "SOP_2.dcm"], on_disk
    assert errors == 1, "the arm did not fail a write"

    rows = _disclosures(db_path)
    assert len(rows) == 1, rows
    assert "2 of 2 exported instances" in rows[0], rows[0]
