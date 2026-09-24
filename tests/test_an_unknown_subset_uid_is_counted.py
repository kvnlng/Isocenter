"""`export(subset=...)` reads its argument the way `patient_ids` does (#725).

Measured at 733f8531, identically on 3.12 and 3.14t:
`export(subset=["NOPE"])` wrote nothing, logged nothing and wrote no row
but the `EXPORT` one, and the report graded `PASS`; `subset=(A,)` raised
`TypeError` where `patient_ids=(A,)` is accepted; `subset=[42]` exported
nothing in silence; and a DataFrame carrying none of the four UID columns
selected nothing, in silence. The same classes #686 and #696 closed for
`patient_ids`, on the other selection `export()` takes.

Owner ruling on #725 (option A): a subset element is unknown when it names
nothing in the session at *any* of the four levels the walk matches
(`_uid_path`: Patient ID, Study, Series or SOP Instance UID), a #544
replacement included. It has no level to report, because the caller named
none. It is counted by position, never named: one `WARNING` log line and one
`WARNING` audit row, at the point the #686 row is written and only when the
export runs, so the report grades `REVIEW_REQUIRED`. The shape is read by
`normalize_id_filter`, before the pre-export scan and the flush.
"""
import logging

import pandas as pd
import pydicom
import pytest

from isocenter.session import DicomSession

from support.project_secret import load_fixed_secret
from test_an_unmatched_patient_id_is_counted import A, B, _grade, _rows, _session
from test_ingest_finds_what_anonymize_renamed import STUDY, M, _cfg, _only
from test_uids_are_replaced_by_the_project_secret import _ct

SENTENCE = "nothing in the session matches"
UNKNOWN_1, UNKNOWN_2 = "1.2.826.0.1.725.1", "1.2.826.0.1.725.2"


def _count_rows(rows):
    return [row for row in rows if SENTENCE in (row[2] or "")]


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records
            if r.levelno == logging.WARNING and SENTENCE in r.getMessage()]


def _uids(session, patient_id):
    """The four `_uid_path` UIDs of `patient_id`'s one instance."""
    (patient,) = [p for p in session.store.patients if p.patient_id == patient_id]
    (study,) = patient.studies
    (series,) = study.series
    (instance,) = series.instances
    return (patient.patient_id, study.study_instance_uid,
            series.series_instance_uid, instance.sop_instance_uid)


def _revisions(session):
    """Every entity's revision: the pre-export scan records a PHI status
    on each one it reads, which advances it, so an unmoved revision says
    the scan never ran."""
    return [entity._revision for p in session.store.patients
            for entity in (p, *p.studies,
                           *(i for st in p.studies for se in st.series
                             for i in se.instances))]


def _written_patients(session, summary):
    owner = {inst.sop_instance_uid: p.patient_id
             for p in session.store.patients for st in p.studies
             for se in st.series for inst in se.instances}
    return {owner[uid] for uid in summary.written_uids}


def _export(session, folder, subset, **options):
    summary = session.export(str(folder), subset=subset, show_progress=False,
                             use_compression=False, **options)
    return _written_patients(session, summary)


# --- S1: every iterable of UIDs is a UID list, as `patient_ids` takes ----------------

@pytest.mark.parametrize("shape", ["list", "tuple", "set", "frozenset",
                                   "generator", "series"])
def test_every_iterable_shape_selects(tmp_path, shape):
    """Only a `list` was taken until #725; a tuple, which `patient_ids`
    accepts, raised `TypeError`. A generator is read once, before the walk."""
    with _session(tmp_path, shape) as session:
        sop = _uids(session, A)[3]
        subset = {"list": lambda: [sop], "tuple": lambda: (sop,),
                  "set": lambda: {sop}, "frozenset": lambda: frozenset([sop]),
                  "generator": lambda: (u for u in [sop]),
                  "series": lambda: pd.Series([sop])}[shape]()
        assert _export(session, tmp_path / "out", subset) == {A}


# --- S2: a shape error is refused before anything is read or written ----------------

