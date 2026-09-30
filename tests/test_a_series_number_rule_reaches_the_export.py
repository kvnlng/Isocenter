"""A rule on Series Number, Instance Number or Modality reaches the export (#869).

`io_handlers.export_stamp_attributes` stamped `(0020,0011)` from
`Series.series_number` and `(0008,0060)` from `Series.modality`, the values
ingest read, over the instance's own elements. `anonymize()` applied a
configuration's REMOVE, REPLACE or EMPTY to the instance's elements, the
instance graded REMEDIATED and the file was stamped `(0012,0062) YES`, while
every file, its `Series_<n>_<modality>_...` folder and its WFDB record name
carried the source values. Measured by the architect on CT_small and
waveform_ecg with SeriesNumber 4242 and InstanceNumber 31337 (spec §1): a
REMOVE on `0020,0011` exported `SeriesNumber '4242'` in `Series_4242_CT_...`;
the WFDB record read `..._4242_31337` under a REMOVE of both numbers.

The export now writes each instance's own elements, what `anonymize()` edits,
as it has written equipment since #570. The folder and the record name are
built from the same attributes after the rules, through one helper
(`io_handlers.exported_number_text`): a removed number reads `0`, an empty one
`NoNumber` in the folder and `0` in the record name (owner rulings Q1-Q5).
Modality (Q6): a REMOVE is honoured, and the file, which is then
non-conformant (Modality is Type 1), is written with one `WARNING` row per
file saying so.

4242 and 31337 occur nowhere else in the fixtures, so a path or a header that
holds either one holds the source value.
"""
import ast
import datetime
import inspect
import os
import re
import textwrap

import pydicom
import pytest
from pydicom.data import get_testdata_file
from pydicom.multival import MultiValue
from pydicom.valuerep import IS, ISfloat

from isocenter import Builder, Session
from isocenter.entities import Patient, Series, Study
from isocenter import io_handlers
from isocenter.io_handlers import DicomExporter, export_stamp_attributes
from isocenter.remediation import RemediationService

OT_STORAGE = "1.2.840.10008.5.1.4.1.1.7"
SERIES = "4242"
INSTANCE = 31337
NON_CONFORMANT = "Modality (0008,0060)"


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


_KEEP = object()


def _write_source(directory, *, name="a.dcm", source="CT_small.dcm",
                  series=SERIES, instance=INSTANCE, modality=_KEEP,
                  sop_suffix="1"):
    """`source` rewritten with the given numbers; None deletes an element."""
    directory.mkdir(parents=True, exist_ok=True)
    ds = pydicom.dcmread(get_testdata_file(source))
    for keyword, value in (("SeriesNumber", series),
                           ("InstanceNumber", instance),
                           ("Modality", modality)):
        if value is _KEEP:
            continue
        if value is None:
            if keyword in ds:
                del ds[keyword]
        else:
            setattr(ds, keyword, value)
    ds.SOPInstanceUID = f"1.2.826.0.1.3680043.10.869.{sop_suffix}"
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    ds.save_as(str(directory / name))
    return directory


def _configured(session, tmp_path, rules):
    config = str(tmp_path / "config.yaml")
    session.create_config(config)
    session.load_config(config)
    for tag, (action, value) in rules.items():
        session.configuration.set_phi_tag(tag, action, value)


def _pipeline(tmp_path, rules, *, fmt="dicom", source_dir=None, **source):
    """ingest -> config -> audit -> anonymize -> export; the output root and
    the WARNING rows' details."""
    source_dir = source_dir or _write_source(tmp_path / "input", **source)
    out = tmp_path / "out"
    options = ({"use_compression": False, "show_progress": False}
               if fmt == "dicom" else {})
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(source_dir))
        _configured(session, tmp_path, rules)
        session.anonymize(session.audit())
        session.export(str(out), format=fmt, **options)
        warnings = [details for _, action, details
                    in session.store_backend.get_audit_errors()
                    if action == "WARNING"]
    return out, warnings


