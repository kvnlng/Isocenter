"""An edit after the scan still grades after a save and a reopen (#767).

Review of #774, finding 1. Condition 8 rests on telling "scanned, then
edited" from "never scanned": the raw status is set and the revision has
moved past it. The save wrote what `_phi_status_record()` reads, which is
UNSCANNED for a stale status, and hydration recorded that at the current
revision -- so a save and a reopen turned an edit no scan had read into
"never scanned", condition 8 went quiet, and the run graded PASS with the
edited value exported. `audit()` never ran.

Owner ruling (2026-09-23): the store keeps the stale state. A row records
that its entity's status went stale, and hydration restores the entity as
edited since its scan, so it grades until `audit()` reads it.
"""
import re
import sqlite3

import pydicom
import pytest

from isocenter import Session
from isocenter.entities import PhiStatus

from support.ct_small_files import study_uid, write_ct

EDITED = "edited after the last PHI scan"
#: The column #774 adds on patients, studies and instances.
STALE_COLUMN = "phi_status_edited"
SUFFIX = "7001"


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _line(patients=0, studies=0, instances=0):
    n = patients + studies + instances
    noun = "entity" if n == 1 else "entities"
    return (f"{n} {noun} {EDITED}: its content was changed after its PHI "
            "status was recorded, and no scan has read the change; "
            f"`audit()` reads it (patients {patients}, studies {studies}, "
            f"instances {instances})")


def _graded(session, root, name):
    out = root / f"out-{name}"
    session.export(str(out), use_compression=False)
    session.generate_report(str(root / f"{name}.md"))
    text = (root / f"{name}.md").read_text(encoding="utf-8")
    basis = text.split("## 5. Validation & Verification", 1)[1].split(
        "*   **Metadata Remediation:**", 1)[0]
    reasons = ([] if "**Grade Basis:** PASS" in basis
               else re.findall(r"^    \*   (.*)$", basis, flags=re.M))
    [written] = list(out.rglob("*.dcm"))
    return reasons, ("**PASS**" in text), pydicom.dcmread(str(written))


def _edited(reasons):
    return [r for r in reasons if EDITED in r]


def _patient(s):
    return s.store.patients[0]


def _study(s):
    return _patient(s).studies[0]


def _series(s):
    return _study(s).series[0]


def _inst(s):
    return _series(s).instances[0]


#: rev-767's probe_reopen shapes: (edit, the line it grades under, what
#: the file carries for it).
SHAPES = {
    "patient_name": (lambda s: setattr(_patient(s), "patient_name", "Alpha^One"),
                     _line(patients=1),
                     lambda ds: str(ds.PatientName) == "Alpha^One"),
    "instance_set_attr": (lambda s: _inst(s).set_attr("0008,1030", "Alpha^One's study"),
                          _line(instances=1),
                          lambda ds: ds.StudyDescription == "Alpha^One's study"),
    "series_uid_set_back": (lambda s: setattr(_series(s), "series_instance_uid",
                                              study_uid(SUFFIX) + ".1"),
                            _line(instances=1),
                            lambda ds: ds.SeriesInstanceUID == study_uid(SUFFIX) + ".1"),
    "study_date": (lambda s: setattr(_study(s), "study_date", "20040119"),
                   _line(studies=1),
                   lambda ds: ds.StudyDate == "20040119"),
}


def _store(root):
    write_ct(root / "in" / "a.dcm", "PID-1", SUFFIX, name="Alpha^One")
    return str(root / "s.db")


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_an_edit_saved_and_reopened_still_grades(tmp_path, shape):
    """Edit, save, reopen: the edit grades under condition 8, the file
    carries the edited value and no `(0012,0062)`, and a re-audit and a
    pass grade PASS. Before, the reopen graded PASS beside the value."""
    edit, line, carries = SHAPES[shape]
    db = _store(tmp_path)
    with Session(db) as session:
        session.ingest(str(tmp_path / "in"))
        session.anonymize(session.audit())
        edit(session)
        session.save(sync=True)
    with Session(db) as session:
        reasons, passed, ds = _graded(session, tmp_path, "reopened")
        assert not passed
        assert _edited(reasons) == [line], reasons
        assert carries(ds)
        assert "PatientIdentityRemoved" not in ds
        session.anonymize(session.audit())
        if shape == "series_uid_set_back":
            # #544 as it stands, on main as here, reopened or not: the
            # re-audit raises the Series finding alone, and its pass leaves
            # the instance IDENTIFIED until a second round reads the copy
            # the Series wrote (a Series handed in late, Q-C5).
            session.anonymize(session.audit())
        reasons, passed, ds = _graded(session, tmp_path, "reaudited")
    assert passed and reasons == [], reasons
    assert ds.PatientIdentityRemoved == "YES"


