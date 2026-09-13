"""`EMPTY` on a binary value writes zero-length bytes, and the scan reads
them as empty (#547).

The `EMPTY` arm always wrote the str `""`. No shipped rule reached a
binary VR until #547 made seven of them basic rules: Encapsulated
Document `(0042,0011)`, Certificate of Signer `(0400,0115)`, Flow
Identifier, Source Identifier and Frame Origin Timestamp `(0034,0002/
0005/0007)`, Selector OB Value `(0072,0065)` and Selector UN Value
`(0072,006d)`, all `D` and so all `EMPTY`. Measured on the PR head:

- `DicomExporter._merge` assigned `""` to an OB element and pydicom
  warned "A value of type 'str' cannot be assigned to a tag with VR OB"
  -- in the export *worker*, where a parent's `catch_warnings` sees
  nothing, and under `pytest.ini`'s `ignore:::pydicom.*`, so the suite
  saw nothing either. The written element was a correct zero-length OB.
- Re-ingesting that export read the element back as `b''`, and the scan's
  `val != ""` is True for `b''`: the floor raised `REPLACE_TAG` findings
  on its own output, so a pipeline that re-audits exports never read
  clean, and `export(check_burned_in=True)` of re-ingested data skipped
  every such instance.

The warning is caught explicitly here (`simplefilter("always")` inside
`catch_warnings`, which overrides the ini filter) against `_merge` called
in this process, and the element's Python type is asserted as well, so
neither the filter nor the worker boundary can hide the regression.
"""
import os
import warnings

import pydicom
import pydicom.data
import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian

from isocenter import Session
from isocenter.entities import Instance
from isocenter.io_handlers import DicomExporter
from isocenter.privacy import PhiFinding, PhiInspector, PhiRemediation
from isocenter.remediation import RemediationService

ENCAPSULATED_DOCUMENT = "0042,0011"     # OB, D
CERTIFICATE_OF_SIGNER = "0400,0115"     # OB, D
UN_RULE = "0072,006d"                   # UN in the dictionary, D
STATION_NAME = "0008,1010"              # SH, X/Z/D


def _empty(instance, tag):
    return PhiFinding(
        entity_uid=instance.sop_instance_uid, entity_type="Instance",
        field_name=tag, value=instance.attributes.get(tag), reason="test",
        tag=tag, entity=instance,
        remediation_proposal=PhiRemediation(
            action_type="REPLACE_TAG", target_attr=tag, new_value="",
            original_value=instance.attributes.get(tag)))


def _pydicom_value_warnings(caught):
    return [str(w.message) for w in caught
            if "cannot be assigned to a tag with VR" in str(w.message)]


@pytest.mark.parametrize("tag,value", [
    (ENCAPSULATED_DOCUMENT, b"%PDF-1.4 Jane Doe"),
    (CERTIFICATE_OF_SIGNER, b"\x01\x02CERT"),
    (UN_RULE, b"\x01\x02"),
    # A str in a binary slot -- a store remediated before this fix
    # reloads its `""` as a str. The dictionary VR decides, not the
    # value's type.
    (ENCAPSULATED_DOCUMENT, ""),
])
def test_empty_on_a_binary_vr_writes_zero_length_bytes(tag, value):
    """Red before: the str `""`.

    Kills: the arm's binary-VR branch removed, and the dictionary lookup
    replaced by the value's type alone."""
    instance = Instance("1.2.826.0.1.547.7", "1.2.840.10008.5.1.4.1.1.104.1", 1)
    instance.set_attr(tag, value)

    RemediationService()._apply_single_remediation(_empty(instance, tag))

    assert instance.attributes[tag] == b""
    assert isinstance(instance.attributes[tag], bytes)


def test_empty_on_a_text_vr_still_writes_a_str():
    """The other side of the branch: a text VR keeps `""`, which is what
    `_merge` and every reader of a text attribute expect."""
    instance = Instance("1.2.826.0.1.547.8", "1.2.840.10008.5.1.4.1.1.2", 1)
    instance.set_attr(STATION_NAME, "STATION-7")

    RemediationService()._apply_single_remediation(_empty(instance, STATION_NAME))

    assert instance.attributes[STATION_NAME] == ""
    assert isinstance(instance.attributes[STATION_NAME], str)


