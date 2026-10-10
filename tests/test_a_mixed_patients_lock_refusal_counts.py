"""The identity lock's refusal over a patient part of which `anonymize()`
already wrote counts what it found, and says what the lock it advises
leaves out (#594, owner ruling Q5 A).

A patient is de-identified, and a study or a series of the same patient
is ingested afterwards. One instance then holds no Patient's Name and an
empty Accession Number, which the pass left, and the other holds both as
the source wrote them. `lock_identities()` refuses that patient, and it
still does. What it said was false: "this patient holds no value in
0010,0010, 0008,0050, which anonymize() emptied or removed, so there is
no original left to stash" is true of one instance of two (measured on
`main` at 067982ec, `arch-f/p_lock.py` S1 to S3), and the lock it advised
goes through while stashing those tags for no instance, so the new
study's name and Accession Number are in no token.

The refusal now counts, per instance: how many a pass left blank in a
named tag, of how many, and on how many such a tag still holds a value.
It says the advised lock stashes those tags for no instance. A patient
no instance of which still holds a value in a blanked tag gets the
sentence it had, byte for byte (`test_a_lock_that_stashes_nothing_is_
refused.py` and `test_a_relock_cannot_lose_a_held_identity.py` pin it
whole and are unchanged).

Nothing about what a token holds changes, and the sentence carries
counts and tags only: no name, ID, UID, Accession Number or date.

**Why this file imports what it does.** `isocenter.session` is named, so
its probe row is charged; `isocenter.entities` builds the hand-made
graphs.
"""
import ast
import re
from datetime import date

import numpy as np
import pydicom
import pytest

from isocenter.entities import Instance, Patient, Series, Study
from isocenter.session import DicomSession

from support.ct_small_files import study_uid, write_ct

PID, NAME = "MRN-594-ZETA", "Quixote^Hortensia"
BIRTH, STUDY_DATE = "19610917", "20190304"
ACC_OLD, ACC_NEW = "ACC-OLD-594", "ACC-NEW-594"
NAME_TAG, ID_TAG, ACC, OTHER = "0010,0010", "0010,0020", "0008,0050", "0010,1000"
BIRTH_TAG = "0010,0030"
SEQ = "0400,0500"
TAGS = [NAME_TAG, ID_TAG]
TAIL = "; the token this call would have written is unchanged."


def counted(blanked, left, total, holding, rest):
    """The refusal for a patient some of whose instances still hold a
    value in a tag a pass blanked on others."""
    one = len(blanked) == 1
    named = blanked[0] if one else "one or more of " + ", ".join(blanked)
    opening = (
        f"lock_identities: anonymize() emptied or removed {named} on {left} of "
        f"this patient's {total} instances, and "
        f"{'it' if one else 'one or more of them'} still holds a value on "
        f"{holding}, "
        + ("so the lock would stash an original for some instances and none for "
           "others. " if one else
           "so the lock would not stash an original in each of them for every "
           "instance. "))
    if not rest:
        return (opening + "tags_to_lock names no other tag, so there is nothing "
                "else to lock" + TAIL)
    return (opening + f"To lock this patient without {'it' if one else 'them'}, "
            f"call lock_identities(<its Patient ID>, tags_to_lock={rest!r}), which "
            f"stashes {'it' if one else 'them'} for no instance: a value "
            f"{'it still holds' if one else 'they still hold'} would be in no "
            "token, and could not be recovered if a later anonymize() empties or "
            "removes it there too" + TAIL)


def all_blank(blanked, rest):
    """The sentence a patient blank throughout has had since #574/#593:
    unchanged, to the byte."""
    advice = (f"To lock this patient without {'it' if len(blanked) == 1 else 'them'}, "
              f"call lock_identities(<its Patient ID>, tags_to_lock={rest!r})"
              if rest else
              "tags_to_lock names no other tag, so there is nothing else to lock")
    return (f"lock_identities: this patient holds no value in {', '.join(blanked)}, "
            "which anonymize() emptied or removed, so there is no original left to "
            f"stash. {advice}" + TAIL)


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _session(tmp_path):
    session = DicomSession(str(tmp_path / "s.db"))
    session.enable_reversible_anonymization(str(tmp_path / "k.key"))
    return session