def test_a_stale_status_saved_twice_is_still_stale(tmp_path):
    """The second save writes the entity again (another edit dirtied it):
    what it writes is still the stale state, not UNSCANNED."""
    db = _store(tmp_path)
    with Session(db) as session:
        session.ingest(str(tmp_path / "in"))
        session.anonymize(session.audit())
        _inst(session).set_attr("0008,1030", "first")
        session.save(sync=True)
        _inst(session).set_attr("0008,1030", "second")
        session.save(sync=True)
    with Session(db) as session:
        reasons, passed, _ds = _graded(session, tmp_path, "r")
    assert not passed and _edited(reasons) == [_line(instances=1)], reasons


def test_a_stale_status_survives_reopen_save_reopen(tmp_path):
    """Hydrated stale, saved again untouched and after an unrelated edit,
    reopened: still stale. A hydration that recorded the stale entity as
    current would be written back as current by the next save of it."""
    db = _store(tmp_path)
    with Session(db) as session:
        session.ingest(str(tmp_path / "in"))
        session.anonymize(session.audit())
        _patient(session).patient_name = "Alpha^One"
        session.save(sync=True)
    with Session(db) as session:
        patient = _patient(session)
        assert patient.phi_status is PhiStatus.UNSCANNED
        assert not patient.has_unsaved_changes
        session.save(sync=True)
    with Session(db) as session:
        # Dirty the stale patient by a further edit, so the save rewrites
        # its row from the hydrated state.
        _patient(session).patient_name = "Other^Name"
        session.save(sync=True)
    with Session(db) as session:
        reasons, passed, _ds = _graded(session, tmp_path, "r")
    assert not passed and _edited(reasons) == [_line(patients=1)], reasons


def test_a_current_status_is_still_restored_current(tmp_path):
    """The ordinary path: a pass, a save, a reopen -- nothing stale, PASS,
    and every status as the pass left it."""
    db = _store(tmp_path)
    with Session(db) as session:
        session.ingest(str(tmp_path / "in"))
        session.anonymize(session.audit())
        session.save(sync=True)
        expected = [e.phi_status for e in (_patient(session), _study(session), _inst(session))]
    with Session(db) as session:
        assert [e.phi_status for e in (_patient(session), _study(session), _inst(session))] == expected
        reasons, passed, _ds = _graded(session, tmp_path, "r")
    assert passed and reasons == [], reasons


def test_a_store_written_before_the_column_still_opens(tmp_path):
    """A store from before #774 has no column for the stale state. It opens,
    the column is added, and its statuses read as they did."""
    db = _store(tmp_path)
    with Session(db) as session:
        session.ingest(str(tmp_path / "in"))
        session.anonymize(session.audit())
        session.save(sync=True)
    with sqlite3.connect(db) as conn:
        for table in ("patients", "studies", "instances"):
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {STALE_COLUMN}")
    with Session(db) as session:
        assert _inst(session).phi_status is PhiStatus.REMEDIATED
        reasons, passed, _ds = _graded(session, tmp_path, "r")
    assert passed and reasons == [], reasons
    with sqlite3.connect(db) as conn:
        for table in ("patients", "studies", "instances"):
            assert STALE_COLUMN in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _keep_everything(session, root):
    path = root / "none.yaml"
    path.write_text(
        "privacy_profile: none\nremove_private_tags: false\nphi_tags:\n"
        "  '0010,0010': {action: KEEP}\n  '0010,0020': {action: KEEP}\n"
        "  '0008,0020': {action: KEEP}\n", encoding="utf-8")
    session.load_config(str(path))


