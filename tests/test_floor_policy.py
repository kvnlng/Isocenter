"""A session that has loaded no configuration de-identifies against a floor
policy (#495), and the floor is the table the scaffold is generated from.

Measured on `168fdd6` with pydicom's `CT_small.dcm`: `Session` -> `ingest`
-> `audit()` -> `anonymize()` -> `export()` wrote a file carrying Study ID
`1CT1`, Series/Acquisition/Content Date `19970430`, Station Name
`CT01_OC0`, Institution Name `JFK IMAGING CENTER` and Study Time `072730`,
graded `PASS`, with a method line reading `Session defaults: 6 tag rules`
-- six rules the scan never applied, because `audit()` read
`configuration.phi_tags` (`{}`) while the report read the shipped
`phi_tags.json`. The documented Quick Start was worse: `create_config` ->
`load_config` -> `anonymize` -> `export` on the same file raised
`ExportError ... ['[Type 1 Error] Missing 0008,0030 in Common']` and wrote
nothing, because the basic profile removed Study Time and `IODValidator`
called it Type 1 (it is Type 2, PS3.3 C.7.2.1).

Every test here names the mutant it kills in its docstring.
"""
import json
import logging
import os
import shutil

import pydicom
import pydicom.data
import pytest
import yaml
from pydicom.dataset import Dataset

from isocenter.session import DicomSession as Session
from isocenter.validation import IODValidator


CT_IMAGE_STORAGE = "1.2.840.10008.5.1.4.1.1.2"


def _ct_dataset_with_empty_study_time() -> Dataset:
    """A CT dataset carrying every Common/CTImage element the validator
    checks, with Study Time present and empty -- the shape the floor's
    EMPTY action leaves behind."""
    ds = Dataset()
    ds.file_meta = pydicom.dataset.FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = CT_IMAGE_STORAGE
    ds.SOPClassUID = CT_IMAGE_STORAGE
    ds.SOPInstanceUID = "1.2.3.4"
    ds.StudyDate = "20030525"
    ds.StudyTime = ""
    ds.Modality = "CT"
    ds.SeriesInstanceUID = "1.2.3"
    ds.SliceThickness = "1"
    ds.KVP = "120"
    ds.ImagePositionPatient = [0, 0, 0]
    ds.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
    ds.PixelSpacing = [0.5, 0.5]
    return ds


def test_the_floor_does_not_reach_the_validator_as_a_type_1_gap():
    """Study Time is Type 2 in General Study (PS3.3 C.7.2.1): present and
    empty is conformant. Kills `_MODULE_DEFINITIONS["Common"]["0008,0030"]`
    reverted to `'1'`, whose Type-1 arm rejects the empty value and made
    every CT export on the documented path raise."""
    ds = _ct_dataset_with_empty_study_time()

    assert IODValidator.validate(ds) == []

    # The positive control: the same element *absent* is still an error,
    # so the change is Type 1 -> Type 2 and not "stop checking it".
    del ds.StudyTime
    assert IODValidator.validate(ds) == ["[Type 2 Error] Missing 0008,0030 in Common"]


# ---------------------------------------------------------------------------
# The floor itself
# ---------------------------------------------------------------------------

def test_the_floor_is_the_basic_profile_plus_the_research_defaults():
    """One table, derived. Kills a hand-maintained `FLOOR_POLICY` that
    drifts from `{**BASIC_PROFILE, **RESEARCH_DEFAULTS}`, an uppercase
    key in either, Station Name missing from the profile, Study Time
    left at REMOVE, and any research default whose action changes."""
    from isocenter.profiles import BASIC_PROFILE, FLOOR_POLICY, RESEARCH_DEFAULTS

    assert FLOOR_POLICY == {**BASIC_PROFILE, **RESEARCH_DEFAULTS}
    assert all(tag == tag.lower() for tag in FLOOR_POLICY)
    # Patient's Age is a basic rule since #547, so all three research
    # defaults override one and the floor adds nothing.
    assert len(FLOOR_POLICY) == 620

    assert RESEARCH_DEFAULTS["0008,0020"]["action"] == "JITTER"
    assert RESEARCH_DEFAULTS["0010,0040"]["action"] == "KEEP"
    assert RESEARCH_DEFAULTS["0010,1010"]["action"] == "KEEP"
    assert set(RESEARCH_DEFAULTS) == {"0008,0020", "0010,0040", "0010,1010"}

    # The two profile edits the ruling and the export need. Station Name
    # is X/Z/D in Table E.1-1, so EMPTY since #547.
    assert BASIC_PROFILE["0008,1010"]["action"] == "EMPTY"      # Station Name
    assert BASIC_PROFILE["0008,0030"]["action"] == "EMPTY"      # Study Time
    assert len(BASIC_PROFILE) == 620

    # Derived, not aliased: the floor's entries are not the profile's
    # objects, so an edit to one cannot rewrite the other.
    assert FLOOR_POLICY["0010,0010"] is not BASIC_PROFILE["0010,0010"]


