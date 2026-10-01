"""A private element holding a UID that the keyed UID replacement replaces
in the same instance gets the same replacement (#765, owner ruling Q6 A,
2026-10-01).

Measured at 7579d4df on CT_small under `privacy_profile: basic` and
`remove_private_tags: false`, with three private UI elements holding the
file's Study, Series and SOP Instance UIDs (`0009,101e`, `0033,1010`,
`0033,1012`). Every source UID was exported beside its replacement, under
PASS and `(0012,0062) YES`, from an explicit-VR source (str in the graph)
and an implicit-VR one (bytes in the graph) alike, so the export linked
back to its source.

Now, when private tags are kept, a private element (not a private
creator) whose value, or one of whose backslash-separated values, is a
UID this scan replaces in the same instance is raised with that UID's
keyed replacement, in the element's own type. Stated limits: a UID only
another instance carries is not matched, and text that merely contains a
UID is not.
"""
import os
import sqlite3

import pydicom
import pytest
from pydicom.uid import ExplicitVRLittleEndian, ImplicitVRLittleEndian

from isocenter import Session
from isocenter.privacy import _replacement_uid_for

from support.ct_small_files import write_ct

CONFIG = "version: '2.0'\nprivacy_profile: basic\nremove_private_tags: {remove}\n"
PRIVATE = (0x0009101E, 0x00331010, 0x00331012)


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _source(tmp_path, implicit=False, other_uid=None, creator_uid=False,
            serial=None, multi=None):
    path = write_ct(tmp_path / "in" / "a.dcm", "PA", 765)
    ds = pydicom.dcmread(path)
    if serial is not None:
        ds.DeviceSerialNumber = serial
    block = ds.private_block(0x0009, "GEMS_IDEN_01", create=True)
    block.add_new(0x1E, "UI", ds.StudyInstanceUID)
    own = ds.private_block(0x0033, "B1 TEST", create=True)
    own.add_new(0x10, "UI", ds.SeriesInstanceUID)
    own.add_new(0x11, "LO", "ref " + ds.SOPInstanceUID)
    own.add_new(0x12, "UI", ds.SOPInstanceUID)
    if other_uid is not None:
        own.add_new(0x13, "UI", other_uid)
    if multi is not None:
        own.add_new(0x14, "UI", [ds.SeriesInstanceUID, multi])
    if creator_uid:
        # A private creator whose text is the Study UID: a creator names a
        # block, it is never a copy of anything.
        ds.add_new(0x00350010, "LO", ds.StudyInstanceUID)
        ds.add_new(0x00351001, "LO", "kept")
    ds.file_meta.TransferSyntaxUID = (ImplicitVRLittleEndian if implicit
                                      else ExplicitVRLittleEndian)
    ds.save_as(path, enforce_file_format=True)
    return ds


def _session(tmp_path, remove="false"):
    (tmp_path / "cfg.yaml").write_text(CONFIG.format(remove=remove))
    session = Session(str(tmp_path / "s.db"))
    session.ingest(str(tmp_path / "in"))
    session.load_config(str(tmp_path / "cfg.yaml"))
    return session


def _instance(session):
    [patient] = session.store.patients
    return patient.studies[0].series[0].instances[0]


def _export(session, tmp_path):
    session.export(str(tmp_path / "out"), use_compression=False,
                   show_progress=False)
    files = [os.path.join(root, f) for root, _, names in os.walk(tmp_path / "out")
             for f in names if f.endswith(".dcm")]
    assert len(files) == 1
    return files[0]


def _text(value):
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("ascii", "replace").rstrip("\x00 ")
    return str(value)


def _declines(session):
    session.store_backend.flush_audit_queue()
    with sqlite3.connect(session.persistence_file) as conn:
        return conn.execute("SELECT entity_uid, details FROM audit_log "
                            "WHERE action_type='REMEDIATION_DECLINED'").fetchall()


@pytest.mark.parametrize("implicit", [False, True], ids=["explicit", "implicit"])
def test_each_private_copy_takes_its_uids_replacement(tmp_path, implicit):
    src = _source(tmp_path, implicit=implicit)
    with _session(tmp_path) as session:
        inst = _instance(session)
        typed = {t: isinstance(inst.attributes[t], bytes) for t in
                 ("0009,101e", "0033,1010", "0033,1012")}
        assert set(typed.values()) == {implicit}
        report = session.audit()
        assert sorted(f.tag for f in report.findings
                      if f.tag in ("0009,101e", "0033,1010", "0033,1012")) == [
                          "0009,101e", "0033,1010", "0033,1012"]
        session.anonymize(report)
        assert {t: isinstance(inst.attributes[t], bytes) for t in typed} == typed
        assert _declines(session) == []
        path = _export(session, tmp_path)
    out = pydicom.dcmread(path)
    assert _text(out[0x0009101E].value) == out.StudyInstanceUID
    assert _text(out[0x00331010].value) == out.SeriesInstanceUID
    assert _text(out[0x00331012].value) == out.SOPInstanceUID
    sources = {src.StudyInstanceUID, src.SeriesInstanceUID, src.SOPInstanceUID}
    assert [(el.tag, _text(el.value)) for el in out.iterall()
            if isinstance(el.value, (str, bytes)) and _text(el.value) in sources] == []
    # `0033,1011` holds the SOP UID inside text: a stated limit, unmatched.
    assert _text(out[0x00331011].value) == "ref " + src.SOPInstanceUID


