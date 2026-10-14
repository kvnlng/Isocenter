"""A machine rule can select by manufacturer and model: `match:` (#1108, part C).

`manufacturer` and `model_name` on a rule are labels, and stay labels:
`add_rule()` (tier 1, frozen) writes `'Unknown'` for both by default and
`create_config()` writes the equipment's own values, so reading them as
selectors would make every such rule stop matching a machine whose
manufacturer is not literally `Unknown`. A device whose files carry no
Device Serial Number could therefore be named only by `""` (every
serial-less series at once) or `"*"` (every series).

`match: {manufacturer: ..., model_name: ...}` (either key or both) is the
selector instead. A rule has exactly one selector, `serial_number` or
`match`; values compare exactly (case and whitespace significant) against
the series' equipment, whatever its serial, and an equipment-less series
(no manufacturer and no model) is never matched. Applying zones has no
precedence: every rule that covers a series applies, zones unioned (#908).
Where a rule must be chosen -- the rule an OCR leak's suggested zone goes
to -- the owner's ruling on #1108 is narrowest first: an exact serial, then
a `match:` rule (first in rule order), then `""`, then `"*"`.

OCR is stubbed on the module in threads (`ocr_present`).
"""
import glob
import hashlib
import json
import os
import sqlite3

import pydicom
import pytest

from isocenter import pixel_analysis
from isocenter.config_manager import ConfigLoader
from isocenter.entities import Equipment
from isocenter.privacy import _redaction_uid_for
from isocenter.services import _redaction_config_hash, rule_covers
from isocenter.session import DicomSession
from support.project_secret import FIXED_A, load_fixed_secret
from support.serial_less_burned_cr import (BOX, ELSEWHERE, INK_PIXELS,
                                           MANUFACTURER, MODEL, OVER, SERIAL,
                                           ink_left, source_uid, write_inputs)

FUJI = {"manufacturer": MANUFACTURER, "model_name": MODEL}


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _session(tmp_path, kinds=("none",)):
    session = DicomSession(str(tmp_path / "s1108c.db"))
    load_fixed_secret(session)
    session.ingest(write_inputs(str(tmp_path / "input"), kinds))
    return session


def _series(session, kind):
    want = source_uid(kind).rsplit(".", 1)[0]
    for patient in session.store.patients:
        for study in patient.studies:
            for series in study.series:
                if series.series_instance_uid == want:
                    return series
    raise AssertionError(f"no {kind} series")


def _ink(session, kind):
    return ink_left(_series(session, kind).instances[0].get_pixel_data())


def _match_rule(match=None, zone=OVER):
    return {"match": dict(FUJI if match is None else match),
            "redaction_zones": [list(zone)]}


def _serial_rule(serial, zone=OVER):
    return {"serial_number": serial, "redaction_zones": [list(zone)]}


# --- The loader ------------------------------------------------------------

def _load(tmp_path, body):
    path = tmp_path / "c.yaml"
    path.write_text("machines:\n" + body)
    return ConfigLoader.load_unified_config(str(path))


def test_a_match_rule_loads(tmp_path):
    _, rules, _, _, _ = _load(tmp_path, (
        "  - match:\n"
        f"      manufacturer: \"{MANUFACTURER}\"\n"
        f"      model_name: {MODEL}\n"
        "    redaction_zones: [[0, 10, 0, 10]]\n"
        "  - match: {model_name: IRS}\n"
        "    redaction_zones: [[0, 10, 0, 10]]\n"))
    assert rules[0]["match"] == FUJI
    assert rules[1]["match"] == {"model_name": MODEL}
    assert "serial_number" not in rules[0]


@pytest.mark.parametrize("body, fragment", [
    ('  - serial_number: "X"\n    match: {model_name: IRS}\n',
     "exactly one of 'serial_number' and 'match'"),
    ("  - redaction_zones: []\n", "Missing 'serial_number'"),
    ("  - match: IRS\n", "'match' must be a mapping"),
    ("  - match: {}\n", "'match' names no field"),
    ("  - match: {serial: X}\n", "serial"),
    ("  - match: {model_name: 5}\n", "must be a string"),
    ('  - match: {model_name: "  "}\n', "is blank"),
    ("  - match: {model_name: null}\n", "must be a string"),
], ids=["both", "neither", "not-a-mapping", "empty", "unknown-key",
        "not-a-string", "blank", "null"])
