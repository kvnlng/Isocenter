"""Every identifier Table E.1-1 names is gone from a bare session's export (#547).

The measurement that opened #547, kept as a test: pydicom's `CT_small.dcm`
carrying one identifier of each kind the table names, through a bare
`Session` -- ingest, audit, anonymize, export -- with the written file
walked element by element. On 0.9.7, 25 of the 26 probes reached the
file.

The walk is the assertion, not the probe list. Every element in the
written file whose tag the table has must be absent, zero-length, or a
zero-item sequence -- or, where the row's code has a D arm, hold its
VR's dummy (#557), or, on a `U` row, a `2.25.` UID the source did not
carry (#544) -- unless the table gives it no rule or a named
departure says why not. Every even-group 50xx and 60xx element must be
absent outright, whatever its row (#556). The probe list only makes sure
each kind of identifier is in the input, so an edit to the fixture
cannot quietly stop testing one.

Both worker paths, because the process path pickles the 646-rule policy
into every scan task and the thread path does not.
"""
import os
import re

import pydicom
import pydicom.data
import pytest
from pydicom.dataset import Dataset
from pydicom.sequence import Sequence

from isocenter import Session
from isocenter import parallel

from support.annex_e import (DEVIATIONS, GROUP_RULES, NO_ENTRY, NO_ENTRY_CODES,
                             load_table)

#: What a value-less REPLACE writes, by VR (#557). Literal, never read
#: from `config_manager.VR_DUMMY`.
DUMMY = {"DA": "19000101", "DT": "19000101", "TM": "000000", "AS": "000D",
         **{vr: "ANONYMIZED" for vr in ("AE", "CS", "LO", "LT", "PN", "SH",
                                        "ST", "UC", "UR", "UT")},
         "OB": b"\x00\x00", "UN": b"\x00\x00"}

#: tag -> (kind of identifier, value written into the source).
PROBES = {
    # dates and times
    0x00080012: ("date", "DA", "19970430"),        # Instance Creation Date (X/D)
    0x00080013: ("date", "TM", "072731"),          # Instance Creation Time (X/Z/D)
    0x00181012: ("date", "DA", "19970430"),        # Date of Secondary Capture
    0x00380020: ("date", "DA", "19970429"),        # Admitting Date
    0x001021D0: ("date", "DA", "19970401"),        # Last Menstrual Date
    0x00080024: ("date", "DA", "19970430"),        # Overlay Date (retired)
    0x00400002: ("date", "DA", "19970430"),        # Scheduled Procedure Step Start Date
    # patient identity and demographics
    0x00101060: ("patient", "PN", "Mother^Maiden"),   # Patient's Mother's Birth Name
    0x00102154: ("patient", "SH", "555-0100"),        # Patient's Telephone Numbers
    0x00101080: ("patient", "LO", "Colonel"),         # Military Rank
    0x00102180: ("patient", "SH", "Welder"),          # Occupation
    0x00101030: ("patient", "DS", "71.5"),            # Patient's Weight
    # personnel
    0x00081048: ("personnel", "PN", "Record^Doc"),      # Physician(s) of Record
    0x00081060: ("personnel", "PN", "Reading^Doc"),     # Name of Physician(s) Reading Study
    0x00321032: ("personnel", "PN", "Requesting^Doc"),  # Requesting Physician
    # organisation and device
    0x00181000: ("device", "LO", "SN-12345"),         # Device Serial Number
    0x00181030: ("device", "LO", "Dr Smith head"),    # Protocol Name
    0x00400241: ("device", "AE", "CTSTATION1"),       # Performed Station AE Title
    # free text
    0x00324000: ("free text", "LT", "Study comments for Jane Doe"),   # Study Comments
    0x00081080: ("free text", "LO", "Admitting dx Jane"),             # Admitting Diagnoses Description
    0x00082111: ("free text", "ST", "Cropped by Dr Smith"),           # Derivation Description
    # repeating group, in a group other than 6000
    0x60024000: ("repeating group", "LT", "Overlay comment Jane"),    # Overlay Comments
}

#: Sequence probes: tag -> kind. Built in `_build`.
SEQUENCE_PROBES = {
    0x00400275: "sequence",               # Request Attributes Sequence (X)
    0x00081110: "sequence",               # Referenced Study Sequence (X/Z)
    0x04000561: "sequence",               # Original Attributes Sequence (X)
    0x00101002: "nested in a sequence",   # Other Patient IDs Sequence: nested Patient ID
    0x0040A730: "nested in a sequence",   # Content Sequence (no rule): nested DT and PN
    0x0040A073: "nested in a sequence",   # Verifying Observer Sequence (no rule): nested SQ and PN
}

KINDS = {"date", "patient", "personnel", "device", "free text", "repeating group",
         "sequence", "nested in a sequence"}

#: Retired Curve Data (50xx): removed by the `50xx,xxxx` rule since #556.
#: It survived every profile until then, and this test said so.
CURVE_ELEMENT = 0x50000005

