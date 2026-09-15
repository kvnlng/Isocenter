"""The date jitter is seeded on the patient, not on the PatientID text (#517).

`_get_date_shift` hashed whatever PatientID the scan saw, and pass 1 of
`anonymize()` replaces that id -- so every later pass hashed a different
string and computed a different offset, and a date first shifted in a
later pass fell off the offset its siblings got. "Date jitter is
deterministic per patient so intervals survive" held only within one
pass.

**The rule these tests hold.** One identity, one offset, whichever
spelling of its id the pass happens to read: the original, or the
pseudonym pass 1 wrote over it.

**What changed in 0.9.7, and why this file was rewritten.** 0.9.6 kept
the rule by *reading the seed back out of the pseudonym*: the pseudonym
carried 12 hex characters of an unkeyed SHA-256 of the original, and the
jitter seeded on the first 8 of them. That coupling was the defect --
anyone holding an exported file could compute its offset. The tests that
pinned the coupling bit for bit are gone with it. The rule is now kept
under the project secret: an original id is canonicalized to the
pseudonym it would become, and the offset is an HMAC of that under its
own label, so both spellings agree and neither reveals the offset
(`test_the_project_secret_keys_the_pseudonym_and_offset.py` holds the
derivation; this file holds the pipeline).

**Across stores**, the rule holds only for stores sharing a project
secret, and that condition *is* the security property: an export must
not carry what is needed to reproduce its offsets. A store that ingests
another project's export without its secret shifts under its own, and
says so in a `WARNING` row.

**Literals** are under `FIXED_A`/`FIXED_B`, computed once and pasted.

**Why this file imports what it does.** It reaches the offset through
`isocenter.remediation`, the pseudonym through `isocenter.privacy`, and
runs a whole `isocenter.session`, so it charges all three modules' probe
rows; see `test_mutation_probe_targets.py`.
"""
import os
import sqlite3
from datetime import date

import pydicom
import pytest

from isocenter.entities import Instance, Patient, Series, Study
from isocenter.session import DicomSession

from support.project_secret import FIXED_A, FIXED_B, load_fixed_secret

SC_SOP_CLASS = "1.2.840.10008.5.1.4.1.1.7"


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _with_study(patient, suffix, study_date):
    study = Study(f"1.2.826.0.1.517.{suffix}", study_date)
    series = Series(f"1.2.826.0.1.517.{suffix}.1", "OT", 1)
    instance = Instance(f"1.2.826.0.1.517.{suffix}.1.0", SC_SOP_CLASS, 1)
    series.instances.append(instance)
    study.series.append(series)
    patient.studies.append(study)
    return study


def test_a_study_shifted_in_a_later_pass_keeps_the_interval(tmp_path):
    """T6. The defect, end to end.

    A study added after pass 1 is raised by pass 2's scan, by when
    PatientID is the pseudonym. Under `FIXED_A`, `P1` is -364 days in
    both passes. Red on: the canonical key taken as the id's own text
    (pass 2 seeds on the pseudonym's text and lands elsewhere).
    """
    with DicomSession(str(tmp_path / "seed.db")) as session:
        load_fixed_secret(session, tmp_path, FIXED_A)
        patient = Patient("P1", "Orig^Name")
        session.store.patients.append(patient)
        first = _with_study(patient, "a", date(2023, 1, 1))
        session.anonymize(session.audit())
        assert (first.study_date - date(2023, 1, 1)).days == -364

        assert patient.patient_id == "ANON_d932e13c0c2fa7dafbd43b77"
        second = _with_study(patient, "b", date(2023, 6, 1))
        session.anonymize(session.audit())

        assert (second.study_date - date(2023, 6, 1)).days == -364
        assert (second.study_date - first.study_date) == (
            date(2023, 6, 1) - date(2023, 1, 1)), (
            "the interval between two studies of one patient did not survive")


