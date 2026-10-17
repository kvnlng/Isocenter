"""`SCRUB` keeps a description and removes the identifiers in it (#1130).

PS3.15 E.3.5's Clean Descriptors Option, as a rule:
`0008,103e: {action: SCRUB, target: [dates, ids]}`. Before this a
description could only be kept or removed whole, so an identifier inside
one cost the clinical text a study selects series by.

Through a session: the scan proposes the cleaned value, remediation writes
it with a row of counts only, the export carries it, and a re-audit, here
or after a reopen, finds nothing. Every value here is invented, but for
the shapes #1130 quotes.
"""
import logging
import os
import sqlite3

import pydicom
import pytest
import yaml
from pydicom.dataset import Dataset
from pydicom.sequence import Sequence
from pydicom.uid import ImplicitVRLittleEndian

from isocenter import Session, text_kinds
from isocenter.config_manager import ConfigLoader
from isocenter.pixel_analysis import NlpUnavailableError
from isocenter.privacy import PhiInspector

from support.ct_small_files import write_ct

ID_TEXT = "CHEST CT for 4815162342"
CLEANED = "CHEST CT"


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _source(tmp_path, implicit=False, **elements):
    path = write_ct(tmp_path / "in" / "a.dcm", "PA", 1130,
                    name=elements.pop("PatientName", None))
    ds = pydicom.dcmread(path)
    ds.remove_private_tags()
    for keyword, value in elements.items():
        if keyword == "private":
            block = ds.private_block(0x0009, "ACME_DESC_01", create=True)
            block.add_new(0x10, "LO", value)
        else:
            setattr(ds, keyword, value)
    if implicit:
        ds.file_meta.TransferSyntaxUID = ImplicitVRLittleEndian
    ds.save_as(path, enforce_file_format=True)
    return ds


def _config(tmp_path, rules, remove_private="false"):
    path = tmp_path / "cfg.yaml"
    path.write_text(yaml.safe_dump({
        "privacy_profile": "basic", "remove_private_tags": remove_private == "true",
        "phi_tags": rules}))
    return path


def _session(tmp_path, rules, db="s.db", **kwargs):
    session = Session(str(tmp_path / db))
    session.ingest(str(tmp_path / "in"))
    session.load_config(str(_config(tmp_path, rules, **kwargs)))
    return session


def _instance(session):
    [patient] = session.store.patients
    return patient.studies[0].series[0].instances[0]


def _rows(session, action_type):
    session.store_backend.flush_audit_queue()
    with sqlite3.connect(session.persistence_file) as conn:
        return [row[0] for row in conn.execute(
            "SELECT details FROM audit_log WHERE action_type=?", (action_type,))]


def _export(session, tmp_path):
    session.export(str(tmp_path / "out"), use_compression=False, show_progress=False)
    [path] = [os.path.join(root, f) for root, _, names in os.walk(tmp_path / "out")
              for f in names if f.endswith(".dcm")]
    return pydicom.dcmread(path)


def _grade(session, tmp_path):
    session.generate_report(str(tmp_path / "r.md"))
    text = (tmp_path / "r.md").read_text(encoding="utf-8")
    return [g for g in ("PASS", "REVIEW_REQUIRED", "FAIL") if f"**{g}**" in text]


SERIES = {"0008,103e": {"action": "SCRUB", "target": ["dates", "ids"],
                        "name": "Series Description"}}


def test_scrub_writes_the_cleaned_text_and_a_counts_only_row(tmp_path, caplog):
    _source(tmp_path, SeriesDescription=ID_TEXT)
    with caplog.at_level(logging.DEBUG, logger="isocenter"):
        with _session(tmp_path, SERIES) as session:
            report = session.audit()
            [finding] = [f for f in report.findings if f.tag == "0008,103e"]
            assert finding.remediation_proposal.new_value == CLEANED
            session.anonymize(report)
            assert _instance(session).attributes["0008,103e"] == CLEANED
            out = _export(session, tmp_path)
            rows = [r for r in _rows(session, "REMEDIATION_REPLACE") if "0008,103e" in r]
    assert out.SeriesDescription == CLEANED
    assert len(rows) == 1 and rows[0].endswith("(Tag 0008,103e): removed 1 id"), rows
    assert "4815162342" not in rows[0] and CLEANED not in rows[0]
    assert "4815162342" not in caplog.text
    assert "CHEST CT" not in caplog.text


