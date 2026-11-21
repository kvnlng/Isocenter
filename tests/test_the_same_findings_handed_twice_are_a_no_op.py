"""The same findings handed to `anonymize()` twice: the second call is a
no-op (#636, #990, #991, #1196, #989).

The first call applies the report. The second meets a graph that already
holds every end state the report asks for, each with the record that a
remediation left it there. Before this, the second call wrote the
owner-level remediations again (#990), every instance-level and nested
date shift (#1196) and nested `REPLACE` (#991) again, each with a row and
counted; on pydicom's JPEG2000.dcm it wrote one `REMEDIATION_DECLINED`
row and demoted the instance (#636); and a saved instance read
`has_unsaved_changes` after a pass that changed nothing (#989).

The grid: three policies, two shapes of what is handed (the instance
findings, the whole report), with and without `save(sync=True)` between
the two calls, over two sources. Each cell asserts the whole outcome:
the call returns 0, writes no audit row of any type, changes no value in
the graph, leaves the instance's status as it was, and (saved between)
leaves the instance with nothing to save.

**Why this file imports what it does.** The pipeline through
`isocenter.session`; the graph walk and the status through
`isocenter.entities`. So it is in the `session`, `remediation` and
`entities` rows of the probe's `TARGETS`.
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

ROOT = "1.2.826.0.1.3680043.10.1196"
PRIVATE_DATE = "0009,1042"


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)


def _ct_with_a_nested_item(src):
    ds = pydicom.dcmread(get_testdata_file("CT_small.dcm"))
    ds.PatientID = "PID-X3"
    ds.PatientName = "Alpha^One"
    ds.StudyInstanceUID = f"{ROOT}.1"
    ds.SeriesInstanceUID = f"{ROOT}.1.1"
    ds.SOPInstanceUID = f"{ROOT}.1.1.1"
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    item = Dataset()
    item.ReferencedSOPClassUID = ds.SOPClassUID
    item.ReferencedSOPInstanceUID = f"{ROOT}.9.9.9"
    item.ContentDate = "19970430"
    item.SeriesDescription = "nested description"
    item.InstitutionName = "Nested Hospital"
    ds.ReferencedImageSequence = Sequence([item])
    ds.save_as(str(src / "a.dcm"))


def _jpeg2000(src):
    ds = pydicom.dcmread(get_testdata_file("JPEG2000.dcm"))
    assert ds[0x0009, 0x1042].VR == "DA" and ds[0x0009, 0x1042].value
    ds.save_as(str(src / "a.dcm"))


SOURCES = {
    # Content Date jittered at the top level and in the item (#1196); the
    # nested item carries the UID, a dummy and a value under basic (#991).
    "ct-nested": (_ct_with_a_nested_item,
                  {"0008,0023": {"name": "Content Date", "action": "JITTER"}}),
    # The private sweep's REMOVE and the JITTER rule on one key (#636).
    "jpeg2000": (_jpeg2000,
                 {PRIVATE_DATE: {"name": "private date", "action": "JITTER"}}),
}

POLICIES = {
    "floor": (None, {"0010,0010": {"name": "Patient Name", "action": "REMOVE"}}),
    "basic": ("basic", {}),
    "none": ("none", {"0010,0010": {"name": "Patient Name", "action": "REMOVE"}}),
}


def _rows(session):
    session.store_backend.flush_audit_queue()
    with sqlite3.connect(session.store_backend.db_path) as conn:
        return conn.execute(
            "SELECT action_type, entity_uid, details FROM audit_log ORDER BY rowid").fetchall()


def _snapshot(patients):
    """Every value the graph holds, keyed by where it lives."""
    out = {}
    for p in patients:
        out[("patient", "patient_id")] = repr(p.patient_id)
        out[("patient", "patient_name")] = repr(p.patient_name)
        for st in p.studies:
            for field in ("study_instance_uid", "study_date", "study_time",
                          "date_shifted", "_shifted_study_date"):
                out[("study", field)] = repr(getattr(st, field))
            for se in st.series:
                out[("series", "series_instance_uid")] = repr(se.series_instance_uid)
                for inst in se.instances:
                    for item, path in iter_item_tree(inst):
                        for tag, value in item.attributes.items():
                            out[("inst", path, tag)] = repr(value)
                        for tag, seq in item.sequences.items():
                            out[("inst", path, tag, "items")] = len(seq.items)
    return out


def _session(tmp_path, source, policy):
    build, source_rules = SOURCES[source]
    profile, policy_rules = POLICIES[policy]
    src = tmp_path / "src"
    src.mkdir()
    build(src)
    session = DicomSession(str(tmp_path / "s.db"))
    load_fixed_secret(session, tmp_path, FIXED_A)
    if profile is not None:
        cfg = tmp_path / "c.yaml"
        cfg.write_text(f"privacy_profile: {profile}\nphi_tags: {{}}\n")
        session.load_config(str(cfg))
    tags = dict(session.configuration.phi_tags)
    tags.update(policy_rules)
    tags.update(source_rules)
    session.configuration.phi_tags = tags
    session.ingest(str(src))
    return session


@pytest.mark.parametrize("save_between", [True, False], ids=["saved", "unsaved"])
@pytest.mark.parametrize("handed", ["instance", "whole"])
@pytest.mark.parametrize("policy", sorted(POLICIES))
@pytest.mark.parametrize("source", sorted(SOURCES))
def test_the_second_call_changes_nothing(tmp_path, source, policy, handed, save_between):
    session = _session(tmp_path, source, policy)
    with session:
        report = session.audit()
        findings = (list(report.findings) if handed == "whole"
                    else [f for f in report.findings if f.entity_type == "Instance"])
        instance = session.store.patients[0].studies[0].series[0].instances[0]
        # Non-vacuity: the first call does work.
        assert session.anonymize(findings) > 0
        if save_between:
            session.save(sync=True)
            assert not instance.has_unsaved_changes
        status = instance.phi_status
        graph = _snapshot(session.store.patients)
        rows = _rows(session)

        assert session.anonymize(findings) == 0

        assert _rows(session)[len(rows):] == []
        assert _snapshot(session.store.patients) == graph
        assert instance.phi_status is status
        if save_between:
            assert not instance.has_unsaved_changes
