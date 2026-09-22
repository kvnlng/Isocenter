"""A sequence item's PHI status survives a reopen (#564).

An item remediated inside a sequence read `REMEDIATED` in the session that
remediated it and `UNSCANNED` after `save()` and a reopen, while its
instance kept `REMEDIATED`: only patients, studies and instances had a
status column. The item status is the one record of *which* nested item a
pass remediated.

The item's status is now stored with the item, in `attributes_json`, as
`"__phi__": {"status": ...}` beside `__vrs__` and `__shifted__`, only when
it is not `unscanned`, and never at the root (the root's status has its
own columns: one home per fact). Hydration pops the key before the
attributes are applied, at every depth, and records the status as the
item's last step, after its own sub-sequences are built. An item is never
scanned (`audit()` records no status on it), so its status carries no
policy; its instance carries the policy (#555).

Replaces `test_nested_remediation_reaches_the_instance.py::
test_a_nested_item_status_is_session_scoped`, the characterization that
said to retire it once this was fixed.

**Why this file imports what it does.** The serializer and hydration are
in `isocenter.persistence`, driven through `isocenter.session`, and the
status on `isocenter.entities`; see `test_mutation_probe_targets.py`.
"""
import json
import pathlib
import sqlite3

import pydicom
import pytest
from pydicom.data import get_testdata_file
from pydicom.dataset import Dataset
from pydicom.sequence import Sequence

from isocenter.entities import PhiStatus
from isocenter.session import DicomSession

SEQ = "0040,0275"          # Request Attributes Sequence
STEP = "0040,0007"         # Scheduled Procedure Step Description
INNER_SEQ = "0040,0008"    # Scheduled Protocol Code Sequence
MEANING = "0008,0104"      # Code Meaning
NESTED_PHI = "Nested^PHI"


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "tags.yaml"  # JSON is YAML; the loader wants the suffix
    path.write_text(json.dumps({"privacy_profile": "none",
                                "phi_tags": {STEP: {"action": "REPLACE"}}}))
    return str(path)


def _write_nested_only_ct(path, inner=False):
    """CT_small whose only finding under the STEP rule is the nested one.

    With `inner`, the remediated item also holds a sequence of its own, so
    its hydration calls `add_sequence_item` on it before the status can be
    restored.
    """
    ds = pydicom.dcmread(get_testdata_file("CT_small.dcm"))
    item = Dataset()
    item.ScheduledProcedureStepDescription = NESTED_PHI
    if inner:
        code = Dataset()
        code.CodeMeaning = "A protocol"
        item.ScheduledProtocolCodeSequence = Sequence([code])
    ds.RequestAttributesSequence = Sequence([item])
    ds.remove_private_tags()
    ds.PatientID = "ANON_probe"
    ds.PatientName = "ANONYMIZED"
    if "StudyDate" in ds:
        del ds.StudyDate
    path.parent.mkdir(parents=True, exist_ok=True)
    ds.save_as(str(path))


def _remediated_store(tmp_path, config, inner=False):
    _write_nested_only_ct(tmp_path / "in" / "a.dcm", inner=inner)
    db = str(tmp_path / "store.db")
    with DicomSession(db) as session:
        session.ingest(str(tmp_path / "in"))
        session.load_config(config)
        session.anonymize(session.audit())
        inst = _instance(session)
        assert inst.sequences[SEQ].items[0].phi_status is PhiStatus.REMEDIATED
        policy = inst.phi_status_policy
        session.save(sync=True)
    return db, policy


def _instance(session):
    [patient] = session.store.patients
    return patient.studies[0].series[0].instances[0]


def _item(session):
    return _instance(session).sequences[SEQ].items[0]


def _stored_json(db):
    with sqlite3.connect(db) as conn:
        [(text,)] = conn.execute(
            "SELECT attributes_json FROM instances").fetchall()
    return json.loads(text)


def _write_json(db, data):
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE instances SET attributes_json = ?",
                     (json.dumps(data),))


def _every_item(entity):
    for seq in entity.sequences.values():
        for item in seq.items:
            yield item
            yield from _every_item(item)


def test_a_remediated_item_reads_remediated_after_a_reopen(tmp_path, config):
    """Kills: the serializer omitting the key; no restore."""
    db, policy = _remediated_store(tmp_path, config)
    assert _stored_json(db)["__sequences__"][SEQ][0]["__phi__"] == {
        "status": "remediated"}
    with DicomSession(db) as session:
        item = _item(session)
        assert item.attributes[STEP] != NESTED_PHI
        assert item.phi_status is PhiStatus.REMEDIATED
        assert item.phi_status_policy is None
        inst = _instance(session)
        assert inst.phi_status is PhiStatus.REMEDIATED
        assert inst.phi_status_policy == policy


