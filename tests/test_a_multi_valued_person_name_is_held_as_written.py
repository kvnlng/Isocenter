"""A Person Name holding several values is held, and written, as the source
wrote it (#937, owner rulings Q5 A and Q6 A, 2026-10-06).

PS3.5 6.4 delimits the values of a character-string element with a
backslash, so `A^B\\C^D` is two values, and pydicom reads a PN element
holding it as a `MultiValue` of two `PersonName`s. Measured on `main` at
de5b26d9 and again at a7f8aeb9: two lines of `isocenter.io_handlers` took
`str()` of that value, which is the text of a Python list.

- `ingest_worker`'s `pname`, which becomes `Patient.patient_name`;
- `populate_attrs`' PN arm, which writes **every** PN element at every
  depth: Patient's Name (VM 1), Operators' Name (VM 1-n, so two values
  are conformant), a PN inside a sequence item, a private PN.

So `SECRETA^B\\SECRETC^D` was held as `[SECRETA^B, SECRETC^D]` and
exported as *one* value holding that text, with no row. Every shipped base
replaces Patient's Name anyway; under a KEEP, in an export with no pass,
and for any PN a configuration keeps, the bracket text was the file's
value, graded PASS beside `(0012,0062) YES`, and the identity token stashed
and restored it.

Now both lines hold the source's own text, values joined by the backslash
that delimited them (`_pn_text`), and the export writes the source's
values with the source's VM, as it does for every other text VR. No row.

**The assertions on an exported file read the element raw** (`get_item`,
bytes), never `str(ds.PatientName)`: pydicom prints a `MultiValue` of two
names as `[SECRETA^B, SECRETC^D]`, which is byte for byte the text the
defect wrote, so a string comparison passes on both trees.

The one named residual (Q6 A): a *private* PN holding several values is
written `UT` under an Explicit VR export, with the existing re-VR
`WARNING` row. Pinned here so a later change to it is seen.
"""
import os
import sqlite3

import pydicom
import pytest
from pydicom.dataset import Dataset
from pydicom.sequence import Sequence

from isocenter import Session
from isocenter import io_handlers

from support.ct_small_files import write_ct

IGNORE = pydicom.config.IGNORE
PID = "PID-937"
NAME = "0010,0010"
TWO = "SECRETA^B\\SECRETC^D"
#: The 19 characters of `TWO`, padded to even length as the writer pads.
TWO_ON_THE_WIRE = b"SECRETA^B\\SECRETC^D "
BRACKETS = "[SECRETA^B, SECRETC^D]"

KEEP = "phi_tags:\n  '0010,0010': {action: KEEP, name: Name}\n"


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _pn(ds, tag, text):
    """Put `text` at `tag` as a PN, past pydicom's VM check: a two-valued
    Patient's Name is what this file is about."""
    ds[tag] = pydicom.DataElement(tag, "PN", text, validation_mode=IGNORE)


def _write(tmp_path, name=TWO, edit=None):
    """CT_small under `PID` with Patient's Name `name` (None deletes it),
    Explicit VR Little Endian, then `edit(ds)`."""
    path = write_ct(tmp_path / "in" / "a.dcm", PID, 937)
    ds = pydicom.dcmread(path)
    ds.file_meta.TransferSyntaxUID = pydicom.uid.ExplicitVRLittleEndian
    if name is None:
        del ds.PatientName
    else:
        _pn(ds, 0x00100010, name)
    if edit is not None:
        edit(ds)
    ds.save_as(path)
    return path


def _exported(folder):
    files = [os.path.join(root, f) for root, _, names in os.walk(folder)
             for f in names if f.endswith(".dcm")]
    assert len(files) == 1, files
    return pydicom.dcmread(files[0])


def _raw(ds, tag):
    """`(wire VR, value bytes)` of an element, before pydicom converts it."""
    raw = ds.get_item(tag)
    return raw.VR, bytes(raw.value)


def _rows(session, *kinds):
    session.store_backend.flush_audit_queue()
    with sqlite3.connect(session.persistence_file) as conn:
        rows = conn.execute("SELECT action_type, details FROM audit_log").fetchall()
    return [row for row in rows if row[0] in kinds]


def _grade(session, tmp_path):
    path = tmp_path / "report.md"
    session.generate_report(str(path))
    line = next(line for line in path.read_text().splitlines()
                if "Validation Status" in line)
    for grade in ("REVIEW_REQUIRED", "PASS", "FAIL"):
        if f"**{grade}**" in line:
            return grade
    return line


def _session(tmp_path, config=None):
    session = Session(str(tmp_path / "s.db"))
    if config is not None:
        cfg = tmp_path / "c.yaml"
        cfg.write_text(config, encoding="utf-8")
        session.load_config(str(cfg))
    session.ingest(str(tmp_path / "in"))
    return session


