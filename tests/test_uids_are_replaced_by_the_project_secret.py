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
    `_live_uid`, and a Series' UID not read by `_uid_of`."""
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