def test_a_bare_configuration_seeds_its_own_copy_of_the_floor():
    """`IsocenterConfiguration()` with no `phi_tags` carries the floor, and
    a fresh copy of it. Kills the seed reverting to `{}`, and a seed that
    hands every session the same dict (one session's `set_phi_tag` would
    then edit the next session's policy, and the module table)."""
    from isocenter.configuration import IsocenterConfiguration
    from isocenter.profiles import FLOOR_POLICY

    first = IsocenterConfiguration()
    second = IsocenterConfiguration()

    assert first.phi_tags == FLOOR_POLICY
    assert first.phi_tags is not FLOOR_POLICY
    assert first.phi_tags is not second.phi_tags
    assert first.phi_tags["0010,0010"] is not FLOOR_POLICY["0010,0010"]

    # An explicit policy is honoured as given.
    assert IsocenterConfiguration(phi_tags={"0018,1030": "Protocol"}).phi_tags == {
        "0018,1030": "Protocol"}


# ---------------------------------------------------------------------------
# The bare session
# ---------------------------------------------------------------------------

def _bare_ct_instance():
    """A hand-built CT instance carrying the four tags #495 measured on
    the exported file."""
    from isocenter.entities import Patient, Study, Series, Instance

    patient = Patient("PAT495", "Doe^Jane")
    study = Study("1.2.826.0.1.3680043.8.498.1", "20030525")
    series = Series("1.2.826.0.1.3680043.8.498.2", "CT", 1)
    instance = Instance("1.2.826.0.1.3680043.8.498.3", "/nonexistent/one.dcm", 1)
    instance.attributes.update({
        "0020,0010": "1CT1",                 # Study ID
        "0008,1010": "CT01_OC0",             # Station Name
        "0008,0080": "JFK IMAGING CENTER",   # Institution Name
        "0008,0021": "19970430",             # Series Date
    })
    series.instances.append(instance)
    study.series.append(series)
    patient.studies.append(study)
    return patient, instance


def test_a_bare_session_audits_against_the_floor(tmp_path, caplog):
    """`Session()` with no config raises a finding for each of the four
    tags the issue measured on disk, and does not warn that no tags are
    defined. Kills the seed reverting to `{}` (three hardcoded findings
    and the warning), and a floor without Station Name."""
    patient, instance = _bare_ct_instance()

    with Session(str(tmp_path / "bare.db")) as session:
        session.store.patients.append(patient)
        with caplog.at_level(logging.WARNING, logger="isocenter"):
            report = session.audit()

    flagged = {f.tag for f in report if f.entity_uid == instance.sop_instance_uid}
    assert {"0020,0010", "0008,1010", "0008,0080", "0008,0021"} <= flagged, flagged
    assert "No PHI tags defined" not in caplog.text


def _ct_small_into(folder):
    src = pydicom.data.get_testdata_file("CT_small.dcm")
    os.makedirs(folder, exist_ok=True)
    shutil.copy(src, os.path.join(folder, "CT_small.dcm"))
    return pydicom.dcmread(src)


def _exported_dicoms(folder):
    return [os.path.join(root, name)
            for root, _, names in os.walk(folder)
            for name in names if name.endswith(".dcm")]