def test_the_rekey_gate_reads_a_restored_stale_status(tmp_path):
    """`io_handlers._its_key_is_in_use` reads the raw status. An ID-less
    subject is scanned under its key (a policy that finds nothing, so no
    shift and no token leave other evidence), then its patient, study and
    instance are each edited and saved. After a reopen a real-ID file of
    its study must not re-key it: the scan ran under the key. Before, each
    stale status was stored as UNSCANNED, the gate saw nothing, and the
    patient was re-keyed to the real ID."""
    src = tmp_path / "in"
    write_ct(src / "a.dcm", "", SUFFIX, study_date=None)
    db = str(tmp_path / "s.db")
    with Session(db) as session:
        _keep_everything(session, tmp_path)
        session.ingest(str(src))
        session.anonymize(session.audit())
        assert _inst(session).phi_status is PhiStatus.CLEARED, "setup"
        _patient(session).patient_name = "Edited^Name"
        _study(session).study_time = "235959"
        _inst(session).set_attr("0008,1030", "edited")
        session.save(sync=True)
    other = tmp_path / "in2"
    write_ct(other / "b.dcm", "PID-REAL", SUFFIX, study_date=None)
    ds = pydicom.dcmread(str(other / "b.dcm"))
    ds.SOPInstanceUID = ds.file_meta.MediaStorageSOPInstanceUID = study_uid(SUFFIX) + ".1.2"
    ds.save_as(str(other / "b.dcm"))
    with Session(db) as session:
        session.ingest(str(other))
        ids = sorted(p.patient_id for p in session.store.patients)
    assert "PID-REAL" not in ids, ids


def test_a_restored_stale_status_draws_neither_555_notice(tmp_path, caplog):
    """#555's two notices read a status with its policy. A stale status
    reads UNSCANNED and no policy -- restored as it was in memory -- so it
    is neither a status "with no recorded policy" at load nor one
    "recorded under a policy other than the one in force" at export.
    Kills the stale restore counted as a legacy status."""
    db = _store(tmp_path)
    with Session(db) as session:
        session.ingest(str(tmp_path / "in"))
        session.anonymize(session.audit())
        _inst(session).set_attr("0008,1030", "edited")
        session.save(sync=True)
    caplog.clear()
    with caplog.at_level("WARNING"):
        with Session(db) as session:
            assert _inst(session).phi_status is PhiStatus.UNSCANNED
            session.export(str(tmp_path / "out"), use_compression=False)
            rows = [d for _, _, d in session.store_backend.get_audit_errors()]
    assert not [r for r in caplog.messages if "no recorded policy" in r], caplog.messages
    assert not [d for d in rows if "recorded under a policy other than the one in force" in d
                or "no recorded policy" in d], rows


def _rows(db):
    with sqlite3.connect(db) as conn:
        return {table: conn.execute(
                    f"SELECT phi_status, {STALE_COLUMN} FROM {table}").fetchall()
                for table in ("patients", "studies", "instances")}


def test_the_row_says_stale_while_stale_and_not_after_a_scan(tmp_path):
    """What the store records, table by table: a stale entity's row says
    `unscanned` -- what a build before the column reads -- beside the
    status the edit left; a re-audit and a pass write the current status
    and clear the stale one, which a later reopen must not read back."""
    db = _store(tmp_path)
    with Session(db) as session:
        session.ingest(str(tmp_path / "in"))
        session.anonymize(session.audit())
        _patient(session).patient_name = "Alpha^One"
        _study(session).study_date = "20040119"
        _inst(session).set_attr("0008,1030", "edited")
        session.save(sync=True)
        assert _rows(db) == {"patients": [("unscanned", "remediated")],
                             "studies": [("unscanned", "remediated")],
                             "instances": [("unscanned", "remediated")]}
        session.anonymize(session.audit())
        session.save(sync=True)
        rows = _rows(db)
    assert all(status != "unscanned" and stale is None
               for table in rows.values() for status, stale in table), rows
