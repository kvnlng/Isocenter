"""Recovery reads the token a patient carries, and a restored Study Date
that is not a date leaves the `Study` alone (#616, #619).

**#616.** `recover_patient_identity()` walked the patient's studies with a
`break` that left the series loop only, so it ended on the first instance
of the **last** study with instances. A patient whose token sits on an
earlier study -- a pair sharing a Patient ID merged by `audit()` (#563),
or a study ingested after the lock -- raised `no encrypted identity token
on this patient's instances; ...` although it was locked and recoverable
(measured on 57400d1, 347ee93 and be0752e, `probes-I/p_dup_lock.py
single`). Recovery now reads the first instance that carries a token of
ours (`ReversibilityService.token_of_ours`), and falls back to the first
instance at all, so a patient with no token anywhere still gets that
message and a patient with no instances still gets its own.

"A token of ours", not "an Encrypted Attributes Sequence": since #617 a
foreign `(0400,0500)` -- one holding no Fernet token -- is "no token", so
a walk that stopped at the first sequence of any kind would stop on a
foreign item in study 1 and never reach the token in study 3.

**#619.** Ingest maps a blank or unreadable Study Date to `None`, but the
#566 restore compared and wrote the token's raw string, so a study
ingested as `None` came back holding `''` or `'20041399'` -- dirty,
persisted, exported, and in the unreadable case raised again by the next
`audit()` (measured on be0752e, `probes-I/p566.py`). The restored value is
now read through `entities.normalize_study_date`, the parser the `Study`
setter and hydration share, and a value that is not a date leaves the
`Study` untouched with one WARNING carrying no date and no identifier.

**Why this file imports what it does.** `isocenter.session` is named, so
its probe row is charged.
"""
import logging
from datetime import date

import numpy as np
import pydicom
import pytest
from cryptography.fernet import Fernet

from isocenter.entities import (JITTER_SCHEME_KEYED, JITTER_SCHEME_UNKEYED,
                                DicomItem, Instance, Patient, Series, Study)
from isocenter.session import DicomSession

from support.ct_small_files import study_uid, write_ct

PID, NAME = "PAT-616", "Secret^Sixteen"
SEQ, CONTENT, SYNTAX = "0400,0500", "0400,0510", "0400,0520"
TAGS = ["0010,0010", "0010,0020"]
NO_TOKEN = ("no encrypted identity token on this patient's instances; was it "
            "locked with lock_identities() before anonymize()?")
UNREADABLE = ("The restored Study Date could not be read as a date, so the "
              "Study keeps the date it holds (#619).")


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


# ---------------------------------------------------------------------------
# #616
# ---------------------------------------------------------------------------

def _instance(uid):
    inst = Instance(uid, "1.2.840.10008.5.1.4.1.1.2", 1)
    inst.file_path = None
    inst.set_attr("0010,0010", NAME)
    inst.set_attr("0010,0020", PID)
    inst.set_pixel_data(np.zeros((8, 8), dtype=np.uint16))
    return inst


def _patient(session, layout, pid=PID, prefix=None):
    """A hand-built patient, one `Study` per character of `layout`:
    `i` a study with one instance, `s` a study whose one series holds no
    instance, `e` a study with no series at all. UIDs are spelled from
    `prefix` (the Patient ID by default). Returns the patient and its
    instances, in graph order."""
    patient = Patient(pid, NAME)
    prefix = prefix or pid
    instances = []
    for n, kind in enumerate(layout, start=1):
        study = Study(f"ST_{prefix}_{n}", date(2023, 1, n))
        if kind != "e":
            series = Series(f"SE_{prefix}_{n}", "CT", n)
            if kind == "i":
                inst = _instance(f"SOP_{prefix}_{n}")
                series.instances.append(inst)
                instances.append(inst)
            study.series.append(series)
        patient.studies.append(study)
    session.store.patients.append(patient)
    return patient, instances


def _anonymize_by_hand(instances, patient):
    """What a pass leaves on the identifiers, without running one: the
    restore has something to put back."""
    for inst in instances:
        inst.set_attr("0010,0010", "ANONYMIZED")
        inst.set_attr("0010,0020", "ANON_616")
    patient.patient_name, patient.patient_id = "ANONYMIZED", "ANON_616"


def _keep_tokens_only_on(instances, keep):
    for n, inst in enumerate(instances):
        if n not in keep:
            del inst.sequences[SEQ]


