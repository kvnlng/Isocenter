"""UIDs are replaced with UIDs derived from the project secret (#544).

The derivation's literals were computed once from the implementation and
pasted; they equal the L10 architect's prototype (spec section 1.5), which
is what makes them a pin rather than a restatement.
"""
import hmac
import hashlib
import shutil
import sqlite3
from pathlib import Path

import numpy as np
import pydicom
import pytest
from pydicom.data import get_testdata_file
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.sequence import Sequence

from isocenter import privacy
from isocenter.entities import PhiStatus, SOURCE_SOP_UID_ATTR
from isocenter.privacy import (_redaction_uid_for, _replacement_uid_for,
                               _uid_is_minted)
from isocenter.session import DicomSession

from support.annex_e import load_table
from support.project_secret import FIXED_A, FIXED_B, load_fixed_secret

CT_SMALL_SOP = "1.3.6.1.4.1.5962.1.1.1.1.1.20040119072730.12322"
CT_SMALL_STUDY = "1.3.6.1.4.1.5962.1.2.1.20040119072730.12322"
CT_SMALL_SERIES = "1.3.6.1.4.1.5962.1.3.1.1.20040119072730.12322"
CT_CLASS = "1.2.840.10008.5.1.4.1.1.2"

#: The tags a value-less REPLACE on a UI covers in `basic@2026c`: the
#: table's `U` rows and Annotation Group UID (D on a UI).
U_ROWS = frozenset({row["key"] for row in load_table()["rows"]
                    if row["basic"] == "U"} | {"006a,0003"})
#: The Python attributes the owners' findings name.
OWNER_FIELDS = {"study_instance_uid": "0020,000d",
                "series_instance_uid": "0020,000e"}


def _uuid_int(uid):
    assert uid.startswith("2.25.")
    return int(uid[5:])


def M(uid, secret=FIXED_A):
    return _replacement_uid_for(uid, secret)


def _ct(path, sop, study="1.2.3.99.1", series="1.2.3.99.2", frame="1.2.3.99.3",
        patient_id="L10-P1", serial="L10-SN", refs=(), extra=None):
    """A 32x32 CT with fixed UIDs; `refs` are SOP UIDs this one's
    Referenced Image Sequence names."""
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = CT_CLASS
    meta.MediaStorageSOPInstanceUID = sop
    meta.TransferSyntaxUID = "1.2.840.10008.1.2.1"
    ds = FileDataset(str(path), {}, file_meta=meta, preamble=b"\0" * 128)
    ds.SOPClassUID = CT_CLASS
    ds.SOPInstanceUID = sop
    ds.StudyInstanceUID = study
    ds.SeriesInstanceUID = series
    ds.FrameOfReferenceUID = frame
    ds.PatientID = patient_id
    ds.PatientName = "Doe^Jane"
    ds.Modality = "CT"
    ds.StudyDate = "20200101"
    ds.DeviceSerialNumber = serial
    ds.Manufacturer = "L10"
    ds.ManufacturerModelName = "Probe"
    ds.ImagePositionPatient = [0, 0, 0]
    ds.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
    ds.PixelSpacing = [1, 1]
    ds.Rows = ds.Columns = 32
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.PixelData = (np.arange(1024, dtype=np.uint16) % 200 + 50).tobytes()
    if refs:
        items = []
        for ref in refs:
            item = Dataset()
            item.ReferencedSOPClassUID = CT_CLASS
            item.ReferencedSOPInstanceUID = ref
            items.append(item)
        ds.ReferencedImageSequence = Sequence(items)
    for keyword, value in (extra or {}).items():
        setattr(ds, keyword, value)
    ds.save_as(str(path), enforce_file_format=True)
    return path


def _ui_elements(ds, path=()):
    """`{(path, tag): value}` of every UI element, at every depth."""
    out = {}
    for el in ds:
        tag = f"{el.tag.group:04x},{el.tag.element:04x}"
        if el.VR == "SQ":
            for i, item in enumerate(el.value):
                out.update(_ui_elements(item, path + ((tag, i),)))
        elif el.VR == "UI":
            out[(path, tag)] = el.value
    return out


def _values(value):
    if isinstance(value, (list, tuple, pydicom.multival.MultiValue)):
        return [str(v) for v in value]
    return [str(value)]


def _pipeline(tmp_path, src, *, config="privacy_profile: basic\n",
              secret=FIXED_A, name="s", redact=False):
    """ingest -> load_config -> audit -> anonymize (-> redact) -> export."""
    cfg = tmp_path / f"{name}.yaml"
    cfg.write_text(config, encoding="utf-8")
    out = tmp_path / f"{name}-out"
    with DicomSession(str(tmp_path / f"{name}.db")) as session:
        load_fixed_secret(session, secret=secret)
        session.ingest(str(src))
        session.load_config(str(cfg))
        session.audit()
        session.anonymize()
        if redact:
            session.redact(show_progress=False)
        session.export(str(out), use_compression=False)
    return out


def _exported(out):
    return {p.relative_to(out): pydicom.dcmread(p) for p in sorted(out.rglob("*.dcm"))}


# --------------------------------------------------------------------
# T1-T3: the derivation
# --------------------------------------------------------------------

def test_the_replacement_is_a_pinned_keyed_version_8_uid():
    minted = _replacement_uid_for(CT_SMALL_SOP, FIXED_A)
    assert minted == "2.25.72143901791669322985000875885226733249"
    assert _replacement_uid_for(CT_SMALL_STUDY, FIXED_A) == \
        "2.25.48879930114314090957151844872658561654"
    assert _replacement_uid_for(CT_SMALL_SOP, FIXED_B) == \
        "2.25.100713774000139417550006952818007004078"
    for uid in (minted, _replacement_uid_for(CT_SMALL_SOP, FIXED_B)):
        n = _uuid_int(uid)
        assert (n >> 76) & 0xF == 0x8, "version 8"
        assert (n >> 62) & 0x3 == 0b10, "variant 10"
        assert len(uid) <= 64
    labels = [privacy._LABEL_PATIENT_ID, privacy._LABEL_PSEUDONYM_CHECK,
              privacy._LABEL_DATE_JITTER, privacy._LABEL_UID,
              privacy._LABEL_REDACTED_UID, privacy._LABEL_UID_CHECK]
    assert len(set(labels)) == 6
    # The redacted kind is never the replacement kind of the same input.
    assert _redaction_uid_for(CT_SMALL_SOP, "h", FIXED_A) != minted


def test_the_redaction_uid_is_pinned_and_keyed_on_the_zones():
    one = _redaction_uid_for(CT_SMALL_SOP, "0" * 32, FIXED_A)
    assert one == _redaction_uid_for(CT_SMALL_SOP, "0" * 32, FIXED_A)
    assert one != _redaction_uid_for(CT_SMALL_SOP, "1" * 32, FIXED_A)
    assert one != _redaction_uid_for(CT_SMALL_SOP, "0" * 32, FIXED_B)
    assert _uid_is_minted(one, FIXED_A)


