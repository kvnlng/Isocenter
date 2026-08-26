"""`config_tags` values: description or rule, never a bare action (#111).

`PhiInspector` accepts two shapes for a tag's value. A dict is a rule
(`{"name": ..., "action": ...}`). Anything else is the tag's *display
name*, and the action is `REPLACE` regardless of what the string says.

That is a coherent design and nothing said so. The type hint reads
`Dict[str, str]`, so the string form looks like the primary shape, and a
caller who writes `{"0008,0020": "SHIFT"}` gets their date replaced with
`ANONYMIZED` rather than shifted -- destroying the interval information
that shifting exists to preserve, silently.
"""
import logging

import pytest

from isocenter.entities import Instance
from isocenter.privacy import PhiInspector


def _instance_with_date():
    inst = Instance("1.2.3.4.5", "1.2.840.10008.5.1.4.1.1.2", 1)
    inst.attributes["0008,0020"] = "20230101"
    return inst


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records
            if r.levelno >= logging.WARNING]


@pytest.mark.parametrize("action", ["SHIFT", "REMOVE", "EMPTY", "JITTER"])
def test_a_bare_action_name_as_a_value_is_reported(action, caplog):
    """The value is read as a description, so this silently means REPLACE."""
    with caplog.at_level(logging.WARNING):
        PhiInspector(config_tags={"0008,0020": action})

    msgs = _warnings(caplog)
    assert any("0008,0020" in m for m in msgs), msgs
    assert any(action in m for m in msgs), msgs


def test_an_ordinary_description_is_not_reported(caplog):
    """The string form is legitimate and must not be nagged at."""
    with caplog.at_level(logging.WARNING):
        PhiInspector(config_tags={"0008,0020": "Study Date"})

    assert not _warnings(caplog)


def test_the_rule_form_is_not_reported(caplog):
    with caplog.at_level(logging.WARNING):
        PhiInspector(
            config_tags={"0008,0020": {"action": "SHIFT", "name": "Study Date"}})

    assert not _warnings(caplog)


def test_the_string_form_still_means_replace():
    """Documented, not changed: altering it would be breaking, and a
    caller may legitimately have a tag described as "Shift"."""
    inspector = PhiInspector(config_tags={"0008,0020": "SHIFT"})

    findings = inspector._scan_instance(_instance_with_date(), "P1", None)
    dated = [f for f in findings if f.tag == "0008,0020"]

    assert dated
    assert dated[0].remediation_proposal.action_type == "REPLACE_TAG"


def test_the_rule_form_still_shifts():
    inspector = PhiInspector(
        config_tags={"0008,0020": {"action": "SHIFT", "name": "Study Date"}})

    findings = inspector._scan_instance(_instance_with_date(), "P1", None)
    dated = [f for f in findings if f.tag == "0008,0020"]

    assert dated
    assert dated[0].remediation_proposal.action_type == "SHIFT_DATE"
