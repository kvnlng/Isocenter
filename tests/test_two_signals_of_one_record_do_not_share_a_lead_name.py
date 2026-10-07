"""Two signals of one WFDB record do not share a lead name the table wrote (#832).

Measured on `main` at bb5e6f1f, identically on 3.12 and 3.14t, each file
ingested and exported with `format="wfdb"`:

* a 12-lead ECG coded to PS3.16's 2008 lead table, where `MDC 2:3` is both
  Lead III and Lead V1, wrote `V1` twice, in the `.hea` and in
  `annotations.json`, with no row;
* its limb leads alone (`2:1 2:2 2:3`) wrote `I II V1`: Lead III named V1,
  with no second V1 beside it to notice;
* Lead II coded twice wrote `II II`.

`wfdb.rdrecord(channel_names=["V1"])` over such a record returns one of the
two signals and says nothing.

Owner rulings (2026-10-07, on #832):

* **Q1 A.** An `MDC 2:3` channel is named by its own Code Meaning: `III`
  when it says Lead III, else `V1` (`tests/test_waveform_model.py` holds
  the one-channel half).
* **Q2 A.** When two signals of one record still end up with one name,
  each one the lead table named is written as its Code Value, and the
  export writes one `WARNING` row for the record, naming channel numbers
  and the lead name and nothing from the file. The run grades
  `REVIEW_REQUIRED`.

Every expected list below is a literal. The row is asserted whole.
"""
import json
import os
import sqlite3

import numpy as np
import pytest
import wfdb

from isocenter import Session
from isocenter.exporters.wfdb import format_header
from isocenter.murmur import build_annotations
from isocenter.waveform import Waveform, WaveformChannel

WARNED = ("WARNING", "ERROR", "DATA_LOSS")

ROW_H = ("WFDB record: channels 1 and 2 both resolve to the lead name II, so "
         "each is written as its Channel Source Code Value. A name shared by "
         "two signals cannot say which lead either is.")
ROW_THREE = ("WFDB record: channels 1, 2 and 3 all resolve to the lead name "
             "V6, so each is written as its Channel Source Code Value. A name "
             "shared by more than one signal cannot say which lead each of "
             "them is.")

# (scheme, Code Value, Code Meaning, Channel Label) per channel.
MDC_2008_12_LEAD = [
    ("MDC", "2:1", "Lead I", ""), ("MDC", "2:2", "Lead II", ""),
    ("MDC", "2:3", "Lead III", ""),
    ("MDC", "2:62", "aVR, augmented voltage, right", ""),
    ("MDC", "2:63", "aVL, augmented voltage, left", ""),
    ("MDC", "2:64", "aVF, augmented voltage, foot", ""),
    ("MDC", "2:3", "Lead V1", ""), ("MDC", "2:4", "Lead V2", ""),
    ("MDC", "2:5", "Lead V3", ""), ("MDC", "2:6", "Lead V4", ""),
    ("MDC", "2:7", "Lead V5", ""), ("MDC", "2:8", "Lead V6", "")]
MDC_2008_LIMB = MDC_2008_12_LEAD[:3]
LEAD_II_TWICE = [("MDC", "2:2", "Lead II", ""), ("MDC", "2:2", "Lead II", "")]
MDC_2013 = [("MDC", "2:1", "Lead I", ""), ("MDC", "2:61", "Lead III", ""),
            ("MDC", "2:3", "Lead V1", "")]
PRESSURE_TWICE = [("SRT", "G-DB22", "Aortic pressure waveform", ""),
                  ("SRT", "G-DB22", "Aortic pressure waveform", "")]

TWELVE = ["I", "II", "III", "aVR", "aVL", "aVF",
          "V1", "V2", "V3", "V4", "V5", "V6"]


def _waveform(channels):
    return Waveform(
        sampling_frequency=500, num_channels=len(channels), num_samples=4,
        channels=[WaveformChannel(label=label, source_code=code,
                                  source_scheme=scheme, source_meaning=meaning)
                  for scheme, code, meaning, label in channels])


# -- 1. The rule, over one record's channel list ---------------------------

