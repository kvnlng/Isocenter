import numpy as np
import pytest

from isocenter.waveform import (
    Waveform,
    WaveformChannel,
    decode_samples,
    UnsupportedInterpretation,
)


def test_decode_signed_16_bit_roundtrips():
    original = np.array([[1, 2], [3, 4], [-5, -6]], dtype=np.int16)
    decoded = decode_samples(original.tobytes(), "SS", 3, 2)
    assert decoded.dtype == np.int16
    assert decoded.shape == (3, 2)
    np.testing.assert_array_equal(decoded, original)


def test_decode_unsigned_16_bit_shifts_to_signed_range():
    raw = np.array([[0, 65535]], dtype=np.uint16)
    decoded = decode_samples(raw.tobytes(), "US", 1, 2)
    assert decoded.dtype == np.int16
    assert decoded[0, 0] == -32768
    assert decoded[0, 1] == 32767


def test_decode_signed_8_bit_widens():
    raw = np.array([[-128, 127]], dtype=np.int8)
    decoded = decode_samples(raw.tobytes(), "SB", 1, 2)
    assert decoded.dtype == np.int16
    np.testing.assert_array_equal(decoded, np.array([[-128, 127]], dtype=np.int16))


def test_companded_audio_is_rejected():
    with pytest.raises(UnsupportedInterpretation):
        decode_samples(b"\x00\x01", "MB", 1, 2)
    with pytest.raises(UnsupportedInterpretation):
        decode_samples(b"\x00\x01", "AB", 1, 2)


def test_decode_rejects_wrong_length_payload():
    with pytest.raises(ValueError):
        decode_samples(b"\x00\x01\x02", "SS", 4, 2)


def test_gain_is_reciprocal_of_effective_sensitivity():
    ch = WaveformChannel(label="II", source_code="MDC_ECG_LEAD_II",
                         source_scheme="MDC", sensitivity=0.005,
                         correction_factor=1.0, units="mV", baseline=0.0)
    assert ch.gain() == pytest.approx(200.0)


def test_correction_factor_participates_in_gain():
    ch = WaveformChannel(label="II", source_code="MDC_ECG_LEAD_II",
                         source_scheme="MDC", sensitivity=0.01,
                         correction_factor=0.5, units="mV", baseline=0.0)
    assert ch.gain() == pytest.approx(200.0)


def test_zero_baseline_maps_to_zero_adc():
    ch = WaveformChannel(label="II", source_code="MDC_ECG_LEAD_II",
                         source_scheme="MDC", sensitivity=0.005,
                         correction_factor=1.0, units="mV", baseline=0.0)
    assert ch.wfdb_baseline() == 0


def test_nonzero_baseline_maps_to_negated_adc_offset():
    # physical = adc/gain + baseline  =>  physical == 0 at adc = -baseline*gain
    ch = WaveformChannel(label="II", source_code="MDC_ECG_LEAD_II",
                         source_scheme="MDC", sensitivity=0.005,
                         correction_factor=1.0, units="mV", baseline=0.5)
    assert ch.wfdb_baseline() == -100


def test_from_dicom_item_reads_the_generated_fixture():
    from isocenter.io_handlers import populate_attrs
    from isocenter.entities import DicomItem
    from scripts.generate_waveform_test_data import build_ecg_dataset, LEADS

    ds = build_ecg_dataset(num_samples=200, baseline_uv=0.0)
    item = DicomItem()
    populate_attrs(ds.WaveformSequence[0], item)

    wf = Waveform.from_dicom_item(item)
    assert wf.num_channels == len(LEADS)
    assert wf.num_samples == 200
    assert wf.sampling_frequency == pytest.approx(500.0)
    assert wf.bits_allocated == 16
    assert wf.sample_interpretation == "SS"
    assert len(wf.channels) == len(LEADS)
    assert wf.channels[0].source_code == "MDC_ECG_LEAD_I"
    assert wf.channels[0].units == "uV"


def test_coded_source_still_wins():
    """A coded value is preferred over free text whenever one is present.

    Not because it cannot contain operator text -- it demonstrably can
    (see `test_coded_channel_source_newline_cannot_manufacture_a_hea_comment`
    in `tests/test_wfdb_conformance.py`, which injects one) -- but because
    a conformant coding scheme value is far less likely to carry it than
    an unconstrained free-text label.
    """
    channel = WaveformChannel(label="anything at all", source_code="MDC_ECG_LEAD_II")
    assert channel.wfdb_description(0) == "MDC_ECG_LEAD_II"


def test_recognisable_lead_names_survive():
    """Real lead names stay in the header -- a positional token for every
    uncoded channel would make records much harder to interpret."""
    for label in ["II", "v5", "aVR", "Lead I", " III "]:
        channel = WaveformChannel(label=label)
        assert channel.wfdb_description(3) == label.strip(), (
            f"{label!r} is a valid lead name and should survive verbatim")


def test_free_text_label_is_replaced_with_a_positional_token():
    """Operator free text must never reach the header, coded or not."""
    for label in [
        "OPERATOR NOTE Smith^John DOB19800101",
        "Lead I taken by Jane Doe",
        "II - patient moved",
        "MRN-12345678",
    ]:
        channel = WaveformChannel(label=label)
        assert channel.wfdb_description(3) == "ch3", (
            f"{label!r} is not a lead name and must be replaced")


def test_absent_label_is_positional():
    assert WaveformChannel(label="").wfdb_description(2) == "ch2"


def test_index_is_optional_for_callers_that_lack_one():
    assert WaveformChannel(label="").wfdb_description() == "signal"


def test_a_locally_defined_99_designator_is_never_treated_as_published(monkeypatch):
    """The "99" prefix is checked, not merely absent from the allowlist.

    DICOM PS3.3 reserves designators beginning "99" for locally defined
    schemes. The tempting fix for "our site's codes are being suppressed"
    is to add the designator to KNOWN_CODING_SCHEMES, which would reopen
    exactly the free-text passthrough the allowlist exists to close -- so
    the prefix rule holds independently of the set's contents.
    """
    from isocenter import waveform

    monkeypatch.setattr(waveform, "KNOWN_CODING_SCHEMES",
                        frozenset({"SCT", "99ACME"}))

    assert waveform._is_known_coding_scheme("SCT")
    assert not waveform._is_known_coding_scheme("99ACME")
