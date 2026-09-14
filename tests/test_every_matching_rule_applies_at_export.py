"""Every redaction rule `redact()` applies, the export applies too (#580).

`redact()` runs every rule, and a rule whose serial is `"*"` covers every
series with a serial. The export looked its zones up with
`Configuration.get_rule(serial)` -- exact spelling, first match -- so,
measured at c8b4f15 and again at 220a20f on 3.12.14 (CT_small carrying
`DeviceSerialNumber SN-580`, native export):

- a `"*"` rule not yet run through `redact()` exported the zone
  **intact**, with no row, and the store-wide icon gate read False;
- an exact rule plus a `"*"` rule, or two exact rules on one serial,
  exported the first rule's zone zeroed and the second's **intact**;
- a zone written `{"roi": [...]}` -- the shape the shipped knowledge base
  and the scaffolder use, which `load_config` accepts and `redact()`
  applies -- failed **every** export of a matching instance with
  `ValueError: invalid literal for int() with base 10: 'roi'`, with or
  without `redact()`.

Now one matcher (`services.rules_matching`, over `rule_applies_to`) and
one zone parser (`services.zone_rois`) serve `redact()`'s target walk,
the export's zones and the icon gate. `get_rule` is unchanged. The
matcher lives in `services.py`, which the mutation probe does not reach;
its mutants were run by hand (PR body).
"""
import itertools
import os

import numpy as np
import pydicom
import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from isocenter.services import rule_applies_to, rules_matching, zone_rois
from isocenter.session import DicomSession

SC = "1.2.840.10008.5.1.4.1.1.7"
ZONE_A = [0, 8, 0, 8]
ZONE_B = [16, 24, 16, 24]
_serial = itertools.count(1)


def _write(directory, *, serial="SN-580", manufacturer="ACME"):
    """A 32x32 all-255 Secondary Capture carrying `serial`."""
    directory.mkdir(parents=True, exist_ok=True)
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = SC
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = Dataset()
    ds.file_meta = meta
    ds.SOPClassUID = SC
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.StudyInstanceUID = generate_uid()
    ds.SeriesInstanceUID = generate_uid()
    ds.PatientID = "PAT580"
    ds.PatientName = "Doe^Jane"
    ds.StudyDate = "20230101"
    ds.StudyTime = "120000"
    ds.Modality = "OT"
    if manufacturer is not None:
        ds.Manufacturer = manufacturer
        ds.ManufacturerModelName = "Model-580"
    if serial is not None:
        ds.DeviceSerialNumber = serial
    ds.Rows = ds.Columns = 32
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    ds.PixelData = np.full((32, 32), 255, np.uint8).tobytes()
    ds.save_as(str(directory / f"{next(_serial)}.dcm"),
               enforce_file_format=True)
    return directory


def _state(arr, zone):
    r1, r2, c1, c2 = zone
    return "zeroed" if not arr[r1:r2, c1:c2].any() else "intact"


def _run(tmp_path, rules, *, serial="SN-580", redact=False):
    """Ingest one image, load `rules`, optionally redact, export.

    Returns (exported arrays, icon gate, redact() count, audit summary).
    """
    source = _write(tmp_path / "src", serial=serial)
    out = tmp_path / "out"
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(str(source))
        session.configuration.rules = rules
        applied = session.redact() if redact else None
        gate = session._foreign_icon_gate()
        summary = session.export(str(out), use_compression=False,
                                 show_progress=False)
        rows = session.store_backend.get_audit_summary()
    assert summary.written == 1, summary.failures
    arrays = [pydicom.dcmread(os.path.join(d, f)).pixel_array
              for d, _, names in os.walk(out) for f in names
              if f.endswith(".dcm")]
    return arrays, gate, applied, rows


# ---------------------------------------------------------------------------
# The export applies what redact() applies
# ---------------------------------------------------------------------------

def test_a_star_rule_applies_at_export_without_redact(tmp_path):
    """`"*"` zeroes its zone at export, and the store-wide icon gate
    reads True because the rule matches a series in the store (#542's
    narrowing, which the gate keeps). Killing mutation: the matcher
    exact-only (zone intact, gate False)."""
    arrays, gate, _, _ = _run(tmp_path, [
        {"serial_number": "*", "redaction_zones": [ZONE_A]}])

    assert [_state(a, ZONE_A) for a in arrays] == ["zeroed"]
    assert [_state(a, ZONE_B) for a in arrays] == ["intact"]
    assert gate is True


@pytest.mark.parametrize("second", ["*", "SN-580"])
def test_every_matching_rule_applies_at_export(tmp_path, second):
    """An exact rule followed by `"*"`, or by a second exact rule on the
    same serial: both zones are zeroed. Killing mutation: first match
    only (zone B intact)."""
    arrays, _, _, _ = _run(tmp_path, [
        {"serial_number": "SN-580", "redaction_zones": [ZONE_A]},
        {"serial_number": second, "redaction_zones": [ZONE_B]}])

    assert [(_state(a, ZONE_A), _state(a, ZONE_B)) for a in arrays] == [
        ("zeroed", "zeroed")]


