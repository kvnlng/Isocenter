"""What compaction guarantees when it does not finish.

`compact_sidecar` rewrites the pixel sidecar and then rewrites every
offset in the database to match. Those two are the same fact stored in
two places, and the window between them is the whole risk: a database
describing a layout the file does not hold produces silent garbage, not
an error, because every read lands at a plausible-looking wrong offset.

The success path already has tests. These cover what happens when the
database write fails after the file has been swapped -- the case whose
ordering the code goes to some trouble to get right, and which nothing
exercised.
"""
import os

import numpy as np
import pytest

from isocenter.entities import Instance
from isocenter.session import DicomSession


@pytest.fixture
def session(tmp_path, request):
    """A session with a real file-based store, as the sidecar requires."""
    sess = DicomSession(
        persistence_file=str(tmp_path / f"compact_{request.node.name}.db"))
    yield sess
    sess.close()


def instance_with_pixels(uid, fill):
    inst = Instance(uid, "1.2.840.10008.5.1.4.1.1.2", 1, file_path=None)
    inst.set_pixel_data(np.full((100, 1000), fill, dtype=np.uint8))
    return inst


def populate(session, count=3):
    """Writes `count` frames to the sidecar and returns their instances."""
    from isocenter.entities import Patient, Study, Series

    patient = Patient("P1", "Test^Patient")
    study = Study("S1", "20230101")
    series = Series("SE1", "CT", 1)

    instances = []
    for i in range(count):
        inst = instance_with_pixels(f"1.1.{i}", i + 1)
        session.store_backend.persist_pixel_data(inst)
        series.instances.append(inst)
        instances.append(inst)

    study.series.append(series)
    patient.studies.append(study)
    session.store.patients.append(patient)
    session.save(sync=True)
    return instances


def test_a_failed_database_update_leaves_the_original_sidecar_in_place(
        session, monkeypatch):
    """File and database must never describe different generations.

    The file is swapped first and the database updated second, so a
    failure here has to put the original file back. If it did not, every
    offset in the database would point into a file that no longer has
    that layout -- and each read would return whatever bytes happen to
    live there rather than failing.
    """
    instances = populate(session)
    sidecar_path = session.store_backend.sidecar_path
    original = open(sidecar_path, "rb").read()
    stored_offsets = [i._pixel_loader.offset for i in instances]

    real_connection = session.store_backend._get_connection
    calls = {"n": 0}

    def fail_on_the_update(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] > 1:          # the query pass first, then the update
            raise RuntimeError("database went away mid-compaction")
        return real_connection(*args, **kwargs)

    monkeypatch.setattr(session.store_backend, "_get_connection",
                        fail_on_the_update)

    with pytest.raises(RuntimeError, match="database went away"):
        session.store_backend.compact_sidecar()

    assert open(sidecar_path, "rb").read() == original, (
        "the compacted file was left in place while the database still "
        "describes the old layout")

    # The offsets the database holds must still resolve to the same bytes.
    with open(sidecar_path, "rb") as handle:
        for inst, offset in zip(instances, stored_offsets):
            handle.seek(offset)
            assert handle.read(inst._pixel_loader.length)


def test_a_failed_compaction_leaves_no_working_files_behind(
        session, monkeypatch):
    """The backup is the only remaining copy while the swap is half done.

    It must be removed once the original is back, and never before --
    but it must not be left lying next to the sidecar either, where the
    next run would find a stale copy of pre-compaction pixel data.
    """
    populate(session)
    sidecar_path = session.store_backend.sidecar_path

    real_connection = session.store_backend._get_connection
    calls = {"n": 0}

    def fail_on_the_update(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("database went away mid-compaction")
        return real_connection(*args, **kwargs)

    monkeypatch.setattr(session.store_backend, "_get_connection",
                        fail_on_the_update)

    with pytest.raises(RuntimeError):
        session.store_backend.compact_sidecar()

    assert os.path.exists(sidecar_path)
    assert not os.path.exists(sidecar_path + ".compact.tmp")
    assert not os.path.exists(sidecar_path + ".compact.bak")


def test_compacting_an_empty_sidecar_does_nothing(session):
    """Nothing live means nothing to rewrite, and no file churn."""
    result = session.store_backend.compact_sidecar()

    assert result == {}
    assert not os.path.exists(
        session.store_backend.sidecar_path + ".compact.tmp")
