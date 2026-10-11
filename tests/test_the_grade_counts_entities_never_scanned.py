"""A store that has been audited does not grade PASS over data no scan has read (#1064).

Condition 9 of the grade (`docs/analytics.md`, "How the grade is
decided"). Measured on main at 7325ff3a, on 3.12 and 3.14t alike: file A is
ingested, audited, anonymized and saved; file B is ingested afterwards;
`export()` writes B as ingested and `generate_report()` grades the run
PASS. With B a new series of A's study the file even looks de-identified,
because the export stamps the pseudonym and the replaced Patient ID from
the owners, while its Accession Number, birth date, institution and
physician are the source's.

The rule (owner rulings of 2026-10-11): in a store that holds any recorded
PHI status, a patient, study or instance on which none was ever recorded
grades REVIEW_REQUIRED. Ruling Q3 on #573 stands for a store nobody ever
audited: `ingest` -> `export` with no `audit()` still grades PASS, and
section 5 says how many instances no scan has read.

"Holds a recorded status" is read from the raw record of every patient,
study and instance, a stale one included, because that is what the store
keeps: a status an edit left behind is stored (`phi_status_edited`, #767)
and comes back on a reopen. A Series is never read, for the gate or for
the count. In memory a pass leaves a raw status on one; the store has no
column for it, so a rule that read it would grade a store differently
from its own reopened self.

Every shape of the measurement table in the CHANGELOG entry is a row of
`SHAPES`, run through the public API with the report read from its file.
"""
import re

import pydicom
import pytest
from pydicom.data import get_testdata_file
from pydicom.dataset import Dataset

import isocenter.reporting as reporting_module
import isocenter.session as session_module
from isocenter import Session
from isocenter.entities import PhiStatus
from isocenter.io_handlers import DicomExporter

ROOT = "1.2.826.0.1.3680043.10.7778"
PID_A, NAME_A = "MRN-G-KAPPAQ", "Zyxwvut^Qponmlk"
PID_B, NAME_B = "MRN-G-OMEGAZ", "Hjklmnb^Vcxzasd"
ACC_A, ACC_B = "ACCQ-ALPHA-11", "ACCQ-BETA-22"

A = dict(study="1", series="1", accession=ACC_A)
B_STUDY = dict(study="2", series="1", accession=ACC_B)
B_SERIES = dict(study="1", series="9", accession=ACC_B)
B_PATIENT = dict(study="5", series="1", accession=ACC_B, pid=PID_B, name=NAME_B)

#: Every identifier a file written by `_write` carries.
IDENTIFIERS = (PID_A, "Zyxwvut", PID_B, "Hjklmnb", ACC_A, ACC_B, "19610917", "20190304",
               "Xanthippe", "Oberholtzer", "STID-88231", ROOT)

NONE_CONFIG = 'version: "2.0"\nprivacy_profile: none\nremove_private_tags: false\n'
KEEP_CONFIG = (NONE_CONFIG + 'phi_tags:\n  "0010,0010": {action: KEEP}\n'
               '  "0010,0020": {action: KEEP}\n  "0008,0020": {action: KEEP}\n')

CT, ECG = "CT_small.dcm", "waveform_ecg.dcm"


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _write(folder, fname, *, study, series, accession, pid=PID_A, name=NAME_A,
           src=CT, nested=False):
    ds = pydicom.dcmread(get_testdata_file(src))
    ds.PatientID = pid
    ds.PatientName = name
    ds.PatientBirthDate = "19610917"
    ds.StudyDate = "20190304"
    ds.AccessionNumber = accession
    ds.InstitutionName = "Xanthippe Infirmary"
    ds.ReferringPhysicianName = "Oberholtzer^Wim"
    ds.StudyID = "STID-88231"
    ds.StudyInstanceUID = f"{ROOT}.{study}"
    ds.SeriesInstanceUID = f"{ROOT}.{study}.{series}"
    ds.SOPInstanceUID = f"{ROOT}.{study}.{series}.1"
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    if nested:
        item = Dataset()
        item.AccessionNumber = accession
        item.ReferencedSOPInstanceUID = f"{ROOT}.{study}.77"
        ds.RequestAttributesSequence = [item]
    if "WaveformSequence" in ds:
        # One multiplex group: the bundled file's second is discarded at
        # export with a SIGNAL loss row, which grades by itself (condition
        # 3) and would hide the question asked here.
        ds.WaveformSequence = ds.WaveformSequence[:1]
    folder.mkdir(parents=True, exist_ok=True)
    ds.save_as(str(folder / fname))


