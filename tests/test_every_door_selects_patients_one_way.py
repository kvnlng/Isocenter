"""Every method that takes a patient selection reads it one way (#696).

#678 gave the two export doors one reading of `patient_ids`; the report
and query doors kept their own, so one argument selected two different
cohorts. Measured at 042aa01f, identically on 3.12 and 3.14t, over two
patients `COH-A` and `COH-B`: `get_cohort_report(patient_ids="COH-ACOH-B")`
reported both patients (a substring match) where `export()` wrote none; a
generator yielding the second patient gave the report no rows;
`get_flattened_instances` split a `str` into characters and raised
`sqlite3.ProgrammingError` for a generator; `lock_identities_batch("COH-A")`
iterated the characters; `lock_identities(frozenset(...))` was read as one
id; and on every door an element that is not a `str` selected nobody in
silence.

The rule, from `io_handlers.normalize_id_filter`: `None` is every patient
(never on the lock pair), an empty iterable is nobody, an iterator is read
once, and a bare `str`, a bytes-like value, a non-iterable or a non-`str`
element is a `TypeError` naming the option -- never a value -- before
anything is read, flushed or written.

`_DOORS` is the divergence detector: every test is parametrised over the
doors it applies to, so a door that stops calling the helper goes red in
its own cell. The unmatched-id count (#686) is in
`test_an_unmatched_patient_id_is_counted.py`.
"""
import logging
import os

import pandas as pd
import pytest

from isocenter.exporters.wfdb import record_name_for
from isocenter.session import DicomSession

A, B = "COH-A", "COH-B"


def _session(tmp_path, name, *extra):
    """Two patients (plus `extra` Patient IDs), one waveform instance each,
    saved, so the store door sees them too. Waveform-bearing so both export
    formats write every patient."""
    from scripts.generate_waveform_test_data import write_fixture

    source = tmp_path / f"src_{name}"
    source.mkdir()
    for index, pid in enumerate((A, B) + extra):
        write_fixture(str(source / f"{index}.dcm"), num_samples=64,
                      patient_id=pid, patient_name=f"Name^{index}")
    session = DicomSession(persistence_file=str(tmp_path / f"{name}.db"))
    session.ingest(str(source))
    session.save(sync=True)
    assert len(session.store.patients) == 2 + len(extra), (
        "the fixture did not ingest every patient; the assertions below "
        "would pass vacuously")
    return session


def _instances(session):
    return [(patient, study, series, instance)
            for patient in session.store.patients
            for study in patient.studies
            for series in study.series
            for instance in series.instances]


def _owner_by_uid(session):
    return {inst.sop_instance_uid: p.patient_id
            for p, _st, _se, inst in _instances(session)}


# --- the doors -------------------------------------------------------------
#
# Each takes `(session, tmp_path, selection)` and returns the set of Patient
# IDs it selected, read off what the door itself returns or wrote.

def _cohort_report(session, tmp_path, selection):
    return set(session.get_cohort_report(patient_ids=selection)["PatientID"])


def _export_dataframe(session, tmp_path, selection):
    # In a directory of its own that does not exist yet, so a refusal that
    # came after `makedirs` is visible as the directory.
    path = tmp_path / "csv" / "frame.csv"
    frame = session.export_dataframe(str(path), patient_ids=selection)
    on_disk = set(pd.read_csv(path, keep_default_na=False)["PatientID"]
                  .astype(str))
    returned = set(frame["PatientID"])
    assert on_disk == returned, (on_disk, returned)
    return returned


def _flattened(session, tmp_path, selection):
    return {row["patient_id"] for row in
            session.store_backend.get_flattened_instances(patient_ids=selection)}


def _export_dicom(session, tmp_path, selection):
    owner = _owner_by_uid(session)
    summary = session.export(str(tmp_path / "dicom"), patient_ids=selection,
                             show_progress=False)
    return {owner[uid] for uid in summary.written_uids}


def _export_wfdb(session, tmp_path, selection):
    expected = {record_name_for(p, st, se, inst) + ".hea": p.patient_id
                for p, st, se, inst in _instances(session)}
    written = session.export(str(tmp_path / "wfdb"), format="wfdb",
                             patient_ids=selection)
    return {expected[os.path.basename(path)] for path in written}


