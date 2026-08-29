"""Private tags reach the graph and then vanish at export (#118).

`remove_private_tags=False` is the caller saying "keep the vendor block".
Ingest honours it -- the odd-group tags are in the object graph and in the
`instance_attributes` table -- and then `DicomExporter._merge` drops every
one of them on the way out, because `dictionary_VR` raises for a tag the
standard dictionary does not know and the `except` arm only logged.

So the flag appeared to work right up until the exported file was read
back. The audit report listed the tags as retained; the file did not have
them.
"""
import glob
import logging
import os

import numpy as np
import pydicom
import pytest
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.multival import MultiValue
from pydicom.tag import Tag
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from isocenter.io_handlers import DicomExporter
from isocenter.session import DicomSession

# A value longer than LO's 64-character limit, so the fallback cannot
# simply reach for LO and hope.
LONG_VALUE = "acquisition-profile-" + ("x" * 80)


def _write_src(folder, private=None):
    """A minimal single-instance study carrying a private block."""
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.7"
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian

    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientID, ds.PatientName = "PAT1", "DOE^JOHN"
    ds.StudyInstanceUID, ds.SeriesInstanceUID = generate_uid(), generate_uid()
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.SOPClassUID = meta.MediaStorageSOPClassUID
    ds.Modality, ds.SeriesNumber, ds.InstanceNumber = "OT", 1, 1
    ds.StudyDate = "20230101"

    # The control for #165: a *standard* VM > 1 element. `dictionary_VR`
    # resolves it, so it never reaches the fallback -- which is what makes
    # the defect the fallback path specifically rather than list handling.
    ds.ImageOrientationPatient = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]

    ds.add_new(0x00090010, 'LO', 'ACME_HEADER')        # Private Creator
    ds.add_new(0x00091001, 'LO', 'acquisition-v7')
    ds.add_new(0x00091002, 'LT', LONG_VALUE)

    # VM > 1, one per value type a real vendor block carries (#165).
    # pydicom hands these back as `MultiValue`, which is not a `list`
    # subclass, and the fallback had no arm for either.
    ds.add_new(0x00091010, 'US', [1, 2, 3])
    ds.add_new(0x00091011, 'LO', ['alpha', 'beta'])
    ds.add_new(0x00091012, 'DS', [1.5, 2.5])
    ds.add_new(0x00091014, 'IS', [4, 5])
    # Each value inside LO's 64-character cap, their join well past it.
    ds.add_new(0x00091015, 'LO', ['q' * 50, 'r' * 50])

    ds.Rows = ds.Columns = 4
    ds.BitsAllocated = ds.BitsStored = 8
    ds.HighBit = 7
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelRepresentation = 0
    ds.PixelData = np.zeros((4, 4), dtype=np.uint8).tobytes()

    path = os.path.join(folder, "one.dcm")
    ds.save_as(path, enforce_file_format=True)
    return path


def _private(ds):
    """`{"gggg,eeee": value}` for every odd-group tag in `ds`."""
    return {f"{el.tag.group:04x},{el.tag.element:04x}": el.value
            for el in ds if el.tag.group % 2 == 1}


def _roundtrip(tmp_path, anonymize=None):
    """Ingest a private-block study, optionally anonymize, export, re-read.

    `anonymize` is None (export straight through) or the value to give
    `remove_private_tags` before running the privacy pipeline.
    """
    src = tmp_path / "src"
    src.mkdir()
    _write_src(str(src))
    out = tmp_path / "out"

    session = DicomSession(persistence_file=str(tmp_path / "priv.db"))
    try:
        session.ingest(str(src))
        if anonymize is not None:
            session.configuration.remove_private_tags = anonymize
            session.audit()
            session.anonymize()
        session.export(str(out), format="dicom", show_progress=False)
    finally:
        session.close()

    written = glob.glob(str(out / "**" / "*.dcm"), recursive=True)
    assert written, "export produced no .dcm files"
    return pydicom.dcmread(written[0])


def test_a_private_tag_survives_export(tmp_path):
    """The bug in one line: ingested, held, exported without it."""
    assert _private(_roundtrip(tmp_path)).get("0009,1001") == "acquisition-v7"


