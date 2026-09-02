"""A store written before the #160 fix still exports a hollow multiplex item (#168).

#160 fixed the truncation at ingest: `ingest_worker` drops the Waveform
Sequence items whose samples it discards. That covers new ingests only.
A store indexed *before* the fix holds one item per multiplex group with
samples behind item 0 alone, and `ingest_worker` never runs again --
`persistence.py` hydrates whatever the store holds, and the export
writes every item back, declaring a multiplex group with no Waveform
Data (5400,1010), which is Type 1 (PS3.3 C.10.9).

The affected window is any index that ingested a multi-group waveform
between 0.8.2 (where #36's warn-plus-audit landed) and the #160 fix.
Nothing else self-heals it: `session.compact()` rewires sidecar offsets
and never re-reads source files.

The fixture builds the legacy state the way the legacy code did: by
walking the whole Waveform Sequence with `populate_attrs` and saving
the resulting graph -- current `ingest_worker` cannot produce it.
"""
import copy
import glob
import logging
import os

import pydicom
import pytest

from isocenter.entities import Instance, Patient, Series, Study
from isocenter.io_handlers import populate_attrs
from isocenter.persistence import SqliteStore
from scripts.generate_waveform_test_data import (add_annotation,
                                                 build_ecg_dataset)


def _legacy_store(db_path, annotate_groups=()):
    """A store holding what pre-#160 ingest left: items for every group,
    samples for group 0 only.

    Args:
        db_path (str): Where to create the store.
        annotate_groups: 1-based multiplex group ordinals; one waveform
            annotation is written per entry, exactly as a pre-#177 store
            holds them.

    Returns:
        str: The SOP Instance UID of the single stored instance.
    """
    ds = build_ecg_dataset(num_samples=200, sampling_frequency=500.0)
    second = copy.deepcopy(ds.WaveformSequence[0])
    second.SamplingFrequency = 1000.0
    ds.WaveformSequence.append(second)
    for group in annotate_groups:
        add_annotation(ds, start_sample=10, group=group)

    inst = Instance(str(ds.SOPInstanceUID), str(ds.SOPClassUID), 1)
    # The whole sequence, exactly as pre-#160 ingest kept it.
    populate_attrs(ds, inst)
    assert len(inst.sequences["5400,0100"].items) == 2

    patient = Patient("LEG1", "Legacy^Patient")
    study = Study("LEG1.STUDY", "20230101")
    series = Series("LEG1.SERIES", "ECG", 1)
    series.instances.append(inst)
    study.series.append(series)
    patient.studies.append(study)

    store = SqliteStore(db_path)
    store.save_all([patient])
    # Group 0's samples reached the sidecar; group 1's never did.
    store.persist_blob(inst, "waveform",
                       bytes(ds.WaveformSequence[0].WaveformData))
    store.stop()
    return inst.sop_instance_uid


def test_a_legacy_hollow_multiplex_item_is_pruned_on_load(tmp_path):
    """Hydration heals the graph to what current ingest produces."""
    db = str(tmp_path / "legacy.db")
    _legacy_store(db)

    loaded = SqliteStore(db).load_all()[0].studies[0].series[0].instances[0]

    assert len(loaded.sequences["5400,0100"].items) == 1


def test_the_prune_is_a_logged_warning_naming_the_instance(tmp_path, caplog):
    """An edit to a graph the user did not ask to have edited must say so.

    The original discard was audited by the session that ingested (#36,
    0.8.2 onward), so the store's own audit log already records the
    loss; the warning is about the heal, not the loss.
    """
    db = str(tmp_path / "legacy.db")
    uid = _legacy_store(db)

    with caplog.at_level(logging.WARNING):
        SqliteStore(db).load_all()

    warnings = [r.getMessage() for r in caplog.records
                if r.levelno >= logging.WARNING]
    matching = [m for m in warnings if uid in m and "multiplex" in m.lower()]
    assert matching, warnings
    # The remedy is in the message: the samples are unrecoverable from
    # this store, and only a re-ingest of the source can bring them back.
    assert any("re-ingest" in m.lower() for m in matching), matching


def test_a_legacy_annotation_on_a_discarded_group_is_pruned_with_it(tmp_path):
    """The heal must not stop half way and create #177's shape instead.

    Before the prune the dangling reference was masked by the hollow
    item; pruning the item without filtering the annotation would
    surface it. Current ingest filters both (#160, #177), and hydration
    heals to what current ingest produces.
    """
    db = str(tmp_path / "legacy.db")
    _legacy_store(db, annotate_groups=(1, 2))

    loaded = SqliteStore(db).load_all()[0].studies[0].series[0].instances[0]

    anns = loaded.sequences["0040,b020"].items
    assert len(anns) == 1, [a.attributes for a in anns]
    assert list(anns[0].attributes["0040,a0b0"]) == [1, 1]