def _session(tmp_path, config=None):
    session = Session(str(tmp_path / "s.db"))
    if config:
        path = tmp_path / "config.yaml"
        if not path.exists():
            path.write_text(config, encoding="utf-8")
        session.load_config(str(path))
    return session


def _report(session, tmp_path, name="report.md"):
    """`(grade, reasons, PHI Scan line)`, read from the report file."""
    path = tmp_path / name
    session.generate_report(str(path))
    text = path.read_text(encoding="utf-8")
    [grade] = re.findall(r"\*\*Validation Status\*\* \| \*\*(\w+)\*\*", text)
    section_5 = text.split("## 5. Validation & Verification", 1)[1]
    basis = section_5.split("*   **Metadata Remediation:**", 1)[0]
    reasons = re.findall(r"^    \*   (.*)$", basis, flags=re.M)
    assert ("**Grade Basis:** PASS" in basis) == (grade == "PASS"), basis
    assert bool(reasons) == (grade == "REVIEW_REQUIRED"), basis
    [scan] = [l for l in section_5.splitlines() if "**PHI Scan (`audit()`):**" in l]
    return grade, reasons, scan


def _never(patients, studies, instances):
    """Condition 9's Grade Basis line for these counts."""
    n = patients + studies + instances
    noun = "entity" if n == 1 else "entities"
    return (f"{n} {noun} never scanned, in a store that holds a recorded PHI "
            "status: no PHI status is recorded on it, by a scan or by a pass, "
            "so nothing says what the policy names was looked for in it; "
            f"`audit()` reads it (patients {patients}, studies {studies}, "
            f"instances {instances})")


def _is_line_9(reason):
    return "never scanned" in reason


def _is_line_7(reason):
    return "read IDENTIFIED" in reason


def _is_line_8(reason):
    return "edited after the last PHI scan" in reason


def _exported(folder):
    return [pydicom.dcmread(str(p)) for p in sorted(folder.rglob("*.dcm"))]


def _values(ds):
    for elem in ds:
        if elem.VR == "SQ":
            for item in elem.value:
                yield from _values(item)
        elif elem.VR not in ("OB", "OW", "UN", "OF"):
            yield str(elem.value)


def _originals(ds):
    """Which of `IDENTIFIERS` an exported file still holds."""
    values = list(_values(ds))
    return sorted(o for o in IDENTIFIERS if any(o in v for v in values))


def _instances(session):
    return [i for p in session.store.patients for st in p.studies
            for se in st.series for i in se.instances]


def _run(tmp_path, first, later, *, config=None, first_pass=True, after=None,
         reopen=False, export="dicom", subset_a=False, patient_a=False,
         report_first=False, src=CT):
    """One row of the table. `first` is ingested and, with `first_pass`,
    passed and saved; `later` is ingested after it; `after` is None,
    "audit" or "pass". Returns the reports taken, in order."""
    for n, kw in enumerate(first):
        _write(tmp_path / "a", f"a{n}.dcm", src=src, **kw)
    for n, kw in enumerate(later):
        _write(tmp_path / "b", f"b{n}.dcm", src=src, **kw)
    reports = []
    session = _session(tmp_path, config)
    try:
        session.ingest(str(tmp_path / "a"))
        if first_pass:
            session.anonymize(session.audit())
            session.save(sync=True)
        a_uids = [i.sop_instance_uid for i in _instances(session)]
        a_pids = [p.patient_id for p in session.store.patients]
        if later:
            session.ingest(str(tmp_path / "b"))
        if after == "audit":
            session.audit()
        elif after == "pass":
            session.anonymize(session.audit())
        if reopen:
            session.save(sync=True)
            session.close()
            session = _session(tmp_path, config)
        if report_first:
            reports.append(_report(session, tmp_path, "before.md"))
        out = tmp_path / "out"
        if export == "tree":
            for patient in session.store.patients:
                DicomExporter.write_tree(patient, str(out), show_progress=False)
        elif export:
            kwargs = {} if export == "wfdb" else {"use_compression": False}
            if subset_a:
                kwargs["subset"] = a_uids
            if patient_a:
                kwargs["patient_ids"] = a_pids
            session.export(str(out), format=export, **kwargs)
        reports.append(_report(session, tmp_path))
        return reports
    finally:
        session.close()


