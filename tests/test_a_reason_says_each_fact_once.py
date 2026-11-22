"""A recorded reason says each fact once, and holds no memory address (#1089, #1091).

**#1089.** `describe_exception` appends ` (caused by <cause>)` to every
exception with a direct cause. The imagecodecs fallback's refusal
already quotes its cause in its message -- pydicom's words first, the
fallback's error after -- so the tail said one of them again. Measured
on 3e4ee91a (3.12.14 and 3.14.7t), the cohort's
`fewer_fragments_than_frames-short`:

    Decompression Failed: ValueError: Unable to generate frames ... may
    be incorrect; imagecodecs could not decode it either: ValueError:
    Unable to generate frames ... may be incorrect (caused by
    ValueError: Unable to generate frames ... may be incorrect)

The tail is left out when the message already holds the cause: it
contains the cause's own one-line description, or it begins with the
cause's message (which is not blank). Never a bare substring test of the
cause's message, so `OSError("EIO")` behind a message that says "EIO"
by chance is still named. `(caused by StopIteration)` stays: an empty
message matches neither test. Where the fallback's decode fails in
exactly pydicom's words, it says "for the same reason" rather than
quote them a second time.

**#1091.** pydicom's Pillow plugin quotes `<_io.BytesIO object at
0x122f547c0>`, an address that differs on every run, so one file under
one policy wrote a different row each time. Every recorded reason goes
through `describe_exception`, which now writes it `<_io.BytesIO
object>`.

Names `isocenter.logger` and `isocenter.io_handlers`.
"""
import os
import sqlite3

import imagecodecs
import numpy as np
import pydicom
import pytest
from pydicom.data import get_testdata_file
from pydicom.encaps import encapsulate

from isocenter.entities import Instance
from isocenter.io_handlers import ingest_worker
from isocenter.logger import (describe_exception,
                              describe_exception_without_paths)
from isocenter.session import DicomSession
from support.decode_doors import J2K_LOSSLESS, SOP_CLASS, dataset, write

FEWER = ("Unable to generate frames from the encapsulated pixel data as "
         "there are fewer fragments than frames; the dataset may be "
         "corrupt or the number of frames may be incorrect")


def _raised(exc, cause):
    """`exc`, raised from `cause` and caught, so `__cause__` is set."""
    try:
        raise exc from cause
    except BaseException as caught:  # pylint: disable=broad-except
        return caught


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("describe", [describe_exception,
                                      describe_exception_without_paths])
def test_a_cause_the_message_begins_with_is_not_repeated(describe):
    """The fallback's shape: pydicom's message first, without its type."""
    cause = ValueError("pydicom could not read it")
    exc = _raised(RuntimeError(f"{cause}; imagecodecs could not decode it "
                               f"either: Jpeg2kError: bad"), cause)
    assert describe(exc) == (
        "RuntimeError: pydicom could not read it; imagecodecs could not "
        "decode it either: Jpeg2kError: bad")


@pytest.mark.parametrize("describe", [describe_exception,
                                      describe_exception_without_paths])
def test_a_cause_whose_description_the_message_holds_is_not_repeated(
        describe):
    """The fallback's other shape: the cause quoted with its type."""
    cause = KeyError("lj92")
    exc = _raised(RuntimeError(f"no: {describe_exception(cause)}"), cause)
    assert describe(exc) == "RuntimeError: no: KeyError: 'lj92'"


@pytest.mark.parametrize("describe", [describe_exception,
                                      describe_exception_without_paths])
def test_a_cause_whose_message_occurs_by_chance_is_still_said(describe):
    """Red if the rule is a bare substring test of the cause's message."""
    cause = OSError("EIO")
    exc = _raised(RuntimeError("the read hit EIO twice"), cause)
    # `OSError` with no `strerror` is its type alone in the path-free
    # variant; the tail is said either way.
    assert describe(exc) == (
        f"RuntimeError: the read hit EIO twice (caused by {describe(cause)})")
    assert describe(exc).endswith(" (caused by OSError: EIO)") == (
        describe is describe_exception)
    # A one-word KeyError, quoted by the message as text, not as itself.
    cause = KeyError("x")
    exc = _raised(RuntimeError("no x here: 'x'"), cause)
    assert describe(exc).endswith(" (caused by KeyError: 'x')")


@pytest.mark.parametrize("describe", [describe_exception,
                                      describe_exception_without_paths])
def test_an_empty_cause_is_still_named(describe):
    """`StopIteration` has no message: neither test can match it."""
    exc = _raised(RuntimeError("StopIteration came from the walk"),
                  StopIteration())
    assert describe(exc) == (
        "RuntimeError: StopIteration came from the walk (caused by "
        "StopIteration)")


def test_the_path_free_variant_applies_the_same_rule():
    """An `OSError` cause is described by its `strerror` in that variant."""
    cause = OSError(5, "Input/output error", "/home/doe/scan.dcm")
    exc = _raised(RuntimeError(
        f"load failed: {describe_exception_without_paths(cause)}"), cause)
    assert describe_exception_without_paths(exc) == (
        "RuntimeError: load failed: OSError: Input/output error")
    # And where the message does not hold it, the tail stays, path-free.
    exc = _raised(RuntimeError("load failed"), cause)
    assert describe_exception_without_paths(exc) == (
        "RuntimeError: load failed (caused by OSError: Input/output error)")


