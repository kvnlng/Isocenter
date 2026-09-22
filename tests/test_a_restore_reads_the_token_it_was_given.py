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
#: "Its de-identified Study Date", the words #566's multi-study WARNING
#: used until #583 retired it, not "the date it holds": where only the
#: instance copy was blank, what the Study holds is the shifted date
#: (review of #640, P-5).
UNREADABLE = ("The restored Study Date could not be read as a date, so the "
              "Study keeps its de-identified Study Date (#619).")


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
    `i` a study with one instance, `d` a study whose one series holds two,
    `s` a study whose one series holds no instance, `e` a study with no
    series at all. UIDs are spelled from `prefix` (the Patient ID by
    default). Returns the patient and its instances, in graph order."""
    patient = Patient(pid, NAME)
    prefix = prefix or pid
    instances = []
    for n, kind in enumerate(layout, start=1):
        study = Study(f"ST_{prefix}_{n}", date(2023, 1, n))
        if kind != "e":
            series = Series(f"SE_{prefix}_{n}", "CT", n)
            for k in range({"i": 1, "d": 2}.get(kind, 0)):
                inst = _instance(f"SOP_{prefix}_{n}_{k}" if kind == "d"
                                 else f"SOP_{prefix}_{n}")
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

        result = session.recover_patient_identity("ANON_616", restore=False)
        assert [v["0010,0020"] for v in result.values()] == [PID] * len(keep)
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
    fix. It now recovers. Until #583 `restore=True` wrote the token's
    values onto every instance of the patient, the second study's too, so
    study 2 took study 1's `ACC-ONE` (review of #640, P-3). Since #583 an
    instance carrying no token takes only the patient-level identifiers
    (group 0010) of the first token found, so study 2 keeps the Accession
    Number the pass left, and one WARNING counts it. Kills M6 (a sibling's
    token restored onto an instance carrying none)."""
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
        passed = patient.studies[1].series[0].instances[0].attributes.get("0008,0050")
        assert passed != ACC_ONE

        result = session.recover_patient_identity(pseudonym, restore=False)
        assert [v["0010,0020"] for v in result.values()] == ["PAT-A"]
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="isocenter"):
            session.recover_patient_identity(pseudonym, restore=True)
        assert patient.patient_id == "PAT-A"
        assert [(st.series[0].instances[0].attributes["0010,0020"],
                 st.series[0].instances[0].attributes.get("0008,0050"))
                for st in patient.studies] == [("PAT-A", ACC_ONE), ("PAT-A", passed)]
    assert _tokenless_warnings(caplog) == [tokenless(1, 2)], caplog.text


# ---------------------------------------------------------------------------
# #616, review of #640 F-1: a restore onto instances that do not carry the
# token read says so
# ---------------------------------------------------------------------------

ACC_ONE, ACC_TWO = "ACC-ONE", "ACC-TWO"


def tokenless(count, total):
    """The WARNING a restore logs when `count` of the `total` instances it
    wrote carry no token of ours (#583, Q-A). Until #583 it was F-1's
    "...restored onto N of M instances that do not carry that token...",
    which counted an instance carrying another token too."""
    return (f"{count} of {total} instances of this patient carry no identity "
            "token, so they took only the patient-level identifiers (group "
            "0010) of the token the patient's identity was restored from, and "
            "their other locked identifiers keep what anonymize() left (#583).")


def _tokenless_warnings(caplog):
    return [r.getMessage() for r in caplog.records
            if r.name == "isocenter" and r.levelno == logging.WARNING
            and "(#583)" in r.getMessage()]


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
        return {st.study_instance_uid: st.series[0].instances[0].attributes.get("0008,0050")
                for st in patient.studies}


def _value_free(warning, pseudonym):
    for secret in ("PAT-A", "PAT-B", NAME, pseudonym, ACC_ONE, ACC_TWO,
                   "6171", "6172", "6173", "6174"):
        assert secret not in warning, warning


def _locked_then_second_study_ingested(tmp_path):
    """Study 1 (`ACC-ONE`) locked under the default `tags_to_lock` and
    persisted, study 2 (`ACC-TWO`, same Patient ID) ingested after the lock,
    then audited, anonymized and saved. Returns (db, key, pseudonym, study
    2's Accession Number as the pass left it)."""
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
        passed = patient.studies[1].series[0].instances[0].attributes.get("0008,0050")
        assert passed != ACC_ONE
        return db, key, patient.patient_id, passed