def test_the_pruned_graph_reads_as_clean(tmp_path):
    """The heal is not an edit: nothing may look unsaved after a load.

    A prune that advanced `_revision` would leave every legacy instance
    claiming unsaved changes and discard its stored `phi_status` -- the
    same invariant `_apply_vertical_attributes` documents.
    """
    db = str(tmp_path / "legacy.db")
    _legacy_store(db)

    patients = SqliteStore(db).load_all()
    inst = patients[0].studies[0].series[0].instances[0]

    assert not inst.has_unsaved_changes
    assert not patients[0].has_unsaved_changes


def test_a_reopened_legacy_store_exports_a_conformant_record(tmp_path):
    """The issue's measured repro, end to end (#168).

    reloaded items: 2 / item 1: WaveformData present=False was the
    pre-fix state -- a Type 1 violation in the written file.
    """
    from isocenter.session import DicomSession

    db = str(tmp_path / "legacy.db")
    _legacy_store(db)

    out = tmp_path / "out"
    session = DicomSession(persistence_file=db)
    try:
        session.export(str(out), format="dicom", show_progress=False)
    finally:
        session.close()

    written = glob.glob(str(out / "**" / "*.dcm"), recursive=True)
    assert written, "export produced no .dcm files"
    exported = pydicom.dcmread(written[0])

    assert len(exported.WaveformSequence) == 1
    assert getattr(exported.WaveformSequence[0], "WaveformData", None)


def test_a_single_item_store_is_left_alone(tmp_path, caplog):
    """The heal fires on the damage, not on every waveform store."""
    ds = build_ecg_dataset(num_samples=200)
    inst = Instance(str(ds.SOPInstanceUID), str(ds.SOPClassUID), 1)
    populate_attrs(ds, inst)
    del inst.sequences["5400,0100"].items[1:]  # none to delete; explicit

    patient = Patient("OK1", "Healthy^Patient")
    study = Study("OK1.STUDY", "20230101")
    series = Series("OK1.SERIES", "ECG", 1)
    series.instances.append(inst)
    study.series.append(series)
    patient.studies.append(study)

    db = str(tmp_path / "ok.db")
    store = SqliteStore(db)
    store.save_all([patient])
    store.persist_blob(inst, "waveform",
                       bytes(ds.WaveformSequence[0].WaveformData))
    store.stop()

    with caplog.at_level(logging.WARNING):
        loaded = SqliteStore(db).load_all()[0]

    inst = loaded.studies[0].series[0].instances[0]
    assert len(inst.sequences["5400,0100"].items) == 1
    assert not any("multiplex" in r.getMessage().lower()
                   for r in caplog.records)


def test_write_tree_still_writes_a_hand_built_graph_as_it_stands(tmp_path):
    """The serializer applies no gates, by design -- pinned, not fixed.

    `DicomExporter.write_tree()` reaches the hollow-item state from a
    different direction: a hand-built graph with no ingest and no store
    anywhere in the picture (the `scripts/` fixture generators). The
    hydration heal deliberately does not reach it; a serializer that
    edits the graph it was handed is a second, quieter answer to "which
    multiplex groups does this record have" (#168, and CLAUDE.md on the
    export/serializer split).
    """
    from isocenter.io_handlers import DicomExporter

    ds = build_ecg_dataset(num_samples=100)
    second = copy.deepcopy(ds.WaveformSequence[0])
    ds.WaveformSequence.append(second)

    inst = Instance(str(ds.SOPInstanceUID), str(ds.SOPClassUID), 1)
    populate_attrs(ds, inst)
    assert len(inst.sequences["5400,0100"].items) == 2

    patient = Patient("HB1", "Hand^Built")
    study = Study("HB1.STUDY", "20230101")
    series = Series("HB1.SERIES", "ECG", 1)
    series.instances.append(inst)
    study.series.append(series)
    patient.studies.append(study)

    out = str(tmp_path / "tree")
    DicomExporter.write_tree(patient, out, show_progress=False)

    written = glob.glob(os.path.join(out, "**", "*.dcm"), recursive=True)
    assert written
    assert len(pydicom.dcmread(written[0]).WaveformSequence) == 2
