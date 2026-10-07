"""An instance whose SOP Instance UID is `None` is refused by the save, by
name, before anything is written (#721).

`instances.sop_instance_uid` is `NOT NULL`, and the save bound the field as
it stood. So `save(sync=True)`, both export formats (each begins with a
save) and `compact()` raised `sqlite3.IntegrityError: NOT NULL constraint
failed: instances.sop_instance_uid`: sqlite's class and sqlite's words,
from a tier-1 door, after the save's pixel frames had already been appended
to the sidecar. `isocenter.persistence` now counts such instances before it
takes the sidecar gate and raises `ValueError` with the count;
`isocenter.session`'s doors let it through (owner ruling Q4 A).

The count is all the message can say: an instance with no UID has no name,
and its path does not go in an exception.
"""
import os
import shutil

import numpy as np
import pydicom
import pytest
from pydicom.data import get_testdata_file
from pydicom.uid import generate_uid

from isocenter.persistence import SqliteStore
from isocenter.session import DicomSession

MESSAGE = ("save: 1 instance(s) hold a SOP Instance UID that is not a str (None, "
           "or a value of another type), and the store keys an instance by "
           "that text, so nothing was saved. Give each one a str "
           "(instance.sop_instance_uid = ...) or remove it from its series.")


def _session(tmp_path):
    """Two CTs, ingested and saved. Returns the session, the instance that
    will lose its UID, and the other, which holds unsaved pixels."""
    src = tmp_path / "in"
    src.mkdir()
    shutil.copy(get_testdata_file("CT_small.dcm"), str(src / "a.dcm"))
    ds = pydicom.dcmread(get_testdata_file("CT_small.dcm"))
    ds.SOPInstanceUID = generate_uid()
    ds.SeriesInstanceUID = generate_uid()
    ds.save_as(str(src / "b.dcm"))
    session = DicomSession(str(tmp_path / "s.db"))
    assert session.ingest(str(src)).ingested == 2
    session.save(sync=True)
    victim, other = [i for p in session.store.patients for st in p.studies
                     for se in st.series for i in se.instances]
    # Unsaved pixels on the *other* instance: a save that got as far as
    # its frame prepass appends them, and the sidecar grows.
    other.set_pixel_data(other.get_pixel_data() + np.int16(1))
    return session, victim, other


def _sidecar(tmp_path):
    return os.path.getsize(str(tmp_path / "s_pixels.bin"))


DOORS = [
    pytest.param(lambda s, out: s.save(sync=True), id="save"),
    pytest.param(lambda s, out: s.export(out), id="export-dicom"),
    pytest.param(lambda s, out: s.export(out, format="wfdb"), id="export-wfdb"),
    pytest.param(lambda s, out: s.compact(), id="compact"),
]


@pytest.mark.parametrize("door", DOORS)
def test_every_door_that_saves_refuses_by_count_before_anything_is_written(
        tmp_path, door):
    session, victim, other = _session(tmp_path)
    out = str(tmp_path / "out")
    try:
        before = _sidecar(tmp_path)
        assert before > 0
        victim.sop_instance_uid = None
        # Red on main: `sqlite3.IntegrityError: NOT NULL constraint
        # failed: instances.sop_instance_uid`, with the sidecar grown by
        # the other instance's frame.
        with pytest.raises(ValueError) as refused:
            door(session, out)
        assert str(refused.value) == MESSAGE
        assert not os.path.exists(out)
        assert _sidecar(tmp_path) == before
        assert other.has_unsaved_changes and victim.has_unsaved_changes
    finally:
        session.close()


def test_two_such_instances_are_counted(tmp_path):
    session, victim, other = _session(tmp_path)
    try:
        victim.sop_instance_uid = None
        other.sop_instance_uid = None
        with pytest.raises(ValueError, match=r"^save: 2 instance\(s\) hold a SOP Instance UID that is not a str "):
            session.save(sync=True)
    finally:
        session.close()