def _locked_owners(session, result):
    owner = {id(inst): p.patient_id for p, _st, _se, inst in _instances(session)}
    return {owner[id(inst)] for inst in result}


def _lock_batch(session, tmp_path, selection):
    session.enable_reversible_anonymization(str(tmp_path / "k.key"))
    return _locked_owners(session, session.lock_identities_batch(selection))


def _lock(session, tmp_path, selection):
    session.enable_reversible_anonymization(str(tmp_path / "k.key"))
    return _locked_owners(session, session.lock_identities(selection))


def _in_own_dir(door):
    """Every door writes (a CSV, an export, a key) under its own directory."""
    def call(session, tmp_path, selection):
        tmp_path.mkdir(parents=True, exist_ok=True)
        return door(session, tmp_path, selection)
    return call


_DOORS = {name: _in_own_dir(door) for name, door in {
    "cohort_report": _cohort_report,
    "export_dataframe": _export_dataframe,
    "flattened": _flattened,
    "export_dicom": _export_dicom,
    "export_wfdb": _export_wfdb,
    "lock_batch": _lock_batch,
    "lock": _lock,
}.items()}
_ALL = sorted(_DOORS)
_NOT_LOCK = [d for d in _ALL if d not in ("lock", "lock_batch")]
_NOT_SINGLE_LOCK = [d for d in _ALL if d != "lock"]


def _assert_nothing_written(session, tmp_path):
    """No file under either export folder, no CSV, no key file, no token."""
    for folder in ("dicom", "wfdb"):
        root = tmp_path / folder
        found = [os.path.join(d, f) for d, _s, fs in os.walk(root) for f in fs]
        assert not found, f"a refused call wrote {found}"
    assert not (tmp_path / "csv").exists(), (
        "a refused export_dataframe created its directory")
    assert not (tmp_path / "k.key").exists(), (
        "a refused lock created a key file")
    service = session.reversibility_service
    if service is not None:
        tokens = [inst.sop_instance_uid for _p, _st, _se, inst in _instances(session)
                  if service.token_of_ours(inst) is not None]
        assert not tokens, f"a refused lock left tokens on {tokens}"


def _names_the_option(door, message):
    """A refusal opens with the argument as the call spelled it:
    `lock_identities(patient_id)`, `patient_ids` everywhere else."""
    option = "patient_id" if door == "lock" else "patient_ids"
    return message.startswith(option + " ")


def _refused(door, session, tmp_path, selection):
    with pytest.raises(TypeError) as caught:
        _DOORS[door](session, tmp_path, selection)
    _assert_nothing_written(session, tmp_path)
    return str(caught.value)


# --- S1, S2: an iterator and a frozenset are selections -----------------------

@pytest.mark.parametrize("door", _ALL)
def test_a_generator_selects_the_patients_it_names(tmp_path, door):
    """A generator yielding the *second* patient in store order selects it,
    and an iterator of both selects both. Red at 042aa01f on
    `cohort_report` and `export_dataframe` (the first membership test ate
    the generator: nothing), `flattened` (`ProgrammingError`: the
    placeholders consumed it) and `lock` (a generator was read as one id)."""
    with _session(tmp_path, door) as session:
        second = _DOORS[door](session, tmp_path / "g1", (x for x in [B]))
    with _session(tmp_path, door + "_2") as session:
        both = _DOORS[door](session, tmp_path / "g2", iter([A, B]))
    assert second == {B}, f"{door}: a generator yielding {B!r} selected {second}"
    assert both == {A, B}, f"{door}: an iterator of both selected {both}"


@pytest.mark.parametrize("door", _ALL)
def test_a_frozenset_is_a_selection(tmp_path, door):
    """`frozenset({A})` selects A. Red at 042aa01f on `lock`, whose dispatch
    tested `(list, tuple, set)` and read a frozenset as one Patient ID."""
    with _session(tmp_path, door) as session:
        selected = _DOORS[door](session, tmp_path / "f", frozenset({A}))
    assert selected == {A}, f"{door}: frozenset({{{A!r}}}) selected {selected}"


