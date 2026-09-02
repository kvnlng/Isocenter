"""The export worker reads the graph; it must never write it (#184).

`_export_instance_worker` used to end its dataset assembly with
`populate_attrs(ds, inst)` -- the *ingest* reader, pointed at the
dataset the worker had just built and writing what it found back onto
the live instance. `add_sequence_item` appends, so every sequence item
came back as a duplicate (1 -> 2 -> 3 across exports), and every
patient/study/series tag stamped onto the dataset landed in
`inst.attributes` through `set_attr`. Both writes bump `_revision`, so
the next `save()` persisted the duplicates and a reload had them for
real.

Latent through `session.export()` -- `maxtasksperchild=25` pins the
workers to subprocesses on every interpreter, so only a pickled copy
was mutated (that pinning is #185, filed separately) -- but real
through the public `DicomExporter.export_batch()`/`write_tree()`, whose
`maxtasksperchild` defaults to None: on a free-threaded build the
worker runs on the parent's own objects. Measured on 3.14.7t before
the fix: `write_tree()` on a graph with one `(0008,1140)` item left
two.

What the call actually contributed was measured before it was deleted
(#184): the exported file is byte-identical with and without it, for a
hand-built pixel instance with a sequence, an ingested CT with a
sequence and a private tag, and an ingested ECG waveform. Its one live
effect was indirect -- the worker's two later
`inst.attributes.get("0008,0060")` reads saw the *merged* modality via
the writeback -- and those reads now ask the assembled dataset
directly, which carries the same merged view without touching the
graph. The modality tests below pin that behaviour on both sides.
"""
import os

import numpy as np
import pydicom
import pytest

from isocenter.entities import DicomItem, Instance
from isocenter.io_handlers import ExportContext, _export_instance_worker

SC = "1.2.840.10008.5.1.4.1.1.7"
SR = "1.2.840.10008.5.1.4.1.1.88.11"


def _pixel_instance():
    inst = Instance("1.2.3.4.500", SC, 1)
    inst.attributes.update({
        "0008,0060": "OT",
        "0028,0010": 4, "0028,0011": 4,
        "0028,0100": 8, "0028,0101": 8, "0028,0102": 7,
        "0028,0002": 1, "0028,0004": "MONOCHROME2", "0028,0103": 0,
    })
    inst.set_pixel_data(np.arange(16, dtype=np.uint8).reshape(4, 4))
    item = DicomItem()
    item.set_attr("0008,0100", "12345")
    item.set_attr("0008,0104", "a code meaning")
    inst.add_sequence_item("0040,a730", item)
    return inst


def _ctx(inst, out, series_modality="CT"):
    return ExportContext(
        instance=inst,
        output_path=out,
        patient_attributes={"0010,0010": "DOE^J", "0010,0020": "P1"},
        study_attributes={"0020,000d": "1.2.3"},
        series_attributes={"0020,000e": "1.2.4",
                           "0008,0060": series_modality,
                           "0020,0011": "1"},
        compression=None)


def test_an_in_process_export_leaves_the_graph_exactly_as_it_found_it(
        tmp_path):
    """The threads-style pin: worker on the live object, nothing moves.

    Called in-process deliberately -- this is what
    `export_batch()`/`write_tree()` do under threads (3.14t's default
    path), where `ctx.instance` *is* the caller's object. Everything the
    old writeback touched is asserted: the sequence item count, the
    attribute dict, and `_revision` -- the last one because it is what
    decides whether the next `save()` persists a mutation the caller
    never made.
    """
    inst = _pixel_instance()
    attrs_before = dict(inst.attributes)
    revision_before = inst._revision
    items_before = len(inst.sequences["0040,a730"].items)

    outcome = _export_instance_worker(
        _ctx(inst, str(tmp_path / "one.dcm")))
    assert outcome.ok, outcome.error

    assert len(inst.sequences["0040,a730"].items) == items_before, (
        "the worker appended a duplicate sequence item to the live graph")
    assert inst.attributes == attrs_before, (
        "the worker wrote merged patient/study/series tags back onto "
        "the instance")
    assert inst._revision == revision_before, (
        "the worker dirtied the instance, so the next save() would "
        "persist an edit the caller never made")


def test_repeated_exports_write_byte_identical_files(tmp_path):
    """The accumulation, pinned at the artefact: 1 -> 1 -> 1, not 1 -> 2 -> 3.

    Before the fix each pass appended what the previous pass left, so
    the *third* file carried three copies of the sequence item while the
    first carried one. Byte-comparing successive outputs is the
    strongest available spelling of "the exported file is unchanged".
    """
    inst = _pixel_instance()
    digests = []
    for n in range(3):
        out = str(tmp_path / f"pass_{n}.dcm")
        outcome = _export_instance_worker(_ctx(inst, out))
        assert outcome.ok, outcome.error
        with open(out, "rb") as fh:
            digests.append(fh.read())

    assert digests[0] == digests[1] == digests[2], (
        "successive exports of one instance produced different files")
    exported = pydicom.dcmread(str(tmp_path / "pass_2.dcm"))
    assert len(exported[0x0040A730].value) == 1


def test_a_series_level_modality_still_reaches_the_missing_pixel_check(
        tmp_path):
    """The one thing the writeback did that mattered, kept without it.

    The worker's "Pixels missing for Image Modality" refusal reads the
    modality *after* the levels are merged, so an instance whose
    modality lives only on its series -- a hand-built graph, the
    write_tree() population -- must still be judged by the series value.
    The writeback used to smuggle that value into `inst.attributes`;
    the read now asks the assembled dataset. Both directions:

    - series says CT, no pixels anywhere: refused, naming CT;
    - series says SR, no pixels: written, because SR legitimately has
      none -- under the old default this instance would have been
      judged as "OT", which is in `_IMAGE_MODALITIES`, and refused.
    """
    ct_inst = Instance("1.2.3.4.501", SC, 1)
    outcome = _export_instance_worker(
        _ctx(ct_inst, str(tmp_path / "ct.dcm"), series_modality="CT"))
    assert outcome.ok is False
    assert "Pixels missing for Image Modality CT" in str(outcome.error)
    assert not os.path.exists(str(tmp_path / "ct.dcm"))

    sr_inst = Instance("1.2.3.4.502", SR, 1)
    outcome = _export_instance_worker(
        _ctx(sr_inst, str(tmp_path / "sr.dcm"), series_modality="SR"))
    assert outcome.ok, outcome.error
    assert os.path.exists(str(tmp_path / "sr.dcm"))
    # And the judged value did not come to rest on the graph.
    assert "0008,0060" not in sr_inst.attributes