def _dicoms(root):
    return sorted(p for p in root.rglob("*.dcm"))


def _relpaths(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*")
                  if p.is_file())


def _assert_no_source_numbers(root):
    for rel in _relpaths(root):
        assert SERIES not in rel and str(INSTANCE) not in rel, rel


def _series_folder(root):
    [path] = _dicoms(root)
    return path.parent.name


# ---------------------------------------------------------------------------
# T12: the helper's value table
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("attributes, expected", [
    ({}, "0"),
    ({"0020,0011": None}, ""),
    ({"0020,0011": ""}, ""),
    ({"0020,0011": MultiValue(IS, [])}, ""),
    ({"0020,0011": 7}, "7"),
    ({"0020,0011": IS("04")}, "04"),
    ({"0020,0011": IS(" 12")}, "12"),
    ({"0020,0011": "7"}, "7"),
    ({"0020,0011": " 7 "}, "7"),
    ({"0020,0011": -5}, "-5"),
    ({"0020,0011": "-5"}, "-5"),
    ({"0020,0011": 2**31 - 1}, str(2**31 - 1)),
    ({"0020,0011": -2**31}, str(-2**31)),
    ({"0020,0011": 2**31}, "0"),
    ({"0020,0011": str(2**31)}, "0"),
    ({"0020,0011": True}, "0"),
    ({"0020,0011": "ab12cd"}, "0"),
    ({"0020,0011": 1.5}, "0"),
    ({"0020,0011": ISfloat("1.0")}, "0"),
    ({"0020,0011": MultiValue(IS, ["1", "2"])}, "0"),
    ({"0020,0011": b"12"}, "0"),
    ({"0020,0011": "12"}, "12"),
    ({"0020,0011".upper(): "5"}, "5"),
    ({"0020,000E": "5"}, "0"),
], ids=repr)
def test_the_helper_reads_what_the_export_writes(attributes, expected):
    """One row per case of the spec's table (§2.1). The absent row is `0`
    (the owner's "a removed tag contributes 0"), the zero-length rows `""`
    (the callers spell it), IS keeps its text, and anything a name cannot
    carry as a number is `0`. Kills: the absent arm returning `""`; the text
    read through `int`; the garbage arm returning the text."""
    assert io_handlers.exported_number_text(attributes, "0020,0011") == expected


# ---------------------------------------------------------------------------
# T1, T2: the element and the folder follow the rule
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rule, element, folder", [
    (("REMOVE", None), None, "Series_0_"),
    (("REPLACE", "7"), "7", "Series_7_"),
    (("EMPTY", None), "", "Series_NoNumber_"),
    (None, SERIES, f"Series_{SERIES}_"),
], ids=["remove", "replace", "empty", "no-rule"])
def test_the_series_number_element_and_folder_follow_the_rule(
        tmp_path, rule, element, folder):
    """`session.export()` writes the instance's `0020,0011` after the rule,
    and names the folder from it. The no-rule row is the control that stops
    an over-eager fix. Kills: the stamp restored; the folder reading
    `Series.series_number`."""
    rules = {"0020,0011": rule} if rule else {}
    out, _warnings = _pipeline(tmp_path, rules)

    [path] = _dicoms(out)
    written = pydicom.dcmread(str(path))
    if element is None:
        assert "SeriesNumber" not in written
    else:
        assert "SeriesNumber" in written
        assert str(written["SeriesNumber"].value or "") == element
    assert _series_folder(out).startswith(folder), _series_folder(out)
    if rule:
        _assert_no_source_numbers(out)


