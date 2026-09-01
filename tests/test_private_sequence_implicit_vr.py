"""A private sequence under Implicit VR LE hides PHI from the scan (#167).

Implicit VR Little Endian carries no VR on the wire, and the standard
data dictionary has no entry for an odd-group tag, so pydicom resolves a
private sequence to `UN` and hands back bytes. `populate_attrs` had no
arm for that: the bytes landed in `attributes`, nothing landed in
`sequences`, and `PhiInspector._scan_instance`'s structural walk had
nothing to walk into. The identifiers inside the sequence were never
found, the compliance report graded `PASS`, and the export carried them.

The same file written Explicit VR LE resolves to `SQ`, is walked, is
remediated, and exports clean. One file, two transfer syntaxes, two
answers -- and the wrong one is the silent one.

Two rules these tests keep, both learned from #57:

1. **The fixtures are written by hand.** The private element's bytes are
   assembled with `struct.pack`, element header and item framing
   included, and handed to pydicom as an `OB` value so that an
   Implicit VR write puts exactly those bytes on the wire. No helper
   supplies the value under test; if `_sequence_from_un_bytes` and the
   fixture ever agree because they share a code path, the test is
   measuring itself.
2. **Every assertion routes through `session.audit()` or
   `session.export()`.** A test that calls `PhiInspector` directly
   cannot see this class of bug -- the inspector was never the broken
   half.
"""
import glob
import os
import struct

import pydicom
import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.tag import Tag
from pydicom.uid import ExplicitVRLittleEndian, ImplicitVRLittleEndian

from isocenter import Session

SOP_CLASS = "1.2.840.10008.5.1.4.1.1.7"
PRIVATE_SQ = "0009,1003"
PRIVATE_CREATOR = "0009,0010"
STANDARD_SQ = "0008,1140"          # Referenced Image Sequence
NAME_TAG = "0010,0010"
ID_TAG = "0010,0020"

SECRET_NAME = "SECRET^PHI"
SECRET_ID = "MRN-999"

PHI_TAGS = {
    NAME_TAG: {"name": "Patient Name", "action": "REPLACE"},
    ID_TAG: {"name": "Patient ID", "action": "REPLACE"},
}


# --------------------------------------------------------------------------
# Hand-built bytes
# --------------------------------------------------------------------------

def _elem(group, element, value):
    """One Implicit VR LE data element: tag, 4-byte length, value."""
    return struct.pack("<HHI", group, element, len(value)) + value


def _item(payload):
    """One sequence Item (FFFE,E000) with a *defined* length.

    Defined is the whole population: an undefined-length private
    sequence is already recovered as a `Sequence` by pydicom whatever
    the item framing, so it was never affected.
    """
    return struct.pack("<HHI", 0xFFFE, 0xE000, len(payload)) + payload


#: One item carrying two identifiers. `MRN-999 ` is padded to an even
#: length because DICOM values are; the trailing space is stripped on
#: read, which is why the assertions say `MRN-999`.
PRIVATE_SEQUENCE_BYTES = _item(
    _elem(0x0010, 0x0010, SECRET_NAME.encode())
    + _elem(0x0010, 0x0020, b"MRN-999 "))

#: The same shape written with an undefined-length *item* inside a
#: defined-length element -- the second of the two framings §1.2 of the
#: spec measured as affected.
UNDEFINED_ITEM_BYTES = (
    struct.pack("<HHI", 0xFFFE, 0xE000, 0xFFFFFFFF)
    + _elem(0x0010, 0x0010, SECRET_NAME.encode())
    + _elem(0x0010, 0x0020, b"MRN-999 ")
    + struct.pack("<HHI", 0xFFFE, 0xE00D, 0))

#: An item whose second element is `OB` in pydicom's private dictionary
#: for the creator beside it. Binary VRs never reach the object graph,
#: so restoring the structure turns a child nobody could see into a
#: reported DATA_LOSS (#137).
BINARY_CHILD_BYTES = _item(
    _elem(0x0009, 0x0010, b"BrainLAB_Conversion ")
    + _elem(0x0009, 0x1002, b"\x01\x02\x03\x04")
    + _elem(0x0010, 0x0010, SECRET_NAME.encode()))

