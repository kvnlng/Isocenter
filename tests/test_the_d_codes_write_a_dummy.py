"""PS3.15's D writes a dummy consistent with the VR (#557).

Measured on 63a64158: `basic` emptied the table's `D` rows and removed
its `X/D` rows, because Isocenter had no dummy-value action. In pydicom's
`test-SR.dcm` twelve D-arm values were written zero-length, among them
Content Date and Time and, inside Verifying Observer Sequence, Verifying
Observer Name, Verifying Organization and Verification DateTime, all
Type 1 (PS3.3 C.17.2); `rtplan.dcm`'s RT Plan Label was emptied the same
way. Table E.1-1a defines D as "replace with a non-zero length value that
may be a dummy value and consistent with the VR".

Now `D` and every code with a D arm map to REPLACE, and REPLACE with no
`value:` writes the dummy of the tag's dictionary VR. The expected values
below are literals, never read from `config_manager.VR_DUMMY`: an
expectation derived from the module under test passes any change to it.
"""
import json
import pathlib
import re
import shutil

import pydicom
import pydicom.data
import pytest
import yaml
from pydicom import config as pydicom_config
from pydicom.valuerep import validate_value

from isocenter.entities import Instance
from isocenter.exporters.wfdb import WfdbExporter
from isocenter.profiles import BASIC_PROFILE
from isocenter.session import DicomSession

from support.annex_e import load_table

#: Literal. Not imported from isocenter.
DUMMY = {"DA": "19000101", "DT": "19000101", "TM": "000000", "AS": "000D",
         "AE": "ANONYMIZED", "CS": "ANONYMIZED", "LO": "ANONYMIZED",
         "LT": "ANONYMIZED", "PN": "ANONYMIZED", "SH": "ANONYMIZED",
         "ST": "ANONYMIZED", "UC": "ANONYMIZED", "UR": "ANONYMIZED",
         "UT": "ANONYMIZED", "OB": b"\x00\x00", "UN": b"\x00\x00"}

ROWS = {row["key"]: row for row in load_table()["rows"]}


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")


def _has_d_arm(key):
    row = ROWS.get(key)
    return row is not None and "D" in row["basic"].split("/")


def _key(tag):
    return f"{tag.group:04x},{tag.element:04x}"


def _walk(ds, path=()):
    """(path, key, element) for every element, at every depth. A path is
    a tuple of (sequence key, item index)."""
    for element in ds:
        yield path, _key(element.tag), element
        if element.VR == "SQ":
            for index, item in enumerate(element.value):
                yield from _walk(item, path + ((_key(element.tag), index),))


def _at(ds, path):
    for key, index in path:
        ds = ds[int(key.replace(",", ""), 16)].value[index]
    return ds


def _config(tmp_path, **data):
    path = tmp_path / "cfg.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return str(path)


def _export(tmp_path, source, profile="basic", phi_tags=None, anonymize=True):
    """Ingest `source`, load `profile`, audit and anonymize, export
    uncompressed; the written dataset and the session's db path."""
    src = tmp_path / "in"
    src.mkdir(exist_ok=True)
    if isinstance(source, str):
        shutil.copy(source, src / "x.dcm")
    else:
        source.save_as(str(src / "x.dcm"))
    config = {"privacy_profile": profile}
    if phi_tags:
        config["phi_tags"] = phi_tags
    db = str(tmp_path / "s.db")
    with DicomSession(db) as session:
        session.ingest(str(src))
        session.load_config(_config(tmp_path, **config))
        if anonymize:
            session.anonymize(session.audit())
        summary = session.export(str(tmp_path / "out"), use_compression=False)
    assert summary.written == 1, summary.failures
    (written,) = list((tmp_path / "out").rglob("*.dcm"))
    return pydicom.dcmread(str(written)), db


SR = pydicom.data.get_testdata_file("test-SR.dcm")

