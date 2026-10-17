"""A structured report's content can be kept and its Text Values scrubbed
(#1131).

`basic@2026c` removes Content Sequence `(0040,A730)`, a departure from the
table's `D` made in #848, because a TEXT item's Text Value `(0040,A160)`
is free text no row reaches. With `SCRUB` (#1130) it is reachable, so a
configuration can keep the tree:

    0040,a730: {action: KEEP}
    0040,a160: {action: SCRUB, target: [names, dates, ids]}

and the rows nested in content items (Person Name, Date, UID, ...) keep
applying inside it. This is not PS3.15's Clean Structured Content Option,
which Table E.3.4-1 keys by concept name, and nothing here claims it.

A dcmqi-style report is built in the test with pydicom: a TEXT holding an
injected ID, a tracking identifier and two provenance strings (#1131
quotes `Nodule 1`, `pylidc` and `0.2.0` as kept verbatim), a PNAME, a DATE,
a NUM with units, and a UIDREF. Every other value is invented.
"""
import os
import sqlite3

import pydicom
import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import ExplicitVRLittleEndian

from isocenter import Session

ROOT = "1.2.826.0.1.3680043.10.9998"
INJECTED = "CHEST NODULE for 4815162342"
KEPT_TEXTS = ("Nodule 1", "pylidc", "0.2.0")
SOURCE_DATE = "20210623"
OBSERVER = "OBSERVER^ONE"
REF_UID = f"{ROOT}.77.1"


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _code(value, scheme, meaning):
    code = Dataset()
    code.CodeValue, code.CodingSchemeDesignator, code.CodeMeaning = value, scheme, meaning
    return code


def _item(value_type, concept, relationship="CONTAINS", **values):
    item = Dataset()
    item.RelationshipType = relationship
    item.ValueType = value_type
    item.ConceptNameCodeSequence = Sequence([_code(*concept)])
    for keyword, value in values.items():
        setattr(item, keyword, value)
    return item


def _text(text, concept=("121106", "DCM", "Comment")):
    return _item("TEXT", concept, TextValue=text)


def _content():
    measured = Dataset()
    measured.NumericValue = "42.5"
    measured.MeasurementUnitsCodeSequence = Sequence([_code("mm", "UCUM", "millimeter")])
    return [
        _text(INJECTED),
        _text("Nodule 1", ("112039", "DCM", "Tracking Identifier")),
        _text("pylidc", ("111526", "DCM", "Algorithm Name")),
        _text("0.2.0", ("111528", "DCM", "Algorithm Version")),
        _item("PNAME", ("121008", "DCM", "Person Observer Name"),
              relationship="HAS OBS CONTEXT", PersonName=OBSERVER),
        _item("DATE", ("111060", "DCM", "Study Date"), Date=SOURCE_DATE),
        _item("NUM", ("G-D705", "SRT", "Volume"),
              MeasuredValueSequence=Sequence([measured])),
        _item("UIDREF", ("121232", "DCM", "Source series for segmentation"),
              UID=REF_UID),
    ]


def _write_sr(tmp_path, content=None, acquisition_context=None, name="sr.dcm"):
    ds = Dataset()
    ds.file_meta = FileMetaDataset()
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.file_meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.88.33"
    ds.file_meta.MediaStorageSOPInstanceUID = f"{ROOT}.3.1"
    ds.SOPClassUID = ds.file_meta.MediaStorageSOPClassUID
    ds.SOPInstanceUID = ds.file_meta.MediaStorageSOPInstanceUID
    ds.StudyInstanceUID = f"{ROOT}.1"
    ds.SeriesInstanceUID = f"{ROOT}.2"
    ds.PatientID = "SR1131"
    ds.PatientName = "REPORT^SUBJECT"
    ds.StudyDate = "20210623"
    ds.Modality = "SR"
    ds.SeriesNumber = 1
    ds.InstanceNumber = 1
    ds.ValueType = "CONTAINER"
    ds.ConceptNameCodeSequence = Sequence([_code("126000", "DCM",
                                                 "Imaging Measurement Report")])
    ds.ContinuityOfContent = "SEPARATE"
    ds.CompletionFlag = "COMPLETE"
    ds.VerificationFlag = "UNVERIFIED"
    ds.ContentSequence = Sequence(content if content is not None else _content())
    if acquisition_context is not None:
        ds.AcquisitionContextSequence = Sequence(acquisition_context)
    os.makedirs(tmp_path / "in", exist_ok=True)
    path = tmp_path / "in" / name
    ds.save_as(path, enforce_file_format=True)
    return ds