# --- S3: a bare str is refused ---------------------------------------------------

@pytest.mark.parametrize("value", [A, A + B], ids=["one_id", "two_ids_joined"])
@pytest.mark.parametrize("door", _NOT_SINGLE_LOCK)
def test_a_bare_str_is_refused(tmp_path, door, value):
    """A bare `str` raises `TypeError` naming the option, before anything is
    written, and without quoting the value (owner ruling Q2 on #686).
    Measured at 042aa01f: the export doors read it as one id with a
    warning, the report doors substring-matched (`"COH-ACOH-B"` reported
    both patients), the store split it into characters, and the batch lock
    iterated the characters. `lock` is excluded: `lock_identities("COH-A")`
    is the single-id spelling (`test_lock_identities_still_takes_one_str`)."""
    (tmp_path / "s").mkdir()
    with _session(tmp_path, door) as session:
        message = _refused(door, session, tmp_path / "s", value)
    assert _names_the_option(door, message) and "bare str" in message, message
    assert A not in message and B not in message, message


def test_lock_identities_still_takes_one_str(tmp_path):
    """The single-id spelling is unchanged: a `str` is one Patient ID, not a
    selection to refuse. Kills a dispatch that sends a `str` to the batch."""
    with _session(tmp_path, "one") as session:
        assert _lock(session, tmp_path, A) == {A}


# --- S4, S5, S6: bytes-like, non-str elements, non-iterables ---------------------

@pytest.mark.parametrize("value", [b"COH-A", bytearray(b"COH-A"),
                                   memoryview(b"COH-A")],
                         ids=["bytes", "bytearray", "memoryview"])
@pytest.mark.parametrize("door", _ALL)
def test_a_bytes_like_selection_is_refused(tmp_path, door, value):
    """Every bytes-like selection raises `TypeError` naming the option and
    the type. Green at 042aa01f on the two export doors (#678); on the
    others `bytes` raised Python's own message or selected nobody, and a
    `memoryview` selected nobody in silence everywhere but the exports."""
    label = type(value).__name__
    (tmp_path / "s").mkdir()
    with _session(tmp_path, door) as session:
        message = _refused(door, session, tmp_path / "s", value)
    assert _names_the_option(door, message) and label in message, message


@pytest.mark.parametrize("element", [b"COH-A", 42, None, ("COH-A",)],
                         ids=["bytes", "int", "None", "tuple"])
@pytest.mark.parametrize("door", _ALL)
def test_a_non_str_element_is_refused(tmp_path, door, element):
    """`[A, <element>]` raises `TypeError` naming the option, `position 2`
    (1-based) and the element's type, and not the value. Red at 042aa01f on
    every door: the element matched nobody and A was selected in silence."""
    (tmp_path / "s").mkdir()
    with _session(tmp_path, door) as session:
        message = _refused(door, session, tmp_path / "s", [A, element])
    assert _names_the_option(door, message), message
    # The lock pair also takes findings, and its refusal says so.
    admits = "a str or a finding" if door.startswith("lock") else "a str:"
    assert f"which is not {admits}" in message, message
    assert "position 2" in message, message
    assert type(element).__name__ in message, message
    assert A not in message and "COH" not in message, message


@pytest.mark.parametrize("door", _ALL)
def test_a_non_iterable_is_refused_in_our_words(tmp_path, door):
    """`patient_ids=42` raises our `TypeError`, which names the option.
    Python's own (`'int' object is not iterable`, worded differently on
    3.14) does not; `lock` logged an error and returned nothing."""
    (tmp_path / "s").mkdir()
    with _session(tmp_path, door) as session:
        message = _refused(door, session, tmp_path / "s", 42)
    assert _names_the_option(door, message) and "int" in message, message


def test_an_iterators_own_typeerror_is_not_reworded():
    """`iter()` alone is inside the shape check's `try`: a `TypeError` the
    caller's own iterator raises part-way surfaces as itself, not as a
    refusal of the argument's shape that hides the caller's bug."""
    from isocenter.io_handlers import normalize_id_filter  # pylint: disable=import-outside-toplevel

    def ids():
        yield A
        raise TypeError("the iterator's own")

    with pytest.raises(TypeError, match="the iterator's own"):
        normalize_id_filter(ids(), "patient_ids")