#: Keyword -> what the floor leaves on disk. `None` means the element is
#: absent; `""` means present and empty. Since #547 every code with a `Z`
#: arm in PS3.15 Table E.1-1 empties rather than removes, so only the
#: `X/D` pair (Series Date and Time) is absent.
_CT_SMALL_AFTER_THE_FLOOR = {
    "StudyID": "",               # Z
    "SeriesDate": None,          # X/D
    "AcquisitionDate": "",       # X/Z
    "ContentDate": "",           # Z/D
    "StationName": "",           # X/Z/D
    "InstitutionName": "",       # X/Z/D
    "ContentTime": "",           # Z/D
    "SeriesTime": None,          # X/D
    "AcquisitionTime": "",       # X/Z
    "StudyTime": "",             # Z
    "StudyDescription": "",      # X, emptied for folder naming
}


def test_a_bare_session_export_on_ct_small_carries_none_of_the_issues_tags(tmp_path):
    """The #495 table, end to end, on the bare path. Kills Study Time at
    REMOVE (the validator refuses the absent Type 2 element, export
    writes 0 of 1), the validator back at '1' (it refuses the empty
    value), and any single floor entry for these tags dropped (its value
    reaches the file)."""
    original = _ct_small_into(str(tmp_path / "in"))

    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        session.anonymize(session.audit())
        summary = session.export(str(tmp_path / "out"), use_compression=False)

    assert summary.written == 1, summary.failures
    written = _exported_dicoms(str(tmp_path / "out"))
    assert len(written) == 1
    ds = pydicom.dcmread(written[0])

    for keyword, expected in _CT_SMALL_AFTER_THE_FLOOR.items():
        assert keyword in original, f"fixture drift: CT_small has no {keyword}"
        if expected is None:
            assert keyword not in ds, (
                f"{keyword} reached the export as {ds[keyword].value!r}")
        else:
            assert keyword in ds and str(ds[keyword].value) == expected, (
                f"{keyword} is {ds.get(keyword)!r}, expected {expected!r}")

    # Study Date is jittered, not removed: present and different.
    assert "StudyDate" in ds
    assert str(ds.StudyDate) != str(original.StudyDate)
    # The research defaults keep Sex and Age.
    assert str(ds.PatientSex) == str(original.PatientSex)
    assert str(ds.PatientAge) == str(original.PatientAge)
    assert str(ds.PatientID) != str(original.PatientID)


def test_type_2_attributes_survive_as_zero_length(tmp_path):
    """Accession Number, Referring Physician's Name, Study ID and Patient's
    Birth Date are Type 2 in the Patient and General Study modules, and
    PS3.15 Table E.1-1 gives each `Z`. On 0.9.7 the basic profile removed
    all four, so every bare-session export was missing required elements,
    and `IODValidator` -- which does not check them -- said nothing: #503's
    Study Time defect at four more tags (#547).

    Kills: any one of the four flipped back to REMOVE."""
    original = _ct_small_into(str(tmp_path / "in"))
    type_2 = ("AccessionNumber", "ReferringPhysicianName", "StudyID",
              "PatientBirthDate")
    for keyword in type_2:
        assert keyword in original, f"fixture drift: CT_small has no {keyword}"

    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        session.anonymize(session.audit())
        summary = session.export(str(tmp_path / "out"), use_compression=False)

    assert summary.written == 1, summary.failures
    ds = pydicom.dcmread(_exported_dicoms(str(tmp_path / "out"))[0])
    for keyword in type_2:
        assert keyword in ds, f"{keyword} is absent: a Type 2 element was removed"
        assert str(ds[keyword].value) == "", f"{keyword} is {ds[keyword].value!r}"


def test_the_documented_quick_start_exports_ct_small(tmp_path):
    """`create_config` -> `load_config` -> `audit` -> `anonymize` ->
    `export` on `CT_small.dcm`: the README's Quick Start, end to end, on a
    CT file (#503). It raised `ExportError ... ['[Type 1 Error] Missing
    0008,0030 in Common']` and wrote nothing: the basic profile removed
    Study Time and the validator called it Type 1. No test covered it --
    `test_profile_end_to_end.py` builds Secondary Capture, which has no
    `Common` module in the validator's table. Kills Study Time back at
    REMOVE and the validator back at '1' (both write 0 of 1), and Station
    Name missing from the basic profile (its value reaches the file)."""
    original = _ct_small_into(str(tmp_path / "in"))
    config = tmp_path / "config.yaml"

    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        session.create_config(str(config))
        session.load_config(str(config))
        assert session.configuration.privacy_profile == "basic"
        session.anonymize(session.audit())
        summary = session.export(str(tmp_path / "out"), use_compression=False)

    assert summary.written == 1, summary.failures
    written = _exported_dicoms(str(tmp_path / "out"))
    assert len(written) == 1
    ds = pydicom.dcmread(written[0])

    assert "StudyTime" in ds and str(ds.StudyTime) == ""
    for keyword in ("StationName", "StudyID", "InstitutionName", "SeriesDate"):
        assert keyword in original, f"fixture drift: CT_small has no {keyword}"
        assert not ds.get(keyword), f"{keyword} reached the export as {ds[keyword].value!r}"