@pytest.mark.parametrize("subset, says", [
    (42, "a query str, a DataFrame, or an iterable of UIDs; got int"),
    (b"1.2.3", "bytes-like"),
    (bytearray(b"1.2.3"), "bytes-like"),
    ([42], "holds a int at position 1"),
    (["1.2.3", None], "holds a NoneType at position 2"),
    ([b"1.2.3"], "holds a bytes at position 1"),
])
def test_a_wrong_shape_is_refused_before_the_scan(tmp_path, subset, says):
    """`[42]` and `[None]` exported nothing in silence until #725. The
    refusal lands before `check_burned_in`'s `audit()` and the flush:
    no audit row of any kind is written for an export that did not run."""
    with _session(tmp_path, "shape") as session:
        before, revisions = len(_rows(session)), _revisions(session)
        with pytest.raises(TypeError) as excinfo:
            session.export(str(tmp_path / "out"), subset=subset,
                           check_burned_in=True, show_progress=False)
        added = _rows(session)[before:]
    assert "subset" in str(excinfo.value), excinfo.value
    assert says in str(excinfo.value), excinfo.value
    assert added == [], added
    assert _revisions(session) == revisions, "the pre-export scan ran"
    assert not (tmp_path / "out").exists()


def test_a_broken_query_is_refused_before_the_scan(tmp_path):
    """The query arm resolves at the top too: its `ValueError` used to
    arrive after the pre-export scan had written its rows."""
    with _session(tmp_path, "query") as session:
        before, revisions = len(_rows(session)), _revisions(session)
        with pytest.raises(ValueError, match="could not be run"):
            session.export(str(tmp_path / "out"), subset="NoSuchColumn == 'x'",
                           check_burned_in=True, show_progress=False)
        added = _rows(session)[before:]
    assert added == [], added
    assert _revisions(session) == revisions, "the pre-export scan ran"


def test_a_dataframe_with_no_uid_column_is_refused(tmp_path):
    """A frame carrying none of the four columns can select nothing, and
    selected nothing in silence. Refused from the argument alone, naming
    the columns it reads, before any row is written (owner, on #725)."""
    with _session(tmp_path, "cols") as session:
        before, revisions = len(_rows(session)), _revisions(session)
        with pytest.raises(ValueError) as excinfo:
            session.export(str(tmp_path / "out"),
                           subset=pd.DataFrame({"Modality": ["ECG"]}),
                           check_burned_in=True, show_progress=False)
        added = _rows(session)[before:]
    for column in ("SOPInstanceUID", "SeriesInstanceUID",
                   "StudyInstanceUID", "PatientID"):
        assert column in str(excinfo.value), excinfo.value
    assert "subset" in str(excinfo.value), excinfo.value
    assert added == [], added
    assert _revisions(session) == revisions, "the pre-export scan ran"


def test_an_empty_frame_with_a_uid_column_is_an_empty_selection(tmp_path, caplog):
    """The column refusal reads columns, not rows: a filter that kept no
    row is a selection of nothing, like `[]`."""
    with _session(tmp_path, "emptyframe") as session:
        frame = session.get_cohort_report().iloc[0:0]
        before = len(_rows(session))
        with caplog.at_level(logging.WARNING, logger="isocenter"):
            assert _export(session, tmp_path / "out", frame) == set()
        added = _rows(session)[before:]
    assert _count_rows(added) == [] and _warnings(caplog) == [], added
    assert [row[0] for row in added] == ["EXPORT"], added


# --- S3: an unknown UID is counted, by position, never named ------------------------

def test_an_unknown_uid_is_counted_and_the_rest_exported(tmp_path, caplog):
    folder = tmp_path / "out"
    with _session(tmp_path, "mixed") as session:
        sop = _uids(session, A)[3]
        before = len(_rows(session))
        with caplog.at_level(logging.WARNING, logger="isocenter"):
            written = _export(session, folder, (UNKNOWN_1, sop, UNKNOWN_2))
        added = _rows(session)[before:]
        grade = _grade(session, tmp_path)
    assert written == {A}, "the UID that matched was not exported (best-effort)"
    counted = _count_rows(added)
    assert len(counted) == 1, added
    action, entity_uid, details = counted[0]
    assert action == "WARNING" and entity_uid == str(folder), counted[0]
    assert details.startswith(f"DICOM export to {folder}: subset: "), details
    assert "2 of the 3 UIDs given" in details, details
    assert "(positions 1, 3, in the order given)" in details, details
    (message,) = _warnings(caplog)
    assert message == details.split(": ", 1)[1], (message, details)
    for identifier in (UNKNOWN_1, UNKNOWN_2, sop):
        assert identifier not in details and identifier not in message
    exports = [row for row in added if row[0] == "EXPORT"]
    assert len(exports) == 1 and SENTENCE not in exports[0][2], exports
    assert grade == "REVIEW_REQUIRED", grade