def _instances(session):
    return [inst for p in session.store.patients for st in p.studies
            for se in st.series for inst in se.instances]


def _write(path, suffix, accession, same_study_as=None, name=True):
    """CT_small under PID with a birth date and a study date no other file
    here holds; `same_study_as` makes it a new series of that study, and
    `name=False` a file with no Patient's Name element at all."""
    write_ct(path, PID, suffix, study_date=STUDY_DATE, name=NAME, accession=accession)
    ds = pydicom.dcmread(str(path))
    ds.PatientBirthDate = BIRTH
    if not name:
        del ds.PatientName
    if same_study_as is not None:
        ds.StudyInstanceUID = study_uid(same_study_as)
        ds.SeriesInstanceUID = f"{study_uid(same_study_as)}.77"
        ds.SOPInstanceUID = f"{study_uid(same_study_as)}.77.1"
        ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    ds.save_as(str(path))


def _hand(session, specs, pid=PID):
    """One patient, one study per spec list, one series each; a spec is the
    instance's attributes beside its name and ID, and `"pass": [tags]`
    names the tags a pass emptied there (record first, then the write)."""
    patient = Patient(pid, NAME)
    made = []
    for n, study_specs in enumerate(specs, start=1):
        study = Study(f"ST_{pid}_{n}", date(2023, 1, n))
        series = Series(f"SE_{pid}_{n}", "CT", n)
        for k, spec in enumerate(study_specs):
            spec = dict(spec)
            emptied = spec.pop("pass", [])
            inst = Instance(f"SOP_{pid}_{n}_{k}", "1.2.840.10008.5.1.4.1.1.2", 1)
            inst.file_path = None
            inst.set_attr(NAME_TAG, NAME)
            inst.set_attr(ID_TAG, pid)
            for tag, value in spec.items():
                inst.set_attr(tag, value)
            for tag in emptied:
                inst.record_remediation(tag, "")
                inst.set_attr(tag, "")
            inst.set_pixel_data(np.zeros((8, 8), dtype=np.uint16))
            series.instances.append(inst)
            made.append(inst)
        study.series.append(series)
        patient.studies.append(study)
    session.store.patients.append(patient)
    return made


def _advised(message):
    """The `tags_to_lock` list the refusal advises, read out of its text."""
    [found] = re.findall(r"tags_to_lock=(\[[^\]]*\])\)", message)
    return ast.literal_eval(found)


