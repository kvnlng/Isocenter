"""Patient's Name, Patient ID and Study Date follow their `phi_tags` rule (#537).

The three are owned by the `Patient` and the `Study`, and the exporter
stamps the owner's value on every file. Until 0.9.8 `scan_patient` and
`_scan_study` proposed their replacement without looking the rule up, so
`anonymize()` wrote `ANONYMIZED`, the keyed `ANON_` pseudonym and the
per-patient shift under every policy. Measured on ac33641, CT_small,
under the floor, `none` with each of KEEP / REMOVE / EMPTY / REPLACE /
REPLACE `value:`, and `basic`: every export read `ANONYMIZED`, `ANON_...`
and a shifted date. A rule on one of the three changed only the
instance's own copy, which #492 then overwrote with the owner's value.

Each case here runs ingest -> load_config -> audit -> anonymize -> export
on a CT_small copy and asserts four places: the exported file, the
owner's field, the instance's top-level copy, and a re-audit that raises
nothing on the tag -- the last is what kills a "is this already the
replacement?" test left comparing against the constant.

**Why this file imports what it does.** `PhiInspector` through
`isocenter.privacy`, `RemediationService` through `isocenter.remediation`,
so both modules' probe rows are charged; see
`test_mutation_probe_targets.py`.
"""
import datetime
import re

import pydicom
import pytest
import yaml

from isocenter.entities import JITTER_SCHEME_KEYED
from isocenter.io_handlers import format_study_date
from isocenter.privacy import PhiInspector, _replacement_id_for
from isocenter.remediation import RemediationService
from isocenter.session import DicomSession

from support.ct_small_files import write_ct
from support.project_secret import FIXED_A, load_fixed_secret

NAME = "Orig^Name"
PID = "P537"
DATE = "20040119"
OWNED = ("0010,0010", "0010,0020", "0008,0020")


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)


def _shifted(date_str=DATE):
    """The study date under FIXED_A's offset for PID, computed here from
    the offset derivation rather than read back off the pipeline."""
    days = RemediationService(project_secret=FIXED_A)._get_date_shift(  # pylint: disable=protected-access
        PID, JITTER_SCHEME_KEYED)
    start = datetime.datetime.strptime(date_str, "%Y%m%d").date()
    return (start + datetime.timedelta(days=days)).strftime("%Y%m%d")


PSEUDONYM = _replacement_id_for(PID, FIXED_A)


def _config(tmp_path, tags, profile="none", name="cfg.yaml"):
    path = tmp_path / name
    body = {"phi_tags": tags}
    if profile is not None:
        body["privacy_profile"] = profile
    path.write_text(yaml.safe_dump(body), encoding="utf-8")
    return str(path)


class Run:
    """One pipeline pass and everything a case asserts about it."""

    def __init__(self, tmp_path, tags, profile="none"):
        self.tmp_path = tmp_path
        write_ct(tmp_path / "in" / "a.dcm", PID, "537", study_date=DATE, name=NAME)
        self.session = DicomSession(str(tmp_path / "s.db"))
        load_fixed_secret(self.session, tmp_path, FIXED_A)
        self.session.ingest(str(tmp_path / "in"))
        if tags is not None:
            self.session.load_config(_config(tmp_path, tags, profile))
        self.session.anonymize(self.session.audit())

    @property
    def patient(self):
        return self.session.store.patients[0]

    @property
    def study(self):
        return self.patient.studies[0]

    @property
    def instance(self):
        return self.study.series[0].instances[0]

    def copy(self, tag):
        return self.instance.attributes.get(tag, "<absent>")

    def export(self):
        out = self.tmp_path / "out"
        summary = self.session.export(str(out), use_compression=False)
        assert summary.written == 1, summary.failures
        (path,) = list(out.rglob("*.dcm"))
        return pydicom.dcmread(str(path)), path.relative_to(out)

    def reaudit(self, tag):
        return [(f.entity_type, f.remediation_proposal.action_type)
                for f in self.session.audit() if f.tag == tag]

    def close(self):
        self.session.close()


@pytest.fixture
def run(tmp_path):
    runs = []

    def make(tags, profile="none", sub="a"):
        r = Run(tmp_path / sub, tags, profile)
        runs.append(r)
        return r

    yield make
    for r in runs:
        r.close()


def _file_value(ds, keyword):
    assert keyword in ds, f"{keyword} is absent from the file"
    return str(ds[keyword].value)


# ---------------------------------------------------------------------------
# Patient's Name
# ---------------------------------------------------------------------------