def _instance(session):
    [patient] = session.store.patients
    [instance] = [i for st in patient.studies for se in st.series for i in se.instances]
    return patient, instance


# ---------------------------------------------------------------------------
# What ingest holds
# ---------------------------------------------------------------------------

def test_the_worker_holds_the_sources_text_in_both_places(tmp_path):
    """Two assertions on purpose: the patient's name and the instance's
    copy come from two lines, and either fixed alone leaves the other
    bracketed."""
    result = io_handlers.ingest_worker(_write(tmp_path))
    meta, instance, error = result[0], result[1], result[-1]
    assert error is None
    assert meta["pname"] == "SECRETA^B\\SECRETC^D"
    assert type(meta["pname"]) is str
    assert instance.attributes[NAME] == "SECRETA^B\\SECRETC^D"
    assert type(instance.attributes[NAME]) is str


@pytest.mark.parametrize("name, held", [
    ("Alpha^One", "Alpha^One"),
    ("A^B=C^D", "A^B=C^D"),
    ("", ""),
    (None, ""),
], ids=["single", "component_groups", "empty", "absent"])
def test_a_name_of_one_value_is_held_as_before(tmp_path, name, held):
    """Controls. One value is the same `str` it was; an empty and an absent
    name are `''` (#746)."""
    result = io_handlers.ingest_worker(_write(tmp_path, name=name))
    meta, instance = result[0], result[1]
    assert result[-1] is None
    assert meta["pname"] == held
    assert type(meta["pname"]) is str
    if name is None:
        assert NAME not in instance.attributes
    else:
        assert instance.attributes[NAME] == held
        assert type(instance.attributes[NAME]) is str


def test_the_store_round_trips_the_text(tmp_path):
    _write(tmp_path)
    with _session(tmp_path) as session:
        session.save(sync=True)
    with Session(str(tmp_path / "s.db")) as reopened:
        patient, instance = _instance(reopened)
        assert patient.patient_name == TWO
        assert type(patient.patient_name) is str
        assert instance.attributes[NAME] == TWO
        assert type(instance.attributes[NAME]) is str


# ---------------------------------------------------------------------------
# What the export writes
# ---------------------------------------------------------------------------

def test_a_kept_name_is_exported_as_the_two_values_the_source_held(tmp_path):
    """The floor with a KEEP on the name: on main the file carried one
    value, `[SECRETA^B, SECRETC^D]`, PASS, `(0012,0062) YES`."""
    _write(tmp_path)
    with _session(tmp_path, KEEP) as session:
        report = session.audit()
        assert [f for f in report.findings if f.tag == NAME] == []
        session.anonymize(report)
        session.export(str(tmp_path / "out"), use_compression=False,
                       show_progress=False)
        ds = _exported(tmp_path / "out")
        assert _raw(ds, 0x00100010)[1] == TWO_ON_THE_WIRE
        assert ds[0x00100010].VM == 2
        assert [str(v) for v in ds[0x00100010].value] == ["SECRETA^B", "SECRETC^D"]
        assert _rows(session, "WARNING", "ERROR", "DATA_LOSS") == []
        assert _grade(session, tmp_path) == "PASS"


@pytest.mark.parametrize("compression", [False, True], ids=["implicit", "explicit"])
def test_an_export_with_no_pass_writes_the_sources_bytes_and_no_row(
        tmp_path, compression):
    """Q5 A: no row. Under the Explicit VR arm the wire still says `PN`."""
    _write(tmp_path)
    with _session(tmp_path) as session:
        session.export(str(tmp_path / "out"), use_compression=compression,
                       show_progress=False)
        ds = _exported(tmp_path / "out")
        vr, value = _raw(ds, 0x00100010)
        assert value == TWO_ON_THE_WIRE
        assert vr == ("PN" if compression else None)
        rows = _rows(session, "WARNING", "ERROR", "DATA_LOSS")
        assert rows == []


@pytest.mark.parametrize("config", [None, "privacy_profile: basic\n",
                                    "privacy_profile: none\n"],
                         ids=["floor", "basic", "none"])
def test_a_replacing_policy_writes_what_it_wrote_before(tmp_path, config):
    """Control, green on main: under every shipped base the name is
    replaced whole, and the fix moves nothing."""
    _write(tmp_path)
    with _session(tmp_path, config) as session:
        session.anonymize(session.audit())
        session.export(str(tmp_path / "out"), use_compression=False,
                       show_progress=False)
        ds = _exported(tmp_path / "out")
        assert _raw(ds, 0x00100010)[1] == b"ANONYMIZED"
        assert ds.PatientIdentityRemoved == "YES"
        assert _grade(session, tmp_path) == "PASS"


def _other_names(ds):
    # Operators' Name is VM 1-n: two values are conformant.
    _pn(ds, 0x00081070, "OpA^X\\OpB^Y")
    item = Dataset()
    _pn(item, 0x00321032, "ReqA^X\\ReqB^Y")
    ds.RequestAttributesSequence = Sequence([item])