def _foreign_item():
    item = DicomItem()
    item.set_attr(CONTENT, b"NOT-OUR-TOKEN-FROM-A-SOURCE-FILE")
    item.set_attr(SYNTAX, "1.2.840.10008.1.2")
    return item


#: (layout, indexes into the patient's instances that keep the token).
#: Before the fix the walk read the first instance of the last study that
#: has one; M25c, the lock's own walk with its outer `break`, reads the
#: first instance of all. Which each layout fails:
#:
#: - `first_study_only_across_a_series_without_instances` ("isi", token
#:   on study 1): the old walk (reads study 3).
#: - `second_study_only_after_a_study_without_series` ("eii", study 2):
#:   the old walk (reads study 3).
#: - `first_study_only_before_a_series_without_instances` ("iis", study 1):
#:   the old walk (reads study 2; study 3's series is empty).
#: - `last_study_only` ("iei", study 3): M25c (reads study 1).
#: - `middle_study_only` ("iii", study 2): both.
LAYOUTS = {
    "first_study_only_across_a_series_without_instances": ("isi", {0}),
    "second_study_only_after_a_study_without_series": ("eii", {0}),
    "first_study_only_before_a_series_without_instances": ("iis", {0}),
    "last_study_only": ("iei", {1}),
    "middle_study_only": ("iii", {1}),
}


@pytest.mark.parametrize("layout,keep", LAYOUTS.values(), ids=LAYOUTS.keys())
def test_a_patient_whose_token_is_not_on_the_last_study_still_recovers(
        tmp_path, layout, keep):
    """T23. `restore=False` answers without raising, and `restore=True`
    puts the held name and ID back on every instance and the patient.
    Kills M25 (the inner `break` only, the walk before the fix) and M25c
    (the first instance only), each on the layouts `LAYOUTS` names."""
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.enable_reversible_anonymization(str(tmp_path / "k.key"))
        patient, instances = _patient(session, layout)
        session.lock_identities(PID, tags_to_lock=TAGS)
        _keep_tokens_only_on(instances, keep)
        _anonymize_by_hand(instances, patient)

        assert session.recover_patient_identity("ANON_616", restore=False) is None
        assert patient.patient_id == "ANON_616", "restore=False wrote something"

        session.recover_patient_identity("ANON_616", restore=True)
        assert (patient.patient_name, patient.patient_id) == (NAME, PID)
        assert [(i.attributes["0010,0010"], i.attributes["0010,0020"])
                for i in instances] == [(NAME, PID)] * len(instances)


def test_a_foreign_sequence_on_the_first_study_does_not_stop_the_walk(tmp_path):
    """The walk looks for a token **of ours**. Study 1 carries a foreign
    Encrypted Attributes Sequence (a source file's; no Fernet token) and
    study 3 the lock's token. Kills M25b: the walk stopping on
    `_token_item`, any sequence item, which reads study 1's foreign blob
    and raises "no token" for a recoverable patient."""
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.enable_reversible_anonymization(str(tmp_path / "k.key"))
        patient, instances = _patient(session, "isi")
        session.lock_identities(PID, tags_to_lock=TAGS)
        instances[0].sequences[SEQ].items[:] = [_foreign_item()]
        _anonymize_by_hand(instances, patient)

        session.recover_patient_identity("ANON_616", restore=True)
        assert (patient.patient_name, patient.patient_id) == (NAME, PID)