def test_the_private_creator_survives_export(tmp_path):
    """Without (gggg,0010) the block is an anonymous byte range.

    A private tag whose Private Creator was dropped cannot be attributed
    to a vendor, which makes the value that *was* kept unreadable.
    """
    assert _private(_roundtrip(tmp_path)).get("0009,0010") == "ACME_HEADER"


def test_a_value_too_long_for_LO_survives_export(tmp_path):
    """The fallback VR has to be chosen from the value, not assumed.

    `LO` caps at 64 characters, so a fallback that always reached for it
    would round-trip the short tag in the test above and quietly mangle
    this one.
    """
    assert _private(_roundtrip(tmp_path)).get("0009,1002") == LONG_VALUE


def test_removing_private_tags_still_removes_them(tmp_path):
    """The other half of the flag, which was never broken -- and must not
    become broken by teaching the exporter to write these."""
    kept = _private(_roundtrip(tmp_path, anonymize=True))
    assert "0009,1001" not in kept, kept


def test_keeping_private_tags_keeps_them_through_anonymization(tmp_path):
    """#118 as reported: the flag says keep, the file says otherwise."""
    kept = _private(_roundtrip(tmp_path, anonymize=False))
    assert kept.get("0009,1001") == "acquisition-v7", kept


def test_a_binary_private_value_is_written_as_UN(tmp_path):
    """PS3.5 A.1: an unknown private value of raw bytes is `UN`.

    `UN` cannot hold a `str` -- pydicom raises at write time, not at
    `add_new` -- so this is the only branch it is right for.
    """
    ds = pydicom.Dataset()
    DicomExporter._merge(ds, {"0009,1003": b"\x01\x02\x03\x04"})

    assert ds[0x00091003].VR == "UN"
    assert ds[0x00091003].value == b"\x01\x02\x03\x04"


def test_an_unwritable_element_is_named_as_data_loss(caplog):
    """The fallback is not total, and the residue must not read as routine.

    `_merge` runs inside `_export_instance_worker`, which may be a
    subprocess, so there is no store handle to write an audit entry
    against (#126). The log line is all there is; it has to say that
    something was lost.
    """
    with caplog.at_level(logging.WARNING):
        DicomExporter._merge(pydicom.Dataset(), {"0009,1004": object()})

    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("0009,1004" in m for m in msgs), msgs
    assert any("not exported" in m.lower() for m in msgs), msgs


def test_merging_the_same_attributes_twice_is_idempotent():
    """`_merge` must not be able to poison its own input.

    It rebinds `v` when the fallback encodes a value, so a second pass
    over the same mapping must not be handed what the first pass
    produced. It is not -- the rebind is local and the source dict is
    untouched.

    Until #179 the reason was blunt: `_export_instance_worker` merged
    `inst.attributes` twice outright. That copy-paste is gone, and the
    property is still worth pinning, because the four surviving merges
    overlap by tag and `write_tree()` merges the same instance mapping
    on its own path. None of the round-trip tests above would notice if
    the rebind started leaking.
    """
    attrs = {"0009,1003": b"\x01\x02", "0009,1004": "x" * 80,
             "0009,1005": 5, "0009,1006": MultiValue(str, ["a", "b"])}
    before = dict(attrs)

    ds = pydicom.Dataset()
    DicomExporter._merge(ds, attrs)
    DicomExporter._merge(ds, attrs)

    assert attrs == before, "_merge mutated the attributes it was given"
    assert (ds[0x00091003].VR, ds[0x00091003].value) == ("UN", b"\x01\x02")
    assert (ds[0x00091004].VR, ds[0x00091004].value) == ("UT", "x" * 80)
    assert (ds[0x00091005].VR, ds[0x00091005].value) == ("LO", "5")
    assert (ds[0x00091006].VR, list(ds[0x00091006].value)) == ("LO", ["a", "b"])


