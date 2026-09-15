"""A lock writes one identity token per distinct set of locked values,
captured from each instance, and a restore writes each instance's own
values back (#583).

`lock_identities()` captured the record from the patient's **first**
instance and embedded that one token on every instance, so
`recover_patient_identity(restore=True)` wrote study 1's values onto every
study: measured on e418d3d, 3.12 and 3.14t (`probes-I3/p1_premise.py`),
study 2's `ACC-TWO` became `ACC-ONE` in the file `export()` wrote, and an
instance-level or series-level locked tag crossed the same way. The owner
ruled one token per distinct value-set, captured per instance (brief-I3
addendum, section 7). What each ruling pins here:

- **The lock** groups instances by the record captured from each and
  embeds one token per group. A value a pass wrote on *any* instance is
  refused, not only on the first (a mixed patient); an instance whose
  record would be empty refuses the lock, counted (#638 per instance,
  Q-D d1). An existing token is judged against its first holder's capture
  (Q-C c3), so a 0.9.7 store re-locks as it did.
- **The restore** opens every distinct token before it writes anything,
  gives each instance its own token's values, gives an instance carrying
  no token only the patient-level identifiers (group 0010) of the first
  token found (Q-A), and puts each study's own Study Date on its `Study`.
- **A token written before 0.9.8** that one lock shared across studies
  (non-blank values outside group 0010, not stamped by this store) is
  restored in full on the first study that carries it and as group 0010
  only elsewhere (Q-B b2), with a WARNING.
- **Tokens that disagree** on Patient's Name or Patient ID leave each
  instance its own; the patient takes the first token's, with a WARNING
  (Q-F f1).

Every WARNING and refusal here carries counts and tags, never a value, a
Patient ID or a UID (P6).

**Why this file imports what it does.** `isocenter.session` is named, so
its probe row is charged.
"""
import logging
from datetime import date

import numpy as np
import pydicom
import pytest
from cryptography.fernet import Fernet

from isocenter.entities import Instance, Patient, Series, Study
from isocenter.session import DicomSession

from support.ct_small_files import study_uid, write_ct

PID, NAME = "PAT-583", "Secret^Eighty"
SEQ, CONTENT = "0400,0500", "0400,0510"
ACC = "0008,0050"
ACC_ONE, ACC_TWO = "ACC-ONE", "ACC-TWO"
TAGS = ["0010,0010", "0010,0020"]
DEFAULT = ["0010,0010", "0010,0020", "0010,0030", "0010,0040", "0008,0050"]


def tokenless(count, total):
    """The WARNING a restore logs for instances carrying no token (Q-A)."""
    return (f"{count} of {total} instances of this patient carry no identity "
            "token, so they took only the patient-level identifiers (group "
            "0010) of the first token found, and their other locked identifiers "
            "keep what anonymize() left (#583).")


def old_shared(count, total):
    """The WARNING a restore logs for holders of a pre-0.9.8 token shared
    across studies, outside the first study carrying it (Q-B b2)."""
    return (f"{count} of {total} instances of this patient carry an identity "
            "token written before 0.9.8 and shared across studies, which holds "
            "one study's values, so outside the first study carrying it they "
            "took only its patient-level identifiers (group 0010), and their "
            "other locked identifiers keep what anonymize() left (#583).")


def disagree(count, total):
    """The WARNING a restore logs when tokens disagree on name or ID (Q-F)."""
    return (f"{count} of {total} identity tokens of this patient hold a "
            "Patient's Name or Patient ID different from the first token found; "
            "the patient takes the first token's, which export() stamps on "
            "every study (#583).")


def empty_records(count, total, tags):
    """The #638 refusal, per instance (Q-D d1)."""
    return (f"lock_identities: {count} of {total} instances of this patient "
            f"hold no value in {', '.join(tags)}, every tag tags_to_lock names, "
            "so there is nothing to stash on them and the lock would secure "
            "nothing there. Name tags every instance of this patient carries; "
            "the token this call would have written is unchanged.")


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _session(tmp_path, key="k.key"):
    session = DicomSession(str(tmp_path / "s.db"))
    session.enable_reversible_anonymization(str(tmp_path / key))
    return session


def _instance(uid, name=NAME, pid=PID, **attrs):
    inst = Instance(uid, "1.2.840.10008.5.1.4.1.1.2", 1)
    inst.file_path = None
    inst.set_attr("0010,0010", name)
    inst.set_attr("0010,0020", pid)
    for tag, value in attrs.items():
        inst.set_attr(tag.replace("_", ","), value)
    inst.set_pixel_data(np.zeros((8, 8), dtype=np.uint16))
    return inst