def test_a_bare_session_status_and_manifest_after_anonymize(tmp_path):
    """A floor finding is an ordinary instance finding: applied by
    `_apply_single_remediation`, stamped REMEDIATED by its success block,
    and demoted by `apply_remediation`'s pass-end rule when it declines
    (#486). Kills a floor that bypasses `apply_remediation` (no REMEDIATED
    stamp, `anonymized: false` on the clean pass) and a stamping path
    that skips the demotion (`true` after a decline)."""
    from isocenter.entities import PhiStatus

    _ct_small_into(str(tmp_path / "in"))
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        session.anonymize(session.audit())
        instance = session.store.patients[0].studies[0].series[0].instances[0]
        assert instance.phi_status is PhiStatus.REMEDIATED

        session.generate_manifest(str(tmp_path / "m1.json"), format="json")
        with open(tmp_path / "m1.json", encoding="utf-8") as f:
            items = json.load(f)["items"]
        assert [item["anonymized"] for item in items] == [True]

    # One floor finding declined: the audit sees Series Date, then the
    # value is gone before the remediation runs, so REMOVE_TAG matches no
    # arm and `_record_decline` names the instance. A REMOVE rule, because
    # an EMPTY one (Station Name's since #547) writes "" over a missing
    # value rather than declining.
    _ct_small_into(str(tmp_path / "in2"))
    with Session(str(tmp_path / "s2.db")) as session:
        session.ingest(str(tmp_path / "in2"))
        report = session.audit()
        instance = session.store.patients[0].studies[0].series[0].instances[0]
        assert any(f.tag == "0008,0021" for f in report), (
            "the floor did not flag Series Date")
        del instance.attributes["0008,0021"]
        session.anonymize(report)

        assert instance.phi_status is PhiStatus.IDENTIFIED
        session.generate_manifest(str(tmp_path / "m2.json"), format="json")
        with open(tmp_path / "m2.json", encoding="utf-8") as f:
            items = json.load(f)["items"]
        assert [item["anonymized"] for item in items] == [False]


def test_a_lock_after_anonymize_is_refused_on_the_floor_path(tmp_path):
    """#492's refusal holds on the bare path. The floor removes the
    instance's own 0010,0010/0010,0020, so a refusal that read only those
    copies saw nothing: measured, lock -> anonymize -> lock again raised
    nothing and wrote a token holding only `{'0010,0040': 'O'}` over the
    good one, and recovery lost the name and ID. Kills the stash's
    fallback to the patient's name and ID removed (M17): the absent copies
    are then never stashed, so never checked, and both orders stop
    raising."""
    _ct_small_into(str(tmp_path / "in"))
    with Session(str(tmp_path / "s.db")) as session:
        session.enable_reversible_anonymization(str(tmp_path / "k.key"))
        session.ingest(str(tmp_path / "in"))
        inst = session.store.patients[0].studies[0].series[0].instances[0]
        session.lock_identities("1CT1")
        first = session.reversibility_service.recover_original_data(inst)
        assert first["0010,0010"] == "CompressedSamples^CT1"
        assert first["0010,0020"] == "1CT1"

        session.anonymize(session.audit())
        assert "0010,0010" not in inst.attributes, "the floor did not remove the copy"
        patient = session.store.patients[0]
        token = inst.sequences["0400,0500"].items[0].attributes["0400,0510"]
        with pytest.raises(RuntimeError, match=r"0010,0010 \('ANONYMIZED'\)"):
            session.lock_identities(patient.patient_id)
        assert inst.sequences["0400,0500"].items[0].attributes["0400,0510"] == token
        assert session.reversibility_service.recover_original_data(inst) == first

    # The reverse of the documented order, as one call: nothing to stash.
    _ct_small_into(str(tmp_path / "in2"))
    with Session(str(tmp_path / "s2.db")) as session:
        session.enable_reversible_anonymization(str(tmp_path / "k2.key"))
        session.ingest(str(tmp_path / "in2"))
        session.anonymize(session.audit())
        patient = session.store.patients[0]
        inst = patient.studies[0].series[0].instances[0]
        with pytest.raises(RuntimeError, match=patient.patient_id):
            session.lock_identities(patient.patient_id)
        assert "0400,0500" not in inst.sequences


