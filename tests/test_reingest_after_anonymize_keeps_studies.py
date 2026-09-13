"""A later study for an anonymized patient keeps every study in the store (#548).

The issue's reproduction, turned into assertions: two patients ingested,
anonymized and saved; then a new study for the first patient, still under
its original Patient ID, ingested, audited and anonymized, ending in
`save()`, in `export()`, or in `save()` with both passes in one session.
Measured on 1c41e5e in all three flows and on both parallel paths:
2 / 2 / 2 / 2 rows after pass 1, 3 / 3 / 3 / 3 after pass 2's ingest, and
**2 / 1 / 1 / 1** after pass 2 -- the stored study and the new one both
deleted, with no warning and no audit row; `export()` wrote all three
files and then left the store in the same state.

**What this file can and cannot kill.** Two layers keep the rows now, and
either one alone is enough to keep this test green: the save no longer
deletes a row any object in memory holds
(`tests/test_save_keeps_rows_memory_holds.py`), and `anonymize()` merges
the re-ingested patient into the stored one
(`tests/test_patients_sharing_an_id_are_merged.py`). This is the
end-to-end smoke test for both reverted together, and for any change
that re-seeds the moved study's date offset; each layer's own killer is
in its own file.
"""
import glob
from datetime import date

import pydicom
import pytest

from isocenter import Session

from support.ct_small_files import row_counts, stored_study_uids, study_uid, write_ct

MODES = ["threads", "processes"]


@pytest.fixture(params=MODES)
def mode(request, monkeypatch):
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)
    if request.param == "threads":
        monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
        monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    else:
        monkeypatch.setenv("ISOCENTER_FORCE_PROCESSES", "1")
        monkeypatch.delenv("ISOCENTER_FORCE_THREADS", raising=False)
    return request.param


def _pass_one(session, folder):
    session.ingest(folder)
    session.anonymize(session.audit())
    session.save(sync=True)


def _pass_two(session, folder):
    session.ingest(folder)
    session.anonymize(session.audit())


@pytest.mark.parametrize("flow", ["save", "export", "same-session"])
def test_a_later_study_for_an_anonymized_patient_keeps_every_study(
        tmp_path, mode, flow):
    first, second = str(tmp_path / "first"), str(tmp_path / "second")
    # CT_small's own StudyDate is 20040119; the later study is ten days on.
    write_ct(tmp_path / "first" / "a.dcm", "PAT-001", "1")
    write_ct(tmp_path / "first" / "b.dcm", "PAT-002", "2")
    write_ct(tmp_path / "second" / "c.dcm", "PAT-001", "3",
             study_date="20040129")
    db = str(tmp_path / "store.db")
    out = tmp_path / "export"

    if flow == "same-session":
        with Session(db) as session:
            _pass_one(session, first)
            assert row_counts(db) == (2, 2, 2, 2)
            _pass_two(session, second)
            session.save(sync=True)
    else:
        with Session(db) as session:
            _pass_one(session, first)
        assert row_counts(db) == (2, 2, 2, 2)
        with Session(db) as session:
            _pass_two(session, second)
            if flow == "save":
                session.save(sync=True)
            else:
                session.export(str(out))

    assert row_counts(db) == (2, 3, 3, 3)
    assert stored_study_uids(db) == {study_uid("1"), study_uid("2"),
                                     study_uid("3")}

    if flow == "export":
        files = sorted(glob.glob(str(out / "**" / "*.dcm"), recursive=True))
        assert len(files) == 3
        by_study = {}
        for path in files:
            ds = pydicom.dcmread(path)
            by_study[ds.StudyInstanceUID] = ds.PatientID
        assert by_study[study_uid("1")] == by_study[study_uid("3")]
        assert by_study[study_uid("1")].startswith("ANON_")
        assert by_study[study_uid("2")] != by_study[study_uid("1")]

    with Session(db) as session:
        assert len(session.store.patients) == 2
        holder = next(p for p in session.store.patients
                      if study_uid("1") in
                      [s.study_instance_uid for s in p.studies])
        studies = {s.study_instance_uid: s for s in holder.studies}
        assert sorted(studies) == [study_uid("1"), study_uid("3")]
        shifted_1 = studies[study_uid("1")].study_date
        shifted_3 = studies[study_uid("3")].study_date
        assert isinstance(shifted_1, date) and isinstance(shifted_3, date)
        assert shifted_1 != date(2004, 1, 19), "setup: the date was not shifted"
        # One offset for the subject: the ten days between the originals
        # survive, because the pseudonym and the original seed one key.
        assert (shifted_3 - shifted_1).days == 10