def _patient(session, studies, pid=PID, name=NAME):
    """A hand-built patient: `studies` is one list per study of per-instance
    attribute dicts (`{"0008_0050": "ACC-ONE"}`), each study one series.
    Returns the patient and its instances by study."""
    patient = Patient(pid, name)
    by_study = []
    for n, specs in enumerate(studies, start=1):
        study = Study(f"ST_{pid}_{n}", date(2023, 1, n))
        series = Series(f"SE_{pid}_{n}", "CT", n)
        insts = []
        for k, spec in enumerate(specs):
            spec = dict(spec)
            inst = _instance(f"SOP_{pid}_{n}_{k}", name=spec.pop("name", name),
                             pid=spec.pop("pid", pid), **spec)
            series.instances.append(inst)
            insts.append(inst)
        study.series.append(series)
        patient.studies.append(study)
        by_study.append(insts)
    session.store.patients.append(patient)
    return patient, by_study


def _all(by_study):
    return [inst for insts in by_study for inst in insts]


def _tokens(session, instances):
    return [session.reversibility_service.token_of_ours(inst) for inst in instances]


def _held(session, inst):
    rs = session.reversibility_service
    return rs.open_token(rs.token_of_ours(inst))


def _anonymize_by_hand(patient, instances, pseudonym="ANON_583", **tags):
    """What a pass leaves on the identifiers, without running one."""
    for inst in instances:
        inst.set_attr("0010,0010", "ANONYMIZED")
        inst.set_attr("0010,0020", pseudonym)
        for tag, value in tags.items():
            inst.set_attr(tag.replace("_", ","), value)
    patient.patient_name, patient.patient_id = "ANONYMIZED", pseudonym


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records
            if r.name == "isocenter" and r.levelno == logging.WARNING
            and "(#583)" in r.getMessage()]


def _restore(session, caplog, pseudonym="ANON_583"):
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="isocenter"):
        session.recover_patient_identity(pseudonym, restore=True)
    return _warnings(caplog)


def _value_free(text, *secrets):
    for secret in (PID, NAME, "ANON_583", ACC_ONE, ACC_TWO, *secrets):
        assert secret not in text, text


def _exported(folder, *keywords):
    found = {}
    for path in sorted(folder.rglob("*.dcm")):
        ds = pydicom.dcmread(str(path))
        found[str(ds.StudyInstanceUID)] = tuple(str(ds.get(k, None)) for k in keywords)
    return found


def _pre_098_token(session, instances, record, stamped=False):
    """What a release before this fix left: one token, built from `record`
    (the first instance's), embedded on every instance. `stamped=False` is
    the 0.9.7 shape (no `__locked__`); `stamped=True` is a 0.9.8
    pre-release's, which stamped the one shared token."""
    rs = session.reversibility_service
    session._key_for_locking()
    token = rs.generate_identity_token(record)
    for inst in instances:
        rs.embed_identity_token(inst, token)
        if not stamped:
            inst._locked_token = None
    return token


# ---------------------------------------------------------------------------
# The lock: one token per value-set
# ---------------------------------------------------------------------------

def test_two_studies_get_their_own_tokens_and_their_own_accessions_back(tmp_path):
    """T27. The #583 shape as filed, end to end: two studies of one patient,
    `ACC-ONE` and `ACC-TWO`, the default `tags_to_lock`, then `audit()`,
    `anonymize()`, a save and a reopen. Two distinct tokens, each holding
    its own study's Accession Number; the restore puts each back, draws no
    #583 WARNING, and `export()` writes each study's own. Kills M1 (the
    capture taken per patient, from the first instance)."""
    write_ct(tmp_path / "in" / "a.dcm", PID, "5831", name=NAME, accession=ACC_ONE)
    write_ct(tmp_path / "in" / "b.dcm", PID, "5832", name=NAME, accession=ACC_TWO)
    db, key = str(tmp_path / "s.db"), str(tmp_path / "k.key")
    with DicomSession(db) as session:
        session.enable_reversible_anonymization(key)
        session.ingest(str(tmp_path / "in"))
        session.lock_identities(PID)
        [patient] = session.store.patients
        firsts = [st.series[0].instances[0] for st in patient.studies]
        assert len(set(_tokens(session, firsts))) == 2
        assert [_held(session, inst)[ACC] for inst in firsts] == [ACC_ONE, ACC_TWO]
        report = session.audit()
        session.anonymize(report)
        session.save(sync=True)
        pseudonym = patient.patient_id

    with DicomSession(db) as session:
        session.enable_reversible_anonymization(key)
        caplog_records = []
        handler = logging.Handler()
        handler.emit = caplog_records.append
        logging.getLogger("isocenter").addHandler(handler)
        try:
            session.recover_patient_identity(pseudonym, restore=True)
        finally:
            logging.getLogger("isocenter").removeHandler(handler)
        assert not [r for r in caplog_records if "(#583)" in r.getMessage()]
        [patient] = session.store.patients
        assert {st.study_instance_uid: st.series[0].instances[0].attributes[ACC]
                for st in patient.studies} == {study_uid("5831"): ACC_ONE,
                                               study_uid("5832"): ACC_TWO}
        session.export(str(tmp_path / "out"), use_compression=False)
    assert _exported(tmp_path / "out", "AccessionNumber") == {
        study_uid("5831"): (ACC_ONE,), study_uid("5832"): (ACC_TWO,)}