#: A private sequence *inside* a private sequence, implicit at both
#: levels. The inner element is `UN` bytes within the outer item, so the
#: recursion has to run the gate again on the way down -- the shape the
#: nested tests below cover with a *standard* outer sequence never does
#: that, because a standard SQ is parsed by pydicom itself.
PRIVATE_IN_PRIVATE_BYTES = _item(
    _elem(0x0009, 0x0010, b"ACME_HEADER ")
    + _elem(0x0009, 0x1003, _item(
        _elem(0x0010, 0x0010, SECRET_NAME.encode()))))

#: Values that begin with the item tag and are *not* a sequence. Each
#: name says which of the gate's four rules refuses it; three of the
#: four get past `read_sequence` without raising and without leaving
#: bytes unread, so only the byte-exact re-encode can tell them apart
#: from the real thing.
ADVERSARIAL = {
    # Rule 2: read_sequence raises.
    "truncated item header": b"\xfe\xff\x00\xe0\x22\x00",
    # Rule 4: parses as one item holding a garbage element, re-encodes
    # to sixteen *different* bytes.
    "item then garbage": _item(b"\x01\x02\x03\x04\x05\x06\x07\x08"),
    # Rule 4: the item's declared length is nonsense; the re-encode
    # writes the length actually consumed.
    "huge item length": (struct.pack("<HHI", 0xFFFE, 0xE000, 0xFFFFFF00)
                         + b"\x00" * 8),
    # Rule 4, and the reason rule 4 is not optional: an empty item
    # followed by eight bytes of vendor payload. It parses, it consumes
    # every byte, and accepting it would destroy those eight bytes.
    "coincidental vendor blob": (struct.pack("<HHI", 0xFFFE, 0xE000, 0)
                                 + b"\x01\x02\x03\x04\x05\x06\x07\x08"),
}


# --------------------------------------------------------------------------
# Fixtures on disk
# --------------------------------------------------------------------------

def _base(uid_suffix="1"):
    ds = Dataset()
    ds.SOPClassUID = SOP_CLASS
    ds.SOPInstanceUID = f"1.2.826.0.1.3680043.8.167.{uid_suffix}"
    ds.StudyInstanceUID = f"1.2.826.0.1.3680043.8.167.{uid_suffix}.1"
    ds.SeriesInstanceUID = f"1.2.826.0.1.3680043.8.167.{uid_suffix}.2"
    ds.Modality = "OT"
    ds.PatientName = "Top^Level"
    ds.PatientID = "TOP-1"
    ds.StudyDate = "20240101"
    ds.SeriesNumber = 1
    ds.InstanceNumber = 1
    ds.add_new(Tag(0x0009, 0x0010), "LO", "ACME_HEADER")
    return ds


def _save(ds, path, transfer_syntax):
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = ds.SOPClassUID
    meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    meta.TransferSyntaxUID = transfer_syntax
    ds.file_meta = meta
    ds.preamble = b"\0" * 128
    pydicom.dcmwrite(path, ds, enforce_file_format=True)
    return path


def implicit_file(folder, value=PRIVATE_SEQUENCE_BYTES, uid_suffix="1"):
    """A file whose private element is exactly `value` on the wire.

    The value is handed to pydicom as `OB`, which under Implicit VR LE
    writes the tag, a four-byte length and the bytes -- no VR, because
    the transfer syntax has nowhere to put one. That is byte for byte
    what a vendor's own writer emits for a private `SQ`, and reading the
    file back gives `UN` and bytes. Using `OB` here is a way to get the
    bytes on disk, not a claim about what they are: pydicom does not
    record the VR anywhere in the file.
    """
    folder = str(folder)
    os.makedirs(folder, exist_ok=True)
    ds = _base(uid_suffix)
    ds.add_new(Tag(0x0009, 0x1003), "OB", value)
    _save(ds, os.path.join(folder, "implicit.dcm"), ImplicitVRLittleEndian)
    return folder


