
import pytest
import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.sequence import Sequence
from isocenter import Session
from isocenter.entities import Instance
from isocenter.io_handlers import populate_attrs, process_sequence
from isocenter.privacy import PhiInspector, PhiFinding

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

    findings = PhiInspector()._scan_instance(inst, "P1", None)
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