#: Overlay Description in group 6002: free text Table E.1-1 does not list,
#: removed with its group by the `60xx,xxxx` rule (#556).
OVERLAY_DESCRIPTION = 0x60020022


def _build(path):
    ds = pydicom.dcmread(pydicom.data.get_testdata_file("CT_small.dcm"))
    for tag, (_kind, vr, value) in PROBES.items():
        ds.add_new(tag, vr, value)

    request = Dataset()
    request.add_new(0x00401001, "SH", "RP-777")      # Requested Procedure ID
    request.add_new(0x00400009, "SH", "SPS-888")     # Scheduled Procedure Step ID
    ds.add_new(0x00400275, "SQ", Sequence([request]))

    reference = Dataset()
    reference.add_new(0x00081150, "UI", "1.2.840.10008.3.1.2.3.1")
    reference.add_new(0x00081155, "UI", "1.2.826.0.1.547.7")
    ds.add_new(0x00081110, "SQ", Sequence([reference]))

    other_id = Dataset()
    other_id.add_new(0x00100020, "LO", "OTHER-ID-1")
    ds.add_new(0x00101002, "SQ", Sequence([other_id]))

    content = Dataset()
    content.add_new(0x0040A120, "DT", "19970430072731")   # DateTime
    content.add_new(0x0040A123, "PN", "Observer^Person")  # Person Name
    ds.add_new(0x0040A730, "SQ", Sequence([content]))

    original = Dataset()
    original.add_new(0x00100010, "PN", "Doe^Jane^Original")
    ds.add_new(0x04000561, "SQ", Sequence([original]))

    code = Dataset()
    code.add_new(0x00080100, "SH", "12345")
    code.add_new(0x00080102, "SH", "LOCAL")
    code.add_new(0x00080104, "LO", "Dr Observer")
    observer = Dataset()
    observer.add_new(0x0040A075, "PN", "Observer^Verifying")   # Verifying Observer Name
    observer.add_new(0x0040A088, "SQ", Sequence([code]))       # its Identification Code Sequence
    ds.add_new(0x0040A073, "SQ", Sequence([observer]))

    ds.add_new(CURVE_ELEMENT, "US", 1)                          # Curve Dimensions
    ds.add_new(OVERLAY_DESCRIPTION, "LO", "Overlay by Jane Doe")
    ds.save_as(path)
    return ds


def _key(tag):
    return f"{tag.group:04x},{tag.element:04x}"


def _is_repeating(tag):
    """An even group of the retired Curve or the Overlay range (PS3.5 7.6)."""
    return ((0x5000 <= tag.group <= 0x501E or 0x6000 <= tag.group <= 0x601E)
            and tag.group % 2 == 0)


def _row_for(rows, tag):
    """The element's row, most specific first: the concrete key, then the
    table's element mask, then its group mask. Written here, not imported
    from `privacy._rule_for`, so a change there cannot move this."""
    key = _key(tag)
    if key in rows:
        return rows[key]
    if _is_repeating(tag):
        prefix = f"{tag.group:04x}"[:2]
        return (rows.get(f"{prefix}xx,{tag.element:04x}")
                or rows.get(f"{prefix}xx,xxxx"))
    return None


_REPLACED_UID = re.compile(r"2\.25\.(0|[1-9][0-9]*)")


def _uid_values(element):
    value = element.value
    return [str(v) for v in (value if isinstance(value, (list, tuple, pydicom.multival.MultiValue))
                             else [value])]


def _is_replaced_uid(element, source_uids):
    """A UI element whose every value is a `2.25.` UID the source file
    carried nowhere: what UID replacement writes (#544). The derivation
    itself is pinned in `test_uids_are_replaced_by_the_project_secret.py`;
    here only that nothing of the source's UIDs survives."""
    values = _uid_values(element)
    return (element.VR == "UI" and bool(values)
            and all(_REPLACED_UID.fullmatch(v) and v not in source_uids for v in values))


def _is_empty(element):
    if element.VR == "SQ":
        return len(element.value) == 0
    return element.value in (None, "", b"") or element.is_empty


@pytest.fixture
def strategy(request, monkeypatch):
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)
    if request.param == "processes":
        monkeypatch.delenv("ISOCENTER_FORCE_THREADS", raising=False)
        monkeypatch.setenv("ISOCENTER_FORCE_PROCESSES", "1")
    else:
        monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
        monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    return request.param


def test_the_probes_cover_every_kind_of_identifier():
    """Kills a probe list edited down until a kind of identifier is no
    longer in the input, and a probe on a tag the table gives no rule
    (which would test nothing)."""
    rows = {row["key"]: row for row in load_table()["rows"]}
    assert {kind for kind, _vr, _value in PROBES.values()} | set(
        SEQUENCE_PROBES.values()) == KINDS
    for tag in list(PROBES) + [t for t, k in SEQUENCE_PROBES.items() if k == "sequence"]:
        row = _row_for(rows, pydicom.tag.Tag(tag))
        assert row is not None, f"{tag:08x} is not a Table E.1-1 row"
        assert row["basic"] not in NO_ENTRY_CODES and row["key"] not in NO_ENTRY
        # A row folded into a group rule has no rule of its own; a probe
        # on it tests something only while a group rule covers it (#556).
        if DEVIATIONS.get(row["key"], {"action": ""})["action"] is None:
            group = row["key"][:2] + "xx,xxxx"
            assert group in GROUP_RULES or group in rows, (
                f"{tag:08x}: its row has no rule and no group rule covers it")