def explicit_file(folder, uid_suffix="2"):
    """The same private sequence, written Explicit VR LE as a real `SQ`.

    pydicom parses this one on its own -- it is the control the implicit
    file has to converge on.
    """
    folder = str(folder)
    os.makedirs(folder, exist_ok=True)
    ds = _base(uid_suffix)
    item = Dataset()
    item.PatientName = SECRET_NAME
    item.PatientID = SECRET_ID
    ds.add_new(Tag(0x0009, 0x1003), "SQ", Sequence([item]))
    _save(ds, os.path.join(folder, "explicit.dcm"), ExplicitVRLittleEndian)
    return folder


def nested_file(folder, value=PRIVATE_SEQUENCE_BYTES, uid_suffix="3"):
    """A private `UN` value one level inside a *standard* sequence."""
    folder = str(folder)
    os.makedirs(folder, exist_ok=True)
    ds = _base(uid_suffix)
    item = Dataset()
    item.add_new(Tag(0x0009, 0x0010), "LO", "ACME_HEADER")
    item.add_new(Tag(0x0009, 0x1003), "OB", value)
    ds.ReferencedImageSequence = Sequence([item])
    _save(ds, os.path.join(folder, "nested.dcm"), ImplicitVRLittleEndian)
    return folder


# --------------------------------------------------------------------------
# Session plumbing
# --------------------------------------------------------------------------

@pytest.fixture
def sessions(tmp_path):
    """Opens sessions and guarantees they are closed.

    `Session` holds a process pool and two sqlite threads; a test that
    leaks one leaks worker subprocesses into the rest of the suite.
    """
    opened = []

    def _open(name="sess", remove_private=False, phi_tags=PHI_TAGS):
        sess = Session(str(tmp_path / f"{name}.db"))
        sess.configuration.remove_private_tags = remove_private
        if phi_tags is not None:
            sess.configuration.phi_tags = dict(phi_tags)
        opened.append(sess)
        return sess

    yield _open

    for sess in opened:
        try:
            sess.close()
        except Exception:            # pragma: no cover - teardown only
            pass


def _instance(session):
    return session.store.patients[0].studies[0].series[0].instances[0]


def _exported(folder):
    """The one exported file's bytes and its parsed dataset."""
    files = glob.glob(os.path.join(folder, "**", "*.dcm"), recursive=True)
    assert len(files) == 1, files
    with open(files[0], "rb") as handle:
        return handle.read(), pydicom.dcmread(files[0], force=True)


def _nested(findings, tag):
    """Findings for `tag` raised *inside* a sequence, not at the top."""
    return [f for f in findings if f.tag == tag and f.entity_path]


# --------------------------------------------------------------------------
# T1 -- the scan has to see inside the sequence
# --------------------------------------------------------------------------

def test_audit_finds_the_identifiers_inside_an_implicit_vr_private_sequence(
        tmp_path, sessions):
    """The reported bug, at the layer that reports clean.

    Red before the fix: the audit returned five findings, all top-level,
    and neither identifier inside the sequence was among them.
    """
    sess = sessions()
    sess.ingest(implicit_file(tmp_path / "src"))

    findings = sess.audit().findings

    names = _nested(findings, NAME_TAG)
    ids = _nested(findings, ID_TAG)
    assert [f.value for f in names] == [SECRET_NAME], (
        "the Patient Name inside the private sequence was not found")
    assert [f.value for f in ids] == [SECRET_ID], (
        "the Patient ID inside the private sequence was not found")
    assert names[0].entity_path == ((PRIVATE_SQ, 0),), (
        "the finding must carry a path to the item it was raised in, "
        "or remediation cannot reach the value")
    assert ids[0].entity_path == ((PRIVATE_SQ, 0),)


# --------------------------------------------------------------------------
# T2 -- and the export has to be clean
# --------------------------------------------------------------------------