def test_an_export_of_only_unknown_uids_says_why_it_wrote_nothing(tmp_path, caplog):
    """The issue's own call. It planned nothing, and still says why.
    Also pins the singular form."""
    with _session(tmp_path, "nope") as session:
        before = len(_rows(session))
        with caplog.at_level(logging.WARNING, logger="isocenter"):
            written = _export(session, tmp_path / "out", ["NOPE"])
        added = _rows(session)[before:]
        grade = _grade(session, tmp_path)
    assert written == set()
    (row,) = _count_rows(added)
    assert "1 of the 1 UID given (position 1, in the order given); it " \
           "selects nothing" in row[2], row[2]
    assert "NOPE" not in row[2]
    assert len(_warnings(caplog)) == 1, caplog.messages
    assert grade == "REVIEW_REQUIRED", grade


@pytest.mark.parametrize("level", [0, 1, 2, 3],
                         ids=["patient", "study", "series", "instance"])
def test_a_uid_known_at_any_level_is_not_counted(tmp_path, level, caplog):
    """The walk matches each element at all four levels, so each level
    makes an element known -- one that read only instances would count a
    study UID the export then wrote."""
    with _session(tmp_path, f"lvl{level}") as session:
        uid = _uids(session, B)[level]
        before = len(_rows(session))
        with caplog.at_level(logging.WARNING, logger="isocenter"):
            written = _export(session, tmp_path / "out", [uid])
        added = _rows(session)[before:]
        grade = _grade(session, tmp_path)
    assert written == {B}
    assert _count_rows(added) == [] and _warnings(caplog) == [], added
    assert "**PASS**" in grade, grade


def test_a_uid_outside_patient_ids_is_not_counted(tmp_path, caplog):
    """Counted against the session, not the export (option B rejected):
    two filters that intersect to nothing are the caller's conjunction,
    and `patient_ids` counts against the session too."""
    with _session(tmp_path, "conj") as session:
        study_b = _uids(session, B)[1]
        before = len(_rows(session))
        with caplog.at_level(logging.WARNING, logger="isocenter"):
            summary = session.export(str(tmp_path / "out"), patient_ids=[A],
                                     subset=[study_b], show_progress=False)
        added = _rows(session)[before:]
    assert summary.written == 0
    assert _count_rows(added) == [] and _warnings(caplog) == [], added


def test_an_empty_selection_is_not_counted(tmp_path, caplog):
    """`()` selected nothing and said so in the `EXPORT` row; nothing was
    unknown, so nothing is counted -- `patient_ids=[]` reads the same."""
    with _session(tmp_path, "empty") as session:
        before = len(_rows(session))
        with caplog.at_level(logging.WARNING, logger="isocenter"):
            assert _export(session, tmp_path / "out", ()) == set()
        added = _rows(session)[before:]
    assert _count_rows(added) == [] and _warnings(caplog) == [], added
    assert [row[0] for row in added] == ["EXPORT"], added


def test_a_query_matching_no_row_is_not_counted(tmp_path, caplog):
    """A valid query that kept no row selected nothing; it named no UID,
    so there is nothing unknown to count (owner's lean on #725)."""
    with _session(tmp_path, "q0") as session:
        before = len(_rows(session))
        with caplog.at_level(logging.WARNING, logger="isocenter"):
            assert _export(session, tmp_path / "out",
                           "PatientID == 'NOBODY'") == set()
        added = _rows(session)[before:]
    assert _count_rows(added) == [] and _warnings(caplog) == [], added