def test_only_a_uid_this_project_minted_verifies():
    minted = _replacement_uid_for(CT_SMALL_SOP, FIXED_A)
    assert _uid_is_minted(minted, FIXED_A)
    assert not _uid_is_minted(minted, FIXED_B)
    assert not _uid_is_minted(CT_SMALL_SOP, FIXED_A)
    assert not _uid_is_minted(_replacement_uid_for(CT_SMALL_SOP, FIXED_B), FIXED_A)
    # A version-8 UUID whose check was not made under this secret.
    random_v8 = (0x0123456789ABCDEF0123456789ABCDEF & ~(0xF << 76)) | (0x8 << 76)
    random_v8 = (random_v8 & ~(0x3 << 62)) | (0x2 << 62)
    assert not _uid_is_minted(f"2.25.{random_v8}", FIXED_A)
    assert not _uid_is_minted("2.25.0", FIXED_A)
    assert not _uid_is_minted("ab" * 32, FIXED_A)
    # One check bit flipped: the lowest bit of the integer is in the check.
    flipped = f"2.25.{_uuid_int(minted) ^ 1}"
    assert not _uid_is_minted(flipped, FIXED_A)
    # Spellings of the same number that are not the number.
    assert not _uid_is_minted("2.25.0" + minted[5:], FIXED_A), "leading zero"
    assert not _uid_is_minted(minted + " ", FIXED_A), "trailing space"
    assert not _uid_is_minted(" " + minted, FIXED_A), "leading space"
    assert not _uid_is_minted(f"2.25.{_uuid_int(minted) + (1 << 128)}", FIXED_A), \
        "at or above 2**128"
    assert not _uid_is_minted([minted], FIXED_A), "a multi-value is not one UID"
    assert not _uid_is_minted(None, FIXED_A)


def test_the_check_is_computed_over_the_masked_digest():
    """The check covers the 11 bytes as they are written (version and
    variant bits set), so a verifier that reads the UID back can
    recompute it."""
    minted = _replacement_uid_for(CT_SMALL_SOP, FIXED_A)
    raw = _uuid_int(minted).to_bytes(16, "big")
    expected = hmac.new(FIXED_A, privacy._LABEL_UID_CHECK + raw[:11],
                        hashlib.sha256).digest()[:5]
    assert raw[11:] == expected


def test_no_uid_without_a_secret():
    with pytest.raises(RuntimeError):
        _replacement_uid_for(CT_SMALL_SOP, None)
    with pytest.raises(RuntimeError):
        _redaction_uid_for(CT_SMALL_SOP, "h", b"")
    with pytest.raises(RuntimeError):
        _uid_is_minted(_replacement_uid_for(CT_SMALL_SOP, FIXED_A), None)
    # Whatever the value's shape: without a secret "not minted" is not an
    # answer, since it would read as "replace this".
    with pytest.raises(RuntimeError):
        _uid_is_minted(CT_SMALL_SOP, None)


# --------------------------------------------------------------------
# T8: the configuration vocabulary
# --------------------------------------------------------------------

@pytest.mark.parametrize("rule", [{"action": "REPLACE"}, {"action": "REPLACE", "value": ""},
                                  "Frame of Reference UID"],
                         ids=["mapping", "empty-value", "string-form"])
@pytest.mark.parametrize("door", ["load_config", "audit_config_path", "set_phi_tag",
                                  "audit_assigned", "inspector"])
def test_replace_without_a_value_loads_on_a_uid(tmp_path, door, rule):
    """0.9.8 refused both spellings on a UI (#560): `REPLACE` with no value
    wrote `ANONYMIZED`. Since #544 it is the keyed UID, at every door a
    policy comes in by. Kills the #560 refusal left in place at any door."""
    import yaml
    from isocenter.privacy import PhiInspector
    from isocenter.session import DicomSession
    from support.project_secret import load_fixed_secret

    tag = "0020,0052"
    if door == "inspector":
        PhiInspector(config_tags={tag: rule})
        return
    path = tmp_path / "cfg.yaml"
    path.write_text(yaml.safe_dump({"privacy_profile": "none",
                                    "phi_tags": {tag: rule}}), encoding="utf-8")
    with DicomSession(str(tmp_path / "s.db")) as session:
        load_fixed_secret(session)
        if door == "load_config":
            session.load_config(str(path))
        elif door == "audit_config_path":
            session.audit(config_path=str(path))
        elif door == "audit_assigned":
            session.configuration.phi_tags = {tag: rule}
            session.audit()
        else:
            if isinstance(rule, str):
                pytest.skip("set_phi_tag spells a rule, not a display name")
            session.configuration.set_phi_tag(tag, "REPLACE")


def test_replace_without_a_value_elsewhere_is_what_it_was():
    """Only the UI arm moved. A CS takes its dummy (#557), a numeric VR is
    still refused, and a private key -- no dictionary VR -- still writes
    `ANONYMIZED`. Kills the #560 refusal dropped wholesale, and the UI arm
    widened past value-less REPLACE."""
    from isocenter.config_manager import _refused_phi_rule, validate_phi_policy
    assert _refused_phi_rule("0008,0060", {"action": "REPLACE"}) is None
    assert _refused_phi_rule("0020,0052", {"action": "REPLACE"}) is None
    assert _refused_phi_rule("0009,1001", {"action": "REPLACE"}) is None
    with pytest.raises(ValueError, match="0010,1030 is DS"):
        validate_phi_policy({"0010,1030": {"action": "REPLACE"}}, "cfg.yaml")
    with pytest.raises(ValueError, match="0008,0016 is UI, which cannot hold it"):
        validate_phi_policy({"0008,0016": {"action": "REPLACE", "value": "x.y"}},
                            "cfg.yaml")


# --------------------------------------------------------------------
# T4-T7, T9-T11: the scan, the pass, the export
# --------------------------------------------------------------------

@pytest.fixture
def cohort(tmp_path):
    """rtdose (a nested reference), CT_small, and a synthetic pair where
    B's Referenced Image Sequence names A and both share one Frame of
    Reference; B also carries a two-valued Failed SOP Instance UID List
    (0008,0058, VM 1-n) whose first value is A's SOP UID."""
    src = tmp_path / "src"
    src.mkdir()
    shutil.copy(get_testdata_file("rtdose.dcm"), src / "rtdose.dcm")
    shutil.copy(get_testdata_file("CT_small.dcm"), src / "CT_small.dcm")
    _ct(src / "a.dcm", "1.2.3.99.10")
    _ct(src / "b.dcm", "1.2.3.99.11", refs=["1.2.3.99.10"],
        extra={"FailedSOPInstanceUIDList": ["1.2.3.99.10", "1.2.3.99.12"]})
    return src