def test_a_second_audit_finds_nothing(tmp_path):
    _source(tmp_path, SeriesDescription=ID_TEXT)
    with _session(tmp_path, SERIES) as session:
        session.anonymize(session.audit())
        assert [f for f in session.audit().findings if f.tag == "0008,103e"] == []
        session.export(str(tmp_path / "out"), use_compression=False, show_progress=False)
        assert _grade(session, tmp_path) == ["PASS"]


def test_a_reopened_store_finds_nothing(tmp_path):
    _source(tmp_path, SeriesDescription=ID_TEXT)
    with _session(tmp_path, SERIES) as session:
        session.anonymize(session.audit())
        session.save(sync=True)
    with Session(str(tmp_path / "s.db")) as session:
        session.load_config(str(tmp_path / "cfg.yaml"))
        inst = _instance(session)
        assert inst.attributes["0008,103e"] == CLEANED
        assert [f for f in session.audit().findings if f.tag == "0008,103e"] == []
        # A scan that finds nothing records CLEARED, as on any reopen.
        assert inst.phi_status.name == "CLEARED"
        session.export(str(tmp_path / "out"), use_compression=False, show_progress=False)
        assert _grade(session, tmp_path) == ["PASS"]


def test_scrub_reaches_a_nested_item(tmp_path):
    item = Dataset()
    item.RequestedProcedureDescription = "MRI KNEE 2017-07-07 for 4815162342"
    _source(tmp_path, RequestAttributesSequence=Sequence([item]))
    rules = {"0040,0275": {"action": "KEEP"},
             "0032,1060": {"action": "SCRUB", "target": ["dates", "ids"]}}
    with _session(tmp_path, rules) as session:
        session.anonymize(session.audit())
        out = _export(session, tmp_path)
    assert out.RequestAttributesSequence[0].RequestedProcedureDescription == "MRI KNEE"


def test_a_declined_scrub_names_no_text(tmp_path, caplog):
    """A SCRUB finding whose element has gone since the scan declines; its
    row and log line say "the cleaned text", never the text (#1130). Kills
    the decline quoting the proposal's value."""
    _source(tmp_path, SeriesDescription=ID_TEXT)
    with caplog.at_level(logging.DEBUG, logger="isocenter"):
        with _session(tmp_path, SERIES) as session:
            report = session.audit()
            del _instance(session).attributes["0008,103e"]
            session.anonymize(report)
            rows = [r for r in _rows(session, "REMEDIATION_DECLINED") if "0008,103e" in r]
    assert len(rows) == 1 and "the cleaned text" in rows[0], rows
    assert CLEANED not in rows[0] and "4815162342" not in rows[0]
    assert CLEANED not in caplog.text and "4815162342" not in caplog.text


def test_a_value_scrubbed_to_nothing_is_the_vr_dummy(tmp_path):
    _source(tmp_path, SeriesDescription="for 1234567890")
    with _session(tmp_path, SERIES) as session:
        session.anonymize(session.audit())
        assert _instance(session).attributes["0008,103e"] == "ANONYMIZED"
        assert [f for f in session.audit().findings if f.tag == "0008,103e"] == []


def test_a_multi_valued_value_is_scrubbed_value_by_value(tmp_path):
    """Admitting Diagnoses Description is LO 1-n. The in-memory shape
    straight from ingest, not only the reloaded one."""
    _source(tmp_path, AdmittingDiagnosesDescription=[
        "FRACTURE for 4815162342", "for 20170707", "FOLLOW-UP"])
    rules = {"0008,1080": {"action": "SCRUB", "target": ["dates", "ids"]}}
    with _session(tmp_path, rules) as session:
        report = session.audit()
        [finding] = [f for f in report.findings if f.tag == "0008,1080"]
        assert list(finding.remediation_proposal.new_value) == [
            "FRACTURE", "ANONYMIZED", "FOLLOW-UP"]
        session.anonymize(report)
        out = _export(session, tmp_path)
        rows = [r for r in _rows(session, "REMEDIATION_REPLACE") if "0008,1080" in r]
    assert list(out.AdmittingDiagnosesDescription) == ["FRACTURE", "ANONYMIZED", "FOLLOW-UP"]
    assert rows[0].endswith("removed 1 id, 1 date"), rows


