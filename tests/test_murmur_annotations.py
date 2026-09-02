import json
import os

import jsonschema
import pytest

from isocenter.entities import DicomItem
from isocenter.io_handlers import populate_attrs
from isocenter.murmur import build_annotations, write_annotations, SCHEMA_VERSION
from isocenter.waveform import Waveform
from scripts.generate_waveform_test_data import build_ecg_dataset, add_annotation


SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "fixtures",
                           "annotations.schema.json")


def _instance_from(ds):
    """Build a Isocenter Instance-like item carrying ds's sequences."""
    from isocenter.entities import Instance
    inst = Instance(str(ds.SOPInstanceUID), str(ds.SOPClassUID), 1)
    populate_attrs(ds, inst)
    return inst


def _waveform_from(ds):
    item = DicomItem()
    populate_attrs(ds.WaveformSequence[0], item)
    return Waveform.from_dicom_item(item)


def test_point_annotation_maps_to_a_point_finding():
    ds = build_ecg_dataset(num_samples=1000)
    add_annotation(ds, start_sample=101)
    doc = build_annotations(_instance_from(ds), _waveform_from(ds), "isocenter/test")

    assert doc["schemaVersion"] == SCHEMA_VERSION
    assert len(doc["findings"]) == 1
    finding = doc["findings"][0]
    assert finding["kind"] == "point"
    # DICOM sample positions are 1-based; Murmur's are 0-based.
    assert finding["startSample"] == 100


def test_segment_annotation_maps_to_a_range_finding():
    ds = build_ecg_dataset(num_samples=1000)
    add_annotation(ds, start_sample=101, end_sample=301)
    doc = build_annotations(_instance_from(ds), _waveform_from(ds), "isocenter/test")

    finding = doc["findings"][0]
    assert finding["kind"] == "range"
    assert finding["startSample"] == 100
    assert finding["endSample"] == 300


def test_category_is_the_scheme_qualified_code_and_label_is_the_meaning():
    ds = build_ecg_dataset(num_samples=500)
    add_annotation(ds, code_value="164889003",
                   code_meaning="Atrial fibrillation", scheme="SCT")
    finding = build_annotations(_instance_from(ds), _waveform_from(ds),
                                "isocenter/test")["findings"][0]

    assert finding["category"] == "SCT:164889003"
    assert finding["label"] == "Atrial fibrillation"


def test_lead_comes_from_the_coded_channel_source():
    ds = build_ecg_dataset(num_samples=500)
    add_annotation(ds, channel=2)
    finding = build_annotations(_instance_from(ds), _waveform_from(ds),
                                "isocenter/test")["findings"][0]
    assert finding["lead"] == "MDC_ECG_LEAD_II"


def test_lead_falls_back_to_a_positional_token_for_an_uncoded_channel():
    """`_lead_for` must pass the annotation's own referenced channel
    number through to `wfdb_description`, not rely on its no-argument
    default.

    Mutating `isocenter/murmur.py`'s `wfdb_description(index)` call to
    `wfdb_description()` survived the full suite: every uncoded,
    non-allowlisted channel then reported the same "signal" placeholder
    regardless of which channel it actually was -- leads silently
    indistinguishable in Murmur, even though `CHANGELOG.md` (#39) claims
    the positional-token fix covers the `annotations.json` `lead` field.
    Channel 2 (DICOM ChannelNumber, 1-based) is 0-based index 1, so a
    correct pass-through produces "ch1", not "signal".
    """
    ds = build_ecg_dataset(num_samples=500,
                           channels=[("MDC_ECG_LEAD_I", "Lead I"),
                                     ("MDC_ECG_LEAD_II", "Lead II")])
    chdef = ds.WaveformSequence[0].ChannelDefinitionSequence[1]
    del chdef.ChannelSourceSequence
    chdef.ChannelLabel = "Operator Free Text Not A Lead Name"
    add_annotation(ds, channel=2)

    finding = build_annotations(_instance_from(ds), _waveform_from(ds),
                                "isocenter/test")["findings"][0]
    assert finding["lead"] == "ch1"