def _synthetic_ct(directory):
    """CT_small with synthetic UIDs (its own embed the acquisition date)."""
    ds = pydicom.dcmread(pydicom.data.get_testdata_file("CT_small.dcm"))
    ds.PatientID = "MRN0012345"
    ds.StudyDate = "20040119"
    for keyword, uid in (("StudyInstanceUID", "1.2.826.0.1.517.9"),
                         ("SeriesInstanceUID", "1.2.826.0.1.517.9.1"),
                         ("SOPInstanceUID", "1.2.826.0.1.517.9.1.1"),
                         ("FrameOfReferenceUID", "1.2.826.0.1.517.9.9")):
        setattr(ds, keyword, uid)
    ds.file_meta.MediaStorageSOPInstanceUID = "1.2.826.0.1.517.9.1.1"
    os.makedirs(directory, exist_ok=True)
    ds.save_as(os.path.join(directory, "ct.dcm"))


def _export_project_a(tmp_path):
    """Store one, on FIXED_A: ingest, anonymize (-306 days), export."""
    source = str(tmp_path / "in")
    _synthetic_ct(source)
    out = str(tmp_path / "out")
    with DicomSession(str(tmp_path / "one.db")) as first:
        first.ingest(source)
        load_fixed_secret(first, tmp_path, FIXED_A)
        study = first.store.patients[0].studies[0]
        first.anonymize(first.audit())
        assert (study.study_date - date(2004, 1, 19)).days == -306
        first.export(out)
    return out


def _foreign_notices(db):
    with sqlite3.connect(db) as conn:
        return [r[0] for r in conn.execute(
            "SELECT details FROM audit_log WHERE action_type='WARNING'")
            if "minted under a different project secret" in r[0]]


def _reanonymize(db, out, tmp_path, secret=None):
    """Store two: optionally adopt `secret`, ingest `out`, anonymize."""
    with DicomSession(db) as second:
        if secret is not None:
            load_fixed_secret(second, tmp_path, secret)
        second.ingest(out)
        study = second.store.patients[0].studies[0]
        before = study.study_date
        second.anonymize(second.audit())
        return (study.study_date - before).days


def test_a_reingested_export_keeps_its_offset_with_the_projects_secret(tmp_path):
    """T9. Store two adopts project A's secret, ingests A's export, and
    gives its patient A's offset -- one patient, one offset, across
    stores -- with no foreign-pseudonym warning.

    (Re-anonymizing an export shifts its already-shifted date a second
    time; that is pre-existing and not what this pins. What it pins is
    that the second shift is the patient's own offset.)

    Red on: `load_project_secret` a no-op (store two generates its own).
    """
    out = _export_project_a(tmp_path)
    db = str(tmp_path / "two.db")
    assert _reanonymize(db, out, tmp_path, FIXED_A) == -306
    assert _foreign_notices(db) == []


def test_a_reingested_export_under_another_secret_is_warned(tmp_path):
    """T9b. Store two holds FIXED_B (loaded while it was still empty) and
    ingests A's export: its offset is B's for that pseudonym, and exactly
    one WARNING names the foreign pseudonym.

    Red on: `_pseudonym_verifies` always True (no warning); the #644 seed
    check in `_shift_target_moved` without its holder's-own-ID exemption
    (offset 0: every shift declines).
    """
    out = _export_project_a(tmp_path)
    db = str(tmp_path / "two.db")
    offset = _reanonymize(db, out, tmp_path, FIXED_B)
    # FIXED_B's offset for A's pseudonym `ANON_62ef31e5cf3957e4b1cf54e9`,
    # recomputed from the documented construction outside the module.
    assert offset == -72
    [notice] = _foreign_notices(db)
    assert notice.startswith("1 patient in this store carries an `ANON_` pseudonym")
    assert "a new one was generated" not in notice


def test_a_reingested_export_without_any_secret_generates_and_warns(tmp_path):
    """T9c, case E. Store two loads nothing: it generates its own secret
    and writes exactly one WARNING saying it did.

    Red on: `_pseudonym_verifies` always True (no warning); a warning
    only when the store already had a secret.
    """
    out = _export_project_a(tmp_path)
    db = str(tmp_path / "two.db")
    _reanonymize(db, out, tmp_path)
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT secret_hex, origin FROM project_secret").fetchall()
    assert [(len(bytes.fromhex(r[0])), r[1]) for r in rows] == [(32, "generated")]
    [notice] = _foreign_notices(db)
    assert "and this store had none of its own, so a new one was generated" in notice
