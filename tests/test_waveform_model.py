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


# --- Coded ECG lead names (#828) ----------------------------------------
#
# The rows below are transcribed from the source a second time, apart from
# `isocenter/waveform.py`'s table, so a row deleted or mistyped there is a
# disagreement here. Each row is (scheme, Code Value, Code Meaning as the
# source prints it, the WFDB name). MDC rows: PS3.16 2026d, CID 3001
# "ECG Lead" (version 20130613). SCPECG rows: PS3.16 2007, CID 3001
# "ECG Leads" (version 20020904, SCPECG 1.3), the version the 2026d note
# calls "a prior version of this Context Group". Only leads whose name is
# in `KNOWN_LEAD_NAMES` are named.
CID_3001_NAMED_ROWS = [
    ("MDC", "2:1", "Lead I", "I"),
    ("MDC", "2:2", "Lead II", "II"),
    ("MDC", "2:61", "Lead III", "III"),
    ("MDC", "2:62", "aVR, augmented voltage, right", "aVR"),
    ("MDC", "2:63", "aVL, augmented voltage, left", "aVL"),
    ("MDC", "2:64", "aVF, augmented voltage, foot", "aVF"),
    ("MDC", "2:3", "Lead V1", "V1"),
    ("MDC", "2:4", "Lead V2", "V2"),
    ("MDC", "2:5", "Lead V3", "V3"),
    ("MDC", "2:6", "Lead V4", "V4"),
    ("MDC", "2:7", "Lead V5", "V5"),
    ("MDC", "2:8", "Lead V6", "V6"),
    ("MDC", "2:9", "Lead V7", "V7"),
    ("MDC", "2:66", "Lead V8", "V8"),
    ("MDC", "2:67", "Lead V9", "V9"),
    ("MDC", "2:11", "Lead V3R", "V3R"),
    ("MDC", "2:12", "Lead V4R", "V4R"),
    ("MDC", "2:13", "Lead V5R", "V5R"),
    ("MDC", "2:16", "Lead X", "X"),
    ("MDC", "2:17", "Lead Y", "Y"),
    ("MDC", "2:18", "Lead Z", "Z"),
    ("MDC", "2:92", "Modified chest lead per V1 placement", "MCL1"),
    ("MDC", "2:97", "Modified chest lead per V6 placement", "MCL6"),
    ("MDC", "2:131", "EASI Lead ES", "ES"),
    ("MDC", "2:132", "EASI Lead AS", "AS"),
    ("MDC", "2:133", "EASI Lead AI", "AI"),
    ("SCPECG", "5.6.3-9-1", "Lead I (Einthoven)", "I"),
    ("SCPECG", "5.6.3-9-2", "Lead II", "II"),
    ("SCPECG", "5.6.3-9-61", "Lead III", "III"),
    ("SCPECG", "5.6.3-9-62", "Lead aVR", "aVR"),
    ("SCPECG", "5.6.3-9-63", "Lead aVL", "aVL"),
    ("SCPECG", "5.6.3-9-64", "Lead aVF", "aVF"),
    ("SCPECG", "5.6.3-9-3", "Lead V1", "V1"),
    ("SCPECG", "5.6.3-9-4", "Lead V2", "V2"),
    ("SCPECG", "5.6.3-9-5", "Lead V3", "V3"),
    ("SCPECG", "5.6.3-9-6", "Lead V4", "V4"),
    ("SCPECG", "5.6.3-9-7", "Lead V5", "V5"),
    ("SCPECG", "5.6.3-9-8", "Lead V6", "V6"),
    ("SCPECG", "5.6.3-9-9", "Lead V7", "V7"),
    ("SCPECG", "5.6.3-9-66", "Lead V8", "V8"),
    ("SCPECG", "5.6.3-9-67", "Lead V9", "V9"),
    ("SCPECG", "5.6.3-9-11", "Lead V3R", "V3R"),
    ("SCPECG", "5.6.3-9-12", "Lead V4R", "V4R"),
    ("SCPECG", "5.6.3-9-13", "Lead V5R", "V5R"),
    ("SCPECG", "5.6.3-9-16", "Lead X", "X"),
    ("SCPECG", "5.6.3-9-17", "Lead Y", "Y"),
    ("SCPECG", "5.6.3-9-18", "Lead Z", "Z"),
]


def test_the_lead_table_is_the_cid_3001_rows_and_nothing_else():
    """A row deleted, added or mistyped in the module's table disagrees
    with the independent transcription above."""
    from isocenter.waveform import _CID_3001_LEAD_NAMES

    expected = {(scheme, code): name
                for scheme, code, _meaning, name in CID_3001_NAMED_ROWS}
    assert len(expected) == len(CID_3001_NAMED_ROWS), "duplicate row in the test"
    assert dict(_CID_3001_LEAD_NAMES) == expected


@pytest.mark.parametrize("scheme,code,meaning,name", CID_3001_NAMED_ROWS,
                         ids=[f"{s}:{c}" for s, c, _m, _n in CID_3001_NAMED_ROWS])