def test_a_dataframe_is_counted_by_row(tmp_path, caplog):
    """A frame from another store names UIDs this one lacks. Its column
    is counted by row, the first of the four columns present, as the walk
    reads it."""
    with _session(tmp_path, "frame") as session:
        sop = _uids(session, A)[3]
        frame = pd.DataFrame({"SOPInstanceUID": [sop, UNKNOWN_1],
                              "PatientID": [A, A]})
        before = len(_rows(session))
        with caplog.at_level(logging.WARNING, logger="isocenter"):
            written = _export(session, tmp_path / "out", frame)
        added = _rows(session)[before:]
    assert written == {A}
    (row,) = _count_rows(added)
    assert "1 of the 2 UIDs given (position 2," in row[2], row[2]


def test_positions_are_capped_at_ten(tmp_path, caplog):
    unknown = [f"1.2.826.0.1.725.{n}" for n in range(12)]
    with _session(tmp_path, "cap") as session:
        sop = _uids(session, A)[3]
        with caplog.at_level(logging.WARNING, logger="isocenter"):
            _export(session, tmp_path / "out", [sop] + unknown)
    (message,) = _warnings(caplog)
    assert "12 of the 13 UIDs given" in message, message
    assert "(positions 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, and 2 more," in message


# --- S4: a UID from before anonymize() names its replacement; a Patient ID does not

def test_a_source_uid_after_anonymize_is_not_counted(tmp_path, caplog):
    """#544: a Study UID taken before the pass names its replacement, so
    it is known. The source Patient ID is not: the pseudonym is keyed, not
    a UID replacement, and the sentence says so."""
    _ct(tmp_path / "s1.dcm", "1.2.3.99.40")
    with DicomSession(str(tmp_path / "s.db")) as session:
        load_fixed_secret(session)
        session.load_config(_cfg(tmp_path))
        session.ingest(str(_only(tmp_path / "src", tmp_path / "s1.dcm")))
        session.anonymize()
        session.save(sync=True)
        before = len(_rows(session))
        with caplog.at_level(logging.WARNING, logger="isocenter"):
            session.export(str(tmp_path / "study"), subset=(STUDY,),
                           use_compression=False, show_progress=False)
        known = _rows(session)[before:]
        with caplog.at_level(logging.WARNING, logger="isocenter"):
            session.export(str(tmp_path / "pid"), subset=["L10-P1"],
                           use_compression=False, show_progress=False)
        rows = _rows(session)
    assert [pydicom.dcmread(p).StudyInstanceUID
            for p in (tmp_path / "study").rglob("*.dcm")] == [M(STUDY)]
    assert _count_rows(known) == [], known
    (row,) = _count_rows(rows)
    assert "taken before anonymize() or redact() still names its entity" in row[2], row[2]
    assert "replacement Patient ID" in row[2], row[2]
    assert "L10-P1" not in row[2]



# --- S5: a SOP UID taken before redact() names the redacted instance ----------------

def _redacting_session(tmp_path, name):
    """One CT under a redaction zone, in a store with the fixed secret, so
    `anonymize()` replaces its UIDs and `redact()` re-derives its SOP
    Instance UID (`services._redacted_uid_for`)."""
    from test_a_redacted_uid_is_derived_not_drawn import _config, _write_ct

    src = tmp_path / f"src_{name}"
    src.mkdir()
    _write_ct(src / "a.dcm")
    session = DicomSession(str(tmp_path / f"{name}.db"))
    load_fixed_secret(session)
    session.ingest(str(src))
    session.load_config(_config(tmp_path))
    return session


def _the_instance(session):
    return session.store.patients[0].studies[0].series[0].instances[0]


