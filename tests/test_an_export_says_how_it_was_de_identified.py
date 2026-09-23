"""An exported DICOM file says how it was de-identified (#554).

Measured on 82e82386, and again on f54deaa1 (L10's head, the base of this
change), on 3.12 and 3.14t: `export()` wrote none of Patient Identity
Removed `(0012,0062)`, De-identification Method `(0012,0063)` or
Longitudinal Temporal Information Modified `(0028,0303)`, under the floor,
`basic` or `none`, so an archive that gates on `(0012,0062) = YES` refused
every Isocenter export. A source already carrying another tool's `YES`
and a `113100` code exported them unchanged under every policy, `none`
included. `RemediationService.add_global_deid_tags`, which would have
written `"Isocenter Privacy Profile"` and `113100`, was called by nothing.

What the owner ruled (L12, 2026-09-23, recorded on #554), and what each
test here pins:

- **Q1 (A):** `(0012,0062) YES` wherever the instance, its study and its
  patient read REMEDIATED or CLEARED under one policy the export accepts:
  the policy in force, or one this session audited under -- the set the
  #555 notice accepts, read from one helper. Anything else writes nothing:
  never audited, a finding left open, an edit after the pass, a reopen
  under another policy.
- **Q2 (A):** no CID 7050 code. `113100` asserts the Basic Profile, from
  which `basic@2026c` documents departures.
- **Q3 (a):** `(0028,0303)` from the file's own dates: `REMOVED` when every
  DA and DT in it is gone or a dummy, `MODIFIED` when the rest are shifts
  this store wrote, and nothing when any date is as found.
- **Q4:** `(0012,0063)` gains one value, `isocenter/<version>; <label>;
  v1:<8 hex>`, after any the source carried, unless the last already is it.
  An external profile's label is `external profile`, never its path.

The markers are written at export, in the worker, from the plan: never in
the graph, and never by `write_tree()`, which is the serializer without
the pipeline.

**Literals.** The labels and fingerprint prefixes were measured at
f54deaa1, after L10's U rows moved `basic@2026c`'s canonical form:
`floor over basic@2026c` is `v1:0ee566b4`, `basic@2026c` is
`v1:3bd3e9b7`, `none` is `v1:751bc63e`. The version is read from
`isocenter/_version.py` as text, never from `isocenter`, so a release bump
moves nothing here and a formatter that spelled the tool differently (the
fingerprint's N2 substitution reads exactly `isocenter/<version>`) is red.

**Why this file imports what it does.** `isocenter.session` is named, so
its probe row is charged; `isocenter.configuration` for the formatter
(M12); `isocenter.remediation` and `isocenter.services` for M11.
"""
import hashlib
import json
import re
import shutil
import sqlite3
from pathlib import Path

import pydicom
import pytest
from pydicom.data import get_testdata_file
from pydicom.dataset import Dataset
from pydicom.sequence import Sequence

from isocenter import configuration, remediation, services
from isocenter.entities import PhiStatus, ScanPolicy
from isocenter.io_handlers import DicomExporter
from isocenter.session import DicomSession

from support.project_secret import load_fixed_secret

REPO = Path(__file__).resolve().parents[1]
VERSION = re.search(r'__version__ = "([^"]+)"',
                    (REPO / "isocenter" / "_version.py").read_text()).group(1)

#: Measured at f54deaa1 (see the module docstring).
FLOOR = ("floor over basic@2026c", "v1:0ee566b4")
BASIC = ("basic@2026c", "v1:3bd3e9b7")
NONE = ("none", "v1:751bc63e")

REMOVED_TAG, METHOD, CODES, TEMPORAL = (0x00120062, 0x00120063, 0x00120064,
                                        0x00280303)
NOTICE = "recorded under a policy other than the one in force"


def ours(label, hexed):
    return f"isocenter/{VERSION}; {label}; {hexed}"