def test_a_restore_onto_a_study_ingested_after_the_lock_warns(tmp_path, caplog):
    """The shape I2 turned from a refusal into a restore (review of #640,
    F-1, `after_ingest`): study 1 locked under the default `tags_to_lock`,
    which include Accession Number, study 2 ingested after the lock. Until
    #583 the restore wrote study 1's `ACC-ONE` into study 2's instance, and
    one WARNING counted it. Since #583 study 2 carries no token and takes
    group 0010 only, so it keeps the Accession Number the pass left; one
    WARNING, a count and no value, still says it was not restored in full.
    Kills the WARNING removed and the count miscounted."""
    db, key, pseudonym, passed = _locked_then_second_study_ingested(tmp_path)

    assert _restored_accessions(db, key, pseudonym, caplog) == {
        study_uid("6171"): ACC_ONE, study_uid("6172"): passed}
    warnings = _tokenless_warnings(caplog)
    assert warnings == [tokenless(1, 2)], caplog.text
    _value_free(warnings[0], pseudonym)


def test_a_restore_onto_a_study_ingested_after_the_lock_exports_no_other_studys_accession(
        tmp_path):
    """What reaches the file: after the restore, `export()` writes study 2's
    file without study 1's `ACC-ONE`. Until #583 it wrote `ACC-ONE` there,
    where the source held `ACC-TWO` (review of #640, round 2), and this test
    pinned that. The test above pins the instance in memory; this pins the
    file, because `export()` stamps some study-level tags from the `Study`
    rather than the instance (#566's Study Date), and a claim about the
    file should not rest on the instance. Kills M6 on study 2's file."""
    db, key, pseudonym, _ = _locked_then_second_study_ingested(tmp_path)
    with DicomSession(db) as session:
        session.enable_reversible_anonymization(key)
        session.recover_patient_identity(pseudonym, restore=True)
        session.save(sync=True)
        session.export(str(tmp_path / "out"), use_compression=False)
    written = {}
    for path in sorted((tmp_path / "out").rglob("*.dcm")):
        ds = pydicom.dcmread(str(path))
        written[str(ds.StudyInstanceUID)] = (str(ds.get("AccessionNumber", None)),
                                             str(ds.PatientID))
    assert written[study_uid("6171")] == (ACC_ONE, "PAT-A")
    assert written[study_uid("6172")][0] != ACC_ONE
    assert written[study_uid("6172")][1] == "PAT-A"


def test_a_study_carrying_another_token_is_restored_from_it(tmp_path, caplog):
    """`two_tokens` (review of #640, F-1): the pair's two objects locked
    separately before `audit()` merges them, so each study carries a token
    of ours and the two differ. Until #583 the first token found was the
    one read, study 2 took study 1's `ACC-ONE` over the `ACC-TWO` its own
    token holds, and the F-1 WARNING counted it. Since #583 each instance
    is restored from its own token: `ACC-TWO` back on study 2, and no
    WARNING, since both tokens hold the same name and ID. Kills M7's
    restore half (every instance given the first token's values)."""
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
        study_uid("6173"): ACC_ONE, study_uid("6174"): ACC_TWO}
    assert _tokenless_warnings(caplog) == [], caplog.text


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
    assert _tokenless_warnings(caplog) == [], caplog.text