def test_the_export_keeps_the_private_block_and_loses_the_phi(
        tmp_path, sessions):
    """`remove_private_tags=False` keeps the vendor block, not the PHI.

    Red before the fix: both identifiers were in the exported file.
    """
    sess = sessions(remove_private=False)
    sess.ingest(implicit_file(tmp_path / "src"))
    sess.audit()
    sess.anonymize()
    out = str(tmp_path / "out")
    sess.export(out, use_compression=False)

    data, ds = _exported(out)

    assert SECRET_NAME.encode() not in data, (
        "the export carries a name the audit said nothing about")
    assert SECRET_ID.encode() not in data, (
        "the export carries an MRN the audit said nothing about")
    assert Tag(0x0009, 0x1003) in ds, (
        "the caller asked to keep private tags; the block must survive")
    assert b"ANONYMIZED" in data, (
        "the remediated value has to be written where the PHI was, "
        "not merely deleted from a copy")


# --------------------------------------------------------------------------
# T3 -- one tag, one home
# --------------------------------------------------------------------------

def test_a_parsed_sequence_leaves_no_copy_in_attributes(tmp_path, sessions):
    """The tag lands in `sequences` and nowhere else.

    Keeping the bytes alongside the structure puts one tag through both
    `_merge` and `_merge_sequences`, and their call order -- not any
    stated rule -- would decide whether the exported file carried the
    remediated sequence or the original PHI-bearing bytes.
    """
    sess = sessions()
    sess.ingest(implicit_file(tmp_path / "src"))

    inst = _instance(sess)
    assert PRIVATE_SQ in inst.sequences
    assert PRIVATE_SQ not in inst.attributes, (
        "the raw bytes must not survive beside the structure")


# --------------------------------------------------------------------------
# T4 -- the structure is stored, not re-derived
# --------------------------------------------------------------------------

def test_the_sequence_survives_a_save_and_reload(tmp_path, sessions):
    """Derived at ingest and lost on load is the #84 shape, and #57's bug.

    The re-parse happens once, at ingest, and `__sequences__` carries it
    into the store. Nothing re-derives it on load, deliberately -- a
    second answer to "what is this element" is what #84 removed.
    """
    first = sessions(name="store")
    first.ingest(implicit_file(tmp_path / "src"))
    first.save(sync=True)
    first.close()

    reopened = sessions(name="store")
    inst = _instance(reopened)
    assert PRIVATE_SQ in inst.sequences, "the structure did not round-trip"

    findings = reopened.audit().findings
    assert [f.value for f in _nested(findings, NAME_TAG)] == [SECRET_NAME]
    assert [f.value for f in _nested(findings, ID_TAG)] == [SECRET_ID]


# --------------------------------------------------------------------------
# T5 -- depth is not a special case
# --------------------------------------------------------------------------

def test_a_private_sequence_inside_a_standard_sequence_is_parsed(
        tmp_path, sessions):
    """Gating the parse on the top level would leave this one silent."""
    sess = sessions()
    sess.ingest(nested_file(tmp_path / "src"))

    inst = _instance(sess)
    item = inst.sequences[STANDARD_SQ].items[0]
    assert PRIVATE_SQ in item.sequences
    assert PRIVATE_SQ not in item.attributes

    names = _nested(sess.audit().findings, NAME_TAG)
    assert [f.value for f in names] == [SECRET_NAME]
    assert names[0].entity_path == ((STANDARD_SQ, 0), (PRIVATE_SQ, 0)), (
        "the path has to reach the item two levels down")


# --------------------------------------------------------------------------
# T6 -- the two transfer syntaxes converge
# --------------------------------------------------------------------------

def test_both_transfer_syntaxes_produce_the_same_graph(tmp_path, sessions):
    """#167 is one file disagreeing with itself. This is the agreement."""
    implicit = sessions(name="imp")
    implicit.ingest(implicit_file(tmp_path / "imp_src"))
    explicit = sessions(name="exp")
    explicit.ingest(explicit_file(tmp_path / "exp_src"))

    imp_inst, exp_inst = _instance(implicit), _instance(explicit)

    assert list(imp_inst.sequences) == list(exp_inst.sequences) == [PRIVATE_SQ]
    assert PRIVATE_SQ not in imp_inst.attributes
    assert PRIVATE_SQ not in exp_inst.attributes
    assert (imp_inst.sequences[PRIVATE_SQ].items[0].attributes
            == exp_inst.sequences[PRIVATE_SQ].items[0].attributes)


