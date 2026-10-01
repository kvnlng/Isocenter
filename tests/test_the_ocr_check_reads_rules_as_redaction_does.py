"""`scan_pixel_content()` reads the rules and zones `redact()` applies (#808, #814).

Measured on `main` at 7579d4df with OCR stubbed to one text region,
"JOHN DOE 12345" at box `(x=10, y=5, w=100, h=20)` (zone space rows
5..25, cols 10..110):

* a `"*"` rule with a covering zone scanned nothing ("No matching
  configured instances found to scan"), though `redact()` and `export()`
  apply it (#808);
* an exact rule whose covering zone is written `{"roi": [...]}` reported
  the text as a NEW_LEAK, because the verifier read only list zones; and
  `auto_remediate_config()` then appended a list copy beside the dict
  (#814);
* an exact rule with a non-covering zone beside a `"*"` rule with a
  covering one reported a NEW_LEAK: the verifier read the first matching
  rule only, while `redact()` applies both.

The scan now selects instances with `services.rules_matching` and reads
zones with `services.zone_rois`, the predicates the redaction pass and the
export's zones already share. Owner rulings: a NEW_LEAK covered only by
`"*"` suggests an exact rule for that machine's serial, never a zone on
`"*"` (Q2-B); instances whose covering rules hold no zone are not scanned,
and the message counts them apart from machines no rule names (Q3-A).

Every "0 findings" assertion is paired with a case on the same fixture
that finds one, and with `attempted == 1`, so none passes because the
scan skipped. OCR is stubbed on the module in threads (`ocr_present`,
`ISOCENTER_FORCE_THREADS`): `monkeypatch` does not cross a process.
"""
from datetime import date

import numpy as np
import pytest

from isocenter import pixel_analysis
from isocenter.entities import Equipment, Instance, Patient, Series, Study
from isocenter.session import DicomSession

SC_SOP_CLASS = "1.2.840.10008.5.1.4.1.1.7"
SERIAL = "SN-808"
MAKER, MODEL = "Acme", "Sono1"
REGION = pixel_analysis.TextRegion("JOHN DOE 12345", (10, 5, 100, 20), 95.0, 0)
COVERING = [0, 30, 0, 200]
ELSEWHERE = [200, 210, 200, 210]
PARTIAL = [0, 30, 0, 40]
#: The zone a NEW_LEAK on REGION suggests: (x, y, w, h) -> [y1, y2, x1, x2].
SUGGESTED = [5, 25, 10, 110]


@pytest.fixture(autouse=True)
def _threads_and_stub_ocr(monkeypatch, ocr_present):
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)
    monkeypatch.setattr(
        pixel_analysis, "_ocr_instance",
        lambda inst: pixel_analysis._InstanceOcr([REGION], True, None))


def _session(tmp_path, serials=(SERIAL,)):
    """One instance per serial, each in its own series; '' is no serial."""
    session = DicomSession(str(tmp_path / "ocr808.db"))
    patient = Patient("P808", "Ocr^Check")
    study = Study("1.2.826.0.1.808", date(2023, 1, 1))
    for n, serial in enumerate(serials):
        series = Series(f"1.2.826.0.1.808.{n}", "US", n + 1)
        series.equipment = Equipment(MAKER, MODEL, serial)
        inst = Instance(f"1.2.826.0.1.808.{n}.1", SC_SOP_CLASS, 1)
        inst.set_pixel_data(np.full((64, 256), 7, dtype=np.uint8))
        series.instances.append(inst)
        study.series.append(series)
    patient.studies.append(study)
    session.store.patients.append(patient)
    session.save(sync=True)
    return session


def _scan(session, rules, capsys=None):
    session.configuration.rules = rules
    if capsys is not None:
        capsys.readouterr()
    report = session.scan_pixel_content()
    out = capsys.readouterr().out if capsys is not None else None
    return list(report), session._pixel_scans[-1], out


def _leaks(findings):
    return [f.metadata["leak_type"] for f in findings]


@pytest.mark.parametrize("rules, leaks", [
    pytest.param([{"serial_number": "*", "redaction_zones": [COVERING]}], [],
                 id="1a-wildcard-covering"),
    pytest.param([{"serial_number": "*", "redaction_zones": [ELSEWHERE]}],
                 ["NEW_LEAK"], id="1b-wildcard-elsewhere"),
    pytest.param([{"serial_number": SERIAL,
                   "redaction_zones": [{"roi": COVERING}]}], [],
                 id="2a-dict-covering"),
    pytest.param([{"serial_number": SERIAL,
                   "redaction_zones": [{"roi": ELSEWHERE}]}], ["NEW_LEAK"],
                 id="2b-dict-elsewhere"),
    pytest.param([{"serial_number": SERIAL, "redaction_zones": [ELSEWHERE]},
                  {"serial_number": "*", "redaction_zones": [COVERING]}], [],
                 id="3-every-matching-rule"),
])
def test_the_scan_reads_the_rules_redaction_applies(tmp_path, rules, leaks):
    session = _session(tmp_path)
    try:
        findings, summary, _ = _scan(session, rules)
    finally:
        session.close()
    assert summary.attempted == 1, "the scan skipped the instance"
    assert _leaks(findings) == leaks