PASS, RR = "PASS", "REVIEW_REQUIRED"

#: id -> (`_run` arguments, the grade of each report taken, condition 9's
#: counts or None). The ids are the measurement table's, in the CHANGELOG
#: entry for #1064. Where the counts are given and the grade is
#: REVIEW_REQUIRED, line 9 is the ONLY reason unless `also` names another.
SHAPES = {
    # A passed and saved, B ingested after it, exported with no scan since.
    "r1-new-study": (dict(first=[A], later=[B_STUDY]), [RR], (1, 1, 1)),
    # `patients 1` here is #1065's empty patient, not a designed count:
    # see `test_a_late_series_of_a_passed_study_is_counted`. It moves to
    # `(0, 0, 1)` when #1065 is fixed.
    "r2-new-series": (dict(first=[A], later=[B_SERIES]), [RR], (1, 0, 1)),
    "e01-new-patient": (dict(first=[A], later=[B_PATIENT]), [RR], (1, 1, 1)),
    "e06-profile-none": (dict(first=[A], later=[B_STUDY], config=NONE_CONFIG),
                         [RR], (1, 1, 1)),
    "e07-reopened": (dict(first=[A], later=[B_STUDY], reopen=True), [RR], (1, 1, 1)),
    "e08-subset-leaves-b-out": (dict(first=[A], later=[B_STUDY], subset_a=True),
                                [RR], (1, 1, 1)),
    "e09-patient-ids-leave-b-out": (dict(first=[A], later=[B_PATIENT], patient_a=True),
                                    [RR], (1, 1, 1)),
    "e10-wfdb": (dict(first=[A], later=[B_PATIENT], export="wfdb", src=ECG),
                 [RR], (1, 1, 1)),
    "e12-write-tree": (dict(first=[A], later=[B_STUDY], export="tree"), [RR], (1, 1, 1)),
    "e13-report-before-export": (dict(first=[A], later=[B_STUDY], report_first=True),
                                 [RR, RR], (1, 1, 1)),
    "e17-no-export": (dict(first=[A], later=[B_STUDY], export=None), [RR], (1, 1, 1)),
    # The remedy.
    "r3-then-audit-only": (dict(first=[A], later=[B_STUDY], after="audit"), [RR], None),
    "r4-then-pass": (dict(first=[A], later=[B_STUDY], after="pass"), [PASS], None),
    "e23-new-series-then-pass": (dict(first=[A], later=[B_SERIES], after="pass"),
                                 [PASS], None),
    "e24-new-patient-then-pass": (dict(first=[A], later=[B_PATIENT], after="pass"),
                                  [PASS], None),
    # Ruling Q3 on #573: a store nobody ever audited.
    "e02-no-audit": (dict(first=[A, B_STUDY], later=[], first_pass=False), [PASS], None),
    "e03-no-audit-profile-none": (dict(first=[A, B_STUDY], later=[], first_pass=False,
                                       config=NONE_CONFIG), [PASS], None),
    "e21-no-audit-keep": (dict(first=[A, B_STUDY], later=[], first_pass=False,
                               config=KEEP_CONFIG), [PASS], None),
    "e18-no-audit-reopened": (dict(first=[A, B_STUDY], later=[], first_pass=False,
                                   reopen=True), [PASS], None),
    "e11-no-audit-wfdb": (dict(first=[A, B_PATIENT], later=[], first_pass=False,
                               export="wfdb", src=ECG), [PASS], None),
    # Every entity scanned.
    "e16-full-pass": (dict(first=[A, B_STUDY], later=[]), [PASS], None),
    "e04-profile-none-full-pass": (dict(first=[A, B_STUDY], later=[], config=NONE_CONFIG),
                                   [PASS], None),
    "e05-profile-none-audit-only": (dict(first=[A, B_STUDY], later=[], first_pass=False,
                                         after="audit", config=NONE_CONFIG), [RR], None),
    "e20-keep-audit-only": (dict(first=[A, B_STUDY], later=[], first_pass=False,
                                 after="audit", config=KEEP_CONFIG), [PASS], None),
    "e22-keep-audit-reopened": (dict(first=[A, B_STUDY], later=[], first_pass=False,
                                     after="audit", reopen=True, config=KEEP_CONFIG),
                                [PASS], None),
}


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_the_measured_table(tmp_path, shape):
    """Each row of the #1064 table, at the grade and the line measured.

    A row with counts grades REVIEW_REQUIRED for condition 9 and nothing
    else, so the line is what moved the grade. A row without counts holds
    no line 9 at all: PASS where every entity was read or none ever was,
    and condition 7 alone where `audit()` read the late file and no pass
    acted (r3, e05) -- `audit()` is what clears the line.
    """
    arguments, grades, counts = SHAPES[shape]
    reports = _run(tmp_path, **arguments)

    assert [grade for grade, _, _ in reports] == grades, reports
    for grade, reasons, _ in reports:
        if counts is not None:
            assert reasons == [_never(*counts)], reasons
        else:
            assert not [r for r in reasons if _is_line_9(r)], reasons
            if grade == RR:
                assert len(reasons) == 1 and _is_line_7(reasons[0]), reasons