def test_a_re_lock_after_an_instance_only_anonymize_stashes_the_patients_identity(tmp_path):
    """The third route: only the instance findings are applied, so the
    floor removes the instance's own 0010,0010/0010,0020 while the patient
    still holds the originals. Nothing is a replacement, so nothing is
    refused -- and the stash read only the (now absent) copies: measured
    by review of #509, the new token held only `{'0010,0040': 'O'}` and
    recovery lost the name and ID. The stash now takes an absent copy's
    value from the patient, as the no-instances fallback already did.
    Kills that fallback removed (recovery has no name or ID)."""
    _ct_small_into(str(tmp_path / "in"))
    with Session(str(tmp_path / "s.db")) as session:
        session.enable_reversible_anonymization(str(tmp_path / "k.key"))
        session.ingest(str(tmp_path / "in"))
        patient = session.store.patients[0]
        inst = patient.studies[0].series[0].instances[0]
        session.lock_identities("1CT1")

        session.anonymize([f for f in session.audit() if f.entity_type == "Instance"])
        assert "0010,0010" not in inst.attributes, "the floor did not remove the copy"
        assert "0010,0020" not in inst.attributes, "the floor did not remove the copy"
        assert (str(patient.patient_name), patient.patient_id) == (
            "CompressedSamples^CT1", "1CT1"), "the patient was anonymized too"

        session.lock_identities(patient.patient_id)
        again = session.reversibility_service.recover_original_data(inst)
    assert again["0010,0010"] == "CompressedSamples^CT1", again
    assert again["0010,0020"] == "1CT1", again


# ---------------------------------------------------------------------------
# The scaffold, the report, and the configuration object
# ---------------------------------------------------------------------------

def test_the_scaffold_is_generated_from_the_floor(tmp_path):
    """`create_config` on a bare session writes exactly `RESEARCH_DEFAULTS`
    under `privacy_profile: basic`, and loading that file yields the
    floor. Kills `_scaffold_phi_tags` reverting to its own table (the
    scaffold and the floor can then disagree), and a REMOVE entry leaking
    into the scaffold."""
    from isocenter.config_manager import ConfigLoader
    from isocenter.profiles import FLOOR_POLICY, RESEARCH_DEFAULTS

    config = tmp_path / "scaffold.yaml"
    with Session(str(tmp_path / "s.db")) as session:
        session.create_config(str(config))

    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert data["privacy_profile"] == "basic"
    assert data["phi_tags"] == RESEARCH_DEFAULTS

    tags, _, _, _, profile = ConfigLoader.load_unified_config(str(config))
    assert profile == "basic"
    assert tags == FLOOR_POLICY


def _report_method_line(session, tmp_path, name):
    path = tmp_path / f"{name}.md"
    session.generate_report(str(path))
    lines = [line for line in path.read_text(encoding="utf-8").splitlines()
             if line.startswith("| De-ID Method |")]
    assert len(lines) == 1, lines
    assert "tag rules" in lines[0], lines[0]
    return lines[0]


def test_the_report_counts_the_policy_in_force(tmp_path):
    """The bare report says 620 rules and `session defaults`; a
    `privacy_profile: none` session says 0. Kills `generate_report`'s
    `load_phi_config()` fallback for an empty `phi_tags` -- under it the
    `none` session reports a floor the scan never applied, which is the
    #495 defect shape (a policy named that never ran)."""
    from isocenter.profiles import FLOOR_POLICY

    with Session(str(tmp_path / "bare.db")) as session:
        line = _report_method_line(session, tmp_path, "bare")
    assert f"{len(FLOOR_POLICY)} tag rules" in line
    assert "session defaults" in line.lower()

    none = tmp_path / "none.yaml"
    none.write_text("privacy_profile: none\n", encoding="utf-8")
    with Session(str(tmp_path / "none.db")) as session:
        session.load_config(str(none))
        assert session.configuration.phi_tags == {}
        line = _report_method_line(session, tmp_path, "none")
    assert "0 tag rules" in line