KEEP_AND_SCRUB = (
    "privacy_profile: basic\n"
    "phi_tags:\n"
    "  0040,a730: {action: KEEP, name: Content Sequence}\n"
    "  0040,a160: {action: SCRUB, target: [names, dates, ids], name: Text Value}\n")


def _session(tmp_path, config, db="s.db"):
    (tmp_path / "cfg.yaml").write_text(config)
    session = Session(str(tmp_path / db))
    session.ingest(str(tmp_path / "in"))
    session.load_config(str(tmp_path / "cfg.yaml"))
    return session


def _export(session, tmp_path):
    session.export(str(tmp_path / "out"), use_compression=False, show_progress=False)
    [path] = [os.path.join(root, f) for root, _, names in os.walk(tmp_path / "out")
              for f in names if f.endswith(".dcm")]
    return pydicom.dcmread(path)


def _grade(session, tmp_path):
    session.generate_report(str(tmp_path / "r.md"))
    text = (tmp_path / "r.md").read_text(encoding="utf-8")
    return [g for g in ("PASS", "REVIEW_REQUIRED", "FAIL") if f"**{g}**" in text]


def _by_concept(content):
    return {item.ConceptNameCodeSequence[0].CodeMeaning: item for item in content}


def test_under_basic_the_content_sequence_is_still_removed(tmp_path):
    """The default is unchanged: #848's departure stands."""
    _write_sr(tmp_path)
    with _session(tmp_path, "privacy_profile: basic\n") as session:
        session.anonymize(session.audit())
        out = _export(session, tmp_path)
    assert "ContentSequence" not in out


def test_kept_content_scrubs_text_values_and_keeps_measurements(tmp_path):
    src = _write_sr(tmp_path)
    with _session(tmp_path, KEEP_AND_SCRUB) as session:
        session.anonymize(session.audit())
        out = _export(session, tmp_path)
    items = _by_concept(out.ContentSequence)
    assert items["Comment"].TextValue == "CHEST NODULE"
    assert items["Tracking Identifier"].TextValue == "Nodule 1"
    assert items["Algorithm Name"].TextValue == "pylidc"
    assert items["Algorithm Version"].TextValue == "0.2.0"
    for kept, source in zip(out.ContentSequence, src.ContentSequence):
        assert kept.ValueType == source.ValueType
        assert kept.RelationshipType == source.RelationshipType
        assert kept.ConceptNameCodeSequence == source.ConceptNameCodeSequence
    volume = items["Volume"].MeasuredValueSequence[0]
    assert str(volume.NumericValue) == "42.5"
    assert volume.MeasurementUnitsCodeSequence[0].CodeMeaning == "millimeter"
    assert volume.MeasurementUnitsCodeSequence[0].CodeValue == "mm"


@pytest.mark.parametrize("date_rule", ["", "  0040,a121: {action: JITTER}\n"],
                         ids=["basic", "jitter"])
def test_the_nested_rows_still_apply_inside_a_kept_tree(tmp_path, date_rule):
    _write_sr(tmp_path)
    with _session(tmp_path, KEEP_AND_SCRUB + date_rule) as session:
        session.anonymize(session.audit())
        out = _export(session, tmp_path)
    items = _by_concept(out.ContentSequence)
    assert str(items["Person Observer Name"].PersonName) == "ANONYMIZED"
    date = items["Study Date"].Date
    if date_rule:
        assert date not in (SOURCE_DATE, "19000101") and len(str(date)) == 8
    else:
        assert date == "19000101"
    uid = items["Source series for segmentation"].UID
    assert uid != REF_UID and uid.startswith("2.25.")