@pytest.mark.parametrize("strategy", ["threads", "processes"], indirect=True)
def test_end_to_end_every_identifier_type_is_gone(tmp_path, strategy, monkeypatch):
    """Red on 0.9.7: 25 of the 26 original probes reached the file.

    Kills any single profile entry for a probe removed (its value
    survives), the configured-sequence scan removed (a sequence survives
    with its items), and the overlay rule not written for group 6002."""
    resolved = []
    announce = parallel._announce_recycling_override

    def spy(chosen):
        resolved.append(chosen.use_threads)
        return announce(chosen)

    monkeypatch.setattr(parallel, "_announce_recycling_override", spy)

    os.makedirs(tmp_path / "in")
    source = _build(str(tmp_path / "in" / "ct.dcm"))

    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        resolved.clear()
        session.anonymize(session.audit())
        assert resolved and resolved[0] is (strategy == "threads"), (
            f"the audit ran on {resolved}, not {strategy}")
        summary = session.export(str(tmp_path / "out"), use_compression=False)

    assert summary.written == 1, summary.failures
    written = [os.path.join(root, name) for root, _, names in os.walk(tmp_path / "out")
               for name in names if name.endswith(".dcm")]
    out = pydicom.dcmread(written[0])

    rows = {row["key"]: row for row in load_table()["rows"]}
    no_rule = set(NO_ENTRY) | {key for key, d in DEVIATIONS.items() if d["action"] is None}
    # Owned by the Patient and the Study (#537): the name's and the ID's
    # REPLACE write `ANONYMIZED` and the keyed pseudonym, asserted below.
    entity_owned = {"0010,0010", "0010,0020"}
    # The floor's research defaults: Patient's Sex and Age kept, Study
    # Date jittered rather than the table's Z (the basic profile empties
    # it since #537).
    floor_keeps = {"0010,0040", "0010,1010", "0008,0020"}

    source_uids = {v for element in source.iterall() if element.VR == "UI"
                   for v in _uid_values(element)}
    survivors = []
    for element in out.iterall():
        # Every curve and overlay element goes, whatever its row: the
        # group rules cover the rows folded into them (#556).
        if _is_repeating(element.tag):
            survivors.append((_key(element.tag), "repeating group", element.value))
            continue
        row = _row_for(rows, element.tag)
        if row is None or row["basic"] in NO_ENTRY_CODES:
            continue
        key = row["key"]
        if key in no_rule or key in entity_owned or key in floor_keeps:
            continue
        if _is_empty(element):
            continue
        # A D arm is replaced, not emptied: the element holds its VR's
        # dummy and nothing of the original (#557).
        if ("D" in row["basic"].split("/") and element.VR != "SQ"
                and element.value == DUMMY.get(element.VR)):
            continue
        # A U row, and Annotation Group UID's D, is UID replacement (#544).
        if ((row["basic"] == "U" or key == "006a,0003")
                and _is_replaced_uid(element, source_uids)):
            continue
        survivors.append((_key(element.tag), row["name"], element.value))
    assert survivors == [], survivors

    # The owned three hold the floor's replacements: its REPLACE rows on
    # the name and ID, its JITTER on the date (#537).
    assert out.PatientName == "ANONYMIZED"
    assert re.fullmatch(r"ANON_[0-9a-f]{24}", out.PatientID), out.PatientID
    assert out.StudyDate and out.StudyDate != source.StudyDate

    # Emptied, not removed: a zero-item sequence is present.
    assert (0x0008, 0x1110) in out and len(out[0x0008, 0x1110].value) == 0
    for tag in (0x00400275, 0x04000561):
        assert tag not in out, f"{tag:08x} survived"
    # Cleaned by recursion: the containers with no rule keep their items,
    # and what is inside them is empty.
    assert len(out[0x0040, 0xA730].value) == 1
    observer = out[0x0040, 0xA073].value[0]
    # D: Verifying Observer Name is Type 1 in an SR, so it is replaced by
    # a dummy rather than emptied (#557).
    assert observer[0x0040, 0xA075].value == "ANONYMIZED"
    assert len(observer[0x0040, 0xA088].value) == 0
    # X/D: removed until #557, now holding the DA dummy.
    assert out[0x0008, 0x0012].value == "19000101"

    # The gap this test used to assert, closed: the retired Curve group
    # and the whole overlay group are removed (#556).
    assert CURVE_ELEMENT not in out
    assert OVERLAY_DESCRIPTION not in out