def test_a_pair_merged_by_audit_after_a_lock_recovers(tmp_path, caplog):
    """#616 as filed (`probes-I/p_dup_lock.py single`): two ingested files
    made to share a Patient ID in user code, the first object locked
    before `audit()`, which merges the pair (#563) with the token on study
    1 only; then `anonymize()`. Recovery answered "no token" before the
    fix. It now recovers, and `restore=True` writes the token's values
    onto every instance of the patient, the second study's too, which
    carries none: the token is one study's (#583). The two files hold
    different Accession Numbers, so the second study's taking the first's
    is the cross-study write itself, not a value either restore would
    produce (review of #640, P-3), and the #640 WARNING says so. Kills R1
    (restore only the instances that carry the token)."""
    write_ct(tmp_path / "in" / "a.dcm", "PAT-A", "6161", name=NAME, accession=ACC_ONE)
    write_ct(tmp_path / "in" / "b.dcm", "PAT-B", "6162", name=NAME, accession=ACC_TWO)
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        first, second = sorted(session.store.patients, key=lambda p: p.patient_id)
        second.patient_id = first.patient_id
        for inst in second.studies[0].series[0].instances:
            inst.set_attr("0010,0020", first.patient_id)
        session.enable_reversible_anonymization(str(tmp_path / "k.key"))
        session.lock_identities("PAT-A", tags_to_lock=TAGS + ["0008,0050"])
        report = session.audit()
        session.anonymize(report)
        [patient] = session.store.patients
        carrying = [SEQ in st.series[0].instances[0].sequences for st in patient.studies]
        assert carrying == [True, False]
        assert [st.study_instance_uid for st in patient.studies] == [
            study_uid("6161"), study_uid("6162")]
        pseudonym = patient.patient_id

        assert session.recover_patient_identity(pseudonym, restore=False) is None
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="isocenter"):
            session.recover_patient_identity(pseudonym, restore=True)
        assert patient.patient_id == "PAT-A"
        assert [(st.series[0].instances[0].attributes["0010,0020"],
                 st.series[0].instances[0].attributes["0008,0050"])
                for st in patient.studies] == [("PAT-A", ACC_ONE), ("PAT-A", ACC_ONE)]
    assert _elsewhere_warnings(caplog) == [elsewhere(1, 2)], caplog.text


# ---------------------------------------------------------------------------
# #616, review of #640 F-1: a restore onto instances that do not carry the
# token read says so
# ---------------------------------------------------------------------------

ACC_ONE, ACC_TWO = "ACC-ONE", "ACC-TWO"


def elsewhere(count, total):
    """The WARNING a restore logs when `count` of the `total` instances it
    wrote do not carry the token it read."""
    return (f"The identity token read holds one study's values, and they were "
            f"restored onto {count} of {total} instances that do not carry that "
            "token, so study-level identifiers written onto them, such as "
            "Accession Number, may be another study's (#583).")


def _elsewhere_warnings(caplog):
    return [r.getMessage() for r in caplog.records
            if r.name == "isocenter" and r.levelno == logging.WARNING
            and "identity token read" in r.getMessage()]


def _restored_accessions(db, key, pseudonym, caplog):
    """Reopen, restore, and return {Study Instance UID: the Accession
    Number its instance holds}, with the restore's WARNINGs in `caplog`."""
    with DicomSession(db) as session:
        session.enable_reversible_anonymization(key)
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="isocenter"):
            session.recover_patient_identity(pseudonym, restore=True)
        [patient] = session.store.patients
        assert patient.patient_id == "PAT-A"
        return {st.study_instance_uid: st.series[0].instances[0].attributes["0008,0050"]
                for st in patient.studies}


def _value_free(warning, pseudonym):
    for secret in ("PAT-A", "PAT-B", NAME, pseudonym, ACC_ONE, ACC_TWO,
                   "6171", "6172", "6173", "6174"):
        assert secret not in warning, warning


def test_a_restore_onto_a_study_ingested_after_the_lock_warns(tmp_path, caplog):
    """The shape I2 turned from a refusal into a restore (review of #640,
    F-1, `after_ingest`): study 1 locked under the default `tags_to_lock`,
    which include Accession Number, study 2 ingested after the lock. The
    restore writes study 1's `ACC-ONE` into study 2's instance, whose own
    was `ACC-TWO`. That write is #583's until per-instance tokens land;
    what this pins is that it is no longer silent: one WARNING with the
    count, and no value in it. Before the WARNING, nothing on any channel
    said so, and #566's Study Date WARNING cannot fire under the default
    tags. Kills the WARNING removed and the count miscounted."""
    write_ct(tmp_path / "one" / "a.dcm", "PAT-A", "6171", name=NAME, accession=ACC_ONE)
    write_ct(tmp_path / "two" / "b.dcm", "PAT-A", "6172", name=NAME, accession=ACC_TWO)
    db, key = str(tmp_path / "s.db"), str(tmp_path / "k.key")
    with DicomSession(db) as session:
        session.enable_reversible_anonymization(key)
        session.ingest(str(tmp_path / "one"))
        session.lock_identities("PAT-A", persist=True)
        session.ingest(str(tmp_path / "two"))
        report = session.audit()
        session.anonymize(report)
        session.save(sync=True)
        [patient] = session.store.patients
        assert {st.study_instance_uid: SEQ in st.series[0].instances[0].sequences
                for st in patient.studies} == {study_uid("6171"): True,
                                               study_uid("6172"): False}
        pseudonym = patient.patient_id

    assert _restored_accessions(db, key, pseudonym, caplog) == {
        study_uid("6171"): ACC_ONE, study_uid("6172"): ACC_ONE}
    warnings = _elsewhere_warnings(caplog)
    assert warnings == [elsewhere(1, 2)], caplog.text
    _value_free(warnings[0], pseudonym)


