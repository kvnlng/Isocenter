"""`export(format="wfdb")` saves the session before it writes, as a DICOM export does (#809).

`docs/quickstart.md` promises "`export()` saves the session itself
before it writes". `_export_dicom` does (`save(sync=True)` after its
refusals, before the walk); the WFDB exporter did not. Measured on main
before the fix: ingest -> `anonymize()` -> `export(format="wfdb")` ->
`close()` left the one waveform instance dirty, `close()` warned, and the
reopened store's Patient ID was the source value. The same run with
`format="dicom"` reopened on the pseudonym.

The pair below is the owner's ruling (Q5-A) in two halves:

* the save happens -- no instance is dirty after the export, `close()`
  says nothing, and the reopened store holds the pseudonym read before
  the close (compared whole, not by its `ANON_` prefix, which the
  source could never carry but a different pseudonym would);
* it happens *after the refusals*: a call refused for an unknown option
  or a bare-`str` `patient_ids` leaves the instance unsaved, so a
  refused export has not moved the store.

A save placed above the option check passes the first half and fails
the second; a deleted save fails the first.
"""
import pytest

from isocenter.session import DicomSession
from scripts.generate_waveform_test_data import write_fixture

SOURCE_ID = "WFSAVE-809"
CLOSE_WARNING = "holding unsaved changes"


def _ingested_and_anonymized(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    write_fixture(str(src / "ecg.dcm"), num_samples=64,
                  patient_id=SOURCE_ID, patient_name="Wave^Form")
    db = str(tmp_path / "wfdb_saves.db")
    session = DicomSession(persistence_file=db)
    session.ingest(str(src))
    session.anonymize()
    return session, db


def _instances(session):
    return [inst for p in session.store.patients for st in p.studies
            for se in st.series for inst in se.instances]


def test_a_wfdb_export_leaves_the_store_holding_the_anonymized_graph(
        tmp_path, capsys):
    session, db = _ingested_and_anonymized(tmp_path)
    try:
        pseudonym = session.store.patients[0].patient_id
        assert pseudonym != SOURCE_ID, (
            "precondition: anonymize() must have replaced the Patient ID, "
            "or a reopen reading the source proves nothing")
        assert any(i.has_unsaved_changes for i in _instances(session)), (
            "precondition: anonymize() must leave the instance unsaved, or "
            "the export's save is not what this test measures")

        records = session.export(str(tmp_path / "out"), format="wfdb")
        assert len(records) == 1, records

        assert [i.sop_instance_uid for i in _instances(session)
                if i.has_unsaved_changes] == []
        capsys.readouterr()
    finally:
        session.close()
    assert CLOSE_WARNING not in capsys.readouterr().out

    reopened = DicomSession(persistence_file=db)
    try:
        assert [p.patient_id for p in reopened.store.patients] == [pseudonym]
    finally:
        reopened.close()


@pytest.mark.parametrize("bad_call", [
    pytest.param({"patient_id": ["X"]}, id="unknown-option"),
    pytest.param({"patient_ids": SOURCE_ID}, id="bare-str-patient-ids"),
])
def test_a_refused_wfdb_export_saves_nothing(tmp_path, bad_call):
    session, _ = _ingested_and_anonymized(tmp_path)
    try:
        with pytest.raises(TypeError):
            session.export(str(tmp_path / "out"), format="wfdb", **bad_call)
        assert all(i.has_unsaved_changes for i in _instances(session)), (
            "a refused export saved the session: the refusal must come "
            "before the save")
    finally:
        session.close()