def test_a_value_set_shares_one_token_across_its_instances(tmp_path):
    """T27b. Twenty instances over two studies, two accessions: two distinct
    tokens, not twenty, and one encryption per value-set. Kills M2 (a token
    encrypted per instance: equal records, distinct bytes)."""
    with _session(tmp_path) as session:
        _, by_study = _patient(session, [[{"0008_0050": ACC_ONE}] * 10,
                                         [{"0008_0050": ACC_TWO}] * 10])
        session._key_for_locking()
        encrypts = []
        engine = session.reversibility_service.engine
        real = engine.encrypt
        engine.encrypt = lambda data: encrypts.append(1) or real(data)
        result = session.lock_identities(PID, tags_to_lock=TAGS + [ACC])
        assert len(result) == 20
        assert [len(set(_tokens(session, insts))) for insts in by_study] == [1, 1]
        assert len(set(_tokens(session, _all(by_study)))) == 2
        assert len(encrypts) == 2


def test_an_instance_level_tag_is_captured_from_each_instance(tmp_path, caplog):
    """T27d. Content Date differs between the two instances of one series:
    each takes its own back. Kills M1b (the capture taken per series or
    per study, which the two-study test cannot tell from per instance)."""
    with _session(tmp_path) as session:
        patient, by_study = _patient(session, [[{"0008_0023": "20040111"},
                                                {"0008_0023": "20040112"}]])
        session.lock_identities(PID, tags_to_lock=TAGS + ["0008,0023"])
        instances = _all(by_study)
        assert len(set(_tokens(session, instances))) == 2
        _anonymize_by_hand(patient, instances, **{"0008_0023": "20030101"})
        assert _restore(session, caplog) == []
        assert [i.attributes["0008,0023"] for i in instances] == ["20040111", "20040112"]


def test_the_lock_log_names_every_tag_any_value_set_holds(tmp_path, caplog):
    """The `Secured identity (tags: ...)` line names the union, in
    `tags_to_lock` order, and counts every instance. Study 2 carries Other
    Patient IDs and study 1 does not, so the first record lacks a tag the
    second holds. Kills M20 (the tags read off the first value-set)."""
    with _session(tmp_path) as session:
        _patient(session, [[{"0008_0050": ACC_ONE}],
                           [{"0010_1000": "OTHER-2", "0008_0050": ACC_TWO}]])
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="isocenter"):
            session.lock_identities(PID, persist=True,
                                    tags_to_lock=TAGS + ["0010,1000", ACC])
    assert ("Secured identity (tags: ['0010,0010', '0010,0020', '0010,1000', "
            "'0008,0050']) in 2 instances of one patient.") in caplog.text


# ---------------------------------------------------------------------------
# The lock: refusals judged per value-set
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("replaced", [1, 0], ids=["second_study", "first_study"])
def test_a_mixed_patient_is_refused_whichever_study_the_pass_reached(
        tmp_path, replaced):
    """T30. One study raw, the other holding the Accession Number a pass
    wrote, with its record: refused, naming the tag, and nothing written.
    On e418d3d the lock judged the first instance only, so the pass output
    on study 2 was accepted and study 2's token held study 1's `ACC-ONE`
    (`probes-I3/p4_shapes.py mixed`). Kills M5 (the replacement judged on
    the first instance only)."""
    with _session(tmp_path) as session:
        _, by_study = _patient(session, [[{"0008_0050": ACC_ONE}],
                                         [{"0008_0050": ACC_TWO}]])
        [inst] = by_study[replaced]
        inst.record_remediation(ACC, "PASS-OUTPUT")
        inst.set_attr(ACC, "PASS-OUTPUT")
        with pytest.raises(RuntimeError) as raised:
            session.lock_identities(PID)
        assert str(raised.value) == (
            "lock_identities: this patient already carries a replacement in "
            "0008,0050 ('PASS-OUTPUT'), so there is no original identity left "
            "to stash. Lock identities before anonymize(), and do not re-lock a "
            "patient after it; the token this call would have written is unchanged.")
        assert all(SEQ not in i.sequences for i in _all(by_study))
        _value_free(str(raised.value))