@pytest.mark.parametrize("channels, descriptions, clashes", [
    pytest.param(MDC_2008_12_LEAD, TWELVE, {}, id="a-2008-12-lead"),
    pytest.param(
        [("MDC", "2:1", "", ""), ("MDC", "2:2", "", ""),
         ("MDC", "2:3", "", ""), ("MDC", "2:3", "", ""),
         ("MDC", "2:4", "", "")],
        ["I", "II", "2:3", "2:3", "V2"], {"V1": [3, 4]},
        id="a-prime-2008-12-lead-meanings-empty"),
    pytest.param(MDC_2008_LIMB, ["I", "II", "III"], {}, id="b-2008-limb"),
    pytest.param(
        [("MDC", "2:1", "Lead I", ""), ("MDC", "2:2", "Lead II", ""),
         ("MDC", "2:3", "Ableitung III", "")],
        ["I", "II", "V1"], {}, id="b-prime-foreign-meaning-is-still-v1"),
    pytest.param(
        [("MDC", " 2:1", "Lead I", ""), ("MDC", "2:2", "Lead II", ""),
         ("MDC", " 2:61", "Lead III", "")],
        ["I", "II", "III"], {}, id="c-padded"),
    pytest.param(MDC_2013, ["I", "III", "V1"], {}, id="d-2013"),
    # Nothing was named, so nothing is claimed: a verbatim Code Value has
    # no other spelling to fall back to.
    pytest.param(PRESSURE_TWICE, ["G-DB22", "G-DB22"], {},
                 id="e-one-non-lead-code-twice"),
    pytest.param(
        [("99LOCAL", "V1", "my V1", ""), ("SCPECG", "5.6.3-9-3", "Lead V1", "")],
        ["V1", "5.6.3-9-3"], {"V1": [2]}, id="f-local-code-spelled-as-a-name"),
    pytest.param(
        [("99LOCAL", "v1", "my V1", ""), ("SCPECG", "5.6.3-9-3", "Lead V1", "")],
        ["v1", "5.6.3-9-3"], {"V1": [2]}, id="f-prime-in-another-case"),
    pytest.param(LEAD_II_TWICE, ["2:2", "2:2"], {"II": [1, 2]},
                 id="h-lead-ii-coded-twice"),
    pytest.param(
        [("MDC", "2:2", "Lead II", ""), ("", "", "", "II")],
        ["2:2", "II"], {"II": [1]}, id="i-coded-ii-beside-a-kept-label-ii"),
    pytest.param(
        [("MDC", "2:61", "Lead III", ""), ("MDC", "2:3", "Lead III", "")],
        ["2:61", "2:3"], {"III": [1, 2]}, id="j-two-codes-one-lead"),
    pytest.param(
        [("MDC", "2:2", "Lead II", ""), ("MDC", "2:3", "Lead V1", ""),
         ("MDC", "2:7", "Lead V5", "")],
        ["II", "V1", "V5"], {}, id="k-2013-strip-with-no-lead-iii"),
    pytest.param(
        [("MDC", "2:4", "", ""), ("MDC", "2:2", "", ""), ("MDC", "2:1", "", ""),
         ("MDC", " 2:2", "", ""), ("MDC", "2:4 ", "", "")],
        ["2:4", "2:2", "I", "2:2", "2:4"], {"V2": [1, 5], "II": [2, 4]},
        id="two-names-clash-and-a-padded-code-falls-back-stripped"),
    pytest.param(
        [("", "", "", ""), ("", "", "", "")], ["ch0", "ch1"], {},
        id="positional-tokens"),
])
def test_the_descriptions_of_one_record(channels, descriptions, clashes):
    from isocenter.exporters.wfdb import _signal_descriptions

    found, found_clashes = _signal_descriptions(_waveform(channels))
    assert found == descriptions
    assert found_clashes == clashes
    # In the order of each name's first channel: the row lists them so.
    assert list(found_clashes) == list(clashes)


