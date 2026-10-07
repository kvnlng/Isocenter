"""A store opened by a relative path keeps its place when the process
changes directory (#722).

`SqliteStore` kept `db_path` as given and derived the sidecar's path from
it, and every connection, the sidecar manager, both lock files and every
pixel loader resolved those strings against the current directory at the
moment of use. After an `os.chdir`, `sqlite3.connect("t.db")` therefore
*created a new, empty store* in the new directory: every door failed with
`OperationalError: no such table`, a pixel read failed its integrity
check, and `t.db`, `t_pixels.bin` and `t_pixels.bin.lock` were left behind
there. `isocenter.persistence` now makes the path absolute when the store
is opened; `isocenter.session`'s `persistence_file` stays what the caller
passed.

Every test runs under conftest's per-test `tmp_path` working directory
and moves with `monkeypatch.chdir`, which is undone at teardown.
"""
import os
import pickle
import shutil

import numpy as np
from pydicom.data import get_testdata_file

from isocenter.persistence import SqliteStore
from isocenter.session import DicomSession


def _folders(tmp_path):
    work, other, src = tmp_path / "work", tmp_path / "other", tmp_path / "in"
    for folder in (work, other, src):
        folder.mkdir()
    shutil.copy(get_testdata_file("CT_small.dcm"), str(src / "a.dcm"))
    return work, other, src


def _instances(session):
    return [i for p in session.store.patients for st in p.studies
            for se in st.series for i in se.instances]


def test_every_door_still_reaches_the_store_after_a_chdir(tmp_path, monkeypatch):
    work, other, src = _folders(tmp_path)
    out = tmp_path / "out"
    monkeypatch.chdir(work)
    session = DicomSession("t.db")
    try:
        assert session.ingest(str(src)).ingested == 1
        session.save(sync=True)
        (inst,) = _instances(session)
        monkeypatch.chdir(other)

        # Red on main at the first save: `sqlite3.OperationalError: no
        # such table: patients`, from a store sqlite had just created here.
        inst.set_attr("0008,103e", "EDITED AFTER THE MOVE")
        session.save(sync=True)
        inst.unload_pixel_data()
        # Red on main: `RuntimeError: Pixel Loader failed ... Integrity
        # Error`, read from the empty sidecar the gate created here.
        assert inst.get_pixel_data().shape == (128, 128)
        inst.set_pixel_data(inst.get_pixel_data() + np.int16(1))
        session.save(sync=True)
        session.audit()
        assert session.export(str(out)).written == 1
    finally:
        session.close()
    # Nothing was created where the process went: no second store, no
    # second sidecar, no lock file.
    assert os.listdir(other) == []
    assert {"t.db", "t_pixels.bin"} <= set(os.listdir(work))

    with DicomSession(str(work / "t.db")) as reopened:
        (stored,) = _instances(reopened)
        assert stored.attributes["0008,103e"] == "EDITED AFTER THE MOVE"


def test_a_pickled_store_carries_absolute_paths(tmp_path, monkeypatch):
    """What a spawned worker is handed: it may start in another directory."""
    work, _other, _src = _folders(tmp_path)
    monkeypatch.chdir(work)
    store = SqliteStore("t.db")
    clone = pickle.loads(pickle.dumps(store))
    try:
        assert clone.db_path == str(work / "t.db")
        assert clone.sidecar_path == str(work / "t_pixels.bin")
        assert store.sidecar.filepath == str(work / "t_pixels.bin")
    finally:
        clone.stop()
        store.stop()


def test_an_in_memory_store_is_not_given_a_path():
    store = SqliteStore(":memory:")
    try:
        assert store.db_path == ":memory:"
    finally:
        store.stop()
    assert not os.path.exists(":memory:")


def test_the_session_keeps_the_path_it_was_given(tmp_path, monkeypatch):
    """`Session.persistence_file` is tier 1 and is the caller's spelling;
    only the store's own paths are made absolute."""
    work, _other, _src = _folders(tmp_path)
    monkeypatch.chdir(work)
    with DicomSession("t.db") as session:
        assert session.persistence_file == "t.db"
        assert session.store_backend.db_path == str(work / "t.db")