#: The twelve zero-length values of the measurement, by path and VR.
SR_DUMMIED = [
    ((), "0008,0013", "TM"),
    ((), "0008,0023", "DA"),
    ((), "0008,0033", "TM"),
    ((("0040,a073", 0),), "0040,a027", "LO"),
    ((("0040,a073", 0),), "0040,a030", "DT"),
    ((("0040,a073", 0),), "0040,a075", "PN"),
    ((("0040,a073", 1),), "0040,a027", "LO"),
    ((("0040,a073", 1),), "0040,a030", "DT"),
    ((("0040,a073", 1),), "0040,a075", "PN"),
    ((("0040,a730", 3), ("0040,a730", 0)), "0040,a121", "DA"),
    ((("0040,a730", 3), ("0040,a730", 1)), "0040,a122", "TM"),
    ((("0040,a730", 3), ("0040,a730", 2)), "0040,a120", "DT"),
]

#: The X/D values the measurement found removed.
SR_WERE_REMOVED = [
    ((), "0008,0012", "DA"),
    ((), "0040,a032", "DT"),
    ((("0040,a730", 4),), "0040,a032", "DT"),
    ((("0040,a730", 4), ("0040,a730", 1)), "0040,a032", "DT"),
]


def _d_arm_values(ds):
    """{(path, key): value} for every non-sequence D-arm element present
    and non-empty, Patient ID aside (owned: the keyed pseudonym)."""
    return {(path, key): element.value for path, key, element in _walk(ds)
            if _has_d_arm(key) and element.VR != "SQ" and key != "0010,0020"
            and element.value not in (None, "", b"")}


def test_a_structured_report_exports_its_type_1_dates_and_names_filled(tmp_path):
    """Kills the mapping left at EMPTY or REMOVE; a D arm missed; and the
    nested walk not reaching the content items."""
    source = pydicom.dcmread(SR)
    out, _ = _export(tmp_path, SR)

    for path, key, vr in SR_DUMMIED + SR_WERE_REMOVED:
        element = _at(out, path)[int(key.replace(",", ""), 16)]
        assert element.VR == vr, (path, key, element.VR)
        assert element.value == DUMMY[vr], (path, key, element.value)

    # The walk: every D-arm element the source carried is in the export,
    # holding its VR's dummy. None is zero-length and none is absent.
    carried = _d_arm_values(source)
    assert len(carried) == 16, sorted(carried)
    for (path, key), _value in carried.items():
        item = _at(out, path)
        tag = int(key.replace(",", ""), 16)
        assert tag in item, f"{key} at {path} was removed"
        assert item[tag].value == DUMMY[item[tag].VR], (path, key, item[tag].value)


def test_the_dummy_is_computed_in_the_worker(tmp_path, monkeypatch):
    """The dummy is built by the scan, in whichever worker runs it, and
    travels on the finding. Kills the dummy computed in the parent from
    state a process worker lacks, or not pickled with the finding."""
    exports = {}
    for strategy in ("processes", "threads"):
        if strategy == "processes":
            monkeypatch.delenv("ISOCENTER_FORCE_THREADS", raising=False)
            monkeypatch.setenv("ISOCENTER_FORCE_PROCESSES", "1")
        else:
            monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
            monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
        root = tmp_path / strategy
        root.mkdir()
        out, _ = _export(root, SR)
        exports[strategy] = {k: v for k, v in _d_arm_values(out).items()}
    assert len(exports["processes"]) == 16
    assert exports["processes"] == exports["threads"]


#: One D-arm tag per VR the table gives D. (tag, VR, source value)
PER_VR = [
    ("0008,0023", "DA", "20200101"),
    ("0018,9074", "DT", "20010213184746+0100"),
    ("0008,0033", "TM", "113008"),
    ("0072,005f", "AS", "045Y"),
    ("0072,005e", "AE", "STATION1"),
    ("0400,0565", "CS", "CORRECT"),
    ("0040,a027", "LO", "Hospital of Jane Doe"),
    ("0072,0068", "LT", "Long text about Jane Doe"),
    ("0040,a075", "PN", "Doe^Jane^^Dr=Doe^Jane=Doe^Jane"),
    ("300a,0002", "SH", "PLAN JD"),
    ("300a,0734", "ST", "Treatment for Jane Doe"),
    ("0018,9367", "UC", "Unlimited characters Jane Doe"),
    ("0072,0071", "UR", "http://example.org/jane-doe"),
    ("0072,0070", "UT", "Unlimited text Jane Doe"),
    ("0072,0065", "OB", b"JANE DOE"),
    ("0072,006d", "UN", b"JANE"),
]