# --- S7: None and empty ----------------------------------------------------------

@pytest.mark.parametrize("door", _NOT_LOCK)
def test_only_none_is_everyone_and_empty_is_nobody(tmp_path, door, caplog):
    """`None` selects both; every empty iterable selects nobody and logs
    nothing about `patient_ids`. A guard for #678 and #142: green at
    042aa01f, red for a helper that returns an empty selection for `None`
    or `None` for an empty iterable."""
    with _session(tmp_path, door) as session:
        with caplog.at_level(logging.WARNING, logger="isocenter"):
            everyone = _DOORS[door](session, tmp_path / "none", None)
            empties = {}
            for label, value in [("list", []), ("tuple", ()), ("set", set()),
                                 ("frozenset", frozenset()),
                                 ("iter", iter([]))]:
                (tmp_path / label).mkdir()
                empties[label] = _DOORS[door](session, tmp_path / label, value)
    assert everyone == {A, B}, f"{door}: None selected {everyone}"
    for label, selected in empties.items():
        assert selected == set(), f"{door}: an empty {label} selected {selected}"
    noise = [r.getMessage() for r in caplog.records
             if "patient_ids" in r.getMessage()]
    assert not noise, noise


# --- S8: an empty Patient ID, and an ID-less subject's key -------------------------

@pytest.mark.parametrize("door", _NOT_SINGLE_LOCK)
def test_an_id_less_subject_is_selected_by_its_key(tmp_path, door):
    """A subject ingested with an empty Patient ID is selected by the key
    the graph holds for it -- `get_cohort_report`'s `PatientID` -- on every
    door, and the key is a `str`, so the element check admits it. Written
    against the graph's key rather than `""`, because since #584 ingest keys
    such a subject on its study, not on `""`."""
    with _session(tmp_path, door, "") as session:
        key = next(p.patient_id for p in session.store.patients
                   if p.patient_id not in (A, B))
        (tmp_path / "s").mkdir()
        selected = _DOORS[door](session, tmp_path / "s", [key])
    assert selected == {key}, f"{door}: [<the key>] selected {selected}"


def test_an_empty_patient_id_is_a_patient(tmp_path):
    """`""` is a `str` and a Patient ID (#581): `patient_ids=[""]` selects a
    patient whose ID is `""` and is not refused as a falsy element."""
    with _session(tmp_path, "empty") as session:
        session.store.patients[0].patient_id = ""
        assert _cohort_report(session, tmp_path, [""]) == {""}


# --- S9, S10: where the refusal comes from ---------------------------------------

def test_the_refusal_comes_before_the_flush(tmp_path, monkeypatch):
    """`export(patient_ids=42)` raises before `save(sync=True)`: a refusal
    after the flush has moved the session for an export that will not
    happen. `42`, not a `str`, so it holds whichever reading a str has."""
    with _session(tmp_path, "flush") as session:
        calls = []
        monkeypatch.setattr(session, "save", lambda *a, **k: calls.append(k))
        with pytest.raises(TypeError):
            session.export(str(tmp_path / "out"), patient_ids=42,
                           show_progress=False)
    assert calls == [], "the export flushed before it refused its argument"


def test_the_store_door_refuses_at_the_call(tmp_path):
    """`get_flattened_instances` refuses when it is called, not on the first
    `next()`: it is a plain method wrapping a generator for exactly this."""
    with _session(tmp_path, "call") as session:
        with pytest.raises(TypeError):
            session.store_backend.get_flattened_instances(patient_ids=42)
        with pytest.raises(TypeError):
            session.store_backend.get_flattened_instances(instance_uids="1.2.3")