def test_a_tag_a_pass_emptied_on_one_study_is_refused(tmp_path):
    """The blanked check per value-set: study 2's Accession Number was
    emptied by a pass (its record says so), study 1's is an original. On
    e418d3d the first instance held a value, so the lock was accepted and
    study 2's token held study 1's. Kills M5 on the blanked check."""
    with _session(tmp_path) as session:
        _, by_study = _patient(session, [[{"0008_0050": ACC_ONE}],
                                         [{"0008_0050": ACC_TWO}]])
        [inst] = by_study[1]
        inst.record_remediation(ACC, "")
        inst.set_attr(ACC, "")
        with pytest.raises(RuntimeError) as raised:
            session.lock_identities(PID, tags_to_lock=TAGS + [ACC])
        assert str(raised.value) == (
            "lock_identities: this patient holds no value in 0008,0050, which "
            "anonymize() emptied or removed, so there is no original left to "
            "stash. To lock this patient without it, call lock_identities(<its "
            "Patient ID>, tags_to_lock=['0010,0010', '0010,0020']); the token "
            "this call would have written is unchanged.")
        assert all(SEQ not in i.sequences for i in _all(by_study))


def test_an_instance_with_nothing_to_stash_refuses_the_lock(tmp_path):
    """T31, #638 per instance (Q-D d1). Study 1 carries Other Patient IDs,
    study 2 does not: refused, counted, naming the tags only, and nothing
    written. On e418d3d the lock was accepted and study 2's token held
    study 1's value (`probes-I3/p4_shapes.py empty_second`). Kills M5c
    (the #638 check on the first instance only)."""
    with _session(tmp_path) as session:
        _, by_study = _patient(session, [[{"0010_1000": "OTHER-1"}],
                                         [{}, {}]])
        with pytest.raises(RuntimeError) as raised:
            session.lock_identities(PID, tags_to_lock=["0010,1000"])
        assert str(raised.value) == empty_records(2, 3, ["0010,1000"])
        assert all(SEQ not in i.sequences for i in _all(by_study))
        _value_free(str(raised.value), "OTHER-1")


def test_the_patient_fallback_is_checked_once_not_once_per_instance(
        tmp_path, monkeypatch):
    """T34. Sixty instances with no copy of Patient's Name or Patient ID,
    so both are stashed from the patient (#495), whose check asks every
    instance whether a pass wrote the value. Asked once per tag, that is
    linear; asked once per instance, it is quadratic (3,600 calls per tag
    here, 10^8 at 10k instances). Counted, not timed. Kills M10."""
    calls = []
    real = Instance.remediation_vouches_for
    monkeypatch.setattr(Instance, "remediation_vouches_for",
                        lambda self, tag, value: calls.append(tag) or real(self, tag, value))
    with _session(tmp_path) as session:
        _, by_study = _patient(session, [[{"0008_0050": ACC_ONE}] * 60])
        for inst in _all(by_study):
            del inst.attributes["0010,0010"]
            del inst.attributes["0010,0020"]
        session.lock_identities(PID)
    assert len(calls) <= 3 * 60 * len(DEFAULT), len(calls)


def test_a_batch_numbers_a_mixed_patient_and_locks_nobody(tmp_path):
    """T35. `[n of m]` is still one refusal per patient: B is mixed, A is
    not, and neither is locked."""
    with _session(tmp_path) as session:
        _, a = _patient(session, [[{"0008_0050": ACC_ONE}]], pid="PAT-583-A")
        _, b = _patient(session, [[{"0008_0050": ACC_ONE}], [{"0008_0050": ACC_TWO}]],
                        pid="PAT-583-B")
        b[1][0].record_remediation(ACC, "PASS-OUTPUT")
        b[1][0].set_attr(ACC, "PASS-OUTPUT")
        with pytest.raises(RuntimeError) as raised:
            session.lock_identities(["PAT-583-A", "PAT-583-B"])
        assert str(raised.value).startswith(
            "lock_identities: 1 of 2 patients cannot be locked as asked")
        assert "\n[2 of 2] lock_identities: this patient already carries a " \
               "replacement in 0008,0050" in str(raised.value)
        assert all(SEQ not in i.sequences for i in _all(a) + _all(b))