def test_the_clash_test_covers_every_signal_line_the_header_writes():
    """A column past the channel definitions takes the last definition
    (`format_header`'s rule), so one `MDC 2:8` under three columns is
    three signals named V6."""
    from isocenter.exporters.wfdb import _signal_descriptions

    waveform = _waveform([("MDC", "2:8", "Lead V6", "")])
    assert _signal_descriptions(waveform) == (["V6"], {})
    assert _signal_descriptions(waveform, 3) == (
        ["2:8", "2:8", "2:8"], {"V6": [1, 2, 3]})
    assert _signal_descriptions(_waveform([]), 2) == (["ch0", "ch1"], {})
    assert _signal_descriptions(_waveform([])) == ([], {})


def test_a_writer_handed_no_list_applies_the_rule_itself():
    """`format_header` and `build_annotations` are called directly too."""
    waveform = _waveform(LEAD_II_TWICE)
    header = format_header("r", waveform, np.zeros((4, 2), dtype=np.int16),
                           "r.dat")
    assert _names(header, 2) == ["2:2", "2:2"]


def test_the_header_writes_the_list_it_is_handed():
    waveform = _waveform(LEAD_II_TWICE)
    header = format_header("r", waveform, np.zeros((4, 2), dtype=np.int16),
                           "r.dat", descriptions=["first", "second"])
    assert _names(header, 2) == ["first", "second"]


# -- The session ----------------------------------------------------------

def _names(header_text, n_signals):
    # The description is the ninth field and runs to the end of the line.
    return [line.split(None, 8)[8]
            for line in header_text.splitlines()[1:1 + n_signals]]


def _dataset(channels, patient_id, definitions=None, marks=None):
    from scripts.generate_waveform_test_data import (add_annotation,
                                                     build_ecg_dataset)
    ds = build_ecg_dataset(
        num_samples=64, patient_id=patient_id,
        channels=[(code, meaning) for _s, code, meaning, _l in channels])
    defs = ds.WaveformSequence[0].ChannelDefinitionSequence
    for chdef, (scheme, _code, _meaning, label) in zip(defs, channels):
        chdef.ChannelSourceSequence[0].CodingSchemeDesignator = scheme
        # The fixture copies the meaning into the label, and a table
        # meaning overflows SH's 16 characters.
        if label:
            chdef.ChannelLabel = label
        else:
            del chdef.ChannelLabel
    if definitions is not None:
        ds.WaveformSequence[0].ChannelDefinitionSequence = list(defs)[:definitions]
    # One mark per defined channel, unless the test names the channels.
    for number in marks or range(1, (definitions or len(channels)) + 1):
        add_annotation(ds, 10 * number, channel=number)
    return ds


def _exported(tmp_path, channels, name, definitions=None, marks=None):
    """Ingest one file and export it as WFDB: `(session, .hea path)`."""
    import pydicom

    source = tmp_path / f"src_{name}"
    source.mkdir()
    pydicom.dcmwrite(str(source / "x.dcm"),
                     _dataset(channels, f"P-{name}", definitions, marks),
                     enforce_file_format=True)
    session = Session(str(tmp_path / f"{name}.db"))
    session.ingest(str(source))
    records = session.export(str(tmp_path / f"out_{name}"), format="wfdb")
    assert len(records) == 1, records
    return session, records[0]


def _rows(session):
    session.store_backend.flush_audit_queue()
    with sqlite3.connect(session.store_backend.db_path) as conn:
        return conn.execute(
            "SELECT action_type, entity_uid, details FROM audit_log "
            "ORDER BY id").fetchall()


def _header_names(hea, n_signals):
    with open(hea, encoding="utf-8") as handle:
        return _names(handle.read(), n_signals)


def _leads(hea):
    with open(os.path.splitext(hea)[0] + ".annotations.json",
              encoding="utf-8") as handle:
        return [finding.get("lead") for finding in json.load(handle)["findings"]]


def _grade(session, tmp_path):
    path = tmp_path / "report.md"
    session.generate_report(str(path))
    line = next(line for line in path.read_text(encoding="utf-8").splitlines()
                if "Validation Status" in line)
    return line.split("**")[3]


