import numpy as np
import pytest

from gantry.exporters.wfdb import format_header, signal_checksum
from gantry.waveform import Waveform, WaveformChannel


def _waveform(n_samples=100, n_channels=2, baseline=0.0, units="mV"):
    channels = [
        WaveformChannel(label=f"L{i}", source_code=f"MDC_ECG_LEAD_{i}",
                        source_scheme="MDC", sensitivity=0.005,
                        correction_factor=1.0, units=units, baseline=baseline)
        for i in range(n_channels)
    ]
    return Waveform(sampling_frequency=500.0, num_channels=n_channels,
                    num_samples=n_samples, bits_allocated=16,
                    sample_interpretation="SS", channels=channels)


def test_checksum_is_a_signed_16_bit_sum():
    assert signal_checksum(np.array([1, 2, 3], dtype=np.int16)) == 6
    # Wraps into the negative half of the 16-bit range.
    assert signal_checksum(np.array([32767, 1], dtype=np.int16)) == -32768


def test_checksum_of_empty_signal_is_zero():
    assert signal_checksum(np.array([], dtype=np.int16)) == 0


def test_record_line_carries_geometry():
    wf = _waveform(n_samples=250, n_channels=3)
    samples = np.zeros((250, 3), dtype=np.int16)
    header = format_header("REC001", wf, samples, "REC001.dat")
    first = header.splitlines()[0].split()
    assert first[0] == "REC001"
    assert first[1] == "3"
    assert first[2] == "500"
    assert first[3] == "250"


def test_signal_lines_use_spec_conformant_gain_field():
    """header(5): gain(baseline)/units. Not Murmur's current reading."""
    wf = _waveform(n_channels=1)
    samples = np.zeros((100, 1), dtype=np.int16)
    line = format_header("REC", wf, samples, "REC.dat").splitlines()[1].split()
    assert line[0] == "REC.dat"
    assert line[1] == "16"
    assert line[2] == "200(0)/mV"


def test_nonzero_baseline_appears_in_the_gain_field():
    wf = _waveform(n_channels=1, baseline=0.5)
    samples = np.zeros((100, 1), dtype=np.int16)
    line = format_header("REC", wf, samples, "REC.dat").splitlines()[1].split()
    assert line[2] == "200(-100)/mV"


def test_signal_line_reports_adcres_zero_initval_and_checksum():
    wf = _waveform(n_channels=1)
    samples = np.array([[5], [7], [9]], dtype=np.int16)
    wf.num_samples = 3
    line = format_header("REC", wf, samples, "REC.dat").splitlines()[1].split()
    assert line[3] == "16"        # adcres
    assert line[4] == "0"         # adczero
    assert line[5] == "5"         # initval
    assert line[6] == "21"        # checksum


def test_description_uses_the_coded_source_not_the_free_text_label():
    wf = _waveform(n_channels=1)
    wf.channels[0].label = "Smith, John - Lead II"
    samples = np.zeros((100, 1), dtype=np.int16)
    line = format_header("REC", wf, samples, "REC.dat").splitlines()[1]
    assert "MDC_ECG_LEAD_0" in line
    assert "Smith" not in line


def test_header_never_contains_comment_lines():
    wf = _waveform()
    samples = np.zeros((100, 2), dtype=np.int16)
    header = format_header("REC", wf, samples, "REC.dat")
    assert not any(line.startswith("#") for line in header.splitlines())


def test_start_datetime_is_rendered_in_murmur_and_wfdb_order():
    from datetime import datetime
    wf = _waveform()
    samples = np.zeros((100, 2), dtype=np.int16)
    header = format_header("REC", wf, samples, "REC.dat",
                           start_datetime=datetime(2026, 3, 14, 9, 26, 53))
    first = header.splitlines()[0].split()
    assert first[4] == "09:26:53"
    assert first[5] == "14/03/2026"


def test_gain_is_never_zero_for_a_calibrated_channel():
    """WFDB reads gain 0 as uncalibrated and substitutes 200 adu/mV."""
    wf = _waveform(n_channels=1)
    wf.channels[0].sensitivity = 0.0
    samples = np.zeros((100, 1), dtype=np.int16)
    line = format_header("REC", wf, samples, "REC.dat").splitlines()[1].split()
    assert not line[2].startswith("0(")


