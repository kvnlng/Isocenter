"""A machine rule reaches series with no Device Serial Number (#1108, part A).

Measured on `main` at f31758d3 with the acceptance image
(`support.serial_less_burned_cr`: pydicom's serial-less Fuji CR with a
name burned in at a known box), under a configuration whose only rule is
`serial_number: "*"` with a zone exactly over the name:

* `redact()` returned 0 ("No image matched any loaded rule's
  serial_number."), and the export wrote every ink pixel of the name;
* `scan_pixel_content()` scanned nothing and counted the image as
  belonging to a machine no rule names;
* `serial_number: ""` was refused at load as "Missing 'serial_number'".

`services.rule_applies_to` answered False for any series without a
serial, and `MachinePixelIndex` indexed only series with one, so no rule
could ever reach such a series.

Owner ruling of 2026-10-14 ("Both"): `serial_number: ""` selects every
series with no Device Serial Number (an equipment-less series included,
under the index key `""`), and `"*"` now selects every series, serial-less
ones included. Every door reads the one predicate: `redact()`, the
export's zones, `scan_pixel_content()`, `discover_redaction_zones("")`
and `redact_by_machine("", roi)`.

Every "nothing reached" assertion is paired with a case on the same
fixture that reaches, so none passes because the fixture never could.
OCR is stubbed on the module in threads (`ocr_present`): `monkeypatch`
does not cross a process.
"""
import glob
import os
import sqlite3

import pydicom
import pytest

from isocenter import pixel_analysis
from isocenter.config_manager import ConfigLoader
from isocenter.services import (MachinePixelIndex, RedactionService,
                                _redaction_config_hash, rule_applies_to,
                                rules_matching)
from isocenter.privacy import _redaction_uid_for
from isocenter.session import DicomSession
from support.project_secret import FIXED_A, load_fixed_secret
from support.serial_less_burned_cr import (ALONE_SERIAL, BOX, ELSEWHERE,
                                           INK_PIXELS, OVER, SERIAL, ink_left,
                                           source_uid, write_inputs)


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _session(tmp_path, kinds=("none",)):
    session = DicomSession(str(tmp_path / "s1108.db"))
    load_fixed_secret(session)
    session.ingest(write_inputs(str(tmp_path / "input"), kinds))
    return session


def _by_series_uid(session, kind):
    want = source_uid(kind).rsplit(".", 1)[0]
    for patient in session.store.patients:
        for study in patient.studies:
            for series in study.series:
                if series.series_instance_uid == want:
                    return series
    raise AssertionError(f"no {kind} series")


def _exported_ink(session, tmp_path, name="out"):
    """`{kind: ink pixels left}` in the exported files."""
    out = str(tmp_path / name)
    session.export(out, use_compression=False, show_progress=False)
    left = {}
    for path in glob.glob(os.path.join(out, "**", "*.dcm"), recursive=True):
        ds = pydicom.dcmread(path)
        kind = {source_uid(k).rsplit(".", 1)[0]: k
                for k in ("none", "serial", "bare", "alone")}[ds.SeriesInstanceUID]
        left[kind] = ink_left(ds.pixel_array)
    return left


def _rules(serial, zone=OVER):
    return [{"serial_number": serial, "redaction_zones": [list(zone)]}]


def _audit_rows(session, action):
    session.store_backend.flush_audit_queue()
    with sqlite3.connect(session.store_backend.db_path) as conn:
        return conn.execute(
            "SELECT entity_uid, details FROM audit_log WHERE action_type = ?",
            (action,)).fetchall()


# --- The loader ------------------------------------------------------------

def _load(tmp_path, line):
    path = tmp_path / "c.yaml"
    path.write_text(f"machines:\n  - {line}\n    redaction_zones: [[0, 10, 0, 10]]\n")
    return ConfigLoader.load_unified_config(str(path))


def test_an_empty_serial_loads_as_the_serial_less_selector(tmp_path):
    _, rules, _, _, _ = _load(tmp_path, 'serial_number: ""')
    assert rules[0]["serial_number"] == ""


@pytest.mark.parametrize("line, fragment", [
    ("serial_number:", "Missing 'serial_number'"),
    ('serial_number: "  "', "is blank"),
], ids=["null", "blank"])
def test_null_and_blank_are_still_refused_and_name_the_empty_spelling(
        tmp_path, line, fragment):
    with pytest.raises(ValueError) as caught:
        _load(tmp_path, line)
    message = str(caught.value)
    assert fragment in message, message
    assert 'serial_number: ""' in message, message


def test_a_number_is_still_refused(tmp_path):
    with pytest.raises(ValueError, match="must be a string"):
        _load(tmp_path, "serial_number: 0")