def test_a_file_ingested_after_the_pass_grades_review_required(tmp_path):
    """The reported shape (r1), and the frozen row's test: it goes red when
    condition 9's `append` is deleted.

    Also red when the gate is inverted, or when the count reads
    `phi_status` (A's entities are REMEDIATED there, and B's three read
    UNSCANNED either way, so this one is not that mutant's killer: the
    partition test is). The file is read to show what the PASS stood over:
    B is written as ingested and carries no `(0012,0062)`.
    """
    _write(tmp_path / "a", "a.dcm", **A)
    _write(tmp_path / "b", "b.dcm", **B_STUDY)
    with _session(tmp_path) as session:
        session.ingest(str(tmp_path / "a"))
        session.anonymize(session.audit())
        session.save(sync=True)
        session.ingest(str(tmp_path / "b"))
        session.export(str(tmp_path / "out"), use_compression=False)
        grade, reasons, scan = _report(session, tmp_path)

    assert grade == RR
    assert reasons == [
        "3 entities never scanned, in a store that holds a recorded PHI "
        "status: no PHI status is recorded on it, by a scan or by a pass, so "
        "nothing says what the policy names was looked for in it; `audit()` "
        "reads it (patients 1, studies 1, instances 1)"]
    assert scan == (
        "*   **PHI Scan (`audit()`):** 1 of 2 instance(s) have no PHI scan at "
        "their current revision. Each was never scanned (grade condition 9) "
        "or was edited after its scan (condition 8), and the Grade Basis "
        "counts it.")
    by_accession = {ds.AccessionNumber: ds for ds in _exported(tmp_path / "out")}
    late = by_accession[ACC_B]
    assert _originals(late) == sorted(
        [PID_A, "Zyxwvut", ACC_B, "19610917", "20190304", "Xanthippe", "Oberholtzer",
         "STID-88231", ROOT])
    assert (0x0012, 0x0062) not in late
    [passed] = [ds for acc, ds in by_accession.items() if acc != ACC_B]
    assert _originals(passed) == [] and passed[0x0012, 0x0062].value == "YES"