def test_privacy_profile_none_applies_no_floor(tmp_path, caplog):
    """`privacy_profile: none` is the opt-out the scaffold's header has
    promised since v2.0: the file's `phi_tags` are the whole policy, no
    "Unknown privacy profile" warning, and with no tags at all the "No
    PHI tags defined" warning is the one silence left. Kills `none`
    treated as an unknown name (warned and dropped, or refused) and
    `none` treated as `basic`."""
    one = tmp_path / "one.yaml"
    one.write_text("privacy_profile: none\nphi_tags:\n"
                   "  '0018,1030': {action: REMOVE, name: Protocol}\n",
                   encoding="utf-8")
    with Session(str(tmp_path / "one.db")) as session:
        with caplog.at_level(logging.WARNING, logger="isocenter"):
            session.load_config(str(one))
        assert session.configuration.phi_tags == {
            "0018,1030": {"action": "REMOVE", "name": "Protocol"}}
        assert session.configuration.privacy_profile is None
    assert "Unknown privacy profile" not in caplog.text

    caplog.clear()
    empty = tmp_path / "empty.yaml"
    empty.write_text("privacy_profile: none\n", encoding="utf-8")
    patient, _ = _bare_ct_instance()
    with Session(str(tmp_path / "empty.db")) as session:
        session.load_config(str(empty))
        session.store.patients.append(patient)
        with caplog.at_level(logging.WARNING, logger="isocenter"):
            report = session.audit()
    assert "No PHI tags defined" in caplog.text
    assert not any(f.tag == "0008,1010" for f in report)


def test_set_phi_tag_keys_are_lowercase(tmp_path):
    """`set_phi_tag("0008,103E", "KEEP")` on a bare session yields one
    `0008,103e` entry carrying KEEP, and the audit honours it. Kills
    `tag.upper()` in `set_phi_tag`: the uppercase key sat beside the
    floor's lowercase one as a second rule for the same tag, and which
    won at scan time was dict order."""
    from isocenter.profiles import FLOOR_POLICY

    patient, instance = _bare_ct_instance()
    instance.attributes["0008,103e"] = "Rhythm strip Jane Doe"

    with Session(str(tmp_path / "s.db")) as session:
        session.configuration.set_phi_tag("0008,103E", "KEEP")
        tags = session.configuration.phi_tags
        assert "0008,103E" not in tags
        assert tags["0008,103e"]["action"] == "KEEP"
        assert len(tags) == len(FLOOR_POLICY)

        session.store.patients.append(patient)
        report = session.audit()

    assert not any(f.tag == "0008,103e" for f in report)


def test_a_saved_configuration_reloads_under_the_same_policy(tmp_path):
    """bare -> `set_phi_tag` -> `save()` -> a new session's `load_config`
    -> the same `phi_tags`, and `privacy_profile is None`. Kills `save()`
    writing `privacy_profile: custom`, an unknown name: dropped with a
    warning before #456, refused at reload since."""
    config = tmp_path / "saved.yaml"
    with Session(str(tmp_path / "a.db")) as session:
        session.configuration.config_path = str(config)
        session.configuration.set_phi_tag("0018,1030", "REMOVE")
        expected = dict(session.configuration.phi_tags)

    saved = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert saved["privacy_profile"] == "none"

    with Session(str(tmp_path / "b.db")) as session:
        session.load_config(str(config))
        assert session.configuration.phi_tags == expected
        assert session.configuration.privacy_profile is None


