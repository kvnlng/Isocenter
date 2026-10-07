"""A file whose linkage key holds more than one value is refused by name (#747).

Ingest links a file by four elements: Patient ID, Study Instance UID,
Series Instance UID and SOP Instance UID. Each takes one value. A value
holding a backslash is read by pydicom as a `MultiValue`, and until #747:

- a multi-valued Patient ID or SOP Instance UID was refused in Python's
  words (`unhashable type: 'MultiValue'`), words that differ between 3.12
  and 3.14t, after the parent had already appended the file's frame to the
  sidecar;
- a multi-valued Study or Series Instance UID was *ingested*, keyed on the
  `str()` of the list, and exported with `['1.2.3', '1.2.4']` as the UID.

All four are now refused in the worker, by `isocenter.io_handlers`, with a
row that names the element and the count and never a value. Every expected
row is a literal: no interpreter's exception message is in it, which is
what makes it the same row on both interpreters.
"""
import os
import sqlite3

import pydicom
import pytest
from pydicom.data import get_testdata_file
from pydicom.multival import MultiValue

from isocenter.entities import is_synthetic_patient_id
from isocenter.session import DicomSession

FIRST, SECOND, THIRD = "1.2.826.747.11", "1.2.826.747.22", "1.2.826.747.33"

KEYS = [
    pytest.param("PatientID", "Patient ID (0010,0020)",
                 ("SECRETA", "SECRETB", "SECRETC"), id="patient-id"),
    pytest.param("SOPInstanceUID", "SOP Instance UID (0008,0018)",
                 (FIRST, SECOND, THIRD), id="sop-instance-uid"),
    pytest.param("StudyInstanceUID", "Study Instance UID (0020,000D)",
                 (FIRST, SECOND, THIRD), id="study-instance-uid"),
    pytest.param("SeriesInstanceUID", "Series Instance UID (0020,000E)",
                 (FIRST, SECOND, THIRD), id="series-instance-uid"),
]


def _write(folder, name, **edits):
    """CT_small with each keyword's element replaced by `value`, unvalidated."""
    ds = pydicom.dcmread(get_testdata_file("CT_small.dcm"))
    for keyword, value in edits.items():
        if value is None:
            del ds[keyword]
            continue
        elem = ds.data_element(keyword)
        ds[elem.tag] = pydicom.DataElement(
            elem.tag, elem.VR, value, validation_mode=pydicom.config.IGNORE)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    ds.save_as(path)
    return path


def _error_rows(db):
    with sqlite3.connect(db) as conn:
        return conn.execute("SELECT entity_uid, details FROM audit_log "
                            "WHERE action_type = 'ERROR'").fetchall()


def _sidecar_bytes(db):
    """The sidecar's size, 0 when the store never created one."""
    sidecar = os.path.splitext(db)[0] + "_pixels.bin"
    return os.path.getsize(sidecar) if os.path.exists(sidecar) else 0


def _instances(session):
    return [i for p in session.store.patients for st in p.studies
            for se in st.series for i in se.instances]


def _sentence(name, count):
    return (f"ValueError: {name} holds {count} values and takes one; the "
            f"file is linked by it, and none is chosen for it.")


@pytest.mark.parametrize("keyword, name, values", KEYS)
def test_a_multi_valued_key_is_refused_with_a_row_naming_the_element_and_the_count(
        tmp_path, keyword, name, values):
    folder = str(tmp_path / "in")
    path = _write(folder, "one.dcm", **{keyword: "\\".join(values[:2])})
    # The premise, read back: pydicom hands ingest a MultiValue of two.
    assert isinstance(pydicom.dcmread(path).get(keyword), MultiValue)
    db = str(tmp_path / "t.db")
    with DicomSession(persistence_file=db) as session:
        summary = session.ingest(folder)
        assert _instances(session) == []
        assert session.store.patients == []
    reason = _sentence(name, 2)
    # Red on main. Patient ID and SOP Instance UID: the reason is `Linkage
    # Failed: TypeError: unhashable type: 'MultiValue'` on 3.12 and other
    # words on 3.14t. Study and Series Instance UID: the file is ingested
    # and there is no row at all.
    assert summary.failures == [(path, reason)]
    assert _error_rows(db) == [(path, f"Ingest failed for {path}: {reason}")]
    (_entity, detail), = _error_rows(db)
    told = detail.replace(path, "")
    assert "MultiValue" not in told
    for value in values[:2]:
        assert value not in told