def test_a_patient_locked_whole_restores_after_a_reopen_without_a_warning(
        tmp_path, caplog):
    """The control above never leaves memory, so every instance of a
    value-set holds the one `bytes` object the lock embedded, and a compare
    by identity reads the same as one by value. On every real path the
    store hydrates each instance's token as its own object: two studies
    with the same Accession Number locked together -- one value-set, so
    one token since #583 -- then anonymized, saved and reopened, carry
    equal tokens that are not the same object. They are one token, read
    once, and no WARNING. Kills a grouping or compare made by identity
    (`is not`, `id()`; K3 of the review of #640, round 2), which would read
    two tokens, and one made of a value-set's instances by study."""
    write_ct(tmp_path / "in" / "a.dcm", "PAT-A", "6175", name=NAME, accession=ACC_ONE)
    write_ct(tmp_path / "in" / "b.dcm", "PAT-A", "6176", name=NAME, accession=ACC_ONE)
    db, key = str(tmp_path / "s.db"), str(tmp_path / "k.key")
    with DicomSession(db) as session:
        session.enable_reversible_anonymization(key)
        session.ingest(str(tmp_path / "in"))
        session.lock_identities("PAT-A")
        report = session.audit()
        session.anonymize(report)
        session.save(sync=True)
        [patient] = session.store.patients
        pseudonym = patient.patient_id

    with DicomSession(db) as session:
        session.enable_reversible_anonymization(key)
        [patient] = session.store.patients
        first, second = (session.reversibility_service.token_of_ours(
            st.series[0].instances[0]) for st in patient.studies)
        assert first is not None and first == second and first is not second
        decrypts = []
        engine = session.reversibility_service.engine
        real = engine.decrypt
        engine.decrypt = lambda data: decrypts.append(1) or real(data)
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="isocenter"):
            session.recover_patient_identity(pseudonym, restore=True)
        assert patient.patient_id == "PAT-A"
        assert len(decrypts) == 1
        assert [st.series[0].instances[0].attributes["0008,0050"]
                for st in patient.studies] == [ACC_ONE, ACC_ONE]
    assert _tokenless_warnings(caplog) == [], caplog.text


def test_the_warning_counts_this_patients_instances_carrying_no_token(
        tmp_path, caplog):
    """Every other such test has one instance per study, the token on study
    1, and one patient in the session, where three wrong counts give the
    right answer. Here study 1 holds one instance and no token, study 2 the
    token, and study 3 two instances and no token, beside an unlocked
    patient of three instances: 3 of this patient's 4 instances carry no
    token, and each still takes the name and ID. Kills the count taken of
    the instances that carry one (`1 of 4`), the other patient's instances
    counted (`6 of 4`), and the total given as the studies (`3 of 3`) (K1,
    K2 and K5 of the review of #640, round 2, carried over to #583's
    WARNING)."""
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.enable_reversible_anonymization(str(tmp_path / "k.key"))
        patient, instances = _patient(session, "iid")
        _patient(session, "iii", pid="PAT-OTHER")
        session.lock_identities(PID, tags_to_lock=TAGS)
        _keep_tokens_only_on(instances, {1})
        _anonymize_by_hand(instances, patient)
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="isocenter"):
            session.recover_patient_identity("ANON_616", restore=True)
        assert [(i.attributes["0010,0010"], i.attributes["0010,0020"])
                for i in instances] == [(NAME, PID)] * 4
    assert _tokenless_warnings(caplog) == [tokenless(3, 4)], caplog.text


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
    """Study 1 carries a token of ours under a key this session does not
    hold, study 3 the lock's own: recovery raises the wrong-key text and
    writes nothing, rather than walking on to a token it can open and
    answering for the patient with it (review of #640, P-6). Since #583
    every distinct token is opened before anything is written, so this
    holds whichever study the unopenable token is on
    (`test_one_token_per_value_set.py` puts it on study 2); on study 1 it
    is also the first token found, which still speaks for the patient.
    Kills M26b (the walk's `break` dropped, which reads the last token)."""
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


def test_an_unreadable_date_on_one_study_warns_for_that_study_only(
        tmp_path, caplog):
    """Two studies, the first ingested with a blank Study Date. Since #583
    each study's instances carry their own token, so study 1's holds `''`
    and study 2's holds `20050505`: study 1's `Study` keeps what it held,
    with the one #619 WARNING, and study 2's takes its own date. Until #583
    the one token held study 1's `''`, E2's multi-study WARNING was the
    only one, and neither `Study` moved. Kills the #619 gate dropped on a
    multi-study patient and the gate applied to every study once any
    study's date is unreadable."""
    db, key, pseudonym, stored = _locked_and_anonymized(tmp_path, ["", "20050505"])
    with DicomSession(db) as session:
        session.enable_reversible_anonymization(key)
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="isocenter"):
            session.recover_patient_identity(pseudonym, restore=True)
        after = {st.study_instance_uid: st.study_date
                 for st in session.store.patients[0].studies}
    assert after == {study_uid("6191"): stored[study_uid("6191")],
                     study_uid("6192"): date(2005, 5, 5)}
    assert _study_date_warnings(caplog) == [UNREADABLE], caplog.text