def test_the_basic_pipeline_exports_every_uid_replaced_and_every_reference_consistent(
        tmp_path, cohort):
    """Every `U`-row element, top-level and nested, is the replacement of
    its source value; every other UI element is the source's; a reference
    to another instance equals that instance's exported SOP UID; the file
    meta, the file name and the folder suffixes follow.

    Kills: the derivation keyed on (tag, value); nested items skipped;
    references left behind; replacement by VR (SOP Class would move); the
    multi-valued element written as the repr of a list."""
    sources = {pydicom.dcmread(p).SOPInstanceUID: _ui_elements(pydicom.dcmread(p))
               for p in cohort.iterdir()}
    out = _pipeline(tmp_path, cohort)
    exported = _exported(out)
    assert len(exported) == len(sources) == 4

    by_source = {}
    nested = 0
    for rel, ds in exported.items():
        (src_sop,) = [s for s in sources if M(s) == ds.SOPInstanceUID]
        by_source[src_sop] = (rel, ds)
        source = sources[src_sop]
        for (path, tag), value in _ui_elements(ds).items():
            assert (path, tag) in source, (rel, path, tag)
            if tag in U_ROWS:
                assert _values(value) == [M(v) for v in _values(source[(path, tag)])], \
                    (rel, path, tag)
                nested += bool(path)
            else:
                assert _values(value) == _values(source[(path, tag)]), (rel, path, tag)
        # The file meta, the element and the file name are one UID.
        assert ds.file_meta.MediaStorageSOPInstanceUID == ds.SOPInstanceUID
        assert rel.stem == ds.SOPInstanceUID
        assert rel.parts[1].endswith(ds.StudyInstanceUID[-5:])
        assert rel.parts[2].endswith(ds.SeriesInstanceUID[-5:])
        assert ds.SOPClassUID == source[((), "0008,0016")]
    assert nested >= 2, "rtdose's and B's nested references were compared"

    # B's reference resolves to A as exported; the shared Frame of
    # Reference stays shared.
    a = by_source["1.2.3.99.10"][1]
    b = by_source["1.2.3.99.11"][1]
    assert b.ReferencedImageSequence[0].ReferencedSOPInstanceUID == a.SOPInstanceUID
    assert b.FrameOfReferenceUID == a.FrameOfReferenceUID == M("1.2.3.99.3")
    assert list(b.FailedSOPInstanceUIDList) == [a.SOPInstanceUID, M("1.2.3.99.12")]
    assert b.ReferencedImageSequence[0].ReferencedSOPClassUID == CT_CLASS


def test_two_stores_with_one_secret_export_the_same_uids_and_another_secret_does_not(
        tmp_path):
    """Kills a random or store-local component in the derivation."""
    src = tmp_path / "src"
    src.mkdir()
    shutil.copy(get_testdata_file("CT_small.dcm"), src / "CT_small.dcm")
    one = _pipeline(tmp_path, src, name="one")
    two = _pipeline(tmp_path, src, name="two")
    other = _pipeline(tmp_path, src, name="other", secret=FIXED_B)
    assert sorted(_exported(one)) == sorted(_exported(two))
    (ds_a,) = _exported(one).values()
    (ds_b,) = _exported(other).values()
    assert ds_a.SOPInstanceUID == M(CT_SMALL_SOP)
    assert ds_b.SOPInstanceUID == M(CT_SMALL_SOP, FIXED_B)
    for keyword in ("SOPInstanceUID", "StudyInstanceUID", "SeriesInstanceUID",
                    "FrameOfReferenceUID"):
        assert getattr(ds_a, keyword) != getattr(ds_b, keyword), keyword


def _uid_findings(report):
    return [f for f in report
            if f.tag in U_ROWS or (f.remediation_proposal is not None
                                   and f.remediation_proposal.target_attr in OWNER_FIELDS)]


def test_a_second_pass_and_a_reopen_replace_nothing(tmp_path, cohort):
    """After `anonymize()` the audit raises nothing on a UID, in the same
    session and after a reopen, and the store's own rows hold only minted
    UIDs, with pixels that still load.

    Kills: the minted guard removed (a second pass maps M(x) to M(M(x)));
    the owners' rename not saved."""
    cfg = tmp_path / "c.yaml"
    cfg.write_text("privacy_profile: basic\n", encoding="utf-8")
    db = tmp_path / "s.db"
    with DicomSession(str(db)) as session:
        load_fixed_secret(session)
        session.ingest(str(cohort))
        session.load_config(str(cfg))
        first = session.audit()
        assert {f.entity_type for f in _uid_findings(first)} >= {
            "Study", "Series", "Instance"}
        session.anonymize()
        assert _uid_findings(session.audit()) == []
        session.save(sync=True)
    with DicomSession(str(db)) as session:
        session.load_config(str(cfg))
        assert _uid_findings(session.audit()) == []
        for patient in session.store.patients:
            for study in patient.studies:
                for series in study.series:
                    for inst in series.instances:
                        assert inst.get_pixel_data() is not None
    with sqlite3.connect(str(db)) as conn:
        for table, column in (("studies", "study_instance_uid"),
                              ("series", "series_instance_uid"),
                              ("instances", "sop_instance_uid")):
            uids = [row[0] for row in conn.execute(f"SELECT {column} FROM {table}")]
            assert uids and all(_uid_is_minted(u, FIXED_A) for u in uids), (table, uids)


def test_keep_on_the_uid_rows_retains_them_and_a_value_is_written(tmp_path):
    """Retain UIDs is `KEEP` on the rows (Q1). `REPLACE value:` on a UI that
    is not owned writes the value. Kills the new arm ignoring the action,
    and the rule's value ignored."""
    src = tmp_path / "src"
    src.mkdir()
    shutil.copy(get_testdata_file("CT_small.dcm"), src / "CT_small.dcm")
    out = _pipeline(tmp_path, src, config=(
        "privacy_profile: basic\nphi_tags:\n"
        "  '0008,0018': {action: KEEP}\n"
        "  '0020,000d': {action: KEEP}\n"
        "  '0020,000e': {action: KEEP}\n"
        "  '0020,0052': {action: REPLACE, value: '1.2.3'}\n"))
    (ds,) = _exported(out).values()
    assert ds.SOPInstanceUID == CT_SMALL_SOP
    assert ds.StudyInstanceUID == CT_SMALL_STUDY
    assert ds.SeriesInstanceUID == CT_SMALL_SERIES
    assert ds.FrameOfReferenceUID == "1.2.3"


def test_a_value_on_an_owned_uid_leaves_the_owner_as_0_9_8_did(tmp_path):
    """`REPLACE value:` on Study, Series or SOP Instance UID is what it was
    in 0.9.8 (owner ruling on Q-C): only a value-less REPLACE moves the
    owner, because one literal written into every Study would collide on
    the store's UNIQUE key. The owners keep their UIDs, which the export
    stamps over the copies; the SOP Instance UID element and the file meta
    take the value while the file is still named by the instance's own UID.
    Measured at b7462cd3 (0.9.8's behaviour), unchanged here. Kills the
    owner arm taking a valued REPLACE."""
    src = tmp_path / "src"
    src.mkdir()
    shutil.copy(get_testdata_file("CT_small.dcm"), src / "CT_small.dcm")
    out = _pipeline(tmp_path, src, config=(
        "privacy_profile: basic\nphi_tags:\n"
        "  '0008,0018': {action: REPLACE, value: '1.2.3.4'}\n"
        "  '0020,000d': {action: REPLACE, value: '1.2.3.5'}\n"
        "  '0020,000e': {action: REPLACE, value: '1.2.3.6'}\n"))
    ((rel, ds),) = _exported(out).items()
    assert ds.StudyInstanceUID == CT_SMALL_STUDY
    assert ds.SeriesInstanceUID == CT_SMALL_SERIES
    assert ds.SOPInstanceUID == ds.file_meta.MediaStorageSOPInstanceUID == "1.2.3.4"
    assert rel.stem == CT_SMALL_SOP