@pytest.mark.parametrize("tag,vr,value", PER_VR, ids=[vr for _, vr, _ in PER_VR])
def test_every_vr_the_table_gives_d_has_its_dummy(tmp_path, tag, vr, value):
    """The value pydicom reads back from the written file is the literal
    dummy and passes its VR's validator. Kills a VR missing from the
    table (refused, or `ANONYMIZED` written into a DA), a binary dummy of
    odd length, and a dummy that fails its VR."""
    assert _has_d_arm(tag) and BASIC_PROFILE[tag] == {
        "action": "REPLACE", "name": BASIC_PROFILE[tag]["name"]}
    ds = pydicom.dcmread(pydicom.data.get_testdata_file("CT_small.dcm"))
    ds.add_new(int(tag.replace(",", ""), 16), vr, value)
    out, _ = _export(tmp_path, ds)

    element = out[int(tag.replace(",", ""), 16)]
    assert element.VR == vr
    assert element.value == DUMMY[vr], element.value
    validate_value(vr, element.value, pydicom_config.RAISE)
    if isinstance(element.value, bytes):
        assert len(element.value) % 2 == 0


DUMMIED_ON_CT = ["0008,0012", "0008,0013", "0008,0021", "0008,0023", "0008,0031",
                 "0008,0033", "0008,0080", "0008,1010", "0018,0010", "0072,0065"]


def test_a_dummied_element_reads_clear_on_a_second_audit(tmp_path):
    """Kills the scan comparing with `ANONYMIZED` instead of the dummy
    (every pass rewrites the element and the instance never settles), and
    bytes stored and reloaded as `str`."""
    ds = pydicom.dcmread(pydicom.data.get_testdata_file("CT_small.dcm"))
    ds.add_new(0x00720065, "OB", b"JANE DOE")
    src = tmp_path / "in"
    src.mkdir()
    ds.save_as(str(src / "ct.dcm"))
    config = _config(tmp_path, privacy_profile="basic")
    db = str(tmp_path / "s.db")

    with DicomSession(db) as session:
        session.ingest(str(src))
        session.load_config(config)
        first = session.audit()
        assert {f.tag for f in first} >= set(DUMMIED_ON_CT)
        session.anonymize(first)
        (instance,) = session.store.patients[0].studies[0].series[0].instances
        assert instance.attributes["0008,0021"] == "19000101"
        assert instance.attributes["0072,0065"] == b"\x00\x00"
        again = [f.tag for f in session.audit() if f.tag in DUMMIED_ON_CT]
        assert again == []
        session.save(sync=True)

    with DicomSession(db) as session:
        session.load_config(config)
        (instance,) = session.store.patients[0].studies[0].series[0].instances
        assert instance.attributes["0072,0065"] == b"\x00\x00"
        assert instance.attributes["0008,0013"] == "000000"
        again = [f.tag for f in session.audit() if f.tag in DUMMIED_ON_CT]
        assert again == []