def test_a_bad_selector_is_refused_with_a_named_sentence(tmp_path, body, fragment):
    with pytest.raises(ValueError) as caught:
        _load(tmp_path, body)
    assert fragment in str(caught.value), str(caught.value)


def test_neither_selector_names_both_spellings(tmp_path):
    with pytest.raises(ValueError) as caught:
        _load(tmp_path, "  - redaction_zones: []\n")
    assert "match:" in str(caught.value), str(caught.value)


# --- The predicate ---------------------------------------------------------

def test_the_predicate_compares_exactly_and_never_matches_no_equipment():
    fuji = Equipment(MANUFACTURER, MODEL, "")
    assert rule_covers({"match": FUJI}, fuji)
    assert rule_covers({"match": {"model_name": MODEL}}, fuji)
    assert rule_covers({"match": {"manufacturer": MANUFACTURER}},
                       Equipment(MANUFACTURER, "Other", "SN-9"))
    assert not rule_covers({"match": FUJI}, Equipment(MANUFACTURER, "Other", ""))
    assert not rule_covers({"match": {"model_name": MODEL.lower()}}, fuji)
    assert not rule_covers({"match": {"model_name": MODEL + " "}}, fuji)
    assert not rule_covers({"match": {"model_name": MODEL}}, None)
    # The serial arm is `rule_applies_to`'s.
    assert rule_covers({"serial_number": ""}, None)
    assert rule_covers({"serial_number": "*"}, fuji)
    assert not rule_covers({"serial_number": SERIAL}, fuji)
    assert not rule_covers({}, fuji)


# --- Every door reads it ---------------------------------------------------

def test_redact_blanks_the_name_by_manufacturer_and_model(tmp_path):
    """The serial-less Fuji CR and the same device with a serial are
    reached; another model of the same maker and an equipment-less series
    are not. The attested UID is over the match, not over any serial."""
    session = _session(tmp_path, ("none", "serial", "bare"))
    try:
        session.configuration.rules = [_match_rule()]
        assert session.redact(show_progress=False) == 2
        assert _ink(session, "none") == 0
        assert _ink(session, "serial") == 0
        assert _ink(session, "bare") == INK_PIXELS
        expected = _redaction_uid_for(
            source_uid("none"),
            _redaction_config_hash([{"match": FUJI}], [tuple(OVER)]), FIXED_A)
        assert _series(session, "none").instances[0].sop_instance_uid == expected
    finally:
        session.close()


def test_another_model_is_not_reached(tmp_path):
    session = _session(tmp_path)
    try:
        session.configuration.rules = [_match_rule({"model_name": "CR 85"})]
        assert session.redact(show_progress=False) == 0
        assert _ink(session, "none") == INK_PIXELS
    finally:
        session.close()


def test_the_export_zones_read_the_match(tmp_path):
    session = _session(tmp_path, ("none", "bare"))
    try:
        session.configuration.rules = [_match_rule()]
        out = str(tmp_path / "out")
        session.export(out, use_compression=False, show_progress=False)
        left = {}
        for path in glob.glob(os.path.join(out, "**", "*.dcm"), recursive=True):
            ds = pydicom.dcmread(path)
            left[ds.SeriesInstanceUID] = ink_left(ds.pixel_array)
        assert left == {source_uid("none").rsplit(".", 1)[0]: 0,
                        source_uid("bare").rsplit(".", 1)[0]: INK_PIXELS}
    finally:
        session.close()


def test_add_rules_unknown_labels_still_select_nothing(tmp_path):
    """`add_rule()`'s `'Unknown'` labels are labels: a rule for another
    serial written by it reaches nothing here."""
    session = _session(tmp_path)
    try:
        session.configuration.add_rule("SN-OTHER", manufacturer=MANUFACTURER,
                                       model_name=MODEL,
                                       redaction_zones=[list(OVER)])
        assert session.redact(show_progress=False) == 0
        assert _ink(session, "none") == INK_PIXELS
    finally:
        session.close()


def test_the_redaction_row_names_the_match(tmp_path):
    session = _session(tmp_path)
    try:
        session.configuration.rules = [_match_rule()]
        session.redact(show_progress=False)
        session.store_backend.flush_audit_queue()
        with sqlite3.connect(session.store_backend.db_path) as conn:
            rows = conn.execute("SELECT entity_uid FROM audit_log WHERE "
                                "action_type = 'REDACTION'").fetchall()
        assert rows == [(f"match:manufacturer={MANUFACTURER};model_name={MODEL}",)]
    finally:
        session.close()


