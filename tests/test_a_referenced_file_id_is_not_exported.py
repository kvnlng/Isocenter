"""Referenced File ID (0004,1500) is not exported under `basic@2026c` or the floor.

Referenced File ID is a Directory Record element of the Basic Directory IOD
(the DICOMDIR, PS3.3 Annex F), not part of any composite instance, and its
value is a file-set path, which sites build from MRNs, accession numbers or
dates. PS3.15 Table E.1-1 has no row for it, so through 1.0.0rc20 no
profile had a rule for it, and an image file carrying one exported it as
written under `(0012,0062) YES` with a PASS grade (#1133; found on the
MIDI-B Test set). By the owner's ruling of 2026-10-16, `basic@2026c` and
the floor built on it remove that one element, wherever it sits, and not
the rest of group 0004: Referenced SOP Instance UID in File (0004,1511)
keeps its own `U` row. The rule lives in the profile, so `KEEP` keeps it
and `privacy_profile: none` has no rule for it.
"""
import os
import sqlite3

import pydicom
import pydicom.data
import pytest
from pydicom.dataset import Dataset
from pydicom.sequence import Sequence

from isocenter import Session
from isocenter.profiles import BASIC_PROFILE

#: The issue's two-component shape (synthetic values): letters and digits
#: no replacement UID or dummy can contain by chance.
TOP_LEVEL = ["06301516", "7452617"]
NESTED = ["NESTDIR", "7452618"]
REFERENCED_UID = "1.2.826.0.1.3680043.2.1125.1133.1"


def _with_referenced_file_id(path):
    """pydicom's `CT_small.dcm` with a top-level Referenced File ID and a
    Referenced Image Sequence item holding a SOP reference and a nested
    Referenced File ID."""
    ds = pydicom.dcmread(pydicom.data.get_testdata_file("CT_small.dcm"))
    ds.add_new(0x00041500, "CS", list(TOP_LEVEL))
    item = Dataset()
    item.ReferencedSOPClassUID = "1.2.840.10008.5.1.4.1.1.2"
    item.ReferencedSOPInstanceUID = REFERENCED_UID
    item.add_new(0x00041500, "CS", list(NESTED))
    ds.ReferencedImageSequence = Sequence([item])
    ds.save_as(path)


def _export(tmp_path, config=None):
    """Ingest, audit, anonymize and export that file under `config`, YAML
    text, or a bare session (the floor) for None. Returns the exported
    file's path and the store's path."""
    os.makedirs(tmp_path / "in")
    _with_referenced_file_id(str(tmp_path / "in" / "ct.dcm"))
    store = str(tmp_path / "s.db")
    with Session(store) as session:
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
    return written[0], store


@pytest.mark.parametrize("config", [None, "privacy_profile: basic\n"],
                         ids=["floor", "basic"])
def test_a_referenced_file_id_is_not_exported(tmp_path, config):
    """Kills: the rule dropped from `ADDED_RULES`; `derive()` not reading
    `ADDED_RULES`; `EMPTY` in place of `REMOVE` (the element would be
    present at zero length); and the nested scan skipping the item."""
    path, store = _export(tmp_path, config)

    with open(path, "rb") as handle:
        raw = handle.read()
    exported = [part for part in (b"06301516", b"7452617", b"NESTDIR")
                if part in raw]
    assert exported == []
    out = pydicom.dcmread(path)
    assert 0x00041500 not in out
    item = out.ReferencedImageSequence[0]
    assert 0x00041500 not in item
    assert item.ReferencedSOPInstanceUID.startswith("2.25."), item.ReferencedSOPInstanceUID
    assert out.PatientIdentityRemoved == "YES"
    with sqlite3.connect(store) as conn:
        rows = [details for (details,) in conn.execute(
            "SELECT details FROM audit_log WHERE action_type = 'REMEDIATION_REMOVE'")
            if "0004,1500" in details]
    assert len(rows) == 2, rows


def test_keep_keeps_the_referenced_file_id(tmp_path):
    """Beyond the profile, the configuration decides: `KEEP` exports both
    values as written. Pins the opt-out the docs promise, and that the
    removal lives in the profile, not in the exporter."""
    config = ("privacy_profile: basic\n"
              "phi_tags:\n"
              "  \"0004,1500\": {action: KEEP}\n")

    path, _ = _export(tmp_path, config)

    out = pydicom.dcmread(path)
    assert list(out[0x00041500].value) == TOP_LEVEL
    assert list(out.ReferencedImageSequence[0][0x00041500].value) == NESTED


def test_none_applies_no_referenced_file_id_rule(tmp_path):
    """`privacy_profile: none` has no rule for it, so it is exported as
    written. Kills a hard-coded drop in the exporter, which would make the
    profile's rule decorative and `none` dishonest."""
    config = ("privacy_profile: none\n"
              "phi_tags:\n"
              "  \"0010,0010\": {action: REPLACE, value: ANONYMIZED}\n")

    path, _ = _export(tmp_path, config)

    out = pydicom.dcmread(path)
    assert list(out[0x00041500].value) == TOP_LEVEL
    assert list(out.ReferencedImageSequence[0][0x00041500].value) == NESTED


def test_only_referenced_file_id_is_added_in_group_0004():
    """By the owner's ruling the rule is on 0004,1500 alone, not on group
    0004 (#1133). Kills the ruling widened to the whole group, and
    0004,1511 losing its `U` row."""
    group = {key for key in BASIC_PROFILE if key.startswith("0004,")}

    assert group == {"0004,1500", "0004,1511"}
    assert BASIC_PROFILE["0004,1511"]["action"] == "REPLACE"
    assert BASIC_PROFILE["0004,1500"]["action"] == "REMOVE"