def test_an_empty_source_value_is_not_given_a_dummy(tmp_path):
    """Kills the REPLACE arm's blank check missing `b""` (the bytes are
    raised again for ever), and a dummy written where no value was."""
    ds = pydicom.dcmread(pydicom.data.get_testdata_file("CT_small.dcm"))
    ds.add_new(0x00181030, "LO", "")             # Protocol Name (X/D)
    ds.add_new(0x00720065, "OB", b"")            # D, binary
    src = tmp_path / "in"
    src.mkdir()
    ds.save_as(str(src / "ct.dcm"))
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(str(src))
        session.load_config(_config(tmp_path, privacy_profile="basic"))
        report = session.audit()
        assert not [f for f in report if f.tag in ("0018,1030", "0072,0065")]
        session.anonymize(report)
        session.export(str(tmp_path / "out"), use_compression=False)
    (written,) = list((tmp_path / "out").rglob("*.dcm"))
    out = pydicom.dcmread(str(written))
    assert out[0x00181030].value in ("", None)
    assert out[0x00720065].value in (b"", None)


def test_a_multi_valued_d_element_becomes_one_dummy(tmp_path):
    """Kills the dummy written once per value, and the list kept."""
    ds = pydicom.dcmread(pydicom.data.get_testdata_file("CT_small.dcm"))
    ds.add_new(0x00720061, "DA", ["20200101", "20200202"])
    out, _ = _export(tmp_path, ds)
    assert out[0x00720061].VM == 1
    assert out[0x00720061].value == "19000101"


def test_the_sequence_rows_keep_their_actions(tmp_path):
    """The four D-arm sequences are named departures. Kills one dropped:
    its row maps to REPLACE, which the scan warns has no meaning on a
    sequence and does not apply, so the items stay."""
    assert {key: BASIC_PROFILE[key]["action"] for key in (
        "0008,0082", "0008,1111", "0008,1072", "0040,1101")} == {
            "0008,0082": "EMPTY", "0008,1111": "EMPTY",
            "0008,1072": "REMOVE", "0040,1101": "EMPTY"}

    ds = pydicom.dcmread(pydicom.data.get_testdata_file("CT_small.dcm"))
    code = pydicom.Dataset()
    code.CodeValue, code.CodingSchemeDesignator, code.CodeMeaning = (
        "JD1", "99LOCAL", "Jane Doe Imaging")
    for tag in (0x00080082, 0x00081072, 0x00401101):
        ds.add_new(tag, "SQ", pydicom.Sequence([pydicom.Dataset(code)]))
    reference = pydicom.Dataset()
    reference.ReferencedSOPClassUID = "1.2.840.10008.3.1.2.3.3"
    reference.ReferencedSOPInstanceUID = "1.2.826.0.1.557.1"
    ds.add_new(0x00081111, "SQ", pydicom.Sequence([reference]))

    out, _ = _export(tmp_path, ds)
    for tag in (0x00080082, 0x00081111, 0x00401101):
        assert tag in out and len(out[tag].value) == 0, f"{tag:08x}"
    assert 0x00081072 not in out


def test_patient_id_is_still_the_pseudonym(tmp_path):
    """Patient ID's departure is gone: its Z/D maps to REPLACE, and a
    value-less REPLACE on Patient ID is the keyed pseudonym. Kills the
    owned-tag reading lost (the ID would read `ANONYMIZED`)."""
    out, _ = _export(tmp_path, pydicom.data.get_testdata_file("CT_small.dcm"))
    assert re.fullmatch(r"ANON_[0-9a-f]{24}", out.PatientID), out.PatientID
    assert out.PatientName == "ANONYMIZED"