def test_lead_from_coded_source_is_sanitized_against_hea_comment_injection():
    """`annotations.json`'s `lead` field must get the same line-break
    sanitization the `.hea` signal description gets
    (`isocenter.exporters.wfdb._sanitize_description`).

    A coded Channel Source value is not filtered by the lead-name
    allowlist -- that only guards the free-text Channel Label fallback --
    so a non-conformant source can still carry an embedded newline.
    `isocenter/exporters/wfdb.py` already sanitizes this for the `.hea` file
    (`_sanitize_description`); pre-fix, `isocenter/murmur.py` did not apply
    the same treatment, so `annotations.json` carried a rawer value than
    the `.hea` for the identical input.
    """
    ds = build_ecg_dataset(num_samples=500)
    chdef = ds.WaveformSequence[0].ChannelDefinitionSequence[1]
    chdef.ChannelSourceSequence[0].CodeValue = "MDC\n#patient ZQINJECT01"
    add_annotation(ds, channel=2)

    finding = build_annotations(_instance_from(ds), _waveform_from(ds),
                                "isocenter/test")["findings"][0]
    assert "\n" not in finding["lead"]
    assert finding["lead"] == "MDC #patient ZQINJECT01"


def test_free_text_note_is_written_only_when_opted_in():
    """Covers the note mapping itself (not the default): with
    include_text=True, Unformatted Text Value must still land in `note`.
    Renamed from test_free_text_becomes_the_note now that omission is the
    default -- see test_note_is_omitted_by_default below.
    """
    ds = build_ecg_dataset(num_samples=500)
    add_annotation(ds, text="Onset preceded by R-on-T")
    finding = build_annotations(_instance_from(ds), _waveform_from(ds),
                                "isocenter/test", include_text=True)["findings"][0]
    assert finding["note"] == "Onset preceded by R-on-T"


def test_note_is_omitted_by_default():
    """(0070,0006) is free-text clinical commentary and must not be written
    unless the caller asks for it.

    The PHI scan is tag-gated, so a bare Session() scans almost nothing.
    Defaulting to omit is what makes this safe regardless of configuration.
    """
    ds = build_ecg_dataset(num_samples=500)
    add_annotation(ds, text="Reviewed by Dr Jane Doe, MRN-12345678")
    document = build_annotations(_instance_from(ds), _waveform_from(ds), "isocenter/test")
    assert document["findings"], "fixture produced no findings"
    for finding in document["findings"]:
        assert "note" not in finding, (
            "annotation note text was written without the caller opting in")


def test_note_is_written_when_explicitly_requested():
    """Opting in is a deliberate act, and must actually work."""
    ds = build_ecg_dataset(num_samples=500)
    add_annotation(ds, text="sinus rhythm")
    document = build_annotations(_instance_from(ds), _waveform_from(ds),
                                 "isocenter/test", include_text=True)
    notes = [f.get("note") for f in document["findings"]]
    assert "sinus rhythm" in notes


def test_absolute_time_is_never_emitted():
    ds = build_ecg_dataset(num_samples=500)
    add_annotation(ds, start_sample=10)
    finding = build_annotations(_instance_from(ds), _waveform_from(ds),
                                "isocenter/test")["findings"][0]
    assert "startUnixMS" not in finding
    assert "endUnixMS" not in finding


def test_no_annotation_sequence_yields_no_findings():
    ds = build_ecg_dataset(num_samples=500)
    doc = build_annotations(_instance_from(ds), _waveform_from(ds), "isocenter/test")
    assert doc["findings"] == []


def test_write_annotations_skips_when_there_are_no_findings(tmp_path):
    """An annotation-free instance must produce NO `.annotations.json`
    file -- `test_no_annotation_sequence_yields_no_findings` above only
    covers `build_annotations` (the document shape); nothing previously
    asserted `write_annotations`'s own empty-document skip. Mutating
    `if not document.get("findings"): return None` to `if False:` left
    every existing test green: an exported record would gain an
    `annotations.json` with `"findings": []` and nothing would notice.
    """
    path = str(tmp_path / "rec.annotations.json")
    document = {"schemaVersion": SCHEMA_VERSION, "source": "isocenter/test", "findings": []}

    result = write_annotations(path, document)

    assert result is None, "empty-findings document should not be written"
    assert not os.path.exists(path), (
        "write_annotations wrote a file for a document with no findings")


def test_write_annotations_writes_when_findings_are_present(tmp_path):
    """Positive counterpart to the skip test above: a document that DOES
    carry findings must actually be written, so the skip test above
    cannot be satisfied by a `write_annotations` that always returns
    None.
    """
    path = str(tmp_path / "rec.annotations.json")
    document = {
        "schemaVersion": SCHEMA_VERSION,
        "source": "isocenter/test",
        "findings": [{"kind": "point", "startSample": 0, "category": "AFib"}],
    }

    result = write_annotations(path, document)

    assert result == path
    assert os.path.exists(path)
    with open(path, encoding="utf-8") as f:
        written = json.load(f)
    assert written == document


