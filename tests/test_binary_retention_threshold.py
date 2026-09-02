"""Retention of unrouted binary values keys on size, not on wire VR (#151).

The same private blob used to take opposite paths depending only on the
transfer syntax of the source file. Under Explicit VR it arrived as
`OB`, hit `BINARY_VRS`, and was dropped with a `DATA_LOSS` row; under
Implicit VR the wire carries no VR, pydicom resolves a private tag to
`UN`, and the identical bytes were retained in `attributes` in full --
exported, and never reported. Same bytes, same tag, same code path:

- de-identification became transfer-syntax dependent
  (`remove_private_tags=False` honoured in one syntax and unhonourable
  in the other);
- the memory guarantee had a hole (`# UN left out for safety, usually
  small private tags` -- under Implicit VR, `UN` means *any* private
  value, the megabyte one included);
- the trail was incomplete in the direction that looks clean (the
  implicit twin of a `REVIEW_REQUIRED` cohort graded `PASS`).

Now one documented constant, `BINARY_RETENTION_MAX_BYTES`, decides for
`UN` and the binary VRs alike: a value at or below it is retained on
the graph whatever its wire VR; a value above it is dropped with the
existing `DATA_LOSS` treatment whatever its wire VR. Sequences are
exempt from the size rule -- they are structure, resolved structurally
(#167), and their encoded length is the one place the two syntaxes
genuinely differ. Routed elements (pixels to the sidecar, waveform
samples, the float pair) keep their dedicated handling and never reach
the gate.

The threshold's own justification lives on the constant; what this file
pins is the *symmetry*: every test here runs the same bytes through
both transfer syntaxes and asserts one outcome.
"""
import os
import sqlite3

import numpy as np
import pydicom
import pytest
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import (ExplicitVRLittleEndian, ImplicitVRLittleEndian,
                         generate_uid)

from isocenter.io_handlers import (BINARY_RETENTION_MAX_BYTES,
                                   LOSS_SCOPE_PRIVATE)
from isocenter.session import DicomSession

SC = "1.2.840.10008.5.1.4.1.1.7"
PRIVATE_BINARY = "0009,1002"

SMALL = b"\x01\x02\x03\x04" * 8                    # 32 B, well under
LARGE = b"\xab" * (BINARY_RETENTION_MAX_BYTES + 2)  # just over


def _write_src(folder, syntax, payload):
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = SC
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = syntax

    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientID, ds.PatientName = "PAT1", "DOE^JOHN"
    ds.StudyInstanceUID, ds.SeriesInstanceUID = generate_uid(), generate_uid()
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.SOPClassUID = SC
    ds.Modality, ds.SeriesNumber, ds.InstanceNumber = "OT", 1, 1
    ds.StudyDate = "20230101"
    ds.add_new(0x00090010, 'LO', 'ACME_HEADER')
    ds.add_new(0x00091002, 'OB', payload)

    ds.save_as(os.path.join(folder, "one.dcm"), enforce_file_format=True)
    return ds.SOPInstanceUID


def _ingest(tmp_path, syntax, payload, name, reopen=False,
            remove_private_tags=False):
    src = tmp_path / f"src_{name}"
    src.mkdir()
    _write_src(str(src), syntax, payload)

    db = str(tmp_path / f"{name}.db")
    session = DicomSession(persistence_file=db)
    try:
        session.ingest(str(src))
        session.save()
        db_path = session.store_backend.db_path
    finally:
        session.close()

    session = DicomSession(persistence_file=db)
    try:
        inst = session.store.patients[0].studies[0].series[0].instances[0]
        attrs = dict(inst.attributes)
    finally:
        session.close()

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT details, loss_scope FROM audit_log "
            "WHERE action_type='DATA_LOSS'").fetchall()
    return attrs, rows


def _vr_on_wire(tmp_path, syntax, name):
    """Precondition helper: what VR does pydicom hand the gate?"""
    src = tmp_path / f"probe_{name}"
    src.mkdir()
    _write_src(str(src), syntax, SMALL)
    ds = pydicom.dcmread(os.path.join(str(src), "one.dcm"))
    return ds[0x00091002].VR


def test_the_two_syntaxes_really_produce_different_vrs(tmp_path):
    """The premise: OB on the explicit wire, UN off the implicit one.

    If pydicom ever starts resolving private VRs from its private
    dictionaries here, every symmetry test below goes vacuous -- this is
    the canary.
    """
    assert _vr_on_wire(tmp_path, ExplicitVRLittleEndian, "evr") == "OB"
    assert _vr_on_wire(tmp_path, ImplicitVRLittleEndian, "ivr") == "UN"


@pytest.mark.parametrize("syntax,name", [
    (ExplicitVRLittleEndian, "explicit"),
    (ImplicitVRLittleEndian, "implicit"),
])
def test_a_small_private_blob_is_retained_under_both_syntaxes(
        tmp_path, syntax, name):
    """The explicit half is the behaviour change: a small `OB` used to be
    dropped with a loss row. Retained now, under both syntaxes, with no
    row -- nothing was lost, and a loss entry for a retained value is
    how a trail becomes noise. And retained *durably*: the fixture
    reopens the store, so a value that failed to round-trip the
    persistence tiers would fail here, not in a user's second session
    (#158's lesson).
    """
    attrs, rows = _ingest(tmp_path, syntax, SMALL, f"small_{name}")

    assert attrs.get(PRIVATE_BINARY) == SMALL
    assert rows == [], rows