@pytest.mark.parametrize("profile,phi_tags", [
    ("none", {"0008,0020": "Study Date"}),
    (None, None),
], ids=["string-form-under-none", "the-floor"])
def test_study_dates_string_form_still_shifts(tmp_path, profile, phi_tags):
    """Study Date's value-less REPLACE is the shift (#537). Kills the DA
    dummy computed where the rule is parsed, ahead of the Study Date
    override that changes only the action: a tidy-up reordering the two
    would write `19000101` over the shift."""
    source = pydicom.dcmread(pydicom.data.get_testdata_file("CT_small.dcm"))
    # A Study Date nested in an item: the owner's scan never reaches it,
    # so the instance scan's own reading of the rule (the shift) is the
    # only thing between it and the DA dummy.
    nested = pydicom.Dataset()
    nested.StudyDate = source.StudyDate
    source.add_new(0x00081250, "SQ", pydicom.Sequence([nested]))  # Related Series Sequence
    src = tmp_path / "in"
    src.mkdir()
    source.save_as(str(src / "ct.dcm"))
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(str(src))
        if profile is not None:
            session.load_config(_config(tmp_path, privacy_profile=profile,
                                        phi_tags=phi_tags))
        session.anonymize(session.audit())
        session.export(str(tmp_path / "out"), use_compression=False)
    (written,) = list((tmp_path / "out").rglob("*.dcm"))
    out = pydicom.dcmread(str(written))
    assert out.StudyDate not in ("", None, source.StudyDate, "19000101"), out.StudyDate
    inner = out[0x00081250].value[0].StudyDate
    assert inner not in ("", None, source.StudyDate, "19000101"), inner


def test_no_rule_0_9_8_loaded_writes_something_else():
    """Pillar 3, executable: over the whole standard dictionary, a
    value-less REPLACE writes something other than `ANONYMIZED` only on a
    tag where 0.9.8 refused it, because `ANONYMIZED` did not fit its VR.
    So every rule of that shape a 0.9.8 file loaded writes what it wrote,
    with one exception, named: `0072,006d` (Selector UN Value), the one
    dictionary tag whose VR is UN. pydicom's validator lets any value into
    a UN, so 0.9.8 loaded a value-less REPLACE there and wrote
    `ANONYMIZED`; the UN dummy is two zero bytes (owner ruling Q4 on
    #557), and the CHANGELOG says so. Kills a dummy given to another VR
    that holds `ANONYMIZED` (a CS dummy of `NONE`, say), which would
    change the output of a config that loads today."""
    from pydicom.datadict import DicomDictionary  # pylint: disable=import-outside-toplevel

    from isocenter.config_manager import (  # pylint: disable=import-outside-toplevel
        _dictionary_vr_refuses, _vr_dummy, validate_phi_policy)

    changed, loaded_before = [], []
    for number in DicomDictionary:
        tag = f"{number >> 16:04x},{number & 0xFFFF:04x}"
        written = _vr_dummy(tag)
        if written is None or written == "ANONYMIZED":
            continue
        changed.append(tag)
        # 0.9.8's test, unchanged: would `ANONYMIZED` have been refused?
        if _dictionary_vr_refuses(tag, "ANONYMIZED") is None:
            loaded_before.append(tag)
        # It loads now.
        validate_phi_policy({tag: {"action": "REPLACE"}}, "cfg.yaml")
    assert loaded_before == ["0072,006d"]
    # 273 with pydicom 3.0.2; a floor, so a dictionary refresh does not
    # turn this red, and a loop that checked nothing does.
    assert len(changed) > 200, len(changed)


# --- WFDB: a dummy date is never read as timing (#59) ----------------------

def _ecg(tmp_path):
    from scripts.generate_waveform_test_data import build_ecg_dataset  # pylint: disable=import-outside-toplevel

    ds = build_ecg_dataset(channels=[("MDC_ECG_LEAD_I", "Lead I")],
                           patient_id="DUMMY557", patient_name="Waveform^Test")
    ds.StudyDate = "20230417"
    ds.AcquisitionDateTime = "20230417143005"
    for keyword in ("StudyTime", "ContentTime", "AcquisitionTime"):
        if keyword in ds:
            delattr(ds, keyword)
    src = tmp_path / "in"
    src.mkdir()
    pydicom.dcmwrite(str(src / "wf.dcm"), ds, write_like_original=False)
    return src


def _record_line(header):
    return next(line for line in header.splitlines()
                if line.strip() and not line.startswith("#"))