def test_the_emptied_binary_value_merges_without_a_pydicom_warning():
    """`_merge` in this process, so the warning is not lost at the worker
    boundary, and caught with `always`, so `pytest.ini` cannot hide it.

    Kills: the arm writing the str `""` to a binary VR."""
    instance = Instance("1.2.826.0.1.547.9", "1.2.840.10008.5.1.4.1.1.104.1", 1)
    instance.set_attr(ENCAPSULATED_DOCUMENT, b"%PDF-1.4 Jane Doe")
    instance.set_attr(CERTIFICATE_OF_SIGNER, b"\x01\x02CERT")
    service = RemediationService()
    for tag in (ENCAPSULATED_DOCUMENT, CERTIFICATE_OF_SIGNER):
        service._apply_single_remediation(_empty(instance, tag))

    ds = Dataset()
    ds.file_meta = FileMetaDataset()
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        DicomExporter._merge(ds, instance.attributes)

    assert _pydicom_value_warnings(caught) == []
    for tag in (0x00420011, 0x04000115):
        assert ds[tag].VR == "OB"
        assert ds[tag].value == b""


@pytest.mark.parametrize("value", ["", b"", None])
def test_the_scan_reads_an_empty_value_of_either_type_as_empty(value):
    """Red before for `b""`: `b"" != ""` is True, so EMPTY raised a
    finding on a value that is already empty.

    Kills: the scan's EMPTY test reverted to `val != ""`."""
    instance = Instance("1.2.826.0.1.547.10", "1.2.840.10008.5.1.4.1.1.104.1", 1)
    instance.attributes[ENCAPSULATED_DOCUMENT] = value
    inspector = PhiInspector(
        config_tags={ENCAPSULATED_DOCUMENT: {"action": "EMPTY",
                                             "name": "Encapsulated Document"}},
        remove_private_tags=False)

    assert [f for f in inspector._scan_instance(instance, "P1")
            if f.tag == ENCAPSULATED_DOCUMENT] == []


def _written(folder):
    paths = [os.path.join(root, name) for root, _, names in os.walk(folder)
             for name in names if name.endswith(".dcm")]
    assert len(paths) == 1, paths
    return paths[0]


def test_the_floor_converges_on_its_own_export(tmp_path):
    """Export, re-ingest the export, audit: nothing raised on the binary
    rules. Red before: `REPLACE_TAG` findings on 0042,0011 and 0400,0115
    against the zero-length elements the first pass wrote.

    Kills: either half reverted -- the scan half by the re-audit, the
    write half by the graph's value before export. Not by the file: a
    zero-length element reads back empty whichever type was written."""
    source = tmp_path / "in"
    source.mkdir()
    ds = pydicom.dcmread(pydicom.data.get_testdata_file("CT_small.dcm"))
    ds.add_new(0x00420011, "OB", b"%PDF-1.4 Jane Doe")
    ds.add_new(0x04000115, "OB", b"\x01\x02\x03\x04CERT")
    ds.save_as(str(source / "ct.dcm"))

    with Session(str(tmp_path / "first.db")) as session:
        session.ingest(str(source))
        session.anonymize(session.audit())
        instance = session.store.patients[0].studies[0].series[0].instances[0]
        for tag in (ENCAPSULATED_DOCUMENT, CERTIFICATE_OF_SIGNER):
            assert instance.attributes[tag] == b"", instance.attributes[tag]
        summary = session.export(str(tmp_path / "out"), use_compression=False)
    assert summary.written == 1, summary.failures

    written = pydicom.dcmread(_written(str(tmp_path / "out")))
    for tag in (0x00420011, 0x04000115):
        assert written[tag].VR == "OB"
        assert not written[tag].value, written[tag].value

    with Session(str(tmp_path / "second.db")) as session:
        session.ingest(str(tmp_path / "out"))
        again = session.audit()

    assert [f.tag for f in again
            if f.tag in (ENCAPSULATED_DOCUMENT, CERTIFICATE_OF_SIGNER)] == []