def test_a_coded_ecg_lead_is_written_by_its_name(scheme, code, meaning, name):
    channel = WaveformChannel(label="", source_code=code, source_scheme=scheme)
    assert channel.wfdb_description(0) == name, (scheme, code, meaning)


@pytest.mark.parametrize("scheme,code,meaning,name",
                         [r for r in CID_3001_NAMED_ROWS if r[0] == "SCPECG"],
                         ids=[c for s, c, _m, _n in CID_3001_NAMED_ROWS if s == "SCPECG"])
def test_scpecg_and_mdc_name_the_same_lead_the_same_way(scheme, code, meaning, name):
    """SCP-ECG `5.6.3-9-N` and MDC `2:N` are the same lead for every row
    both schemes name (the two editions agree), so a digit mistyped in
    either scheme's row shows here as well as in the equality test."""
    from isocenter.waveform import _CID_3001_LEAD_NAMES

    n = code.rsplit("-", 1)[1]
    assert _CID_3001_LEAD_NAMES[("MDC", f"2:{n}")] == name


@pytest.mark.parametrize("scheme,code,meaning,name", CID_3001_NAMED_ROWS,
                         ids=[f"{s}:{c}" for s, c, _m, _n in CID_3001_NAMED_ROWS])
def test_every_name_the_table_writes_is_a_known_lead_name(scheme, code, meaning, name):
    """The table's scope is `KNOWN_LEAD_NAMES`: a coded lead is named only
    when the name is one the Channel Label check would also accept."""
    from isocenter.waveform import _is_known_lead_name
    assert _is_known_lead_name(name)


@pytest.mark.parametrize("scheme,code,meaning", [
    # Same short name, a different lead: a "Lead I" substring match would
    # name these I.
    ("MDC", "2:24", "Frank Lead I"),
    ("SCPECG", "5.6.3-9-24", "Lead I (Frank)"),
    ("MDC", "2:31", "Derived Lead I"),
    ("SCPECG", "5.6.3-9-31", "Lead I-cal (Einthoven)"),
    # In CID 3001, with no name in KNOWN_LEAD_NAMES.
    ("MDC", "2:65", "-aVR"),
    ("SCPECG", "5.6.3-9-65", "Lead -aVR"),
    ("MDC", "2:10", "Lead V2R"),
    ("SCPECG", "5.6.3-9-14", "Lead V6R"),
    ("MDC", "2:0", "Unspecified lead"),
    ("SCPECG", "5.6.3-9-0", "Unspecified lead"),
])
def test_a_cid_3001_row_outside_the_named_set_stays_verbatim(scheme, code, meaning):
    channel = WaveformChannel(label="II", source_code=code, source_scheme=scheme)
    assert channel.wfdb_description(0) == code, meaning


@pytest.mark.parametrize("scheme,code", [
    ("MDC", "MDC_ECG_LEAD_II"),      # the reference ID, not a Code Value
    ("SCPECG", "5.6.3-9-999"),       # no such row
    ("MDC", "2:1 "),                 # not the row's Code Value either
])
def test_an_unknown_code_in_a_known_scheme_stays_verbatim(scheme, code):
    channel = WaveformChannel(label="", source_code=code, source_scheme=scheme)
    assert channel.wfdb_description(0) == code


@pytest.mark.parametrize("scheme,code", [
    ("SRT", "G-DB22"),    # CID 3003, Aortic pressure waveform
    ("DCM", "109117"),    # CID 3005, Respiration Waveform
])
def test_a_non_lead_channel_source_stays_verbatim(scheme, code):
    channel = WaveformChannel(label="", source_code=code, source_scheme=scheme)
    assert channel.wfdb_description(0) == code


@pytest.mark.parametrize("scheme", ["", "99MDC", "99SCPECG", "L", "SCP-ECG"])
def test_a_lead_code_under_another_scheme_stays_verbatim(scheme):
    """The table is keyed on the scheme too: `2:1` means Lead I in MDC and
    nothing in particular anywhere else, and a `99` scheme is local."""
    for code in ("2:1", "5.6.3-9-1"):
        channel = WaveformChannel(label="", source_code=code, source_scheme=scheme)
        assert channel.wfdb_description(0) == code, (scheme, code)


@pytest.mark.parametrize("scheme,code,name", [
    ("scpecg", "5.6.3-9-1", "I"),
    ("Mdc", "2:62", "aVR"),
    (" SCPECG ", "5.6.3-9-64", "aVF"),
])
def test_the_scheme_is_compared_as_the_vocabulary_check_compares_it(scheme, code, name):
    channel = WaveformChannel(label="", source_code=code, source_scheme=scheme)
    assert channel.wfdb_description(0) == name


def test_scpecg_is_a_published_vocabulary():
    from isocenter.waveform import KNOWN_CODING_SCHEMES, _is_known_coding_scheme
    assert "SCPECG" in KNOWN_CODING_SCHEMES
    assert _is_known_coding_scheme("SCPECG")
    assert _is_known_coding_scheme("scpecg")
    assert not _is_known_coding_scheme("99SCPECG")
