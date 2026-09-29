
import os

import pytest
import pydicom
import pydicom.data
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.sequence import Sequence
from isocenter import Session
from isocenter.entities import Instance
from isocenter.io_handlers import populate_attrs, process_sequence
from isocenter.privacy import PhiInspector, PhiFinding
from support.project_secret import FIXED_A

def test_the_scan_finds_the_same_tag_at_every_level_it_appears():
    """A tag reused at two depths yields a finding per occurrence.

    This replaces an assertion that the same tags were present in
    `Instance.text_index` (#84). The index had no production consumer and
    is gone; more to the point, asserting that something was *indexed*
    was never evidence that anything *scanned* it -- that gap is #57 in
    one sentence, and this file's other test carries the postmortem.

    The shape worth keeping is specific to structured reports: PatientName
    appears at the top level and again inside a nested Content Sequence
    item, and the two are different values on different entities. A scan
    that deduplicated by tag, or that stopped at the first hit, would
    still satisfy "0010,0010 was found" and leave the clinician's name
    inside the report.
    """
    ds = Dataset()
    ds.PatientName = "Test^Patient"
    ds.PatientID = "123456"

    seq_item = Dataset()
    seq_item.ValueType = "TEXT"
    seq_item.TextValue = "Patient states pain in leg."

    nested_item = Dataset()
    nested_item.PatientName = "Dr. Smeagol"      # same tag, two levels down
    seq_item.ContentSequence = Sequence([nested_item])
    ds.ContentSequence = Sequence([seq_item])

    inst = Instance("1.2.3", "1.2.840.10008.5.1.4.1.1.88.33", 1)
    populate_attrs(ds, inst)

    findings = PhiInspector(project_secret=FIXED_A)._scan_instance(inst, "P1", None)
    names = [f for f in findings if f.tag == "0010,0010"]

    assert len(names) == 2, [(f.tag, f.value) for f in findings]
    assert {str(f.value) for f in names} == {"Test^Patient", "Dr. Smeagol"}
    # Distinct entities, or remediation would write both to one item.
    assert len({id(f.entity) for f in names}) == 2


def test_phi_inspector_deep_scan():
    """
    Verifies that PhiInspector finds PHI nested in a sequence.

    This used to attach `deep_item` to nothing and hand-append it to
    `inst.text_index`, so it pinned the index as the mechanism. The index
    is built once at ingest and is neither rebuilt when a session loads
    from the store nor carried into the worker copies `session.audit()`
    scans -- so it was empty on every real path, and the deep scan this
    test proved was working never once ran in production (#57). The scan
    now walks the item graph, so the item has to actually be in one.
    """
    # 1. Setup Instance with a nested sequence item
    inst = Instance("1.2.3", "class", 1)

    from isocenter.entities import DicomItem
    deep_item = DicomItem()
    deep_item.set_attr("0040,a160", "Patient has history of diabetes.")
    inst.add_sequence_item("0040,a730", deep_item)

    # 2. Setup Inspector with rule for TextValue (0040,A160)
    # We pretend 0040,A160 is flagged as PHI (it usually is or should be cleaned)
    config = {
        "0040,a160": {"name": "Text Value", "action": "REPLACE"}
    }

    inspector = PhiInspector(config_tags=config)

    # 3. Scan
    findings = inspector._scan_instance(inst, "PAT_123")

    # 4. Verify
    assert len(findings) == 1
    f = findings[0]
    assert f.tag == "0040,a160"
    assert f.value == "Patient has history of diabetes."
    assert f.remediation_proposal.new_value == "ANONYMIZED"
    assert f.entity is deep_item  # Crucial: Point to deep item, not root instance
    assert f.entity_path == (("0040,a730", 0),), (
        "the finding must record where the item sits, or it cannot be "
        "found again after crossing a process boundary")


def test_sr_d_codes_hold_their_dummies(tmp_path):
    """pydicom's `test-SR.dcm` through a bare session: Verifying Observer
    Name and Verification DateTime are present and hold the dummy of
    their VR (#557).

    PS3.15 Table E.1-1 gives both `D`: replace with a non-zero-length
    dummy consistent with the VR, and both are Type 1 in the SR. Until
    #557 the basic profile mapped `D` to EMPTY and this test pinned that
    documented non-conformance, zero-length; it flipped when the dummy
    landed, as it said it would.

    Kills: `D` mapped to REMOVE (the elements become absent) or to EMPTY
    (zero-length again), and the Verifying Observer Sequence given a rule
    of its own (it would be removed or emptied, and its items with it)."""
    import os
    import shutil
    import pydicom.data

    os.makedirs(tmp_path / "in")
    src = pydicom.data.get_testdata_file("test-SR.dcm")
    shutil.copy(src, tmp_path / "in" / "sr.dcm")
    original = pydicom.dcmread(src)
    assert original.VerifyingObserverSequence[0].VerifyingObserverName, \
        "fixture drift: test-SR.dcm has no Verifying Observer Name"

    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        session.anonymize(session.audit())
        summary = session.export(str(tmp_path / "out"), use_compression=False)

    assert summary.written == 1, summary.failures
    written = [os.path.join(root, name) for root, _, names in os.walk(tmp_path / "out")
               for name in names if name.endswith(".dcm")]
    out = pydicom.dcmread(written[0])

    observers = out.VerifyingObserverSequence
    assert len(observers) == len(original.VerifyingObserverSequence)
    for item in observers:
        assert item.VerifyingObserverName == "ANONYMIZED"
        assert item.VerificationDateTime == "19000101"