def test_a_restore_onto_a_study_carrying_another_token_warns(tmp_path, caplog):
    """`two_tokens` (review of #640, F-1): the pair's two objects locked
    separately before `audit()` merges them, so each study carries a token
    of ours and the two differ. The first token found is the one read, so
    study 2 takes study 1's `ACC-ONE` over the `ACC-TWO` its own token
    holds. It carries *a* token, not the one read, and the WARNING counts
    it. Kills the count taken as "carries no token" (`is None`), which
    reads study 2 as carrying one and stays silent."""
    write_ct(tmp_path / "in" / "a.dcm", "PAT-A", "6173", name=NAME, accession=ACC_ONE)
    write_ct(tmp_path / "in" / "b.dcm", "PAT-B", "6174", name=NAME, accession=ACC_TWO)
    db, key = str(tmp_path / "s.db"), str(tmp_path / "k.key")
    with DicomSession(db) as session:
        session.enable_reversible_anonymization(key)
        session.ingest(str(tmp_path / "in"))
        first, second = sorted(session.store.patients, key=lambda p: p.patient_id)
        second.patient_id = first.patient_id
        for inst in second.studies[0].series[0].instances:
            inst.set_attr("0010,0020", first.patient_id)
        session.lock_identities("PAT-A")
        session.lock_identities(["PAT-A"])
        tokens = {p.studies[0].study_instance_uid: session.reversibility_service.token_of_ours(
            p.studies[0].series[0].instances[0]) for p in (first, second)}
        assert None not in tokens.values() and len(set(tokens.values())) == 2
        report = session.audit()
        session.anonymize(report)
        session.save(sync=True)
        [patient] = session.store.patients
        assert [st.study_instance_uid for st in patient.studies] == [
            study_uid("6173"), study_uid("6174")]
        pseudonym = patient.patient_id

    assert _restored_accessions(db, key, pseudonym, caplog) == {
        study_uid("6173"): ACC_ONE, study_uid("6174"): ACC_ONE}
    warnings = _elsewhere_warnings(caplog)
    assert warnings == [elsewhere(1, 2)], caplog.text
    _value_free(warnings[0], pseudonym)


def test_a_restore_onto_instances_that_all_carry_the_token_read_does_not_warn(
        tmp_path, caplog):
    """The control: every instance carries the token read, so there is
    nothing to count and no WARNING. Kills the WARNING logged whatever the
    count."""
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.enable_reversible_anonymization(str(tmp_path / "k.key"))
        patient, instances = _patient(session, "iii")
        session.lock_identities(PID, tags_to_lock=TAGS)
        _anonymize_by_hand(instances, patient)
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="isocenter"):
            session.recover_patient_identity("ANON_616", restore=True)
        assert (patient.patient_name, patient.patient_id) == (NAME, PID)
    assert _elsewhere_warnings(caplog) == [], caplog.text


@pytest.mark.parametrize("layout", ["i", "isi", "eie"],
                         ids=["one_study", "three_studies",
                              "one_instance_between_empty_studies"])
def test_a_patient_with_no_token_anywhere_still_says_so(tmp_path, layout):
    """T24. The *no token* message survives the new walk, and a patient
    with no instances keeps its own. Kills M26 (a walk that finds nothing
    falling through to "no instances", or to a silent `None`)."""
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.enable_reversible_anonymization(str(tmp_path / "k.key"))
        _patient(session, "i", pid="PAT-OTHER")
        session.lock_identities("PAT-OTHER", tags_to_lock=TAGS)  # creates the key
        _patient(session, layout)
        with pytest.raises(RuntimeError) as raised:
            session.recover_patient_identity(PID, restore=False)
        assert str(raised.value) == NO_TOKEN

        _patient(session, "se", pid="PAT-HOLLOW")
        with pytest.raises(RuntimeError) as hollow:
            session.recover_patient_identity("PAT-HOLLOW", restore=False)
        assert str(hollow.value) == ("recover_patient_identity: the patient has "
                                     "no instances to recover an identity from")


