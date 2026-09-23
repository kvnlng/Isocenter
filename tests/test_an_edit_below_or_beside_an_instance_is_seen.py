"""An edit made after a scan costs the PASS until a re-audit (#767, widened).

Two edit paths escaped the owner-field tracking (L12 review, owner ruling
on #767, 2026-09-23):

- **A nested item.** `items[0].set_attr("0010,0010", …)` after the pass
  moved only the item. An item has no status the grade reads and no row
  of its own (it is stored inside its instance's `attributes_json`), so
  the instance kept reading REMEDIATED over a name no scan had read, the
  run graded PASS, and in a reopened store the edit was never written.
- **A Series field.** No PHI status is recorded on a Series, so an edit of
  one moved nothing the grade or the export's attestation reads, although
  the export writes the value into every instance of the series.

Now a nested item knows its container (`DicomItem._parent`), and any
change to it (`mark_modified()`, which every mutator calls) advances the
revision of the instance at the top of the chain as well; a change to a
tracked Series field advances every instance of the series. Condition 8
counts instances as well as owners, so an edit made after a scan grades
REVIEW_REQUIRED until one reads it.

The tool's own writes after a scan must not trip it. The pass stamps what
it writes after writing it; redaction carries the status it found
(#486); the lock's token is the tool's own write and not PHI, so it
carries the status too, on #486's guard: only when the status was current
before the lock wrote (owner ruling Q-W2).
"""
import copy
import pickle
import re

import pydicom
import pytest
from pydicom.data import get_testdata_file
from pydicom.dataset import Dataset
from pydicom.sequence import Sequence

from isocenter import Session
from isocenter.entities import DicomItem, Instance, PhiStatus, Series

from support.ct_small_files import study_uid, write_ct

EDITED = "edited after the last PHI scan"
SERIAL = "SN-767W"


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _line(n, patients=0, studies=0, series=0, instances=0):
    noun = "entity" if n == 1 else "entities"
    return (f"{n} {noun} {EDITED}: its content was changed after its PHI "
            "status was recorded, and no scan has read the change; "
            f"`audit()` reads it (patients {patients}, studies {studies}, "
            f"series {series}, instances {instances})")


# ---------------------------------------------------------------------------
# The graph
# ---------------------------------------------------------------------------

def _tree():
    """An instance holding an item holding an item, the instance and the
    middle item stamped REMEDIATED and persisted."""
    inst = Instance(sop_instance_uid="1.2.3.4.5")
    middle, leaf = DicomItem(), DicomItem()
    leaf.set_attr("0010,0010", "Anon")
    middle.add_sequence_item("0008,1140", leaf)
    inst.add_sequence_item("0008,1115", middle)
    for entity in (middle, inst):
        entity.record_phi_status(PhiStatus.REMEDIATED)
        entity.mark_persisted()
    return inst, middle, leaf


NESTED_EDITS = {
    "set_attr": lambda leaf: leaf.set_attr("0010,0010", "Doe^Nested"),
    "add_sequence": lambda leaf: leaf.add_sequence("0040,a730"),
    "add_sequence_item": lambda leaf: leaf.add_sequence_item("0040,a730", DicomItem()),
    "clear_sequence_items": None,  # needs an item to clear; built below
    "mark_modified": lambda leaf: leaf.mark_modified(),
}


@pytest.mark.parametrize("op", sorted(NESTED_EDITS))
def test_a_nested_edit_marks_its_instance(op):
    """Every mutator of a nested item reaches the instance at the top of
    the chain: its status stops describing it, and the save is told there
    is something to write. The middle item's own status (#564) is left as
    it was -- only the root moves. Kills the propagation dropped, and a
    walk that stops one level short."""
    inst, middle, leaf = _tree()
    if op == "clear_sequence_items":
        leaf.add_sequence_item("0040,a730", DicomItem())
        inst.record_phi_status(PhiStatus.REMEDIATED)
        inst.mark_persisted()
        before = inst._revision
        assert leaf.clear_sequence_items("0040,a730")
    else:
        before = inst._revision
        NESTED_EDITS[op](leaf)
    assert inst._revision > before
    assert inst.phi_status is PhiStatus.UNSCANNED
    assert inst.has_unsaved_changes
    assert middle.phi_status is PhiStatus.REMEDIATED


