"""A file with no Study or Series Instance UID does not join another patient's (#584).

Ingest spelled an absent Study Instance UID `UnknownStudy` and an absent
Series Instance UID `UnknownSeries`, and its study and series maps are
global. Measured at 63a64158 (`.agent/v1/L8-spec.md` §1.2): `PA` and `PB`,
both files without a Study Instance UID -- PB's instance was linked under
PA's study, PB was left with no studies, both files were exported under
PA's pseudonym and folder with `0020,000d` = `UnknownStudy` (not a valid
UI), and the run graded PASS. The same with the Series UID absent put PB's
instance in PA's series.

Now such a file gets a deterministic `2.25.` UID (`isocenter.uids`):
a study generated from the file's Series Instance UID (or, lacking that,
its SOP Instance UID), a series generated from its study's UID. Never from
the Patient ID: an unkeyed hash of an MRN in an exported UID lets anyone
confirm a guess. Ingest writes one `WARNING` row per instance, because a
Type 1 element the source lacked is exported with a value it never held.
"""
import sqlite3

import pydicom
import pytest

from isocenter import Session
from isocenter.uids import generated_uid, uid_from_bytes16

from support.ct_small_files import study_uid, write_ct

#: Computed once from the implementation and pasted (spec §8, T-B5): a
#: comparison of two calls in one process passes for a uuid4 mutant only
#: by the luck of the wording; a literal also pins the label and the byte
#: order.
STUDY_FROM_SERIES = {
    "5851": "2.25.73321126328501212774256048351770880361",
    "5852": "2.25.176140913639301721810449023557379761999",
}
STUDY_FROM_SOP = {
    "5851": "2.25.184602839944802793538825181283932367688",
    "5852": "2.25.303885996013008754309356541134245544731",
}
SERIES_FROM_STUDY = {
    "5851": "2.25.67054565810438131949980728583225817120",
    "5852": "2.25.55647523042884041383348055896892366762",
}


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _write(tmp_path, pid, suffix, drop):
    path = write_ct(tmp_path / "in" / f"{pid}.dcm", pid, suffix, name=f"Name^{pid}")
    ds = pydicom.dcmread(path)
    for keyword in drop:
        delattr(ds, keyword)
    ds.save_as(path)
    return str(ds.SOPInstanceUID)


def _warnings(session):
    session.store_backend.flush_audit_queue()
    with sqlite3.connect(session.persistence_file) as conn:
        return conn.execute(
            "SELECT entity_uid, details FROM audit_log WHERE action_type='WARNING'"
        ).fetchall()


CASES = {
    "no_study": (("StudyInstanceUID",),
                 lambda s: (STUDY_FROM_SERIES[s], f"{study_uid(s)}.1"),
                 "Study Instance UID"),
    "no_series": (("SeriesInstanceUID",),
                  lambda s: (study_uid(s), SERIES_FROM_STUDY[s]),
                  "Series Instance UID"),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_two_patients_without_a_uid_keep_their_own_study(tmp_path, case):
    """T-B5. Kills M-B8 (the global `UnknownStudy`/`UnknownSeries`), M-B9
    (anchored on the Patient ID: the pinned literals move) and M-B10 (the
    WARNING row removed)."""
    drop, expected, element = CASES[case]
    sops = {pid: _write(tmp_path, pid, suffix, drop)
            for pid, suffix in (("PA", "5851"), ("PB", "5852"))}
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        held = {p.patient_id: [(st.study_instance_uid, se.series_instance_uid,
                                [i.sop_instance_uid for i in se.instances])
                               for st in p.studies for se in st.series]
                for p in session.store.patients}
        session.anonymize(session.audit())
        session.export(str(tmp_path / "out"), use_compression=False)
        warnings = _warnings(session)

    assert held == {
        "PA": [(*expected("5851"), [sops["PA"]])],
        "PB": [(*expected("5852"), [sops["PB"]])],
    }, held

    exported = {str(ds.SOPInstanceUID): ds for ds in
                (pydicom.dcmread(str(p)) for p in (tmp_path / "out").rglob("*.dcm"))}
    for pid, suffix in (("PA", "5851"), ("PB", "5852")):
        ds = exported[sops[pid]]
        assert (str(ds.StudyInstanceUID), str(ds.SeriesInstanceUID)) == expected(suffix)
    assert (exported[sops["PA"]].StudyInstanceUID
            != exported[sops["PB"]].StudyInstanceUID)

    generated = sorted((uid, details) for uid, details in warnings
                       if element in details)
    assert [uid for uid, _ in generated] == sorted(sops.values()), warnings
    for uid, details in generated:
        assert uid in details
        assert "absent" in details and "generated" in details, details
        # The row names the instance, never a value.
        assert "PA" not in details.split(uid)[1] and "Name^" not in details


def test_a_file_with_neither_uid_is_anchored_on_its_sop_uid(tmp_path):
    """The 8 corpus files (HTJ2K, JLSL, ...) carry neither. The study is
    generated from the SOP Instance UID, its series from that study."""
    sop = _write(tmp_path, "PA", "5851", ("StudyInstanceUID", "SeriesInstanceUID"))
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        [patient] = session.store.patients
        [study] = patient.studies
        [series] = study.series
        warnings = _warnings(session)
    assert study.study_instance_uid == STUDY_FROM_SOP["5851"] == generated_uid("study", sop)
    assert series.series_instance_uid == generated_uid("series", STUDY_FROM_SOP["5851"])
    assert len([w for w in warnings if w[0] == sop]) == 2, warnings


def test_the_uid_carries_version_8_and_the_rfc_variant():
    """Kills M-B15 (pydicom's `generate_uid(prefix=None, ...)`, which
    ignores its entropy sources) and M-B16 (the masking dropped)."""
    uid = uid_from_bytes16(bytes(16))
    assert uid == "2.25.604472133179351442128896"
    value = int(uid[len("2.25."):])
    assert (value >> 76) & 0xF == 8
    assert (value >> 62) & 0x3 == 0b10
    top = uid_from_bytes16(b"\xff" * 16)
    assert len(top) == 44 and (int(top[5:]) >> 76) & 0xF == 8
    # All ones: the variant's low bit must be cleared, not only its high
    # bit set (a zero input cannot tell those apart).
    assert (int(top[5:]) >> 62) & 0x3 == 0b10
    # Asymmetric input pins the byte order: big-endian, as RFC 9562 reads
    # a UUID.
    assert uid_from_bytes16(bytes(range(16))) == "2.25.5233100606847278184134747173490191"
    assert generated_uid("study", "1.2.3") == generated_uid("study", "1.2.3")
    with pytest.raises(ValueError):
        uid_from_bytes16(bytes(15))