def test_wfdb_records_are_colocated_with_the_dicom_export_tree(tmp_path):
    """The brief requires WFDB records to land in the same
    Patient/Study/Series tree the DICOM exporter builds -- meaning the
    tree users actually get from `session.export(folder)` /
    `session.export(folder, format="dicom")`, not any other DICOM
    folder-naming code path in this codebase. Proves it by exporting the
    SAME session as both "dicom" and "wfdb" through the real exporter
    registry and asserting the .hea file's parent directory is exactly
    the directory holding the .dcm file for that series -- not just that
    some file exists somewhere, and without hardcoding the expected
    folder names (so this test cannot drift out of step with the naming
    logic the way an earlier version of it did).
    """
    import datetime
    import os
    from unittest.mock import patch

    from gantry.entities import DicomItem, Instance, Patient, Series, Study
    from gantry.io_handlers import populate_attrs
    from gantry.session import DicomSession
    from gantry.validation import IODValidator
    from scripts.generate_waveform_test_data import build_ecg_dataset

    ds = build_ecg_dataset(num_samples=50, patient_id="COLOC01")
    n_channels = len(ds.WaveformSequence[0].ChannelDefinitionSequence)

    patient = Patient("COLOC01", "ANON")
    study = Study("1.2.3.4.STUDY", datetime.date(2026, 1, 1))
    series = Series("1.2.3.4.SERIES", "ECG", 3)
    instance = Instance("1.2.3.4.SOP", ds.SOPClassUID, 1)
    instance.attributes.update({
        "0008,1030": "Cardiology Study",  # Study Description
        "0008,103e": "12-Lead ECG",       # Series Description (lowercase
                                           # tag key -- matches how
                                           # `session._export_dicom` reads
                                           # it, and how real attribute
                                           # dicts are actually keyed).
    })
    # Only needed so the DICOM export worker's pixel-data check is
    # satisfied; unrelated to the WFDB path, which reads waveform_array.
    instance.pixel_array = np.zeros((1, 1), dtype=np.uint8)

    wf_item = DicomItem()
    populate_attrs(ds.WaveformSequence[0], wf_item)
    instance.add_sequence_item("5400,0100", wf_item)
    instance.waveform_array = np.frombuffer(
        ds.WaveformSequence[0].WaveformData, dtype="<i2"
    ).reshape(50, n_channels).copy()

    series.instances.append(instance)
    study.series.append(series)
    patient.studies.append(study)

    sess = DicomSession(":memory:")
    sess.store.patients.append(patient)

    out_dir = tmp_path / "colocation"

    # Mirrors tests/test_structured_export.py's known-good pattern for
    # exercising a real export without parallel-worker / IOD-validation
    # noise unrelated to folder placement. `session.export(folder)` (the
    # "dicom" format, default) is the actual production path: it goes
    # through `DicomSession._export_dicom`, not the legacy
    # `DicomExporter.save_patient` API.
    with patch('gantry.io_handlers.run_parallel',
              side_effect=lambda func, items, *a, **k: [func(i) for i in items]), \
         patch.object(IODValidator, "validate", lambda ds: []):
        sess.export(str(out_dir), format="dicom")

    dcm_files = list(out_dir.rglob("*.dcm"))
    assert len(dcm_files) == 1
    dcm_dir = os.path.dirname(str(dcm_files[0]))

    hea_paths = sess.export(str(out_dir), format="wfdb")
    assert len(hea_paths) == 1
    hea_dir = os.path.dirname(hea_paths[0])

    assert hea_dir == dcm_dir, (
        f"WFDB record landed in {hea_dir!r} but the DICOM exporter's "
        f"tree for the same series is {dcm_dir!r} -- the two exporters "
        "must share one folder-naming helper so their trees co-locate.")

    # Confirm this actually exercised the real "Hybrid Naming" scheme
    # (UID suffix + modality component) rather than two exporters
    # trivially agreeing on some degenerate/empty path.
    rel_parts = os.path.relpath(hea_dir, str(out_dir)).split(os.sep)
    assert rel_parts[0].startswith("Subject_COLOC01")
    assert rel_parts[1].startswith("Study_")
    assert "STUDY" in rel_parts[1]  # UID suffix of "1.2.3.4.STUDY"
    assert rel_parts[2].startswith("Series_3_ECG_")  # num + modality


