"""A `REPLACE_TAG` whose value a standard tag's VR cannot hold declines (#560).

The load-time check refuses such a rule, so the scan never proposes one.
This is the backstop for a finding built by hand and handed to
`anonymize(findings)`, which reaches `_replace_on_item` without passing
any validator. Measured on ac33641, with the finding applied: an OB
holding `ANONYMIZED` failed the export (`TypeError: a bytes-like object
is required, not 'str'`, 0 of 1 written), and a DA exported the literal,
which the validator did not notice.

A private tag is not checked: the exporter writes a private value its
recorded VR cannot hold as a valid LO, so the identifier is gone, and a
decline there would keep it.

**Why this file imports what it does.** `RemediationService` is reached
through `Session.anonymize`, and `isocenter.remediation` is named here so
its probe row is charged.
"""
import sqlite3

import pydicom
import pydicom.data
import pytest

import isocenter.remediation  # noqa: F401  (probe row; see the docstring)
from isocenter.privacy import PhiFinding, PhiRemediation
from isocenter.session import DicomSession

ENCAPSULATED_DOCUMENT = "0042,0011"   # OB
INSTANCE_CREATION_DATE = "0008,0012"  # DA
PRIVATE_DATE = "0029,1013"            # private, recorded DA


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")


def _replace(instance, tag):
    return PhiFinding(
        entity_uid=instance.sop_instance_uid, entity_type="Instance",
        field_name=tag, value=instance.attributes.get(tag), reason="hand-built",
        tag=tag, entity=instance,
        remediation_proposal=PhiRemediation(
            action_type="REPLACE_TAG", target_attr=tag, new_value="ANONYMIZED",
            original_value=instance.attributes.get(tag)))


def _source(tmp_path):
    source = tmp_path / "in"
    source.mkdir()
    ds = pydicom.dcmread(pydicom.data.get_testdata_file("CT_small.dcm"))
    ds.add_new(0x00420011, "OB", b"%PDF-1.4 Jane Doe")
    ds.add_new(0x00290010, "LO", "ACME 1.0")
    ds.add_new(0x00291013, "DA", "20230515")
    ds.save_as(str(source / "ct.dcm"))
    return source


def _rows(db, action_type):
    with sqlite3.connect(str(db)) as conn:
        return [row[0] for row in conn.execute(
            "SELECT details FROM audit_log WHERE action_type=?", (action_type,))]


def _export_one(session, out):
    summary = session.export(str(out), use_compression=False)
    assert summary.written == 1, summary.failures
    (path,) = list(out.rglob("*.dcm"))
    return pydicom.dcmread(str(path))


def test_replace_on_an_unfit_standard_vr_declines(tmp_path):
    """Kills the backstop deleted: the OB export fails and the DA exports
    `ANONYMIZED`."""
    source = _source(tmp_path)
    db = tmp_path / "s.db"
    with DicomSession(str(db)) as session:
        session.ingest(str(source))
        instance = session.store.patients[0].studies[0].series[0].instances[0]
        applied = session.anonymize([_replace(instance, ENCAPSULATED_DOCUMENT),
                                     _replace(instance, INSTANCE_CREATION_DATE)])
        assert applied == 0
        # Padded to even length by the source write.
        assert instance.attributes[ENCAPSULATED_DOCUMENT].rstrip(b"\x00") == b"%PDF-1.4 Jane Doe"
        assert instance.attributes[INSTANCE_CREATION_DATE] == "20040119"
        session.save(sync=True)
        declines = _rows(db, "REMEDIATION_DECLINED")
        ds = _export_one(session, tmp_path / "out")
    assert len(declines) == 2, declines
    assert any("0042,0011 is OB" in row for row in declines), declines
    assert any("0008,0012 is DA" in row for row in declines), declines
    assert ds.InstanceCreationDate == "20040119"


def test_replace_on_a_private_tag_is_not_declined(tmp_path):
    """Guard. The recorded VR is not read by the backstop: a private DA
    takes `ANONYMIZED` and the exporter writes it as LO. Kills the
    backstop reading `attribute_vrs`."""
    source = _source(tmp_path)
    db = tmp_path / "s.db"
    with DicomSession(str(db)) as session:
        session.configuration.remove_private_tags = False
        session.ingest(str(source))
        instance = session.store.patients[0].studies[0].series[0].instances[0]
        assert instance.attribute_vrs.get(PRIVATE_DATE) == "DA"
        assert session.anonymize([_replace(instance, PRIVATE_DATE)]) == 1
        assert instance.attributes[PRIVATE_DATE] == "ANONYMIZED"
        ds = _export_one(session, tmp_path / "out")
    assert _rows(db, "REMEDIATION_DECLINED") == []
    # An uncompressed export is Implicit VR, so an element whose private
    # creator pydicom does not know reads back as UN bytes.
    value = ds[0x00291013].value
    if isinstance(value, bytes):
        value = value.decode("ascii").strip()
    assert value == "ANONYMIZED"