@pytest.mark.parametrize("profile", ["basic", None], ids=["basic", "floor"])
def test_a_dummy_datetime_is_not_read_as_timing(tmp_path, profile):
    """Acquisition DateTime is X/Z/D, so it now holds `19000101`, which
    `_parse_dicom_dt` reads as 1900-01-01 00:00. Kills the exporter
    pairing that invented `00:00:00` with the study's date (#59 reopened);
    under the floor, whose Study Date is shifted rather than emptied, it
    would be the time of day."""
    src = _ecg(tmp_path)
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(str(src))
        if profile is not None:
            session.load_config(_config(tmp_path, privacy_profile=profile))
        session.anonymize(session.audit())
        (instance,) = session.store.patients[0].studies[0].series[0].instances
        assert instance.attributes["0008,002a"] == "19000101"
        paths = session.export(str(tmp_path / "out"), format="wfdb")
    header = pathlib.Path(paths[0]).read_text(encoding="utf-8")
    fields = _record_line(header).split()
    # name, signals, frequency, samples: no base_time, no base_date.
    assert len(fields) == 4, header
    assert "00:00:00" not in header and "1900" not in header, header


def test_the_instance_timing_readers_skip_a_dummy():
    """Kills the guard missing from either reader."""
    instance = Instance("1.2.3", "1.2.840.10008.5.1.4.1.1.9.1.1", 1)
    instance.attributes["0008,002a"] = "19000101"

    assert WfdbExporter._instance_time_of_day(instance) is None
    assert WfdbExporter._instance_only_datetime(instance) == (None, None)

    instance.attributes["0008,002a"] = "20230417143005"
    assert WfdbExporter._instance_time_of_day(instance).hour == 14
    assert WfdbExporter._instance_only_datetime(instance)[0].year == 2023


def _annotated_ecg(tmp_path, note):
    from scripts.generate_waveform_test_data import (  # pylint: disable=import-outside-toplevel
        add_annotation, build_ecg_dataset)

    ds = build_ecg_dataset(num_samples=500, patient_id="NOTE557",
                           patient_name="Waveform^Test")
    add_annotation(ds, start_sample=101, text=note)
    src = tmp_path / "in"
    src.mkdir()
    pydicom.dcmwrite(str(src / "wf.dcm"), ds, enforce_file_format=True)
    return src


@pytest.mark.parametrize("profile", ["basic", None], ids=["basic", "floor"])
def test_a_dummy_annotation_text_is_not_written_as_a_note(tmp_path, profile):
    """Unformatted Text Value (0070,0006) is D, so it now holds the text
    dummy; opting in to annotation text must not turn that into a Murmur
    `note` on every finding. 0.9.8 emptied it and wrote no note, and the
    owner's ruling of 2026-09-22 keeps that output (review of #557)."""
    src = _annotated_ecg(tmp_path, "Reviewed by Dr Jane Doe")
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(str(src))
        if profile is not None:
            session.load_config(_config(tmp_path, privacy_profile=profile))
        session.anonymize(session.audit())
        (instance,) = session.store.patients[0].studies[0].series[0].instances
        (item,) = instance.sequences["0040,b020"].items
        assert item.attributes["0070,0006"] == DUMMY["UT"]
        session.export(str(tmp_path / "out"), format="wfdb",
                       include_annotation_text=True)
    (path,) = (tmp_path / "out").rglob("*.annotations.json")
    findings = json.loads(path.read_text(encoding="utf-8"))["findings"]
    assert len(findings) == 1, findings
    assert "note" not in findings[0], findings
    assert "ANONYMIZED" not in path.read_text(encoding="utf-8")


def test_the_note_reader_skips_only_the_dummy():
    """Kills the guard removed, or compared with anything but the dummy."""
    from isocenter.murmur import _real_note  # pylint: disable=import-outside-toplevel

    item = Instance("1.2.3", "1.2.840.10008.5.1.4.1.1.9.1.1", 1)
    item.attributes["0070,0006"] = "ANONYMIZED"
    assert _real_note(item) == ""
    item.attributes["0070,0006"] = "sinus rhythm"
    assert _real_note(item) == "sinus rhythm"
    del item.attributes["0070,0006"]
    assert _real_note(item) == ""