def test_the_chain_is_not_touched():
    """The rule changes the words, never `__cause__` (#1092's arm reads it)."""
    cause = ValueError("v")
    exc = _raised(RuntimeError("v; more"), cause)
    describe_exception(exc)
    assert exc.__cause__ is cause


# ---------------------------------------------------------------------------
# #1091: no memory address
# ---------------------------------------------------------------------------

class _Holder:  # pylint: disable=too-few-public-methods
    """An object whose default repr carries an address."""


def test_an_address_is_removed_from_the_message():
    text = describe_exception(ValueError(f"cannot read {_Holder()!r}"))
    assert text == (f"ValueError: cannot read <{__name__}._Holder object>")
    assert "0x" not in text


def test_an_address_inside_a_cause_is_removed_too():
    cause = OSError(f"stream {_Holder()!r} closed")
    exc = _raised(RuntimeError("read failed"), cause)
    for describe in (describe_exception, describe_exception_without_paths):
        assert "0x" not in describe(exc), describe(exc)
    assert describe_exception(exc).endswith(
        f"(caused by OSError: stream <{__name__}._Holder object> closed)")


def test_a_hex_value_that_is_no_address_survives():
    """Only `object at 0x...` inside `<...>` goes: a file's own hex stays."""
    words = ("UID 1.2.840.10008.1.2.4.90, value 0x7FE0, "
             "<0x1> and <thing at 0x1f>")
    assert describe_exception(ValueError(words)) == f"ValueError: {words}"


# ---------------------------------------------------------------------------
# Through the doors
# ---------------------------------------------------------------------------

def _fewer(tmp_path):
    """The cohort's `fewer_fragments_than_frames-short` shape."""
    frames = [imagecodecs.jpeg2k_encode(np.full((4, 4), i, np.uint8),
                                        level=0, codecformat="J2K")
              for i in range(2)]
    ds = dataset(J2K_LOSSLESS, frames, rows=4, cols=4, bits_allocated=8,
                 frames=3)
    ds.PixelData = encapsulate(frames, has_bot=False)
    ds["PixelData"].is_undefined_length = True
    return write(tmp_path, ds, "fewer")


def _zeros(tmp_path, ts, name):
    """One fragment of 64 zero bytes: Pillow cannot identify it."""
    ds = dataset(ts, [bytes(64)], rows=4, cols=4, bits_allocated=8)
    return write(tmp_path, ds, name)


def test_the_fallback_rows_of_the_cohort_shapes_say_each_fact_once(tmp_path):
    """Red while the tail, or the fallback's repeat of pydicom's words, is said."""
    row = ingest_worker(_fewer(tmp_path))[-1]
    assert row == (f"Decompression Failed: ValueError: {FEWER}; imagecodecs "
                   f"could not decode it either, for the same reason")
    assert row.count(FEWER) == 1

    row = ingest_worker(get_testdata_file(
        "JPEG2000-embedded-sequence-delimiter.dcm"))[-1]
    assert row.count("opj_read_header failed") == 1, row
    assert "caused by" not in row
    assert row.endswith("imagecodecs could not decode it either: "
                        "Jpeg2kError: opj_read_header failed")


def _session_rows(tmp_path, folder, name):
    db = os.path.join(str(tmp_path), f"{name}.db")
    report = os.path.join(str(tmp_path), f"{name}.md")
    with DicomSession(persistence_file=db) as session:
        summary = session.ingest(folder)
        session.generate_report(report)
    with sqlite3.connect(db) as conn:
        rows = [details for (details,) in conn.execute(
            "SELECT details FROM audit_log WHERE action_type = 'ERROR' "
            "ORDER BY id")]
    with open(report, encoding="utf-8") as handle:
        return summary, rows, handle.read()


def test_no_memory_address_is_stored(tmp_path):
    """The row, `failures`, the report and the read door hold no address.

    Two ingests of one folder, each in its own spawned pool, write the
    same rows: a row is a function of the file, not of the run.
    """
    folder = os.path.join(str(tmp_path), "in")
    os.makedirs(folder)
    for ts, name in ((J2K_LOSSLESS, "j2k"),
                     ("1.2.840.10008.1.2.4.50", "jpeg")):
        src = _zeros(tmp_path, ts, name)
        os.replace(src, os.path.join(folder, f"{name}.dcm"))

    first = _session_rows(tmp_path, folder, "a")
    second = _session_rows(tmp_path, folder, "b")
    summary, rows, report = first
    assert len(rows) == 2 and len(summary.failures) == 2
    for text in [*rows, *(f[1] for f in summary.failures), report]:
        assert "object at 0x" not in text, text
    assert any("<_io.BytesIO object>" in row for row in rows), rows
    assert sorted(rows) == sorted(second[1])

    path = os.path.join(folder, "j2k.dcm")
    inst = Instance(str(pydicom.dcmread(path).SOPInstanceUID), SOP_CLASS, 1,
                    file_path=path)
    with pytest.raises(RuntimeError) as caught:
        inst.get_pixel_data()
    assert "object at 0x" not in str(caught.value)
    assert "<_io.BytesIO object>" in str(caught.value)
    assert "object at 0x" not in describe_exception(caught.value)
