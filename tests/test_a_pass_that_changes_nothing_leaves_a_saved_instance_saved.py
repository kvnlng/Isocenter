"""A pass that changes nothing leaves a saved instance with nothing to
save (#989).

A satisfied finding stamped its entity REMEDIATED at once, and the pass
end (`_settle_statuses`) then demoted it back to IDENTIFIED when it held
something else open: the status ended where it began, the revision moved
twice, and the next save wrote the row again. A satisfied finding now
records nothing during the pass; its stamp is settled once at the pass
end, net of the demotions, so an entity whose status does not change is
not touched. The other half of #989, the owner arms writing their copies
again, is #990's (`test_an_owner_already_holding_its_replacement_writes_nothing.py`).

The guards: the end status is what it was before for each start, a
direct `_apply_single_remediation` call outside a pass still stamps at
once, and the live Series arm (R2-1) still demotes an instance whose only
change this pass is a satisfied finding.

**Why this file imports what it does.** The pipeline through
`isocenter.session`, the entities and the status through
`isocenter.entities`, the service through `isocenter.remediation`,
hand-built findings through `isocenter.privacy`: those four rows of the
probe's `TARGETS`.
"""
import pytest

from isocenter.entities import Instance, PhiStatus
from isocenter.privacy import PhiFinding, PhiRemediation
from isocenter.remediation import RemediationService
from isocenter.session import DicomSession
from support.ct_small_files import write_ct
from support.project_secret import FIXED_A, load_fixed_secret

POLICIES = {
    "floor": "phi_tags:\n  '0010,0010': {action: REMOVE}\n",
    "none": "privacy_profile: none\nphi_tags:\n  '0010,0010': {action: REMOVE}\n",
    "basic": "privacy_profile: basic\nphi_tags: {}\n",
}
#: The instance copies the export stamps from an owner (`_stamping_owner`).
OWNER_STAMPED = {"0010,0010", "0010,0020", "0020,000d", "0020,000e", "0008,0020"}


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)


def _session(tmp_path, policy):
    write_ct(tmp_path / "src" / "a.dcm", "PID-989", 989, name="Alpha^One")
    cfg = tmp_path / "c.yaml"
    cfg.write_text(POLICIES[policy])
    session = DicomSession(str(tmp_path / "s.db"))
    load_fixed_secret(session, tmp_path, FIXED_A)
    session.load_config(str(cfg))
    session.ingest(str(tmp_path / "src"))
    return session


def _instance(session):
    return session.store.patients[0].studies[0].series[0].instances[0]


@pytest.mark.parametrize("policy", sorted(POLICIES))
def test_instance_findings_handed_twice_leave_the_saved_instance_saved(tmp_path, policy):
    """The instance findings alone: the owners are not handed, so the
    instance ends IDENTIFIED after each call. The second call changes
    nothing and must leave the saved instance saved."""
    with _session(tmp_path, policy) as session:
        report = session.audit()
        findings = [f for f in report.findings if f.entity_type == "Instance"]
        instance = _instance(session)
        session.anonymize(findings)
        session.save(sync=True)
        status = instance.phi_status
        assert status is PhiStatus.IDENTIFIED
        assert not instance.has_unsaved_changes

        session.anonymize(findings)

        assert instance.phi_status is status
        assert not instance.has_unsaved_changes


@pytest.mark.parametrize("start", ["identified", "remediated"])
def test_a_satisfied_entity_ends_where_the_pass_settles_it(tmp_path, start):
    """The end status is the settled one, whichever status the instance
    starts the pass with."""
    with _session(tmp_path, "floor") as session:
        report = session.audit()
        instance = _instance(session)
        handed = (list(report.findings) if start == "remediated"
                  else [f for f in report.findings if f.entity_type == "Instance"])
        session.anonymize(handed)
        assert instance.phi_status is PhiStatus[start.upper()]

        session.anonymize(handed)

        assert instance.phi_status is PhiStatus[start.upper()]


def test_a_direct_apply_single_remediation_still_stamps_a_satisfied_entity():
    """Outside a pass there is no pass end to settle a stamp, so a
    satisfied finding stamps its entity at once, as before."""
    instance = Instance("1.2.3", "1.2.840.10008.5.1.4.1.1.7", 1)
    finding = PhiFinding(
        entity_uid="1.2.3", entity_type="Instance", field_name="0008,0080",
        value=None, reason="test", tag="0008,0080", entity=instance,
        remediation_proposal=PhiRemediation(action_type="REMOVE_TAG",
                                            target_attr="0008,0080"))

    assert RemediationService(project_secret=FIXED_A)._apply_single_remediation(finding) is False

    assert instance.phi_status is PhiStatus.REMEDIATED


def test_the_live_series_arm_still_demotes_an_instance_only_satisfied_this_pass(tmp_path):
    """R2-1's shape: a reopened store, a plain list that holds neither the
    Series' finding nor any owner-stamped copy, so the instance's only
    change this pass is a satisfied finding. The Series still holds its
    source UID, which the floor replaces, so the live arm demotes it."""
    with _session(tmp_path, "floor") as session:
        report = session.audit()
        findings = [f for f in report.findings if f.entity_type == "Instance"
                    and f.tag not in OWNER_STAMPED]
        assert findings
        session.anonymize(findings)
        session.save(sync=True)
    with DicomSession(str(tmp_path / "s.db")) as second:
        second.load_config(str(tmp_path / "c.yaml"))
        instance = _instance(second)
        series = second.store.patients[0].studies[0].series[0]
        assert series.series_instance_uid.startswith("1.2.826.0.1.3680043.10.9999.")

        second.anonymize(findings)

        assert instance.phi_status is PhiStatus.IDENTIFIED