def test_output_validates_against_murmurs_published_schema():
    ds = build_ecg_dataset(num_samples=1000)
    add_annotation(ds, start_sample=101, end_sample=301,
                   text="Range finding")
    add_annotation(ds, start_sample=500, channel=3)
    doc = build_annotations(_instance_from(ds), _waveform_from(ds), "isocenter/1.0")

    # Guard against a trivially-passing validation: an empty findings list
    # validates fine against the schema, so a broken mapping that silently
    # drops every finding would pass jsonschema.validate() undetected.
    assert len(doc["findings"]) == 2

    with open(SCHEMA_PATH, encoding="utf-8") as f:
        schema = json.load(f)

    jsonschema.validate(instance=doc, schema=schema)


def test_schema_rejects_an_unknown_kind():
    """Prove the validator can actually fail: kind must be point/range."""
    doc = {
        "schemaVersion": 1,
        "findings": [
            {"kind": "banana", "category": "AFib", "startSample": 0},
        ],
    }
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        schema = json.load(f)

    with pytest.raises(jsonschema.exceptions.ValidationError):
        jsonschema.validate(instance=doc, schema=schema)


def test_schema_rejects_a_finding_with_no_time_anchor():
    """A finding needs startSample or startUnixMS; neither must fail."""
    doc = {
        "schemaVersion": 1,
        "findings": [
            {"kind": "point", "category": "AFib"},
        ],
    }
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        schema = json.load(f)

    with pytest.raises(jsonschema.exceptions.ValidationError):
        jsonschema.validate(instance=doc, schema=schema)


def test_schema_rejects_an_unknown_top_level_key():
    """additionalProperties: false at the top level must be enforced."""
    doc = {
        "schemaVersion": 1,
        "findings": [],
        "annotations": [],  # the old/wrong key name -- must not be accepted
    }
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        schema = json.load(f)

    with pytest.raises(jsonschema.exceptions.ValidationError):
        jsonschema.validate(instance=doc, schema=schema)


def test_annotations_file_lands_beside_the_header(tmp_path):
    import pydicom
    from isocenter.session import DicomSession
    from scripts.generate_waveform_test_data import build_ecg_dataset, add_annotation

    src = tmp_path / "src"
    src.mkdir()
    ds = add_annotation(build_ecg_dataset(num_samples=500), start_sample=101)
    pydicom.dcmwrite(str(src / "ecg.dcm"), ds, write_like_original=False)

    session = DicomSession(persistence_file=str(tmp_path / "ann.db"))
    try:
        session.ingest(str(src))
        paths = session.export(str(tmp_path / "out"), format="wfdb")
    finally:
        session.close()

    base = os.path.splitext(paths[0])[0]
    ann_path = f"{base}.annotations.json"
    assert os.path.exists(ann_path)

    with open(ann_path, encoding="utf-8") as f:
        doc = json.load(f)
    assert doc["schemaVersion"] == 1
    assert doc["findings"][0]["startSample"] == 100
    assert doc["source"].startswith("isocenter/")


# --- Concept Name free text (#58) -------------------------------------
#
# CodeMeaning sits one line above `note` in the same loop and has the same
# property: for a site-defined coding scheme the cart populates it with
# operator-typed text rather than a term from a published vocabulary.


def test_a_site_defined_scheme_does_not_leak_the_code_meaning_into_label():
    """The reproduction from #58, verbatim.

    A CodeMeaning of "ZQANNMEAN01 Jane Doe" survived a full
    create_config/audit/anonymize pass into annotations.json as
    `"label": "ZQANNMEAN01 Jane Doe"`.
    """
    ds = build_ecg_dataset(num_samples=500)
    add_annotation(ds, code_value="ZQ01",
                   code_meaning="ZQANNMEAN01 Jane Doe", scheme="99LOCAL")
    finding = build_annotations(_instance_from(ds), _waveform_from(ds),
                                "isocenter/test")["findings"][0]

    assert "label" not in finding


def test_a_site_defined_scheme_collapses_the_category_to_uncoded():
    ds = build_ecg_dataset(num_samples=500)
    add_annotation(ds, code_value="ZQ01", code_meaning="Local marking",
                   scheme="99LOCAL")
    finding = build_annotations(_instance_from(ds), _waveform_from(ds),
                                "isocenter/test")["findings"][0]

    assert finding["category"] == "uncoded"