# ---------------------------------------------------------------------------
# A re-lock over a token a release before this fix wrote (Q-C c3)
# ---------------------------------------------------------------------------

def test_a_relock_over_a_097_token_shared_across_raw_studies_is_accepted(tmp_path):
    """T29d. A 0.9.7 store: two raw studies, `ACC-ONE` and `ACC-TWO`, one
    unstamped token holding study 1's record. The re-lock is judged
    against the first holder's capture, as e418d3d judged it against the
    first instance, so it is accepted, and each study now carries a token
    holding its own Accession Number. Kills M5b (every holder judged:
    study 2's own `ACC-TWO` is not the `ACC-ONE` it holds, and a raw
    0.9.7 store is refused with advice that would write `ACC-ONE` over
    it)."""
    with _session(tmp_path) as session:
        _, by_study = _patient(session, [[{"0008_0050": ACC_ONE}],
                                         [{"0008_0050": ACC_TWO}]])
        _pre_098_token(session, _all(by_study),
                       {"0010,0010": NAME, "0010,0020": PID, ACC: ACC_ONE})
        session.lock_identities(PID, tags_to_lock=TAGS + [ACC])
        assert [_held(session, insts[0])[ACC] for insts in by_study] == [ACC_ONE, ACC_TWO]


def test_a_relock_whose_first_holder_differs_from_a_097_token_is_refused(tmp_path):
    """The other half of c3: the first holder's capture still differs from
    what the unstamped token holds, so #607's refusal stands."""
    with _session(tmp_path) as session:
        _, by_study = _patient(session, [[{"0008_0050": "Project-X"}],
                                         [{"0008_0050": ACC_TWO}]])
        _pre_098_token(session, _all(by_study),
                       {"0010,0010": NAME, "0010,0020": PID, ACC: ACC_ONE})
        with pytest.raises(RuntimeError) as raised:
            session.lock_identities(PID, tags_to_lock=TAGS + [ACC])
        assert "did not come from this store, so the value it holds in 0008,0050" \
            in str(raised.value)


# ---------------------------------------------------------------------------
# The restore
# ---------------------------------------------------------------------------

def _locked_then_second_study_ingested(tmp_path, tags=None):
    """Study 1 locked and persisted, study 2 (same Patient ID, `ACC-TWO`)
    ingested after the lock, then audited, anonymized and saved. Both files
    carry a non-blank birth date and sex, since CT_small's birth date is
    blank and a blank-for-blank restore passes by accident. Returns (db,
    key, pseudonym, study 2's Accession Number after the pass)."""
    for folder, suffix, acc in (("one", "5835", ACC_ONE), ("two", "5836", ACC_TWO)):
        path = write_ct(tmp_path / folder / "a.dcm", PID, suffix, name=NAME, accession=acc)
        ds = pydicom.dcmread(path)
        ds.PatientBirthDate, ds.PatientSex = "19700101", "F"
        ds.save_as(path)
    db, key = str(tmp_path / "s.db"), str(tmp_path / "k.key")
    with DicomSession(db) as session:
        session.enable_reversible_anonymization(key)
        session.ingest(str(tmp_path / "one"))
        session.lock_identities(PID, persist=True, tags_to_lock=tags)
        session.ingest(str(tmp_path / "two"))
        report = session.audit()
        session.anonymize(report)
        session.save(sync=True)
        [patient] = session.store.patients
        second = patient.studies[1].series[0].instances[0]
        assert SEQ not in second.sequences
        return db, key, patient.patient_id, dict(second.attributes)