def test_a_text_value_scrubbed_to_nothing_is_the_dummy_not_empty(tmp_path):
    """Text Value is Type 1C, required for a TEXT item (PS3.3 Table 10-2):
    an emptied one is `ANONYMIZED`, never zero-length (owner ruling Q7)."""
    _write_sr(tmp_path, content=[_text("for 4815162342")])
    with _session(tmp_path, KEEP_AND_SCRUB) as session:
        session.anonymize(session.audit())
        out = _export(session, tmp_path)
    assert out.ContentSequence[0].TextValue == "ANONYMIZED"


def test_a_kept_report_reaudits_clean_and_grades_pass(tmp_path):
    _write_sr(tmp_path)
    with _session(tmp_path, KEEP_AND_SCRUB) as session:
        session.anonymize(session.audit())
        assert [f for f in session.audit().findings if f.tag == "0040,a160"] == []
        session.save(sync=True)
    with Session(str(tmp_path / "s.db")) as session:
        session.load_config(str(tmp_path / "cfg.yaml"))
        assert [f for f in session.audit().findings if f.tag == "0040,a160"] == []
        out = _export(session, tmp_path)
        assert _grade(session, tmp_path) == ["PASS"]
    assert _by_concept(out.ContentSequence)["Comment"].TextValue == "CHEST NODULE"


def test_a_deep_content_tree_is_scrubbed_at_every_level(tmp_path):
    """Six levels, a TEXT at each: every finding is rehydrated to its own
    item (`_live_target`), never to the instance."""
    deepest = None
    for level in range(6, 0, -1):
        container = _item("CONTAINER", ("125007", "DCM", "Measurement Group"),
                          ContinuityOfContent="SEPARATE")
        children = [_text(f"LEVEL {level} for 48151623{level:02d}")]
        if deepest is not None:
            children.append(deepest)
        container.ContentSequence = Sequence(children)
        deepest = container
    _write_sr(tmp_path, content=[deepest])
    with _session(tmp_path, KEEP_AND_SCRUB) as session:
        report = session.audit()
        assert len([f for f in report.findings if f.tag == "0040,a160"]) == 6
        session.anonymize(report)
        out = _export(session, tmp_path)
    texts, item = [], out.ContentSequence[0]
    while item is not None:
        children = item.ContentSequence
        texts.append(children[0].TextValue)
        item = children[1] if len(children) > 1 else None
    assert texts == [f"LEVEL {level}" for level in range(1, 7)]
    assert "TextValue" not in out


def test_the_acquisition_context_sequence_follows_the_same_recipe(tmp_path):
    """Acquisition Context `(0040,0555)` is the other Clean Structured
    Content row: kept by a rule, its TEXT items scrubbed the same way."""
    _write_sr(tmp_path, acquisition_context=[
        _text("PROTOCOL B for 4815162342", ("113014", "DCM", "Protocol"))])
    with _session(tmp_path, KEEP_AND_SCRUB
                  + "  0040,0555: {action: KEEP, name: Acquisition Context Sequence}\n"
                  ) as session:
        session.anonymize(session.audit())
        out = _export(session, tmp_path)
    assert out.AcquisitionContextSequence[0].TextValue == "PROTOCOL B"


def test_no_row_names_a_text_value(tmp_path):
    _write_sr(tmp_path)
    with _session(tmp_path, KEEP_AND_SCRUB) as session:
        session.anonymize(session.audit())
        session.store_backend.flush_audit_queue()
        with sqlite3.connect(session.persistence_file) as conn:
            details = [row[0] for row in conn.execute("SELECT details FROM audit_log")]
    assert not any("4815162342" in d or "CHEST NODULE" in d for d in details if d)


def test_a_text_value_loses_a_name_the_report_holds_in_a_pname_item(tmp_path):
    """`names` reads the Person Names the instance holds at every depth,
    so an observer named only in a PNAME content item is removed from the
    report's own text. The patient's name shares no word with it."""
    _write_sr(tmp_path, content=[
        _text("NODULE reviewed by Orla Vaske"),
        _item("PNAME", ("121008", "DCM", "Person Observer Name"),
              relationship="HAS OBS CONTEXT", PersonName="VASKE^ORLA"),
    ])
    with _session(tmp_path, KEEP_AND_SCRUB) as session:
        session.anonymize(session.audit())
        out = _export(session, tmp_path)
    assert out.ContentSequence[0].TextValue == "NODULE reviewed"