# --------------------------------------------------------------------
# VM > 1 (#165)
#
# #118 closed on "private tags reach the exported file". They reached it
# at VM = 1. `_fallback_encoding` dispatched on the Python type of the
# value and had an arm for `bytes`, `bool`, `int`/`float` and `str` and
# none for a sequence of any of them, so it returned None -- the "nothing
# fits" signal -- and `_merge` turned that into a `DATA_LOSS` entry and
# wrote no element. Multi-valued private elements are ordinary in real
# vendor blocks, so `remove_private_tags=False` kept the scalar half of
# the block and dropped the rest.
#
# Loudly, which is the one thing that was already right: the drop files
# `DATA_LOSS` / `PRIVATE` and grades `REVIEW_REQUIRED` (#146, #148). The
# fix is to stop losing the values, not to change how the loss is
# reported.
# --------------------------------------------------------------------

@pytest.mark.parametrize("tag, expected", [
    ("0009,1010", ["1", "2", "3"]),          # source US
    ("0009,1011", ["alpha", "beta"]),        # source LO
    ("0009,1012", ["1.5", "2.5"]),           # source DS
    ("0009,1014", ["4", "5"]),               # source IS
])
def test_a_multi_valued_private_tag_survives_export(tmp_path, tag, expected):
    """#165 as reported: present in the graph, absent from the file.

    The values come back as strings because nothing restores the source
    VR -- that is #154, and deciding it here would be deciding it on the
    wrong ticket. What this asserts is that the *values* and their
    multiplicity survive, which they did not.
    """
    kept = _private(_roundtrip(tmp_path))
    assert list(kept.get(tag, [])) == expected, kept


def test_a_multi_valued_private_tag_is_not_reported_as_data_loss(tmp_path,
                                                                caplog):
    """The loud loss has to stop being filed, not just stop being true.

    A `DATA_LOSS` entry scoped `PRIVATE` grades the whole run
    `REVIEW_REQUIRED` (#146). Leaving one behind for an element that is
    now in the file would put a reviewer in front of a loss that did not
    happen.
    """
    with caplog.at_level(logging.WARNING):
        _roundtrip(tmp_path)

    msgs = [r.getMessage() for r in caplog.records
            if "not exported" in r.getMessage()]
    assert not msgs, msgs


