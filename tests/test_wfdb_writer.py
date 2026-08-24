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