def test_a_series_whose_sources_disagree_is_named_from_its_first_instance(
        tmp_path):
    """Two instances in one series, SeriesNumber `1` and `2`, no rule: one
    folder, named `Series_1_`, the first instance's, as `se_desc` is read.
    Each file carries its own number. Kills: the folder read from
    `instances[-1]`."""
    source = tmp_path / "input"
    _write_source(source, name="a.dcm", series="1", sop_suffix="1")
    _write_source(source, name="b.dcm", series="2", sop_suffix="2")
    out, _warnings = _pipeline(tmp_path, {}, source_dir=source)

    paths = _dicoms(out)
    assert len(paths) == 2
    assert {p.parent for p in paths} == {paths[0].parent}
    assert paths[0].parent.name.startswith("Series_1_"), paths[0].parent.name
    numbers = sorted(str(pydicom.dcmread(str(p)).SeriesNumber) for p in paths)
    assert numbers == ["1", "2"]


# ---------------------------------------------------------------------------
# T5, T6: no fabricated 0; fresh and reopened agree
# ---------------------------------------------------------------------------

def test_no_source_series_number_writes_no_element_on_either_door(tmp_path):
    """CT_small with SeriesNumber deleted and no rule. Ingest builds the
    Series with `0` and the stamp wrote `SeriesNumber '0'`, a value the
    source never held. Now the element is absent on both doors, and the
    folder is still `Series_0_` (owner ruling Q3). Kills: the stamp
    restored."""
    source = _write_source(tmp_path / "input", series=None)
    via_session, via_tree = tmp_path / "out", tmp_path / "tree"
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(source))
        session.export(str(via_session), use_compression=False,
                       show_progress=False)
        DicomExporter.write_tree(session.store.patients[0], str(via_tree),
                                 show_progress=False)

    for root in (via_session, via_tree):
        [path] = _dicoms(root)
        assert "SeriesNumber" not in pydicom.dcmread(str(path))
        assert path.parent.name.startswith("Series_0_"), path.parent.name


def test_a_fresh_and_a_reopened_export_carry_the_same_text(tmp_path):
    """SeriesNumber `04`. The Series column is INTEGER, so a reopened store
    held `4` and exported `Series_4_...` / `IS '4'` where the fresh one
    wrote `04` (the committed fingerprint recorded both). The instance's
    attribute keeps the text across a reopen, and both exports read it.
    Kills: the folder reading `Series.series_number`; the helper reading
    through `int`."""
    source = _write_source(tmp_path / "input", series="04")
    fresh, reopened = tmp_path / "fresh", tmp_path / "reopened"
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(source))
        session.export(str(fresh), use_compression=False, show_progress=False)
        session.save(sync=True)
    with Session(str(tmp_path / "s.db")) as session:
        assert str(session.store.patients[0].studies[0].series[0]
                   .series_number) == "4", "setup: the column is INTEGER"
        session.export(str(reopened), use_compression=False,
                       show_progress=False)

    for root in (fresh, reopened):
        [path] = _dicoms(root)
        assert path.parent.name.startswith("Series_04_"), path.parent.name
        written = pydicom.dcmread(str(path))
        assert written["SeriesNumber"].value.original_string == "04"
    assert _relpaths(fresh) == _relpaths(reopened)


# ---------------------------------------------------------------------------
# T3, T4: the WFDB record name
# ---------------------------------------------------------------------------

def _records(root):
    return sorted(p.name[:-len(".hea")] for p in root.rglob("*.hea"))


@pytest.mark.parametrize("rules, suffix", [
    ({"0020,0011": ("REMOVE", None), "0020,0013": ("REMOVE", None)}, "_0_0"),
    ({"0020,0011": ("REPLACE", "7"), "0020,0013": ("REPLACE", "9")}, "_7_9"),
    ({"0020,0011": ("EMPTY", None), "0020,0013": ("EMPTY", None)}, "_0_0"),
    ({}, f"_{SERIES}_{INSTANCE}"),
], ids=["remove", "replace", "empty", "no-rule"])
def test_the_wfdb_record_name_follows_both_rules(tmp_path, rules, suffix):
    """The record is `<patient>_<series>_<instance>`, read from the
    instance's attributes after the rules. Neither source number reaches a
    path or a `.hea` under a rule. Kills: the name reading
    `Series.series_number`; the name reading `Instance.instance_number`."""
    out, _warnings = _pipeline(tmp_path, rules, fmt="wfdb",
                               source="waveform_ecg.dcm")

    [record] = _records(out)
    assert record.endswith(suffix), record
    assert re.fullmatch(r".+" + re.escape(suffix), record)
    if rules:
        _assert_no_source_numbers(out)
        for hea in out.rglob("*.hea"):
            text = hea.read_text(encoding="utf-8", errors="replace")
            assert SERIES not in text and str(INSTANCE) not in text


