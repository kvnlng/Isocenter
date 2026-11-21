"""An instance finding on a copy the export stamps from its owner, handed
without the owner's finding, leaves the instance IDENTIFIED, with or
without a scan tally behind the pass (#960; owner ruling Q5 option D,
2026-11-21).

The flow: `audit()`, keep the report, `close()`, reopen, and hand
`[f for f in report.findings if f.entity_type == "Instance"]`. A plain
list carries no tally and the reopened session has none, so nothing
settled the instance's open owner-stamped copy: `_owner_stamps_copy`
answered "the owner was not handed in", which wrote no row and named
nothing, and the instance's other successes left it REMEDIATED while its
file carried the source value. Not silent (the patient and study did not
read REMEDIATED, so the manifest was `False` and the grade
REVIEW_REQUIRED), but the instance's own stored status was false.

Now that answer still writes no row, and names the instance for the pass
end's demotion, tally or none. No row means nothing permanent: the owner
handed later, then a re-audit, clears it.

**Why this file imports what it does.** The pipeline through
`isocenter.session`, the status through `isocenter.entities`: the
`session`, `remediation` and `entities` rows of the probe's `TARGETS`.
"""
import copy
import sqlite3

import pytest

from isocenter.entities import PhiStatus
from isocenter.session import DicomSession
from support.ct_small_files import write_ct

RULES = {
    "name-remove": ("0010,0010", "{action: REMOVE}"),
    "name-replace": ("0010,0010", "{action: REPLACE, value: ANONYMIZED}"),
    "study-date-jitter": ("0008,0020", "{action: JITTER}"),
}


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)


def _declines(session):
    session.store_backend.flush_audit_queue()
    with sqlite3.connect(session.store_backend.db_path) as conn:
        return conn.execute("SELECT details FROM audit_log "
                            "WHERE action_type='REMEDIATION_DECLINED'").fetchall()


def _audited(tmp_path, rule, save_after_audit):
    write_ct(tmp_path / "src" / "a.dcm", "PID-960", 960, name="Alpha^One")
    tag, action = RULES[rule]
    cfg = tmp_path / "c.yaml"
    cfg.write_text(f"privacy_profile: none\nphi_tags:\n  '{tag}': {action}\n")
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.load_config(str(cfg))
        session.ingest(str(tmp_path / "src"))
        report = session.audit()
        if save_after_audit:
            session.save(sync=True)
    return report, str(cfg)


def _instance(session):
    return session.store.patients[0].studies[0].series[0].instances[0]


@pytest.mark.parametrize("save_after_audit", [False, True], ids=["unsaved", "saved"])
@pytest.mark.parametrize("rule", sorted(RULES))
def test_instance_findings_from_a_kept_report_after_a_reopen_leave_the_instance_identified(
        tmp_path, rule, save_after_audit):
    report, cfg = _audited(tmp_path, rule, save_after_audit)
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.load_config(cfg)
        findings = [f for f in report.findings if f.entity_type == "Instance"]
        # Non-vacuity: the instance's copy of the owner's tag is among them.
        tag = RULES[rule][0]
        assert any(f.tag == tag for f in findings), [f.tag for f in findings]

        session.anonymize(findings)

        assert _instance(session).phi_status is PhiStatus.IDENTIFIED
        # Option D writes no row for the copy the owner was not handed.
        assert _declines(session) == []


def test_the_owner_handed_later_then_a_reaudit_clears_it(tmp_path):
    report, cfg = _audited(tmp_path, "name-remove", True)
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.load_config(cfg)
        session.anonymize([f for f in report.findings if f.entity_type == "Instance"])
        instance = _instance(session)
        assert instance.phi_status is PhiStatus.IDENTIFIED

        session.anonymize(report)
        session.audit()

        assert instance.phi_status is PhiStatus.CLEARED


def test_a_kept_report_not_a_list_still_settles_by_its_tally(tmp_path):
    """A report narrowed with `copy.copy` carries its tally, which already
    held the instance open before #960; it still does."""
    report, cfg = _audited(tmp_path, "name-remove", True)
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.load_config(cfg)
        narrowed = copy.copy(report)
        narrowed.findings = [f for f in report.findings if f.entity_type == "Instance"]

        session.anonymize(narrowed)

        assert _instance(session).phi_status is PhiStatus.IDENTIFIED
        assert _declines(session) == []