# --- The attestation hash --------------------------------------------------

def _md5(key):
    return hashlib.md5(json.dumps(key, sort_keys=True).encode("utf-8")).hexdigest()


def test_the_serial_shapes_of_the_hash_do_not_move():
    """`_redaction_config_hash`'s docstring forbids moving the serial
    inputs: every stored attestation and redacted UID derives from them."""
    rois = [(1, 2, 3, 4), (0, 1, 0, 1)]
    assert _redaction_config_hash(["SN"], rois) == _md5(
        {"serial": "SN", "rois": sorted([list(r) for r in rois])})
    assert _redaction_config_hash(["SN", "*", "SN"], rois) == _md5(
        {"serials": ["*", "SN"], "rois": sorted([list(r) for r in rois])})


def test_a_match_hash_is_its_own_shape():
    rois = [(1, 2, 3, 4)]
    assert _redaction_config_hash([{"match": FUJI}], rois) == _md5(
        {"match": FUJI, "rois": [[1, 2, 3, 4]]})
    both = _redaction_config_hash([{"match": FUJI}, "*"], rois)
    assert both == _md5({"selectors": ["*", {"match": FUJI}],
                         "rois": [[1, 2, 3, 4]]})
    assert len({both, _redaction_config_hash(["*"], rois),
                _redaction_config_hash([{"match": FUJI}], rois)}) == 3


def test_a_match_and_a_star_rule_fold_into_one_task(tmp_path):
    """#908 for a match rule: one task, both zones, the hash over both."""
    session = _session(tmp_path)
    try:
        session.configuration.rules = [_match_rule(), _serial_rule("*", ELSEWHERE)]
        assert session.redact(show_progress=False) == 1
        expected = _redaction_uid_for(
            source_uid("none"),
            _redaction_config_hash([{"match": FUJI}, "*"],
                                   [tuple(OVER), tuple(ELSEWHERE)]), FIXED_A)
        assert _series(session, "none").instances[0].sop_instance_uid == expected
        assert _ink(session, "none") == 0
    finally:
        session.close()


# --- The configuration's serial-keyed methods -------------------------------

def test_the_serial_keyed_methods_do_not_see_match_rules(tmp_path):
    session = DicomSession(str(tmp_path / "m.db"))
    try:
        rule = _match_rule()
        session.configuration.rules = [rule]
        assert session.configuration.get_rule(None) is None
        assert session.configuration.delete_rule(None) is False
        assert session.configuration.rules == [rule]
        # A hand-set rule holding serial None is what `delete_rule(None)`
        # finds; the match rule beside it stays (review of #1111, M18).
        held = {"serial_number": None, "redaction_zones": []}
        session.configuration.rules = [rule, held]
        assert session.configuration.delete_rule(None) is True
        assert session.configuration.rules == [rule]
    finally:
        session.close()


def test_save_writes_a_match_rule_that_loads_back(tmp_path):
    session = DicomSession(str(tmp_path / "m.db"))
    try:
        session.configuration.rules = [_match_rule()]
        path = str(tmp_path / "saved.yaml")
        session.configuration.config_path = path
        session.configuration.save()
        _, rules, _, _, _ = ConfigLoader.load_unified_config(path)
        assert rules == [_match_rule()]
    finally:
        session.close()


# --- The OCR side, and which rule a leak's zone goes to ----------------------

REGION = pixel_analysis.TextRegion(
    "DOE JANE 1970", (BOX[2], BOX[0], BOX[3] - BOX[2], BOX[1] - BOX[0]), 95.0, 0)


@pytest.fixture
def _stub_ocr(monkeypatch, ocr_present):
    monkeypatch.setattr(
        pixel_analysis, "_ocr_instance",
        lambda inst: pixel_analysis._InstanceOcr([REGION], True, None))


def test_the_scan_reads_a_match_rule(tmp_path, _stub_ocr):
    session = _session(tmp_path)
    try:
        session.configuration.rules = [_match_rule(zone=ELSEWHERE)]
        found = list(session.scan_pixel_content())
        assert session._pixel_scans[-1].attempted == 1
        assert [f.metadata["leak_type"] for f in found] == ["NEW_LEAK"]
        session.configuration.rules = [_match_rule()]
        assert list(session.scan_pixel_content()) == []
    finally:
        session.close()