# -- 2. What the file says ------------------------------------------------

@pytest.mark.parametrize("channels, names", [
    pytest.param(MDC_2008_12_LEAD, TWELVE, id="a-2008-12-lead"),
    pytest.param(MDC_2008_LIMB, ["I", "II", "III"], id="b-2008-limb"),
    pytest.param(LEAD_II_TWICE, ["2:2", "2:2"], id="h-lead-ii-coded-twice"),
])
def test_an_exported_header_names_each_lead_once_or_by_its_code(
        tmp_path, channels, names):
    session, hea = _exported(tmp_path, channels, "s")
    try:
        assert _header_names(hea, len(channels)) == names
        # 8. PhysioNet's reader takes each description as one field.
        assert wfdb.rdheader(os.path.splitext(hea)[0]).sig_name == names
    finally:
        session.close()


# -- 3. The two files agree ------------------------------------------------

def test_the_annotations_name_a_lead_as_the_header_does(tmp_path):
    session, hea = _exported(tmp_path, LEAD_II_TWICE, "h")
    try:
        names = _header_names(hea, 2)
        assert names == ["2:2", "2:2"]
        assert _leads(hea) == names
    finally:
        session.close()


def test_the_annotations_agree_with_a_header_longer_than_the_definitions(
        tmp_path):
    """One definition (`MDC 2:8`) under three sample columns: the header's
    three names clash, a list of the defined channels alone would not, and
    the mark on channel 1 must say what the header's first line says.

    The marks on channels 2 and 3 name sample columns past the one
    definition. Such a mark carried no lead before #832 and carries none
    now: the group defines no channel 2, whatever the header calls the
    column. The list the exporter hands the bridge is three names long, so
    only the bound on the defined channels in `murmur._lead_for` keeps
    `2:8` off them (review of #832: dropping that bound went unseen)."""
    session, hea = _exported(tmp_path, [("MDC", "2:8", "Lead V6", "")] * 3,
                             "overflow", definitions=1, marks=(1, 2, 3))
    try:
        # 6. The clash test covers what the header writes.
        assert _header_names(hea, 3) == ["2:8", "2:8", "2:8"]
        with open(os.path.splitext(hea)[0] + ".annotations.json",
                  encoding="utf-8") as handle:
            findings = json.load(handle)["findings"]
        assert [f["startSample"] for f in findings] == [9, 19, 29]
        assert [f.get("lead", "<absent>") for f in findings] == [
            "2:8", "<absent>", "<absent>"]
        warned = [row for row in _rows(session) if row[0] in WARNED]
        assert [(kind, details) for kind, _uid, details in warned] == [
            ("WARNING", ROW_THREE)]
    finally:
        session.close()


def test_the_murmur_bridge_reads_the_list_it_is_handed(tmp_path):
    """The exporter computes the names once, over the sample columns, and
    hands both writers that list; the bridge must not recompute its own."""
    session, _hea = _exported(tmp_path, LEAD_II_TWICE, "direct")
    try:
        instance = session.store.patients[0].studies[0].series[0].instances[0]
        waveform = Waveform.from_dicom_item(
            instance.sequences["5400,0100"].items[0])

        own = build_annotations(instance, waveform, "isocenter/test")
        assert [f.get("lead") for f in own["findings"]] == ["2:2", "2:2"]
        handed = build_annotations(instance, waveform, "isocenter/test",
                                   descriptions=["first", "second"])
        assert [f.get("lead") for f in handed["findings"]] == [
            "first", "second"]
    finally:
        session.close()


