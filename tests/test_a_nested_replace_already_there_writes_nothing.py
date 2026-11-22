"""A `REPLACE` inside a sequence whose item already holds exactly the
value it would write is satisfied (#991; owner ruling Q2, 2026-11-21).

A top-level element is read as done by the instance's remediation record
(#952). A nested item carries no such record, so a nested `REPLACE`
handed a second time wrote the same value again, wrote a
`REMEDIATION_REPLACE` row, counted as applied and left the saved
instance with something to save. By owner ruling a nested item is read
on equality: it holds exactly what the proposal writes. The scan never
raises a `REPLACE` whose value already equals the rule's, so at that
depth equality means this pipeline wrote it, or a hand edit set exactly
the rule's value; either way the file already holds the rule's value.
No nested record is stored.

**Why this file imports what it does.** The pipeline through
`isocenter.session`: the `session` and `remediation` rows of the probe's
`TARGETS`.
"""
import sqlite3

import pydicom
import pytest
from pydicom.data import get_testdata_file
from pydicom.dataset import Dataset
from pydicom.sequence import Sequence

from isocenter.session import DicomSession
from support.project_secret import FIXED_A, load_fixed_secret

ROOT = "1.2.826.0.1.3680043.10.991"
REF_IMAGE_SEQ = "0008,1140"


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


def _session(tmp_path, config):
    ds = pydicom.dcmread(get_testdata_file("CT_small.dcm"))
    ds.PatientID = "PID-991"
    ds.StudyInstanceUID = f"{ROOT}.1"
    ds.SeriesInstanceUID = f"{ROOT}.1.1"
    ds.SOPInstanceUID = f"{ROOT}.1.1.1"
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    item = Dataset()
    item.ReferencedSOPClassUID = ds.SOPClassUID
    item.ReferencedSOPInstanceUID = f"{ROOT}.9"
    item.ContentDate = "19970430"
    item.SeriesDescription = "nested description"
    item.InstitutionName = "Nested Hospital"
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


VALUE = ("privacy_profile: none\nphi_tags:\n"
         "  '0008,103e': {action: REPLACE, value: SERIES}\n")
BASIC = "privacy_profile: basic\nphi_tags: {}\n"


def _nested(report):
    return [f for f in report.findings if f.entity_path
            and f.remediation_proposal is not None
            and f.remediation_proposal.action_type == "REPLACE_TAG"]


def _second(session, findings):
    session.anonymize(findings)
    session.save(sync=True)
    before = len(_rows(session))
    return session.anonymize(findings), _rows(session)[before:]


def test_a_nested_value_replace_handed_twice_writes_once(tmp_path):
    with _session(tmp_path, VALUE) as session:
        report = session.audit()
        findings = _nested(report)
        assert [f.tag for f in findings] == ["0008,103e"]
        assert _second(session, findings) == (0, [])


def test_nested_dummies_and_uids_under_basic_handed_twice_write_once(tmp_path):
    with _session(tmp_path, BASIC) as session:
        report = session.audit()
        findings = _nested(report)
        # Non-vacuity: a UID replacement, dummies and a value, all nested.
        assert {"0008,1155", "0008,0023", "0008,0080"} <= {f.tag for f in findings}
        assert _second(session, findings) == (0, [])
        item = session.store.patients[0].studies[0].series[0].instances[0] \
            .sequences[REF_IMAGE_SEQ].items[0]
        assert item.attributes["0008,1155"].startswith("2.25.")


def test_a_nested_replace_handed_twice_leaves_the_instance_saved(tmp_path):
    with _session(tmp_path, VALUE) as session:
        report = session.audit()
        instance = session.store.patients[0].studies[0].series[0].instances[0]
        _second(session, _nested(report))
        assert not instance.has_unsaved_changes

