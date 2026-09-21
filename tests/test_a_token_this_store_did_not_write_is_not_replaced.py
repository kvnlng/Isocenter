"""A token this store did not write is not replaced by what a pass left
(#607).

A file exported with a locked identity carries the token `(0400,0500)`
but not the record of what `anonymize()` wrote (`__remediated__`), which
is never a written byte. So a new store that ingested such a file judged
a re-lock by `ANONYMIZED`/`ANON_...` alone, and under a `value:` or a
`KEEP` rule it stashed the pass's output over the original the token
held (measured on 347ee93: `Project-X` over the held name, a shifted
birth date over the held `19700101`; that store's recovery then answered
with the pass's output).

The lock now records the digest of the token it embeds on the instance
-- `Instance._locked_token`, persisted as a `__locked__` key beside
`__shifted__` and `__remediated__`, never a tag and never a byte of a
file -- and a re-lock over a token **no such record vouches for** is
refused where it would change any value that token holds. A token this
store wrote keeps #399's rule: a deliberate `set_attr` followed by a
re-lock stashes the new value, in session and after a reopen.

Every refusal here is asserted together with the held token unchanged,
because a refusal that has already overwritten the token is the defect
with a message. The exact texts are pinned: no message carries a Patient
ID, a held value or a current value (P6).

**Why this file imports what it does.** `isocenter.session`,
`isocenter.entities`, `isocenter.reversibility` and
`isocenter.persistence` are named, so their probe rows are charged.
"""
import hashlib
import json
import sqlite3
from datetime import date

import numpy as np
import pydicom
import pytest
import yaml

from isocenter.entities import DicomItem, Instance, Patient, Series, Study
from isocenter.persistence import SqliteStore  # noqa: F401  (probe row)
from isocenter.privacy import PhiInspector
from isocenter.reversibility import ReversibilityService
from isocenter.session import DicomSession

from support.ct_small_files import write_ct

NAME = "Orig^Name"
PID = "P607"
TAGS = ["0010,0010", "0010,0020"]
BIRTH = "0010,0030"
ACCESSION = "0008,0050"
SEQ, CONTENT, SYNTAX = "0400,0500", "0400,0510", "0400,0520"
KEY = "__locked__"
KEEP_BOTH = {"0010,0010": {"action": "KEEP"}, "0010,0020": {"action": "KEEP"}}
PROJECT_X = {"0010,0010": {"action": "REPLACE", "value": "Project-X"},
             "0010,0020": {"action": "KEEP"}}
SOURCE = {"PatientBirthDate": "19700101", "AccessionNumber": "ACC123"}


def mismatch_refusal(tag, named=True):
    """The #607 refusal, byte for byte: for a tag the lock names, the
    token would hold "a different one"; for a tag it does not name, the
    tag leaves the token -- "nothing", 2(a)'s word (review of #633, P-4)."""
    lost = "a different one" if named else "nothing (tags_to_lock does not name it)"
    return ("lock_identities: this patient's identity token did not come from "
            f"this store, so the value it holds in {tag} cannot be told from what "
            f"anonymize() left, and this lock would replace it with {lost}. "
            "recover_patient_identity(<its Patient ID>, restore=True) puts "
            "the held values back, and a lock after that is accepted; the token "
            "this call would have written is unchanged.")


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _write(tmp_path, suffix="6071", name=NAME, pid=PID, **source):
    path = write_ct(tmp_path / "in" / f"{suffix}.dcm", pid, suffix, name=name)
    if source:
        ds = pydicom.dcmread(path)
        for keyword, value in source.items():
            setattr(ds, keyword, value)
        ds.save_as(path)
    return path


def _session(tmp_path, rules=None, db="s.db", studies=1, **source):
    """A reversible session over one CT (or `studies` CTs of one patient)
    carrying a birth date and an accession, with `rules` loaded."""
    for n in range(studies):
        _write(tmp_path, suffix=f"607{n + 1}", **{**SOURCE, **source})
    session = DicomSession(str(tmp_path / db))
    session.enable_reversible_anonymization(str(tmp_path / "k.key"))
    session.ingest(str(tmp_path / "in"))
    if rules is not None:
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text(yaml.safe_dump({"phi_tags": rules}), encoding="utf-8")
        session.load_config(str(cfg))
    return session