@pytest.mark.parametrize("taken", ["before-anonymize", "between-the-passes"])
def test_a_report_taken_before_redact_still_selects(tmp_path, taken, caplog):
    """The documented order is anonymize, redact, export. A cohort report
    taken at examine time holds the source SOP UID; one taken between
    the passes holds the anonymize-time replacement. `redact()` derives a
    third UID from the source, so both named nothing and the export wrote
    nothing -- in silence before #725, and under a count row that called
    the whole cohort unknown after it (review of #780). Both now name the
    instance through its recorded source UID."""
    with _redacting_session(tmp_path, taken) as session:
        if taken == "before-anonymize":
            report = session.get_cohort_report(expand_metadata=True)
        session.audit()
        session.anonymize()
        if taken == "between-the-passes":
            report = session.get_cohort_report(expand_metadata=True)
        taken_uid = report["SOPInstanceUID"].tolist()
        session.redact(show_progress=False)
        instance = _the_instance(session)
        assert instance.sop_instance_uid not in taken_uid, (
            "the fixture did not move the SOP UID; the test would not test it")
        before = len(_rows(session))
        with caplog.at_level(logging.WARNING, logger="isocenter"):
            summary = session.export(str(tmp_path / "out"), subset=report,
                                     use_compression=False, show_progress=False)
        added = _rows(session)[before:]
        current = instance.sop_instance_uid
    assert list(summary.written_uids) == [current]
    assert _count_rows(added) == [] and _warnings(caplog) == [], added


def test_a_sop_uid_from_another_store_still_counts_after_redact(tmp_path):
    """The source map widens what a value names, never what counts as held:
    a UID no instance here was ever ingested under is still unknown."""
    with _redacting_session(tmp_path, "foreign") as session:
        session.audit()
        session.anonymize()
        session.redact(show_progress=False)
        before = len(_rows(session))
        summary = session.export(str(tmp_path / "out"),
                                 subset=[UNKNOWN_1, "1.2.3.99.30"],
                                 use_compression=False, show_progress=False)
        added = _rows(session)[before:]
    assert summary.written == 1
    (row,) = _count_rows(added)
    assert "1 of the 2 UIDs given (position 1," in row[2], row[2]


def test_a_moved_uid_in_a_store_with_no_secret_still_selects(tmp_path, caplog):
    """`Instance.regenerate_uid()` is documented surface, and a caller can
    move a UID in a store that has replaced nothing, so no replacement
    covers the source: the recorded source UID alone must name it."""
    with _session(tmp_path, "nosecret") as session:
        assert not session.store_backend._project_secret_if_present(), (
            "the fixture has a secret; the test would not test the source arm")
        instance = session.store.patients[0].studies[0].series[0].instances[0]
        source = instance.sop_instance_uid
        instance.regenerate_uid("1.2.826.0.1.725.99")
        before = len(_rows(session))
        with caplog.at_level(logging.WARNING, logger="isocenter"):
            summary = session.export(str(tmp_path / "out"), subset=[source],
                                     use_compression=False, show_progress=False)
        added = _rows(session)[before:]
    assert list(summary.written_uids) == ["1.2.826.0.1.725.99"]
    assert _count_rows(added) == [] and _warnings(caplog) == [], added


def test_a_uid_between_two_regenerations_names_nothing_and_is_counted(tmp_path, caplog):
    """The documented limit, generalised (review nit on #780, for #26).

    Only the first move of a SOP Instance UID is recorded
    (`_take_sop_uid`), so an instance is found by the UID it was ingested
    under, by that UID's `anonymize()` replacement, and by its current
    one. A UID it held in between is none of those: two
    `Instance.regenerate_uid()` calls lose the middle one exactly as a
    `force=True` second redaction loses the first redaction's. The page
    says so; this pins the outcome it states -- counted, never named --
    beside the source UID, which still selects.
    """
    with _session(tmp_path, "twice") as session:
        instance = session.store.patients[0].studies[0].series[0].instances[0]
        source = instance.sop_instance_uid
        instance.regenerate_uid("1.2.826.0.1.725.97")
        instance.regenerate_uid("1.2.826.0.1.725.98")
        before = len(_rows(session))
        with caplog.at_level(logging.WARNING, logger="isocenter"):
            summary = session.export(str(tmp_path / "out"),
                                     subset=["1.2.826.0.1.725.97", source],
                                     use_compression=False, show_progress=False)
        added = _rows(session)[before:]
    assert list(summary.written_uids) == ["1.2.826.0.1.725.98"]
    (row,) = _count_rows(added)
    assert "1 of the 2 UIDs given (position 1," in row[2], row[2]
    assert "1.2.826.0.1.725.97" not in row[2], "a counted UID is never named"
    assert len(_warnings(caplog)) == 1, _warnings(caplog)