def test_a_concept_with_no_scheme_is_treated_as_site_defined():
    """An absent designator is not evidence of a published vocabulary."""
    ds = build_ecg_dataset(num_samples=500)
    add_annotation(ds, code_value="ZQ01", code_meaning="Local marking",
                   scheme="")
    finding = build_annotations(_instance_from(ds), _waveform_from(ds),
                                "isocenter/test")["findings"][0]

    assert finding["category"] == "uncoded"
    assert "label" not in finding


def test_a_site_defined_concept_still_produces_a_finding():
    """Suppressing the text must not delete the mark.

    A reviewer seeing fewer marks than the DICOM carried, with nothing
    saying any were withheld, is silent under-reporting on a review tool.
    """
    ds = build_ecg_dataset(num_samples=500)
    add_annotation(ds, start_sample=101, code_value="ZQ01",
                   code_meaning="ZQANNMEAN01 Jane Doe", scheme="99LOCAL")
    doc = build_annotations(_instance_from(ds), _waveform_from(ds),
                            "isocenter/test")

    assert len(doc["findings"]) == 1
    assert doc["findings"][0]["startSample"] == 100
    assert doc["findings"][0]["lead"] == "MDC_ECG_LEAD_I"


def test_site_defined_concept_text_is_restored_when_opted_in():
    """include_annotation_text is the auditor's override.

    It already means "I accept free text in this output"; a protocol that
    permits site-defined annotation labels says so through that flag.
    """
    ds = build_ecg_dataset(num_samples=500)
    add_annotation(ds, code_value="ZQ01", code_meaning="Local marking",
                   scheme="99LOCAL")
    finding = build_annotations(_instance_from(ds), _waveform_from(ds),
                                "isocenter/test", True)["findings"][0]

    assert finding["category"] == "99LOCAL:ZQ01"
    assert finding["label"] == "Local marking"


# --- Multiplex group resolution (#159) --------------------------------
#
# Referenced Waveform Channels (0040,A0B0) is a list of (multiplex group,
# channel) pairs, and DICOM numbers both from 1. PS3.3 C.10.10.1.1
# ("Referenced Channels") defines the first value of each pair as the
# ordinal of the Waveform Sequence (5400,0100) Item, and its worked
# example writes "the entire first multiplex group and channels 2 and 3
# of the third multiplex group" as 0001 0000 0003 0002 0003 0003. Group
# ORDINAL 1 is therefore Waveform Sequence ITEM 0 -- the only group
# ingest keeps (#36). Ordinal 2 is the second group: the discarded one.
# The 0000 in that example is the same section's rule that channel 0
# means every channel in the group.
#
# The two conventions differ by exactly one at exactly the place the bug
# lives, so every test below says "ordinal" where it matters. #159's own
# prose counts groups from 0 (Isocenter's item index); reading "group 1"
# there as ordinal 1 and writing the check 0-based would drop every
# annotation in every conformant file.


def _add_second_group(ds, sampling_frequency=1000.0):
    """Append a second multiplex group running at a different rate.

    Ingest keeps item 0 and discards this one (#36), which is what makes
    an annotation naming it unplaceable in the exported record.
    """
    import copy
    second = copy.deepcopy(ds.WaveformSequence[0])
    second.SamplingFrequency = str(sampling_frequency)
    ds.WaveformSequence.append(second)
    return ds


def test_an_annotation_on_the_discarded_group_does_not_borrow_a_kept_lead_name():
    """Defect 1 of #159: `_lead_for` read values[1] and ignored values[0].

    `waveform.channels` is the ingested group's channel list, so an
    annotation on ordinal 2, channel 2 came back labelled with the
    INGESTED group's channel 2 -- a plausible lead name at a plausible
    position, both belonging to a different signal.
    """
    ds = build_ecg_dataset(num_samples=1000)
    _add_second_group(ds)
    add_annotation(ds, start_sample=101, channel=2, group=2)

    doc = build_annotations(_instance_from(ds), _waveform_from(ds), "isocenter/test")

    leads = [f.get("lead") for f in doc["findings"]]
    assert "MDC_ECG_LEAD_II" not in leads, (
        "an annotation on the discarded group was labelled with the "
        f"ingested group's channel 2: {doc!r}")
    assert doc["findings"] == [], (
        "an annotation naming a group that was not ingested has no "
        f"placeable position in this record and must be dropped: {doc!r}")


