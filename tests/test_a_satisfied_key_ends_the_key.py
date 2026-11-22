"""A satisfied proposal decides its key for the rest of the pass (#636).

`apply_remediation` decides each dedup key (`_remediation_key`) by its
first proposal that does not decline: applied, folded or satisfied. A
satisfied proposal (a REMOVE whose tag is already gone, an end state
already there with its record) used to claim no key, so a later proposal
on the same key still ran. On pydicom's JPEG2000.dcm under the default
private-tag removal and a `JITTER` rule on its private date, the scan
raises a REMOVE and then a SHIFT on one key; handed again, the REMOVE was
satisfied and the SHIFT then declined "no longer on the Instance", wrote
a `REMEDIATION_DECLINED` row and demoted the instance over a clean graph.

A decline still claims no key: a later proposal on it runs.

**Why this file imports what it does.** The pipeline through
`isocenter.session`, hand-built findings through `isocenter.privacy`, the
status through `isocenter.entities`: the `session`, `remediation`,
`privacy` and `entities` rows of the probe's `TARGETS`.
"""
import shutil
import sqlite3

import pytest
from pydicom.data import get_testdata_file

from isocenter.entities import PhiStatus
from isocenter.privacy import PhiFinding, PhiRemediation
from isocenter.session import DicomSession
from support.ct_small_files import write_ct

PRIVATE_DATE = "0009,1042"


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


def _remediated_session(tmp_path, rules):
    """CT_small under `none` plus `rules`, audited, anonymized and saved."""
    write_ct(tmp_path / "src" / "a.dcm", "PID-636", 636, name="Alpha^One")
    cfg = tmp_path / "c.yaml"
    cfg.write_text("privacy_profile: none\nphi_tags:\n"
                   + "".join(f"  '{tag}': {{action: {action}}}\n"
                             for tag, action in rules.items()))
    session = DicomSession(str(tmp_path / "s.db"))
    session.load_config(str(cfg))
    session.ingest(str(tmp_path / "src"))
    report = session.audit()
    session.anonymize(report)
    session.save(sync=True)
    return session, report


def _instance(session):
    return session.store.patients[0].studies[0].series[0].instances[0]


def _hand(instance, action, original=None):
    return PhiFinding(
        entity_uid=instance.sop_instance_uid, entity_type="Instance",
        field_name="private date", value=original, reason="hand-built",
        entity=instance, tag=PRIVATE_DATE,
        remediation_proposal=PhiRemediation(
            action_type=action, target_attr=PRIVATE_DATE, original_value=original,
            metadata={"patient_id": "PID-636"}))


def test_a_proposal_after_a_satisfied_removal_on_the_same_key_does_not_run(tmp_path):
    session, _ = _remediated_session(tmp_path, {"0008,0080": "REMOVE"})
    with session:
        instance = _instance(session)
        assert instance.phi_status is PhiStatus.REMEDIATED
        assert PRIVATE_DATE not in instance.attributes
        before = len(_rows(session))

        applied = session.anonymize([_hand(instance, "REMOVE_TAG"),
                                     _hand(instance, "SHIFT_DATE", "20200101")])

        assert applied == 0
        assert _rows(session)[before:] == []
        assert instance.phi_status is PhiStatus.REMEDIATED
        assert PRIVATE_DATE not in instance.attributes


def test_a_decline_still_leaves_the_key_to_the_next_proposal(tmp_path):
    """The SHIFT first declines on the absent tag and claims no key, so the
    REMOVE behind it still runs and is satisfied."""
    session, _ = _remediated_session(tmp_path, {"0008,0080": "REMOVE"})
    with session:
        instance = _instance(session)
        before = len(_rows(session))

        applied = session.anonymize([_hand(instance, "SHIFT_DATE", "20200101"),
                                     _hand(instance, "REMOVE_TAG")])

        assert applied == 0
        rows = _rows(session)[before:]
        assert [a for a, _ in rows] == ["REMEDIATION_DECLINED"], rows
        assert "no longer on" in rows[0][1]
        assert PRIVATE_DATE not in instance.attributes


def test_the_satisfied_key_is_still_counted_by_the_tally(tmp_path):
    """A partial pass, then the whole report: the keys the first pass
    applied are satisfied in the second, and the tally still counts them,
    so the instance completes."""
    write_ct(tmp_path / "src" / "a.dcm", "PID-636T", 6361, name="Alpha^One")
    cfg = tmp_path / "c.yaml"
    cfg.write_text("privacy_profile: none\nphi_tags:\n"
                   "  '0008,0080': {action: REMOVE}\n"
                   "  '0008,0090': {action: REMOVE}\n")
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.load_config(str(cfg))
        session.ingest(str(tmp_path / "src"))
        session.store.patients[0].studies[0].series[0].instances[0].set_attr(
            "0008,0080", "Some Hospital")
        session.store.patients[0].studies[0].series[0].instances[0].set_attr(
            "0008,0090", "Some^Doctor")
        report = session.audit()
        instance = _instance(session)
        first = [f for f in report.findings if f.tag == "0008,0080"]
        assert first and len(first) < len(report.findings)
        session.anonymize(first)
        assert instance.phi_status is PhiStatus.IDENTIFIED

        session.anonymize(report)

        assert instance.phi_status is PhiStatus.REMEDIATED


def test_the_scanned_shape_writes_nothing_on_the_second_call(tmp_path):
    """pydicom's JPEG2000.dcm, the default private removal and a `JITTER`
    rule on its private date: the second call returns 0, writes no row
    and leaves the instance REMEDIATED."""
    src = tmp_path / "src"
    src.mkdir()
    shutil.copy(get_testdata_file("JPEG2000.dcm"), src / "a.dcm")
    with DicomSession(str(tmp_path / "s.db")) as session:
        tags = dict(session.configuration.phi_tags)
        tags[PRIVATE_DATE] = {"name": "private date", "action": "JITTER"}
        session.configuration.phi_tags = tags
        session.ingest(str(src))
        report = session.audit()
        session.anonymize(report)
        instance = _instance(session)
        assert instance.phi_status is PhiStatus.REMEDIATED
        before = len(_rows(session))

        assert session.anonymize(report) == 0

        assert _rows(session)[before:] == []
        assert instance.phi_status is PhiStatus.REMEDIATED