def test_a_value_that_is_not_text_is_replaced_by_its_dummy_with_a_row_saying_so(tmp_path):
    """An implicit-VR private element is held as its bytes (#740)."""
    _source(tmp_path, implicit=True, private="SCAN for 4815162342")
    rules = {'0009,"ACME_DESC_01",10': {"action": "SCRUB", "target": ["ids"]}}
    with _session(tmp_path, rules) as session:
        inst = _instance(session)
        assert isinstance(inst.attributes["0009,1010"], bytes)
        session.anonymize(session.audit())
        assert inst.attributes["0009,1010"] == "ANONYMIZED"
        rows = [r for r in _rows(session, "REMEDIATION_REPLACE") if "0009,1010" in r]
    assert len(rows) == 1
    assert rows[0].endswith(
        "SCRUB could not read the value as text; replaced by its dummy"), rows
    assert "(creator ACME_DESC_01)" in rows[0]


def test_scrub_on_a_creator_key(tmp_path):
    _source(tmp_path, private="SCAN for 4815162342")
    rules = {'0009,"ACME_DESC_01",10': {"action": "SCRUB", "target": ["ids"]}}
    with _session(tmp_path, rules) as session:
        session.anonymize(session.audit())
        assert _instance(session).attributes["0009,1010"] == "SCAN"


def test_scrub_names_from_the_patients_own_name(tmp_path):
    """Q2 (a): the names are the instance's own Person Names, the
    patient's and a physician's, with no model."""
    _source(tmp_path, PatientName="QUILL^TAMSIN",
            ReferringPhysicianName="VANCE^ORLO",
            StudyDescription="Report for Tamsin Quill",
            SeriesDescription="Read by Dr. Orlo Vance")
    rules = {tag: {"action": "SCRUB", "target": ["names"]}
             for tag in ("0008,1030", "0008,103e")}
    with _session(tmp_path, rules) as session:
        session.anonymize(session.audit())
        inst = _instance(session)
        assert inst.attributes["0008,1030"] == "Report"
        assert inst.attributes["0008,103e"] == "Read"


def test_names_ask_no_model(tmp_path, monkeypatch):
    def fail():
        raise AssertionError("the model was asked")
    monkeypatch.setattr(text_kinds, "_nlp", fail)
    _source(tmp_path, PatientName="QUILL^TAMSIN", StudyDescription="Tamsin scan")
    rules = {"0008,1030": {"action": "SCRUB", "target": ["names", "dates", "ids"]}}
    with _session(tmp_path, rules) as session:
        session.anonymize(session.audit())
        assert _instance(session).attributes["0008,1030"] == "scan"


# --- refusals -------------------------------------------------------------

REFUSED = {
    "PN": ("0010,0010", {"action": "SCRUB", "target": ["ids"]}, "is PN"),
    "CS": ("0008,0060", {"action": "SCRUB", "target": ["ids"]}, "is CS"),
    "UI": ("0008,0018", {"action": "SCRUB", "target": ["ids"]}, "is UI"),
    "DA": ("0008,0021", {"action": "SCRUB", "target": ["ids"]}, "is DA"),
    "SQ": ("0040,0275", {"action": "SCRUB", "target": ["ids"]}, "is SQ"),
    "mask": ("60xx,xxxx", {"action": "SCRUB", "target": ["ids"]}, "repeating-group"),
    "Patient ID": ("0010,0020", {"action": "SCRUB", "target": ["ids"]},
                   "Patient ID can only be kept"),
    "Study UID": ("0020,000d", {"action": "SCRUB", "target": ["ids"]}, "is UI"),
    "target under REMOVE": ("0008,103e", {"action": "REMOVE", "target": ["ids"]},
                            "has a target under REMOVE; only SCRUB takes a target"),
    "no target": ("0008,103e", {"action": "SCRUB"},
                  "is SCRUB with no target; name the kinds to remove, a list "
                  "of names, dates, ids, orgs"),
    "empty target": ("0008,103e", {"action": "SCRUB", "target": []},
                     "target is an empty list"),
    "unknown kind": ("0008,103e", {"action": "SCRUB", "target": ["places"]},
                     "target item 'places' is not one of names, dates, ids, orgs"),
    "any": ("0008,103e", {"action": "SCRUB", "target": "any"},
            "target must be a list of kinds"),
    "any in a list": ("0008,103e", {"action": "SCRUB", "target": ["any"]},
                      "target item 'any' is not one of"),
    "duplicate": ("0008,103e", {"action": "SCRUB", "target": ["ids", "ids"]},
                  "target lists 'ids' twice"),
    "value": ("0008,103e", {"action": "SCRUB", "target": ["ids"], "value": "x"},
              "has a value under SCRUB; only REPLACE writes a value"),
}