def test_an_undefined_length_item_inside_a_defined_element_parses(
        tmp_path, sessions):
    """The second affected framing, and the one easy to miss.

    Element length defined, *item* length undefined with a delimiter.
    pydicom hands this back as `UN` bytes too, and the gate has to
    accept it -- it re-encodes byte for byte, delimiter included.
    """
    sess = sessions()
    sess.ingest(implicit_file(tmp_path / "src",
                              value=UNDEFINED_ITEM_BYTES))

    assert PRIVATE_SQ in _instance(sess).sequences
    assert [f.value for f in _nested(sess.audit().findings, NAME_TAG)] == [
        SECRET_NAME]


# --------------------------------------------------------------------------
# T7 -- what does not prove out is kept, byte for byte
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(ADVERSARIAL))
def test_an_unverifiable_candidate_keeps_its_bytes_exactly(
        tmp_path, sessions, name):
    """Relaxing the gate to "parsed without raising" loses vendor data.

    Three of these four get past `read_sequence` cleanly and consume
    every byte. Only re-encoding and comparing tells them from a real
    sequence -- and for the coincidental blob, accepting the parse would
    silently delete eight bytes of vendor payload.
    """
    value = ADVERSARIAL[name]
    sess = sessions()
    sess.ingest(implicit_file(tmp_path / "src", value=value))

    inst = _instance(sess)
    assert inst.sequences == {}, f"{name} was accepted as a sequence"
    assert inst.attributes[PRIVATE_SQ] == value, (
        f"{name} did not survive ingest byte for byte")


# --------------------------------------------------------------------------
# T8 -- what could not be scanned is reported and graded
# --------------------------------------------------------------------------

def test_an_unscanned_candidate_is_reported_and_grades_review_required(
        tmp_path, sessions):
    """Narrowing a silent population is not closing it (#137, #169, #194).

    The bytes are exported. What is missing is any assurance about what
    is inside them, and a run that cannot vouch for what it exported
    does not get to grade itself PASS.
    """
    sess = sessions(remove_private=False)
    sess.ingest(implicit_file(
        tmp_path / "src", value=ADVERSARIAL["coincidental vendor blob"]))
    sess.audit()
    sess.anonymize()
    sess.export(str(tmp_path / "out"), use_compression=False)
    report = str(tmp_path / "report.md")
    sess.generate_report(report)

    with open(report) as handle:
        content = handle.read()

    assert "REVIEW_REQUIRED" in content, (
        "an element the scan could not open must not grade PASS")
    assert PRIVATE_SQ in content, (
        "grading without naming the element is the count-with-no-rows "
        "defect #146 argues against")
    assert "Unscanned" in content, (
        "the row needs a section that does not claim the bytes are missing")


def test_a_verified_sequence_does_not_raise_the_grade(tmp_path, sessions):
    """The control for the test above.

    Without it, REVIEW_REQUIRED could be coming from anything -- the
    grade has several inputs. A sequence that *did* parse leaves the
    run at PASS.
    """
    sess = sessions(remove_private=False)
    sess.ingest(implicit_file(tmp_path / "src"))
    sess.audit()
    sess.anonymize()
    sess.export(str(tmp_path / "out"), use_compression=False)
    report = str(tmp_path / "report.md")
    sess.generate_report(report)

    with open(report) as handle:
        content = handle.read()

    assert "**PASS**" in content, content


# --------------------------------------------------------------------------
# T11 -- a child the scan can now see, and now loses
# --------------------------------------------------------------------------

def test_a_binary_child_of_a_parsed_sequence_is_reported_as_lost(
        tmp_path, sessions):
    """Restoring the structure makes an unreported loss reportable.

    Before the parse this instance held one opaque `UN` attribute and
    graded PASS. After it, the item's `OB` child goes through the
    ordinary rules -- binary VRs do not reach the object graph -- so it
    is a DATA_LOSS, it is private, and the run grades REVIEW_REQUIRED.
    That is a behaviour change, and it is the correct one: the loss was
    always happening, in the sense that those bytes were never going to
    be exported as an element.
    """
    sess = sessions(remove_private=False)
    sess.ingest(implicit_file(tmp_path / "src",
                              value=BINARY_CHILD_BYTES))
    sess.audit()
    sess.anonymize()
    sess.export(str(tmp_path / "out"), use_compression=False)
    report = str(tmp_path / "report.md")
    sess.generate_report(report)

    with open(report) as handle:
        content = handle.read()

    assert "0009,1002" in content, (
        "the binary child vanished with nothing saying so -- #137 again")
    assert "REVIEW_REQUIRED" in content


