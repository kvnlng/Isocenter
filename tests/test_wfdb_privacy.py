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
    assert not any(line.startswith("#") for line in _header_text(paths).splitlines())


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


def test_start_datetime_reflects_the_shifted_acquisition_time(tmp_path):
    """The header must carry shifted timing, not the source timestamp."""
    src = tmp_path / "src"
    src.mkdir()
    write_fixture(str(src / "ecg.dcm"), num_samples=100)

    session = DicomSession(persistence_file=str(tmp_path / "shift.db"))
    try:
        session.ingest(str(src))
        instance = session.store.patients[0].studies[0].series[0].instances[0]
        # Simulate the remediation pipeline having shifted the acquisition time.
        instance.set_attr("0008,002a", "20250615081500.000000")

        paths = session.export(str(tmp_path / "out"), format="wfdb")
        record_line = _header_text(paths).splitlines()[0].split()
    finally:
        session.close()

    assert record_line[4] == "08:15:00"
    assert record_line[5] == "15/06/2025"
    assert "2026" not in " ".join(record_line)


def test_missing_acquisition_datetime_omits_timing_fields(tmp_path):
    """No usable date anywhere -> timing fields are omitted entirely.

    DEVIATION FROM BRIEF: the brief's Step 1 version of this test clears
    only Acquisition DateTime (0008,002A). But `_start_datetime`'s own
    Step-3-specified fallback reads Study Date (0008,0020) + Study Time
    (0008,0030) when Acquisition DateTime is absent -- and the fixture
    (scripts.generate_waveform_test_data) always populates both. So the
    brief's version of this test exercises the *fallback* path (and
    correctly produces 6 fields, not 4), contradicting its own name and
    the asserted `len(record_line) == 4`. This is a genuine mismatch
    between the brief's test and the brief's own implementation, not a
    production bug: see the Task 9 report. Clearing Study Date too is
    required to reach the "no usable value at all" path this test's name
    describes.
    """
    src = tmp_path / "src"
    src.mkdir()
    write_fixture(str(src / "ecg.dcm"), num_samples=100)

    session = DicomSession(persistence_file=str(tmp_path / "nodate.db"))
    try:
        session.ingest(str(src))
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
        # the description as the final (9th) field. FREE_TEXT_MARKER
        # itself contains spaces, so split(maxsplit=8) is required to
        # capture it whole; a naive split() would fragment it.
        naive_fields = signal_line.split()
        description_field = signal_line.split(maxsplit=8)[-1]
    finally:
        session.close()

    # CHARACTERIZATION (evidence, not a safety guarantee -- see module
    # docstring above; report, do not silently "fix"):
    #
    # (1) wfdb_description() falls back to the raw ChannelLabel when
    #     source_code is empty, so the free-text marker DOES reach the
    #     .hea signal line's description field verbatim.
    assert description_field == FREE_TEXT_MARKER, (
        "characterization assumption changed: expected the free-text "
        "ChannelLabel fallback to reach the .hea description field "
        f"verbatim; got {description_field!r} instead. If this fallback "
        "was fixed, update this test to match the new (safer) behaviour "
        "instead of deleting the assertion.")
    # (2) Because format_header() does not sanitize or quote the
    #     description field, free text containing spaces also fragments
    #     the header(5) *field count* itself for any reader that
    #     whitespace-splits the signal line (the conventional way to
    #     parse header(5)) -- a second-order structural defect riding on
    #     top of the PHI leak.
    assert len(naive_fields) != 9, (
        "expected the free-text description to fragment the naive "
        "whitespace split away from 9 fields (demonstrating header(5) "
        f"field-count corruption); got exactly 9: {naive_fields!r}. If "
        "the field count is now stable, the fragmentation concern in the "
        "Task 9 report may no longer apply -- update this assertion to "
        "match.")