def test_a_series_collapsed_to_zero_writes_every_record(tmp_path):
    """Two waveform instances in one series, both numbers removed: records
    `<pid>_0_0` and `<pid>_0_0_2`, in write order; neither overwrites the
    other (spec §5)."""
    source = tmp_path / "input"
    _write_source(source, name="a.dcm", source="waveform_ecg.dcm",
                  instance=1, sop_suffix="1")
    _write_source(source, name="b.dcm", source="waveform_ecg.dcm",
                  instance=2, sop_suffix="2")
    rules = {"0020,0011": ("REMOVE", None), "0020,0013": ("REMOVE", None)}
    out, _warnings = _pipeline(tmp_path, rules, fmt="wfdb", source_dir=source)

    records = _records(out)
    assert len(records) == 2, records
    base = min(records, key=len)
    assert base.endswith("_0_0"), records
    assert sorted(records) == sorted([base, base + "_2"])


# ---------------------------------------------------------------------------
# T8: the Builder (T9 is in test_both_write_doors_stamp_one_answer.py)
# ---------------------------------------------------------------------------

def _pixels(inst):
    import numpy as np  # pylint: disable=import-outside-toplevel
    for tag, value in (("0028,0002", 1), ("0028,0004", "MONOCHROME2"),
                       ("0008,0020", "20230102")):
        inst.set_attr(tag, value)
    inst.set_pixel_data(np.zeros((4, 4), np.uint8))


def test_the_builder_writes_the_series_number_and_modality(tmp_path):
    """`add_series(uid, "OT", 5).add_instance(...)`: the instance holds
    `0020,0011 == "5"` and `0008,0060 == "OT"`, and `write_tree` writes
    both in `Series_5_OT_...`. A series built with no number writes no
    Series Number attribute. Kills: the builder's writes deleted."""
    series = (Builder.start_patient("PAT869", "Doe^Jane")
              .add_study("1.2.826.0.2.869", "20230102")
              .add_series("1.2.826.0.3.869", "OT", 5))
    inst = series.add_instance("1.2.826.0.1.869.1", OT_STORAGE, 1).instance
    assert inst.attributes["0020,0011"] == "5"
    assert inst.attributes["0008,0060"] == "OT"
    _pixels(inst)
    patient = series.end_series().end_study().build()
    out = tmp_path / "tree"
    DicomExporter.write_tree(patient, str(out), show_progress=False)

    [path] = _dicoms(out)
    written = pydicom.dcmread(str(path))
    assert str(written.SeriesNumber) == "5"
    assert written.Modality == "OT"
    assert path.parent.name.startswith("Series_5_OT_"), path.parent.name

    bare = (Builder.start_patient("PAT870", "Doe^John")
            .add_study("1.2.826.0.2.870", "20230102")
            .add_series("1.2.826.0.3.870", "OT", None))
    lone = bare.add_instance("1.2.826.0.1.870.1", OT_STORAGE, 1).instance
    assert "0020,0011" not in lone.attributes


# ---------------------------------------------------------------------------
# T10: the cascade
# ---------------------------------------------------------------------------