def test_add_rule_takes_the_empty_serial(tmp_path):
    session = DicomSession(str(tmp_path / "a.db"))
    try:
        session.configuration.add_rule("", redaction_zones=[list(OVER)])
        assert session.configuration.get_rule("")["redaction_zones"] == [list(OVER)]
    finally:
        session.close()


# --- The predicate and the index --------------------------------------------

def test_the_predicate_reads_empty_and_star_as_the_ruling_says():
    """`""` matches exactly the series with no serial (None is no serial);
    `"*"` matches every series; an exact serial matches only itself."""
    for missing in (None, ""):
        assert rule_applies_to("", missing)
        assert rule_applies_to("*", missing)
        assert not rule_applies_to("SN-1", missing)
    assert rule_applies_to("*", "SN-1")
    assert rule_applies_to("SN-1", "SN-1")
    assert not rule_applies_to("", "SN-1")
    assert not rule_applies_to(None, "SN-1")
    assert not rule_applies_to(None, None)
    assert [r["serial_number"] for r in rules_matching(
        [{"serial_number": s} for s in ("", "*", "SN-1")], None)] == ["", "*"]


def test_the_index_keys_both_kinds_of_serial_less_series_under_empty(tmp_path):
    session = _session(tmp_path, ("none", "serial", "bare"))
    try:
        assert _by_series_uid(session, "bare").equipment is None
        assert _by_series_uid(session, "none").equipment.device_serial_number == ""
        index = MachinePixelIndex()
        index.index_store(session.store)
        assert sorted(index._index) == ["", SERIAL]
        assert len(index.get_by_machine("")) == 2
        assert len(index.get_by_machine(SERIAL)) == 1
    finally:
        session.close()


# --- redact(), the export's zones, and the boundary both ways ---------------

@pytest.mark.parametrize("serial", ["", "*"])
def test_redact_blanks_the_name_on_the_serial_less_cr(tmp_path, serial):
    """The issue's acceptance: a `""` zone and a `"*"` zone each blank it
    through `redact()`, with the attested UID over the rule's serial."""
    session = _session(tmp_path)
    try:
        session.configuration.rules = _rules(serial)
        assert session.redact(show_progress=False) == 1
        inst = _by_series_uid(session, "none").instances[0]
        assert ink_left(inst.get_pixel_data()) == 0
        expected = _redaction_uid_for(
            source_uid("none"),
            _redaction_config_hash([serial], [tuple(OVER)]), FIXED_A)
        assert inst.sop_instance_uid == expected
        assert inst.attributes["0028,0301"] == "NO"
        assert _exported_ink(session, tmp_path) == {"none": 0}
    finally:
        session.close()


@pytest.mark.parametrize("serial", ["", "*"])
def test_the_export_zones_blank_it_without_redact(tmp_path, serial):
    """The export's zones agree with `redact()` (no `redact()` call)."""
    session = _session(tmp_path)
    try:
        session.configuration.rules = []
        assert _exported_ink(session, tmp_path, "plain") == {"none": INK_PIXELS}
        session.configuration.rules = _rules(serial)
        assert _exported_ink(session, tmp_path, "zoned") == {"none": 0}
    finally:
        session.close()


def test_empty_does_not_reach_a_series_with_a_serial_and_star_reaches_all(tmp_path):
    session = _session(tmp_path, ("none", "serial", "bare"))
    try:
        session.configuration.rules = _rules("")
        assert _exported_ink(session, tmp_path, "empty") == {
            "none": 0, "bare": 0, "serial": INK_PIXELS}
        assert session.redact(show_progress=False) == 2
        assert ink_left(_by_series_uid(session, "serial").instances[0]
                        .get_pixel_data()) == INK_PIXELS
        session.configuration.rules = _rules("*")
        # All three: the two the `""` pass redacted are attested under
        # `{"serial": ""}`, another configuration than `{"serial": "*"}`.
        assert session.redact(show_progress=False) == 3
        assert _exported_ink(session, tmp_path, "star") == {
            "none": 0, "bare": 0, "serial": 0}
    finally:
        session.close()


def test_an_exact_serial_does_not_reach_the_serial_less_series(tmp_path):
    session = _session(tmp_path, ("none", "serial"))
    try:
        session.configuration.rules = _rules(SERIAL)
        assert session.redact(show_progress=False) == 1
        assert ink_left(_by_series_uid(session, "none").instances[0]
                        .get_pixel_data()) == INK_PIXELS
    finally:
        session.close()