def _reopened(tmp_path, db="s.db"):
    session = DicomSession(str(tmp_path / db))
    session.enable_reversible_anonymization(str(tmp_path / "k.key"))
    return session


def _instance(session, study=0):
    return session.store.patients[0].studies[study].series[0].instances[0]


def _instances(session):
    return [inst for p in session.store.patients for st in p.studies
            for se in st.series for inst in se.instances]


def _token(instance):
    return bytes(instance.sequences[SEQ].items[0].attributes[CONTENT])


def _held(session, instance):
    return session.reversibility_service.recover_original_data(instance)


def _strip_stamp(db):
    """The pre-0.9.8 shape: a store whose rows carry the token and no
    `__locked__`. Everything else a 0.9.7 store holds for a locked, not
    yet anonymized patient is the same: the one-item Encrypted Attributes
    Sequence (#399, 0.9.4), its `(0400,0520)`, the same payload under the
    same key, and no `__remediated__` (no pass ran). The rows differ from
    a real 0.9.7 store only in columns the lock never reads
    (`patients.jitter_scheme`, the project secret)."""
    with sqlite3.connect(str(db)) as conn:
        conn.execute("UPDATE instances SET attributes_json = "
                     f"json_remove(attributes_json, '$.{KEY}')")
        rows = conn.execute("SELECT attributes_json FROM instances").fetchall()
    assert rows and not any(KEY in row[0] for row in rows)


def _reingested(tmp_path, rules, tags):
    """Lock, `anonymize()`, `export()`; a second store under the same key
    ingests the export. Returns (session, held before the pass)."""
    with _session(tmp_path, rules) as session:
        session.lock_identities(PID, tags_to_lock=tags)
        held = _held(session, _instance(session))
        session.anonymize(session.audit())
        session.export(str(tmp_path / "out"))
    session = DicomSession(str(tmp_path / "again.db"))
    session.enable_reversible_anonymization(str(tmp_path / "k.key"))
    session.ingest(str(tmp_path / "out"))
    instance = _instance(session)
    assert instance._locked_token is None, "a stamp arrived in a file"
    assert _held(session, instance) == held
    return session, held


def _hand_patient(session, pid=PID, name=NAME, studies=1, instances=1):
    """A hand-built patient with no file behind it (the #399 shape)."""
    patient = Patient(pid, name)
    for s in range(studies):
        study = Study(f"ST_{s + 1}", date(2023, 1, 1))
        series = Series(f"SE_{s + 1}", "CT", 1)
        for i in range(instances):
            inst = Instance(f"SOP_{s + 1}_{i + 1}", "1.2.840.10008.5.1.4.1.1.2", i + 1)
            inst.file_path = None
            inst.set_attr("0010,0010", name)
            inst.set_attr("0010,0020", pid)
            inst.set_pixel_data(np.zeros((8, 8), dtype=np.uint16))
            series.instances.append(inst)
        study.series.append(series)
        patient.studies.append(study)
    session.store.patients.append(patient)
    return patient


# --- T2, T3: the stamp survives a reopen; its absence is the refusal ----------


def test_a_changed_value_relock_still_wins_after_a_reopen(tmp_path):
    """T2. #399 across a save and a reopen: the stamp is stored with the
    token and read back, so a deliberate `set_attr` followed by a re-lock
    stashes the new value. Kills the stamp not serialized (M5) and the
    hydration that never assigns it."""
    with _session(tmp_path, KEEP_BOTH) as session:
        session.lock_identities(PID, tags_to_lock=TAGS)
        digest = _instance(session)._locked_token
        assert digest == hashlib.sha256(_token(_instance(session))).hexdigest()
        session.save(sync=True)
    with _reopened(tmp_path) as session:
        instance = _instance(session)
        assert instance._locked_token == digest
        assert instance.identity_token_is_this_stores(_token(instance))
        instance.set_attr("0010,0010", "CHANGED^Value")
        session.lock_identities(PID, tags_to_lock=TAGS)
        assert _held(session, instance) == {"0010,0010": "CHANGED^Value", "0010,0020": PID}
        assert instance._locked_token == hashlib.sha256(_token(instance)).hexdigest()