def test_a_mark_on_a_channel_the_record_has_no_signal_for_carries_no_lead(
        tmp_path):
    """Three definitions over two sample columns (#963, owner ruling): the
    header writes two signal lines, so the record has no third signal, and
    a mark on channel 3 names none. It took the third definition's name
    (measured at bb5e6f1f: `['I', 'II', 'III']`), a lead no signal of the
    record carries. The mark is kept; its `lead` key is absent, which is
    how the bridge writes every mark with no lead (the schema does not
    require the key and the bridge never writes null). The header does not
    move and no row is written: none was ruled."""
    import pydicom

    channels = [("MDC", "2:1", "Lead I", ""), ("MDC", "2:2", "Lead II", ""),
                ("MDC", "2:61", "Lead III", "")]
    ds = _dataset(channels, "P-short")
    group = ds.WaveformSequence[0]
    group.NumberOfWaveformChannels = 2
    group.WaveformData = np.zeros((64, 2), dtype="<i2").tobytes()
    source = tmp_path / "src"
    source.mkdir()
    pydicom.dcmwrite(str(source / "x.dcm"), ds, enforce_file_format=True)
    session = Session(str(tmp_path / "short.db"))
    try:
        session.ingest(str(source))
        records = session.export(str(tmp_path / "out"), format="wfdb")
        with open(records[0], encoding="utf-8") as handle:
            header = handle.read().splitlines()
        # Two signal lines and no third, as before.
        assert header[0].split()[1] == "2"
        assert _header_names(records[0], 2) == ["I", "II"]
        assert [line for line in header[3:] if not line.startswith("#")] == []
        with open(os.path.splitext(records[0])[0] + ".annotations.json",
                  encoding="utf-8") as handle:
            findings = json.load(handle)["findings"]
        assert [f["startSample"] for f in findings] == [9, 19, 29]
        assert [f.get("lead", "<absent>") for f in findings] == [
            "I", "II", "<absent>"]
        assert "lead" not in findings[2]
        assert [row for row in _rows(session) if row[0] in WARNED] == []
    finally:
        session.close()


# -- 4. The row -------------------------------------------------------------

def test_a_record_whose_names_clashed_has_one_warning_row(tmp_path, caplog):
    import logging

    with caplog.at_level(logging.WARNING):
        session, _hea = _exported(tmp_path, LEAD_II_TWICE, "h")
    try:
        uid = session.store.patients[0].studies[0].series[0] \
            .instances[0].sop_instance_uid
        warned = [row for row in _rows(session) if row[0] in WARNED]
        assert warned == [("WARNING", uid, ROW_H)]
        # Channel numbers and the name only: nothing a file can choose.
        assert "2:2" not in ROW_H and "Lead II" not in ROW_H
        assert "|" not in ROW_H and "\n" not in ROW_H
        assert [r.getMessage() for r in caplog.records
                if "WFDB record:" in r.getMessage()] == [f"{uid}: {ROW_H}"]
        # 5. The new row is the run's only one, and it costs the PASS.
        assert _grade(session, tmp_path) == "REVIEW_REQUIRED"
    finally:
        session.close()


@pytest.mark.parametrize("channels, names", [
    pytest.param(MDC_2013, ["I", "III", "V1"], id="d-2013"),
    pytest.param(PRESSURE_TWICE, ["G-DB22", "G-DB22"],
                 id="e-one-non-lead-code-twice"),
    pytest.param(MDC_2008_12_LEAD, TWELVE, id="a-2008-12-lead"),
])
def test_a_record_with_nothing_to_fall_back_from_has_no_row(
        tmp_path, channels, names):
    session, hea = _exported(tmp_path, channels, "quiet")
    try:
        assert _header_names(hea, len(channels)) == names
        assert [row for row in _rows(session) if row[0] in WARNED] == []
        # Measured on main at bb5e6f1f before the fix: PASS.
        assert _grade(session, tmp_path) == "PASS"
    finally:
        session.close()


def test_the_row_names_no_value_from_the_file(tmp_path):
    """A non-conformant source can put anything in a Code Value or a Code
    Meaning; the row holds the table's name and channel numbers."""
    channels = [("MDC", "2:2", "MEANING-OF-JANE-DOE", ""),
                ("MDC", " 2:2", "MEANING-OF-JANE-DOE", "")]
    session, hea = _exported(tmp_path, channels, "novalue")
    try:
        assert _header_names(hea, 2) == ["2:2", "2:2"]
        details = [d for kind, _uid, d in _rows(session) if kind == "WARNING"]
        assert details == [ROW_H]
    finally:
        session.close()