def test_an_instance_copy_of_a_replaced_owner_is_not_raised_again_and_an_original_is(
        tmp_path):
    """The owners' write puts the minted Study and Series UIDs on each
    instance's top-level copy, and a re-audit raises nothing there; a copy
    set back to the source UID is raised. Kills an owner-copy skip on
    agreement alone (the #496/#518 shape)."""
    src = tmp_path / "src"
    src.mkdir()
    shutil.copy(get_testdata_file("CT_small.dcm"), src / "CT_small.dcm")
    cfg = tmp_path / "c.yaml"
    cfg.write_text("privacy_profile: basic\n", encoding="utf-8")
    with DicomSession(str(tmp_path / "s.db")) as session:
        load_fixed_secret(session)
        session.ingest(str(src))
        session.load_config(str(cfg))
        session.anonymize()
        study = session.store.patients[0].studies[0]
        series = study.series[0]
        inst = series.instances[0]
        assert study.study_instance_uid == M(CT_SMALL_STUDY)
        assert series.series_instance_uid == M(CT_SMALL_SERIES)
        assert inst.sop_instance_uid == M(CT_SMALL_SOP)
        assert inst.attributes["0020,000d"] == study.study_instance_uid
        assert inst.attributes["0020,000e"] == series.series_instance_uid
        assert inst.attributes["0008,0018"] == inst.sop_instance_uid
        assert inst.attributes[SOURCE_SOP_UID_ATTR] == CT_SMALL_SOP
        assert _uid_findings(session.audit()) == []

        inst.set_attr("0020,000d", CT_SMALL_STUDY)
        raised = _uid_findings(session.audit())
        assert [(f.entity_type, f.tag) for f in raised] == [("Instance", "0020,000d")]
        assert raised[0].remediation_proposal.new_value == M(CT_SMALL_STUDY)


def test_the_series_is_a_finding_entity_of_its_own(tmp_path):
    """Q4: the Series owns `0020,000e`, so its UID is raised against the
    Series (`entity_type == "Series"`), and a report from before the pass
    resolves it. Kills the series scan left out of `scan_patient`."""
    src = tmp_path / "src"
    src.mkdir()
    shutil.copy(get_testdata_file("CT_small.dcm"), src / "CT_small.dcm")
    cfg = tmp_path / "c.yaml"
    cfg.write_text("privacy_profile: basic\n", encoding="utf-8")
    with DicomSession(str(tmp_path / "s.db")) as session:
        load_fixed_secret(session)
        session.ingest(str(src))
        session.load_config(str(cfg))
        report = session.audit()
        (series_finding,) = [f for f in report if f.entity_type == "Series"]
        assert series_finding.entity_uid == CT_SMALL_SERIES
        assert series_finding.entity is session.store.patients[0].studies[0].series[0]
        assert series_finding.tag == "0020,000e"
        assert series_finding.remediation_proposal.new_value == M(CT_SMALL_SERIES)
        (study_finding,) = [f for f in report if f.entity_type == "Study"
                            and f.tag == "0020,000d"]
        assert study_finding.remediation_proposal.target_attr == "study_instance_uid"


def test_a_declined_finding_on_an_instance_whose_uid_moves_keeps_it_identified(
        tmp_path):
    """One instance declines a date it cannot parse, in the same pass that
    replaces its SOP UID. The pass-end demotion still finds it (IDENTIFIED),
    and every remediation row names the UID the scan saw.

    Kills: a UID-keyed map read after the rename, so the demotion or the
    rows miss the instance."""
    src = tmp_path / "src"
    src.mkdir()
    _ct(src / "a.dcm", "1.2.3.99.20", extra={"SeriesDate": "notadate"})
    cfg = tmp_path / "c.yaml"
    cfg.write_text("privacy_profile: basic\nphi_tags:\n"
                   "  '0008,0021': {action: JITTER}\n", encoding="utf-8")
    db = tmp_path / "s.db"
    with DicomSession(str(db)) as session:
        load_fixed_secret(session)
        session.ingest(str(src))
        session.load_config(str(cfg))
        session.anonymize()
        inst = session.store.patients[0].studies[0].series[0].instances[0]
        assert inst.sop_instance_uid == M("1.2.3.99.20")
        assert inst.phi_status is PhiStatus.IDENTIFIED
        session.save(sync=True)
    with sqlite3.connect(str(db)) as conn:
        rows = conn.execute(
            "SELECT entity_uid, details FROM audit_log WHERE action_type IN "
            "('REMEDIATION_REPLACE', 'REMEDIATION_DECLINED') AND details LIKE "
            "'%(Tag 0008,0018)%'").fetchall()
        assert [uid for uid, _ in rows] == ["1.2.3.99.20"], rows
        declined = conn.execute(
            "SELECT entity_uid FROM audit_log WHERE action_type = "
            "'REMEDIATION_DECLINED'").fetchall()
        assert declined == [("1.2.3.99.20",)], declined


@pytest.mark.parametrize("owner", ["study", "series"])
def test_a_reloaded_owners_uid_replacement_is_saved(owner):
    """Pins `entity.mark_modified()` at remediation.py line 291 for the
    owners' UIDs (#544), on a *reloaded* owner: one the store hands back
    REMEDIATED, where the status stamp at the end of the pass
    short-circuits and the bump is the only thing that makes the next save
    write the new UID (#173's shape). The Study and Series replacement
    goes through that Python-attribute arm rather than a sixth
    `mark_modified()` of its own, so no pinned line moved."""
    from isocenter.entities import Series, Study
    from isocenter.privacy import PhiFinding, PhiRemediation
    from isocenter.remediation import RemediationService

    if owner == "study":
        entity, attr, source = Study("1.2.3.99.1", None), "study_instance_uid", "1.2.3.99.1"
    else:
        entity, attr, source = Series("1.2.3.99.2", "CT", 1), "series_instance_uid", "1.2.3.99.2"
    entity.record_phi_status(PhiStatus.REMEDIATED)
    entity.mark_persisted()
    assert not entity.has_unsaved_changes, "setup: starts saved"
    RemediationService(project_secret=FIXED_A).apply_remediation([PhiFinding(
        entity_uid=source, entity_type=owner.title(), field_name=attr,
        value=source, reason="test", tag=OWNER_FIELDS[attr], entity=entity,
        remediation_proposal=PhiRemediation(
            "REPLACE_TAG", attr, M(source), source,
            metadata={privacy.UID_REPLACEMENT: True}))])
    assert getattr(entity, attr) == M(source)
    assert entity.has_unsaved_changes, (
        "the owner reports nothing to save after its UID was replaced, so "
        "the store keeps the source UID")