def test_an_item_status_stamp_does_not_move_its_instance():
    """A status recorded on a nested item is not a change to its content:
    remediation stamps the item, then the instance, and the instance's
    stamp must still describe it."""
    inst, _middle, leaf = _tree()
    before = inst._revision
    leaf.record_phi_status(PhiStatus.REMEDIATED)
    assert inst._revision == before
    assert inst.phi_status is PhiStatus.REMEDIATED


def test_a_detached_item_moves_only_itself():
    item = DicomItem()
    item.set_attr("0010,0010", "Doe")
    assert item._parent is None and item._revision == 2


def test_a_series_edit_marks_each_of_its_instances():
    """The export writes a Series field into every instance of the series,
    and a Series holds no status of its own: the instances are what go
    stale. An equal value changes nothing."""
    series = Series("1.2.3.4", "CT", 1)
    insts = [Instance(sop_instance_uid=f"1.2.3.4.{k}") for k in range(2)]
    series.instances.extend(insts)
    for inst in insts:
        inst.record_phi_status(PhiStatus.REMEDIATED)
        inst.mark_persisted()
    series.series_number = 1
    assert [i.phi_status for i in insts] == [PhiStatus.REMEDIATED] * 2
    series.series_instance_uid = "1.2.3.9"
    assert [i.phi_status for i in insts] == [PhiStatus.UNSCANNED] * 2
    assert all(i.has_unsaved_changes for i in insts)


def test_repr_and_equality_of_a_nested_graph_terminate():
    """`_parent` is `repr=False, compare=False`: otherwise a dataclass
    `__repr__` recurses item -> instance -> sequences -> item."""
    inst, middle, leaf = _tree()
    assert "_parent" not in repr(leaf)
    assert repr(inst) and repr(middle)
    assert inst == inst and middle == middle and leaf == leaf
    assert leaf != DicomItem()


@pytest.mark.parametrize("clone", [lambda i: pickle.loads(pickle.dumps(i)),
                                   copy.deepcopy], ids=["pickle", "deepcopy"])
def test_a_copied_instance_links_its_own_items(clone):
    """A whole instance copied (the worker hand-off, a deepcopy) links its
    copied items to the copy, never back to the original."""
    inst, _middle, _leaf = _tree()
    other = clone(inst)
    [middle] = other.sequences["0008,1115"].items
    [leaf] = middle.sequences["0008,1140"].items
    assert middle._parent is other and leaf._parent is middle
    assert other.phi_status is PhiStatus.REMEDIATED
    before = inst._revision
    leaf.set_attr("0010,0010", "Doe")
    assert other.phi_status is PhiStatus.UNSCANNED
    assert inst._revision == before


# ---------------------------------------------------------------------------
# Every nested item is linked to its container, on every path
# ---------------------------------------------------------------------------

def _write_nested_ct(path, patient_id, suffix, name="Alpha^One"):
    write_ct(path, patient_id, suffix, name=name)
    ds = pydicom.dcmread(str(path))
    item = Dataset()
    item.ReferencedSOPClassUID = ds.SOPClassUID
    item.ReferencedSOPInstanceUID = "1.2.3.4"
    inner = Dataset()
    inner.CodeValue = "113100"
    item.PurposeOfReferenceCodeSequence = Sequence([inner])
    ds.ReferencedImageSequence = Sequence([item])
    ds.DeviceSerialNumber = SERIAL
    ds.save_as(str(path))


def _instances(session):
    return [i for p in session.store.patients for st in p.studies
            for se in st.series for i in se.instances]


