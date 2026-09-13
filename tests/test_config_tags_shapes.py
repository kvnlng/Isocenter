"""`config_tags` values: description or rule, never a bare action (#111).

`PhiInspector` accepts two shapes for a tag's value. A dict is a rule
(`{"name": ..., "action": ...}`). Anything else is the tag's *display
name*, and the action is `REPLACE` regardless of what the string says.

That is a coherent design and nothing said so. The type hint reads
`Dict[str, str]`, so the string form looks like the primary shape, and a
caller who writes `{"0008,0020": "SHIFT"}` gets their date replaced with
`ANONYMIZED` rather than shifted -- destroying the interval information
that shifting exists to preserve, silently.

Two things changed that in 0.9.8. Study Date's string form means the
shift (#537), and a string form on a tag whose VR cannot hold
`ANONYMIZED` -- every other date -- is refused at construction (#560). The
warning remains for a text tag, where REPLACE is what happens.
"""
import logging

import pytest

from isocenter.entities import Instance
from isocenter.privacy import PhiInspector


def _instance_with_date():
    inst = Instance("1.2.3.4.5", "1.2.840.10008.5.1.4.1.1.2", 1)
    inst.attributes["0008,0020"] = "20230101"
    inst.attributes["0008,0080"] = "JFK IMAGING CENTER"
    return inst


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records
            if r.levelno >= logging.WARNING]


@pytest.mark.parametrize("action", ["SHIFT", "REMOVE", "EMPTY", "JITTER"])
def test_a_bare_action_name_as_a_value_is_reported(action, caplog):
    """The value is read as a description, so this silently means REPLACE.

    On Institution Name since 0.9.8: Study Date's string form now means
    the shift (#537), so `"SHIFT"` there does what it says; and on a date
    tag a DA cannot hold `ANONYMIZED`, so the string form is refused
    outright (#560)."""
    with caplog.at_level(logging.WARNING):
        PhiInspector(config_tags={"0008,0080": action})

    msgs = _warnings(caplog)
    assert any("0008,0080" in m for m in msgs), msgs
    assert any(action in m for m in msgs), msgs


def test_study_dates_string_form_is_the_shift_and_is_not_reported(caplog):
    """#537, Q3: on Study Date the string form is REPLACE with no value,
    which is the shift, so a bare `SHIFT` there is no gap between what was
    asked and what happens. Kills the warning left unexempted."""
    with caplog.at_level(logging.WARNING):
        inspector = PhiInspector(config_tags={"0008,0020": "SHIFT"})

    assert not _warnings(caplog)
    dated = [f for f in inspector._scan_instance(_instance_with_date(), "P1", None)
             if f.tag == "0008,0020"]
    assert [f.remediation_proposal.action_type for f in dated] == ["SHIFT_DATE"]


def test_a_string_form_on_a_date_tag_is_refused():
    """#560: the string form is REPLACE, and Instance Creation Date is a
    DA, which cannot hold `ANONYMIZED`. It exported the literal before."""
    with pytest.raises(ValueError, match="0008,0012 is DA"):
        PhiInspector(config_tags={"0008,0012": "SHIFT"})


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
    caller may legitimately have a tag described as "Shift". On a text
    tag since 0.9.8, for the reasons above."""
    inspector = PhiInspector(config_tags={"0008,0080": "SHIFT"})

    findings = inspector._scan_instance(_instance_with_date(), "P1", None)
    named = [f for f in findings if f.tag == "0008,0080"]

    assert named
    assert named[0].remediation_proposal.action_type == "REPLACE_TAG"


def test_the_rule_form_still_shifts():
    inspector = PhiInspector(
        config_tags={"0008,0020": {"action": "SHIFT", "name": "Study Date"}})

    findings = inspector._scan_instance(_instance_with_date(), "P1", None)
    dated = [f for f in findings if f.tag == "0008,0020"]

    assert dated
    assert dated[0].remediation_proposal.action_type == "SHIFT_DATE"


def test_a_tag_described_as_replace_is_not_reported(caplog):
    """`REPLACE` is what the string form already does.

    Warning here would advise the caller to write `{"action": "REPLACE"}`
    to obtain the behaviour they are already getting -- true, and no help
    to anyone. The warning exists for the gap between what was asked and
    what happens; for `REPLACE` there is no gap.
    """
    with caplog.at_level(logging.WARNING):
        PhiInspector(config_tags={"0008,0020": "Replace"})

    assert not _warnings(caplog)