def test_every_person_name_element_is_held_and_written_as_the_source_wrote_it(
        tmp_path):
    """Q6 A: Operators' Name and a PN inside a sequence item, with a
    single-valued Patient's Name beside them. Kills a fix at `pname` only,
    at Patient's Name only, or at the top level only."""
    _write(tmp_path, name="Single^Name", edit=_other_names)
    with _session(tmp_path) as session:
        _, instance = _instance(session)
        assert instance.attributes["0008,1070"] == "OpA^X\\OpB^Y"
        [item] = instance.sequences["0040,0275"].items
        assert item.attributes["0032,1032"] == "ReqA^X\\ReqB^Y"
        session.export(str(tmp_path / "out"), use_compression=False,
                       show_progress=False)
        ds = _exported(tmp_path / "out")
        assert _raw(ds, 0x00081070)[1] == b"OpA^X\\OpB^Y "
        assert ds[0x00081070].VM == 2
        [written] = ds.RequestAttributesSequence
        assert _raw(written, 0x00321032)[1] == b"ReqA^X\\ReqB^Y "
        assert _raw(ds, 0x00100010)[1] == b"Single^Name "
        assert _rows(session, "WARNING", "ERROR", "DATA_LOSS") == []


# ---------------------------------------------------------------------------
# The identity token
# ---------------------------------------------------------------------------

def test_the_token_stashes_and_restores_the_sources_text(tmp_path):
    """On main the token held the bracket text and the restore wrote it
    back onto the patient and the copy."""
    _write(tmp_path)
    with _session(tmp_path) as session:
        session.enable_reversible_anonymization(str(tmp_path / "isocenter.key"))
        patient, instance = _instance(session)
        sop = instance.sop_instance_uid
        assert len(session.lock_identities(PID)) == 1
        held = session.recover_patient_identity(PID, restore=False)
        assert held[sop][NAME] == TWO
        session.anonymize(session.audit())
        assert patient.patient_name == "ANONYMIZED"
        session.recover_patient_identity(patient.patient_id, restore=True)
        assert patient.patient_name == TWO
        assert instance.attributes[NAME] == TWO
        assert BRACKETS not in repr(held)


# ---------------------------------------------------------------------------
# The named residual: a private PN of several values
# ---------------------------------------------------------------------------

RE_VR_ROW = (
    "Private element (0071,1001) recorded PN, written UT. Each value is "
    "written under a VR that holds it, unchanged: the VR recorded at ingest "
    "no longer does -- typically after a REPLACE -- or a value over 64 "
    "characters cannot stay multi-valued, and the values are joined with "
    "backslashes into one, recoverable by splitting. A file written with an "
    "explicit VR transfer syntax names the new VR, and a re-ingest of it "
    "records that VR in place of the source's.")


def _private_names(ds):
    for tag in [tag for tag in ds.keys() if tag.group % 2]:
        del ds[tag]
    ds.add_new(0x00710010, "LO", "C3 PN")
    _pn(ds, 0x00711001, "PrivA^X\\PrivB^Y")
    _pn(ds, 0x00711002, "PrivOne^Only")


def test_a_private_person_name_of_two_values_is_written_ut_with_its_row(tmp_path):
    """Q6 A's residual. The source states `PN` (Explicit VR): since #740 a
    private element whose VR the file does not state never reaches the PN
    arm. `_merge`'s private arm does not put a backslash-bearing `str`
    under a multi-valued VR, so the two values are written `UT`, the
    source's bytes, with the re-VR row; the one-valued PN beside it stays
    `PN`. On main the wire said `PN` over the bracket bytes, with no row.
    Explicit VR export only: an Implicit VR file names no VR."""
    _write(tmp_path, name="Single^Name", edit=_private_names)
    with _session(tmp_path, "privacy_profile: none\n"
                            "remove_private_tags: false\n") as session:
        _, instance = _instance(session)
        assert instance.attributes["0071,1001"] == "PrivA^X\\PrivB^Y"
        session.export(str(tmp_path / "out"), use_compression=True,
                       show_progress=False)
        ds = _exported(tmp_path / "out")
        assert not ds.file_meta.TransferSyntaxUID.is_implicit_VR
        assert _raw(ds, 0x00711001) == ("UT", b"PrivA^X\\PrivB^Y ")
        assert _raw(ds, 0x00711002) == ("PN", b"PrivOne^Only")
        [(kind, details)] = _rows(session, "WARNING", "ERROR", "DATA_LOSS")
        assert kind == "WARNING"
        assert details == RE_VR_ROW
        # The row costs the run its PASS; on main there was no row.
        assert _grade(session, tmp_path) == "REVIEW_REQUIRED"