def _assert_linked(container, where):
    for sequence in container.sequences.values():
        for item in sequence.items:
            assert item._parent is container, (where, sequence.tag)
            _assert_linked(item, where)


def _assert_all_linked(session, where):
    insts = _instances(session)
    assert insts, where
    for inst in insts:
        _assert_linked(inst, where)
    return insts


def test_every_nested_item_is_linked_to_its_container(tmp_path):
    """Instead of a detector of attach sites: the whole pipeline is run
    and every nested item is checked against the container holding it --
    ingest (a CT with a two-level sequence, and an ECG whose waveform
    sequences are nested), a save, a reopen (hydration), the reversible
    lock (its token item), a pass, a redaction (the Derivation Code Sequence), and a
    `clone_sequences` worker copy taken after the reopen."""
    src = tmp_path / "in"
    _write_nested_ct(src / "ct.dcm", "PID-W", "7671")
    ecg = pydicom.dcmread(get_testdata_file("waveform_ecg.dcm"))
    ecg.save_as(str(src / "ecg.dcm"))
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(src))
        _assert_all_linked(session, "ingest")
        session.save(sync=True)
    with Session(str(tmp_path / "s.db")) as session:
        _assert_all_linked(session, "reopen")
        for patient in session.store.patients:
            clone = session._make_lightweight_copy(patient)
            for st in clone.studies:
                for se in st.series:
                    for inst in se.instances:
                        _assert_linked(inst, "worker copy")
        session.enable_reversible_anonymization(str(tmp_path / "k.key"))
        report = session.audit()
        for patient in list(session.store.patients):
            session.lock_identities(patient.patient_id, verbose=False)
        insts = _assert_all_linked(session, "lock")
        assert any("0400,0500" in i.sequences for i in insts)
        session.anonymize(report)
        _assert_all_linked(session, "pass")
        session.configuration.add_rule(SERIAL, zones=[[0, 4, 0, 4]])
        assert session.redact(show_progress=False) == 1
        insts = _assert_all_linked(session, "redact")
        assert any("0008,9215" in i.sequences for i in insts)
        session.save(sync=True)
    with Session(str(tmp_path / "s.db")) as session:
        _assert_all_linked(session, "second reopen")


# ---------------------------------------------------------------------------
# The grade
# ---------------------------------------------------------------------------

def _session(tmp_path, suffix="7672"):
    _write_nested_ct(tmp_path / "in" / "a.dcm", "PID-W", suffix)
    session = Session(str(tmp_path / "s.db"))
    session.ingest(str(tmp_path / "in"))
    return session


def _graded(session, tmp_path, name="r.md"):
    session.export(str(tmp_path / f"out-{name}"), use_compression=False)
    session.generate_report(str(tmp_path / name))
    text = (tmp_path / name).read_text(encoding="utf-8")
    section_5 = text.split("## 5. Validation & Verification", 1)[1]
    basis = section_5.split("*   **Metadata Remediation:**", 1)[0]
    reasons = ([] if "**Grade Basis:** PASS" in basis
               else re.findall(r"^    \*   (.*)$", basis, flags=re.M))
    [written] = list((tmp_path / f"out-{name}").rglob("*.dcm"))
    return reasons, ("**PASS**" in text), pydicom.dcmread(str(written))


def _edited(reasons):
    return [r for r in reasons if EDITED in r]


def _only(session):
    [inst] = _instances(session)
    return inst