def test_a_partial_leak_on_a_dict_zone_grows_that_zone_in_place(tmp_path):
    """4: `best_zone` is the ROI as a list, and the dict keeps its `note`."""
    session = _session(tmp_path)
    try:
        findings, _, _ = _scan(session, [
            {"serial_number": SERIAL,
             "redaction_zones": [{"roi": PARTIAL, "note": "banner"}]}])
        assert _leaks(findings) == ["PARTIAL_LEAK"]
        assert findings[0].metadata["best_zone"] == PARTIAL
        assert type(findings[0].metadata["best_zone"]) is list
        assert findings[0].metadata["rule_serial"] == SERIAL

        assert session.auto_remediate_config(findings) == 1
        assert session.configuration.rules[0]["redaction_zones"] == [
            {"roi": [0, 30, 0, 110], "note": "banner"}]
    finally:
        session.close()


def test_a_suggested_zone_the_rule_holds_as_a_dict_is_not_added_again(tmp_path):
    """5: ADD_ZONE compares ROIs, so `[..]` beside `{"roi": [..]}` is a duplicate."""
    from isocenter.automation import ConfigAutomator
    session = _session(tmp_path)
    try:
        session.configuration.rules = [
            {"serial_number": SERIAL, "redaction_zones": [{"roi": SUGGESTED}]}]
        changed = ConfigAutomator.apply_suggestions(session, [
            {"serial": SERIAL, "action": "ADD_ZONE", "zone": list(SUGGESTED),
             "reason": "test"}])
        assert changed == 0
        assert session.configuration.rules[0]["redaction_zones"] == [
            {"roi": SUGGESTED}]
    finally:
        session.close()


def test_a_leak_covered_only_by_the_wildcard_suggests_a_rule_for_its_machine(
        tmp_path):
    """Q2-B: an exact rule for the serial, never a zone on `"*"`."""
    session = _session(tmp_path)
    wildcard = {"serial_number": "*", "redaction_zones": [ELSEWHERE]}
    try:
        findings, _, _ = _scan(session, [dict(wildcard,
                                              redaction_zones=[ELSEWHERE])])
        assert _leaks(findings) == ["NEW_LEAK"]
        meta = findings[0].metadata
        assert (meta["machine_serial"], meta["manufacturer"],
                meta["model_name"]) == (SERIAL, MAKER, MODEL)

        assert session.auto_remediate_config(findings) == 1
        assert session.configuration.rules == [
            wildcard,
            {"serial_number": SERIAL, "manufacturer": MAKER,
             "model_name": MODEL, "redaction_zones": [SUGGESTED]}]

        # The created rule is one the loader accepts, and it now covers.
        cfg = str(tmp_path / "cfg.yaml")
        session.configuration.config_path = cfg
        session.configuration.save()
        session.load_config(cfg)
        assert [r["serial_number"] for r in session.configuration.rules] == [
            "*", SERIAL]
        findings, summary, _ = _scan(session, session.configuration.rules)
        assert summary.attempted == 1
        assert findings == []
    finally:
        session.close()


def test_a_partial_leak_on_a_wildcard_zone_suggests_a_rule_and_never_widens_it(
        tmp_path):
    """Owner ruling on #899: the grown zone goes to a rule for the serial.

    Widening the `"*"` zone would redact the grown region on every
    machine's images, for a leak seen on one.
    """
    session = _session(tmp_path)
    wildcard = {"serial_number": "*", "redaction_zones": [list(PARTIAL)]}
    try:
        findings, _, _ = _scan(session, [dict(wildcard,
                                              redaction_zones=[list(PARTIAL)])])
        assert _leaks(findings) == ["PARTIAL_LEAK"]
        assert findings[0].metadata["rule_serial"] == "*"

        assert session.auto_remediate_config(findings) == 1
        assert session.configuration.rules == [
            wildcard,
            {"serial_number": SERIAL, "manufacturer": MAKER,
             "model_name": MODEL, "redaction_zones": [[0, 30, 0, 110]]}]
    finally:
        session.close()