NAME_CASES = {
    # id: (rule or None for no rule, file, owner, copy)
    "keep": ({"action": "KEEP"}, NAME, NAME, NAME),
    "remove": ({"action": "REMOVE"}, "", None, "<absent>"),
    "empty": ({"action": "EMPTY"}, "", "", ""),
    "replace": ({"action": "REPLACE"}, "ANONYMIZED", "ANONYMIZED", "ANONYMIZED"),
    "replace-value": ({"action": "REPLACE", "value": "Project-X"},
                      "Project-X", "Project-X", "Project-X"),
    "string-form": ("Patient's Name", "ANONYMIZED", "ANONYMIZED", "ANONYMIZED"),
    "no-rule": (None, "ANONYMIZED", "ANONYMIZED", "ANONYMIZED"),
    # An empty `value:` is no value, as it is on every other tag.
    "replace-empty-value": ({"action": "REPLACE", "value": ""},
                            "ANONYMIZED", "ANONYMIZED", "ANONYMIZED"),
}


@pytest.mark.parametrize("case", sorted(NAME_CASES))
def test_patients_name_follows_its_rule(run, case):
    """Kills each branch of the owned-rule reader for the name, and the
    already-replaced test left at `== "ANONYMIZED"` (REPLACE `value:`
    re-raises on the re-audit)."""
    rule, in_file, owner, copy = NAME_CASES[case]
    tags = {"0008,0080": {"action": "REMOVE"}}
    if rule is not None:
        tags["0010,0010"] = rule
    r = run(tags)
    assert r.patient.patient_name == owner
    assert r.copy("0010,0010") == copy
    assert r.reaudit("0010,0010") == []
    ds, _ = r.export()
    assert _file_value(ds, "PatientName") == in_file
    # The other two owned tags keep their defaults.
    assert ds.PatientID == PSEUDONYM
    assert ds.StudyDate == _shifted()


# ---------------------------------------------------------------------------
# Patient ID
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rule", [{"action": "REPLACE"}, "Patient ID", None],
                         ids=["replace", "string-form", "no-rule"])
def test_patient_id_is_the_keyed_pseudonym_under_replace(run, rule):
    """Kills the pseudonym dropped under REPLACE."""
    tags = {} if rule is None else {"0010,0020": rule}
    r = run(tags)
    assert r.patient.patient_id == PSEUDONYM
    assert r.reaudit("0010,0020") == []
    ds, rel = r.export()
    assert ds.PatientID == PSEUDONYM
    assert rel.parts[0] == f"Subject_{PSEUDONYM}"


def test_patient_id_under_keep_is_kept(run):
    """Kills KEEP ignored on the ID: before, the pseudonym."""
    r = run({"0010,0020": {"action": "KEEP"}})
    assert r.patient.patient_id == PID
    assert r.copy("0010,0020") == PID
    assert r.reaudit("0010,0020") == []
    ds, rel = r.export()
    assert ds.PatientID == PID
    assert rel.parts[0] == f"Subject_{PID}"


def test_a_bare_inspector_scans_an_instance_holding_a_patient_id():
    """Guard. The floor's ID row is REPLACE: the instance arm proposes
    the generic `ANONYMIZED`, which folds into the patient's pseudonym
    write, and mints nothing. Kills a pseudonym mint moved into the
    instance arm (it would propose `ANON_...`). The inspector is given a
    secret since #544: the floor's SOP Instance UID row is a keyed UID
    replacement, and without one the scan raises `RuntimeError` there."""
    from isocenter.entities import Instance
    instance = Instance("1.2.826.0.1.537.1", "1.2.840.10008.5.1.4.1.1.7", 1)
    instance.set_attr("0010,0020", PID)
    findings = [f for f in PhiInspector(project_secret=FIXED_A)._scan_instance(instance, PID)  # pylint: disable=protected-access
                if f.tag == "0010,0020"]
    assert [(f.remediation_proposal.action_type, f.remediation_proposal.new_value)
            for f in findings] == [("REPLACE_TAG", "ANONYMIZED")]


# ---------------------------------------------------------------------------
# Study Date
# ---------------------------------------------------------------------------