def test_a_late_series_of_a_passed_study_is_counted(tmp_path):
    """r2, the shape that looks de-identified: the export stamps the
    pseudonym and the replaced Patient ID from the owners, and the
    Accession Number, birth date, institution and physician are the
    source's.

    The study is A's and holds a status, so `studies 0`; the instance is
    the late one. `patients 1` is NOT a designed count: this ingest leaves
    a second patient in the store under the source Patient ID, holding no
    studies (#1065), and condition 9 counts it because it is a patient
    with no recorded status. When #1065 is fixed this line reads
    `patients 0` and this assertion moves with it. `audit()` records a
    status on that patient too, so the line's "`audit()` reads it" is true
    of it.
    """
    _write(tmp_path / "a", "a.dcm", **A)
    _write(tmp_path / "b", "b.dcm", **B_SERIES)
    with _session(tmp_path) as session:
        session.ingest(str(tmp_path / "a"))
        session.anonymize(session.audit())
        session.save(sync=True)
        session.ingest(str(tmp_path / "b"))
        session.export(str(tmp_path / "out"), use_compression=False)
        grade, reasons, _ = _report(session, tmp_path)
        assert sorted(len(p.studies) for p in session.store.patients) == [0, 1]
        session.audit()
        _, after_audit, _ = _report(session, tmp_path, "after-audit.md")

    assert grade == RR and reasons == [_never(1, 0, 1)]
    [late] = [ds for ds in _exported(tmp_path / "out") if ds.AccessionNumber == ACC_B]
    assert late.PatientID.startswith("ANON_") and "Zyxwvut" not in str(late.PatientName)
    # Not the Study Date either: the export stamps the study's, shifted.
    assert _originals(late) == sorted(
        [ACC_B, "19610917", "Xanthippe", "Oberholtzer", "STID-88231", ROOT])
    assert (0x0012, 0x0062) not in late
    # `ROOT` above only says a source UID is somewhere in the file. Which:
    # the Study Instance UID is the study's replacement, stamped like the
    # name; the Series and SOP Instance UIDs are the source's. Read whole,
    # the owners' four stamps are the only top-level elements that differ
    # from the file ingested (review of #1066, delta).
    source = pydicom.dcmread(str(tmp_path / "b" / "b.dcm"))
    assert late.StudyInstanceUID.startswith("2.25.")
    assert late.SeriesInstanceUID == source.SeriesInstanceUID == f"{ROOT}.1.9"
    assert late.SOPInstanceUID == source.SOPInstanceUID == f"{ROOT}.1.9.1"
    differing = sorted(
        f"{elem.tag.group:04x},{elem.tag.element:04x}" for elem in source
        if elem.tag not in late or late[elem.tag].value != elem.value)
    assert differing == ["0008,0020", "0010,0010", "0010,0020", "0020,000d"]
    assert [elem.tag for elem in late if elem.tag not in source] == []
    assert not [r for r in after_audit if _is_line_9(r)], after_audit
    assert len(after_audit) == 1 and _is_line_7(after_audit[0]), after_audit


def test_never_scanned_and_edited_since_are_counted_apart(tmp_path):
    """Conditions 8 and 9 are disjoint, and between them they are section
    5's count: one instance never scanned, another edited after its scan.

    Kills a condition 9 that reads `phi_status` (the edited instance reads
    UNSCANNED there and would be counted twice, `instances 2`), and a
    condition 9 with no UNSCANNED-or-None test on the raw record.
    """
    _write(tmp_path / "a", "a.dcm", **A)
    _write(tmp_path / "b", "b.dcm", **B_PATIENT)
    with _session(tmp_path) as session:
        session.ingest(str(tmp_path / "a"))
        session.anonymize(session.audit())
        [scanned] = _instances(session)
        session.ingest(str(tmp_path / "b"))
        scanned.set_attr("0008,103e", "an edit after the scan")
        session.export(str(tmp_path / "out"), use_compression=False)
        grade, reasons, scan = _report(session, tmp_path)

    assert grade == RR
    assert reasons == [
        "1 entity edited after the last PHI scan: its content was changed "
        "after its PHI status was recorded, and no scan has read the change; "
        "`audit()` reads it (patients 0, studies 0, instances 1)",
        _never(1, 1, 1)]
    assert scan.startswith(
        "*   **PHI Scan (`audit()`):** 2 of 2 instance(s) have no PHI scan at "
        "their current revision. Each was never scanned (grade condition 9)")


def test_a_status_an_edit_left_stale_is_a_recorded_status(tmp_path):
    """The gate reads the raw record, stale included: A's patient, study
    and instance are each edited after the pass, so no entity in the store
    reads anything but UNSCANNED, and the late patient still grades.

    The stale statuses are the only evidence the store was ever scanned.
    They are also what a reopen brings back (`phi_status_edited`, #767),
    so the same two lines are read by a session that never scanned. Kills
    a gate that reads `phi_status` (closed here: PASS for condition 9,
    leaving line 8 alone).
    """
    _write(tmp_path / "a", "a.dcm", **A)
    _write(tmp_path / "b", "b.dcm", **B_PATIENT)
    with _session(tmp_path) as session:
        session.ingest(str(tmp_path / "a"))
        session.anonymize(session.audit())
        [patient] = session.store.patients
        patient.patient_name = "Edited^After"
        patient.studies[0].study_date = "20010101"
        patient.studies[0].series[0].instances[0].set_attr("0008,103e", "edited")
        session.ingest(str(tmp_path / "b"))
        assert {e.phi_status for p in session.store.patients
                for e in [p, *p.studies, *_instances(session)]} == {PhiStatus.UNSCANNED}
        _, reasons, _ = _report(session, tmp_path)
        session.save(sync=True)

    with _session(tmp_path) as reopened:
        _, reopened_reasons, _ = _report(reopened, tmp_path, "reopened.md")

    expected = [
        "3 entities edited after the last PHI scan: its content was changed "
        "after its PHI status was recorded, and no scan has read the change; "
        "`audit()` reads it (patients 1, studies 1, instances 1)",
        _never(1, 1, 1)]
    assert reasons == expected
    assert reopened_reasons == expected


