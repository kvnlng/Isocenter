"""`lock_identities` has a closed signature, and the README's call works (#379, Q7).

Until 0.9.4 the signature was `(patient_id, persist=False,
_patient_obj=None, verbose=True, **kwargs)`: a private-named
optimisation argument in the public parameter list, and an open
`**kwargs` that carried two different things to two different places --
`tags_to_lock` was read on the single-patient path, and everything was
forwarded to `lock_identities_batch` on the batch path. Measured before
the change, on 3.12.13:

* `session.lock_identities(report, tags_to_lock=[...])` -- the call
  `README.md` and `docs/quickstart.md` teach -- raised
  `TypeError: DicomSession.lock_identities_batch() got an unexpected
  keyword argument 'tags_to_lock'`, because the batch method took only
  `auto_persist_chunk_size`. The documented pipeline's reversible step
  did not run. Nothing caught it: `tests/test_documented_api_exists.py`
  grades the *method names* in the README's fences, not their keyword
  arguments.
* `session.lock_identities("P1", bogus=1)` was accepted in silence, so a
  misspelled `tags_to_lock` locked the default tags and said nothing.

The freeze (#379) pins parameter names, and a pin on `_patient_obj` and
`**kwargs` would have frozen both defects. The owner chose to strip them
before the tag (Q7(b)). What replaces them: `tags_to_lock` is an
explicit parameter on both methods, `lock_identities_batch` passes it
down per patient, and the patient lookup lives in a private helper that
takes the resolved `Patient`. `auto_persist_chunk_size` is the batch
method's alone -- on the single-patient path it did nothing, and a
parameter that does nothing on a path is a dead argument there (CLAUDE.md,
"one spelling per behaviour").

Its own file: `tests/test_reversibility.py` is listed under two probe
targets and these tests buy no kill signal against either. The report
and finding classes are reached through `isocenter.session`, which
binds both, so this file names no probe target's module.
"""
import inspect
import os

import pytest

from isocenter import session as session_module
from isocenter.session import DicomSession
from isocenter.entities import Instance, Patient, Series, Study
from unittest.mock import MagicMock

PhiFinding = session_module.PhiFinding
PhiReport = session_module.PhiReport

TAGS = ["0010,0010", "0010,0020", "0010,0030"]


@pytest.fixture
def session(tmp_path):
    with DicomSession(str(tmp_path / "lock.db")) as s:
        s.enable_reversible_anonymization(str(tmp_path / "k.key"))
        for pid in ("P1", "P2"):
            patient = Patient(pid, f"Name^{pid}")
            study = Study(f"ST_{pid}", None)
            series = Series(f"SE_{pid}", "CT", 1)
            inst = Instance(f"1.2.3.{pid}", "1.2.840.10008.5.1.4.1.1.2", 1)
            inst.set_attr("0010,0010", f"Name^{pid}")
            inst.set_attr("0010,0020", pid)
            series.instances.append(inst)
            study.series.append(series)
            patient.studies.append(study)
            s.store.patients.append(patient)
        # Rows first: `persist=True` writes through `update_attributes`,
        # which raises for an instance the store has never seen (#641; it
        # updated nothing in silence before, which made the persist test
        # below red and green look the same), so the fixture is saved.
        s.save(sync=True)
        yield s


def _report_for(*pids):
    return PhiReport([
        PhiFinding(entity_uid=f"1.2.3.{pid}", entity_type="Instance",
                   field_name="PatientName", value=f"Name^{pid}",
                   reason="test", tag="0010,0010", patient_id=pid)
        for pid in pids])


def test_the_readme_call_locks_every_patient_in_the_report(session):
    """`lock_identities(report, tags_to_lock=[...])`, as README.md:160 spells it.

    Red before the change with the `TypeError` in the module docstring.
    """
    result = session.lock_identities(_report_for("P1", "P2"), tags_to_lock=TAGS)

    assert len(result) == 2, "one instance per patient should be locked"
    recovered = [session.reversibility_service.recover_original_data(inst)
                 for inst in result]
    assert sorted(r["0010,0020"] for r in recovered) == ["P1", "P2"], (
        f"each patient's instance must carry its own identity token: {recovered}")