# --------------------------------------------------------------------
# A waveform's samples follow its instance's new UID
# --------------------------------------------------------------------

def _waveform(session):
    (inst,) = [i for p in session.store.patients for st in p.studies
               for se in st.series for i in se.instances]
    return inst, inst.get_waveform_bytes()


@pytest.mark.parametrize("compact", [False, True], ids=["reopen", "compact-reopen"])
def test_a_replaced_waveform_instance_keeps_its_samples(tmp_path, compact):
    """A waveform's `instance_blobs` row is written once, at ingest, under
    the SOP Instance UID of that moment, and has no column on `instances`
    to ride. Until the save re-emitted it under the current UID, an
    instance `anonymize()` gave its replacement UID reopened with no
    samples ("no sample data reached this export"), and `compact()`
    reclaimed them as an orphan's. Measured red on the first version of
    this branch: `get_waveform_bytes()` is None after the reopen."""
    from scripts.generate_waveform_test_data import write_fixture

    write_fixture(str(tmp_path / "in" / "ecg.dcm"), num_samples=200,
                  patient_id="MRN-544", patient_name="Doe^Jane")
    db = str(tmp_path / "s.db")
    with DicomSession(db) as session:
        load_fixed_secret(session, secret=FIXED_A)
        session.ingest(str(tmp_path / "in"))
        inst, before = _waveform(session)
        source = inst.sop_instance_uid
        assert before, "setup: the ECG carries samples"
        session.anonymize(session.audit())
        assert inst.sop_instance_uid == M(source), "setup: the UID moved"
        session.save(sync=True)
        if compact:
            session.compact()
    with DicomSession(db) as session:
        inst, after = _waveform(session)
        assert inst.sop_instance_uid == M(source)
        assert after == before
        session.export(str(tmp_path / "wfdb"), format="wfdb")
    assert list((tmp_path / "wfdb").rglob("*.hea")), "no WFDB record was written"


# --------------------------------------------------------------------
# Edges of the derivation's readers
# --------------------------------------------------------------------

def test_the_minted_shape_is_version_8_only():
    """The Q-B evidence is read by shape, with no secret to verify, so the
    shape has to exclude the `2.25.` UIDs other software mints: pydicom's
    `generate_uid(prefix=None)` is a version-4 UUID. Kills the version or
    variant test dropped from `_minted_uid_bytes`."""
    minted = _replacement_uid_for(CT_SMALL_SOP, FIXED_A)
    assert privacy._has_minted_uid_shape(minted)
    as_v4 = (_uuid_int(minted) & ~(0xF << 76)) | (0x4 << 76)
    assert not privacy._has_minted_uid_shape(f"2.25.{as_v4}")
    other_variant = (_uuid_int(minted) & ~(0x3 << 62)) | (0x3 << 62)
    assert not privacy._has_minted_uid_shape(f"2.25.{other_variant}")
    assert not privacy._has_minted_uid_shape(CT_SMALL_SOP)


def test_a_padded_bytes_uid_is_read_as_its_text():
    """A UI element whose VR was not on the wire can arrive as bytes, padded
    to even length with a NUL. It is replaced as the UID it spells, so it
    maps to the same replacement as the `str` copy elsewhere. Kills the
    padding left on, and a minted value inside a multi-value replaced
    again."""
    assert privacy._replaced_uids(b"1.2.3\x00", FIXED_A) == M("1.2.3")
    assert privacy._replaced_uids(b"1.2.34 ", FIXED_A) == M("1.2.34")
    assert privacy._replaced_uids([M("1.2.3"), "1.2.4"], FIXED_A) == [M("1.2.3"), M("1.2.4")]
    assert privacy._replaced_uids(["", " "], FIXED_A) is None
    assert privacy._replaced_uids(["", "1.2.4"], FIXED_A) == ["", M("1.2.4")]


def test_no_rule_keeps_every_uid(tmp_path):
    """`privacy_profile: none` with no rule on a UID keeps it: only the
    value-less REPLACE is the replacement. Kills a missing rule read as
    the string form."""
    src = tmp_path / "src"
    src.mkdir()
    shutil.copy(get_testdata_file("CT_small.dcm"), src / "CT_small.dcm")
    out = _pipeline(tmp_path, src, config=(
        "privacy_profile: none\nphi_tags:\n  '0008,0080': {action: REMOVE}\n"))
    (ds,) = _exported(out).values()
    assert (ds.SOPInstanceUID, ds.StudyInstanceUID, ds.SeriesInstanceUID) == (
        CT_SMALL_SOP, CT_SMALL_STUDY, CT_SMALL_SERIES)
    assert "InstitutionName" not in ds


def test_a_replaced_instance_still_reads_its_source_file(tmp_path):
    """A UID replacement changes no pixel, so an instance read from its
    source file keeps reading it; only redaction detaches the file. Kills
    `pixels_changed` ignored, either way round, in `_take_sop_uid` and at
    the remediation arm that calls it."""
    from isocenter.entities import Instance, Patient, Series, Study

    path = tmp_path / "ct.dcm"
    shutil.copy(get_testdata_file("CT_small.dcm"), path)
    ds = pydicom.dcmread(str(path))
    inst = Instance(CT_SMALL_SOP, CT_CLASS, 1, file_path=str(path))
    for keyword in ("Rows", "Columns", "BitsAllocated", "BitsStored", "HighBit",
                    "PixelRepresentation", "SamplesPerPixel",
                    "PhotometricInterpretation"):
        element = ds[keyword]
        inst.set_attr(f"{element.tag.group:04x},{element.tag.element:04x}", element.value)
    series = Series(CT_SMALL_SERIES, "CT", 1)
    series.instances.append(inst)
    study = Study(CT_SMALL_STUDY, None)
    study.series.append(series)
    patient = Patient("P544", "Doe^Jane")
    patient.studies.append(study)
    with DicomSession(str(tmp_path / "s.db")) as session:
        load_fixed_secret(session, secret=FIXED_A)
        session.store.patients.append(patient)
        session.anonymize(session.audit())
        assert inst.sop_instance_uid == M(CT_SMALL_SOP)
        assert inst.file_path == str(path)
        assert np.array_equal(inst.get_pixel_data(), ds.pixel_array)
    redacted = Instance("1.2.3.99.40", CT_CLASS, 1, file_path=str(path))
    redacted.regenerate_uid("2.25.40")
    assert redacted.file_path is None


@pytest.mark.parametrize("blank", ["", "  "], ids=["empty", "spaces"])
def test_a_blank_owner_uid_is_not_replaced(blank):
    """A Study or Series holding no UID has nothing to replace: minting
    one over the blank would give every such owner the same replacement,
    one UNIQUE key for many. Kills the blank test dropped from
    `_scan_owned_uid`."""
    from isocenter.entities import Patient, Series, Study
    from isocenter.privacy import PhiInspector

    patient = Patient("P544", "Doe^Jane")
    study = Study(blank, None)
    study.series.append(Series(blank, "CT", 1))
    patient.studies.append(study)
    findings = PhiInspector(project_secret=FIXED_A).scan_patient(patient)
    assert [f for f in findings if f.tag in ("0020,000d", "0020,000e")] == []