def _one_of(session, level):
    patient = session.store.patients[0]
    return {"patients": patient, "studies": patient.studies[0],
            "instances": patient.studies[0].series[0].instances[0]}[level]


@pytest.mark.parametrize("level", ["patients", "studies", "instances"])
def test_a_status_at_one_level_alone_opens_the_gate(tmp_path, level):
    """A status on one patient, one study or one instance, and on nothing
    else in the store, is a recorded status: every other entity grades.

    Two patients, never audited; one entity is given a status directly
    (what a pass over a hand-built finding leaves). The other five count,
    and the level that holds the status counts one fewer, which is what
    tells the three levels apart. Read again by a reopened session, so the
    evidence is the stored row. Kills a gate that skips a level.
    """
    _write(tmp_path / "a", "a.dcm", **A)
    _write(tmp_path / "a", "b.dcm", **B_PATIENT)
    with _session(tmp_path) as session:
        session.ingest(str(tmp_path / "a"))
        _one_of(session, level).record_phi_status(PhiStatus.CLEARED)
        _, reasons, _ = _report(session, tmp_path)
        session.save(sync=True)

    with _session(tmp_path) as reopened:
        _, reopened_reasons, _ = _report(reopened, tmp_path, "reopened.md")

    counts = {"patients": 2, "studies": 2, "instances": 2}
    counts[level] -= 1
    expected = _never(counts["patients"], counts["studies"], counts["instances"])
    assert [r for r in reasons if _is_line_9(r)] == [expected], reasons
    assert [r for r in reopened_reasons if _is_line_9(r)] == [expected], reopened_reasons


def test_a_status_on_a_series_or_a_nested_item_is_not_read(tmp_path):
    """Neither opens the gate and neither is counted: a store grades as its
    own reopened self does.

    The store has no column for a Series status, so one recorded in memory
    is gone after a reopen; nothing scans a nested item. With a status on
    each and on nothing else the store is still one nobody audited (Q3 on
    #573): no line 9, before and after the reopen. Kills a gate, or a
    count, that walks series or items.
    """
    _write(tmp_path / "a", "a.dcm", nested=True, **A)
    with _session(tmp_path) as session:
        session.ingest(str(tmp_path / "a"))
        [patient] = session.store.patients
        [series] = patient.studies[0].series
        series.record_phi_status(PhiStatus.REMEDIATED)
        [instance] = series.instances
        [item] = instance.sequences["0040,0275"].items
        item.record_phi_status(PhiStatus.REMEDIATED)
        session.export(str(tmp_path / "out"), use_compression=False)
        grade, reasons, scan = _report(session, tmp_path)

    with _session(tmp_path) as reopened:
        reopened_grade, reopened_reasons, _ = _report(reopened, tmp_path, "reopened.md")

    assert (grade, reasons) == (PASS, [])
    assert (reopened_grade, reopened_reasons) == (PASS, [])
    assert scan == (
        "*   **PHI Scan (`audit()`):** 1 of 1 instance(s) have no PHI scan at "
        "their current revision. No PHI status is recorded on any patient, "
        "study or instance in this store, so they are not graded: an entity "
        "never scanned costs the PASS only in a store that holds a recorded "
        "status (grade condition 9).")


