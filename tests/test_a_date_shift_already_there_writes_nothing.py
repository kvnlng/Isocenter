"""A date already holding its shift, with the shift's record, is not
shifted again (#1196).

`_shift_target_moved` admits a target that holds the shift it would
write (#569: `anonymize(report)` twice must stay idempotent), and the arm
then wrote it again: one `REMEDIATION_SHIFT_DATE` row per date per call,
the instance counted as applied and dirtied. Now a target that holds the
shift, with the record of that shift (`date_shift_vouches_for`, any
depth) or, on an instance's owner-stamped copy, the remediation record
an owner's write left, is satisfied: no row, not counted.

A date set by hand to the shifted value, with no record, is still
shifted once with its row: the record is the gate, never equality.

**Why this file imports what it does.** The pipeline through
`isocenter.session`, the graph through `isocenter.entities`: the
`session`, `remediation` and `entities` rows of the probe's `TARGETS`.
"""
import sqlite3

import pydicom
import pytest
from pydicom.data import get_testdata_file
from pydicom.dataset import Dataset
from pydicom.sequence import Sequence

from isocenter.entities import iter_item_tree
from isocenter.session import DicomSession
from support.project_secret import FIXED_A, load_fixed_secret

CONTENT_DATE = "0008,0023"
STUDY_DATE = "0008,0020"
ROOT = "1.2.826.0.1.3680043.10.11961"


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)


def _rows(session):
    session.store_backend.flush_audit_queue()
    with sqlite3.connect(session.store_backend.db_path) as conn:
        return conn.execute(
            "SELECT action_type, details FROM audit_log ORDER BY rowid").fetchall()


def _session(tmp_path, config, nested=False):
    ds = pydicom.dcmread(get_testdata_file("CT_small.dcm"))
    ds.PatientID = "PID-1196"
    ds.StudyInstanceUID = f"{ROOT}.1"
    ds.SeriesInstanceUID = f"{ROOT}.1.1"
    ds.SOPInstanceUID = f"{ROOT}.1.1.1"
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    if nested:
        item = Dataset()
        item.ReferencedSOPClassUID = ds.SOPClassUID
        item.ReferencedSOPInstanceUID = f"{ROOT}.9"
        item.ContentDate = "19970430"
        ds.ReferencedImageSequence = Sequence([item])
    (tmp_path / "src").mkdir()
    ds.save_as(str(tmp_path / "src" / "a.dcm"))
    cfg = tmp_path / "c.yaml"
    cfg.write_text(config)
    session = DicomSession(str(tmp_path / "s.db"))
    load_fixed_secret(session, tmp_path, FIXED_A)
    session.load_config(str(cfg))
    session.ingest(str(tmp_path / "src"))
    return session


JITTER_CONTENT_DATE = "privacy_profile: none\nphi_tags:\n  '0008,0023': {action: JITTER}\n"


def _instance(session):
    return session.store.patients[0].studies[0].series[0].instances[0]


def _twice(session, findings):
    session.anonymize(findings)
    session.save(sync=True)
    before = len(_rows(session))
    applied = session.anonymize(findings)
    return applied, _rows(session)[before:]


def test_an_instance_date_shifted_once_is_not_written_again(tmp_path):
    with _session(tmp_path, JITTER_CONTENT_DATE) as session:
        report = session.audit()
        instance = _instance(session)
        applied, rows = _twice(session, [f for f in report.findings
                                         if f.entity_type == "Instance"])
        assert (applied, rows) == (0, [])
        assert not instance.has_unsaved_changes


def test_a_nested_date_shifted_once_is_not_written_again(tmp_path):
    with _session(tmp_path, JITTER_CONTENT_DATE, nested=True) as session:
        report = session.audit()
        # Non-vacuity: a top-level and a nested Content Date are raised.
        assert sorted(bool(f.entity_path) for f in report.findings
                      if f.tag == CONTENT_DATE) == [False, True]
        instance = _instance(session)
        applied, rows = _twice(session, [f for f in report.findings
                                         if f.entity_type == "Instance"])
        assert (applied, rows) == (0, [])
        assert not instance.has_unsaved_changes


def test_an_owner_stamped_date_copy_under_a_satisfied_study_is_not_written_again(tmp_path):
    """The floor jitters Study Date. On the second call the Study is
    already there, so its write does not reach the instance's `0008,0020`
    copy and that copy's finding does not fold: it is judged on its own,
    and the remediation record the Study's first write left is what reads
    it as done."""
    with _session(tmp_path, "phi_tags: {}\n") as session:
        report = session.audit()
        assert any(f.entity_type == "Instance" and f.tag == STUDY_DATE
                   for f in report.findings)
        applied, rows = _twice(session, report)
        assert [r for r in rows if STUDY_DATE in r[1] or "study_date" in r[1]] == []
        assert applied == 0, rows


def test_a_date_holding_the_shift_without_a_record_is_still_shifted(tmp_path):
    with _session(tmp_path, JITTER_CONTENT_DATE, nested=True) as session:
        report = session.audit()
        findings = [f for f in report.findings if f.entity_type == "Instance"]
        instance = _instance(session)
        session.anonymize(findings)
        # Take every shift record away, leaving the shifted values.
        for item, _ in iter_item_tree(instance):
            item._shifted_dates = None  # pylint: disable=protected-access
        before = len(_rows(session))

        session.anonymize(findings)

        shifted = [r for r in _rows(session)[before:] if r[0] == "REMEDIATION_SHIFT_DATE"]
        assert len(shifted) == 2, shifted