@pytest.mark.parametrize("case", sorted(REFUSED))
def test_scrub_is_refused_where_it_cannot_apply(tmp_path, case):
    tag, rule, words = REFUSED[case]
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump({"privacy_profile": "none", "phi_tags": {tag: rule}}))
    with pytest.raises(ValueError, match=None) as caught:
        ConfigLoader.load_unified_config(str(path))
    assert words in str(caught.value), str(caught.value)
    with pytest.raises(ValueError) as caught:
        PhiInspector(config_tags={tag: dict(rule)})
    assert words in str(caught.value), str(caught.value)


def test_set_phi_tag_refuses_a_value_under_scrub(tmp_path):
    with Session(str(tmp_path / "s.db")) as session:
        with pytest.raises(ValueError, match="only REPLACE writes a value"):
            session.configuration.set_phi_tag("0008,103e", "SCRUB", value="x",
                                              target=["ids"])


@pytest.mark.parametrize("tag", ["0010,0010", "0008,0060", "0008,0021", "60xx,xxxx"])
def test_set_phi_tag_refuses_scrub_where_it_cannot_apply(tmp_path, tag):
    with Session(str(tmp_path / "s.db")) as session:
        before = dict(session.configuration.phi_tags)
        with pytest.raises(ValueError):
            session.configuration.set_phi_tag(tag, "SCRUB", target=["ids"])
        assert session.configuration.phi_tags == before


# --- set_phi_tag(target=) (owner ruling R16) --------------------------------

def test_set_phi_tag_scrub_with_a_target_is_the_files_rule(tmp_path):
    path = tmp_path / "f.yaml"
    path.write_text("privacy_profile: basic\nphi_tags:\n  0008,103e: "
                    "{action: SCRUB, target: [ids, dates], name: Custom Tag}\n")
    with Session(str(tmp_path / "s.db")) as session:
        session.load_config(str(path))
        from_file = session.configuration.phi_tags["0008,103e"]
    with Session(str(tmp_path / "t.db")) as session:
        session.configuration.set_phi_tag("0008,103e", "SCRUB", target=["ids", "dates"])
        assert session.configuration.phi_tags["0008,103e"] == from_file
        session.configuration.config_path = str(tmp_path / "saved.yaml")
        session.configuration.save()
    reloaded = ConfigLoader.load_unified_config(str(tmp_path / "saved.yaml"))[0]
    assert reloaded["0008,103e"] == from_file


def test_set_phi_tag_scrub_without_target_names_the_keyword(tmp_path):
    with Session(str(tmp_path / "s.db")) as session:
        before = dict(session.configuration.phi_tags)
        with pytest.raises(ValueError) as caught:
            session.configuration.set_phi_tag("0008,103e", "SCRUB")
        assert session.configuration.phi_tags == before
    assert str(caught.value) == (
        "set_phi_tag: phi_tags['0008,103e'] is SCRUB with no target; pass "
        "target=, a list of names, dates, ids, orgs "
        '(set_phi_tag(tag, "SCRUB", target=["dates", "ids"]))')


@pytest.mark.parametrize("action", ["KEEP", "REMOVE", "EMPTY", "REPLACE", "SHIFT", "JITTER"])
def test_set_phi_tag_target_is_refused_with_any_other_action(tmp_path, action):
    with Session(str(tmp_path / "s.db")) as session:
        with pytest.raises(ValueError, match="only SCRUB takes a target"):
            session.configuration.set_phi_tag("0008,0021", action, target=["ids"])


def test_set_phi_tag_target_must_be_a_list(tmp_path):
    with Session(str(tmp_path / "s.db")) as session:
        configuration = session.configuration
        with pytest.raises(ValueError,
                           match=r"target must be a list of kinds, such as \['dates'\]"):
            configuration.set_phi_tag("0008,103e", "SCRUB", target="dates")
        with pytest.raises(ValueError, match="target is an empty list"):
            configuration.set_phi_tag("0008,103e", "SCRUB", target=[])
        configuration.set_phi_tag("0008,103e", "SCRUB", target=("ids", "dates"))
        assert configuration.phi_tags["0008,103e"]["target"] == ["ids", "dates"]