def test_a_full_pass_over_series_and_nested_items_grades_pass(tmp_path):
    """Two series of one study and a nested sequence in each file, one full
    pass: PASS, no line 9, nothing unscanned.

    A series has no stored status and nothing scans a nested item, so a
    count that included either would never reach zero in a reopened store.
    The reopened report is the one that kills it: in memory the pass
    leaves a raw status on the series.
    """
    _write(tmp_path / "a", "a.dcm", nested=True, **A)
    _write(tmp_path / "a", "b.dcm", nested=True, **B_SERIES)
    with _session(tmp_path) as session:
        session.ingest(str(tmp_path / "a"))
        session.anonymize(session.audit())
        session.export(str(tmp_path / "out"), use_compression=False)
        first = _report(session, tmp_path)

    with _session(tmp_path) as reopened:
        second = _report(reopened, tmp_path, "reopened.md")

    scan = ("*   **PHI Scan (`audit()`):** 0 of 2 instance(s) have no PHI scan "
            "at their current revision.")
    assert first == (PASS, [], scan)
    assert second == (PASS, [], scan)


def test_the_remedy_returns_the_store_to_pass(tmp_path):
    """e19: the late file is exported by mistake, then `anonymize(audit())`
    and a second export. The first report grades for condition 9; the
    second is PASS, and the second export holds nothing original.

    The condition is read from the store at report time, so it clears when
    a scan reads the entity. A row written at export would have stayed in
    the audit log and graded every later report (the design not taken).
    """
    _write(tmp_path / "a", "a.dcm", **A)
    _write(tmp_path / "b", "b.dcm", **B_STUDY)
    with _session(tmp_path) as session:
        session.ingest(str(tmp_path / "a"))
        session.anonymize(session.audit())
        session.save(sync=True)
        session.ingest(str(tmp_path / "b"))
        session.export(str(tmp_path / "mistake"), use_compression=False)
        mistaken = _report(session, tmp_path, "mistake.md")
        session.anonymize(session.audit())
        session.export(str(tmp_path / "out"), use_compression=False)
        remedied = _report(session, tmp_path)

    assert mistaken[:2] == (RR, [_never(1, 1, 1)])
    assert remedied[:2] == (PASS, [])
    files = _exported(tmp_path / "out")
    assert len(files) == 2
    assert [_originals(ds) for ds in files] == [[], []]
    assert [ds[0x0012, 0x0062].value for ds in files] == ["YES", "YES"]


def test_an_audit_only_store_grades_a_late_file_under_both_conditions(tmp_path):
    """e15: A audited and never passed, B ingested after it. Condition 7
    counts A's three entities, condition 9 B's study and instance; the
    patient is A's, found by its unchanged Patient ID, and holds a status.

    A status of any value opens the gate, IDENTIFIED included: kills a gate
    that reads only REMEDIATED or CLEARED.
    """
    _write(tmp_path / "a", "a.dcm", **A)
    _write(tmp_path / "b", "b.dcm", **B_STUDY)
    with _session(tmp_path) as session:
        session.ingest(str(tmp_path / "a"))
        session.audit()
        session.ingest(str(tmp_path / "b"))
        session.export(str(tmp_path / "out"), use_compression=False)
        grade, reasons, _ = _report(session, tmp_path)

    assert grade == RR
    assert reasons == [
        "3 entities read IDENTIFIED: the last PHI scan raised a finding under "
        "the policy it ran with, and no `anonymize()` pass since acted on it "
        "(patients 1, studies 1, instances 1)",
        _never(0, 1, 1)]


def test_one_entity_is_counted_in_the_singular(tmp_path):
    """A audited and never passed, B a late series of A's study: the
    patient and the study are A's and hold a status, so the late instance
    is the one entity counted. (No second patient here, unlike r2: A's
    Patient ID was never replaced, so B is linked under it.)

    Kills the noun's `== 1`, and a count that adds a level to another.
    """
    _write(tmp_path / "a", "a.dcm", **A)
    _write(tmp_path / "b", "b.dcm", **B_SERIES)
    with _session(tmp_path) as session:
        session.ingest(str(tmp_path / "a"))
        session.audit()
        session.ingest(str(tmp_path / "b"))
        _, reasons, _ = _report(session, tmp_path)

    assert [r for r in reasons if _is_line_9(r)] == [
        "1 entity never scanned, in a store that holds a recorded PHI "
        "status: no PHI status is recorded on it, by a scan or by a pass, so "
        "nothing says what the policy names was looked for in it; `audit()` "
        "reads it (patients 0, studies 0, instances 1)"], reasons