def test_a_study_ingested_after_the_lock_takes_only_patient_level_identifiers(
        tmp_path, caplog):
    """T28 (Q-A, a2-group0010). Study 2 carries no token: it takes Patient's
    Name, Patient ID, Birth Date and Sex from the first token found, keeps
    the Accession Number the pass left, and `export()` writes no
    `ACC-ONE` into its file. One WARNING with the count. On e418d3d it
    took `ACC-ONE`. Kills M6 (a sibling's token restored onto it) and M6b
    (nothing restored onto it: its birth date stays de-identified)."""
    db, key, pseudonym, passed = _locked_then_second_study_ingested(tmp_path)
    assert passed["0010,0030"] != "19700101"
    with DicomSession(db) as session:
        session.enable_reversible_anonymization(key)
        [patient] = session.store.patients
        second = patient.studies[1].series[0].instances[0]
        warnings = _restore(session, caplog, pseudonym)
        assert warnings == [tokenless(1, 2)], caplog.text
        _value_free(warnings[0], pseudonym)
        assert {t: second.attributes.get(t) for t in DEFAULT} == {
            "0010,0010": NAME, "0010,0020": PID, "0010,0030": "19700101",
            "0010,0040": "F", ACC: passed.get(ACC)}
        session.save(sync=True)
        session.export(str(tmp_path / "out"), use_compression=False)
    written = _exported(tmp_path / "out", "AccessionNumber", "PatientID")
    assert written[study_uid("5835")] == (ACC_ONE, PID)
    assert written[study_uid("5836")][0] != ACC_ONE
    assert written[study_uid("5836")][1] == PID


def test_a_patient_study_tag_reaches_a_study_carrying_no_token(tmp_path, caplog):
    """T28b. Group 0010, not only the entity pair: Patient's Age (0010,1010)
    is restored onto the instance carrying no token, as the CHANGELOG
    discloses (it is a Patient Study module attribute, so it may be
    another study's). Kills M6c (only 0010,0010 and 0010,0020)."""
    with _session(tmp_path) as session:
        patient, by_study = _patient(session, [[{"0010_1010": "042Y", "0008_0050": ACC_ONE}],
                                               [{"0010_1010": "043Y", "0008_0050": ACC_TWO}]])
        session.lock_identities(PID, tags_to_lock=TAGS + ["0010,1010", ACC])
        del by_study[1][0].sequences[SEQ]
        _anonymize_by_hand(patient, _all(by_study),
                           **{"0010_1010": "000Y", "0008_0050": "X"})
        assert _restore(session, caplog) == [tokenless(1, 2)]
        second = by_study[1][0]
        assert (second.attributes["0010,1010"], second.attributes[ACC]) == ("042Y", "X")


def test_a_study_carrying_its_own_token_is_restored_from_it(tmp_path, caplog):
    """The pair's two objects locked separately and then merged
    (`two_tokens`): each study is restored from its own token, silently.
    On e418d3d study 2 took study 1's `ACC-ONE` and the F-1 WARNING
    counted it."""
    with _session(tmp_path) as session:
        patient, by_study = _patient(session, [[{"0008_0050": ACC_ONE}]])
        other, other_by_study = _patient(session, [[{"0008_0050": ACC_TWO}]],
                                         pid="PAT-583-B")
        session.lock_identities(PID, tags_to_lock=TAGS + [ACC])
        session.lock_identities("PAT-583-B", tags_to_lock=TAGS + [ACC])
        session.store.patients.remove(other)
        patient.studies.extend(other.studies)
        instances = _all(by_study) + _all(other_by_study)
        _anonymize_by_hand(patient, instances, **{"0008_0050": "X"})
        assert _restore(session, caplog) == [disagree(1, 2)]
        assert [i.attributes[ACC] for i in instances] == [ACC_ONE, ACC_TWO]
        assert [i.attributes["0010,0020"] for i in instances] == [PID, "PAT-583-B"]
        assert patient.patient_id == PID


def test_every_token_is_opened_before_anything_is_written(tmp_path, caplog):
    """T33. Study 1's token opens, study 2's is a token of ours under a key
    this session does not hold: the restore raises the wrong-key text and
    study 1 is left as the pass left it; `restore=False` raises too. On
    e418d3d the first token was the one read, so study 1 was restored and
    study 2 was given its values. Kills M7 (only the first token opened)."""
    key = tmp_path / "k.key"
    with _session(tmp_path) as session:
        patient, by_study = _patient(session, [[{"0008_0050": ACC_ONE}],
                                               [{"0008_0050": ACC_TWO}]])
        session.lock_identities(PID, tags_to_lock=TAGS + [ACC])
        stranger = Fernet(Fernet.generate_key()).encrypt(b'{"0008,0050": "ELSEWHERE"}')
        by_study[1][0].sequences[SEQ].items[0].set_attr(CONTENT, stranger)
        instances = _all(by_study)
        _anonymize_by_hand(patient, instances, **{"0008_0050": "X"})
        wrong = (f"the key at {key} does not decrypt this patient's identity "
                 "token; recovery needs the key the identity was locked with")
        for restore in (True, False):
            with pytest.raises(RuntimeError) as raised:
                session.recover_patient_identity("ANON_583", restore=restore)
            assert str(raised.value) == wrong
        assert [i.attributes["0010,0010"] for i in instances] == ["ANONYMIZED"] * 2
        assert [i.attributes[ACC] for i in instances] == ["X"] * 2
        assert patient.patient_id == "ANON_583"