@pytest.mark.parametrize("clashes, detail", [
    ({"II": [1, 2]}, ROW_H),
    ({"V6": [1, 2, 3]}, ROW_THREE),
    ({"V1": [2]},
     "WFDB record: channel 2 resolves to the lead name V1, which another "
     "signal of the record carries without a lead code, so it is written as "
     "its Channel Source Code Value. A name shared by more than one signal "
     "cannot say which lead each of them is."),
    ({"V2": [1, 5], "II": [2, 4, 6, 7]},
     "WFDB record: channels 1 and 5 both resolve to the lead name V2; "
     "channels 2, 4, 6 and 7 all resolve to the lead name II, so each is "
     "written as its Channel Source Code Value. A name shared by more than "
     "one signal cannot say which lead each of them is."),
])
def test_the_row_is_one_sentence_pair_whatever_clashed(clashes, detail):
    from isocenter.exporters.wfdb import _lead_name_clash_detail

    assert _lead_name_clash_detail(clashes) == detail


ROW_TWO_OF_THREE = (
    "WFDB record: channels 1 and 2 both resolve to the lead name V1, so each "
    "is written as its Channel Source Code Value. A name shared by more than "
    "one signal cannot say which lead each of them is.")

TWO_CODED_V1_AND_A_LOCAL_ONE = [
    ("SCPECG", "5.6.3-9-3", "Lead V1", ""), ("MDC", "2:3", "Lead V1", ""),
    ("99LOCAL", "v1", "my v1", "")]


def test_two_signals_is_said_only_when_two_signals_share_the_name():
    """Two coded V1 beside a local code `v1`: two channels fell back and
    three signals carried the name, so "a name shared by two signals" is
    false of it (review of #832). The closing sentence about two is kept
    for the record where the two that fell back are all that shared it."""
    from isocenter.exporters.wfdb import (_lead_name_clash_detail,
                                          _signal_descriptions)

    descriptions, clashes = _signal_descriptions(
        _waveform(TWO_CODED_V1_AND_A_LOCAL_ONE))
    assert (descriptions, clashes) == (["5.6.3-9-3", "2:3", "v1"],
                                       {"V1": [1, 2]})
    assert _lead_name_clash_detail(clashes, descriptions) == ROW_TWO_OF_THREE
    # The control: the same two channels with nothing else of that name.
    assert _lead_name_clash_detail({"II": [1, 2]}, ["2:2", "2:2"]) == ROW_H
    assert _lead_name_clash_detail({"II": [1, 2]}, ["2:2", "2:2", "V1"]) == ROW_H


def test_the_exported_row_says_more_than_one_when_three_signals_share(tmp_path):
    session, hea = _exported(tmp_path, TWO_CODED_V1_AND_A_LOCAL_ONE, "three")
    try:
        assert _header_names(hea, 3) == ["5.6.3-9-3", "2:3", "v1"]
        details = [d for kind, _uid, d in _rows(session) if kind in WARNED]
        assert details == [ROW_TWO_OF_THREE]
    finally:
        session.close()


def test_a_direct_caller_with_no_store_writes_no_row_and_still_falls_back(
        tmp_path):
    """`_write_instance` is called with no store behind it; the names are
    still resolved, and the warning still logged."""
    session, hea = _exported(tmp_path, LEAD_II_TWICE, "nostore")
    try:
        from isocenter.exporters.wfdb import WfdbExporter
        import logging

        patient = session.store.patients[0]
        study = patient.studies[0]
        series = study.series[0]
        records = []

        class _Recorder(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        logger = logging.getLogger("c4-direct")
        logger.addHandler(_Recorder())
        logger.setLevel(logging.WARNING)
        out = tmp_path / "direct"
        path = WfdbExporter()._write_instance(
            str(out), patient, study, series, series.instances[0], logger, {})
        assert _header_names(path, 2) == ["2:2", "2:2"]
        assert records == [f"{series.instances[0].sop_instance_uid}: {ROW_H}"]
        assert len([r for r in _rows(session) if r[0] == "WARNING"]) == 1
    finally:
        session.close()