def test_a_store_written_without_the_stamp_refuses_a_changed_value_relock(tmp_path):
    """T3. A store from before the stamp, made here by deleting it from
    the stored JSON (`_strip_stamp` says what else such a store holds): a
    changed-value re-lock is refused, the token is unchanged, and the
    refusal is the exact #607 text. This is the one call that worked on
    0.9.7 and now raises (Q2). Kills the unstamped check dropped (M1) and
    `all` weakened to `any` (M2, with T10)."""
    with _session(tmp_path, KEEP_BOTH) as session:
        session.lock_identities(PID, tags_to_lock=TAGS)
        held = _held(session, _instance(session))
        session.save(sync=True)
    _strip_stamp(tmp_path / "s.db")
    with _reopened(tmp_path) as session:
        instance = _instance(session)
        assert instance._locked_token is None
        token = _token(instance)
        instance.set_attr("0010,0010", "CHANGED^Value")
        with pytest.raises(RuntimeError) as caught:
            session.lock_identities(PID, tags_to_lock=TAGS)
        assert str(caught.value) == mismatch_refusal("0010,0010")
        assert _token(instance) == token
        assert _held(session, instance) == held
        assert instance._locked_token is None


def test_a_same_value_relock_over_an_unstamped_token_is_accepted_and_stamps(tmp_path):
    """The clause the BREAKING entry ends on: a lock that stashes the same
    values (re-running the lock cell) over a pre-0.9.8 token is accepted,
    and from then on the token is this store's. Kills the unstamped check
    refusing every unstamped token rather than a changed value (M12)."""
    with _session(tmp_path, KEEP_BOTH) as session:
        session.lock_identities(PID, tags_to_lock=TAGS)
        held = _held(session, _instance(session))
        session.save(sync=True)
    _strip_stamp(tmp_path / "s.db")
    with _reopened(tmp_path) as session:
        instance = _instance(session)
        session.lock_identities(PID, tags_to_lock=TAGS)
        assert _held(session, instance) == held
        assert instance.identity_token_is_this_stores(_token(instance))
        instance.set_attr("0010,0010", "CHANGED^Value")
        session.lock_identities(PID, tags_to_lock=TAGS)
        assert _held(session, instance) == {"0010,0010": "CHANGED^Value", "0010,0020": PID}


# --- T4, T4b: the workaround the message names ----------------------------------


def test_a_restore_then_a_relock_is_the_workaround_the_message_names(tmp_path):
    """T4. On the re-ingested export: the re-lock is refused, a restore
    puts the held values back, the lock after it is accepted and stamps,
    and a changed-value re-lock then behaves as #399 released. Kills the
    stamp never written by a successful lock (M12)."""
    session, held = _reingested(tmp_path, PROJECT_X, TAGS)
    with session:
        instance = _instance(session)
        pseudonym = session.store.patients[0].patient_id
        with pytest.raises(RuntimeError) as caught:
            session.lock_identities(pseudonym, tags_to_lock=TAGS)
        assert str(caught.value) == mismatch_refusal("0010,0010")
        session.recover_patient_identity(pseudonym, restore=True)
        pid = session.store.patients[0].patient_id
        assert pid == PID
        assert {tag: instance.attributes[tag] for tag in TAGS} == held
        session.lock_identities(pid, tags_to_lock=TAGS)
        assert instance.identity_token_is_this_stores(_token(instance))
        assert _held(session, instance) == held
        instance.set_attr("0010,0010", "CHANGED^Value")
        session.lock_identities(pid, tags_to_lock=TAGS)
        assert _held(session, instance) == {"0010,0010": "CHANGED^Value", "0010,0020": PID}