def test_the_loader_lowercases_user_keys_before_the_merge(tmp_path):
    """A user key spelled `0008,103E` under `privacy_profile: basic` yields
    one `0008,103e` entry (620, not 621) carrying the user's action. Kills
    a merge that leaves the uppercase key beside the profile's: the
    inspector collapses them at scan time with the later one winning by
    dict order, and the report counts a rule that never existed."""
    from isocenter.config_manager import ConfigLoader
    from isocenter.profiles import BASIC_PROFILE

    config = tmp_path / "keep.yaml"
    config.write_text("privacy_profile: basic\nphi_tags:\n"
                      "  '0008,103E': {action: KEEP, name: Series Description}\n",
                      encoding="utf-8")
    tags, _, _, _, _ = ConfigLoader.load_unified_config(str(config))

    assert "0008,103E" not in tags
    assert tags["0008,103e"]["action"] == "KEEP"
    assert len(tags) == len(BASIC_PROFILE)


# ---------------------------------------------------------------------------
# A config with no privacy_profile line (an absent line means the floor
# beneath the file's tags; `none` opts out)
# ---------------------------------------------------------------------------

def test_a_config_without_a_profile_line_extends_the_floor(tmp_path, caplog):
    """`phi_tags: {'0018,0015': REMOVE}` with no `privacy_profile` line
    loads the floor plus that one tag, names no profile, and logs that
    the floor was applied. Kills an absent line meaning an empty base
    (1 tag, and Study ID and Institution Name back in the export -- the
    brief's `MODE=onetag` measurement), and the floor applied without a
    word."""
    from isocenter.config_manager import ConfigLoader
    from isocenter.profiles import FLOOR_POLICY

    config = tmp_path / "onetag.yaml"
    config.write_text("phi_tags:\n  '0018,0015': {action: REMOVE, name: Body Part}\n",
                      encoding="utf-8")

    with caplog.at_level(logging.INFO, logger="isocenter"):
        tags, _, _, _, profile = ConfigLoader.load_unified_config(str(config))

    # Body Part Examined, which Table E.1-1 does not name: Protocol Name
    # was the tag here until #547 made it a basic rule, and a tag the
    # floor already holds no longer shows "plus one".
    assert "0018,0015" not in FLOOR_POLICY
    assert tags == {**FLOOR_POLICY,
                    "0018,0015": {"action": "REMOVE", "name": "Body Part"}}
    assert tags["0020,0010"]["action"] == "EMPTY"       # Study ID
    assert profile is None
    assert "floor policy" in caplog.text


def test_keep_opts_a_tag_out_of_the_floor(tmp_path):
    """`action: KEEP` in a file with no profile line opts one tag out of
    the floor, and the key may be spelled uppercase. Loaded: 620 entries,
    one `0008,103e` carrying KEEP (not 621 with the KEEP winning only by
    dict order); the audit raises nothing for it; and a KEEP on
    Institution Name survives to the exported CT_small. Kills the
    override order reversed (floor over user) and the loader not
    lowercasing."""
    from isocenter.profiles import FLOOR_POLICY

    config = tmp_path / "keep.yaml"
    config.write_text(
        "phi_tags:\n"
        "  '0008,103E': {action: KEEP, name: Series Description}\n"
        "  '0008,0080': {action: KEEP, name: Institution Name}\n",
        encoding="utf-8")

    patient, instance = _bare_ct_instance()
    instance.attributes["0008,103e"] = "Rhythm strip Jane Doe"
    with Session(str(tmp_path / "a.db")) as session:
        session.load_config(str(config))
        tags = session.configuration.phi_tags
        assert len(tags) == len(FLOOR_POLICY)
        assert "0008,103E" not in tags
        assert tags["0008,103e"]["action"] == "KEEP"
        session.store.patients.append(patient)
        report = session.audit()
    assert not any(f.tag in ("0008,103e", "0008,0080") for f in report)

    original = _ct_small_into(str(tmp_path / "in"))
    with Session(str(tmp_path / "b.db")) as session:
        session.ingest(str(tmp_path / "in"))
        session.load_config(str(config))
        session.anonymize(session.audit())
        summary = session.export(str(tmp_path / "out"), use_compression=False)
    assert summary.written == 1, summary.failures
    ds = pydicom.dcmread(_exported_dicoms(str(tmp_path / "out"))[0])
    assert str(ds.InstitutionName) == str(original.InstitutionName)
    assert ds.StudyID == "", "the floor beneath the KEEPs did not apply"