def test_an_assigned_scrub_rule_is_applied_and_saved(tmp_path):
    _source(tmp_path, SeriesDescription=ID_TEXT)
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        session.configuration.phi_tags["0008,103e"] = {
            "action": "SCRUB", "target": ["ids"]}
        session.anonymize(session.audit())
        assert _instance(session).attributes["0008,103e"] == CLEANED
        session.configuration.config_path = str(tmp_path / "saved.yaml")
        session.configuration.save()
    reloaded = ConfigLoader.load_unified_config(str(tmp_path / "saved.yaml"))[0]
    assert reloaded["0008,103e"] == {"action": "SCRUB", "target": ["ids"]}


def test_an_assigned_scrub_rule_with_no_target_is_refused_at_audit(tmp_path):
    with Session(str(tmp_path / "s.db")) as session:
        session.configuration.phi_tags["0008,103e"] = {"action": "SCRUB"}
        with pytest.raises(ValueError, match="is SCRUB with no target"):
            session.audit()


# --- orgs ---------------------------------------------------------------------

def _no_model(monkeypatch):
    def fail():
        raise ImportError("No module named 'spacy'")
    monkeypatch.setattr(text_kinds, "_load_model", fail)
    monkeypatch.setattr(text_kinds, "_MODEL", None)


ORGS = {"0008,1030": {"action": "SCRUB", "target": ["dates", "orgs"]},
        "0008,103e": {"action": "SCRUB", "target": ["orgs"]}}


def test_orgs_without_the_model_is_refused_at_audit_not_at_load(tmp_path, monkeypatch):
    _no_model(monkeypatch)
    _source(tmp_path, SeriesDescription=ID_TEXT)
    with _session(tmp_path, ORGS) as session:
        inst = _instance(session)
        revision = inst._revision
        with pytest.raises(NlpUnavailableError) as caught:
            session.audit()
        assert inst._revision == revision
        assert session._audited_phi_tags is None or "0008,1030" not in (
            session._audited_phi_tags or {})
    assert str(caught.value) == (
        "audit: SCRUB target orgs on 0008,1030 needs the nlp extra (pip install "
        '"isocenter[nlp]") and its model (python -m spacy download '
        "en_core_web_sm): ImportError: No module named 'spacy'. Nothing was "
        "scanned.")


def test_check_burned_in_refuses_orgs_without_the_model(tmp_path, monkeypatch):
    _no_model(monkeypatch)
    _source(tmp_path, SeriesDescription=ID_TEXT)
    with _session(tmp_path, ORGS) as session:
        with pytest.raises(NlpUnavailableError, match=r"^export\(check_burned_in=True\): "):
            session.export(str(tmp_path / "out"), check_burned_in=True,
                           use_compression=False, show_progress=False)
    assert not (tmp_path / "out").exists() or not any(
        f.endswith(".dcm") for _, _, fs in os.walk(tmp_path / "out") for f in fs)


def test_a_policy_without_orgs_audits_with_no_model(tmp_path, monkeypatch):
    _no_model(monkeypatch)
    _source(tmp_path, SeriesDescription=ID_TEXT)
    with _session(tmp_path, SERIES) as session:
        session.anonymize(session.audit())
        assert _instance(session).attributes["0008,103e"] == CLEANED


def test_orgs_through_a_session_with_a_stand_in_model(tmp_path, monkeypatch):
    from types import SimpleNamespace

    def model(text):
        ents = []
        if "Harris Community Clinic" in text:
            start = text.index("Harris Community Clinic")
            ents.append(SimpleNamespace(label_="ORG", start_char=start,
                                        end_char=start + len("Harris Community Clinic")))
        return SimpleNamespace(ents=ents)
    monkeypatch.setattr(text_kinds, "_nlp", lambda: model)
    _source(tmp_path, SeriesDescription="LUNG CA ACCRIN 6668 at Harris Community Clinic")
    with _session(tmp_path, ORGS) as session:
        session.anonymize(session.audit())
        rows = [r for r in _rows(session, "REMEDIATION_REPLACE") if "0008,103e" in r]
        assert _instance(session).attributes["0008,103e"] == "LUNG CA ACCRIN 6668"
    assert rows[0].endswith("removed 1 organisation or place"), rows