DATE_CASES = {
    # id: (rule or None, file, owner, copy)
    "keep": ({"action": "KEEP"}, DATE, datetime.date(2004, 1, 19), DATE),
    "remove": ({"action": "REMOVE"}, "", None, "<absent>"),
    "empty": ({"action": "EMPTY"}, "", "", ""),
    "replace-value": ({"action": "REPLACE", "value": "19000101"},
                      "19000101", datetime.date(1900, 1, 1), "19000101"),
    "replace": ({"action": "REPLACE"}, "shifted", "shifted", "shifted"),
    "string-form": ("Study Date", "shifted", "shifted", "shifted"),
    "jitter": ({"action": "JITTER"}, "shifted", "shifted", "shifted"),
    "no-rule": (None, "shifted", "shifted", "shifted"),
    # An empty `value:` is no value, so under Q3 it is the shift and not
    # an empty date (review of #574: `or None` removed from `_owned_rule`
    # emptied it).
    "replace-empty-value": ({"action": "REPLACE", "value": ""},
                            "shifted", "shifted", "shifted"),
}


@pytest.mark.parametrize("case", sorted(DATE_CASES))
def test_study_date_follows_its_rule(run, case):
    """Kills: the REPLACE-`value:` skip missing (re-raises SHIFT_DATE);
    the Q3 mapping missing (the string form writes `ANONYMIZED` or is
    refused); each action branch of `_scan_study`."""
    rule, in_file, owner, copy = DATE_CASES[case]
    shifted = _shifted()
    tags = {} if rule is None else {"0008,0020": rule}
    r = run(tags)
    expect_owner = (datetime.datetime.strptime(shifted, "%Y%m%d").date()
                    if owner == "shifted" else owner)
    assert r.study.study_date == expect_owner
    assert r.copy("0008,0020") == (shifted if copy == "shifted" else copy)
    assert r.reaudit("0008,0020") == []
    ds, _ = r.export()
    assert _file_value(ds, "StudyDate") == (shifted if in_file == "shifted" else in_file)


@pytest.mark.parametrize("rule,owner", [({"action": "EMPTY"}, ""),
                                        ({"action": "REPLACE", "value": "19000101"},
                                         datetime.date(1900, 1, 1))],
                         ids=["empty", "replace-value"])
def test_a_honoured_study_date_is_clean_after_a_reload(tmp_path, rule, owner):
    """The value survives the store and a fresh session's audit reads it
    clean: kills a skip that holds only for the in-memory spelling."""
    r = Run(tmp_path, {"0008,0020": rule})
    try:
        r.session.save(sync=True)
    finally:
        r.close()
    with DicomSession(str(tmp_path / "s.db")) as again:
        again.load_config(_config(tmp_path, {"0008,0020": rule}, name="again.yaml"))
        study = again.store.patients[0].studies[0]
        assert study.study_date == owner
        assert [f for f in again.audit() if f.tag == "0008,0020"] == []


@pytest.mark.parametrize("source_date", [None, ""], ids=["absent", "empty"])
def test_a_replace_value_does_not_give_a_dateless_study_a_date(tmp_path, source_date):
    """A study with no date, or an empty one, is not given the rule's
    `value:`: that is #60's invention by another route, and the instance
    arm's REPLACE skips a blank the same way. Kills the owner arm's
    has-a-date guard removed."""
    write_ct(tmp_path / "in" / "a.dcm", PID, "5370", study_date=source_date, name=NAME)
    with DicomSession(str(tmp_path / "s.db")) as session:
        load_fixed_secret(session, tmp_path, FIXED_A)
        session.ingest(str(tmp_path / "in"))
        session.load_config(_config(tmp_path, {
            "0008,0020": {"action": "REPLACE", "value": "19000101"}}))
        report = session.audit()
        assert [f for f in report if f.tag == "0008,0020"] == []
        session.anonymize(report)
        assert not session.store.patients[0].studies[0].study_date


def test_remove_on_an_owner_already_blank_leaves_it_absent(tmp_path):
    """REMOVE means the owner holds nothing (None), not an empty string,
    even when the source value was already `""`: the export is the same
    zero-length element either way, so only the in-memory owner tells
    them apart (review of #574, F-6). Kills the name and the date REMOVE
    arms each gated on the value being truthy rather than not None."""
    from isocenter.entities import Instance, Patient, Series, Study
    patient = Patient(PID, "")
    study = Study("1.2.826.0.1.537.9", "")
    series = Series("1.2.826.0.1.537.9.1", "OT", 1)
    instance = Instance("1.2.826.0.1.537.9.1.1", "1.2.840.10008.5.1.4.1.1.7", 1)
    series.instances.append(instance)
    study.series.append(series)
    patient.studies.append(study)
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.store.patients.append(patient)
        session.configuration.phi_tags = {
            "0010,0010": {"action": "REMOVE"}, "0010,0020": {"action": "KEEP"},
            "0008,0020": {"action": "REMOVE"}}
        session.anonymize(session.audit())
        assert patient.patient_name is None
        assert study.study_date is None
        assert [f for f in session.audit() if f.tag in OWNED] == []