def test_a_time_offset_annotation_on_the_discarded_group_is_not_converted_at_the_kept_rate():
    """Defect 2 of #159: `_sample_positions` used the ingested group's fs.

    The rhythm strip runs at 500 Hz and the second group at 1000 Hz. A
    1.0 s offset on ordinal 2 was converted with 500 Hz and landed at
    sample 500 of a record it does not belong to.
    """
    ds = build_ecg_dataset(num_samples=1000, sampling_frequency=500.0)
    _add_second_group(ds, sampling_frequency=1000.0)
    add_annotation(ds, group=2, time_offsets=[1.0])

    doc = build_annotations(_instance_from(ds), _waveform_from(ds), "isocenter/test")

    starts = [f.get("startSample") for f in doc["findings"]]
    assert 500 not in starts, (
        "a 1.0 s offset on the 1000 Hz group was converted with the "
        f"ingested group's 500 Hz rate: {doc!r}")
    assert doc["findings"] == [], doc


def test_dropping_an_annotation_reports_the_group_ordinal_the_file_named():
    """A silent drop is a different bug, not a fix (#36's warn-plus-audit)."""
    ds = build_ecg_dataset(num_samples=1000)
    _add_second_group(ds)
    add_annotation(ds, start_sample=101, channel=2, group=2)

    dropped_groups = []
    build_annotations(_instance_from(ds), _waveform_from(ds), "isocenter/test",
                      dropped_groups=dropped_groups)

    # Ordinals, not prose. The exporter holds the logger and the store
    # handle, and it aggregates across the instance before wording
    # anything -- so what crosses this boundary is which groups were
    # named, one list per dropped annotation.
    assert dropped_groups == [[2]], dropped_groups


def test_an_annotation_on_the_ingested_group_still_resolves():
    """Ordinal 1 IS the kept group. The guard must not touch it."""
    ds = build_ecg_dataset(num_samples=1000)
    _add_second_group(ds)
    add_annotation(ds, start_sample=101, channel=2, group=1)

    dropped_groups = []
    doc = build_annotations(_instance_from(ds), _waveform_from(ds),
                            "isocenter/test", dropped_groups=dropped_groups)

    assert len(doc["findings"]) == 1, doc
    assert doc["findings"][0]["lead"] == "MDC_ECG_LEAD_II"
    assert doc["findings"][0]["startSample"] == 100
    assert dropped_groups == [], dropped_groups


def test_a_zero_group_ordinal_is_read_as_the_first_group():
    """0 is not a valid 1-based ordinal, and the only sane reading is "first".

    Isocenter's own fixtures wrote 0 until #159, and real carts that count
    from 0 write it too. Treating it as invalid would drop every
    annotation such a source carries; treating it as the first group can
    never confuse it with a group that survived, because there is no
    other group it could name. This is what the `max(0, ...)` in
    `murmur._item_index` is for -- do not simplify it away.
    """
    ds = build_ecg_dataset(num_samples=1000)
    add_annotation(ds, start_sample=101, channel=1, group=0)

    dropped_groups = []
    doc = build_annotations(_instance_from(ds), _waveform_from(ds),
                            "isocenter/test", dropped_groups=dropped_groups)

    assert len(doc["findings"]) == 1, doc
    assert doc["findings"][0]["lead"] == "MDC_ECG_LEAD_I"
    assert dropped_groups == [], dropped_groups


def test_an_annotation_with_no_referenced_channels_is_kept_without_a_lead():
    """(0040,A0B0) is Type 1C: absent means the whole waveform.

    If "names no kept group" swallowed "names no group at all", a common
    conformant case would silently empty annotations.json.
    """
    ds = build_ecg_dataset(num_samples=1000)
    add_annotation(ds, start_sample=101)
    del ds.WaveformAnnotationSequence[0].ReferencedWaveformChannels

    dropped_groups = []
    doc = build_annotations(_instance_from(ds), _waveform_from(ds),
                            "isocenter/test", dropped_groups=dropped_groups)

    assert len(doc["findings"]) == 1, doc
    assert "lead" not in doc["findings"][0]
    assert doc["findings"][0]["startSample"] == 100
    assert dropped_groups == [], dropped_groups