@pytest.mark.parametrize("studies", [1, 2], ids=["one_study", "two_studies"])
def test_the_workaround_on_a_pre_098_store(tmp_path, studies):
    """T4b (Q2, measured): on a store with the stamp stripped by SQL, the
    changed-value re-lock is refused; `recover_patient_identity(pid,
    restore=True)` then `lock_identities` succeed and stamp every
    instance; a changed-value re-lock then behaves as #399 released. Two
    studies as well as one: every instance of a normally locked patient
    carries the token, so recovery's instance walk (#616, I2's fix) finds
    one whichever study it ends on."""
    with _session(tmp_path, KEEP_BOTH, studies=studies) as session:
        session.lock_identities(PID, tags_to_lock=TAGS)
        held = _held(session, _instance(session))
        session.save(sync=True)
    _strip_stamp(tmp_path / "s.db")
    with _reopened(tmp_path) as session:
        assert len(session.store.patients[0].studies) == studies
        instances = _instances(session)
        assert len(instances) == studies
        for instance in instances:
            instance.set_attr("0010,0010", "CHANGED^Value")
        with pytest.raises(RuntimeError) as caught:
            session.lock_identities(PID, tags_to_lock=TAGS)
        assert str(caught.value) == mismatch_refusal("0010,0010")
        assert all(_held(session, inst) == held for inst in instances)
        session.recover_patient_identity(PID, restore=True)
        assert all(inst.attributes["0010,0010"] == NAME for inst in instances)
        session.lock_identities(PID, tags_to_lock=TAGS)
        assert all(inst.identity_token_is_this_stores(_token(inst)) for inst in instances)
        for instance in instances:
            instance.set_attr("0010,0010", "CHANGED^Value")
        session.lock_identities(PID, tags_to_lock=TAGS)
        assert all(_held(session, inst) == {"0010,0010": "CHANGED^Value", "0010,0020": PID}
                   for inst in instances)


# --- T5, T7: never a file byte, never a tag, never a scan input -----------------


def _bytes_under(root):
    files = [path for path in root.rglob("*") if path.is_file()]
    assert files, f"nothing was written under {root}"
    return [(path, path.read_bytes()) for path in files]


def test_no_written_file_carries_the_stamp(tmp_path):
    """T5 (Q3's R3-style test). `session.export()` and
    `DicomExporter.write_tree()` over a reopened locked CT, the WFDB
    export over a reopened locked ECG, and `export_dataframe(
    expand_metadata=True)`: not one byte and not one column of
    `__locked__`, while the store row holds it. Kills the stamp
    serialized into `attributes` (M6)."""
    from isocenter.io_handlers import DicomExporter
    from scripts.generate_waveform_test_data import write_fixture

    with _session(tmp_path, KEEP_BOTH) as session:
        session.lock_identities(PID, tags_to_lock=TAGS)
        # In the locking session too, before any save: a stamp that had
        # been written into `attributes` would be popped by the reopen
        # below and never seen there.
        assert KEY not in _instance(session).attributes
        session.export(str(tmp_path / "out_same"), format="dicom")
        frame = session.export_dataframe(str(tmp_path / "same.csv"), expand_metadata=True)
        assert KEY not in list(frame.columns)
        session.save(sync=True)
    with sqlite3.connect(str(tmp_path / "s.db")) as conn:
        [(row,)] = conn.execute("SELECT attributes_json FROM instances").fetchall()
    assert f'"{KEY}"' in row, "the store row does not carry the stamp"
    with _reopened(tmp_path) as session:
        session.export(str(tmp_path / "out"), format="dicom")
        DicomExporter.write_tree(session.store.patients[0], str(tmp_path / "tree"))
        frame = session.export_dataframe(str(tmp_path / "meta.csv"), expand_metadata=True)
        assert KEY not in list(frame.columns)
        assert KEY not in (tmp_path / "meta.csv").read_text(encoding="utf-8")
    for root in ("out_same", "out", "tree"):
        for path, data in _bytes_under(tmp_path / root):
            assert KEY.encode() not in data, path

    ecg = tmp_path / "ecg"
    write_fixture(str(ecg / "in" / "ecg.dcm"), num_samples=200,
                  patient_id="MRN-607", patient_name="Doe^Jane")
    with DicomSession(str(ecg / "s.db")) as session:
        session.enable_reversible_anonymization(str(tmp_path / "k.key"))
        session.ingest(str(ecg / "in"))
        session.lock_identities("MRN-607", tags_to_lock=TAGS)
        assert _instance(session)._locked_token
        session.save(sync=True)
    with DicomSession(str(ecg / "s.db")) as session:
        session.export(str(ecg / "wfdb"), format="wfdb")
    for path, data in _bytes_under(ecg / "wfdb"):
        assert KEY.encode() not in data, path