@pytest.fixture(autouse=True)
def _threads_by_default(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _source(folder, prior=False, identity_removed=None, edit=None):
    """CT_small in `folder`. `prior`: another tool's markers, as m1 wrote
    them. `identity_removed`: a source `(0012,0062)` value alone."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    ds = pydicom.dcmread(get_testdata_file("CT_small.dcm"))
    if prior:
        ds.PatientIdentityRemoved = "YES"
        ds.DeidentificationMethod = ["OtherTool 3.2", "site profile 7"]
        item = Dataset()
        item.CodeValue, item.CodingSchemeDesignator = "113100", "DCM"
        item.CodeMeaning = "Basic Application Confidentiality Profile"
        ds.DeidentificationMethodCodeSequence = Sequence([item])
        ds.LongitudinalTemporalInformationModified = "MODIFIED"
    if identity_removed is not None:
        ds.PatientIdentityRemoved = identity_removed
    if edit is not None:
        edit(ds)
    ds.save_as(str(folder / "ct.dcm"), enforce_file_format=True)
    return str(folder)


def _config(tmp_path, name, **body):
    path = tmp_path / name   # JSON is YAML; the loader wants the suffix
    path.write_text(json.dumps(body), encoding="utf-8")
    return str(path)


def _profile_config(tmp_path, profile, name, **extra):
    """None for the floor (a bare session), else a config naming it, at
    `<name>.yaml`. A file with no `privacy_profile` line is the floor."""
    if profile == "floor" and not extra:
        return None
    body = dict(extra)
    if profile != "floor":
        body["privacy_profile"] = profile
    return _config(tmp_path, f"{name}.yaml", **body)


def _written(folder):
    [path] = list(Path(folder).rglob("*.dcm"))
    return pydicom.dcmread(str(path))


def _markers(ds):
    """The three marker elements and the code sequence, as plain values."""
    def value(tag):
        el = ds.get(tag)
        if el is None:
            return None
        if el.VR == "SQ":
            return [(i.CodeValue, i.CodingSchemeDesignator) for i in el.value]
        v = el.value
        return list(v) if isinstance(v, pydicom.multival.MultiValue) else v
    return {name: value(tag) for name, tag in (
        ("removed", REMOVED_TAG), ("method", METHOD), ("codes", CODES),
        ("temporal", TEMPORAL))}


def _pipeline(tmp_path, profile="floor", source=None, name="s", **extra):
    """ingest, (load_config), audit, anonymize, export: the written file."""
    source = source or _source(tmp_path / f"{name}_in")
    out = tmp_path / f"{name}_out"
    with DicomSession(str(tmp_path / f"{name}.db")) as session:
        session.ingest(source)
        config = _profile_config(tmp_path, profile, name, **extra)
        if config:
            session.load_config(config)
        session.anonymize(session.audit())
        session.export(str(out), use_compression=False, show_progress=False)
    return _written(out)


# --- M1 ---------------------------------------------------------------------

@pytest.mark.parametrize("profile, expected", [("floor", FLOOR), ("basic", BASIC),
                                               ("none", NONE)])
def test_a_remediated_export_says_yes_and_names_its_policy(tmp_path, profile, expected):
    """M1. Kills: no stamp; the tool spelled other than `isocenter/<v>`
    (the fingerprint's N2 reads exactly that); the fingerprint recomputed
    or cut to another width; a CID 7050 code written (Q2)."""
    ds = _pipeline(tmp_path, profile)
    assert _markers(ds)["removed"] == "YES"
    assert _markers(ds)["method"] == ours(*expected)
    assert _markers(ds)["codes"] is None, "no CID 7050 code (Q2)"
    assert ds[METHOD].VR == "LO" and ds[REMOVED_TAG].VR == "CS"


def test_the_label_is_the_policy_the_scan_ran_under(tmp_path):
    """A policy this session audited under (`audit(config_path=)`) is
    accepted, and its label and fingerprint are written, not the policy in
    force's. Kills: the in-force label written in place of the recorded
    one; the accepted set missing the session's scanned policies (reviewer
    attack 2: the marker would be withheld)."""
    out = tmp_path / "out"
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(_source(tmp_path / "in"))
        report = session.audit(config_path=_config(tmp_path, "b.yaml",
                                                   privacy_profile="basic"))
        session.anonymize(report)
        assert session.configuration._scan_policy().base == FLOOR[0], \
            "setup: the floor is in force"
        session.export(str(out), use_compression=False, show_progress=False)
    assert _markers(_written(out))["method"] == ours(*BASIC)
    assert _markers(_written(out))["removed"] == "YES"


def test_a_cleared_status_is_applied_in_full_too(tmp_path):
    """A re-audit after the pass finds nothing and records CLEARED: the
    policy was applied in full all the same, so the file says so. Kills:
    the predicate accepting REMEDIATED only."""
    out = tmp_path / "out"
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(_source(tmp_path / "in"))
        session.anonymize(session.audit())
        session.audit()
        patient = session.store.patients[0]
        assert {e.phi_status for e in (patient, patient.studies[0],
                                       *_instances(session))} == {PhiStatus.CLEARED}, \
            "setup"
        session.export(str(out), use_compression=False, show_progress=False)
    assert _markers(_written(out))["removed"] == "YES"
    assert _markers(_written(out))["method"] == ours(*FLOOR)


# --- M2 ---------------------------------------------------------------------

def _no_audit(session, tmp_path):
    pass


def _declined(session, tmp_path):
    """One instance finding not handed in (#553): the instance reads
    IDENTIFIED."""
    report = session.audit()
    left = next(f for f in report.findings
                if f.entity_type == "Instance" and f.tag == "0008,0080")
    session.anonymize([f for f in report.findings if f is not left])
    [inst] = _instances(session)
    assert inst.phi_status is PhiStatus.IDENTIFIED, "setup"


def _instance_edited(session, tmp_path):
    session.anonymize(session.audit())
    _instances(session)[0].set_attr("0008,1030", "Edited after the pass")


def _patient_edited(session, tmp_path):
    """#767's shape: an owner field set after the pass. Only the patient
    changes; the instance still reads REMEDIATED."""
    session.anonymize(session.audit())
    session.store.patients[0].patient_name = "Doe^Real"
    [inst] = _instances(session)
    assert inst.phi_status is PhiStatus.REMEDIATED, "setup: only the patient moved"


def _series_edited(session, tmp_path):
    """Review of L12, F2: a Series field set back after the pass. No status
    is recorded on a Series, so none goes stale, and the instance, its
    study and its patient still read REMEDIATED beside the source Series
    Instance UID."""
    session.anonymize(session.audit())
    [inst] = _instances(session)
    inst_series = session.store.patients[0].studies[0].series[0]
    inst_series.series_instance_uid = pydicom.dcmread(
        get_testdata_file("CT_small.dcm")).SeriesInstanceUID
    assert inst.phi_status is PhiStatus.REMEDIATED, "setup: no status moved"


def _with_a_referenced_image(ds):
    item = Dataset()
    item.ReferencedSOPClassUID = ds.SOPClassUID
    item.ReferencedSOPInstanceUID = "1.2.3.4"
    ds.ReferencedImageSequence = Sequence([item])


def _nested_edited(session, tmp_path):
    """Review of L12, F3: a name written into a nested item after the pass.
    The item's revision moves; its instance's status does not."""
    session.anonymize(session.audit())
    [inst] = _instances(session)
    inst.sequences["0008,1140"].items[0].set_attr("0010,0010", "Doe^Nested")
    assert inst.phi_status is PhiStatus.REMEDIATED, "setup: no status moved"


_nested_edited.source_edit = _with_a_referenced_image


def _owner_status_moved(level):
    """The patient's, or the study's, status alone off REMEDIATED, the
    instance's left as the pass recorded it. No pipeline gives that today
    (a finding on an owner not handed in demotes its instances too, #624),
    so it is recorded directly: the predicate must read each of the three.
    Kills: the predicate reading the instance only, or skipping one owner."""
    def step(session, tmp_path):
        session.anonymize(session.audit())
        patient = session.store.patients[0]
        owner = patient if level == "patient" else patient.studies[0]
        owner.record_phi_status(PhiStatus.IDENTIFIED)
        [inst] = _instances(session)
        assert inst.phi_status is PhiStatus.REMEDIATED, "setup"
    step.__name__ = f"_{level}_status_moved"
    return step


def _mixed_policies(session, tmp_path):
    """The patient's status recorded under one policy this session audited
    under, its instance's under another: both accepted, and still no
    single policy the file can name. Kills: the one-fingerprint check."""
    basic = session.audit(config_path=_config(tmp_path, "b.yaml",
                                              privacy_profile="basic"))._scan_policy
    session.anonymize(session.audit())
    session.store.patients[0].record_phi_status(PhiStatus.REMEDIATED, basic)
    [inst] = _instances(session)
    assert inst.phi_status is PhiStatus.REMEDIATED, "setup"
    assert inst.phi_status_policy.fingerprint != basic.fingerprint, "setup"


def _series_finding_left(session, tmp_path):
    """After L10: a report with its Series findings filtered out leaves the
    Series' instances IDENTIFIED (f54deaa1's pass-end demotion), and the
    file would carry the source Series Instance UID."""
    report = session.audit()
    assert any(f.entity_type == "Series" for f in report.findings), "setup"
    session.anonymize([f for f in report.findings if f.entity_type != "Series"])
    [inst] = _instances(session)
    assert inst.phi_status is PhiStatus.IDENTIFIED, "setup"


def _instances(session):
    return [i for p in session.store.patients for st in p.studies
            for se in st.series for i in se.instances]


#: #767, open: an edit after the pass that moves no status the marker
#: reads. An owner field assigned directly (measured:
#: `Patient.patient_name = ...` leaves `_revision` where it was), a Series
#: field (no Series records a status), or a nested item (its revision
#: moves, its instance's status does not): the three entities still read
#: REMEDIATED and the file says YES beside the value set back. The marker
#: rests on the status and is as honest as it is. Owner ruling: #767's PR
#: makes each of these edits mark the containing instances changed; strict,
#: so that fix turns these red, and whichever of L12 and #767 lands second
#: removes all three marks.
_EDIT_UNTRACKED = pytest.mark.xfail(
    strict=True, reason="#767: an edit after the pass that moves no status")


@pytest.mark.parametrize("step", [_no_audit, _declined, _instance_edited,
                                  pytest.param(_patient_edited, marks=_EDIT_UNTRACKED),
                                  pytest.param(_series_edited, marks=_EDIT_UNTRACKED),
                                  pytest.param(_nested_edited, marks=_EDIT_UNTRACKED),
                                  _owner_status_moved("patient"),
                                  _owner_status_moved("study"),
                                  _mixed_policies, _series_finding_left],
                         ids=["never_audited", "a_finding_left_open",
                              "the_instance_edited_after", "the_patient_edited_after",
                              "the_series_edited_after", "a_nested_item_edited_after",
                              "the_patient_not_remediated", "the_study_not_remediated",
                              "two_policies_in_one_file", "a_series_finding_left_open"])
def test_no_marker_where_the_policy_was_not_applied_in_full(tmp_path, step):
    """M2 (a)-(d), (f). Kills: the predicate reading the instance only
    (the patient and study cases); reading a status without its revision;
    the Series seam (a marker beside a source Series Instance UID)."""
    out = tmp_path / "out"
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(_source(tmp_path / "in",
                               edit=getattr(step, "source_edit", None)))
        step(session, tmp_path)
        session.export(str(out), use_compression=False, show_progress=False)
    markers = _markers(_written(out))
    assert markers == {"removed": None, "method": None, "codes": None,
                       "temporal": None}


def test_no_marker_on_a_status_recorded_under_another_policy(tmp_path):
    """M2 (e): remediated under a narrow policy, reopened bare under the
    floor, not re-audited. The #555 notice fires and the file says
    nothing. Kills: the predicate reading `phi_status` without its policy."""
    db = str(tmp_path / "s.db")
    narrow = _config(tmp_path, "narrow.yaml", privacy_profile="none",
                     phi_tags={"0008,1010": {"action": "REMOVE"}})
    with DicomSession(db) as session:
        session.ingest(_source(tmp_path / "in"))
        session.load_config(narrow)
        session.anonymize(session.audit())
        session.save(sync=True)
    out = tmp_path / "out"
    with DicomSession(db) as session:
        session.export(str(out), use_compression=False, show_progress=False)
        notices = [d for _, _, d in session.store_backend.get_audit_errors()
                   if NOTICE in d]
    assert len(notices) == 1
    assert _markers(_written(out))["removed"] is None
    assert _markers(_written(out))["method"] is None


def test_no_marker_on_a_status_with_no_policy(tmp_path):
    """A status with no recorded policy -- remediated from a findings list
    that is not a whole `audit()` report, in a session that did not scan
    (#555's legacy row) -- names no policy the file could name. Kills: the
    predicate accepting a status whose policy is None."""
    db = str(tmp_path / "s.db")
    with DicomSession(db) as session:
        session.ingest(_source(tmp_path / "in"))
        session.save(sync=True)
        report = session.audit()
    out = tmp_path / "out"
    with DicomSession(db) as session:
        session.anonymize(list(report.findings))
        [inst] = _instances(session)
        assert inst.phi_status is PhiStatus.REMEDIATED, "setup"
        assert inst.phi_status_policy is None, "setup"
        session.export(str(out), use_compression=False, show_progress=False)
    assert _markers(_written(out))["removed"] is None
    assert _markers(_written(out))["method"] is None


# --- M3, M4 -----------------------------------------------------------------

def test_another_tools_markers_are_kept_and_ours_appended(tmp_path):
    """M3. Kills: ours replacing theirs; theirs dropped; a code item
    written or the source's dropped."""
    ds = _pipeline(tmp_path, "floor", source=_source(tmp_path / "in", prior=True))
    markers = _markers(ds)
    assert markers["removed"] == "YES"
    assert markers["method"] == ["OtherTool 3.2", "site profile 7", ours(*FLOOR)]
    assert markers["codes"] == [("113100", "DCM")]

    ds = _pipeline(tmp_path, "floor", name="no",
                   source=_source(tmp_path / "no_in", identity_removed="NO"))
    assert _markers(ds)["removed"] == "YES", "a source NO is replaced"

    def empty_method(dataset):
        dataset.DeidentificationMethod = ""
    ds = _pipeline(tmp_path, "floor", name="empty",
                   source=_source(tmp_path / "empty_in", edit=empty_method))
    assert _markers(ds)["method"] == ours(*FLOOR), \
        "a zero-length source value is no step to keep"


def test_re_exporting_our_own_export_adds_no_second_value(tmp_path):
    """M4. Our export re-ingested and passed again under the same policy
    and release: one value of ours, not two. Under another policy: both,
    in order. Kills: the append without the last-value check; the check
    reading the first value."""
    first = tmp_path / "first_out"
    with DicomSession(str(tmp_path / "first.db")) as session:
        session.ingest(_source(tmp_path / "in"))
        session.anonymize(session.audit())
        session.export(str(first), use_compression=False, show_progress=False)
    assert _markers(_written(first))["method"] == ours(*FLOOR)

    same = _pipeline(tmp_path, "floor", source=str(first), name="same")
    assert _markers(same)["method"] == ours(*FLOOR)

    other = _pipeline(tmp_path, "basic", source=str(first), name="other")
    assert _markers(other)["method"] == [ours(*FLOOR), ours(*BASIC)]

    # Back under the floor: the last value is basic's, so the floor is a
    # step again, although it is also the first value.
    other_folder = tmp_path / "other_out"
    back = _pipeline(tmp_path, "floor", source=str(other_folder), name="back")
    assert _markers(back)["method"] == [ours(*FLOOR), ours(*BASIC), ours(*FLOOR)]


# --- M5, M6, M7 -------------------------------------------------------------

def test_write_tree_writes_no_marker(tmp_path):
    """M5. The same graph after the pass: `export()` writes the markers,
    `write_tree()` none, and the store holds none after the export (they
    never enter the graph). Kills: the stamp placed in
    `export_stamp_attributes` or `_create_ds`, which both doors share, or
    set on the instance."""
    via_session, via_tree = tmp_path / "session", tmp_path / "tree"
    db = str(tmp_path / "s.db")
    with DicomSession(db) as session:
        session.ingest(_source(tmp_path / "in"))
        session.anonymize(session.audit())
        session.export(str(via_session), use_compression=False,
                       show_progress=False)
        for patient in session.store.patients:
            DicomExporter.write_tree(patient, str(via_tree), show_progress=False)
        [inst] = _instances(session)
        assert not {"0012,0062", "0012,0063", "0028,0303"} & set(inst.attributes)
        session.save(sync=True)
    assert _markers(_written(via_session))["removed"] == "YES"
    assert _markers(_written(via_tree)) == {"removed": None, "method": None,
                                            "codes": None, "temporal": None}
    with sqlite3.connect(db) as conn:
        [(blob,)] = conn.execute("SELECT attributes_json FROM instances").fetchall()
    assert "0012,0062" not in blob and "0012,0063" not in blob


def test_an_external_profile_is_named_not_located(tmp_path):
    """M6. An external profile's base is a filesystem path, and a path in
    exported data is the operator's directory layout (#655). Kills: the
    path, or the file's name, written."""
    profile = tmp_path / "private_site" / "site-profile-7.yaml"
    profile.parent.mkdir()
    profile.write_text(json.dumps({"phi_tags": {
        "0010,0010": {"action": "REMOVE"},
        "0008,0080": {"action": "EMPTY"}}}), encoding="utf-8")
    ds = _pipeline(tmp_path, str(profile))
    method = _markers(ds)["method"]
    assert re.fullmatch(rf"isocenter/{re.escape(VERSION)}; external profile; "
                        r"v1:[0-9a-f]{8}", method), method

    def texts(dataset):
        for el in dataset:
            if el.VR == "SQ":
                for item in el.value:
                    yield from texts(item)
            else:
                yield str(el.value)
    found = [t for t in texts(ds) if str(tmp_path) in t or "site-profile-7" in t
             or "private_site" in t]
    assert found == []


#: The floor with one override is its own policy. Each prefix measured at
#: f54deaa1; the label is the floor's, since the file names no profile.
@pytest.mark.parametrize("rule, expected", [
    ({"0012,0063": {"action": "REMOVE"}},
     {"removed": "YES", "method": None}),
    ({"0012,0062": {"action": "EMPTY"}},
     {"removed": "", "method": "v1:153c1dde"}),
    ({"0012,0062": {"action": "KEEP"}},
     {"removed": "NO", "method": "v1:bec9eb34"}),
    ({"0028,0303": {"action": "KEEP"}},
     {"removed": "YES", "method": "v1:a31f9c17", "temporal": None}),
], ids=["remove_method", "empty_removed", "keep_a_source_no", "keep_temporal"])
def test_a_rule_on_a_marker_tag_decides(tmp_path, rule, expected):
    """M7. The user's configuration decides: any rule on a marker tag, KEEP
    included, and Isocenter does not stamp that tag; the others are still
    stamped. The source carries `(0012,0062) NO`, which EMPTY leaves
    zero-length, and KEEP leaves `NO`. Kills: stamping over the
    user's rule; one rule suppressing every marker."""
    source = _source(tmp_path / "in", identity_removed="NO")
    ds = _pipeline(tmp_path, "floor", source=source, phi_tags=rule)
    markers = _markers(ds)
    want = {k: (ours(FLOOR[0], v) if k == "method" and v else v)
            for k, v in expected.items()}
    assert {k: markers[k] for k in want} == want
    if "temporal" not in expected:
        assert markers["temporal"] == "MODIFIED"


# --- M8 ---------------------------------------------------------------------

@pytest.mark.parametrize("executor", ["threads", "processes"])
def test_the_worker_writes_the_same_markers_under_both_executors(
        tmp_path, monkeypatch, executor):
    """M8. The markers are decided in the parent and carried to the worker:
    a process worker's lightweight copy holds no status to decide from.
    Kills: the marker computed from worker-side state."""
    if executor == "processes":
        monkeypatch.setenv("ISOCENTER_FORCE_PROCESSES", "1")
        monkeypatch.delenv("ISOCENTER_FORCE_THREADS", raising=False)
    out = tmp_path / "out"
    with DicomSession(str(tmp_path / "s.db")) as session:
        load_fixed_secret(session)
        session.ingest(_source(tmp_path / "in", prior=True))
        session.anonymize(session.audit())
        session.export(str(out), use_compression=False, show_progress=False)
    assert _markers(_written(out)) == {
        "removed": "YES",
        "method": ["OtherTool 3.2", "site profile 7", ours(*FLOOR)],
        "codes": [("113100", "DCM")], "temporal": "MODIFIED"}


# --- M9 ---------------------------------------------------------------------

#: The WFDB files the ECG cohort member exports under the floor and
#: `FIXED_A`, with `isocenter/<version>` read as `isocenter/<V>`: (bytes,
#: sha256), measured at f54deaa1, before this change.
WFDB_AT_BASE = {
    ".annotations.json": (445, "beb221a1f838d8412861b49a5bf8ee94439f23374b1e769f55aa61d8f1de7551"),
    ".dat": (16000, "d645e12558109f881b09380b5284a9736a30179a7ceb7e074f873350882f9017"),
    ".hea": (745, "08e3e2bfa58897e057e7587cee58a2465af1308aef5f71ec5132d837d00e2182"),
}


def test_wfdb_output_is_unchanged(tmp_path):
    """M9. A WFDB header has no such field, and the Murmur bridge already
    says `source: isocenter/<version>`. Kills: the stamp leaking into the
    WFDB arm."""
    (tmp_path / "in").mkdir()
    shutil.copy(REPO / "fingerprint" / "cohort" / "ecg" / "ecg-1.dcm",
                tmp_path / "in" / "ecg-1.dcm")
    with DicomSession(str(tmp_path / "s.db")) as session:
        load_fixed_secret(session)
        session.ingest(str(tmp_path / "in"))
        session.anonymize(session.audit())
        session.export(str(tmp_path / "wfdb"), format="wfdb")
    found = {}
    for path in (tmp_path / "wfdb").rglob("*"):
        if path.is_file():
            data = path.read_bytes().replace(f"isocenter/{VERSION}".encode(),
                                             b"isocenter/<V>")
            suffix = ".annotations.json" if path.name.endswith(
                ".annotations.json") else path.suffix
            found[suffix] = (len(data), hashlib.sha256(data).hexdigest())
    assert found == WFDB_AT_BASE


# --- M10 --------------------------------------------------------------------

#: A DA the floor has no rule for (Expiry Date), so a nested copy of it is
#: left as found.
UNRULED_DA = 0x00141020


def _nested_unruled_date(ds):
    item = Dataset()
    item.ReferencedSOPClassUID = ds.SOPClassUID
    item.ReferencedSOPInstanceUID = "1.2.826.0.1.3680043.554.1"
    item.add_new(UNRULED_DA, "DA", "20040119")
    ds.ReferencedImageSequence = Sequence([item])


def _private_date(ds):
    ds.add_new(0x00330010, "LO", "L12 PRIVATE")
    ds.add_new(0x00331001, "DA", "20040119")


def _unruled_datetime(ds):
    """Study Update DateTime `(0008,041f)`: a DT the floor has no rule for."""
    ds.add_new(0x0008041F, "DT", "20040119120000")


def test_the_study_date_read_is_the_one_the_file_carries(tmp_path):
    """The file's Study Date is stamped from the `Study` (#566, #624), so
    the walk reads the stamps too, not only the instance's own elements.
    Here the Study Date is kept (as found) and the instance's own copy is
    taken out of its dict behind the graph's back: the export still writes
    the owner's date, so the file holds a date as found. Kills: the walk
    reading the instance's attributes without the stamps over them."""
    out = tmp_path / "out"
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(_source(tmp_path / "in"))
        session.load_config(_config(tmp_path, "keep.yaml", phi_tags={
            "0008,0020": {"action": "KEEP"}}))
        session.anonymize(session.audit())
        [inst] = _instances(session)
        del inst.attributes["0008,0020"]
        assert inst.phi_status is PhiStatus.REMEDIATED, "setup: no revision moved"
        session.export(str(out), use_compression=False, show_progress=False)
    ds = _written(out)
    assert ds.StudyDate == "20040119", "setup: the owner's date is written"
    assert _markers(ds)["removed"] == "YES"
    assert _markers(ds)["temporal"] is None


@pytest.mark.parametrize("profile, extra, edit, prior, expected", [
    ("floor", {}, None, False, "MODIFIED"),
    ("basic", {}, None, False, "REMOVED"),
    ("floor", {"phi_tags": {"0008,0021": {"action": "KEEP"}}}, None, False, None),
    ("floor", {"phi_tags": {"0008,0021": {"action": "KEEP"}}}, None, True, "MODIFIED"),
    ("floor", {}, _nested_unruled_date, False, None),
    ("floor", {"remove_private_tags": False}, _private_date, False, None),
    ("floor", {}, _unruled_datetime, False, None),
], ids=["floor_shifts", "basic_removes", "a_kept_series_date",
        "a_kept_date_under_a_source_modified", "a_nested_date_as_found",
        "a_private_date_as_found", "a_datetime_as_found"])
def test_the_temporal_marker_follows_the_files_dates(
        tmp_path, profile, extra, edit, prior, expected):
    """M10 (Q3 arm a). CT_small's six DA elements under the floor: Study
    Date shifted, Instance Creation, Series and Content Date the dummy,
    Acquisition Date and Birth Date empty -- `MODIFIED`. Under `basic`
    the Study Date is emptied too -- `REMOVED`. A date left as found, at
    the top level, nested, or private with its VR recorded, writes
    nothing, and a source's `MODIFIED` stays. Kills: the walk skipping
    nested items or private tags; the dummy read as found; `UNMODIFIED`
    written; the source value overwritten when nothing is determined."""
    source = _source(tmp_path / "in", prior=prior, edit=edit)
    ds = _pipeline(tmp_path, profile, source=source, **extra)
    assert _markers(ds)["removed"] == "YES", "setup: the pass was whole"
    assert _markers(ds)["temporal"] == expected


# --- M13: a declared burned-in annotation (review of L12, F1) ---------------

def _burned_in(value):
    def edit(ds):
        ds.BurnedInAnnotation = value
    return edit


@pytest.mark.parametrize("value", ["YES", "yes"])
def test_a_declared_burned_in_annotation_holds_back_yes(tmp_path, value):
    """Owner ruling on the review of L12 (F1). PS3.3 C.7.1.1 defines
    `(0012,0062) YES` as identity removed from the Attributes *and the Pixel
    Data*, and Isocenter does not read the pixels: a file that itself says
    `(0028,0301) YES` (text drawn into the pixels, by the scanner's own
    account) cannot also say the identity is gone. YES is held back; the
    method value, which only names what ran, and the temporal marker are
    written as ever. `yes` is not a valid CS, and is read as the scanner
    meant it. Kills: the check dropped; a case-sensitive comparison;
    everything held back, not YES alone."""
    ds = _pipeline(tmp_path, source=_source(tmp_path / "in",
                                            edit=_burned_in(value)))
    assert ds.BurnedInAnnotation == value, "setup: the flag reaches the file"
    assert _markers(ds) == {"removed": None, "method": ours(*FLOOR),
                            "codes": None, "temporal": "MODIFIED"}


def test_a_burned_in_annotation_of_no_leaves_yes(tmp_path):
    """The other direction: `(0028,0301) NO`, the flag `redact()` writes on
    the pixels it cleared, is no declaration of burned-in text, so the file
    says YES. Kills: holding back on the element's presence, not its
    value."""
    ds = _pipeline(tmp_path, source=_source(tmp_path / "in",
                                            edit=_burned_in("NO")))
    assert ds.BurnedInAnnotation == "NO", "setup"
    assert _markers(ds)["removed"] == "YES"


def test_held_back_yes_leaves_a_source_no_as_it_was(tmp_path):
    """Held back means not written: a source `(0012,0062) NO` stays `NO`,
    and nothing else is put in its place. Kills: YES held back by writing
    `NO`, or by deleting the source's value."""
    ds = _pipeline(tmp_path, source=_source(tmp_path / "in", identity_removed="NO",
                                            edit=_burned_in("YES")))
    assert _markers(ds)["removed"] == "NO"


# --- M11, M12 ---------------------------------------------------------------

def test_the_dead_deid_stamper_is_gone():
    """M11. Deleted, not deprecated (one spelling per behaviour): it wrote
    `113100`, the claim Q2 declines, and nothing called it."""
    assert not hasattr(remediation.RemediationService, "add_global_deid_tags")
    assert not hasattr(services, "CODE_BASIC_PROFILE")
    assert not hasattr(services, "CODE_CLEAN_PIXEL")


def test_every_method_value_fits_an_lo():
    """M12. LO holds 64 characters. The longest built-in label under a
    17-character version is exactly 64; a wider hex prefix, or any label
    longer than the floor's, overflows, and pydicom refuses or truncates
    at write. Kills: a format or label that overflows LO."""
    from pydicom.config import RAISE
    from pydicom.valuerep import validate_value
    version = "10.10.10rc10.dev1"
    assert len(version) == 17
    lengths = {}
    for base in (*configuration.profiles.PRIVACY_PROFILES, "none",
                 "floor over basic@2026c", "/Users/someone/secret/profile.yaml"):
        value = configuration._deid_method_value(
            ScanPolicy("v1:" + "f" * 64, base), version)
        validate_value("LO", value, RAISE)
        lengths[base] = len(value)
    assert lengths == {"basic@2026c": 53, "none": 46,
                       "floor over basic@2026c": 64,
                       "/Users/someone/secret/profile.yaml": 58}