@pytest.mark.parametrize("level", ["instance", "series"])
def test_a_misnamed_finding_that_moves_its_entity_settles_the_uid_it_had(tmp_path, level):
    """The pass is handed one finding: an instance's SOP Instance UID, or a
    series' Series Instance UID, replacement, filed under a name that is
    not the entity's. The tally settles the finding's live UID beside its
    name, and since #544 the finding itself moves that UID, so the UID read
    at the settle was the replacement, under which the audit raised
    nothing, and the entity was stamped REMEDIATED over every finding the
    audit raised under its real UID that the pass was never handed. The UID
    is read as the pass began. Kills the pass-start snapshot dropped from
    `_live_uid`. (For a Series the pass-end series settle asks its
    pass-start UID too, since review finding 2 of #544, so `_uid_of`'s
    Series arm is a second road there.)"""
    import dataclasses

    src = tmp_path / "src"
    src.mkdir()
    _ct(src / "a.dcm", "1.2.3.99.21", series="1.2.3.99.22")
    with DicomSession(str(tmp_path / "s.db")) as session:
        load_fixed_secret(session)
        session.ingest(str(src))
        series = session.store.patients[0].studies[0].series[0]
        entity, tag, source = ((series.instances[0], "0008,0018", "1.2.3.99.21")
                               if level == "instance" else (series, "0020,000e", "1.2.3.99.22"))
        # One more finding under the entity's real UID, never handed over.
        session.configuration.phi_tags = {
            tag: {"action": "REPLACE"},
            "0008,0080": {"action": "REMOVE"},
            "0020,000e" if level == "series" else "0020,0052": {"action": "REPLACE"}}
        report = session.audit()
        (moving,) = [f for f in report.findings
                     if f.tag == tag and f.entity is entity and not f.entity_path]
        session.anonymize([dataclasses.replace(moving, entity_uid="1.2.3.99.29")])
        assert (entity.sop_instance_uid if level == "instance"
                else entity.series_instance_uid) == M(source)
        assert entity.phi_status is not PhiStatus.REMEDIATED


@pytest.mark.parametrize("level", ["series", "study"])
def test_a_sop_uid_finding_handed_an_owner_moves_no_instance(tmp_path, level):
    """`anonymize(findings)` takes findings from the caller, and one can
    name a SOP Instance UID replacement against a Series or Study. The
    `0008,0018` arm moves an identity only for an instance: the owner keeps
    its UID and so does every instance under it. Neither reaches the arm
    today -- a Series is declined because its attributes do not hold
    `0008,0018`, and a Study is not routed to the element arm -- so the
    arm's instance check is a second guard (its mutant survives, explained
    in #544's PR); this pins the outcome if that routing changes."""
    import dataclasses

    src = tmp_path / "src"
    src.mkdir()
    _ct(src / "a.dcm", "1.2.3.99.21", study="1.2.3.99.20", series="1.2.3.99.22")
    with DicomSession(str(tmp_path / "s.db")) as session:
        load_fixed_secret(session)
        session.ingest(str(src))
        study = session.store.patients[0].studies[0]
        series = study.series[0]
        owner, entity_type, uid = ((series, "Series", "1.2.3.99.22") if level == "series"
                                   else (study, "Study", "1.2.3.99.20"))
        session.configuration.phi_tags = {"0008,0018": {"action": "REPLACE"}}
        (finding,) = [f for f in session.audit().findings
                      if f.tag == "0008,0018" and not f.entity_path]
        session.anonymize([dataclasses.replace(
            finding, entity=owner, entity_type=entity_type, entity_uid=uid)])
        assert (study.study_instance_uid, series.series_instance_uid) == (
            "1.2.3.99.20", "1.2.3.99.22")
        assert [i.sop_instance_uid for i in series.instances] == ["1.2.3.99.21"]