def _nest_a_stamp(db):
    """Hand-edit the stored JSON so a sequence item carries `__locked__`
    too: hydration reaches every depth, and the key must be popped there
    as well, onto an item that has no slot for it."""
    with sqlite3.connect(str(db)) as conn:
        uid, stored = conn.execute(
            "SELECT sop_instance_uid, attributes_json FROM instances").fetchone()
        data = json.loads(stored)
        assert KEY in data, "the lock left no stamp to find"
        data.setdefault("__sequences__", {})["0008,1140"] = [{
            "0008,1150": "1.2.840.10008.5.1.4.1.1.2", "0008,1155": "1.2.3.607",
            KEY: "f" * 64}]
        conn.execute("UPDATE instances SET attributes_json = ? WHERE sop_instance_uid = ?",
                     (json.dumps(data), uid))


def _items(item):
    found = []
    for seq in item.sequences.values():
        for nested in seq.items:
            found.append(nested)
            found.extend(_items(nested))
    return found


def test_a_reopened_store_scans_and_examines_no_stamp(tmp_path, monkeypatch, capsys):
    """T7. With `__locked__` at the root and hand-nested in a sequence
    item, the reopened graph has it as a tag nowhere, the nested item has
    no slot for it, and `audit()`/`examine()` hand the scan no instance
    carrying it at any depth. Kills the pop at the root only (M7) and the
    assignment at any depth (M8: a `DicomItem` is a slots dataclass and
    has no `_locked_token`)."""
    with _session(tmp_path, KEEP_BOTH) as session:
        session.lock_identities(PID, tags_to_lock=TAGS)
        session.save(sync=True)
    _nest_a_stamp(tmp_path / "s.db")
    handed = []
    real = PhiInspector._scan_instance

    def spy(self, instance, *args, **kwargs):
        handed.append([KEY in instance.attributes]
                      + [KEY in item.attributes for item in _items(instance)])
        return real(self, instance, *args, **kwargs)

    monkeypatch.setattr(PhiInspector, "_scan_instance", spy)
    with _reopened(tmp_path) as session:
        instance = _instance(session)
        assert instance._locked_token == hashlib.sha256(_token(instance)).hexdigest()
        assert KEY not in instance.attributes
        assert "0008,1140" in instance.sequences
        nested = instance.sequences["0008,1140"].items[0]
        assert KEY not in nested.attributes
        assert not hasattr(nested, "_locked_token")
        report = session.audit()
        session.examine()
    assert handed and not any(any(flags) for flags in handed), handed
    assert not [f for f in report.findings if "locked" in str(f.tag) + str(f.field_name)]
    assert "__locked__" not in capsys.readouterr().out


# --- T6: recorded before the embed ----------------------------------------------


def test_the_stamp_is_recorded_before_the_token_reaches_the_store(tmp_path, monkeypatch):
    """T6. At the moment `add_sequence` writes the Encrypted Attributes
    Sequence, the instance already vouches for the token about to go in
    it, on the first lock and on a re-lock. Recorded after the embed, a
    background `save()` between the two would store the token without
    its stamp, and the next re-lock would read this store's own token as
    foreign. A spy on `mark_modified` cannot see this (C's P-2 lesson);
    a spy on the write can. Kills the stamp moved after the embed (M3)."""
    seen = []
    real = Instance.add_sequence

    def spy(self, tag):
        if tag == SEQ:
            seen.append(self._locked_token)
        return real(self, tag)

    monkeypatch.setattr(Instance, "add_sequence", spy)
    with _session(tmp_path, KEEP_BOTH) as session:
        instance = _instance(session)
        session.lock_identities(PID, tags_to_lock=TAGS)
        first = hashlib.sha256(_token(instance)).hexdigest()
        instance.set_attr("0010,0010", "CHANGED^Value")
        session.lock_identities(PID, tags_to_lock=TAGS)
        second = hashlib.sha256(_token(instance)).hexdigest()
    assert first != second
    assert seen == [first, second], seen