def _grown(session, rules):
    """Run the scan and the suggestions over `rules` (each with a zone
    away from the text) and return the index of the rule that took the
    new zone, or `"new"` for an added rule."""
    session.configuration.rules = rules
    found = list(session.scan_pixel_content())
    assert [f.metadata["leak_type"] for f in found] == ["NEW_LEAK"]
    assert session.auto_remediate_config(found) == 1
    grown = [i for i, rule in enumerate(session.configuration.rules)
             if list(OVER) in rule["redaction_zones"]]
    assert len(grown) == 1, session.configuration.rules
    return grown[0] if grown[0] < len(rules) else "new"


@pytest.mark.parametrize("order", [
    ("*", "", "match"), ("match", "", "*"), ("", "*", "match")])
def test_a_serial_less_leak_goes_to_the_match_rule_before_empty_and_star(
        tmp_path, _stub_ocr, order):
    build = {"*": _serial_rule("*", ELSEWHERE), "": _serial_rule("", ELSEWHERE),
             "match": _match_rule(zone=ELSEWHERE)}
    session = _session(tmp_path)
    try:
        assert _grown(session, [build[k] for k in order]) == order.index("match")
    finally:
        session.close()


def test_a_serial_less_leak_goes_to_empty_before_star(tmp_path, _stub_ocr):
    session = _session(tmp_path)
    try:
        assert _grown(session, [_serial_rule("*", ELSEWHERE),
                                _serial_rule("", ELSEWHERE)]) == 1
    finally:
        session.close()


@pytest.mark.parametrize("order", [
    ("*", "match", "exact"), ("match", "exact", "*")])
def test_a_leak_goes_to_the_exact_serial_before_the_match_rule(
        tmp_path, _stub_ocr, order):
    build = {"*": _serial_rule("*", ELSEWHERE),
             "exact": _serial_rule(SERIAL, ELSEWHERE),
             "match": _match_rule(zone=ELSEWHERE)}
    session = _session(tmp_path, ("serial",))
    try:
        assert _grown(session, [build[k] for k in order]) == order.index("exact")
    finally:
        session.close()


def test_a_leak_on_a_serial_device_goes_to_match_before_star(tmp_path, _stub_ocr):
    session = _session(tmp_path, ("serial",))
    try:
        assert _grown(session, [_serial_rule("*", ELSEWHERE),
                                _match_rule(zone=ELSEWHERE)]) == 1
    finally:
        session.close()


def test_a_partial_leak_on_a_match_zone_grows_that_zone(tmp_path, _stub_ocr):
    partial = [BOX[0], BOX[1], BOX[2], BOX[2] + 40]
    session = _session(tmp_path)
    try:
        session.configuration.rules = [_match_rule(zone=partial)]
        found = list(session.scan_pixel_content())
        assert [f.metadata["leak_type"] for f in found] == ["PARTIAL_LEAK"]
        assert found[0].metadata["rule_match"] == FUJI
        assert session.auto_remediate_config(found) == 1
        assert session.configuration.rules[0]["redaction_zones"] == [list(OVER)]
    finally:
        session.close()


def test_the_direct_service_path_finds_a_match_rules_targets(tmp_path):
    """`redact_machine_instances` with no `targets` resolves a match
    selector through the equipment, not as an index key (which raised
    `TypeError: unhashable type: 'dict'`)."""
    from isocenter.services import RedactionService
    session = _session(tmp_path, ("none", "bare"))
    try:
        service = RedactionService(session.store, session.store_backend)
        service.redact_machine_instances({"match": dict(FUJI)}, [tuple(OVER)],
                                         show_progress=False,
                                         project_secret=FIXED_A)
        assert _ink(session, "none") == 0
        assert _ink(session, "bare") == INK_PIXELS
        expected = _redaction_uid_for(
            source_uid("none"),
            _redaction_config_hash([{"match": FUJI}], [tuple(OVER)]), FIXED_A)
        assert _series(session, "none").instances[0].sop_instance_uid == expected
        assert (_series(session, "bare").instances[0].sop_instance_uid
                == source_uid("bare"))
    finally:
        session.close()