def test_a_time_offset_annotation_on_the_ingested_group_uses_its_rate():
    """The fallback still works for the group it is allowed to describe."""
    ds = build_ecg_dataset(num_samples=2000, sampling_frequency=500.0)
    _add_second_group(ds, sampling_frequency=1000.0)
    add_annotation(ds, group=1, time_offsets=[1.0])

    doc = build_annotations(_instance_from(ds), _waveform_from(ds), "isocenter/test")

    assert len(doc["findings"]) == 1, doc
    assert doc["findings"][0]["startSample"] == 500


def test_one_annotation_naming_several_discarded_groups_reports_all_of_them():
    """The message says "groups"; it has to mean it.

    (0040,A0B0) is VM 2-2n, so one annotation can name several discarded
    groups at once. Reporting only the first ordinal under-reports the
    loss the audit entry exists to disclose, while the plural promises
    otherwise -- and the reader cannot tell a partial list from a
    complete one.
    """
    ds = build_ecg_dataset(num_samples=1000)
    _add_second_group(ds)
    add_annotation(ds, start_sample=101, group=2, channel=2)
    # Ordinal 2 channel 2, then ordinal 3 channel 1: two discarded
    # groups, neither of them the kept one.
    ds.WaveformAnnotationSequence[0].ReferencedWaveformChannels = [2, 2, 3, 1]

    dropped_groups = []
    doc = build_annotations(_instance_from(ds), _waveform_from(ds),
                            "isocenter/test", dropped_groups=dropped_groups)

    assert doc["findings"] == [], doc
    assert dropped_groups == [[2, 3]], dropped_groups


def test_a_channel_number_of_zero_applies_to_the_whole_kept_group():
    """PS3.3 C.10.10.1.1: channel 0 means every channel in the group.

    The standard's own worked example uses it -- `0001 0000` is "all of
    the first multiplex group". The mark belongs to the exported record,
    so it must survive; it names no single channel, so it carries no
    `lead`. Dropping it would lose a conformant annotation, and picking
    a channel for it would invent one.
    """
    ds = build_ecg_dataset(num_samples=1000)
    add_annotation(ds, start_sample=101, group=1, channel=0)

    dropped_groups = []
    doc = build_annotations(_instance_from(ds), _waveform_from(ds),
                            "isocenter/test", dropped_groups=dropped_groups)

    assert len(doc["findings"]) == 1, doc
    assert doc["findings"][0]["startSample"] == 100
    assert "lead" not in doc["findings"][0], doc
    assert dropped_groups == [], dropped_groups


def test_an_unpaired_trailing_value_is_ignored_not_mispaired():
    """An odd-length (0040,A0B0) is nonconformant; it must not rescue a drop.

    `[2, 3, 1]` is one pair (ordinal 2, channel 3) plus a stray 1.
    Sliding the window by one -- or reading the trailing value as a
    group -- would find "ordinal 1", conclude the annotation names the
    kept group, and resolve a discarded group's mark against the
    exported signal: the exact defect #159 is about, reintroduced
    through a malformed value.
    """
    ds = build_ecg_dataset(num_samples=1000)
    _add_second_group(ds)
    add_annotation(ds, start_sample=101, group=2, channel=3)
    ds.WaveformAnnotationSequence[0].ReferencedWaveformChannels = [2, 3, 1]

    dropped_groups = []
    doc = build_annotations(_instance_from(ds), _waveform_from(ds),
                            "isocenter/test", dropped_groups=dropped_groups)

    assert doc["findings"] == [], doc
    assert dropped_groups == [[2]], dropped_groups


def test_an_annotation_naming_both_groups_resolves_against_the_ingested_one():
    """(0040,A0B0) is VM 2-2n: one annotation can name several groups.

    Reading only the first pair would drop a finding that genuinely
    applies to the exported signal as well.
    """
    ds = build_ecg_dataset(num_samples=1000)
    _add_second_group(ds)
    add_annotation(ds, start_sample=101, group=2, channel=3)
    # Ordinal 2 channel 3, then ordinal 1 channel 2 -- the discarded
    # group named first, so the kept pair is only found by looking past it.
    ds.WaveformAnnotationSequence[0].ReferencedWaveformChannels = [2, 3, 1, 2]

    dropped_groups = []
    doc = build_annotations(_instance_from(ds), _waveform_from(ds),
                            "isocenter/test", dropped_groups=dropped_groups)

    assert len(doc["findings"]) == 1, doc
    assert doc["findings"][0]["lead"] == "MDC_ECG_LEAD_II"
    assert dropped_groups == [], dropped_groups