def test_restore_false_under_a_key_that_does_not_open_the_token_says_so(tmp_path):
    """The walk reaches study 1's token and reads it under a second, valid
    key: the wrong-key text, not "no token". Before the fix the walk ended
    on study 3, which carries none, and a caller holding the wrong key was
    told the patient had never been locked. Kills a walk that treats a
    token it cannot open as absent and walks on."""
    real, other = str(tmp_path / "real.key"), tmp_path / "other.key"
    other.write_bytes(Fernet.generate_key())
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.enable_reversible_anonymization(real)
        patient, instances = _patient(session, "isi")
        session.lock_identities(PID, tags_to_lock=TAGS)
        _keep_tokens_only_on(instances, {0})
        _anonymize_by_hand(instances, patient)

        session.enable_reversible_anonymization(str(other))
        with pytest.raises(RuntimeError) as raised:
            session.recover_patient_identity("ANON_616", restore=False)
        assert str(raised.value) == (
            f"the key at {other} does not decrypt this patient's identity token; "
            "recovery needs the key the identity was locked with")
        assert raised.value.__cause__ is None and raised.value.__suppress_context__


def test_the_first_token_found_is_the_one_read(tmp_path):
    """The first token found is the one read, whatever else the patient
    carries (a merged pair locked as two objects carries two; review of
    #640, P-6). Study 1 carries a token of ours under a key this session does not
    hold, study 3 the lock's own: recovery raises the wrong-key text and
    writes nothing, rather than walking on to a token it can open and
    answering for the patient with it. Kills M26b (the walk's `break`
    dropped, which reads the last token)."""
    key = str(tmp_path / "k.key")
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.enable_reversible_anonymization(key)
        patient, instances = _patient(session, "isi")
        session.lock_identities(PID, tags_to_lock=TAGS)
        elsewhere = Fernet(Fernet.generate_key()).encrypt(b'{"0010,0010": "Other^Name"}')
        instances[0].sequences[SEQ].items[0].set_attr(CONTENT, elsewhere)
        _anonymize_by_hand(instances, patient)

        with pytest.raises(RuntimeError) as raised:
            session.recover_patient_identity("ANON_616", restore=True)
        assert str(raised.value) == (
            f"the key at {key} does not decrypt this patient's identity token; "
            "recovery needs the key the identity was locked with")
        assert patient.patient_id == "ANON_616"
        assert all(i.attributes["0010,0010"] == "ANONYMIZED" for i in instances)


def test_a_restore_refused_across_jitter_schemes_writes_nothing(tmp_path, monkeypatch):
    """The #548 refusal is still asked before anything is written, now that
    a patient whose token is on study 1 reaches it: a raw patient already
    holds the ID the token would restore, under a different date-offset
    scheme. Before the fix this raised "no token" and so never reached the
    scheme check; a fix that read the token and wrote before asking would
    leave the instances holding the originals beside the refusal."""
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.enable_reversible_anonymization(str(tmp_path / "k.key"))
        patient, instances = _patient(session, "isi")
        session.lock_identities(PID, tags_to_lock=TAGS)
        _keep_tokens_only_on(instances, {0})
        _anonymize_by_hand(instances, patient)
        raw, _ = _patient(session, "i", pid=PID, prefix="RAW")
        patient._jitter_scheme = JITTER_SCHEME_UNKEYED
        raw._jitter_scheme = JITTER_SCHEME_KEYED
        before = [(dict(i.attributes), i._revision) for i in instances]
        revision = patient._revision
        drained = []
        monkeypatch.setattr(session.persistence_manager, "flush",
                            lambda: drained.append(1))

        with pytest.raises(RuntimeError) as raised:
            session.recover_patient_identity("ANON_616", restore=True)
        assert str(raised.value) == (
            "2 patients in this session share a Patient ID but were "
            "de-identified under different date-offset schemes; merging "
            "them would give their dates two offsets")
        assert [(dict(i.attributes), i._revision) for i in instances] == before
        assert (patient.patient_id, patient._revision) == ("ANON_616", revision)
        assert len(session.store.patients) == 2 and drained == []


# ---------------------------------------------------------------------------
# #619
# ---------------------------------------------------------------------------

STUDY_TAGS = ["0010,0010", "0010,0020", "0008,0020"]
SID = "PAT-619"