def test_the_default_phi_policy_is_the_floor():
    """`ConfigLoader.load_phi_config()` with no path, which `PhiInspector()`
    with no policy calls, returns a fresh copy of the floor now that
    `resources/phi_tags.json` is gone. Kills a loader that still reads
    the resource (RuntimeError on the deleted file) and one that returns
    the module table itself. The expectation is built here from the two
    source tables, never from `FLOOR_POLICY`: comparing a result against
    the table it was copied from passes when both have been edited by the
    same leak (review of #509, mutant O1)."""
    from isocenter.config_manager import ConfigLoader
    from isocenter.privacy import PhiInspector
    from isocenter.profiles import BASIC_PROFILE, FLOOR_POLICY, RESEARCH_DEFAULTS

    expected = {tag: dict(rule) for tag, rule in {**BASIC_PROFILE, **RESEARCH_DEFAULTS}.items()}
    tags = ConfigLoader.load_phi_config()
    assert tags == expected
    assert tags is not FLOOR_POLICY
    assert tags["0010,0010"] is not FLOOR_POLICY["0010,0010"]
    assert PhiInspector().phi_tags == expected


def test_a_loaded_config_does_not_edit_the_floor_a_later_session_seeds_from(tmp_path):
    """Session 1 loads `0008,0080: KEEP` with no profile line; a later
    bare Session still REMOVEs it. Kills the loader's floor taken by
    reference (`floor = FLOOR_POLICY`, O1): the user's KEEP is merged into
    the module table, and every later bare session in the process -- and
    `load_phi_config()` -- keeps Institution Name."""
    from isocenter.profiles import BASIC_PROFILE, FLOOR_POLICY, RESEARCH_DEFAULTS

    expected = {tag: dict(rule) for tag, rule in {**BASIC_PROFILE, **RESEARCH_DEFAULTS}.items()}
    config = tmp_path / "keep.yaml"
    config.write_text(
        "phi_tags:\n  '0008,0080': {action: KEEP, name: Institution Name}\n",
        encoding="utf-8")
    with Session(str(tmp_path / "a.db")) as first:
        first.load_config(str(config))
        assert first.configuration.phi_tags["0008,0080"]["action"] == "KEEP"
    with Session(str(tmp_path / "b.db")) as later:
        assert later.configuration.phi_tags["0008,0080"]["action"] == "EMPTY"
        assert later.configuration.phi_tags == expected
    assert FLOOR_POLICY == expected


def test_privacy_profile_none_lowercases_the_files_keys(tmp_path):
    """The `none` branch returns the file's tags as the whole policy, so
    they must be lowercased before it returns, not only in the arms that
    merge. Kills lowercasing moved from validation into the merge arms
    (O2): `0008,103E` loads as spelled and the inspector, whose tags are
    lowercase throughout, reads a second key for one tag."""
    config = tmp_path / "none.yaml"
    config.write_text(
        "privacy_profile: none\n"
        "phi_tags:\n  '0008,103E': {action: REMOVE, name: Series Description}\n",
        encoding="utf-8")
    with Session(str(tmp_path / "s.db")) as session:
        session.load_config(str(config))
        assert session.configuration.phi_tags == {
            "0008,103e": {"action": "REMOVE", "name": "Series Description"}}


def test_report_section_5_names_the_decline_on_a_bare_session(tmp_path):
    """A floor finding that declines costs the bare run its PASS, and
    section 5 says why. The decline is made as in the manifest test:
    Series Date is flagged by the audit and gone before the remediation
    runs. Kills the declined-remediation term dropped from
    `generate_report`'s review reasons (section 5 then gives no reason
    for the decline)."""
    _ct_small_into(str(tmp_path / "in"))
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        report = session.audit()
        instance = session.store.patients[0].studies[0].series[0].instances[0]
        assert any(f.tag == "0008,0021" for f in report)
        del instance.attributes["0008,0021"]
        session.anonymize(report)
        path = tmp_path / "report.md"
        session.generate_report(str(path))
    text = path.read_text(encoding="utf-8")
    s5 = text.split("## 5. Validation & Verification", 1)[1].split("---\n", 1)[0]
    assert "**Grade Basis:** REVIEW_REQUIRED" in s5, s5
    assert "1 declined remediation(s) in section 3.3" in s5, s5