def test_a_second_audit_raises_nothing_on_the_copies(tmp_path):
    _source(tmp_path)
    with _session(tmp_path) as session:
        session.anonymize(session.audit())
        again = session.audit()
        assert [f.tag for f in again.findings
                if int(f.tag.split(",")[0], 16) % 2] == []


def test_a_private_uid_of_no_uid_of_its_instance_is_kept(tmp_path):
    other = "1.2.826.0.1.3680043.10.9999.4242"
    _source(tmp_path, other_uid=other)
    with _session(tmp_path) as session:
        report = session.audit()
        assert [f for f in report.findings if f.tag == "0033,1013"] == []
        session.anonymize(report)
        out = pydicom.dcmread(_export(session, tmp_path))
    assert _text(out[0x00331013].value) == other


@pytest.mark.parametrize("implicit", [False, True], ids=["explicit", "implicit"])
def test_a_multi_valued_copy_replaces_its_uid_and_keeps_the_other_value(tmp_path, implicit):
    """A private UI holding the instance's Series UID beside a UID of no
    element of the instance: the first value takes the replacement, the
    second is kept as it was, and nothing is declined. The pass checks each
    value (`_foreign_uid_refused`) as either left as it was or this store's
    replacement of it; a check that accepted only the replacement would
    decline the whole element and leave the source Series UID in the file
    (review of #896, finding 2)."""
    other = "1.2.826.0.1.3680043.10.9999.4343"
    src = _source(tmp_path, implicit=implicit, multi=other)
    with _session(tmp_path) as session:
        report = session.audit()
        assert [f.tag for f in report.findings if f.tag == "0033,1014"] == ["0033,1014"]
        session.anonymize(report)
        assert _declines(session) == []
        out = pydicom.dcmread(_export(session, tmp_path))
    value = out[0x00331014].value
    texts = (_text(value).split("\\") if isinstance(value, bytes)
             else [str(v) for v in (value if isinstance(value, (list, pydicom.multival.MultiValue))
                                    else [value])])
    assert texts == [out.SeriesInstanceUID, other]
    assert out.SeriesInstanceUID != src.SeriesInstanceUID


def test_a_private_creator_is_never_replaced(tmp_path):
    src = _source(tmp_path, creator_uid=True)
    with _session(tmp_path) as session:
        report = session.audit()
        assert [f for f in report.findings if f.tag == "0035,0010"] == []
        session.anonymize(report)
        out = pydicom.dcmread(_export(session, tmp_path))
    assert _text(out[0x00350010].value) == src.StudyInstanceUID


def test_with_private_tags_removed_the_sweep_decides(tmp_path):
    _source(tmp_path)
    with _session(tmp_path, remove="true") as session:
        report = session.audit()
        private = [f for f in report.findings if f.tag == "0009,101e"]
        assert [f.remediation_proposal.action_type for f in private] == ["REMOVE_TAG"]
        session.anonymize(report)
        out = pydicom.dcmread(_export(session, tmp_path))
    assert 0x0009101E not in out


def test_a_redacted_instances_copy_of_its_source_sop_uid_is_replaced(tmp_path):
    """The redacted instance's SOP UID is minted; the private copy holds
    the source UID it was read under, which is replaced like any other."""
    src = _source(tmp_path, serial="SN-765")
    with _session(tmp_path) as session:
        inst = _instance(session)
        serial = session.store.patients[0].studies[0].series[0] \
            .equipment.device_serial_number
        assert serial
        session.configuration.add_rule(serial, redaction_zones=[[0, 4, 0, 4]])
        assert session.redact() == 1
        assert inst.sop_instance_uid != src.SOPInstanceUID
        session.anonymize(session.audit())
        secret = session.store_backend._project_secret_for_use(diagnose=False)
        assert _text(inst.attributes["0033,1012"]) == _replacement_uid_for(
            src.SOPInstanceUID, secret)
        path = _export(session, tmp_path)
    out = pydicom.dcmread(path)
    assert [el.tag for el in out.iterall() if isinstance(el.value, (str, bytes))
            and _text(el.value) == src.SOPInstanceUID] == []


def test_a_private_element_with_a_rule_of_its_own_follows_its_rule(tmp_path):
    """The configuration decides: a KEEP on the private tag keeps the
    copy, and no second finding is raised beside the rule's."""
    src = _source(tmp_path)
    (tmp_path / "cfg.yaml").write_text(
        CONFIG.format(remove="false")
        + "phi_tags:\n  '0009,101e':\n    action: KEEP\n")
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        session.load_config(str(tmp_path / "cfg.yaml"))
        report = session.audit()
        assert [f for f in report.findings if f.tag == "0009,101e"] == []
        assert [f.tag for f in report.findings if f.tag == "0033,1010"] == ["0033,1010"]
        session.anonymize(report)
        out = pydicom.dcmread(_export(session, tmp_path))
    assert _text(out[0x0009101E].value) == src.StudyInstanceUID