@pytest.mark.parametrize("redact", [False, True])
def test_a_dict_zone_exports(tmp_path, redact):
    """`{"roi": [...]}`, with and without `redact()`: the file is
    delivered with the zone zeroed. Killing mutation: `zone_rois` reading
    list zones only (a dict zone is dropped and exported intact) -- and
    before #580, raw zones reaching `apply_redaction_to_array`, which
    failed the export."""
    arrays, _, applied, _ = _run(tmp_path, [
        {"serial_number": "SN-580", "redaction_zones": [{"roi": ZONE_A}]}],
        redact=redact)

    if redact:
        assert applied == 1
    assert [_state(a, ZONE_A) for a in arrays] == ["zeroed"]


def test_a_series_with_no_serial_matches_no_rule(tmp_path):
    """A `"*"` rule over a series with no Device Serial Number: `redact()`
    applies nothing, the export applies nothing, and the icon gate stays
    False -- the index `redact()` walks holds only series with a serial,
    and the export agrees with it. `_redaction_zones_for` hands a
    serial-less series `None` rather than answering for it, so this
    reaches the matcher: killing mutation, `"*"` matching a missing
    serial (the export zeroes what `redact()` did not, and the gate reads
    True)."""
    arrays, gate, applied, _ = _run(tmp_path, [
        {"serial_number": "*", "redaction_zones": [ZONE_A]}],
        serial=None, redact=True)

    assert applied == 0
    assert [_state(a, ZONE_A) for a in arrays] == ["intact"]
    assert gate is False


def test_export_after_redact_with_a_star_rule_is_idempotent(tmp_path):
    """`redact()` then export under `"*"`: the zone is applied again at
    export, which zeroes zeros. The pixels equal an exact rule's run, and
    so does every REDACTION, WARNING and ERROR row -- the export writes no
    second attestation and no row of its own for re-applying a zone."""
    star = tmp_path / "star"
    exact = tmp_path / "exact"
    star.mkdir()
    exact.mkdir()
    star_arrays, _, star_applied, star_rows = _run(star, [
        {"serial_number": "*", "redaction_zones": [ZONE_A]}], redact=True)
    exact_arrays, _, exact_applied, exact_rows = _run(exact, [
        {"serial_number": "SN-580", "redaction_zones": [ZONE_A]}],
        redact=True)

    assert star_applied == exact_applied == 1
    assert len(star_arrays) == len(exact_arrays) == 1
    assert np.array_equal(star_arrays[0], exact_arrays[0])

    def counted(summary):
        return {kind: summary.get(kind, 0)
                for kind in ("REDACTION", "WARNING", "ERROR")}

    assert counted(star_rows) == counted(exact_rows)
    assert counted(star_rows)["REDACTION"] == 1, star_rows


# ---------------------------------------------------------------------------
# The matcher and the parser
# ---------------------------------------------------------------------------

def test_the_matcher_is_exact_or_star_and_needs_a_serial():
    """Killing mutations: `"*"` read as a literal; a prefix or
    case-insensitive compare; a falsy series serial matched by `"*"`; a
    rule with no serial matching anything."""
    assert rule_applies_to("SN-1", "SN-1")
    assert rule_applies_to("*", "SN-1")
    assert not rule_applies_to("SN-1", "SN-2")
    assert not rule_applies_to("sn-1", "SN-1")
    assert not rule_applies_to("SN", "SN-1")
    for missing in (None, ""):
        assert not rule_applies_to("*", missing)
        assert not rule_applies_to(missing, "SN-1")
        assert not rule_applies_to(missing, missing)


def test_every_matching_rule_is_returned_in_rule_order():
    """Killing mutations: first match only; `"*"` rules moved first."""
    rules = [{"serial_number": "SN-1", "redaction_zones": [[1, 2, 3, 4]]},
             {"serial_number": "SN-2", "redaction_zones": [[5, 6, 7, 8]]},
             {"serial_number": "*", "redaction_zones": [[9, 10, 11, 12]]},
             {"redaction_zones": [[0, 1, 0, 1]]},
             {"serial_number": "SN-1", "redaction_zones": []}]

    assert rules_matching(rules, "SN-1") == [rules[0], rules[2], rules[4]]
    assert rules_matching(rules, "SN-3") == [rules[2]]
    assert rules_matching(rules, None) == []


def test_the_zone_parser_takes_both_shapes_and_keeps_the_values():
    """A list zone and a `{"roi": ...}` zone both parse; anything without
    four values is reported and dropped. The values pass through as given
    (a tuple, no `int()`), because `prepare_redaction_tasks` hashes them
    into the attestation. A tuple *zone* is not a shape: `load_config`
    refuses one and nothing builds one, so the parser reports it as it
    reports any other non-list, non-dict zone. Killing mutations:
    list-only; a tuple zone accepted; `len >= 4`; coercion to `int`; the
    invalid callback not called."""
    invalid = []
    rois = zone_rois(
        [[1, 2, 3, 4], {"roi": [5, 6, 7, 8], "note": "banner"},
         [1, 2, 3], {"roi": [1, 2, 3, 4, 5]}, {"note": "no roi"},
         (9, 9, 9, 9), ["1", "2", "3", "4"]],
        on_invalid=invalid.append)

    assert rois == [(1, 2, 3, 4), (5, 6, 7, 8), ("1", "2", "3", "4")]
    assert invalid == [[1, 2, 3], [1, 2, 3, 4, 5], None, None]
    assert zone_rois([]) == [] and zone_rois(None) == []