def test_a_relock_between_the_two_reads_of_a_save_stores_the_token_with_its_stamp(tmp_path):
    """The read order in `_serialize_item`, pinned rather than argued
    (review of #633, F-1). A save reads the sequences -- where the token
    lives -- and then the stamp. A `dict` subclass on `inst.sequences`
    whose first `__len__` under the save runs a changed-value re-lock puts
    a new token and a new stamp between the two reads, deterministically:
    the snapshot must then hold the new token under the new stamp, its
    `__locked__` the digest of the token the same snapshot carries.

    Kills the reviewer's R1 -- the stamp read moved above
    `data = item.attributes.copy()` -- which survived every other test
    here: measured on 3.12 and 3.14t, R1 stores the new token under the
    old stamp (this store's own token, refused as foreign on the next
    changed-value re-lock) and the shipped order stores the new token
    under the new stamp."""
    with _session(tmp_path, KEEP_BOTH) as session:
        instance = _instance(session)
        session.lock_identities(PID, tags_to_lock=TAGS)
        old_token, old_stamp = _token(instance), instance._locked_token
        fired = []

        class Relocking(dict):
            """`if item.sequences:` is the first read of the sequences,
            and `bool(dict)` is `__len__`; the re-lock runs there, once."""

            def __len__(self):
                if not fired:
                    fired.append(True)
                    instance.set_attr("0010,0010", "CHANGED^Value")
                    session.lock_identities(PID, tags_to_lock=TAGS)
                return dict.__len__(self)

        instance.sequences = Relocking(instance.sequences)
        try:
            snapshot = session.store_backend._serialize_item(instance)
        finally:
            instance.sequences = dict(instance.sequences)
        assert fired, "the re-lock never ran under the save"
        new_token = _token(instance)
        assert new_token != old_token and instance._locked_token != old_stamp
        stored = bytes(snapshot["__sequences__"][SEQ][0][CONTENT])
        assert stored == new_token, "the save read the sequences before the re-lock"
        assert snapshot[KEY] == hashlib.sha256(stored).hexdigest(), (
            "the stored token and the stored stamp disagree")
        assert snapshot[KEY] == instance._locked_token


# --- T8, T9, T10: what the stamp is keyed on, the order, and every instance -----


def test_a_token_swapped_under_the_stamp_is_not_this_stores(tmp_path):
    """T8. The stamp is the digest of the token, not a flag: a token made
    under the same key and put in the token's place is not vouched for,
    and a changed-value re-lock over it is refused. Kills the stamp as a
    bool or a constant (M9)."""
    with _session(tmp_path, KEEP_BOTH) as session:
        instance = _instance(session)
        session.lock_identities(PID, tags_to_lock=TAGS)
        other = session.reversibility_service.generate_identity_token({"0010,0010": NAME})
        assert other != _token(instance)
        assert not instance.identity_token_is_this_stores(other)
        instance.sequences[SEQ].items[0].set_attr(CONTENT, other)
        instance.set_attr("0010,0010", "CHANGED^Value")
        with pytest.raises(RuntimeError) as caught:
            session.lock_identities(PID, tags_to_lock=TAGS)
        assert str(caught.value) == mismatch_refusal("0010,0010")
        assert _token(instance) == other


def test_a_relock_that_would_blank_a_held_value_keeps_its_own_message(tmp_path):
    """T9 (D9). The re-ingested export under the floor, re-locked on the
    birth date alone: the held date would be lost to the floor's blank,
    and the 2(a) text is what says so, byte for byte -- a blank is not
    "a different value". Kills the two checks swapped (M10)."""
    session, held = _reingested(tmp_path, None, [BIRTH])
    with session:
        instance = _instance(session)
        token = _token(instance)
        with pytest.raises(RuntimeError) as caught:
            session.lock_identities(session.store.patients[0].patient_id,
                                    tags_to_lock=[BIRTH])
        assert str(caught.value) == (
            "lock_identities: this patient already has a locked identity "
            "holding 0010,0030, and this lock would replace it with an empty "
            "value; lock identities before anonymize(), and do not re-lock a "
            "patient after it; the token this call would have written is "
            "unchanged.")
        assert _token(instance) == token
        assert _held(session, instance) == held