#: Free text a radiologist might dictate into an SR: a person, an
#: MRN-shaped number and an institution. Letters in every part, so no
#: replacement UID can contain one by chance.
SR_FREE_TEXT = "Discussed with Dr Quillon Vantreese, MRN QV-7654321, at Harrowgate General."


def _sr_with_free_text(path):
    """pydicom's `test-SR.dcm` with one more TEXT content item at the root
    holding `SR_FREE_TEXT`. The source's own TEXT items stay."""
    ds = pydicom.dcmread(pydicom.data.get_testdata_file("test-SR.dcm"))
    concept = Dataset()
    concept.CodeValue = "121106"
    concept.CodingSchemeDesignator = "DCM"
    concept.CodeMeaning = "Comment"
    item = Dataset()
    item.RelationshipType = "CONTAINS"
    item.ValueType = "TEXT"
    item.ConceptNameCodeSequence = Sequence([concept])
    item.TextValue = SR_FREE_TEXT
    ds.ContentSequence.append(item)
    ds.save_as(path)


def _export_sr_with_free_text(tmp_path, config=None):
    """Ingest, audit, anonymize and export that SR under `config`, YAML
    text, or a bare session (the floor) for None. Returns the exported
    file's path."""
    os.makedirs(tmp_path / "in")
    _sr_with_free_text(str(tmp_path / "in" / "sr.dcm"))
    with Session(str(tmp_path / "s.db")) as session:
        if config is not None:
            (tmp_path / "config.yaml").write_text(config)
            session.load_config(str(tmp_path / "config.yaml"))
        session.ingest(str(tmp_path / "in"))
        session.anonymize(session.audit())
        summary = session.export(str(tmp_path / "out"), use_compression=False)
    assert summary.written == 1, summary.failures
    written = [os.path.join(root, name) for root, _, names in os.walk(tmp_path / "out")
               for name in names if name.endswith(".dcm")]
    assert len(written) == 1, written
    return written[0]


def _content_items(ds):
    """Every content item of the tree under `ds`, at any depth."""
    for item in ds.get("ContentSequence", []):
        yield item
        yield from _content_items(item)


def _text_values(ds):
    return [str(item.TextValue) for item in _content_items(ds)
            if item.get("ValueType") == "TEXT"]


@pytest.mark.parametrize("config", [None, "privacy_profile: basic\n"],
                         ids=["floor", "basic"])
def test_an_sr_s_free_text_is_not_exported(tmp_path, config):
    """PS3.15 Table E.1-1 codes Content Sequence (0040,A730) `D`. A TEXT
    item's Text Value (0040,A160) is free text and no row of its own, so
    the rows nested inside cannot clean it: `basic@2026c`, and the floor
    built on it, remove the sequence, as they do Graphic Annotation
    Sequence (#848). Removed, not emptied: Content Sequence is Type 1C,
    "one or more Items", so a zero-item one breaks PS3.3, while without
    it the SR's root content item is a leaf. Until 1.0.0rc4 the sequence
    had no rule, and every TEXT item -- a name, an MRN, an institution --
    was exported as written under `(0012,0062) YES`; 1.0.0rc4 emptied it.

    Kills: `0040,a730` without a rule again, or given `KEEP`; `EMPTY` in
    place of `REMOVE` (the sequence would be present with zero items);
    and the pass declining the finding, which would withhold the
    marker."""
    source = pydicom.dcmread(pydicom.data.get_testdata_file("test-SR.dcm"))
    assert _text_values(source), "fixture drift: test-SR.dcm has no TEXT items"

    path = _export_sr_with_free_text(tmp_path, config)

    with open(path, "rb") as handle:
        raw = handle.read()
    exported = [part for part in (b"Quillon Vantreese", b"QV-7654321", b"Harrowgate")
                if part in raw]
    assert exported == []
    out = pydicom.dcmread(path)
    assert "ContentSequence" not in out
    assert out.PatientIdentityRemoved == "YES"


def test_a_content_sequence_the_configuration_keeps_exports_its_text(tmp_path):
    """Beyond the table, the configuration decides. `KEEP` on Content
    Sequence keeps a Structured Report's content, and its TEXT items are
    exported as written: no row reaches Text Value, and Isocenter does not
    implement the Clean Structured Content option, which would clean
    them. The rows nested inside still apply, so the DATE item holds the
    DA dummy.

    Kills: a `KEEP` on a sequence that still empties it, a rule on Text
    Value added to the profile unannounced (the kept text would change),
    and the nested rows skipped under a kept sequence."""
    config = ("privacy_profile: basic\n"
              "phi_tags:\n"
              "  \"0040,a730\": {action: KEEP}\n")

    path = _export_sr_with_free_text(tmp_path, config)

    out = pydicom.dcmread(path)
    assert SR_FREE_TEXT in _text_values(out)
    dates = [str(item.Date) for item in _content_items(out)
             if item.get("ValueType") == "DATE"]
    assert dates, "fixture drift: test-SR.dcm has no DATE item"
    assert set(dates) == {"19000101"}, dates