def test_tokens_that_disagree_on_the_name_keep_each_instance_its_own(tmp_path, caplog):
    """T32 (Q-F f1). One Patient ID whose two studies spell the name
    differently: two tokens. Each instance takes its own name back, the
    patient takes the first token's, and one WARNING counts the tokens
    that disagree, with no name in it. Kills M9 (the instance copies
    harmonized to the first token's, which loses the second spelling on a
    restore-then-relock)."""
    with _session(tmp_path) as session:
        patient, by_study = _patient(session, [[{"name": "Doe^Ann"}],
                                               [{"name": "DOE^ANN"}]], name="Doe^Ann")
        session.lock_identities(PID, tags_to_lock=TAGS)
        instances = _all(by_study)
        assert len(set(_tokens(session, instances))) == 2
        _anonymize_by_hand(patient, instances)
        warnings = _restore(session, caplog)
        assert warnings == [disagree(1, 2)]
        _value_free(warnings[0], "Doe^Ann", "DOE^ANN")
        assert [i.attributes["0010,0010"] for i in instances] == ["Doe^Ann", "DOE^ANN"]
        assert (patient.patient_name, patient.patient_id) == ("Doe^Ann", PID)


def test_a_merged_pair_whose_tokens_disagree_on_the_id_stays_one_patient(
        tmp_path, caplog):
    """The #548 interaction Q-F asked to re-measure: two patients locked
    separately, made to share an ID by hand, merged by `audit()`, then
    anonymized. The restore gives study 2's instance its own `PAT-B`, the
    patient the first token's `PAT-A`: no second merge, no refusal, one
    patient before and after a save and a reopen, and `export()` stamps
    `PAT-A` on both files, as the #548 merge already did."""
    write_ct(tmp_path / "in" / "a.dcm", "PAT-A", "5837", name="Doe^Ann", accession=ACC_ONE)
    write_ct(tmp_path / "in" / "b.dcm", "PAT-B", "5838", name="Roe^Bea", accession=ACC_TWO)
    db, key = str(tmp_path / "s.db"), str(tmp_path / "k.key")
    with DicomSession(db) as session:
        session.enable_reversible_anonymization(key)
        session.ingest(str(tmp_path / "in"))
        first, second = sorted(session.store.patients, key=lambda p: p.patient_id)
        session.lock_identities("PAT-A")
        session.lock_identities("PAT-B")
        second.patient_id = first.patient_id
        for inst in second.studies[0].series[0].instances:
            inst.set_attr("0010,0020", first.patient_id)
        report = session.audit()
        session.anonymize(report)
        [patient] = session.store.patients
        pseudonym = patient.patient_id
        warnings = _restore(session, caplog, pseudonym)
        assert warnings == [disagree(1, 2)], caplog.text
        assert len(session.store.patients) == 1
        assert (patient.patient_name, patient.patient_id) == ("Doe^Ann", "PAT-A")
        assert {st.study_instance_uid: (st.series[0].instances[0].attributes["0010,0020"],
                                        st.series[0].instances[0].attributes[ACC])
                for st in patient.studies} == {study_uid("5837"): ("PAT-A", ACC_ONE),
                                               study_uid("5838"): ("PAT-B", ACC_TWO)}
        session.save(sync=True)
    with DicomSession(db) as session:
        [patient] = session.store.patients
        assert patient.patient_id == "PAT-A" and len(patient.studies) == 2
        session.export(str(tmp_path / "out"), use_compression=False)
    assert _exported(tmp_path / "out", "PatientID", "AccessionNumber") == {
        study_uid("5837"): ("PAT-A", ACC_ONE), study_uid("5838"): ("PAT-A", ACC_TWO)}


# ---------------------------------------------------------------------------
# A token a release before this fix shared across studies (Q-B b2)
# ---------------------------------------------------------------------------