# --------------------------------------------------------------------------
# T9 -- the default configuration has to remove a private *sequence*
# --------------------------------------------------------------------------

@pytest.mark.parametrize("writer", [implicit_file, explicit_file],
                         ids=["implicit", "explicit"])
def test_remove_private_tags_removes_a_private_sequence(
        tmp_path, sessions, writer):
    """`remove_private_tags=True` is the default, and it missed sequences.

    The sweep built its targets from `attributes` alone, so a private
    `SQ` was never a target and `REMOVE_TAG` never looked in
    `sequences`. Under Explicit VR that has always exported an orphaned
    private sequence whose creator was stripped -- red before this fix.
    Under Implicit VR it was masked, because the blob was an *attribute*
    and the attribute sweep caught it; restoring the structure (#167)
    removes that accident, which is why the two halves ship together.
    """
    sess = sessions(remove_private=True)
    sess.ingest(writer(tmp_path / "src"))
    sess.audit()
    sess.anonymize()
    out = str(tmp_path / "out")
    sess.export(out, use_compression=False)

    _data, ds = _exported(out)

    leftover = [str(elem.tag) for elem in ds if elem.tag.group % 2]
    assert leftover == [], f"private elements survived the sweep: {leftover}"


# --------------------------------------------------------------------------
# T10 -- remove the container after remediating what is inside it
# --------------------------------------------------------------------------

def test_the_sequence_removal_is_proposed_after_its_contents(
        tmp_path, sessions):
    """Order decides whether the audit trail describes live objects.

    A configured-tag finding raised inside the sequence holds a live
    reference to an item within it. Remediating the contents first means
    every audit row describes an item that was still in the graph when
    the row was written; removing the container first leaves the nested
    remediations writing into a detached item and filing rows for it.
    """
    sess = sessions(remove_private=True)
    sess.ingest(implicit_file(tmp_path / "src"))

    uid = _instance(sess).sop_instance_uid
    findings = [f for f in sess.audit().findings if f.entity_uid == uid]

    removal = [i for i, f in enumerate(findings)
               if f.tag == PRIVATE_SQ and f.value == "<PRIVATE>"]
    nested = [i for i, f in enumerate(findings) if f.entity_path]
    assert [f.tag for f in findings if f.tag == PRIVATE_CREATOR], (
        "the attribute sweep stopped seeing the private creator; the "
        "sequence arm is an addition to it, not a replacement")
    assert removal, "the private sequence was never proposed for removal"
    assert nested, "the identifiers inside it were never found"
    assert min(removal) > max(nested), (
        "the container is removed before its contents are remediated")


def test_remove_private_tags_reaches_a_nested_private_sequence(
        tmp_path, sessions):
    """"At every depth" is a claim, so it needs a test that holds it.

    The sweep walks `iter_item_tree`, not `instance.sequences`. Replace
    the walk with the instance's own sequences and the test above still
    passes -- this is the one that goes red, and a private block one
    level inside a standard sequence is the ordinary shape for a vendor
    that hangs its data off Referenced Image Sequence.
    """
    sess = sessions(remove_private=True)
    sess.ingest(nested_file(tmp_path / "src"))
    sess.audit()
    sess.anonymize()
    out = str(tmp_path / "out")
    sess.export(out, use_compression=False)

    data, ds = _exported(out)

    assert SECRET_NAME.encode() not in data
    leftover = [str(elem.tag)
                for item in ds.get((0x0008, 0x1140)).value
                for elem in item if elem.tag.group % 2]
    assert leftover == [], (
        f"private elements survived one level down: {leftover}")


