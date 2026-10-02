"""`redact()` says why it redacted nothing (#807).

Measured on `main` at 7579d4df: a `create_config()` scaffold names each
machine with `redaction_zones: []`, and `redact()` over it printed `No
matching images found for any loaded rules.` -- false, since the rule's
serial matched every image of that machine; what was missing was a
zone. The adjacent case: a loaded configuration that names no machine
printed `No configuration loaded. Use .load_config() first.`, also false
when `load_config()` had just run.

`redact()` now tells the three outcomes that end in no task apart, with
the predicates the pass itself uses (`RedactionService._targets_for`,
`zone_rois`), never a second reading:

* no rules at all -> one sentence true whether or not a file was loaded;
* rules, but no image a rule's serial covers -> the no-match sentence;
* covered images whose rules hold no valid zone -> the count and the
  zones sentence.

Every assertion compares the whole sentence: `"1 instance"` is a prefix
of `"1 instances"`, so a substring check would pass on either plural.
Case (f) is a rule whose only zone is `[1, 2, 3]` (three values), which
`zone_rois` rejects, so it counts as zoneless; it is what fails if the
count reads `redaction_zones` raw instead.
"""
from datetime import date

import numpy as np
import pytest

from isocenter.entities import Equipment, Instance, Patient, Series, Study
from isocenter.session import DicomSession

CT_STORAGE = "1.2.840.10008.5.1.4.1.1.2"

NO_RULES = ("No redaction rules: the configuration names no machines. "
            "Load one with load_config(), or add machines to its rules.")
NO_MATCH = "No image matched any loaded rule's serial_number."


def _zoneless(n):
    noun = "instance" if n == 1 else "instances"
    return (f"{n} {noun} matched rules with no redaction zones; "
            f"nothing to redact.")


def _session(n_instances=1, serial="SN1"):
    session = DicomSession(":memory:")
    patient = Patient("PAT807", "Why^Nothing")
    study = Study("ST807", date(2023, 1, 1))
    series = Series("SE807", "CT", 1)
    series.equipment = Equipment("Maker", "Model", serial)
    for n in range(n_instances):
        inst = Instance(f"1.2.826.0.1.807.{n}", CT_STORAGE, n + 1)
        inst.file_path = None
        inst.set_attr("0008,0060", "CT")
        inst.set_pixel_data(np.full((8, 8), 100, dtype=np.uint16))
        series.instances.append(inst)
    study.series.append(series)
    patient.studies.append(study)
    session.store.patients.append(patient)
    session.save(sync=True)
    return session


def _redact(session, rules, capsys):
    session.configuration.rules = rules
    capsys.readouterr()
    redacted = session.redact(show_progress=False)
    return redacted, capsys.readouterr().out


@pytest.mark.parametrize("n_instances, rules", [
    pytest.param(1, [{"serial_number": "SN1", "redaction_zones": []}],
                 id="a-exact-rule-no-zones-one"),
    pytest.param(2, [{"serial_number": "SN1", "redaction_zones": []}],
                 id="b-exact-rule-no-zones-two"),
    pytest.param(1, [{"serial_number": "*", "redaction_zones": []}],
                 id="d-wildcard-counts-as-a-match"),
    pytest.param(1, [{"serial_number": "SN1", "redaction_zones": [[1, 2, 3]]}],
                 id="f-only-an-invalid-zone"),
])
def test_matched_instances_with_no_zone_are_counted(n_instances, rules, capsys):
    session = _session(n_instances)
    try:
        redacted, out = _redact(session, rules, capsys)
    finally:
        session.close()
    assert redacted == 0
    assert _zoneless(n_instances) in out
    assert NO_MATCH not in out
    assert "No matching images found" not in out


def test_a_rule_no_image_carries_says_nothing_matched(capsys):
    session = _session()
    try:
        redacted, out = _redact(
            session, [{"serial_number": "OTHER",
                       "redaction_zones": [[0, 4, 0, 4]]}], capsys)
    finally:
        session.close()
    assert redacted == 0
    assert NO_MATCH in out
    assert "matched rules with no redaction zones" not in out


def test_a_loaded_configuration_with_no_machines_is_not_called_unloaded(
        tmp_path, capsys):
    """(e): `create_config()` over a cohort with no serial writes no machine."""
    session = _session(serial="")
    try:
        cfg = str(tmp_path / "cfg.yaml")
        session.create_config(cfg)
        session.load_config(cfg)
        assert session.configuration.rules == [], (
            "precondition: the scaffold must name no machine")
        capsys.readouterr()
        redacted = session.redact(show_progress=False)
        out = capsys.readouterr().out
    finally:
        session.close()
    assert redacted == 0
    assert NO_RULES in out
    assert "load_config() first" not in out
