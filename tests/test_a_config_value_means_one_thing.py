"""Four values a configuration could spell two ways (#730).

Found in the reviews of #728 and #738, each ruled by the owner on
2026-09-21:

1. `privacy_profile:` with no value (YAML null) means the floor, as an
   absent line does. It read as `none` -- no base at all -- so a bare
   `privacy_profile:` line, a template's blank left unfilled, switched
   off 620 rules.
2. `version` is canonical `MAJOR.MINOR`: `"2.00"` loaded as 2.0 and
   `"02.0"` was refused as a major this library does not read, which is
   the wrong reason. Both are refused as spellings.
3. A blank or whitespace-only `serial_number` is refused, as an empty one
   is: no Device Serial Number is blank, so the rule matched nothing.
4. A phi rule's `value: null` is absent: REPLACE writes its default. Kept
   as it was, and pinned here. The same for `name: null`, which the scan
   read as the name `None` rather than falling back to `Unknown Tag`.

**Why this file imports what it does.** The loader through
`isocenter.config_manager`, `IsocenterConfiguration` through
`isocenter.configuration`, and `PhiInspector` through `isocenter.privacy`.
"""
import copy

import pytest

from isocenter.config_manager import ConfigLoader
from isocenter.configuration import IsocenterConfiguration
from isocenter.entities import (DicomItem, DicomSequence, Instance, Patient,
                                Series, Study)
from isocenter.privacy import PhiInspector
from isocenter.profiles import FLOOR_POLICY
from isocenter.session import DicomSession as Session

from support.project_secret import FIXED_A

INSTITUTION = "0008,0080"
REFERENCED_STUDY = "0008,1110"


def _write(tmp_path, text, name="c.yaml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


@pytest.mark.parametrize("line", ["privacy_profile:\n", "privacy_profile: null\n",
                                  "privacy_profile: ~\n"],
                         ids=["bare", "null", "tilde"])
def test_a_null_privacy_profile_is_the_floor(tmp_path, line):
    """Kills null read as `none` (the policy would be the one tag)."""
    path = _write(tmp_path, line + "phi_tags:\n  '0018,1030': {action: KEEP}\n")
    with Session(str(tmp_path / "s.db")) as session:
        session.load_config(path)
        configuration = session.configuration
        assert configuration.privacy_profile is None
        assert configuration._floor is True  # pylint: disable=protected-access
        expected = copy.deepcopy(FLOOR_POLICY)
        expected["0018,1030"] = {"action": "KEEP"}
        assert configuration.phi_tags == expected


def test_none_still_means_no_base(tmp_path):
    """The other half: `none` is unchanged. Kills the null fix reaching
    `none` too."""
    path = _write(tmp_path, "privacy_profile: none\nphi_tags:\n  '0018,1030': {action: KEEP}\n")
    with Session(str(tmp_path / "s.db")) as session:
        session.load_config(path)
        assert session.configuration.phi_tags == {"0018,1030": {"action": "KEEP"}}
        assert session.configuration._floor is False  # pylint: disable=protected-access


@pytest.mark.parametrize("version", ["2.00", "02.0", "2.01", "002.0"])
def test_a_version_with_a_leading_zero_is_refused(tmp_path, version):
    """Kills the shape check left accepting leading zeros (`2.00` loaded
    as 2.0), and `02.0` refused for its major rather than its spelling."""
    path = _write(tmp_path, f'version: "{version}"\n')
    with pytest.raises(ValueError, match="leading zero") as refused:
        ConfigLoader.load_unified_config(path)
    assert repr(version) in str(refused.value)


@pytest.mark.parametrize("version", ["2.0", "2.10"])
def test_a_canonical_version_loads(tmp_path, version):
    """Kills a canonical check that refuses `0` or a two-digit minor."""
    ConfigLoader.load_unified_config(_write(tmp_path, f'version: "{version}"\n'))


@pytest.mark.parametrize("serial", ["", "   ", "\t"], ids=["empty", "spaces", "tab"])
def test_a_blank_serial_is_refused_by_the_loader(tmp_path, serial):
    """Kills the blank serial loaded (only `""` was refused)."""
    path = _write(tmp_path, f'machines:\n  - serial_number: "{serial}"\n')
    with pytest.raises(ValueError, match="serial_number"):
        ConfigLoader.load_unified_config(path)


def test_a_blank_serial_is_refused_by_add_rule():
    """The same check on the in-code door, before the rule is stored."""
    configuration = IsocenterConfiguration()
    with pytest.raises(ValueError, match="serial_number"):
        configuration.add_rule("  ")
    assert configuration.rules == []


def _patient_with(instance):
    patient = Patient("P730", "Orig^Name")
    study = Study("1.2.826.0.1.730", "20240101")
    series = Series("1.2.826.0.1.730.1", "OT", 1)
    series.instances.append(instance)
    study.series.append(series)
    patient.studies.append(study)
    return patient


def _instance():
    instance = Instance("1.2.826.0.1.730.1.1", "1.2.840.10008.5.1.4.1.1.7", 1)
    instance.set_attr(INSTITUTION, "General Hospital")
    return instance


def _loaded_tags(tmp_path, rule_text):
    path = _write(tmp_path, f"privacy_profile: none\nphi_tags:\n  '{INSTITUTION}': {rule_text}\n")
    tags, _, _, _, _ = ConfigLoader.load_unified_config(path)
    return tags


def test_a_null_value_writes_the_default_replacement(tmp_path):
    """`value: null` is absent: REPLACE writes `ANONYMIZED`. Kills a null
    value refused, or written as the string `None`."""
    tags = _loaded_tags(tmp_path, "{action: REPLACE, value: null}")
    findings = PhiInspector(config_tags=tags, project_secret=FIXED_A).scan_patient(
        _patient_with(_instance()))
    (finding,) = [f for f in findings if f.tag == INSTITUTION]
    assert finding.remediation_proposal.new_value == "ANONYMIZED"


def test_a_null_name_reads_as_absent_at_scan_time(tmp_path):
    """The loader reads `name: null` as absent (#728); the scan read it as
    `None` and named the finding so. Kills `get("name", "Unknown Tag")`
    left at the attribute arm."""
    tags = _loaded_tags(tmp_path, "{action: REMOVE, name: null}")
    findings = PhiInspector(config_tags=tags, project_secret=FIXED_A).scan_patient(
        _patient_with(_instance()))
    (finding,) = [f for f in findings if f.tag == INSTITUTION]
    assert finding.field_name == "Unknown Tag"


def test_a_null_name_on_a_sequence_rule_reads_as_absent(tmp_path):
    """The same on the sequence arm. Kills the fix made at one arm only."""
    path = _write(tmp_path, f"privacy_profile: none\nphi_tags:\n"
                            f"  '{REFERENCED_STUDY}': {{action: REMOVE, name: null}}\n")
    tags, _, _, _, _ = ConfigLoader.load_unified_config(path)
    instance = _instance()
    item = DicomItem()
    item.set_attr("0008,1150", "1.2.840.10008.3.1.2.3.1")
    instance.sequences[REFERENCED_STUDY] = DicomSequence(tag=REFERENCED_STUDY, items=[item])
    findings = PhiInspector(config_tags=tags, project_secret=FIXED_A).scan_patient(
        _patient_with(instance))
    (finding,) = [f for f in findings if f.tag == REFERENCED_STUDY]
    assert finding.field_name == "Unknown Tag"