def _locked_and_anonymized(tmp_path, dates, blank_instance=False):
    """One patient, one study per source date, locked with Study Date,
    anonymized and saved -- `test_a_restored_study_date_reaches_the_file`'s
    shape. `blank_instance` blanks the first instance's `0008,0020` by hand
    before the lock, so the token holds `''` while the `Study` holds a
    date. Returns (db, key, pseudonym, {study uid: Study.study_date})."""
    for n, day in enumerate(dates, start=1):
        write_ct(tmp_path / "in" / f"{n}.dcm", SID, f"619{n}", study_date=day)
    db, key = str(tmp_path / "s.db"), str(tmp_path / "k.key")
    with DicomSession(db) as session:
        session.ingest(str(tmp_path / "in"))
        session.enable_reversible_anonymization(key)
        [patient] = session.store.patients
        if blank_instance:
            patient.studies[0].series[0].instances[0].set_attr("0008,0020", "")
        report = session.audit()
        session.lock_identities(SID, tags_to_lock=STUDY_TAGS)
        session.anonymize(report)
        session.save(sync=True)
        return db, key, patient.patient_id, {
            st.study_instance_uid: st.study_date for st in patient.studies}


def _as_da(day):
    """A `Study.study_date` as the DA string the exporter writes: `''` for
    `None`."""
    return day.strftime("%Y%m%d") if day is not None else ""


def _study_date_warnings(caplog):
    return [r.getMessage() for r in caplog.records
            if r.name == "isocenter" and r.levelno == logging.WARNING
            and "Study Date" in r.getMessage()]


CASES = {
    # source StudyDate element, blank the instance copy, token's 0008,0020
    "empty_source": ("", False, ""),
    "invalid_source": ("20041399", False, "20041399"),
    "blank_instance": ("20040119", True, ""),
}


@pytest.mark.parametrize("source,blank,held", CASES.values(), ids=CASES.keys())
def test_a_restored_study_date_that_is_not_a_date_leaves_the_study_alone(
        tmp_path, caplog, source, blank, held):
    """T25/T26. The `Study` keeps what it held (`None` for a source ingest
    could not read, the shifted date where only the instance copy was
    blank), its revision does not move, the store and the exported file
    say the same after a save, and one WARNING says so with no date and no
    identifier. The instances still take the token's value: that is what
    the source held. Kills M27 (the gate dropped, on all three), M28 (a
    truthiness gate, which passes `'20041399'`) and M28c (the restored
    value interpolated into the WARNING)."""
    db, key, pseudonym, stored = _locked_and_anonymized(tmp_path, [source], blank)
    [(uid, kept)] = stored.items()
    assert kept is None or isinstance(kept, date)
    with DicomSession(db) as session:
        session.enable_reversible_anonymization(key)
        [study] = session.store.patients[0].studies
        revision = study._revision
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="isocenter"):
            session.recover_patient_identity(pseudonym, restore=True)
        assert study.study_date == kept
        assert study._revision == revision
        inst = study.series[0].instances[0]
        assert inst.attributes["0008,0020"] == held
        session.save(sync=True)
    warnings = _study_date_warnings(caplog)
    assert warnings == [UNREADABLE], caplog.text
    for secret in (SID, pseudonym, "20041399", "20040119", "2004-01-19",
                   _as_da(kept) or "never-a-date"):
        assert secret not in warnings[0]

    with DicomSession(db) as session:
        [study] = session.store.patients[0].studies
        assert study.study_date == kept
        session.export(str(tmp_path / "out"), use_compression=False)
    [path] = sorted((tmp_path / "out").rglob("*.dcm"))
    assert str(pydicom.dcmread(str(path)).StudyDate) == _as_da(kept)


def test_an_unreadable_date_on_a_multi_study_patient_keeps_the_one_warning(
        tmp_path, caplog):
    """The #619 gate lives inside the single-study arm. Two studies, the
    first ingested with a blank Study Date, so the token holds `''`: E2's
    multi-study WARNING is the only one, and neither `Study` moves. Kills
    the gate moved above the study count, which warns twice."""
    db, key, pseudonym, stored = _locked_and_anonymized(tmp_path, ["", "20050505"])
    with DicomSession(db) as session:
        session.enable_reversible_anonymization(key)
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="isocenter"):
            session.recover_patient_identity(pseudonym, restore=True)
        after = {st.study_instance_uid: st.study_date
                 for st in session.store.patients[0].studies}
    assert after == stored
    [warning] = _study_date_warnings(caplog)
    assert "2 studies" in warning