def test_a_nested_sop_uid_is_replaced_in_its_item_and_moves_no_instance(tmp_path):
    """A SOP Instance UID inside a sequence item -- Source Image Sequence
    `(0008,2112)[0]>(0008,0018)`, which the cohort carries -- is replaced
    in the item, and the instance's own identity moves only for its own
    top-level `0008,0018`: the item is not an instance. Kills the two
    guards on the `0008,0018` arm dropped together (each alone is
    redundant with the other)."""
    own, nested = "1.2.3.99.61", "1.2.3.99.62"
    item = Dataset()
    item.ReferencedSOPClassUID = "1.2.840.10008.5.1.4.1.1.2"
    item.ReferencedSOPInstanceUID = "1.2.3.99.63"
    item.SOPInstanceUID = nested
    src = tmp_path / "src"
    src.mkdir()
    _ct(src / "a.dcm", own, extra={"SourceImageSequence": Sequence([item])})
    cfg = tmp_path / "c.yaml"
    cfg.write_text("privacy_profile: basic\n", encoding="utf-8")
    db = tmp_path / "s.db"
    with DicomSession(str(db)) as session:
        load_fixed_secret(session)
        session.load_config(str(cfg))
        session.ingest(str(src))
        session.anonymize()
        inst = session.store.patients[0].studies[0].series[0].instances[0]
        assert inst.sop_instance_uid == M(own)
        assert inst.attributes[SOURCE_SOP_UID_ATTR] == own
        assert inst.phi_status is PhiStatus.REMEDIATED
        session.export(str(tmp_path / "out"), use_compression=False)
    (path,) = (tmp_path / "out").rglob("*.dcm")
    ds = pydicom.dcmread(path)
    assert ds.SOPInstanceUID == M(own)
    assert ds.SourceImageSequence[0].SOPInstanceUID == M(nested)
    assert ds.SourceImageSequence[0].ReferencedSOPInstanceUID == M("1.2.3.99.63")
    with sqlite3.connect(str(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action_type IN "
                            "('ERROR', 'REMEDIATION_DECLINED')").fetchone()[0] == 0


# --------------------------------------------------------------------
# A Series finding raised and not acted on (review of #544, finding 2)
# --------------------------------------------------------------------

def _validation_status(session, tmp_path, name):
    path = tmp_path / name
    session.generate_report(str(path))
    return [line for line in path.read_text(encoding="utf-8").splitlines()
            if line.startswith("| **Validation Status**")]


def _series_filtered(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _ct(src / "a.dcm", "1.2.3.99.71", study="1.2.3.99.70", series="1.2.3.99.72")
    cfg = tmp_path / "c.yaml"
    cfg.write_text("privacy_profile: basic\n", encoding="utf-8")
    return src, str(cfg)


@pytest.mark.parametrize("reaudit", [False, True], ids=["same-report", "fresh-audit"])
def test_a_series_finding_not_acted_on_does_not_grade_pass(tmp_path, reaudit):
    """A Series has no status of its own; its instances bear it. A report
    filtered to everything but the Series finding -- a filter code written
    before "Series" was an entity type would write -- leaves the source
    Series Instance UID in every file, and the instances under it read
    IDENTIFIED, so the export grades REVIEW_REQUIRED, not PASS; and a fresh
    audit that raises the Series finding reads them IDENTIFIED too. Kills
    an unacted Series finding costing nothing (#573's condition 7)."""
    src, cfg = _series_filtered(tmp_path)
    with DicomSession(str(tmp_path / "s.db")) as session:
        load_fixed_secret(session)
        session.load_config(cfg)
        session.ingest(str(src))
        report = session.audit()
        series = session.store.patients[0].studies[0].series[0]
        (inst,) = series.instances
        assert inst.phi_status is PhiStatus.IDENTIFIED
        session.anonymize([f for f in report if f.entity_type != "Series"])
        assert series.series_instance_uid == "1.2.3.99.72"
        assert inst.sop_instance_uid == M("1.2.3.99.71")
        assert inst.phi_status is PhiStatus.IDENTIFIED
        if reaudit:
            # The instance's own copy follows its Series, which was not
            # handed in (#624, Q-C5): it holds the source UID the file
            # carries, and is raised beside the Series.
            again = session.audit()
            assert sorted((f.entity_type, f.tag) for f in again) == [
                ("Instance", "0020,000e"), ("Series", "0020,000e")]
            assert inst.phi_status is PhiStatus.IDENTIFIED
        session.export(str(tmp_path / "out"), use_compression=False)
        grade = _validation_status(session, tmp_path, "r.md")
    assert grade and "REVIEW_REQUIRED" in grade[0], grade
    (path,) = (tmp_path / "out").rglob("*.dcm")
    assert pydicom.dcmread(path).SeriesInstanceUID == "1.2.3.99.72"


@pytest.mark.parametrize("then", ["the_instance_findings_again", "a_reaudit"])
def test_a_series_finding_handed_later_grades_pass(tmp_path, then):
    """The Series handed in a later pass writes its replacement onto every
    instance copy; then the instance findings handed again (the copy is
    satisfied by the Series' write) or a re-audit (which finds nothing)
    completes the instance, and the export grades PASS -- #624's rule for
    an owner handed in late (Q-C5), which the Series follows. Kills an
    unacted Series that no later pass can clear."""
    src, cfg = _series_filtered(tmp_path)
    with DicomSession(str(tmp_path / "s.db")) as session:
        load_fixed_secret(session)
        session.load_config(cfg)
        session.ingest(str(src))
        report = session.audit()
        rest = [f for f in report if f.entity_type != "Series"]
        session.anonymize(rest)
        session.anonymize([f for f in report if f.entity_type == "Series"])
        series = session.store.patients[0].studies[0].series[0]
        inst = series.instances[0]
        assert series.series_instance_uid == M("1.2.3.99.72")
        assert inst.attributes["0020,000e"] == M("1.2.3.99.72")
        assert inst.phi_status is PhiStatus.IDENTIFIED
        if then == "a_reaudit":
            again = session.audit()
            assert list(again) == []
            session.anonymize(again)
            assert inst.phi_status is PhiStatus.CLEARED
        else:
            session.anonymize(rest)
            assert inst.phi_status is PhiStatus.REMEDIATED
        session.export(str(tmp_path / "out"), use_compression=False)
        grade = _validation_status(session, tmp_path, "r.md")
    assert grade and "PASS" in grade[0], grade


def test_a_series_pass_alone_completes_no_instance(tmp_path):
    """The Series handed in alone replaces its UID and writes every copy,
    and its instances still read IDENTIFIED: their own findings are open.
    Kills a Series' completion read as its instances'."""
    src, cfg = _series_filtered(tmp_path)
    with DicomSession(str(tmp_path / "s.db")) as session:
        load_fixed_secret(session)
        session.load_config(cfg)
        session.ingest(str(src))
        report = session.audit()
        session.anonymize([f for f in report if f.entity_type == "Series"])
        series = session.store.patients[0].studies[0].series[0]
        assert series.series_instance_uid == M("1.2.3.99.72")
        assert series.instances[0].phi_status is PhiStatus.IDENTIFIED


def test_another_stores_report_writes_no_uid_minted_there(tmp_path):
    """A report resolves against the live graph by the UIDs it names, so a
    report store A raised reaches store B over the same file, carrying A's
    replacements. Written, they linked B's export to A's by UID -- the link
    the project secret exists to prevent, #644's shape for UIDs -- and B's
    next audit replaced them again. Each is refused, with a row naming no
    value; B's own audit and pass then give B's replacements. Kills the
    refusal dropped (`_foreign_uid_refused`)."""
    src = tmp_path / "src"
    src.mkdir()
    _ct(src / "a.dcm", "1.2.3.99.81", study="1.2.3.99.80", series="1.2.3.99.82",
        frame="1.2.3.99.83", refs=("1.2.3.99.84",))
    sources = ("1.2.3.99.80", "1.2.3.99.81", "1.2.3.99.82", "1.2.3.99.83", "1.2.3.99.84")
    with DicomSession(str(tmp_path / "a.db")) as store_a:
        load_fixed_secret(store_a, secret=FIXED_A)
        store_a.ingest(str(src))
        report = store_a.audit()
    db = tmp_path / "b.db"
    with DicomSession(str(db)) as store_b:
        load_fixed_secret(store_b, secret=FIXED_B)
        store_b.ingest(str(src))
        store_b.anonymize(report)
        store_b.export(str(tmp_path / "first"), use_compression=False)
        store_b.anonymize(store_b.audit())
        store_b.export(str(tmp_path / "second"), use_compression=False)
    minted_by_a = {M(u, FIXED_A) for u in sources}
    (first,) = [pydicom.dcmread(p) for p in (tmp_path / "first").rglob("*.dcm")]
    assert not [el for el in first.iterall() if str(el.value) in minted_by_a]
    (second,) = [pydicom.dcmread(p) for p in (tmp_path / "second").rglob("*.dcm")]
    assert (second.StudyInstanceUID, second.SOPInstanceUID, second.SeriesInstanceUID,
            second.FrameOfReferenceUID,
            second.ReferencedImageSequence[0].ReferencedSOPInstanceUID) == tuple(
                M(u, FIXED_B) for u in sources)
    with sqlite3.connect(str(db)) as conn:
        rows = [d for (d,) in conn.execute(
            "SELECT details FROM audit_log WHERE action_type='REMEDIATION_DECLINED'")]
    refused = [d for d in rows if "is not this store's replacement for the UID" in d]
    assert refused and not [d for d in refused for u in minted_by_a if u in d], rows


def _series_uid_absent(tmp_path):
    """A CT whose file carries no Series Instance UID: ingest generates one
    for the Series (#554's L8 half), and the instance holds no copy of it."""
    src = tmp_path / "src"
    src.mkdir()
    _ct(src / "a.dcm", "1.2.3.99.91", study="1.2.3.99.90")
    ds = pydicom.dcmread(str(src / "a.dcm"))
    del ds.SeriesInstanceUID
    ds.save_as(str(src / "a.dcm"))
    cfg = tmp_path / "c.yaml"
    cfg.write_text("privacy_profile: basic\n", encoding="utf-8")
    return src, str(cfg)


@pytest.mark.parametrize("reaudit", [False, True], ids=["same-report", "fresh-audit"])
def test_a_copy_less_instance_bears_its_series_finding(tmp_path, reaudit):
    """Where an instance carries a top-level Series Instance UID, its own
    copy's finding follows the Series (#624) and keeps it IDENTIFIED while
    the Series is not acted on. An instance whose file carried none has no
    such finding, and only its Series' finding can say so: the scan records
    it IDENTIFIED while its Series has one, a pass that leaves the Series
    unhandled keeps it from reading REMEDIATED, and the export grades
    REVIEW_REQUIRED, not PASS (review of #544, finding 2). Kills the scan's
    carry dropped, the pass-end Series settle dropped, a held-open Series
    demoting itself but not its instances, and the Series list not handed
    to the service."""
    src, cfg = _series_uid_absent(tmp_path)
    with DicomSession(str(tmp_path / "s.db")) as session:
        load_fixed_secret(session)
        session.load_config(cfg)
        session.ingest(str(src))
        series = session.store.patients[0].studies[0].series[0]
        (inst,) = series.instances
        assert "0020,000e" not in inst.attributes
        report = session.audit()
        assert inst.phi_status is PhiStatus.IDENTIFIED
        session.anonymize([f for f in report if f.entity_type != "Series"])
        assert inst.phi_status is PhiStatus.IDENTIFIED
        if reaudit:
            again = session.audit()
            assert [(f.entity_type, f.tag) for f in again] == [("Series", "0020,000e")]
            assert inst.phi_status is PhiStatus.IDENTIFIED
        session.export(str(tmp_path / "out"), use_compression=False)
        grade = _validation_status(session, tmp_path, "r.md")
    assert grade and "REVIEW_REQUIRED" in grade[0], grade


# --------------------------------------------------------------------
# The Series across a reopen (review of #544, round 2, R2-1)
# --------------------------------------------------------------------

def _audited_and_closed(tmp_path, shape):
    """Audit under basic, save, close. Returns the report, the config, and
    the Series' source UID -- for the copy-less shape, the one ingest
    generated."""
    src, cfg = (_series_filtered if shape == "copy" else _series_uid_absent)(tmp_path)
    with DicomSession(str(tmp_path / "s.db")) as session:
        load_fixed_secret(session)
        session.load_config(cfg)
        session.ingest(str(src))
        report = session.audit()
        series = session.store.patients[0].studies[0].series[0]
        assert series.instances[0].phi_status is PhiStatus.IDENTIFIED
        source = series.series_instance_uid
        session.save(sync=True)
    return report, cfg, source


@pytest.mark.parametrize("shape", ["copy", "copy-less"])
def test_a_reopened_pass_without_the_series_does_not_grade_pass(tmp_path, shape):
    """A Series has no stored status, so what its finding said is gone once
    the session closes, and a plain list carries no tally: a pass after a
    reopen handed every finding but the Series' stamped the instance
    REMEDIATED over a file carrying the source Series Instance UID, and the
    export graded PASS. At the end of every pass a Series whose UID the
    policy would still raise -- a value-less REPLACE on `0020,000e`, and a
    UID this store did not mint -- keeps its instances from reading
    REMEDIATED, tally or none. Kills the live check dropped, or run only
    under a tally."""
    report, cfg, source = _audited_and_closed(tmp_path, shape)
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.load_config(cfg)
        session.anonymize([f for f in report.findings if f.entity_type != "Series"])
        series = session.store.patients[0].studies[0].series[0]
        assert series.series_instance_uid == source
        assert series.instances[0].phi_status is PhiStatus.IDENTIFIED
        session.export(str(tmp_path / "out"), use_compression=False)
        grade = _validation_status(session, tmp_path, "r.md")
    assert grade and "REVIEW_REQUIRED" in grade[0], grade
    (path,) = (tmp_path / "out").rglob("*.dcm")
    assert pydicom.dcmread(path).SeriesInstanceUID == source


@pytest.mark.parametrize("shape", ["copy", "copy-less"])
def test_a_reopened_pass_with_the_series_grades_pass(tmp_path, shape):
    """The same reopen handed the whole report replaces the Series UID, and
    the check reads the UID the Series holds at the pass end -- the one it
    minted -- so the instance reads REMEDIATED and the export grades PASS.
    Kills the check reading the pass-start UID, or ignoring whether the
    UID was minted here. The copy-less shape is graded by its status only:
    its export writes a Series UID ingest generated, and says so in a
    WARNING row (#584) that grades it REVIEW_REQUIRED on its own."""
    report, cfg, source = _audited_and_closed(tmp_path, shape)
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.load_config(cfg)
        session.anonymize(list(report.findings))
        series = session.store.patients[0].studies[0].series[0]
        assert series.series_instance_uid == M(source)
        assert series.instances[0].phi_status is PhiStatus.REMEDIATED
        session.export(str(tmp_path / "out"), use_compression=False)
        grade = _validation_status(session, tmp_path, "r.md")
    (path,) = (tmp_path / "out").rglob("*.dcm")
    assert pydicom.dcmread(path).SeriesInstanceUID == M(source)
    if shape == "copy":
        assert grade and "PASS" in grade[0], grade


def test_a_series_kept_by_the_audited_policy_is_not_held_open(tmp_path):
    """The check reads the policy the session last audited under, as the
    lock does (review of #574), and the configuration only when no audit
    ran: audited under a config that KEEPs the Series Instance UID, then
    handed the report under basic, the Series was never raised, the pass
    applied everything that was, and the export grades PASS with the
    source UID the audited policy kept -- #555's rule as released. Kills
    the check reading the configuration over the audited policy, or
    dropping the value-less REPLACE condition."""
    src, _ = _series_filtered(tmp_path)
    keep = tmp_path / "keep.yaml"
    keep.write_text('privacy_profile: basic\nphi_tags:\n'
                    '  "0020,000e": {name: Series Instance UID, action: KEEP}\n',
                    encoding="utf-8")
    basic = tmp_path / "basic.yaml"
    basic.write_text("privacy_profile: basic\n", encoding="utf-8")
    with DicomSession(str(tmp_path / "s.db")) as session:
        load_fixed_secret(session)
        session.ingest(str(src))
        session.load_config(str(keep))
        report = session.audit()
        assert not [f for f in report if f.entity_type == "Series"]
        session.load_config(str(basic))
        session.anonymize(report)
        series = session.store.patients[0].studies[0].series[0]
        assert series.series_instance_uid == "1.2.3.99.72"
        assert series.instances[0].phi_status is PhiStatus.REMEDIATED
        session.export(str(tmp_path / "out"), use_compression=False)
        grade = _validation_status(session, tmp_path, "r.md")
    assert grade and "PASS" in grade[0], grade