def test_an_item_with_its_own_sequence_keeps_its_status(tmp_path, config):
    """Kills: stamping before the item's sub-sequences hydrate, whose
    `add_sequence_item` advances the item's revision past the stamp."""
    db, _ = _remediated_store(tmp_path, config, inner=True)
    with DicomSession(db) as session:
        item = _item(session)
        assert item.sequences[INNER_SEQ].items[0].attributes[MEANING] == (
            "A protocol")
        assert item.phi_status is PhiStatus.REMEDIATED


def test_the_status_key_is_not_a_tag(tmp_path, config):
    """Kills: the pop missing, or placed after `attributes.update`."""
    db, _ = _remediated_store(tmp_path, config)
    with DicomSession(db) as session:
        inst = _instance(session)
        for item in _every_item(inst):
            assert "__phi__" not in item.attributes
        assert "__phi__" not in inst.attributes
        frame = session.export_dataframe(expand_metadata=True)
        assert not [c for c in frame.columns if "__phi__" in str(c)]
        session.export(str(tmp_path / "out"))
    [path] = list(pathlib.Path(tmp_path / "out").rglob("*.dcm"))
    assert b"__phi__" not in path.read_bytes()


def test_a_reopened_graph_has_nothing_to_save(tmp_path, config):
    """Kills: the stamp placed after `mark_subtree_persisted`."""
    db, _ = _remediated_store(tmp_path, config, inner=True)
    with DicomSession(db) as session:
        [patient] = session.store.patients
        entities = [patient, patient.studies[0], patient.studies[0].series[0],
                    _instance(session), *_every_item(_instance(session))]
        assert [e for e in entities if e.has_unsaved_changes] == []


def test_an_item_edited_after_remediation_stores_no_status(tmp_path, config):
    """Kills: the serializer writing the raw slot instead of the
    revision-checked status."""
    db, _ = _remediated_store(tmp_path, config)
    with DicomSession(db) as session:
        item = _item(session)
        item.set_attr(STEP, "Edited")
        assert item.phi_status is PhiStatus.UNSCANNED
        _instance(session).mark_modified()
        session.save(sync=True)
    assert "__phi__" not in _stored_json(db)["__sequences__"][SEQ][0]
    with DicomSession(db) as session:
        assert _item(session).phi_status is PhiStatus.UNSCANNED


@pytest.mark.parametrize("stored", [
    "remediated", ["remediated"], {"status": "bogus"}, {"status": None},
    {"status": ["remediated"]}],
    ids=["bare-string", "list", "unknown", "null", "list-status"])
def test_a_malformed_item_status_reads_unscanned(tmp_path, config, stored):
    """Kills: a `KeyError` or `TypeError` escaping hydration; an unknown
    value read as an assurance."""
    db, _ = _remediated_store(tmp_path, config)
    data = _stored_json(db)
    data["__sequences__"][SEQ][0]["__phi__"] = stored
    _write_json(db, data)
    with DicomSession(db) as session:
        item = _item(session)
        assert item.phi_status is PhiStatus.UNSCANNED
        assert "__phi__" not in item.attributes


def test_the_root_status_lives_in_its_columns_only(tmp_path, config):
    """Kills: the root writing a second copy; the restore running on an
    `Instance` (no `isinstance` guard)."""
    db, _ = _remediated_store(tmp_path, config)
    data = _stored_json(db)
    assert "__phi__" not in data
    data["__phi__"] = {"status": "cleared"}
    _write_json(db, data)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE instances SET phi_status = 'identified'")
    with DicomSession(db) as session:
        inst = _instance(session)
        assert inst.phi_status is PhiStatus.IDENTIFIED
        assert "__phi__" not in inst.attributes


def test_load_patient_restores_item_statuses(tmp_path, config):
    """Kills: only `load_all` wired."""
    db, _ = _remediated_store(tmp_path, config)
    with DicomSession(db) as session:
        patient = session.store_backend.load_patient("ANON_probe")
    item = patient.studies[0].series[0].instances[0].sequences[SEQ].items[0]
    assert item.phi_status is PhiStatus.REMEDIATED
    assert not item.has_unsaved_changes


def test_a_lock_keeps_the_item_status(tmp_path, config):
    """The lock (`lock_identities(persist=True)`) rewrites `attributes_json`
    through `update_attributes`, and nothing else writes the row there.
    Kills: that path serializing without the key."""
    _write_nested_only_ct(tmp_path / "in" / "a.dcm")
    db = str(tmp_path / "store.db")
    with DicomSession(db) as session:
        session.ingest(str(tmp_path / "in"))
        session.load_config(config)
        session.save(sync=True)
        session.anonymize(session.audit())
        assert _item(session).phi_status is PhiStatus.REMEDIATED
        session.store_backend.update_attributes([_instance(session)])
        assert _stored_json(db)["__sequences__"][SEQ][0]["__phi__"] == {
            "status": "remediated"}
