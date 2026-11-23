"""An icon whose Pixel Data was not ingested is not exported (#1082).

Ingest drops an Icon Image Sequence item's Pixel Data that it cannot
carry: case B, 5 bytes where 2x2x3 needs 12; case C, a label that is not
a defined term, so the decode fails. Its row named the element and not
the item. The export then wrote the item with all nine pixel descriptors
and no Pixel Data, which is not a conformant item (PS3.3 C.7.6.1.1.6),
under PASS. Measured on 3e4ee91a, 3.12.14 and 3.14.7t; the fingerprint's
cohort holds it twice (`label_spelling-icon`, `-icon-whitespace`).

Now ingest's row names the item (`at 0088,0200[0]`), and the export
removes every icon item, at any depth, that holds no Pixel Data, with one
STANDARD `DATA_LOSS` row naming the item: the rule
`_write_back_nested_pixels` already applies to every icon it cannot write
(owner ruling 2026-11-22, Q11 and Q12). The graph is not changed. It is in
the build half, so a plugin export gets it too.

Names `isocenter.io_handlers`.
"""
import json
import os
import sqlite3

import pydicom
import pytest
from pydicom.dataset import Dataset
from pydicom.sequence import Sequence

from isocenter import exporters
from isocenter.session import DicomSession
from support import plugin_exporters as plugins
from test_export_photometric_admissibility import _icon_source

INGEST_ROW = ("Standard tag 7fe0,0010 (OB) at 0088,0200[0] was not ingested; "
              "unrouted pixel elements are not held in the object graph, so "
              "the item is not in the exported file.")


def _export_row(at="0088,0200[0]"):
    return (f"Icon Image Sequence item at {at} was not exported: it held no "
            f"Pixel Data, and an icon item's pixel descriptors without its "
            f"pixels describe no image (PS3.3 C.7.6.1.1.6).")


def _source(folder, case):
    """Case B (5 bytes) or C (an undefined label), or a good icon."""
    _icon_source(str(folder), samples=3)
    path = os.path.join(str(folder), "one.dcm")
    ds = pydicom.dcmread(path)
    icon = ds.IconImageSequence[0]
    if case == "B":
        icon.PixelData = bytes(5)
    elif case == "C":
        icon.PhotometricInterpretation = "NONSENSE"
    ds.save_as(path, enforce_file_format=True)
    return path


def _loss_rows(db):
    with sqlite3.connect(str(db)) as conn:
        return conn.execute(
            "SELECT details, loss_scope FROM audit_log WHERE action_type = "
            "'DATA_LOSS' ORDER BY id").fetchall()


def _run(tmp_path, src, fmt="dicom"):
    db = tmp_path / "s.db"
    out = tmp_path / "out"
    with DicomSession(str(db)) as session:
        session.ingest(str(src))
        options = {"use_compression": False} if fmt == "dicom" else {}
        summary = session.export(str(out), format=fmt, show_progress=False,
                                 **options)
        assert summary.written == 1, summary.failures
    written = [os.path.join(r, f) for r, _d, fs in os.walk(out) for f in fs]
    return _loss_rows(db), written[0]


@pytest.mark.parametrize("case", ["B", "C"])
def test_the_ingest_row_names_the_item_and_the_export_drops_it(tmp_path, case):
    src = tmp_path / "src"
    src.mkdir()
    _source(src, case)

    rows, written = _run(tmp_path, src)

    assert (INGEST_ROW, "STANDARD") in rows, rows
    assert (_export_row(), "STANDARD") in rows, rows
    ds = pydicom.dcmread(written)
    assert "IconImageSequence" not in ds


def test_the_graph_keeps_the_item(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _source(src, "B")
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(str(src))
        session.export(str(tmp_path / "out"), use_compression=False,
                       show_progress=False)
        inst = session.store.patients[0].studies[0].series[0].instances[0]
        assert len(inst.sequences["0088,0200"].items) == 1


def test_the_reingest_of_the_export_is_clean(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _source(src, "B")
    _rows, written = _run(tmp_path, src)
    again = tmp_path / "again"
    again.mkdir()
    os.replace(written, again / "one.dcm")
    with DicomSession(str(tmp_path / "t.db")) as session:
        summary = session.ingest(str(again))
        assert not summary.failures
    assert _loss_rows(tmp_path / "t.db") == []


def test_a_good_icon_is_written_and_draws_no_row(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _source(src, "good")
    rows, written = _run(tmp_path, src)
    assert rows == []
    assert len(pydicom.dcmread(written).IconImageSequence) == 1


def test_only_the_icon_without_pixels_is_removed(tmp_path):
    """Two icons under one parent, the second without pixels: the first is
    written. Red if removal is by index after an earlier removal, or
    removes the wrong one of two."""
    src = tmp_path / "src"
    src.mkdir()
    path = _source(src, "good")
    ds = pydicom.dcmread(path)
    bad = pydicom.dcmread(path).IconImageSequence[0]
    bad.PixelData = bytes(5)
    ds.IconImageSequence = Sequence([ds.IconImageSequence[0], bad])
    ds.save_as(path, enforce_file_format=True)

    rows, written = _run(tmp_path, src)

    assert (_export_row("0088,0200[1]"), "STANDARD") in rows, rows
    icons = pydicom.dcmread(written).IconImageSequence
    assert len(icons) == 1
    assert icons[0].PixelData == bytes(range(12))


def test_an_icon_nested_in_another_sequence_is_found(tmp_path):
    """At any depth: an icon inside a Referenced Image item."""
    src = tmp_path / "src"
    src.mkdir()
    path = _source(src, "B")
    ds = pydicom.dcmread(path)
    holder = Dataset()
    holder.ReferencedSOPClassUID = "1.2.840.10008.5.1.4.1.1.2"
    holder.ReferencedSOPInstanceUID = "1.2.3.4"
    holder.IconImageSequence = ds.IconImageSequence
    del ds.IconImageSequence
    ds.ReferencedImageSequence = Sequence([holder])
    ds.save_as(path, enforce_file_format=True)

    rows, written = _run(tmp_path, src)

    at = "0008,1140[0] > 0088,0200[0]"
    assert (_export_row(at), "STANDARD") in rows, rows
    out = pydicom.dcmread(written)
    assert "IconImageSequence" not in out.ReferencedImageSequence[0]


@pytest.fixture
def _recorder():
    before = dict(exporters._REGISTRY)  # pylint: disable=protected-access
    exporters.register("recorder", plugins.Recorder)
    yield
    exporters._REGISTRY.clear()  # pylint: disable=protected-access
    exporters._REGISTRY.update(before)  # pylint: disable=protected-access


def test_a_plugin_export_gets_the_same_row(tmp_path, _recorder):  # pylint: disable=redefined-outer-name
    src = tmp_path / "src"
    src.mkdir()
    _source(src, "B")
    rows, written = _run(tmp_path, src, fmt="recorder")
    assert (_export_row(), "STANDARD") in rows, rows
    with open(written, encoding="utf-8") as handle:
        assert json.load(handle)["icons"] == 0