# --- Final review Important 1/2/3 -------------------------------------
#
# Three defects found in the whole-branch final review, reproduced
# end-to-end through a real session.ingest() -> session.export() before
# being fixed here:
#
# 1. A Channel Label containing a newline (e.g.
#    "Lead I\n# Patient Jane Doe MRN9988776") made the .hea signal line
#    embed a literal newline followed by a `#`-prefixed line.
#    PhysioNet's own reader (`wfdb.rdheader`) surfaces that as a REAL
#    header comment (`comments=['Patient Jane Doe MRN9988776']`),
#    falsifying this module's own docstring and docs/waveforms.md's "No
#    `#` comment lines are ever written" guarantee -- a PHI escape route
#    on a de-identification product.
# 2. `units` (Channel Sensitivity Units Sequence CodeValue) is field 3
#    of 9 inside `gain(baseline)/units`, not the last field. CodeValue
#    "mV per s" made wfdb.rdheader parse units=['mV'] and
#    sig_name=['per s 16 0 0 1225 0 MDC_ECG_LEAD_I'] -- every field
#    after gain silently wrong, no error raised.
# 3. `channels[-1]` on an empty `waveform.channels` list (NumberOfWaveform
#    Channels > 0 but an absent/empty Channel Definition Sequence) raised
#    IndexError with no per-instance guard, aborting session.export()
#    entirely -- one bad instance in a 500-patient batch would silently
#    drop the other 499.


def test_description_with_embedded_newline_cannot_inject_a_comment_line():
    """Pre-fix, this failed with:

        AssertionError: assert not True
         +  where True = any(<genexpr>)

    because `header.splitlines()` split the embedded '\\n' inside the
    unsanitized description into its own '#...' line. PhysioNet's
    `wfdb.rdheader` treats that line as a real header comment (verified
    directly: `comments=['Patient Jane Doe MRN9988776']`) -- see
    tests/test_wfdb_conformance.py for the reference-reader-level proof.

    Since the lead-name allowlist (#39) landed, "Lead I\\n# Patient Jane
    Doe MRN9988776" is not a recognisable lead name at all, so besides not
    injecting a fake comment line, none of this operator text should reach
    the header -- it is replaced outright with a positional token.
    """
    wf = _waveform(n_channels=1)
    wf.channels[0].source_code = ""
    wf.channels[0].label = "Lead I\n# Patient Jane Doe MRN9988776"
    samples = np.zeros((100, 1), dtype=np.int16)
    header = format_header("REC", wf, samples, "REC.dat")

    lines = header.splitlines()
    assert not any(line.startswith("#") for line in lines), (
        f"a newline embedded in the channel description injected a "
        f"'#' comment line; header was:\n{header}")
    # The free-text label is not a recognisable lead name, so it must be
    # replaced with a positional token rather than reach the header at all.
    assert lines[1].split()[-1] == "ch0"
    assert "Patient Jane Doe MRN9988776" not in lines[1]


def test_units_with_embedded_whitespace_does_not_shift_signal_line_fields():
    """Pre-fix, this failed with:

        AssertionError: assert '200(0)/mV' == '200(0)/mVpers'

    because unsanitized "mV per s" left embedded spaces inside the
    gain(baseline)/units field, which PhysioNet's reader parses as
    units=['mV'] and shifts every subsequent field (sig_name became
    'per s 16 0 0 1225 0 MDC_ECG_LEAD_I' instead of the real lead code)
    -- see tests/test_wfdb_conformance.py for the reference-reader-level
    proof.
    """
    wf = _waveform(n_channels=1, units="mV per s")
    samples = np.zeros((100, 1), dtype=np.int16)
    line = format_header("REC", wf, samples, "REC.dat").splitlines()[1].split()

    assert line[2] == "200(0)/mVpers", (
        "whitespace survived in the units field, which would shift "
        f"every later field for a naive/spec-conformant parser: {line!r}")
    # Field alignment intact: description is still the last, correct field.
    assert line[-1] == "MDC_ECG_LEAD_0"


def test_empty_channel_definitions_do_not_raise():
    """NumberOfWaveformChannels > 0 but an absent/empty Channel
    Definition Sequence (non-conformant source) must not crash via
    `waveform.channels[-1]` on an empty list.

    Pre-fix, this failed with:

        IndexError: list index out of range

    With no per-instance guard around the caller in
    `WfdbExporter.export`, that exception propagated out of
    `session.export(folder, format="wfdb")` and aborted the entire run
    -- see test_one_failing_instance_does_not_abort_the_whole_export
    below for the batch-level containment half of this fix.
    """
    wf = Waveform(sampling_frequency=500.0, num_channels=1, num_samples=100,
                 bits_allocated=16, sample_interpretation="SS", channels=[])
    samples = np.zeros((100, 1), dtype=np.int16)

    header = format_header("REC", wf, samples, "REC.dat")

    line = header.splitlines()[1].split()
    assert line[0] == "REC.dat"
    assert len(line) == 9, f"malformed signal line: {line!r}"


