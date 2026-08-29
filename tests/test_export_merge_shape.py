"""`_export_instance_worker` merges each attribute source exactly once (#179).

A copy-paste in `0efc25d` left the `# 0. Base Attributes` and `# 1. Patient
Level` blocks in the worker twice, comments included, and #135 subsequently
updated both copies -- which is how they stayed identical and invisible for
seven releases. The duplication was inert: `_merge` writes through
`add_new`, so the second pass overwrote with the same values, `_merge` also
deduped its loss entries, and `_merge_sequences` rebuilds a fresh
`Sequence()` per call rather than appending. The exported file was
byte-identical either way.

Inert is exactly why nothing caught it, and why this test counts calls
rather than comparing output. There is no observable difference in the
file, so the only way to pin "merged once" is to watch the merging.

Two things this buys, neither of them a bug fix:

* Six of `io_handlers.py`'s mutation sites were permanently unkillable --
  deleting any one of the duplicated calls changed nothing, so
  `scripts/mutation_probe.py` reported SURVIVED forever. Those are not
  coverage gaps, and writing tests for them would have been the wrong
  answer; deletion is the right one (#140).
* Three merges per instance, one of them a full sequence rebuild, on
  every instance of a 100GB+ export.

The worker is called directly here rather than through `session.export()`.
`_export_instance_worker` normally runs in a `ProcessPoolExecutor` child,
where a monkeypatched `_merge` in the parent is invisible and the
assertion below would pass vacuously on the unfixed code.
"""
import os

import pytest

from isocenter.entities import DicomItem, DicomSequence, Instance
from isocenter.io_handlers import DicomExporter, ExportContext
from isocenter import io_handlers


@pytest.fixture
def sr_context(tmp_path):
    """One structured-report instance, ready for the worker.

    Modality is `SR` on purpose: `OT` is in the worker's
    `IMAGE_MODALITIES` set, so an instance with no pixels raises
    `RuntimeError` there. `SR` takes the `arr = None` branch, which keeps
    this test off the pixel path entirely.
    """
    inst = Instance(sop_instance_uid="1.2.3.4",
                    sop_class_uid="1.2.840.10008.5.1.4.1.1.88.11",
                    instance_number=1)
    inst.attributes.update({
        "0008,0016": "1.2.840.10008.5.1.4.1.1.88.11",
        "0008,0018": "1.2.3.4",
        "0008,0060": "SR",
        "0020,0013": "1",
    })
    item = DicomItem()
    item.attributes.update({"0008,0100": "121071",
                            "0008,0102": "DCM"})
    inst.sequences["0040,a730"] = DicomSequence("0040,a730", [item])

    return ExportContext(
        instance=inst,
        output_path=os.path.join(str(tmp_path), "out.dcm"),
        patient_attributes={"0010,0010": "ANON", "0010,0020": "PAT1"},
        study_attributes={"0020,000d": "1.2.3", "0008,0020": "20230101"},
        series_attributes={"0020,000e": "1.2.3.9", "0008,0060": "SR",
                           "0020,0011": "1"},
    )


def test_no_attribute_source_is_merged_twice(sr_context, monkeypatch):
    """The duplication in one assertion.

    Counting by the identity of the mapping handed in, rather than by a
    total, so adding a genuinely new level stays a one-line change here
    instead of a magic number to bump.
    """
    seen = []
    real_merge = DicomExporter._merge
    real_seqs = DicomExporter._merge_sequences

    def counting_merge(ds, attrs, losses=None):
        seen.append(("attrs", id(attrs)))
        return real_merge(ds, attrs, losses)

    def counting_seqs(ds, sequences, losses=None):
        seen.append(("sequences", id(sequences)))
        return real_seqs(ds, sequences, losses)

    monkeypatch.setattr(DicomExporter, "_merge", staticmethod(counting_merge))
    monkeypatch.setattr(DicomExporter, "_merge_sequences",
                        staticmethod(counting_seqs))

    outcome = io_handlers._export_instance_worker(sr_context)
    assert outcome.ok, outcome.error

    repeats = [key for key in set(seen) if seen.count(key) > 1]
    assert not repeats, (
        f"{len(repeats)} attribute source(s) merged more than once: {seen}")


def test_every_level_is_still_merged(sr_context, monkeypatch):
    """The other half: deleting one copy must not delete both.

    Without this, `test_no_attribute_source_is_merged_twice` is satisfied
    by a worker that merges nothing at all.

    Identity again rather than contents, and here for a second reason:
    the worker's `populate_attrs(ds, inst)` call reads the assembled
    dataset back onto the instance, so by the time this returns,
    `inst.attributes` holds the patient/study/series tags too and a
    by-value comparison would no longer find what was passed in.
    """
    merged = []
    real_merge = DicomExporter._merge

    def recording_merge(ds, attrs, losses=None):
        merged.append(id(attrs))
        return real_merge(ds, attrs, losses)

    monkeypatch.setattr(DicomExporter, "_merge", staticmethod(recording_merge))

    outcome = io_handlers._export_instance_worker(sr_context)
    assert outcome.ok, outcome.error

    for label, source in (("instance", sr_context.instance.attributes),
                          ("patient", sr_context.patient_attributes),
                          ("study", sr_context.study_attributes),
                          ("series", sr_context.series_attributes)):
        assert id(source) in merged, f"{label} attributes were never merged"