# ---------------------------------------------------------------------------
# Through the public API: a pass, then more of the same patient
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("same_study", [None, "5941"], ids=["new_study", "new_series"])
def test_a_patient_ingested_again_after_a_pass_is_refused_in_counts(tmp_path, same_study):
    """S1 and S3 of the measurement: Patient's Name under REMOVE and the ID
    kept, one file de-identified, then a second file of the same patient
    (a new study, or a new series of the de-identified study). The refusal
    is asserted whole: three tags (name, birth date, Accession Number), 1
    of 2 instances blank, 1 still holding, and the advised list. Nothing is written. On `main` the sentence was the
    all-blank one, which is false of the second instance.

    Goes through `ingest -> audit -> anonymize -> ingest`, so the records
    the counts read are the ones a pass writes, not ones a test wrote."""
    _write(tmp_path / "a" / "a.dcm", "5941", ACC_OLD)
    _write(tmp_path / "b" / "b.dcm", "5942", ACC_NEW, same_study_as=same_study)
    with _session(tmp_path) as session:
        session.configuration.set_phi_tag(NAME_TAG, "REMOVE")
        session.configuration.set_phi_tag(ID_TAG, "KEEP")
        session.ingest(str(tmp_path / "a"))
        session.anonymize(session.audit())
        session.save(sync=True)
        session.ingest(str(tmp_path / "b"))
        [patient] = session.store.patients
        old, new = _instances(session)
        assert (old.attributes.get(NAME_TAG), old.attributes.get(ACC)) == (None, "")
        assert (new.attributes.get(NAME_TAG), new.attributes.get(ACC)) == (NAME, ACC_NEW)

        with pytest.raises(RuntimeError) as raised:
            session.lock_identities(PID)
        message = str(raised.value)
        # The floor empties the birth date these files carry, beside the
        # Accession Number; the rule set here removes the name.
        assert (old.attributes.get(BIRTH_TAG), new.attributes.get(BIRTH_TAG)) == ("", BIRTH)
        rest = [ID_TAG, "0010,0040"]
        assert message == counted([NAME_TAG, BIRTH_TAG, ACC], 1, 2, 1, rest)
        assert all(SEQ not in inst.sequences for inst in (old, new))

        # The text names no value (Q5 A): nothing from either file, nor
        # what the pass put in their place.
        uids = {u for inst in (old, new) for u in (
            inst.sop_instance_uid, inst.attributes.get("0020,000d"),
            inst.attributes.get("0020,000e"))}
        uids |= {st.study_instance_uid for st in patient.studies}
        uids |= {se.series_instance_uid for st in patient.studies for se in st.series}
        secrets = {PID, NAME, "Quixote", "Hortensia", BIRTH, STUDY_DATE, ACC_OLD,
                   ACC_NEW, study_uid("5941"), study_uid("5942"), "ACC-",
                   *(u for u in uids if u)}
        for dated in (old, new):
            secrets |= {str(v) for t, v in dated.attributes.items()
                        if t in ("0008,0020", "0008,0021", "0008,0022", "0008,0023",
                                 "0010,0030") and str(v or "").strip()}
        for secret in secrets:
            assert secret not in message, secret

        # The advised call, made as written: it locks, and it stashes the
        # dropped tags for no instance, as the sentence says. The second
        # file's name and Accession Number are in no token.
        result = session.lock_identities(PID, tags_to_lock=_advised(message))
        assert _advised(message) == rest
        assert len(result) == 2
        rs = session.reversibility_service
        for inst in (old, new):
            held = rs.recover_original_data(inst)
            assert sorted(held) == sorted(rest)
            assert held[ID_TAG] == PID
        assert (new.attributes[NAME_TAG], new.attributes[BIRTH_TAG],
                new.attributes[ACC]) == (NAME, BIRTH, ACC_NEW)


@pytest.mark.parametrize("beside", ["after_the_pass", "in_the_pass"])
@pytest.mark.parametrize("reopen", [False, True], ids=["same_session", "reopened"])
def test_a_file_that_never_carried_a_name_is_not_one_a_pass_emptied(
        tmp_path, beside, reopen):
    """Review of #1061, finding 1. Name under REMOVE, ID kept. File A goes
    through the pass, file B arrives afterwards with a name, and file C,
    of the same patient, has no `(0010,0010)` element at all; C arrives
    after the pass, or goes through it beside A. Only A's own record says
    a pass removed its name: 1 of 3, and 1 holding.

    The lock stashes a name an instance has no copy of from the patient,
    and asks every instance of the patient whether a pass wrote that
    value. That answers whether the patient is refused, and it is not the
    count: read for the count, it called C a pass's doing and said 2 of
    3. C has no record of its own in either arrangement, the pass having
    had no element of C's to remove. Kills the left count read through
    the patient-level vouch."""
    first, later = tmp_path / "a", tmp_path / "b"
    _write(first / "a.dcm", "5941", ACC_OLD)
    _write(later / "b.dcm", "5942", ACC_NEW)
    _write((later if beside == "after_the_pass" else first) / "c.dcm", "5943",
           "ACC-THIRD-594", name=False)
    session = _session(tmp_path)
    try:
        session.configuration.set_phi_tag(NAME_TAG, "REMOVE")
        session.configuration.set_phi_tag(ID_TAG, "KEEP")
        session.ingest(str(first))
        session.anonymize(session.audit())
        session.save(sync=True)
        session.ingest(str(later))
        if reopen:
            session.save(sync=True)
            session.close()
            session = _session(tmp_path)
        [patient] = session.store.patients
        made = _instances(session)
        assert sorted(str(inst.attributes.get(NAME_TAG)) for inst in made) == sorted(
            ["None", "None", NAME])
        assert sum(inst.remediation_vouches_for(NAME_TAG, None) for inst in made) == 1
        assert patient.patient_name is None
        with pytest.raises(RuntimeError) as raised:
            session.lock_identities(PID, tags_to_lock=TAGS)
        assert str(raised.value) == counted([NAME_TAG], 1, 3, 1, [ID_TAG])
        assert all(SEQ not in inst.sequences for inst in made)
    finally:
        session.close()