@pytest.mark.parametrize("syntax,name", [
    (ExplicitVRLittleEndian, "explicit"),
    (ImplicitVRLittleEndian, "implicit"),
])
def test_a_large_private_blob_is_dropped_with_a_loss_row_under_both(
        tmp_path, syntax, name):
    """The implicit half is the behaviour change: a large `UN` used to be
    silently retained -- the memory hole -- and, worse for the trail,
    its explicit twin graded differently. Both now drop, both file the
    loss, and the scope still grades it (#146).
    """
    attrs, rows = _ingest(tmp_path, syntax, LARGE, f"large_{name}")

    assert PRIVATE_BINARY not in attrs
    assert len(rows) == 1, rows
    details, scope = rows[0]
    assert PRIVATE_BINARY in details
    assert scope == LOSS_SCOPE_PRIVATE


def test_a_retained_small_blob_is_exported_under_both_syntaxes(tmp_path):
    """Retention is only worth anything if the bytes reach the file.

    The exported element rides `_fallback_encoding`'s bytes arm (`UN`),
    so both syntaxes produce the same exported value -- which is the
    whole point: the de-identification outcome stops depending on the
    wire format the source site happened to choose.
    """
    exported = {}
    for syntax, name in ((ExplicitVRLittleEndian, "exp_e"),
                         (ImplicitVRLittleEndian, "exp_i")):
        src = tmp_path / f"src_{name}"
        src.mkdir()
        _write_src(str(src), syntax, SMALL)
        out = tmp_path / f"out_{name}"

        session = DicomSession(persistence_file=str(tmp_path / f"{name}.db"))
        try:
            session.configuration.remove_private_tags = False
            session.ingest(str(src))
            session.export(str(out), format="dicom", use_compression=False)
        finally:
            session.close()

        written = [os.path.join(r, f)
                   for r, _d, files in os.walk(str(out))
                   for f in files if f.endswith(".dcm")]
        assert len(written) == 1, written
        exported[name] = pydicom.dcmread(written[0])

    for name, ds in exported.items():
        assert (0x0009, 0x1002) in ds, name
        assert bytes(ds[0x00091002].value) == SMALL, name


def test_the_boundary_sits_exactly_at_the_constant(tmp_path):
    """At the threshold retained, one past it dropped -- so the constant
    is the behaviour, not a suggestion near it."""
    at = b"\xcd" * BINARY_RETENTION_MAX_BYTES
    over = b"\xcd" * (BINARY_RETENTION_MAX_BYTES + 2)

    attrs_at, rows_at = _ingest(
        tmp_path, ImplicitVRLittleEndian, at, "at_threshold")
    attrs_over, rows_over = _ingest(
        tmp_path, ImplicitVRLittleEndian, over, "over_threshold")

    assert attrs_at.get(PRIVATE_BINARY) == at
    assert rows_at == [], rows_at

    assert PRIVATE_BINARY not in attrs_over
    assert len(rows_over) == 1, rows_over


def test_a_recovered_private_sequence_is_not_size_gated(tmp_path):
    """Structure is exempt: a large implicit-VR private *sequence* is
    still restored as a sequence (#167), never dropped as a blob.

    Sequences are the one shape whose encoded length genuinely differs
    between the syntaxes, so a size rule applied to them would keep a
    sequence in one syntax and drop it in the other -- re-creating
    the exact divergence this fix removes, one shape over.
    """
    from pydicom.dataset import Dataset
    from pydicom.sequence import Sequence

    src = tmp_path / "src_seq"
    src.mkdir()

    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = SC
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ImplicitVRLittleEndian

    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientID, ds.PatientName = "PAT1", "DOE^JOHN"
    ds.StudyInstanceUID, ds.SeriesInstanceUID = generate_uid(), generate_uid()
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.SOPClassUID = SC
    ds.Modality, ds.SeriesNumber, ds.InstanceNumber = "OT", 1, 1
    ds.add_new(0x00090010, 'LO', 'ACME_HEADER')
    # Items big enough that the encoded sequence clears the threshold.
    items = []
    for n in range(3):
        item = Dataset()
        # (0020,4000) Image Comments is LT in the standard dictionary,
        # so the implicit-VR reader resolves it correctly inside the
        # recovered sequence. 30000 bytes x 3 items puts the encoded
        # sequence well over the threshold.
        item.add_new(0x00204000, 'LT', f"item-{n}-" + "x" * 30000)
        items.append(item)
    ds.add_new(0x00091010, 'SQ', Sequence(items))
    ds.save_as(os.path.join(str(src), "one.dcm"), enforce_file_format=True)

    session = DicomSession(persistence_file=str(tmp_path / "seq.db"))
    try:
        session.ingest(str(src))
        inst = session.store.patients[0].studies[0].series[0].instances[0]
        seq = inst.sequences.get("0009,1010")
        db_path = session.store_backend.db_path
        assert seq is not None and len(seq.items) == 3, (
            "the implicit-VR private sequence was not restored")
        assert "0009,1010" not in inst.attributes
    finally:
        session.close()

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT details FROM audit_log "
            "WHERE action_type='DATA_LOSS'").fetchall()
    assert not any("0009,1010" in d for (d,) in rows), rows