def test_a_refused_patient_id_leaves_no_frame_in_the_sidecar(tmp_path):
    """The refusal is the worker's, so the parent never appends the frame.

    Red on main at 22273 bytes: the parent's linkage raised after the
    append. The clean file of the test below shows the helper reads a
    sidecar that does hold bytes."""
    folder = str(tmp_path / "in")
    _write(folder, "one.dcm", PatientID="SECRETA\\SECRETB")
    db = str(tmp_path / "t.db")
    with DicomSession(persistence_file=db) as session:
        assert session.ingest(folder).ingested == 0
    assert _sidecar_bytes(db) == 0


@pytest.mark.parametrize("keyword, name, values", KEYS)
def test_the_count_is_the_files_own(tmp_path, keyword, name, values):
    folder = str(tmp_path / "in")
    path = _write(folder, "one.dcm", **{keyword: "\\".join(values)})
    db = str(tmp_path / "t.db")
    with DicomSession(persistence_file=db) as session:
        summary = session.ingest(folder)
    assert summary.failures == [(path, _sentence(name, 3))]


def test_a_patient_id_of_two_blank_values_is_refused_not_read_as_absent(tmp_path):
    """Two blanks are two values. Reading them as "no Patient ID" would put
    the file under #584's synthetic key on the strength of a join."""
    folder = str(tmp_path / "in")
    path = _write(folder, "one.dcm", PatientID=" \\ ")
    assert isinstance(pydicom.dcmread(path).get("PatientID"), MultiValue)
    db = str(tmp_path / "t.db")
    with DicomSession(persistence_file=db) as session:
        summary = session.ingest(folder)
        assert session.store.patients == []
    assert summary.failures == [(path, _sentence("Patient ID (0010,0020)", 2))]


def test_a_single_valued_patient_id_holding_a_space_is_ingested_under_it(tmp_path):
    folder = str(tmp_path / "in")
    _write(folder, "one.dcm", PatientID="A B")
    db = str(tmp_path / "t.db")
    with DicomSession(persistence_file=db) as session:
        summary = session.ingest(folder)
        assert (summary.ingested, summary.failures) == (1, [])
        assert [p.patient_id for p in session.store.patients] == ["A B"]
    assert _error_rows(db) == []


@pytest.mark.parametrize("value", [None, ""], ids=["absent", "empty"])
def test_a_file_with_no_patient_id_still_takes_the_synthetic_key(tmp_path, value):
    folder = str(tmp_path / "in")
    _write(folder, "one.dcm", PatientID=value)
    db = str(tmp_path / "t.db")
    with DicomSession(persistence_file=db) as session:
        summary = session.ingest(folder)
        assert (summary.ingested, summary.failures) == (1, [])
        (patient,) = session.store.patients
        assert is_synthetic_patient_id(patient.patient_id)
    assert _error_rows(db) == []


def test_the_refusal_is_one_files_and_the_file_beside_it_is_ingested(tmp_path):
    folder = str(tmp_path / "in")
    refused = _write(folder, "a-refused.dcm", StudyInstanceUID=f"{FIRST}\\{SECOND}")
    _write(folder, "b-clean.dcm")
    db = str(tmp_path / "t.db")
    with DicomSession(persistence_file=db) as session:
        summary = session.ingest(folder)
        assert summary.ingested == 1
        assert [p for p, _r in summary.failures] == [refused]
        (study,) = [st for p in session.store.patients for st in p.studies]
        # The clean file's own study, and no study keyed on a list's text.
        assert study.study_instance_uid == "1.3.6.1.4.1.5962.1.2.1.20040119072730.12322"
        session.save(sync=True)
    assert len(_error_rows(db)) == 1
    assert _sidecar_bytes(db) > 0