def test_a_new_leak_on_a_machine_with_an_exact_rule_adds_a_zone_to_that_rule(
        tmp_path):
    """`rule_serial` is the exact rule, and the suggestion is ADD_ZONE.

    The end state alone cannot tell this apart from an ADD_RULE that
    falls back to the exact rule, so the finding and the suggestion are
    asserted too (review of #899, M-a).
    """
    from isocenter.automation import ConfigAutomator
    session = _session(tmp_path)
    try:
        findings, _, _ = _scan(session, [
            {"serial_number": SERIAL, "redaction_zones": [{"roi": ELSEWHERE}]},
            {"serial_number": "*", "redaction_zones": [ELSEWHERE]}])
        assert _leaks(findings) == ["NEW_LEAK"]
        assert findings[0].metadata["rule_serial"] == SERIAL
        assert [s["action"] for s in
                ConfigAutomator.suggest_config_updates(findings)] == ["ADD_ZONE"]
    finally:
        session.close()


def test_a_wildcard_partial_leak_on_a_machine_with_its_own_rule_grows_that_rule(
        tmp_path):
    """ADD_RULE never makes a second rule for a serial (review of #899, M-b)."""
    session = _session(tmp_path)
    try:
        findings, _, _ = _scan(session, [
            {"serial_number": SERIAL, "redaction_zones": [list(ELSEWHERE)]},
            {"serial_number": "*", "redaction_zones": [list(PARTIAL)]}])
        assert _leaks(findings) == ["PARTIAL_LEAK"]
        from isocenter.automation import ConfigAutomator
        (suggestion,) = ConfigAutomator.suggest_config_updates(findings)
        # The reason is true of a machine that has its own rule too.
        assert "only the '*' rule covers" not in suggestion["reason"]
        assert f"a rule for serial {SERIAL}" in suggestion["reason"]
        assert session.auto_remediate_config(findings) == 1
        assert session.configuration.rules == [
            {"serial_number": SERIAL,
             "redaction_zones": [ELSEWHERE, [0, 30, 0, 110]]},
            {"serial_number": "*", "redaction_zones": [PARTIAL]}]
    finally:
        session.close()


def test_two_leaks_on_a_wildcard_only_machine_make_one_rule(tmp_path, monkeypatch):
    """The second ADD_RULE adds its zone to the rule the first created."""
    second = pixel_analysis.TextRegion("MRN 998877", (150, 40, 60, 10), 95.0, 0)
    monkeypatch.setattr(
        pixel_analysis, "_ocr_instance",
        lambda inst: pixel_analysis._InstanceOcr([REGION, second], True, None))
    session = _session(tmp_path)
    try:
        findings, _, _ = _scan(session, [
            {"serial_number": "*", "redaction_zones": [list(ELSEWHERE)]}])
        assert _leaks(findings) == ["NEW_LEAK", "NEW_LEAK"]
        assert session.auto_remediate_config(findings) == 2
        assert session.configuration.rules[1:] == [
            {"serial_number": SERIAL, "manufacturer": MAKER,
             "model_name": MODEL,
             "redaction_zones": [SUGGESTED, [40, 50, 150, 210]]}]
    finally:
        session.close()


def test_the_serial_filter_is_applied_before_anything_is_counted(tmp_path, capsys):
    """A series `serial_number=` leaves out is not a skip (review of #899, M-c)."""
    session = _session(tmp_path, serials=(SERIAL, "OTHER"))
    try:
        session.configuration.rules = [
            {"serial_number": SERIAL, "redaction_zones": []}]
        capsys.readouterr()
        session.scan_pixel_content(serial_number=SERIAL)
        out = capsys.readouterr().out
        summary = session._pixel_scans[-1]
    finally:
        session.close()
    assert summary.skipped == 1
    assert ("No matching configured instances found to scan. (Skipped 1 "
            "instance(s) whose rules have no redaction zones)\n") in out


def test_a_series_with_no_serial_is_not_scanned_by_the_wildcard(tmp_path, capsys):
    """6: `rule_applies_to` matches no serial-less series, as `redact()` reads it."""
    session = _session(tmp_path, serials=("",))
    try:
        findings, summary, out = _scan(
            session, [{"serial_number": "*", "redaction_zones": [ELSEWHERE]}],
            capsys)
    finally:
        session.close()
    assert findings == [] and summary.attempted == 0
    assert summary.skipped == 1
    assert ("No matching configured instances found to scan. (Skipped 1 "
            "instance(s) of machines no rule names)") in out


def test_the_message_counts_zoneless_instances_apart(tmp_path, capsys):
    """7: a scaffold (zones `[]`) and a machine no rule names, counted apart."""
    session = _session(tmp_path, serials=(SERIAL, SERIAL + "-B", "OTHER"))
    try:
        findings, summary, out = _scan(session, [
            {"serial_number": SERIAL, "redaction_zones": []},
            {"serial_number": SERIAL + "-B", "redaction_zones": [[1, 2, 3]]}],
            capsys)
    finally:
        session.close()
    assert findings == [] and summary.attempted == 0
    assert summary.skipped == 3
    assert ("No matching configured instances found to scan. (Skipped 1 "
            "instance(s) of machines no rule names; 2 instance(s) whose "
            "rules have no redaction zones)") in out
