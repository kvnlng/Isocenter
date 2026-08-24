import os

import pytest

from gantry.session import DicomSession
from scripts.generate_waveform_test_data import build_ecg_dataset, write_fixture


@pytest.fixture
def session_with_ecg(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    write_fixture(str(src / "ecg.dcm"), num_samples=200,
                  patient_id="MRN-12345678", patient_name="Doe^Jane")

    session = DicomSession(persistence_file=str(tmp_path / "phi.db"))
    session.ingest(str(src))
    # NOTE: the brief's fixture (task-9-brief.md Step 1) omits this call.
    # Without it, `patient.patient_id` is never pseudonymized -- ingest()
    # alone does not de-identify -- so the raw source MRN and PatientName
    # persist unchanged in session.store, and any "PHI never appears in
    # output" assertion below would either fail honestly (record name) or
    # pass vacuously for the wrong reason (patient name -- see the comment
    # on test_patient_name_never_appears_in_output). Calling anonymize()
    # here is required to make these tests exercise real de-identification
    # rather than an untouched pipeline stage. See task-9-report.md for the
    # full writeup of this brief defect.
    session.anonymize()
    yield session, tmp_path
    session.close()


def _header_text(paths):
    with open(paths[0], encoding="utf-8") as f:
        return f.read()


def test_channel_label_is_indexed_for_phi_scanning(session_with_ecg):
    """Free-text SH/UT inside the waveform sequence must reach the inspector."""
    session, _ = session_with_ecg
    instance = session.store.patients[0].studies[0].series[0].instances[0]
    indexed_tags = {tag for _, tag in instance.text_index}
    assert "003a,0203" in indexed_tags


def test_header_contains_no_comment_lines(session_with_ecg):
    session, tmp_path = session_with_ecg
    paths = session.export(str(tmp_path / "out"), format="wfdb")
    header = _header_text(paths)
    # Positive precondition: guard against a vacuous pass. An empty (or
    # missing) header would also contain no "#" lines, so first prove the
    # header is real and has the shape we expect -- a record line plus at
    # least one signal line -- before trusting the negative assertion.
    assert paths, "export produced no .hea files"
    assert header.strip(), "header file was empty"
    lines = header.splitlines()
    assert len(lines) >= 2, "header has no signal line beyond the record line"
    assert len(lines[0].split()) >= 4, "record line missing expected leading fields"
    assert not any(line.startswith("#") for line in lines)


def test_record_name_excludes_the_source_patient_id(session_with_ecg):
    session, tmp_path = session_with_ecg
    paths = session.export(str(tmp_path / "out"), format="wfdb")
    header = _header_text(paths)
    # Positive precondition: the export produced a real, non-empty header
    # with a record line naming a pseudonymized record, so the negative
    # assertions below cannot pass vacuously against an empty/missing file.
    assert paths, "export produced no .hea files"
    assert header.strip(), "header file was empty"
    assert header.splitlines()[0].startswith("ANON_"), (
        "expected a pseudonymized record name; got: "
        f"{header.splitlines()[0]!r}")
    assert "MRN-12345678" not in header
    assert "MRN" not in os.path.basename(paths[0])


def test_patient_name_never_appears_in_output(session_with_ecg):
    session, tmp_path = session_with_ecg
    paths = session.export(str(tmp_path / "out"), format="wfdb")
    header = _header_text(paths)
    # Positive precondition: guard against a vacuous pass. The WFDB
    # header(5) format has no PatientName field at all, so "Doe"/"Jane"
    # not appearing proves nothing about de-identification by itself --
    # it would hold even against an unanonymized session. Assert instead
    # that the patient identifier that WOULD have carried the name-bearing
    # PatientID has actually been pseudonymized, so this test fails loudly
    # if anonymization stops running.
    assert paths, "export produced no .hea files"
    assert header.strip(), "header file was empty"
    assert session.store.patients[0].patient_id.startswith("ANON_"), (
        "patient_id was not pseudonymized; this test would otherwise pass "
        "vacuously since the WFDB header carries no PatientName field")
    assert "Doe" not in header
    assert "Jane" not in header


def test_header_date_reflects_the_real_shifted_study_date(tmp_path):
    """The header date must come from a REAL `session.anonymize()` shift,
    not merely an injected instance tag.

    ROUND-1 DEFECT (coordinator CRITICAL 1, fixed here): the shipped
    `gantry/resources/phi_tags.json` has no date tags, so instance-level
    Acquisition DateTime (0008,002A) / Study Date (0008,0020) / Study
    Time (0008,0030) are never covered by the default remediation config
    and are NEVER shifted by `session.anonymize()`. The date shift that
    actually runs is a Study-level scan (`gantry/privacy.py:_scan_study`)
    whose SHIFT_DATE remediation (`gantry/remediation.py`) writes the new
    date onto `study.study_date` and sets `study.date_shifted = True`.
    `_start_datetime` must read THAT field, not the instance tags, or the
    header silently leaks the real source date past a genuine anonymize
    pass -- a real Safe Harbor violation, not a hypothetical one.

    This test runs a REAL `session.anonymize()` (no hand-injected tags)
    and asserts the header date equals the shifted `study.study_date`
    AND differs from the untouched source date.
    """
    src = tmp_path / "src"
    src.mkdir()
    write_fixture(str(src / "ecg.dcm"), num_samples=100)
    # Fixture source date: scripts.generate_waveform_test_data sets
    # StudyDate="20260101" / AcquisitionDateTime="20260101101530.000000".
    source_date_token = "01/01/2026"

    session = DicomSession(persistence_file=str(tmp_path / "shift.db"))
    try:
        session.ingest(str(src))
        session.anonymize()
        study = session.store.patients[0].studies[0]

        # Positive precondition: anonymize() really shifted the Study, so
        # a passing date-comparison below reflects real remediation, not
        # an unrelated no-op.
        assert study.date_shifted is True, (
            "study was not marked date_shifted; anonymize() did not run "
            "the SHIFT_DATE remediation this test depends on")
        assert study.study_date is not None, "study_date is missing after anonymize()"
        expected_date_token = study.study_date.strftime("%d/%m/%Y")
        assert expected_date_token != source_date_token, (
            "shifted study_date coincides with the source date by chance; "
            "this test cannot distinguish shifted from unshifted timing")

        paths = session.export(str(tmp_path / "out"), format="wfdb")
        assert paths, "export produced no .hea files"
        header = _header_text(paths)
        assert header.strip(), "header file was empty"
        record_line = header.splitlines()[0].split()
        assert len(record_line) == 6, (
            f"expected record line with timing fields, got: {record_line!r}")
    finally:
        session.close()

    assert record_line[5] == expected_date_token
    assert record_line[5] != source_date_token
    assert source_date_token not in " ".join(record_line)


def test_start_datetime_falls_back_to_instance_tags_without_study_date(tmp_path):
    """Fall back to instance tags only when `study.study_date` is absent.

    Un-anonymized session (no `session.anonymize()` call): `study.study_date`
    still holds real (un-shifted) timing like every other un-remediated
    field, so `_start_datetime` uses it -- there is no policy of
    suppressing timing on un-anonymized sessions (that would be a design
    change per the coordinator, not a bug fix). To exercise the true
    "study has no date at all" fallback path, this test clears
    `study.study_date` directly and confirms the instance's own Acquisition
    DateTime is used instead.
    """
    src = tmp_path / "src"
    src.mkdir()
    write_fixture(str(src / "ecg.dcm"), num_samples=100)

    session = DicomSession(persistence_file=str(tmp_path / "fallback.db"))
    try:
        session.ingest(str(src))
        study = session.store.patients[0].studies[0]
        study.study_date = None
        instance = session.store.patients[0].studies[0].series[0].instances[0]
        instance.set_attr("0008,002a", "20250615081500.000000")

        paths = session.export(str(tmp_path / "out"), format="wfdb")
        assert paths, "export produced no .hea files"
        header = _header_text(paths)
        assert header.strip(), "header file was empty"
        record_line = header.splitlines()[0].split()
        assert len(record_line) == 6, (
            f"expected record line with timing fields, got: {record_line!r}")
    finally:
        session.close()

    assert record_line[4] == "08:15:00"
    assert record_line[5] == "15/06/2025"


def test_missing_acquisition_datetime_omits_timing_fields(tmp_path):
    """No usable date anywhere -> timing fields are omitted entirely.

    DEVIATION FROM BRIEF: the brief's Step 1 version of this test clears
    only Acquisition DateTime (0008,002A). But timing now also has a
    `study.study_date` source (coordinator CRITICAL 1 fix), and the
    original instance-tag fallback (Study Date (0008,0020) + Study Time
    (0008,0030)) still applies beneath that -- and the fixture
    (scripts.generate_waveform_test_data) always populates both. So
    reaching "no usable value anywhere" requires clearing all three:
    `study.study_date`, Acquisition DateTime, and Study Date.
    """
    src = tmp_path / "src"
    src.mkdir()
    write_fixture(str(src / "ecg.dcm"), num_samples=100)

    session = DicomSession(persistence_file=str(tmp_path / "nodate.db"))
    try:
        session.ingest(str(src))
        study = session.store.patients[0].studies[0]
        study.study_date = None
        instance = session.store.patients[0].studies[0].series[0].instances[0]
        instance.set_attr("0008,002a", "")
        instance.set_attr("0008,0020", "")
        paths = session.export(str(tmp_path / "out"), format="wfdb")
        # Positive precondition: the export produced a real 4-field record
        # line to inspect, not an empty/missing file that would make the
        # length assertion pass by accident.
        assert paths, "export produced no .hea files"
        header = _header_text(paths)
        assert header.strip(), "header file was empty"
        record_line = header.splitlines()[0].split()
    finally:
        session.close()

    assert len(record_line) == 4


# --- Characterization: ChannelLabel free-text fallback (Task 4 carry-forward) ---
#
# WaveformChannel.wfdb_description() (gantry/waveform.py) prefers the coded
# Channel Source Sequence value, but falls back to the free-text ChannelLabel
# when no source code is present. ChannelLabel is operator-typed SH text, so
# this fallback is a plausible PHI carrier. The brief's fixture always
# populates ChannelSourceSequence, so that fallback path is never exercised
# by the tests above. This test builds a dataset with an EMPTY/ABSENT Channel
# Source Sequence and a recognisable free-text marker in ChannelLabel, then
# asserts what actually reaches the .hea signal line.
#
# This test does NOT assert the fallback is safe or unsafe -- it documents
# current behaviour so a reviewer can see it directly. Do not change
# wfdb_description() to make this test "pass differently" without sign-off;
# see the Task 9 report for the CONCERN this raises.
FREE_TEXT_MARKER = "OPERATOR NOTE Smith^John DOB19800101"


def _write_fixture_with_uncoded_channel(path, patient_id="WFTEST001",
                                        patient_name="Waveform^Test"):
    """Build+write an ECG dataset whose sole channel has no Channel Source
    Sequence, and whose ChannelLabel carries a free-text PHI-shaped marker.
    """
    ds = build_ecg_dataset(
        channels=[("MDC_ECG_LEAD_I", "Lead I")],
        patient_id=patient_id,
        patient_name=patient_name,
    )
    chdef = ds.WaveformSequence[0].ChannelDefinitionSequence[0]
    assert chdef.ChannelLabel == "Lead I"  # precondition: fixture builder set it
    del chdef.ChannelSourceSequence  # simulate a source with no coded value
    chdef.ChannelLabel = FREE_TEXT_MARKER

    import pydicom
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    pydicom.dcmwrite(path, ds, enforce_file_format=True)
    return path


def test_uncoded_channel_label_fallback_characterization(tmp_path):
    """Characterize: with no coded channel source, does free-text
    ChannelLabel reach the .hea signal line?

    This is evidence, not a safety guarantee -- see module docstring above.
    """
    src = tmp_path / "src"
    src.mkdir()
    dcm_path = _write_fixture_with_uncoded_channel(str(src / "ecg.dcm"))

    # Precondition: the source file we just wrote really has no coded
    # channel source and really carries the free-text marker, so a failure
    # below reflects export behaviour, not a broken fixture.
    import pydicom
    raw = pydicom.dcmread(dcm_path)
    raw_chdef = raw.WaveformSequence[0].ChannelDefinitionSequence[0]
    assert not hasattr(raw_chdef, "ChannelSourceSequence") or not raw_chdef.ChannelSourceSequence
    assert raw_chdef.ChannelLabel == FREE_TEXT_MARKER

    session = DicomSession(persistence_file=str(tmp_path / "fallback.db"))
    try:
        session.ingest(str(src))
        paths = session.export(str(tmp_path / "out"), format="wfdb")

        # Precondition: export actually produced a non-empty header with a
        # signal line, so the assertions below aren't vacuous.
        assert paths, "export produced no .hea files"
        header = _header_text(paths)
        assert header.strip(), "header file was empty"
        lines = header.splitlines()
        assert len(lines) >= 2, "header has no signal line to inspect"
        signal_line = lines[1]
        # header(5) signal lines are 9 whitespace-delimited fields, with
        # the description as the final (9th) field, legally running to
        # end of line -- the PhysioNet `wfdb` 4.3.1 reference reader
        # parses a line ending "... 0 0 Lead I taken by Jane Doe" as
        # sig_name=['Lead I taken by Jane Doe']. FREE_TEXT_MARKER itself
        # contains spaces, so split(maxsplit=8) is required to capture it
        # whole and matches how a spec-conformant reader treats it.
        description_field = signal_line.split(maxsplit=8)[-1]
    finally:
        session.close()

    # CHARACTERIZATION (evidence, not a safety guarantee -- see module
    # docstring above; report, do not silently "fix"): wfdb_description()
    # falls back to the raw ChannelLabel when source_code is empty, so
    # the free-text marker DOES reach the .hea signal line's description
    # field verbatim. Embedded whitespace in that field is spec-conformant
    # header(5) (see reference-reader evidence above), not a format
    # defect -- only the PHI question is live here.
    assert description_field == FREE_TEXT_MARKER, (
        "characterization assumption changed: expected the free-text "
        "ChannelLabel fallback to reach the .hea description field "
        f"verbatim; got {description_field!r} instead. If this fallback "
        "was fixed, update this test to match the new (safer) behaviour "
        "instead of deleting the assertion.")


# --- Record-name collision: instances missing InstanceNumber (Task 9 round 2, IMPORTANT 6) ---
#
# `record_name_for` (gantry/exporters/wfdb.py) derives its instance
# component from `instance.instance_number`, but `io_handlers.py` defaults
# a missing InstanceNumber to 0 (`row['instance_number'] or 0`), and
# `_sanitize(0)` collapsed to the literal token "record" (0 is falsy, so
# `name or ""` discarded it) -- so every instance lacking InstanceNumber in
# one series proposed the SAME record name. The second instance's write
# would silently clobber the first's `.hea`/`.dat` files: real data loss,
# not just a cosmetic naming collision.


def _write_series_with_two_uncounted_instances(root, patient_id="WFTEST001",
                                               patient_name="Waveform^Test"):
    """Two instances in the same Study+Series, both missing InstanceNumber,
    with different sample counts so a collision (one overwriting the
    other) is unambiguously detectable after the fact.
    """
    ds1 = build_ecg_dataset(channels=[("MDC_ECG_LEAD_I", "Lead I")],
                            patient_id=patient_id, patient_name=patient_name,
                            num_samples=40)
    ds2 = build_ecg_dataset(channels=[("MDC_ECG_LEAD_I", "Lead I")],
                            patient_id=patient_id, patient_name=patient_name,
                            num_samples=60)
    ds2.StudyInstanceUID = ds1.StudyInstanceUID
    ds2.SeriesInstanceUID = ds1.SeriesInstanceUID
    del ds1.InstanceNumber
    del ds2.InstanceNumber

    import pydicom
    os.makedirs(root, exist_ok=True)
    p1 = os.path.join(root, "a.dcm")
    p2 = os.path.join(root, "b.dcm")
    pydicom.dcmwrite(p1, ds1, enforce_file_format=True)
    pydicom.dcmwrite(p2, ds2, enforce_file_format=True)
    return p1, p2


def test_two_instances_missing_instance_number_get_distinct_records(tmp_path):
    """Two instances that both default to instance_number=0 must not
    overwrite each other's WFDB record.
    """
    src = tmp_path / "src"
    dcm1, dcm2 = _write_series_with_two_uncounted_instances(str(src))

    # Precondition: both source files really lack InstanceNumber, so a
    # collision below reflects export behaviour, not a broken fixture.
    import pydicom
    for p in (dcm1, dcm2):
        raw = pydicom.dcmread(p)
        assert not hasattr(raw, "InstanceNumber"), f"{p} unexpectedly has InstanceNumber"

    session = DicomSession(persistence_file=str(tmp_path / "dupnum.db"))
    try:
        session.ingest(str(src))
        series = session.store.patients[0].studies[0].series[0]
        # Precondition: ingestion really produced two instances in one
        # series, both defaulted to instance_number 0 -- the exact
        # collision this test targets.
        assert len(series.instances) == 2, (
            f"expected 2 instances in one series, got {len(series.instances)}")
        assert {i.instance_number for i in series.instances} == {0}, (
            "expected both instances to default instance_number to 0; "
            f"got {[i.instance_number for i in series.instances]!r}")

        paths = session.export(str(tmp_path / "out"), format="wfdb")
    finally:
        session.close()

    assert len(paths) == 2, (
        f"expected 2 distinct .hea files, got {len(paths)}: {paths!r} "
        "-- a naming collision silently dropped one record")
    assert len(set(paths)) == 2, "the two .hea paths were not actually distinct"

    sample_counts = []
    for hea_path in paths:
        with open(hea_path, encoding="utf-8") as f:
            record_line = f.readline().split()
        assert len(record_line) >= 4, f"malformed record line in {hea_path}: {record_line!r}"
        n_samples = int(record_line[3])
        dat_path = os.path.join(os.path.dirname(hea_path), record_line[0] + ".dat")
        assert os.path.exists(dat_path), f"{dat_path} referenced by header but missing"
        dat_size = os.path.getsize(dat_path)
        assert dat_size == n_samples * 2, (
            f"{dat_path} size {dat_size} does not match header's declared "
            f"{n_samples} samples (16-bit mono) -- data was overwritten/truncated")
        sample_counts.append(n_samples)

    # The two source instances had 40 and 60 samples respectively; both
    # must survive independently, proving no overwrite occurred.
    assert sorted(sample_counts) == [40, 60], (
        f"expected one 40-sample and one 60-sample record; got {sample_counts!r}")