def test_the_refusal_ends_when_the_instance_is_given_a_uid(tmp_path):
    session, victim, _other = _session(tmp_path)
    out = str(tmp_path / "out")
    try:
        victim.sop_instance_uid = None
        with pytest.raises(ValueError):
            session.save(sync=True)
        victim.sop_instance_uid = "1.2.826.721.1"
        session.save(sync=True)
        assert not victim.has_unsaved_changes
        assert session.export(out).written == 2
    finally:
        session.close()


def test_a_uid_of_another_type_is_refused_in_the_same_words(tmp_path):
    """The check is "not a `str`", and the message says so: an `int` is a
    UID of a kind, and "holds no SOP Instance UID" was not true of it
    (review of #947). On main this save *returned* and reported the
    instance saved, and the reopened store held no such instance."""
    session, victim, other = _session(tmp_path)
    try:
        before = _sidecar(tmp_path)
        victim.sop_instance_uid = 12345
        with pytest.raises(ValueError) as refused:
            session.save(sync=True)
        assert str(refused.value) == MESSAGE
        assert _sidecar(tmp_path) == before
        assert other.has_unsaved_changes and victim.has_unsaved_changes
    finally:
        session.close()


@pytest.mark.parametrize("compress", [False, True], ids=["native", "j2k"])
def test_an_export_over_a_uid_of_another_type_writes_no_file_and_no_row(
        tmp_path, compress):
    """What the 1.0.0rc14 `**Output:**` line for #721 left out. At
    v1.0.0rc13, with `sop_instance_uid = 12345` on one of two instances,
    `export()` returned: it wrote the other instance's file and an `ERROR`
    row (`Export failed for instance 12345: TypeError: A UID must be
    created from a string`), and the run graded REVIEW_REQUIRED. Now it
    raises before anything is written: no folder, no file for either
    instance, and no row. With the UID put back the same call writes both
    (the control that this session can export at all)."""
    import sqlite3

    session, victim, _other = _session(tmp_path)
    out = str(tmp_path / "out")
    try:
        kept = victim.sop_instance_uid
        victim.sop_instance_uid = 12345
        with pytest.raises(ValueError) as refused:
            session.export(out, use_compression=compress)
        assert str(refused.value) == MESSAGE
        assert not os.path.exists(out)
        session.store_backend.flush_audit_queue()
        with sqlite3.connect(str(tmp_path / "s.db")) as conn:
            assert conn.execute(
                "SELECT action_type, details FROM audit_log").fetchall() == []
        victim.sop_instance_uid = kept
        assert session.export(out, use_compression=compress).written == 2
        assert len([f for _r, _d, fs in os.walk(out) for f in fs
                    if f.endswith(".dcm")]) == 2
    finally:
        session.close()


def test_close_still_returns_over_such_an_instance(tmp_path):
    """`close()` warns about unsaved instances and does not save them, so
    it has nothing to refuse."""
    session, victim, _other = _session(tmp_path)
    victim.sop_instance_uid = None
    session.close()


def test_an_empty_uid_is_not_this_refusal(tmp_path):
    """`""` is a `str` and reaches the write door, where #613's refusal of
    a UID-less file lives; this check is for the value sqlite cannot bind
    to a `NOT NULL` key. Kills the check widened to `not uid`."""
    session, victim, _other = _session(tmp_path)
    try:
        victim.sop_instance_uid = ""
        session.save(sync=True)
        assert not victim.has_unsaved_changes
    finally:
        session.close()


def test_the_store_refuses_before_it_takes_the_sidecar_gate(tmp_path, monkeypatch):
    """Placement, directly: the gate is never asked for. A check below the
    gate would have held it across a refusal that needs nothing from it."""
    session, victim, _other = _session(tmp_path)
    try:
        victim.sop_instance_uid = None

        def asked(*_args, **_kwargs):
            raise AssertionError("the sidecar gate was taken")

        monkeypatch.setattr(SqliteStore, "_hold_sidecar_gate", asked)
        with pytest.raises(ValueError, match="hold a SOP Instance UID that is not a str"):
            session.store_backend.save_all(session.store.patients)
    finally:
        monkeypatch.undo()
        session.close()
