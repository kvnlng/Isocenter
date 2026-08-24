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
    Patient/Study/Series tree the DICOM exporter builds. Proves it by
    running BOTH exporters over the same session and asserting the .hea
    file and the .dcm file end up in the identical directory -- not just
    that some file exists somewhere.
    """
    import datetime
    import os
    from unittest.mock import patch

    from gantry.entities import DicomItem, Instance, Patient, Series, Study
    from gantry.exporters import get_exporter
    from gantry.io_handlers import DicomExporter, populate_attrs
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
        "0008,103E": "12-Lead ECG",       # Series Description
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
    # exercising DicomExporter.save_patient without parallel-worker /
    # IOD-validation noise unrelated to folder placement.
    with patch('gantry.io_handlers.run_parallel',
              side_effect=lambda func, items, *a, **k: [func(i) for i in items]), \
         patch.object(IODValidator, "validate", lambda ds: []):
        DicomExporter.save_patient(patient, str(out_dir))

    dcm_files = list(out_dir.rglob("*.dcm"))
    assert len(dcm_files) == 1
    dcm_dir = os.path.dirname(str(dcm_files[0]))

    hea_paths = get_exporter("wfdb").export(sess, str(out_dir))
    assert len(hea_paths) == 1
    hea_dir = os.path.dirname(hea_paths[0])

    assert hea_dir == dcm_dir, (
        f"WFDB record landed in {hea_dir!r} but the DICOM exporter's "
        f"tree for the same series is {dcm_dir!r} -- the two exporters "
        "must share one folder-naming helper so their trees co-locate.")

    rel_parts = os.path.relpath(hea_dir, str(out_dir)).split(os.sep)
    assert rel_parts[0] == "Subject_COLOC01"
    assert rel_parts[1].startswith("Study_20260101_")
    assert rel_parts[2].startswith("Series_3_")