def test_an_edit_of_series_number_does_not_stale_the_instances(tmp_path):
    """`Series.series_number` is the source value and no longer reaches the
    export, so assigning it changes nothing a scan read: the instances keep
    their status (owner ruling Q2). `series_instance_uid`, which the export
    still stamps, still stales them. The series itself is still a change the
    store must hold. Kills: the cascade kept for `series_number`."""
    source = _write_source(tmp_path / "input")
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(source))
        session.anonymize(session.audit())
        series = session.store.patients[0].studies[0].series[0]
        [inst] = series.instances
        status, revision = inst.phi_status, inst._revision
        series_revision = series._revision

        series.series_number = 9

        assert series._revision > series_revision
        assert series.has_unsaved_changes
        assert inst._revision == revision
        assert inst.phi_status is status

        series.series_instance_uid = series.series_instance_uid + ".9"
        assert inst._revision > revision


# ---------------------------------------------------------------------------
# T11: the stamp inventory
# ---------------------------------------------------------------------------

#: Tags the export stamps from an owner that have no `_owner_stamps_copy`
#: arm, each with the reason that is safe.
EXEMPT = {
    "0008,0030": "Study.study_time is never populated by ingest, and no "
                 "inspector raises a finding on it; ENTITY_FIELD_TAGS holds it",
}


def _owner_arm_tags():
    """Every `tag == "gggg,eeee"` comparison in `_owner_stamps_copy`, by
    AST: calling it writes to the instance and re-records its status."""
    source = textwrap.dedent(inspect.getsource(
        RemediationService._owner_stamps_copy))
    tags = set()
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, ast.Compare)
                and isinstance(node.left, ast.Name) and node.left.id == "tag"
                and len(node.ops) == 1 and isinstance(node.ops[0], ast.Eq)
                and isinstance(node.comparators[0], ast.Constant)):
            tags.add(node.comparators[0].value)
    return tags


def test_every_stamped_tag_has_an_owner_arm_or_a_reason():
    """Every tag the export stamps over an instance's own element is either
    one `anonymize()` keeps in step with its owner (`_owner_stamps_copy`) or
    exempted here with a reason. A stamp of any other tag overwrites what a
    rule wrote on the instance: #869 was `0020,0011` and `0008,0060`.
    Kills: either stamp restored."""
    arms = _owner_arm_tags()
    assert {"0010,0010", "0020,000d", "0020,000e"} <= arms, arms

    patient = Patient("PAT", "Doe^Jane")
    study = Study("1.2.3", datetime.date(2003, 3, 6), study_time="120000")
    series = Series("1.2.3.4", "CT", 7)
    stamped = set()
    for attributes in export_stamp_attributes(patient, study, series):
        stamped |= set(attributes)

    assert stamped - arms - set(EXEMPT) == set()
    assert "0020,0011" not in stamped
    assert "0008,0060" not in stamped


# ---------------------------------------------------------------------------
# T13: the grade is true of the file
# ---------------------------------------------------------------------------

def test_a_removed_series_number_grades_pass_over_a_file_without_it(tmp_path):
    """REMOVE on `0020,0011`, full pipeline: the report grades PASS, the
    file carries `(0012,0062) YES`, and it carries no Series Number. Before
    the fix the same run graded PASS over `'4242'`."""
    source = _write_source(tmp_path / "input")
    out, report = tmp_path / "out", tmp_path / "report.md"
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(source))
        _configured(session, tmp_path, {"0020,0011": ("REMOVE", None)})
        session.anonymize(session.audit())
        session.export(str(out), use_compression=False, show_progress=False)
        session.generate_report(str(report))

    assert "**PASS**" in report.read_text(encoding="utf-8")
    [path] = _dicoms(out)
    written = pydicom.dcmread(str(path))
    assert written.PatientIdentityRemoved == "YES"
    assert "SeriesNumber" not in written
    assert SERIES.encode() not in path.read_bytes()


# ---------------------------------------------------------------------------
# Modality (owner ruling Q6)
# ---------------------------------------------------------------------------

def _modality_rows(warnings):
    return [w for w in warnings if NON_CONFORMANT in w]