def test_every_token_on_the_patient_is_read_not_just_the_first_instances(tmp_path):
    """T10. A patient of two studies whose **second** study's instance
    carries a token nothing vouches for (its stamp gone, as an instance
    that arrived in a file has none), while the first study's token is
    this store's: a changed-value re-lock is refused, because the write
    replaces every token and the second is read too. Kills the plan
    reading the first instance's token only (M13) and `all` weakened to
    `any` (M2)."""
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.enable_reversible_anonymization(str(tmp_path / "k.key"))
        patient = _hand_patient(session, studies=2)
        session.lock_identities(PID, tags_to_lock=TAGS)
        first, second = [st.series[0].instances[0] for st in patient.studies]
        assert first.identity_token_is_this_stores(_token(first))
        second._locked_token = None
        held = _held(session, second)
        tokens = (_token(first), _token(second))
        for instance in (first, second):
            instance.set_attr("0010,0010", "CHANGED^Value")
        with pytest.raises(RuntimeError) as caught:
            session.lock_identities(PID, tags_to_lock=TAGS)
        assert str(caught.value) == mismatch_refusal("0010,0010")
        assert (_token(first), _token(second)) == tokens
        assert _held(session, second) == held


# --- T11, T12: the message, and the cost ----------------------------------------


def test_the_refusal_names_no_patient_id_and_no_value(tmp_path):
    """T11 (P6). The re-ingested `Project-X` shape: the Patient ID, the
    pseudonym, the held name and the current value are all absent from
    the message, which is the pinned text and nothing more. Kills a value
    interpolated "for clarity" (M14)."""
    session, _ = _reingested(tmp_path, PROJECT_X, TAGS)
    with session:
        pseudonym = session.store.patients[0].patient_id
        with pytest.raises(RuntimeError) as caught:
            session.lock_identities(pseudonym, tags_to_lock=TAGS)
    message = str(caught.value)
    assert message == mismatch_refusal("0010,0010")
    for secret in (PID, pseudonym, NAME, "Project-X"):
        assert secret not in message, message


def test_one_decrypt_per_distinct_token(tmp_path, monkeypatch):
    """T12. Twenty instances carrying one token: the plan decrypts it
    once, not twenty times. Kills a decrypt per instance (M15)."""
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.enable_reversible_anonymization(str(tmp_path / "k.key"))
        patient = _hand_patient(session, instances=20)
        session.lock_identities(PID, tags_to_lock=TAGS)
        instances = patient.studies[0].series[0].instances
        assert len({_token(inst) for inst in instances}) == 1
        engine = session.reversibility_service.engine
        calls = []
        real = engine.decrypt
        monkeypatch.setattr(engine, "decrypt", lambda token: calls.append(token) or real(token))
        session.lock_identities(PID, tags_to_lock=TAGS)
        assert len(calls) == 1, len(calls)


# --- The rest of the CHANGELOG's sentences --------------------------------------


def test_redact_does_not_disturb_the_stamp(tmp_path):
    """`redact()` rewrites the instance's UID and pixels; the stamp is
    untouched, and a changed-value re-lock after it is still this
    store's.

    Until #712 this wrote `{rules: [{name, match, zones}]}`, a schema no
    version of the loader has had: `rules` was ignored, no rule loaded,
    and `redact()` ran over nothing, so the stamp "survived" a redaction
    that never happened. The rule is now a `machines:` rule on the
    instance's own serial, with a zone that selects pixels (`[0, 4, 0, 4]`
    is rows 0-4, columns 0-4; `[0, 0, 4, 4]` would select none), and the
    test asserts the redaction ran before it asserts anything survived it.
    """
    with _session(tmp_path, KEEP_BOTH, DeviceSerialNumber="SN607") as session:
        instance = _instance(session)
        session.lock_identities(PID, tags_to_lock=TAGS)
        before = instance._locked_token
        uid_before = instance.sop_instance_uid
        cfg = tmp_path / "redact.yaml"
        cfg.write_text(yaml.safe_dump({"machines": [
            {"serial_number": "SN607", "redaction_zones": [[0, 4, 0, 4]]}]}),
            encoding="utf-8")
        session.load_config(str(cfg))
        assert session.redact() == 1
        instance = _instance(session)
        assert instance.sop_instance_uid != uid_before
        assert instance._locked_token == before
        instance.set_attr("0010,0010", "CHANGED^Value")
        session.lock_identities(PID, tags_to_lock=TAGS)
        assert _held(session, instance) == {"0010,0010": "CHANGED^Value", "0010,0020": PID}