def test_a_nested_item_edited_after_the_pass_is_not_pass(tmp_path):
    """L12's probe P3. The pass writes; a nested item's Patient's Name is
    then set by hand. The file carries it inside the sequence, and the run
    graded PASS with the instance reading REMEDIATED. Now the instance
    reads UNSCANNED and condition 8 names it; a re-audit reads the name
    and a pass removes it, and the run grades PASS again."""
    with _session(tmp_path) as session:
        session.anonymize(session.audit())
        inst = _only(session)
        assert inst.phi_status is PhiStatus.REMEDIATED
        inst.sequences["0008,1140"].items[0].set_attr("0010,0010", "Doe^Nested")
        assert inst.phi_status is PhiStatus.UNSCANNED
        reasons, passed, ds = _graded(session, tmp_path)
        assert not passed
        assert _edited(reasons) == [_line(1, instances=1)], reasons
        assert str(ds.ReferencedImageSequence[0].PatientName) == "Doe^Nested"
        session.anonymize(session.audit())
        reasons, passed, ds = _graded(session, tmp_path, "r2.md")
    assert passed and reasons == [], reasons
    assert "PatientName" not in ds.ReferencedImageSequence[0] or \
        str(ds.ReferencedImageSequence[0].PatientName) != "Doe^Nested"


def test_a_series_uid_set_back_after_the_pass_is_not_pass(tmp_path):
    """L12's probe P1. The pass replaces the Series Instance UID (#544);
    it is then set back to the source by plain assignment. The export
    writes it into every instance, and the run graded PASS. Now the Series
    and each of its instances read UNSCANNED and condition 8 names both."""
    source = study_uid("7672") + ".1"
    with _session(tmp_path) as session:
        session.anonymize(session.audit())
        [series] = session.store.patients[0].studies[0].series
        assert series.series_instance_uid != source
        series.series_instance_uid = source
        reasons, passed, ds = _graded(session, tmp_path)
    assert not passed
    assert _edited(reasons) == [_line(2, series=1, instances=1)], reasons
    assert ds.SeriesInstanceUID == source


def test_the_pass_writing_a_series_uid_is_not_an_edit(tmp_path):
    """Q-W1. A pass handed the Series finding after the instance findings
    writes the Series UID onto the Series and records each instance's
    status after writing its copy (#544: IDENTIFIED, until its own
    findings are handed again). A cascade from the Series write, landing
    after that record, made it stale -- condition 8 tripped by the pass's
    own write (`entities.PASS_WRITING`). The instance findings handed again
    grade PASS; a user's edit after the pass is outside it and still
    cascades. Kills the flag dropped, and the flag left set after the
    pass."""
    source = study_uid("7672") + ".1"
    with _session(tmp_path) as session:
        report = session.audit()
        rest = [f for f in report.findings if f.entity_type != "Series"]
        session.anonymize(rest)
        session.anonymize([f for f in report.findings if f.entity_type == "Series"])
        [series] = session.store.patients[0].studies[0].series
        [inst] = series.instances
        assert series.series_instance_uid != source
        assert inst.phi_status is PhiStatus.IDENTIFIED
        session.anonymize(rest)
        assert inst.phi_status is PhiStatus.REMEDIATED
        reasons, passed, _ds = _graded(session, tmp_path)
        assert passed and reasons == [], reasons
        series.series_number = 767
        reasons, passed, _ds = _graded(session, tmp_path, "r2.md")
    assert not passed
    assert _edited(reasons) == [_line(2, series=1, instances=1)], reasons


def test_an_instance_attribute_edited_after_the_pass_is_not_pass(tmp_path):
    """The plainest edit: `instance.set_attr` after the pass. It read
    UNSCANNED before this change too, and UNSCANNED does not grade (Q3);
    a status recorded and then left behind now does."""
    with _session(tmp_path) as session:
        session.anonymize(session.audit())
        _only(session).set_attr("0008,1030", "Alpha^One's study")
        reasons, passed, ds = _graded(session, tmp_path)
    assert not passed
    assert _edited(reasons) == [_line(1, instances=1)], reasons
    assert ds.StudyDescription == "Alpha^One's study"