def test_one_failing_instance_does_not_abort_the_whole_export(tmp_path, monkeypatch):
    """A single instance whose write raises must not abort the whole
    batch -- `WfdbExporter.export` must catch, log, and continue per
    instance, the same containment `DicomExporter._export_instance_worker`
    already provides for the DICOM format.

    Pre-fix (no try/except around `_write_instance` in the per-instance
    loop), this failed with the injected RuntimeError propagating all
    the way out of `session.export()`, and GOOD01's record was never
    written because BAD01 (first in patient order) aborted the loop
    before GOOD01 was ever reached.
    """
    import datetime

    from gantry.entities import DicomItem, Instance, Patient, Series, Study
    from gantry.exporters.wfdb import WfdbExporter
    from gantry.io_handlers import populate_attrs
    from gantry.session import DicomSession
    from scripts.generate_waveform_test_data import build_ecg_dataset

    def _make_patient(patient_id):
        ds = build_ecg_dataset(channels=[("MDC_ECG_LEAD_I", "Lead I")],
                               patient_id=patient_id, num_samples=50)
        patient = Patient(patient_id, f"ANON_{patient_id}")
        study = Study(f"1.2.3.{patient_id}.STUDY", datetime.date(2026, 1, 1))
        series = Series(f"1.2.3.{patient_id}.SERIES", "ECG", 1)
        instance = Instance(f"1.2.3.{patient_id}.SOP", ds.SOPClassUID, 1)
        wf_item = DicomItem()
        populate_attrs(ds.WaveformSequence[0], wf_item)
        instance.add_sequence_item("5400,0100", wf_item)
        instance.waveform_array = np.frombuffer(
            ds.WaveformSequence[0].WaveformData, dtype="<i2"
        ).reshape(50, 1).copy()
        series.instances.append(instance)
        study.series.append(series)
        patient.studies.append(study)
        return patient

    sess = DicomSession(":memory:")
    # BAD01 sorts/iterates first -- if the loop aborts on it, GOOD01 is
    # never reached, proving containment (not just exception timing).
    sess.store.patients.append(_make_patient("BAD01"))
    sess.store.patients.append(_make_patient("GOOD01"))

    original_write_instance = WfdbExporter._write_instance

    def _write_instance_maybe_boom(self, folder, patient, study, series, instance,
                                   logger, used_names):
        if patient.patient_id == "BAD01":
            raise RuntimeError("simulated malformed instance from a non-conformant cart")
        return original_write_instance(self, folder, patient, study, series,
                                       instance, logger, used_names)

    monkeypatch.setattr(WfdbExporter, "_write_instance", _write_instance_maybe_boom)

    paths = sess.export(str(tmp_path / "out"), format="wfdb")

    assert len(paths) == 1, (
        f"expected exactly GOOD01's record despite BAD01 failing, got {paths!r}")
    assert "GOOD01" in paths[0]


def test_sanitize_preserves_falsy_zero():
    """`_sanitize`'s `name if name is not None else ""` must not
    collapse a legitimate falsy-but-meaningful value like the int 0
    (InstanceNumber 0 is valid DICOM) to the empty string.

    Pinned directly, not just through `_unique_record_name` (which
    would otherwise mask a regression to the old `name or ""` -- see
    the final review: reverting the falsy-zero fix alone left the full
    97-test targeted suite green).
    """
    from gantry.exporters.wfdb import _sanitize

    assert _sanitize(0) == "0"
    assert _sanitize(None) == "record"
    assert _sanitize("") == "record"