# --- The drop is said out loud, at export (#159) -----------------------
#
# `build_annotations` has no logger and no store handle -- it runs inside
# the WFDB exporter, which has both. The messages ride an out-parameter
# for the same reason `populate_attrs`'s `dropped_private_binary` list
# does (#125): the caller does the warning and the audit entry, and it
# words the message, because only the caller can see the whole instance
# and aggregate across it.


def _two_group_annotated_file(path, group=2, num_samples=500):
    """Write a two-group ECG whose annotation names `group`."""
    import pydicom
    ds = build_ecg_dataset(num_samples=num_samples, sampling_frequency=500.0)
    _add_second_group(ds, sampling_frequency=1000.0)
    add_annotation(ds, start_sample=101, channel=2, group=group)
    pydicom.dcmwrite(str(path), ds, enforce_file_format=True)
    return str(path)


def _data_loss_rows(db_path):
    import sqlite3
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT details, loss_scope FROM audit_log "
            "WHERE action_type='DATA_LOSS'").fetchall()


def test_a_dropped_annotation_is_warned_and_audited(tmp_path, caplog):
    """Warn-plus-audit, the shape #36 established for the group discard.

    The audit row is scoped STANDARD even though #150 grades the group
    discard itself as SIGNAL: an annotation is a mark *about* the
    signal, and the acquired-samples loss it described already costs the
    run its PASS via the ingest-side multiplex row. Grading this row too
    would double-charge one loss under two entries.
    """
    import logging
    from isocenter.io_handlers import LOSS_SCOPE_STANDARD
    from isocenter.session import DicomSession

    src = tmp_path / "src"
    src.mkdir()
    _two_group_annotated_file(src / "multi.dcm")

    session = DicomSession(persistence_file=str(tmp_path / "drop.db"))
    db_path = session.store_backend.db_path
    try:
        session.ingest(str(src))
        with caplog.at_level(logging.WARNING):
            session.export(str(tmp_path / "out"), format="wfdb")
    finally:
        session.close()

    warnings = [r.getMessage() for r in caplog.records
                if r.levelno >= logging.WARNING]
    assert any("multiplex group 2" in m for m in warnings), warnings

    rows = _data_loss_rows(db_path)
    annotation_rows = [r for r in rows if "annotation" in r[0].lower()]
    assert len(annotation_rows) == 1, rows
    assert annotation_rows[0][1] == LOSS_SCOPE_STANDARD, annotation_rows

    # Two rows, and that is the expected shape rather than a double
    # count: one for the groups the ingest discarded, one for the marks
    # that referenced them. Since #177 both are written at ingest --
    # dangling annotations are filtered from the graph beside #160's
    # `del`, so the WFDB bridge's own drop fires only for a graph that
    # never passed through ingest. `docs/waveforms.md` says so, so it is
    # asserted here rather than left as prose.
    assert len(rows) == 2, rows


def test_several_dropped_annotations_file_one_audit_row_naming_every_group(tmp_path):
    """One row per instance, not one per mark.

    A cart that marks forty beats on a discarded group would otherwise
    put forty near-identical rows into section 3 of the compliance
    report, and a section nobody can read reports nothing -- the same
    argument #146 made for the bare `DATA_LOSS: 3` count, one layer up.
    #36's emitter, which this descends from, reports the discard once
    with a count; this matches it. The count is of annotations and the
    list is of distinct groups, because they answer different questions.
    """
    import pydicom
    from isocenter.session import DicomSession

    src = tmp_path / "src"
    src.mkdir()
    ds = build_ecg_dataset(num_samples=500, sampling_frequency=500.0)
    _add_second_group(ds, sampling_frequency=1000.0)
    add_annotation(ds, start_sample=101, channel=2, group=2)
    add_annotation(ds, start_sample=201, channel=3, group=2)
    add_annotation(ds, start_sample=301, channel=1, group=3)
    add_annotation(ds, start_sample=401, channel=1, group=1)  # kept: survives
    pydicom.dcmwrite(str(src / "multi.dcm"), ds, enforce_file_format=True)

    session = DicomSession(persistence_file=str(tmp_path / "many.db"))
    db_path = session.store_backend.db_path
    try:
        session.ingest(str(src))
        paths = session.export(str(tmp_path / "out"), format="wfdb")
    finally:
        session.close()

    annotation_rows = [r for r in _data_loss_rows(db_path)
                       if "annotation" in r[0].lower()]
    assert len(annotation_rows) == 1, _data_loss_rows(db_path)
    detail = annotation_rows[0][0]
    assert "3 waveform annotations" in detail, detail
    assert "groups 2, 3" in detail, detail

    # The one annotation on the ingested group is untouched by any of it.
    with open(f"{os.path.splitext(paths[0])[0]}.annotations.json",
              encoding="utf-8") as f:
        findings = json.load(f)["findings"]
    assert len(findings) == 1, findings
    assert findings[0]["startSample"] == 400