def test_redact_by_machine_reaches_the_serial_less_series(tmp_path):
    session = _session(tmp_path, ("none", "serial"))
    try:
        session.redact_by_machine("", list(OVER))
        assert ink_left(_by_series_uid(session, "none").instances[0]
                        .get_pixel_data()) == 0
        assert ink_left(_by_series_uid(session, "serial").instances[0]
                        .get_pixel_data()) == INK_PIXELS
        assert session.configuration.rules == []
    finally:
        session.close()


def test_the_redaction_row_names_the_serial_less_selector(tmp_path):
    """One `REDACTION` row per rule-pass; the `""` rule's names `(no serial)`,
    never an empty entity, and `"*"`'s stays `*`."""
    session = _session(tmp_path)
    try:
        session.configuration.rules = _rules("") + _rules("*", ELSEWHERE)
        assert session.redact(show_progress=False) == 1
        rows = sorted(_audit_rows(session, "REDACTION"))
        assert sorted(entity for entity, _ in rows) == ["(no serial)", "*"], rows
        assert all("Applied 1 of 1" in details for _, details in rows), rows
    finally:
        session.close()


def test_a_serial_less_zone_task_is_prepared_for_the_empty_rule(tmp_path):
    """Kills a falsy gate in `prepare_redaction_tasks`."""
    session = _session(tmp_path)
    try:
        service = RedactionService(session.store, session.store_backend)
        tasks = service.prepare_redaction_tasks(
            _rules("")[0], project_secret=FIXED_A)
        assert len(tasks) == 1 and tasks[0]["machine_sn"] == ""
        assert service.prepare_redaction_tasks(
            {"redaction_zones": [list(OVER)]}, project_secret=FIXED_A) == []
    finally:
        session.close()


# --- The OCR side ----------------------------------------------------------

#: The burned name as OCR hands it: (x, y, w, h) over `BOX`.
REGION = pixel_analysis.TextRegion(
    "DOE JANE 1970", (BOX[2], BOX[0], BOX[3] - BOX[2], BOX[1] - BOX[0]), 95.0, 0)


@pytest.fixture
def _stub_ocr(monkeypatch, ocr_present):
    monkeypatch.setattr(
        pixel_analysis, "_ocr_instance",
        lambda inst: pixel_analysis._InstanceOcr([REGION], True, None))


@pytest.mark.parametrize("serial", ["", "*"])
def test_the_scan_reports_the_name_when_no_zone_covers_it(tmp_path, _stub_ocr, serial):
    session = _session(tmp_path)
    try:
        session.configuration.rules = _rules(serial, ELSEWHERE)
        found = list(session.scan_pixel_content())
        assert session._pixel_scans[-1].attempted == 1
        assert [f.metadata["leak_type"] for f in found] == ["NEW_LEAK"]
        session.configuration.rules = _rules(serial, OVER)
        assert list(session.scan_pixel_content()) == []
        assert session._pixel_scans[-1].attempted == 1
    finally:
        session.close()


def test_an_empty_scan_filter_still_means_every_configured_series(tmp_path, _stub_ocr):
    """`scan_pixel_content(serial_number="")` is no filter, as before."""
    session = _session(tmp_path, ("none", "serial"))
    try:
        session.configuration.rules = _rules("*", ELSEWHERE)
        session.scan_pixel_content(serial_number="")
        assert session._pixel_scans[-1].attempted == 2
    finally:
        session.close()


def test_a_leak_under_the_empty_rule_grows_that_rule(tmp_path, _stub_ocr):
    session = _session(tmp_path)
    try:
        session.configuration.rules = _rules("", ELSEWHERE)
        found = list(session.scan_pixel_content())
        assert found[0].metadata["rule_serial"] == ""
        assert found[0].metadata["machine_serial"] == ""
        assert session.auto_remediate_config(found) == 1
        assert session.configuration.rules == [
            {"serial_number": "", "redaction_zones": [list(ELSEWHERE), list(OVER)]}]
    finally:
        session.close()


def test_a_leak_under_star_alone_suggests_an_empty_rule(tmp_path, _stub_ocr):
    """Owner ruling on #1108 Q-B: a serial-less series covered only by
    `"*"` gets an `ADD_RULE` for `serial_number: ""`, never a zone on `"*"`
    (Q2-B), and never nothing."""
    session = _session(tmp_path)
    try:
        session.configuration.rules = _rules("*", ELSEWHERE)
        found = list(session.scan_pixel_content())
        assert found[0].metadata["rule_serial"] == "*"
        assert session.auto_remediate_config(found) == 1
        assert session.configuration.rules[0] == {
            "serial_number": "*", "redaction_zones": [list(ELSEWHERE)]}
        added = session.configuration.rules[1]
        assert added["serial_number"] == ""
        assert added["redaction_zones"] == [list(OVER)]
    finally:
        session.close()