def _pre_098_patient(session, accessions, stamped=False):
    patient, by_study = _patient(session, [[{"0008_0050": a}] for a in accessions])
    record = {"0010,0010": NAME, "0010,0020": PID, ACC: accessions[0]}
    _pre_098_token(session, _all(by_study), record, stamped=stamped)
    _anonymize_by_hand(patient, _all(by_study), **{"0008_0050": "X"})
    return patient, _all(by_study)


def test_a_097_token_shared_across_studies_restores_study_level_values_on_the_first(
        tmp_path, caplog):
    """T29. The 0.9.7 shape: one unstamped token holding study 1's
    `ACC-ONE`, on both studies. Study 1 takes it back in full; study 2
    takes only group 0010, keeps the pass's Accession Number, and one
    WARNING counts it. Kills M3 (the detector read as "shared bytes"
    alone would also fire on T29b) and the b2 restore dropped."""
    with _session(tmp_path) as session:
        patient, instances = _pre_098_patient(session, [ACC_ONE, ACC_TWO])
        warnings = _restore(session, caplog)
        assert warnings == [old_shared(1, 2)]
        _value_free(warnings[0])
        assert [(i.attributes["0010,0010"], i.attributes[ACC]) for i in instances] == [
            (NAME, ACC_ONE), (NAME, "X")]
        assert patient.patient_id == PID


@pytest.mark.parametrize("held", ["", None], ids=["blank_accession", "no_accession"])
def test_a_097_token_holding_no_study_level_value_draws_no_warning(tmp_path, caplog, held):
    """T29b. CT_small carries its Accession Number blank and the default
    tags lock it, so an ordinary 0.9.7 multi-study token holds `''`
    outside group 0010: not a study's value, and no WARNING. Kills M4 (the
    brief's detector, which counts a blank)."""
    with _session(tmp_path) as session:
        patient, by_study = _patient(session, [[{}], [{}]])
        record = {"0010,0010": NAME, "0010,0020": PID}
        if held is not None:
            record[ACC] = held
        _pre_098_token(session, _all(by_study), record)
        _anonymize_by_hand(patient, _all(by_study))
        assert _restore(session, caplog) == []
        assert [i.attributes["0010,0010"] for i in _all(by_study)] == [NAME, NAME]


def test_a_shared_token_this_store_stamped_is_restored_on_every_holder(tmp_path, caplog):
    """T29c, residual (i) as ruled: a 0.9.8 pre-release stamped the one
    token it shared, and a stamped token reads as one this fix wrote, so
    every holder takes it in full with no WARNING. Pinned so that a change
    to it is a decision. Kills M3b (the stamp exemption dropped)."""
    with _session(tmp_path) as session:
        _, instances = _pre_098_patient(session, [ACC_ONE, ACC_TWO], stamped=True)
        assert _restore(session, caplog) == []
        assert [i.attributes[ACC] for i in instances] == [ACC_ONE, ACC_ONE]


def test_a_097_token_over_equal_accessions_still_warns(tmp_path, caplog):
    """Residual (ii) as ruled: two studies whose Accession Numbers were
    equal cannot be told from two that differed, so study 2 keeps the
    pass's value and the WARNING fires, although restoring it would have
    been right."""
    with _session(tmp_path) as session:
        _, instances = _pre_098_patient(session, ["ACC-SAME", "ACC-SAME"])
        assert _restore(session, caplog) == [old_shared(1, 2)]
        assert [i.attributes[ACC] for i in instances] == ["ACC-SAME", "X"]


def test_a_097_token_shared_inside_one_study_is_restored_in_full(tmp_path, caplog):
    """Residual (iii) as ruled: an earlier release's one token over the two
    instances of a single study, holding a non-blank Content Date, is not
    shared across studies, so both instances take it in full and no
    WARNING fires -- the second instance's own date, which that token never
    held, cannot be told apart. Pinned so that a change to it is a
    decision. (The detector counting instances rather than studies, M15,
    is equivalent under the ruling: every holder of such a token is in the
    first study carrying it, so all of them take it in full either way.)"""
    with _session(tmp_path) as session:
        patient, by_study = _patient(session, [[{"0008_0023": "20040111"},
                                                {"0008_0023": "20040112"}]])
        instances = _all(by_study)
        _pre_098_token(session, instances, {"0010,0010": NAME, "0010,0020": PID,
                                            "0008,0023": "20040111"})
        _anonymize_by_hand(patient, instances, **{"0008_0023": "20030101"})
        assert _restore(session, caplog) == []
        assert [i.attributes["0008,0023"] for i in instances] == ["20040111", "20040111"]