def test_sanitize_description_strips_line_breaking_control_characters():
    """`_sanitize_description` must remove every character class its own
    docstring promises to strip -- CR, LF, vertical tab, form feed, the
    C1 file/group/record/unit separators (\\x1c-\\x1f), NEL (\\x85), and
    the Unicode LINE/PARAGRAPH SEPARATORS (U+2028/U+2029) -- replacing
    each with a space (not deleting it, which would silently glue
    adjacent words together) and trimming the ends.

    Pinned directly against the function, not only through a channel
    label/source fixture: mutating `_sanitize_description` to a no-op
    (`return str(value)`) left the full suite green before this test
    existed, because every fixture that reached it via the lead-name
    allowlist either failed the allowlist first (label replaced with a
    positional token before the sanitizer ever saw the injected
    character) or never carried a control character at all. See
    test_wfdb_conformance.py::test_coded_channel_source_newline_cannot_manufacture_a_hea_comment
    for the one production path (a coded Channel Source value) that
    still reaches this function with attacker-controlled text.
    """
    from gantry.exporters.wfdb import _sanitize_description

    assert _sanitize_description("Lead I\nJane Doe") == "Lead I Jane Doe"
    assert _sanitize_description("Lead I\rJane Doe") == "Lead I Jane Doe"
    assert _sanitize_description("Lead I\x0bJane Doe") == "Lead I Jane Doe"  # vertical tab
    assert _sanitize_description("Lead I\x0cJane Doe") == "Lead I Jane Doe"  # form feed
    assert _sanitize_description("Lead I\x1cJane Doe") == "Lead I Jane Doe"  # file separator
    assert _sanitize_description("Lead I\x1dJane Doe") == "Lead I Jane Doe"  # group separator
    assert _sanitize_description("Lead I\x1eJane Doe") == "Lead I Jane Doe"  # record separator
    assert _sanitize_description("Lead I\x1fJane Doe") == "Lead I Jane Doe"  # unit separator
    assert _sanitize_description("Lead I\x85Jane Doe") == "Lead I Jane Doe"  # NEL
    assert _sanitize_description("Lead I\u2028Jane Doe") == "Lead I Jane Doe"  # LINE SEPARATOR
    assert _sanitize_description("Lead I\u2029Jane Doe") == "Lead I Jane Doe"  # PARAGRAPH SEPARATOR

    # Leading/trailing control characters are stripped away entirely.
    assert _sanitize_description("\nLead I\n") == "Lead I"

    # Ordinary spaces are NOT stripped: the description is the last (9th)
    # field on a header(5) signal line and legally runs to end of line,
    # embedded spaces and all.
    assert _sanitize_description("Lead I taken by Jane Doe") == "Lead I taken by Jane Doe"


def test_write_instance_with_no_sample_data_is_skipped_not_crashed(tmp_path, caplog):
    """An instance that declares a Waveform Sequence but carries no
    sample data (`get_waveform_data()` returns None) must be skipped --
    logged and returned as None -- not crash in
    `np.ascontiguousarray(None, dtype="<i2")`.

    Calls `WfdbExporter._write_instance` DIRECTLY rather than through
    `session.export()`: going through `export()` would let the
    Important-3 per-instance containment fix (try/except around
    `_write_instance`, added in this same review round) silently mask a
    reverted guard -- a `TypeError` from `np.ascontiguousarray(None, ...)`
    would be caught, logged, and skipped either way, so `paths == []`
    would pass whether or not this specific guard exists. Calling the
    method directly means a reverted guard raises straight into this
    test, not into an unrelated safety net.
    """
    import datetime
    import logging

    from gantry.entities import DicomItem, Instance, Patient, Series, Study
    from gantry.exporters.wfdb import WfdbExporter
    from gantry.io_handlers import populate_attrs
    from scripts.generate_waveform_test_data import build_ecg_dataset

    ds = build_ecg_dataset(channels=[("MDC_ECG_LEAD_I", "Lead I")],
                           patient_id="NODATA01", num_samples=50)
    patient = Patient("NODATA01", "ANON_NODATA01")
    study = Study("1.2.3.NODATA.STUDY", datetime.date(2026, 1, 1))
    series = Series("1.2.3.NODATA.SERIES", "ECG", 1)
    instance = Instance("1.2.3.NODATA.SOP", ds.SOPClassUID, 1)
    wf_item = DicomItem()
    populate_attrs(ds.WaveformSequence[0], wf_item)
    instance.add_sequence_item("5400,0100", wf_item)
    # Deliberately leave waveform_array unset (None) and give it no
    # loader -- get_waveform_data() returns None, simulating a Waveform
    # Sequence with no backing sample data.
    assert instance.get_waveform_data() is None

    logger = logging.getLogger("gantry.wfdb_no_sample_data_test")

    with caplog.at_level(logging.WARNING):
        result = WfdbExporter()._write_instance(
            str(tmp_path / "out"), patient, study, series, instance, logger, {})

    assert result is None, f"expected None (skipped), got {result!r}"
    assert any("no sample data" in r.message for r in caplog.records), (
        "expected a warning explaining the instance was skipped")