def test_discovery_reaches_an_equipment_less_series(tmp_path):
    """`discover_redaction_zones("")` reached a serial-less series with
    equipment before; now the equipment-less one too, and never a series
    with a serial. Asked of the helper the call samples from."""
    session = _session(tmp_path, ("none", "bare", "serial"))
    try:
        targets = session._discovery_targets("")
        assert sorted(i.sop_instance_uid for i in targets) == sorted(
            [source_uid("none"), source_uid("bare")])
        assert [i.sop_instance_uid for i in session._discovery_targets(SERIAL)] == [
            source_uid("serial")]
    finally:
        session.close()


@pytest.mark.parametrize("serial", ["", "*"])
def test_preview_counts_what_redact_would_reach(tmp_path, capsys, serial):
    """`preview_config()` asks `_targets_for`, the pass's own predicate; a
    lookup of the literal serial previewed `"*"` as matching nothing."""
    session = _session(tmp_path)
    try:
        session.configuration.rules = _rules(serial)
        capsys.readouterr()
        session.preview_config()
        out = capsys.readouterr().out
        assert "Found 1 images in current session." in out, out
        assert "NO MATCH" not in out, out
    finally:
        session.close()



# --- A serial on a file that names no manufacturer and no model -------------
#
# Review of #1111, B1, owner ruling of 2026-10-14. `Equipment.from_parts`
# made a series equipment only when it had a manufacturer or a model, so a
# file carrying Device Serial Number `SN-ALONE` and neither was held with no
# equipment: an exact `SN-ALONE` rule reached nothing (on main too: all
# 9,600 ink pixels exported), and `""` reached a series that has a serial.
# The serial is now equipment by itself, at that one source of truth.

def test_an_exact_rule_reaches_a_serial_with_no_manufacturer_or_model(tmp_path):
    session = _session(tmp_path, ("alone",))
    try:
        equipment = _by_series_uid(session, "alone").equipment
        assert equipment is not None
        assert equipment.device_serial_number == ALONE_SERIAL
        session.configuration.rules = _rules(ALONE_SERIAL)
        assert _exported_ink(session, tmp_path, "zoned") == {"alone": 0}
        assert session.redact(show_progress=False) == 1
        assert ink_left(_by_series_uid(session, "alone").instances[0]
                        .get_pixel_data()) == 0
    finally:
        session.close()


def test_empty_does_not_reach_a_serial_with_no_manufacturer_or_model(tmp_path):
    session = _session(tmp_path, ("alone", "none"))
    try:
        session.configuration.rules = _rules("")
        assert session.redact(show_progress=False) == 1
        assert _exported_ink(session, tmp_path) == {"alone": INK_PIXELS, "none": 0}
        session.configuration.rules = _rules("*")
        assert _exported_ink(session, tmp_path, "star") == {"alone": 0, "none": 0}
    finally:
        session.close()


def test_a_serial_with_no_manufacturer_or_model_survives_a_reopen(tmp_path):
    """The row keeps the serial now (`_upsert_series` writes the
    equipment's fields), and the reload builds equipment from it."""
    db = str(tmp_path / "s1108.db")
    session = _session(tmp_path, ("alone",))
    session.save(sync=True)
    session.close()
    with DicomSession(db) as reopened:
        equipment = _by_series_uid(reopened, "alone").equipment
        assert equipment is not None
        assert equipment.device_serial_number == ALONE_SERIAL
        reopened.configuration.rules = _rules(ALONE_SERIAL)
        assert _exported_ink(reopened, tmp_path, "reopened") == {"alone": 0}


def test_a_store_saved_before_the_fix_reads_such_a_series_as_serial_less(tmp_path):
    """A store an earlier release saved held such a series with no
    equipment, and wrote `""` for all three series columns: the serial is
    not in the row, so a reopen cannot recover it. It reads as a series
    with no serial, reached by `""` and `"*"` and not by its exact serial,
    until its files are ingested into a new store."""
    db = str(tmp_path / "s1108.db")
    session = _session(tmp_path, ("alone",))
    session.save(sync=True)
    session.close()
    with sqlite3.connect(db) as conn:
        changed = conn.execute(
            "UPDATE series SET manufacturer = '', model_name = '', "
            "device_serial_number = '' WHERE series_instance_uid = ?",
            (source_uid("alone").rsplit(".", 1)[0],)).rowcount
    assert changed == 1
    with DicomSession(db) as reopened:
        assert _by_series_uid(reopened, "alone").equipment is None
        reopened.configuration.rules = _rules(ALONE_SERIAL)
        assert _exported_ink(reopened, tmp_path, "exact") == {"alone": INK_PIXELS}
        reopened.configuration.rules = _rules("")
        assert _exported_ink(reopened, tmp_path, "empty") == {"alone": 0}