def test_instance_uids_is_read_the_same_way(tmp_path):
    """The store's other filter goes through the same helper, and its
    refusals say "SOP Instance UID". Red at 042aa01f: a generator raised
    `ProgrammingError`, a `str` was split into characters."""
    with _session(tmp_path, "uids") as session:
        store = session.store_backend
        uid_a = next(inst.sop_instance_uid for p, _st, _se, inst in _instances(session)
                     if p.patient_id == A)
        rows = list(store.get_flattened_instances(
            instance_uids=(u for u in [uid_a])))
        with pytest.raises(TypeError) as bare:
            store.get_flattened_instances(instance_uids=uid_a)
        with pytest.raises(TypeError) as element:
            store.get_flattened_instances(instance_uids=[uid_a, 7])
    assert [row["patient_id"] for row in rows] == [A]
    assert "instance_uids" in str(bare.value), str(bare.value)
    assert "SOP Instance UID" in str(bare.value), str(bare.value)
    assert uid_a not in str(bare.value), str(bare.value)
    assert "position 2" in str(element.value), str(element.value)
    assert "int" in str(element.value), str(element.value)


# --- S11, S12, S13: the lock pair ---------------------------------------------------

@pytest.mark.parametrize("value", [A, b"COH-A", None],
                         ids=["str", "bytes", "None"])
def test_a_refused_batch_lock_creates_no_key(tmp_path, value):
    """The batch's selection is read before its key: a refused argument
    creates no key file. Red at 042aa01f, where each reached
    `_key_for_locking()` first (and `None` raised Python's own message)."""
    key = tmp_path / "k.key"
    with _session(tmp_path, "key") as session:
        session.enable_reversible_anonymization(str(key))
        with pytest.raises(TypeError) as caught:
            session.lock_identities_batch(value)
    assert not key.exists(), "a refused batch lock created the key file"
    assert "patient_ids" in str(caught.value), str(caught.value)


@pytest.mark.parametrize("door", ["lock", "lock_batch"])
def test_none_locks_nobody(tmp_path, door):
    """`None` is not "every patient" on the lock pair: there is no spelling
    for locking everyone. Red at 042aa01f (`lock`: an ERROR and nothing
    locked; `lock_batch`: Python's `TypeError`, naming no option). Kills
    `allow_none=False` dropped, which would lock the whole session."""
    (tmp_path / "s").mkdir()
    with _session(tmp_path, door) as session:
        message = _refused(door, session, tmp_path / "s", None)
    assert _names_the_option(door, message) and "None" in message, message


def test_one_finding_is_not_a_patient_id(tmp_path):
    """`lock_identities(<one PhiFinding>)` goes to the batch, which refuses
    it by its type (a finding is not iterable), before any key exists.
    Measured at 042aa01f: looked up as one Patient ID, an ERROR, nothing
    locked -- and `anonymize()` then removed the identity."""
    key = tmp_path / "k.key"
    with _session(tmp_path, "finding") as session:
        finding = session.audit().findings[0]
        session.enable_reversible_anonymization(str(key))
        with pytest.raises(TypeError) as caught:
            session.lock_identities(finding)
    assert _names_the_option("lock", str(caught.value)), str(caught.value)
    assert "PhiFinding" in str(caught.value), str(caught.value)
    assert not key.exists()


def test_the_lock_count_stays_over_distinct_ids(tmp_path, caplog):
    """The lock's unmatched count is over distinct ids, as its `[n of m]`
    numbering is over distinct patients; the export's count is positional.
    A guard: `["NOPE", "NOPE", A]` locks A and counts one."""
    with _session(tmp_path, "distinct") as session:
        with caplog.at_level(logging.ERROR, logger="isocenter"):
            locked = _lock_batch(session, tmp_path, ["NOPE", "NOPE", A])
    assert locked == {A}
    errors = [r.getMessage() for r in caplog.records
              if r.levelno == logging.ERROR]
    assert any("1 Patient ID given matched no patient" in m for m in errors), errors


def test_a_mixed_list_of_findings_and_ids_still_locks(tmp_path):
    """A list mixing `str` and findings is the batch's documented input,
    and the element check admits both."""
    with _session(tmp_path, "mixed") as session:
        finding_b = next(f for f in session.audit().findings
                         if f.patient_id == B)
        assert _lock_batch(session, tmp_path, [A, finding_b]) == {A, B}