def test_the_dropped_annotation_row_renders_as_one_report_table_cell(tmp_path):
    """Section 3 is a markdown table, and this detail carries commas.

    The audit-log assertions above read the row straight out of sqlite,
    which is the layer under the one #146 and #157 were about. A detail
    string that breaks the pipe table would leave the Scope column
    rendering under the wrong header, with the whole suite still green --
    that is the failure mode #157 pinned for the ingest-side messages,
    and this message has a shape none of those had.
    """
    import pydicom
    from isocenter.io_handlers import LOSS_SCOPE_STANDARD
    from isocenter.session import DicomSession

    src = tmp_path / "src"
    src.mkdir()
    ds = build_ecg_dataset(num_samples=500, sampling_frequency=500.0)
    _add_second_group(ds, sampling_frequency=1000.0)
    add_annotation(ds, start_sample=101, channel=2, group=2)
    add_annotation(ds, start_sample=201, channel=1, group=3)
    pydicom.dcmwrite(str(src / "multi.dcm"), ds, enforce_file_format=True)

    report = tmp_path / "report.md"
    session = DicomSession(persistence_file=str(tmp_path / "report.db"))
    try:
        session.ingest(str(src))
        # The annotation row is written at ingest since #177, but the
        # export is kept: this test renders the report a real WFDB run
        # produces, and a report generated before an export carries the
        # boundary note (#153) beside the same table.
        session.export(str(tmp_path / "out"), format="wfdb")
        session.store_backend.flush_audit_queue()
        session.generate_report(str(report))
    finally:
        session.close()

    lines = report.read_text(encoding="utf-8").splitlines()
    row = [ln for ln in lines if "waveform annotations" in ln]
    assert len(row) == 1, lines

    # Header is | Timestamp | Instance | Element | Scope |, so a row that
    # kept its detail in one cell has exactly four cells.
    cells = [c.strip() for c in row[0].strip().strip("|").split("|")]
    assert len(cells) == 4, cells
    assert "groups 2, 3" in cells[2], cells
    assert cells[3] == LOSS_SCOPE_STANDARD, cells


def test_an_ordinary_single_group_export_records_no_annotation_data_loss(tmp_path):
    """The guard must not turn every ECG export into audit noise."""
    import pydicom
    from isocenter.session import DicomSession

    src = tmp_path / "src"
    src.mkdir()
    ds = add_annotation(build_ecg_dataset(num_samples=500), start_sample=101)
    pydicom.dcmwrite(str(src / "ecg.dcm"), ds, enforce_file_format=True)

    session = DicomSession(persistence_file=str(tmp_path / "clean.db"))
    db_path = session.store_backend.db_path
    try:
        session.ingest(str(src))
        paths = session.export(str(tmp_path / "out"), format="wfdb")
    finally:
        session.close()

    assert _data_loss_rows(db_path) == []
    with open(f"{os.path.splitext(paths[0])[0]}.annotations.json",
              encoding="utf-8") as f:
        assert len(json.load(f)["findings"]) == 1


def test_dropping_an_annotation_does_not_change_the_hea(tmp_path):
    """annotations.json and the .hea signal lines must not desynchronise.

    The `.hea` is built from Waveform Sequence item 0 and the sample
    array; the guard reads only (0040,B020). Dropping a finding removes a
    mark -- it must not move a channel count, gain, rate or sample count.
    """
    from isocenter.session import DicomSession

    def _export(name, group):
        src = tmp_path / f"src_{name}"
        src.mkdir()
        _two_group_annotated_file(src / "ecg.dcm", group=group)
        session = DicomSession(persistence_file=str(tmp_path / f"{name}.db"))
        try:
            session.ingest(str(src))
            paths = session.export(str(tmp_path / f"out_{name}"), format="wfdb")
        finally:
            session.close()
        with open(paths[0], "rb") as f:
            return f.read()

    assert _export("kept", 1) == _export("dropped", 2)