def test_tags_to_lock_reaches_each_patient_on_the_batch_path(session):
    """The tags asked for are the tags embedded, patient by patient.

    A batch that accepted `tags_to_lock` and dropped it on the floor
    would pass the test above; this one decrypts the token and looks.
    Killing mutation: `tags_to_lock` not forwarded from the batch loop.
    """
    session.lock_identities(["P1"], tags_to_lock=["0010,0020"])
    inst = session.store.patients[0].studies[0].series[0].instances[0]
    recovered = session.reversibility_service.recover_original_data(inst)

    assert set(recovered) == {"0010,0020"}, recovered


def test_the_private_optimisation_argument_is_gone(session):
    with pytest.raises(TypeError, match="_patient_obj"):
        session.lock_identities("P1", _patient_obj=None)


def test_an_unknown_keyword_is_refused_rather_than_swallowed(session):
    """A misspelled `tags_to_lock` used to lock the defaults in silence."""
    with pytest.raises(TypeError, match="tags_to_lokc"):
        session.lock_identities("P1", tags_to_lokc=TAGS)


def test_chunked_persistence_is_the_batch_methods_alone(session):
    """`auto_persist_chunk_size` does nothing for one patient and is not accepted there."""
    with pytest.raises(TypeError, match="auto_persist_chunk_size"):
        session.lock_identities("P1", auto_persist_chunk_size=1)

    assert session.lock_identities_batch(["P1", "P2"], auto_persist_chunk_size=1) == []


def test_the_old_positional_slot_fails_loudly(session):
    """`lock_identities(pid, False, patient)` filled `_patient_obj` positionally.

    With that parameter gone, the third slot would be `verbose` and a
    `Patient` would be read as a truthy flag in silence. `verbose` and
    `tags_to_lock` are keyword-only so the old call is a `TypeError`
    instead. Killing mutation: the `*` removed from the signature.
    """
    patient = session.store.patients[0]
    with pytest.raises(TypeError, match="positional"):
        session.lock_identities("P1", False, patient)


def _reloaded_token(tmp_path, pid):
    """The identity token as a *fresh* session reads it from the rows."""
    with DicomSession(str(tmp_path / "lock.db")) as again:
        again.enable_reversible_anonymization(str(tmp_path / "k.key"))
        patient = next(p for p in again.store.patients if p.patient_id == pid)
        inst = patient.studies[0].series[0].instances[0]
        try:
            return again.reversibility_service.recover_original_data(inst)
        except Exception:  # pylint: disable=broad-exception-caught
            return None


def test_persist_reaches_the_store_on_the_batch_path(session, tmp_path):
    """`lock_identities(report, persist=True)` writes the rows (#379, Q10).

    The README's form is the batch form, and until 0.9.4 the batch loop
    hardcoded `persist=False`: the documented call with `persist=True`
    returned the locked instances and wrote nothing, in silence -- a
    fresh session on the same file found no token. Read through a second
    session rather than `has_unsaved_changes`, which stays `True` after a
    working persist (`update_attributes` never marks; filed separately).
    Killing mutation: `persist` not forwarded from the batch loop.
    """
    session.lock_identities(_report_for("P1", "P2"), persist=True, tags_to_lock=TAGS)

    for pid in ("P1", "P2"):
        recovered = _reloaded_token(tmp_path, pid)
        assert recovered and recovered["0010,0020"] == pid, (
            f"{pid}: the token never reached the store: {recovered}")


def test_persist_false_on_the_batch_path_writes_nothing(session, tmp_path):
    """The default is still in memory only, as the single-patient path."""
    session.lock_identities(_report_for("P1"), persist=False)

    assert _reloaded_token(tmp_path, "P1") is None


def test_verbose_reaches_each_patient_on_the_batch_path(session, monkeypatch):
    """`verbose` is forwarded too; the loop no longer hardcodes `False`.

    Killing mutation: `verbose` not forwarded from the batch loop.
    """
    fake = MagicMock()
    monkeypatch.setattr(session_module, "get_logger", lambda: fake)

    session.lock_identities(["P1", "P2"], verbose=False)
    assert not [c for c in fake.debug.call_args_list
                if "Preserving identity" in str(c)], "verbose=False still logged"

    session.lock_identities(["P1", "P2"], verbose=True)
    logged = [str(c) for c in fake.debug.call_args_list if "Preserving identity" in str(c)]
    assert len(logged) == 2, logged