def test_a_wider_relock_over_an_unstamped_token_whose_values_match_is_accepted(tmp_path):
    """Q4, pinned as ruled: only a value the token holds is protected. The
    re-ingested export under `KEEP` on both, re-locked with a tag the
    token never held: accepted, and the new tag's value is whatever the
    file carries -- the residual the CHANGELOG names."""
    session, held = _reingested(tmp_path, KEEP_BOTH, TAGS)
    with session:
        instance = _instance(session)
        pseudonym = session.store.patients[0].patient_id
        session.lock_identities(pseudonym, tags_to_lock=TAGS + [ACCESSION])
        stashed = _held(session, instance)
        assert {tag: stashed[tag] for tag in TAGS} == held
        assert stashed[ACCESSION] == instance.attributes[ACCESSION]
        assert instance.identity_token_is_this_stores(_token(instance))


def test_a_narrower_relock_over_an_unstamped_token_whose_unnamed_value_moved_is_refused(
        tmp_path):
    """The unnamed-tag arm of the #607 check (review of #633, F-2): the
    re-ingested `Project-X` export, re-locked on the ID alone. The name is
    not named, so it would leave the token, and the instance carries the
    pass's `Project-X` where the token holds the original: refused,
    naming the name tag -- one the caller did not name -- in 2(a)'s
    words, "nothing (tags_to_lock does not name it)"; the token and the
    held name are unchanged, and nothing is stamped.

    Kills the arm narrowed to the tags the caller named (the reviewer's
    R2, `if tag not in tags_to_lock: continue`), which the suite left
    green while it accepted this lock and dropped `Orig^Name` from the
    token (measured: held `{'0010,0020': 'P607'}` afterwards)."""
    session, held = _reingested(tmp_path, PROJECT_X, TAGS)
    with session:
        instance = _instance(session)
        token = _token(instance)
        assert instance.attributes["0010,0010"] == "Project-X"
        with pytest.raises(RuntimeError) as caught:
            session.lock_identities(session.store.patients[0].patient_id,
                                    tags_to_lock=["0010,0020"])
        assert str(caught.value) == mismatch_refusal("0010,0010", named=False)
        assert _token(instance) == token
        assert _held(session, instance) == held
        assert instance._locked_token is None


def test_the_stamp_is_the_digest_of_the_token_bytes():
    """`Instance.record_identity_token` / `identity_token_is_this_stores`
    on their own: keyed on the bytes, `str` and `bytearray` spellings
    equal, nothing vouched for before a record, and a different token not
    vouched for after one."""
    inst = Instance("SOP", "1.2.840.10008.5.1.4.1.1.2", 1)
    token = b"gAAAAABtoken-bytes"
    assert not inst.identity_token_is_this_stores(token)
    inst.record_identity_token(token)
    assert inst._locked_token == hashlib.sha256(token).hexdigest()
    assert inst.identity_token_is_this_stores(token)
    assert inst.identity_token_is_this_stores(bytearray(token))
    assert inst.identity_token_is_this_stores(token.decode())
    assert not inst.identity_token_is_this_stores(token + b"x")
    assert not inst.identity_token_is_this_stores(b"")


def test_embed_identity_token_stamps_through_the_service(tmp_path):
    """`ReversibilityService.embed_identity_token` is the one site: a
    token embedded through it is vouched for, and each instance is
    vouched for its own token and no other. `DicomItem` has no slot, and
    the embed on it is not attempted (the service takes an `Instance`).

    The second instance's token is generated from the same attributes as
    the first's and is still a different token -- a Fernet token carries
    a random IV and a timestamp -- so the last assertion reads the stamp
    against bytes, not against the attributes they encrypt. It kills an
    `identity_token_is_this_stores` narrowed to `bool(self._locked_token)`,
    which answers yes for any token once one is recorded."""
    from isocenter.crypto import KeyManager
    manager = KeyManager(str(tmp_path / "k.key"))
    manager.load_or_generate_key()
    service = ReversibilityService(manager)
    inst = Instance("SOP", "1.2.840.10008.5.1.4.1.1.2", 1)
    token = service.generate_identity_token({"0010,0010": NAME})
    service.embed_identity_token(inst, token)
    assert inst.identity_token_is_this_stores(_token(inst))
    other = Instance("SOP2", "1.2.840.10008.5.1.4.1.1.2", 2)
    other_token = service.generate_identity_token({"0010,0010": NAME})
    assert other_token != token
    service.embed_identity_token(other, other_token)
    assert other.identity_token_is_this_stores(_token(other))
    assert not other.identity_token_is_this_stores(token)