def test_a_status_set_back_to_unscanned_is_no_recorded_status(tmp_path):
    """`record_phi_status(PhiStatus.UNSCANNED)` over a status leaves the raw
    record UNSCANNED, not unset, and the store keeps no trace of the status
    it replaced (it writes `unscanned` with no `phi_status_edited`). So it
    reads as no recorded status, in the session and after a reopen alike:
    the only status in this store is taken back, and the store is one
    nobody audited again -- no line 9.

    Kills a predicate that tests the raw record against None alone (the
    gate would stay open in the session, count the patient and the study,
    and close on the reopen).
    """
    _write(tmp_path / "a", "a.dcm", **A)
    with _session(tmp_path) as session:
        session.ingest(str(tmp_path / "a"))
        [instance] = _instances(session)
        instance.record_phi_status(PhiStatus.CLEARED)
        instance.record_phi_status(PhiStatus.UNSCANNED)
        assert instance._phi_status is PhiStatus.UNSCANNED
        session.export(str(tmp_path / "out"), use_compression=False)
        first = _report(session, tmp_path)[:2]

    with _session(tmp_path) as reopened:
        second = _report(reopened, tmp_path, "reopened.md")[:2]

    assert first == (PASS, [])
    assert second == (PASS, [])


def test_the_report_object_holds_the_counts_with_the_gate_open_and_shut(
        tmp_path, monkeypatch):
    """`ComplianceReport.never_scanned` is the per-level count whether or
    not it grades, and `phi_status_recorded` is the gate (review of #1066,
    note 1).

    Every other test here reads the report file, and the renderer reads
    only the gate, so nothing else sees the counts: with the field not
    handed to the report at all, or handed empty while the gate is shut,
    all of them passed. The report object is taken from the renderer
    `generate_report` hands it to. Gate shut first (two files, never
    audited: counted, not graded), then open (a pass, then a late file).
    """
    taken = []

    class _Keeping(reporting_module.MarkdownRenderer):
        def render(self, report, output_path):
            taken.append(report)
            super().render(report, output_path)

    monkeypatch.setattr(session_module, "get_renderer", lambda _format: _Keeping())
    _write(tmp_path / "a", "a.dcm", **A)
    _write(tmp_path / "a", "b.dcm", **B_PATIENT)
    _write(tmp_path / "c", "c.dcm", study="8", series="1", accession=ACC_B)
    with _session(tmp_path) as session:
        session.ingest(str(tmp_path / "a"))
        shut_grade, _, _ = _report(session, tmp_path, "shut.md")
        session.anonymize(session.audit())
        session.ingest(str(tmp_path / "c"))
        open_grade, open_reasons, _ = _report(session, tmp_path, "open.md")

    shut, opened = taken
    assert shut_grade == RR  # no audit rows yet: condition 1, not 9
    assert (shut.never_scanned, shut.phi_status_recorded) == (
        {"patients": 2, "studies": 2, "instances": 2}, False)
    assert not [r for r in shut.review_reasons if _is_line_9(r)]
    assert (opened.never_scanned, opened.phi_status_recorded) == (
        {"patients": 1, "studies": 1, "instances": 1}, True)
    assert (open_grade, open_reasons) == (RR, [_never(1, 1, 1)])


def test_the_line_holds_counts_and_no_value(tmp_path):
    """The Grade Basis names how many, never which: no identifier of
    either file, and no UID, is anywhere in section 5 of r1's report."""
    _write(tmp_path / "a", "a.dcm", **A)
    _write(tmp_path / "b", "b.dcm", **B_PATIENT)
    with _session(tmp_path) as session:
        session.ingest(str(tmp_path / "a"))
        session.anonymize(session.audit())
        session.ingest(str(tmp_path / "b"))
        path = tmp_path / "report.md"
        session.generate_report(str(path))

    section_5 = path.read_text(encoding="utf-8").split(
        "## 5. Validation & Verification", 1)[1]
    # The line itself, so this is not satisfied by the PHI Scan bullet's
    # own "never scanned" (which main's report already held).
    assert f"    *   {_never(1, 1, 1)}\n" in section_5
    assert [o for o in IDENTIFIERS if o in section_5] == []