# ---------------------------------------------------------------------------
# What is counted
# ---------------------------------------------------------------------------

def test_instances_are_counted_not_value_sets(tmp_path):
    """Two instances a pass blanked share one value-set, and three still
    hold an Accession Number in two value-sets: 2 of 5 and 3, not 1 of 3
    and 2. Kills the counts taken per value-set."""
    with _session(tmp_path) as session:
        made = _hand(session, [[{ACC: ACC_OLD, "pass": [ACC]}] * 2,
                               [{ACC: ACC_NEW}] * 2, [{ACC: "ACC-THIRD"}]])
        with pytest.raises(RuntimeError) as raised:
            session.lock_identities(PID, tags_to_lock=TAGS + [ACC])
        assert str(raised.value) == counted([ACC], 2, 5, 3, TAGS)
        assert all(SEQ not in inst.sequences for inst in made)


def test_a_blank_no_pass_wrote_is_in_neither_count(tmp_path):
    """One instance a pass emptied, one whose source held the tag empty
    and one whose source never carried it (no record on either: nobody's
    doing), one holding a value. The source's blank shares the emptied
    instance's value-set and is still not called a pass's, and the
    instance without the tag is a value-set of its own that holds
    nothing: 1 of 4, and 1 holding. Kills the left count taken per
    value-set, and the holding count taken as "the rest"."""
    with _session(tmp_path) as session:
        _hand(session, [[{ACC: ACC_OLD, "pass": [ACC]}], [{ACC: ""}], [{}],
                        [{ACC: ACC_NEW}]])
        with pytest.raises(RuntimeError) as raised:
            session.lock_identities(PID, tags_to_lock=TAGS + [ACC])
        assert str(raised.value) == counted([ACC], 1, 4, 1, TAGS)


def test_a_patient_no_instance_of_which_holds_a_value_keeps_its_sentence(tmp_path):
    """The same patient without the instance that holds a value: a pass's
    blank beside a source's blank and a source without the tag. Nothing
    is left out by the advised lock, so the sentence is the one it was.
    Kills the counted sentence raised whenever some instance is not a
    pass's blank."""
    with _session(tmp_path) as session:
        _hand(session, [[{ACC: ACC_OLD, "pass": [ACC]}], [{ACC: ""}], [{}]])
        with pytest.raises(RuntimeError) as raised:
            session.lock_identities(PID, tags_to_lock=TAGS + [ACC])
        assert str(raised.value) == all_blank([ACC], TAGS)


def test_a_value_in_a_tag_no_pass_blanked_is_not_counted_as_holding(tmp_path):
    """One instance: its Accession Number emptied by a pass, its Other
    Patient IDs as the source wrote them. The advised lock keeps Other
    Patient IDs, so nothing is left out and the sentence is the one it
    was. Kills the holding count read over every tag `tags_to_lock`
    names rather than the blanked ones."""
    with _session(tmp_path) as session:
        _hand(session, [[{ACC: ACC_OLD, OTHER: "OTHER-594", "pass": [ACC]}]])
        with pytest.raises(RuntimeError) as raised:
            session.lock_identities(PID, tags_to_lock=TAGS + [ACC, OTHER])
        assert str(raised.value) == all_blank([ACC], TAGS + [OTHER])


def test_two_tags_blanked_on_different_instances_are_each_counted_once(tmp_path):
    """Instance 1 lost its Accession Number to a pass and holds Other
    Patient IDs; instance 2 the reverse. Each is one a pass reached and
    one that still holds a value, so both counts are 2 of 2, and the
    sentence says "one or more of" rather than that either instance
    holds neither. The advised list drops both."""
    with _session(tmp_path) as session:
        _hand(session, [[{ACC: ACC_OLD, OTHER: "OTHER-594", "pass": [ACC]}],
                        [{ACC: ACC_NEW, OTHER: "OTHER-595", "pass": [OTHER]}]])
        with pytest.raises(RuntimeError) as raised:
            session.lock_identities(PID, tags_to_lock=TAGS + [ACC, OTHER])
        assert str(raised.value) == counted([ACC, OTHER], 2, 2, 2, TAGS)
        for secret in (PID, NAME, ACC_OLD, ACC_NEW, "OTHER-594", "OTHER-595"):
            assert secret not in str(raised.value)