def test_the_ordinary_path_stays_pass(tmp_path):
    """Nothing the pipeline writes itself counts: ingest, audit, a pass,
    a redaction and a save between each."""
    with _session(tmp_path) as session:
        session.save(sync=True)
        session.anonymize(session.audit())
        session.save(sync=True)
        session.configuration.add_rule(SERIAL, zones=[[0, 4, 0, 4]])
        assert session.redact(show_progress=False) == 1
        session.save(sync=True)
        reasons, passed, _ds = _graded(session, tmp_path)
    assert passed and reasons == [], reasons


def test_a_nested_edit_in_a_reopened_store_reaches_the_store(tmp_path):
    """Items have no row of their own; they are written inside their
    instance's, which the save writes only when the instance holds unsaved
    changes. A nested edit left it clean, and the edit was lost at the
    next reopen."""
    with _session(tmp_path) as session:
        session.save(sync=True)
    with Session(str(tmp_path / "s.db")) as session:
        _only(session).sequences["0008,1140"].items[0].set_attr(
            "0008,1150", "1.2.3.4.767")
        session.save(sync=True)
    with Session(str(tmp_path / "s.db")) as session:
        item = _only(session).sequences["0008,1140"].items[0]
        assert item.attributes["0008,1150"] == "1.2.3.4.767"


# ---------------------------------------------------------------------------
# The lock carries the status it found (owner ruling Q-W2)
# ---------------------------------------------------------------------------

def _keep_everything(session, tmp_path):
    path = tmp_path / "none.yaml"
    path.write_text(
        "privacy_profile: none\nremove_private_tags: false\nphi_tags:\n"
        "  '0010,0010': {action: KEEP}\n  '0010,0020': {action: KEEP}\n"
        "  '0008,0020': {action: KEEP}\n", encoding="utf-8")
    session.load_config(str(path))


def test_the_lock_carries_the_status_it_found(tmp_path):
    """Under a policy that finds nothing, the audit records every instance
    CLEARED and the pass writes nothing, so nothing re-stamps what the lock
    writes. The token is the tool's own write and not PHI: the instance
    keeps CLEARED, and the documented reversible path grades PASS rather
    than a false alarm. Kills the carry dropped."""
    with _session(tmp_path) as session:
        _keep_everything(session, tmp_path)
        session.enable_reversible_anonymization(str(tmp_path / "k.key"))
        report = session.audit()
        inst = _only(session)
        assert inst.phi_status is PhiStatus.CLEARED
        session.lock_identities("PID-W", verbose=False)
        assert "0400,0500" in inst.sequences
        assert inst.phi_status is PhiStatus.CLEARED
        session.anonymize(report)
        reasons, passed, _ds = _graded(session, tmp_path)
    assert passed and reasons == [], reasons


def test_the_lock_does_not_launder_an_edit_made_before_it(tmp_path):
    """#486's guard: the lock carries a status only if it was current when
    the lock wrote. An edit between the audit and the lock left the status
    stale, and the lock must not hand it back. Kills the carry made
    unconditional (the status read raw rather than through the revision)."""
    with _session(tmp_path) as session:
        _keep_everything(session, tmp_path)
        session.enable_reversible_anonymization(str(tmp_path / "k.key"))
        report = session.audit()
        inst = _only(session)
        inst.set_attr("0008,1030", "Alpha^One's study")
        session.lock_identities("PID-W", verbose=False)
        assert inst.phi_status is PhiStatus.UNSCANNED
        session.anonymize(report)
        reasons, passed, _ds = _graded(session, tmp_path)
    assert not passed
    assert _edited(reasons) == [_line(1, instances=1)], reasons


def test_the_documented_reversible_path_stays_pass(tmp_path):
    """audit -> enable + lock -> anonymize -> export, under the floor."""
    with _session(tmp_path) as session:
        session.enable_reversible_anonymization(str(tmp_path / "k.key"))
        report = session.audit()
        session.lock_identities("PID-W", verbose=False)
        session.anonymize(report)
        reasons, passed, _ds = _graded(session, tmp_path)
    assert passed and reasons == [], reasons