def test_the_signatures_are_closed():
    """No `**kwargs`, no underscored name, on either method; the rest keyword-only."""
    for method in (DicomSession.lock_identities, DicomSession.lock_identities_batch):
        params = inspect.signature(method).parameters
        assert not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()), (
            f"{method.__name__} still takes **kwargs")
        assert not any(name.startswith("_") for name in params), (
            f"{method.__name__} exposes a private-named parameter: {list(params)}")

    params = inspect.signature(DicomSession.lock_identities).parameters
    assert [p.kind for p in params.values()][3:] == [inspect.Parameter.KEYWORD_ONLY] * 2, (
        "verbose and tags_to_lock must be keyword-only (see the docstring)")

    params = inspect.signature(DicomSession.lock_identities_batch).parameters
    assert list(params)[4:] == ["persist", "verbose"], list(params)
    assert [p.kind for p in params.values()][4:] == [inspect.Parameter.KEYWORD_ONLY] * 2, (
        "persist and verbose are keyword-only on the batch method (Q10)")
    assert [p.default for p in params.values()][4:] == [False, True], (
        "the batch method's persist/verbose defaults are lock_identities's")


# --- #26: the report arm, and the cwd key, frozen (owner rulings on #789) -----

def _instance_of(session, pid):
    (patient,) = [p for p in session.store.patients if p.patient_id == pid]
    return patient.studies[0].series[0].instances[0]


def test_a_report_locks_the_patients_its_findings_name_and_no_other(session):
    """`lock_identities(report)` is tier 1 (owner ruling Q1 on #789): a
    `PhiReport` selects the patients its findings name. The README test
    above locks every patient in the session, so a report read as "every
    patient" would pass it; this one names P1 alone, and P2 is untouched."""
    result = session.lock_identities(_report_for("P1"), tags_to_lock=TAGS)
    assert [inst.sop_instance_uid for inst in result] == ["1.2.3.P1"]
    assert not _instance_of(session, "P2").has_unsaved_changes, (
        "a patient the report does not name was locked")


def test_findings_may_be_mixed_with_patient_ids(session):
    """The same arm as elements: a finding in a list stands for its
    patient, beside a plain Patient ID."""
    (finding,) = _report_for("P1").findings
    result = session.lock_identities([finding, "P2"], tags_to_lock=TAGS)
    assert sorted(inst.sop_instance_uid for inst in result) == ["1.2.3.P1", "1.2.3.P2"]


def test_a_report_with_no_findings_locks_nobody(session, tmp_path):
    """What the code does with an empty report, pinned as it stands: it
    locks nobody and returns an empty `LockingResult`, and, like any lock,
    creates the key file when none exists (the key is resolved before the
    selection is read)."""
    result = session.lock_identities(PhiReport([]))
    assert isinstance(result, session_module.LockingResult) and list(result) == []
    assert not any(_instance_of(session, pid).has_unsaved_changes for pid in ("P1", "P2"))
    assert (tmp_path / "k.key").exists()


def _write_key(path):
    from cryptography.fernet import Fernet
    path.write_bytes(Fernet.generate_key())


def test_a_key_in_the_working_directory_enables_reversible_anonymization(tmp_path):
    """`Session()` enables reversible anonymization when `isocenter.key`
    exists in the current working directory at construction (owner ruling
    Q2 on #789: frozen, not deleted). The path is resolved then, so a
    later `chdir` does not move it. conftest runs each test in `tmp_path`."""
    _write_key(tmp_path / "isocenter.key")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    with DicomSession(str(tmp_path / "auto.db")) as session:
        assert session.reversibility_service is not None
        assert session.key_manager.key_path == str(tmp_path / "isocenter.key")
        os.chdir(elsewhere)
        try:
            assert session.key_manager.key_path == str(tmp_path / "isocenter.key")
        finally:
            os.chdir(tmp_path)


def test_no_key_in_the_working_directory_leaves_it_off_and_creates_none(tmp_path):
    """The other arm: with no `isocenter.key` in the working directory --
    even one beside the store, in another directory -- nothing is enabled
    and no key is created."""
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    _write_key(store_dir / "isocenter.key")
    with DicomSession(str(store_dir / "auto.db")) as session:
        assert session.reversibility_service is None
        assert session.key_manager is None
    assert not (tmp_path / "isocenter.key").exists()
