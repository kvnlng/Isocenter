import numpy as np
import pytest

from scripts.generate_waveform_test_data import build_ecg_dataset, LEADS


def test_dataset_has_waveform_sequence():
    ds = build_ecg_dataset(num_samples=1000)
    assert ds.Modality == "ECG"
    assert ds.SOPClassUID == "1.2.840.10008.5.1.4.1.1.9.1.1"
    assert len(ds.WaveformSequence) == 1


def test_waveform_item_declares_expected_geometry():
    ds = build_ecg_dataset(num_samples=1000)
    item = ds.WaveformSequence[0]
    assert item.NumberOfWaveformChannels == len(LEADS)
    assert item.NumberOfWaveformSamples == 1000
    assert float(item.SamplingFrequency) == 500.0
    assert item.WaveformBitsAllocated == 16
    assert item.WaveformSampleInterpretation == "SS"


def test_waveform_data_length_matches_geometry():
    ds = build_ecg_dataset(num_samples=1000)
    item = ds.WaveformSequence[0]
    expected_bytes = 1000 * len(LEADS) * 2
    assert len(item.WaveformData) == expected_bytes


def test_channel_definitions_carry_calibration():
    ds = build_ecg_dataset(num_samples=100)
    chdefs = ds.WaveformSequence[0].ChannelDefinitionSequence
    assert len(chdefs) == len(LEADS)
    first = chdefs[0]
    assert float(first.ChannelSensitivity) == pytest.approx(1.0)
    assert float(first.ChannelSensitivityCorrectionFactor) == pytest.approx(1.0)
    assert first.ChannelSensitivityUnitsSequence[0].CodeValue == "uV"
    assert first.ChannelSourceSequence[0].CodeValue == LEADS[0][0]


def test_nonzero_baseline_is_recorded():
    ds = build_ecg_dataset(num_samples=100, baseline_uv=250.0)
    chdef = ds.WaveformSequence[0].ChannelDefinitionSequence[0]
    assert float(chdef.ChannelBaseline) == pytest.approx(250.0)