def test_a_date_shifted_in_an_earlier_pass_is_emptied_when_the_rule_becomes_empty(run):
    """Pass 1 under the floor shifts the date; pass 2 says EMPTY. Kills
    `_scan_study`'s "this pipeline shifted it" early return left above
    the action branches: the shifted date is then never raised again and
    is exported."""
    r = run(None)
    assert r.study.study_date == datetime.datetime.strptime(_shifted(), "%Y%m%d").date()
    r.session.load_config(_config(r.tmp_path, {"0008,0020": {"action": "EMPTY"}},
                                  name="pass2.yaml"))
    r.session.anonymize(r.session.audit())
    assert r.study.study_date == ""
    ds, _ = r.export()
    assert ds.StudyDate == ""


def test_a_change_of_replacement_value_rewrites_the_name(run):
    """Pass 1 has no name rule (`ANONYMIZED`), pass 2 REPLACE `v1`, pass 3
    REPLACE `v2`, pass 4 REPLACE with no value again. The scan compares
    against the value the rule resolves to now, not against any
    replacement: kills `ANONYMIZED` ORed into the scan's skip (pass 2
    would keep it), and a skip on any earlier `value:` (pass 4 would keep
    `v2`)."""
    r = run({"0008,0080": {"action": "REMOVE"}})
    assert r.patient.patient_name == "ANONYMIZED"
    for value, expected in (("Cohort-1", "Cohort-1"), ("Cohort-2", "Cohort-2"),
                            (None, "ANONYMIZED")):
        rule = {"action": "REPLACE"}
        if value is not None:
            rule["value"] = value
        r.session.load_config(_config(r.tmp_path, {"0010,0010": rule},
                                      name=f"{expected}.yaml"))
        r.session.anonymize(r.session.audit())
        assert r.patient.patient_name == expected
        assert r.copy("0010,0010") == expected


def test_a_nested_study_date_under_the_string_form_is_shifted_not_replaced():
    """Q3 at every depth: the string form on Study Date means the shift,
    and a nested copy is judged by the same rule. Kills the mapping
    applied only at the top level (the nested proposal would be
    `REPLACE_TAG ANONYMIZED`, which a DA cannot hold)."""
    from isocenter.entities import DicomItem, DicomSequence, Instance
    instance = Instance("1.2.826.0.1.537.2", "1.2.840.10008.5.1.4.1.1.7", 1)
    item = DicomItem()
    item.set_attr("0008,0020", DATE)
    instance.sequences["0008,1115"] = DicomSequence(tag="0008,1115", items=[item])
    for rule in ("Study Date", {"action": "REPLACE"}):
        findings = [f for f in PhiInspector(config_tags={"0008,0020": rule})
                    ._scan_instance(instance, PID)  # pylint: disable=protected-access
                    if f.tag == "0008,0020"]
        assert [(f.entity_path, f.remediation_proposal.action_type)
                for f in findings] == [((("0008,1115", 0),), "SHIFT_DATE")]


# ---------------------------------------------------------------------------
# The profiles
# ---------------------------------------------------------------------------

def test_basic_empties_study_date_and_the_floor_still_shifts_it(run):
    """Kills the basic row left REMOVE (the file would read the same, so
    the owner's `""` against REMOVE's None is what tells them apart)."""
    basic = run({}, profile="basic", sub="basic")
    assert basic.study.study_date == ""
    ds, _ = basic.export()
    assert ds.StudyDate == ""

    floor = run(None, sub="floor")
    ds, _ = floor.export()
    assert ds.StudyDate == _shifted()


def test_the_floor_exports_are_unchanged(run):
    """A bare session: name `ANONYMIZED`, ID the keyed pseudonym, date the
    per-patient shift, byte for byte against values computed here. Kills
    the floor's name row flipped to EMPTY and its ID row to anything but
    the pseudonym."""
    r = run(None)
    ds, _ = r.export()
    assert (str(ds.PatientName), ds.PatientID, ds.StudyDate) == (
        "ANONYMIZED", PSEUDONYM, _shifted())
    assert re.fullmatch(r"ANON_[0-9a-f]{24}", ds.PatientID)
    assert r.copy("0010,0010") == "ANONYMIZED"
    assert r.copy("0010,0020") == PSEUDONYM
    assert format_study_date(r.study.study_date) == _shifted()