def test_a_standard_multi_valued_tag_is_unaffected(tmp_path):
    """The control, and the reason #165 is a fallback defect.

    `ImageOrientationPatient` is VM 6 and survived the whole time, on the
    same instance, in the same export, because `dictionary_VR` answers
    for it and the fallback is never consulted. Isocenter is not bad at
    lists; it was bad at lists whose VR it had to choose. Asserted here
    so a future change to `_fallback_multivalue` cannot start capturing
    standard elements without a test noticing.
    """
    ds = _roundtrip(tmp_path)
    assert list(ds.ImageOrientationPatient) == [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
    assert ds[0x00200037].VR == "DS"


def test_a_multi_valued_value_is_written_as_one_element_not_a_join(tmp_path):
    """Multiplicity is DICOM's, not a string the exporter builds.

    A joined `"alpha\\\\beta"` under a VM-1 VR would read back as one
    value containing a literal backslash, which is a different element
    from the two-valued one that was ingested.
    """
    value = _private(_roundtrip(tmp_path))["0009,1011"]
    assert not isinstance(value, str), value
    assert len(value) == 2, value


# --- the encoder itself, where the arms are ---

def test_the_fallback_encodes_a_MultiValue():
    """The in-memory shape. `MultiValue` is what pydicom hands back for a
    multi-valued element, and it is a `MutableSequence`, *not* a `list`
    subclass -- an `isinstance(value, list)` arm would miss the path
    `session.export()` actually takes."""
    assert DicomExporter._fallback_encoding(
        MultiValue(str, ["alpha", "beta"])) == ('LO', ["alpha", "beta"])


def test_the_fallback_encodes_a_plain_list():
    """The reloaded shape (#158). `load_vertical_attributes` reassembles
    the EAV rows as a list of strings, so after a save/close/reopen the
    same tag arrives here as a `list` rather than a `MultiValue`."""
    assert DicomExporter._fallback_encoding(
        ["alpha", "beta"]) == ('LO', ["alpha", "beta"])


def test_the_LO_cap_is_checked_per_value_not_against_the_join():
    """PS3.5 6.2 caps each *value* of a multi-valued element at 64
    characters, not their backslash-joined encoding.

    Measured, not assumed: pydicom writes and reads back two 50-character
    values under `LO` -- a 101-byte encoding -- with no warning under both
    implicit and explicit VR. Escalating to `UT` on the joined length
    would collapse a conformant two-valued element into one string
    carrying a literal backslash, for no gain.
    """
    assert DicomExporter._fallback_encoding(
        ['q' * 50, 'r' * 50]) == ('LO', ['q' * 50, 'r' * 50])


def test_the_LO_cap_admits_a_value_of_exactly_64_characters():
    """The boundary itself, because `<= 64` and `< 64` are one keystroke
    apart and only one of them is right.

    Added in review of #165: nothing pinned the edge. The fixture's
    longest multi-valued atom is 50 characters and the next case up is
    80, so changing the comparison in `_fallback_multivalue` from `<=` to
    `<` left the whole suite green while quietly sending a conformant
    two-valued element to `UT` and collapsing its multiplicity to 1.
    Measured as a mutant that SURVIVED before this test and is killed by
    it.

    64 is legal and 65 is not, measured rather than read off the spec:
    pydicom writes and reads `['a' * 64, 'b' * 64]` back under `LO` with
    no warning, and warns "The value length (65) exceeds the maximum
    length of 64 allowed for VR LO" one character later.
    """
    assert DicomExporter._fallback_encoding(['a' * 64, 'b' * 64]) == (
        'LO', ['a' * 64, 'b' * 64])
    # One past the cap and no text VR holds both the length and the
    # multiplicity, so the element collapses to `UT`.
    assert DicomExporter._fallback_encoding(['a' * 65, 'b'])[0] == 'UT'


def test_a_value_too_long_for_LO_collapses_the_whole_element_to_UT():
    """`UT` has a value multiplicity of 1, so this is a real trade.

    One over-long value means no text VR can hold the element *and* its
    multiplicity: `LO` is 1-n but caps at 64, `UT` is unbounded but VM 1.
    The element is joined into a single `UT` string carrying literal
    backslashes, which is exactly what `_fallback_encoding` already does
    to an over-long single value that contains them. Losing the arity is
    worse than losing nothing and better than losing the values.
    """
    vr, value = DicomExporter._fallback_encoding(['x' * 80, 'y'])
    assert vr == 'UT'
    assert value == 'x' * 80 + '\\' + 'y'


def test_an_empty_multi_valued_private_tag_is_a_zero_length_element():
    """An empty value is a legal element, and it was a `DATA_LOSS` entry.

    Behaviour change beyond the headline of #165, so it is stated rather
    than absorbed: `[]` used to reach the "no VR fits" arm and be
    reported as loss. A zero-length `LO` says "this tag was here and had
    no value", which is what the graph held.
    """
    assert DicomExporter._fallback_encoding([]) == ('LO', [])


def test_a_multi_valued_value_the_encoder_cannot_take_is_still_a_loss():
    """One unencodable value takes its siblings with it, on purpose.

    There is no half-written element in DICOM, and a partial element --
    two of three vendor values, silently -- is the corruption shape #165
    is explicitly not trading a loud loss for. `bytes` inside a sequence
    is the same case: `UN` is an OB-family VR with no multiplicity, so
    there is nothing to encode a list of blobs as.
    """
    assert DicomExporter._fallback_encoding(["alpha", object()]) is None
    assert DicomExporter._fallback_encoding([b"\x01", b"\x02"]) is None
    assert DicomExporter._fallback_encoding([["nested"], "x"]) is None


def test_a_multi_valued_AT_stringifies_exactly_as_a_single_one_does():
    """#154's open question is not answered here, in either direction.

    `Tag` is an `int` subclass whose `str` is `'(0010,0010)'`, so a
    multi-valued `AT` now takes the same stringification the VM = 1 case
    has taken since #118. Special-casing it would fork the two
    multiplicities and pre-empt the VR decision that is #154's to make.
    """
    single = DicomExporter._fallback_encoding(Tag(0x0010, 0x0010))
    multi = DicomExporter._fallback_encoding(
        MultiValue(Tag, [Tag(0x0010, 0x0010), Tag(0x0010, 0x0020)]))

    assert single == ('LO', '(0010,0010)')
    assert multi == ('LO', ['(0010,0010)', '(0010,0020)'])