def test_a_mixed_patient_with_no_other_tag_to_lock_is_told_so(tmp_path):
    """`tags_to_lock` names only the tag a pass blanked on one instance:
    counted, and no list is advised because none is left."""
    with _session(tmp_path) as session:
        made = _hand(session, [[{ACC: ACC_OLD, "pass": [ACC]}], [{ACC: ACC_NEW}]])
        with pytest.raises(RuntimeError) as raised:
            session.lock_identities(PID, tags_to_lock=[ACC])
        assert str(raised.value) == counted([ACC], 1, 2, 1, [])
        assert all(SEQ not in inst.sequences for inst in made)


def test_two_blanked_tags_and_no_other_tag_to_lock_advise_no_list(tmp_path):
    """Review of #1061, finding 3. `tags_to_lock` names exactly the two
    tags a pass blanked, each on a different instance: counted, in the
    several-tag words, and the advice is that nothing else is left, never
    a call with `tags_to_lock=[]`. Kills the no-other-tag arm reached for
    one blanked tag only."""
    with _session(tmp_path) as session:
        made = _hand(session, [[{ACC: ACC_OLD, OTHER: "OTHER-594", "pass": [ACC]}],
                               [{ACC: ACC_NEW, OTHER: "OTHER-595", "pass": [OTHER]}]])
        with pytest.raises(RuntimeError) as raised:
            session.lock_identities(PID, tags_to_lock=[ACC, OTHER])
        assert str(raised.value) == counted([ACC, OTHER], 2, 2, 2, [])
        assert "tags_to_lock=" not in str(raised.value)
        assert all(SEQ not in inst.sequences for inst in made)


@pytest.mark.parametrize("removed_by_a_pass", [False, True],
                         ids=["never_carried", "copy_removed_by_a_pass"])
def test_a_name_the_lock_would_stash_from_the_patient_is_a_value_held(
        tmp_path, removed_by_a_pass):
    """One instance's name emptied by a pass, one with no copy of the name
    under a patient that still holds it. The lock stashes the patient's
    name for the second (#495), so that is an original the advised lock
    leaves out: 1 of 2, and 1 holding. `holding` reads what the lock
    would stash; only the first count reads the instance's own record.

    And where that copy is gone because an instance-only pass removed it
    (its record says so) while the patient kept the name, the instance is
    still not one of the first count: the lock would stash the name for
    it, so nothing was left blank there. Kills the first count read from
    the record without asking whether the lock has a value to stash."""
    with _session(tmp_path) as session:
        emptied, bare = _hand(session, [[{"pass": [NAME_TAG]}], [{}]])
        if removed_by_a_pass:
            bare.record_remediation(NAME_TAG, None)
        del bare.attributes[NAME_TAG]
        with pytest.raises(RuntimeError) as raised:
            session.lock_identities(PID, tags_to_lock=TAGS)
        assert str(raised.value) == counted([NAME_TAG], 1, 2, 1, [ID_TAG])
        assert NAME not in str(raised.value)


def test_a_batch_numbers_the_counted_refusal_and_locks_nobody(tmp_path):
    """The batch form raises the same sentence under its number, whole,
    and locks neither patient."""
    with _session(tmp_path) as session:
        a = _hand(session, [[{ACC: ACC_OLD}]], pid="MRN-594-ALPHA")
        b = _hand(session, [[{ACC: ACC_OLD, "pass": [ACC]}], [{ACC: ACC_NEW}] * 2],
                  pid="MRN-594-BETA")
        with pytest.raises(RuntimeError) as raised:
            session.lock_identities(["MRN-594-ALPHA", "MRN-594-BETA"],
                                    tags_to_lock=TAGS + [ACC])
        assert str(raised.value) == (
            "lock_identities: 1 of 2 patients cannot be locked as asked, so no "
            "patient was locked. Each is numbered by its place among the patients "
            "found, in Patient ID order. Lock the others without these, and each of "
            f"these as its message says:\n[2 of 2] {counted([ACC], 1, 3, 2, TAGS)}")
        assert all(SEQ not in inst.sequences for inst in a + b)