def test_a_modality_rule_that_replaces_is_honoured(tmp_path):
    """REPLACE `OT` on a CT: the file and its folder carry `OT`, and no
    non-conformance row is written. Kills: the Modality stamp restored; the
    folder reading `Series.modality`."""
    out, warnings = _pipeline(tmp_path, {"0008,0060": ("REPLACE", "OT")})

    [path] = _dicoms(out)
    assert pydicom.dcmread(str(path)).Modality == "OT"
    assert "_OT_" in path.parent.name and "_CT_" not in path.parent.name
    assert _modality_rows(warnings) == []


def test_no_modality_rule_keeps_the_source_value(tmp_path):
    """The control: no rule, the source's `CT` in the file and the folder,
    and no row."""
    out, warnings = _pipeline(tmp_path, {})

    [path] = _dicoms(out)
    assert pydicom.dcmread(str(path)).Modality == "CT"
    assert "_CT_" in path.parent.name
    assert _modality_rows(warnings) == []


def test_a_modality_rule_that_removes_is_honoured_and_reported(tmp_path):
    """REMOVE on a CT's Modality, which is Type 1. The file is written, not
    refused, without the element, and one `WARNING` row for that file says
    it is non-conformant (owner ruling Q6: the configuration decides, the
    library reports the fact). The folder falls back to `OT`, as for a
    series with no modality. Kills: the stamp restored (the element is
    back); the validator's Type 1 refusal restored (no file); the row not
    written."""
    out, warnings = _pipeline(tmp_path, {"0008,0060": ("REMOVE", None)})

    [path] = _dicoms(out)
    written = pydicom.dcmread(str(path))
    assert "Modality" not in written
    assert "_OT_" in path.parent.name and "_CT_" not in path.parent.name
    rows = _modality_rows(warnings)
    assert len(rows) == 1, warnings
    assert "absent" in rows[0] and "Type 1" in rows[0], rows[0]
    assert "not conformant" in rows[0], rows[0]


def test_a_modality_rule_that_empties_is_honoured_and_reported(tmp_path):
    """EMPTY on a CT's Modality: written zero-length, and the same row, for
    an empty Type 1 element is as non-conformant as an absent one (pending
    owner ruling M3). Kills: the row limited to an absent element."""
    out, warnings = _pipeline(tmp_path, {"0008,0060": ("EMPTY", None)})

    [path] = _dicoms(out)
    written = pydicom.dcmread(str(path))
    assert "Modality" in written and written.Modality in (None, "")
    rows = _modality_rows(warnings)
    assert len(rows) == 1, warnings
    assert "empty" in rows[0] and "Type 1" in rows[0], rows[0]


def test_every_file_without_modality_gets_its_own_row(tmp_path):
    """One row per file written without Modality, as every other fact about
    one file's header is recorded (the photometric and ambiguous-VR rows):
    two instances, two rows."""
    source = tmp_path / "input"
    _write_source(source, name="a.dcm", sop_suffix="1")
    _write_source(source, name="b.dcm", sop_suffix="2")
    _out, warnings = _pipeline(tmp_path, {"0008,0060": ("REMOVE", None)},
                               source_dir=source)

    assert len(_modality_rows(warnings)) == 2, warnings


def test_a_source_with_no_modality_is_written_without_one_and_reported(
        tmp_path):
    """CT_small with Modality deleted and no rule. Ingest builds the Series
    with `OT` and the stamp wrote `Modality 'OT'`, a value the source never
    held. Now the file carries none, as for a source with no Series Number
    (Q3's analog), and the non-conformance row is written: the worker
    cannot tell a rule's removal from a source's absence, and the file is
    non-conformant either way. Pending owner ruling M2 on #869, which may
    choose to record at ingest whether the source had Modality and write
    the row only for a rule's removal; this test is its own commit so that
    reversal is cheap. Kills: the stamp restored."""
    out, warnings = _pipeline(tmp_path, {}, modality=None)

    [path] = _dicoms(out)
    assert "Modality" not in pydicom.dcmread(str(path))
    assert "_OT_" in path.parent.name
    rows = _modality_rows(warnings)
    assert len(rows) == 1 and "absent" in rows[0], warnings