def test_an_unscanned_candidate_one_level_down_is_still_reported(
        tmp_path, sessions):
    """The row has to survive the recursion, not just the top level.

    `process_sequence` forwards `unscanned` into every nested
    `populate_attrs`, and the standard-sequence call site forwards it
    in. Drop it at either point and an unverifiable private value inside
    a standard sequence goes back to being exported with nothing saying
    so -- silent again, one level down, which is the failure this fix
    exists to close.
    """
    sess = sessions(remove_private=False)
    sess.ingest(nested_file(tmp_path / "src",
                            value=ADVERSARIAL["coincidental vendor blob"]))
    sess.audit()
    sess.anonymize()
    sess.export(str(tmp_path / "out"), use_compression=False)
    report = str(tmp_path / "report.md")
    sess.generate_report(report)

    with open(report) as handle:
        content = handle.read()

    assert PRIVATE_SQ in content, (
        "an unscanned value inside a standard sequence was not reported")
    assert "REVIEW_REQUIRED" in content


# --------------------------------------------------------------------------
# A private sequence inside a private sequence
# --------------------------------------------------------------------------

def test_a_private_sequence_inside_a_private_sequence_is_parsed(
        tmp_path, sessions):
    """The gate has to run again on the way down, not only at the top.

    A private SQ nested under a *standard* SQ -- the case above -- never
    exercises that: pydicom parses the standard container itself, and
    the inner private value reaches `populate_attrs` as an ordinary
    element of an ordinary item. Here the outer container is itself
    recovered from `UN` bytes, so the inner one is `UN` bytes *inside a
    dataset the gate just produced*, and the recursion is the only thing
    that opens it.
    """
    sess = sessions(remove_private=False)
    sess.ingest(implicit_file(tmp_path / "src",
                              value=PRIVATE_IN_PRIVATE_BYTES))

    inst = _instance(sess)
    outer = inst.sequences[PRIVATE_SQ].items[0]
    assert PRIVATE_SQ in outer.sequences, (
        "the inner private sequence stayed an opaque UN blob")
    assert PRIVATE_SQ not in outer.attributes

    names = _nested(sess.audit().findings, NAME_TAG)
    assert [f.value for f in names] == [SECRET_NAME]
    assert names[0].entity_path == ((PRIVATE_SQ, 0), (PRIVATE_SQ, 0)), (
        "the path has to reach the item two private levels down")

    sess.anonymize()
    out = str(tmp_path / "out")
    sess.export(out, use_compression=False)
    data, ds = _exported(out)

    assert SECRET_NAME.encode() not in data
    assert Tag(0x0009, 0x1003) in ds, (
        "the caller asked to keep private tags; the block must survive")


def test_a_nested_private_sequence_is_removed_before_its_container(
        tmp_path, sessions):
    """Depth-first, for the reason the removals come last at all.

    `iter_item_tree` yields a container before what is inside it, so the
    walk's own order proposes the outer sequence for removal first.
    Remediating in that order deletes the outer sequence from the
    instance and then deletes the inner one from a dict nothing can
    reach any more -- and files a `REMEDIATION_REMOVE` row saying so.
    The export is clean either way, which is exactly why only the
    ordering can be asserted here: sort the list the other way and this
    is the test that notices.
    """
    sess = sessions(remove_private=True)
    sess.ingest(implicit_file(tmp_path / "src",
                              value=PRIVATE_IN_PRIVATE_BYTES))

    uid = _instance(sess).sop_instance_uid
    findings = [f for f in sess.audit().findings if f.entity_uid == uid]
    removals = [(i, f) for i, f in enumerate(findings)
                if f.field_name.startswith("Private Sequence")]

    assert [f.entity_path for _i, f in removals] == [
        ((PRIVATE_SQ, 0),), ()], (
        "the nested sequence must be proposed for removal before the "
        "container that holds it")

    sess.anonymize()
    out = str(tmp_path / "out")
    sess.export(out, use_compression=False)
    data, ds = _exported(out)

    assert SECRET_NAME.encode() not in data
    leftover = [str(elem.tag) for elem in ds if elem.tag.group % 2]
    assert leftover == [], f"private elements survived the sweep: {leftover}"
