"""A file whose linkage key is written under a VR that is not text is refused,
that one file, in the worker (#1022, owner ruling Q3 A, 2026-10-09).

Ingest links a file by four elements: SOP Instance UID, Study Instance UID,
Series Instance UID and Patient ID. An Explicit VR file states a VR for
each, and nothing makes it state the dictionary's. Measured on `main` at
07bdbcad, on 3.12.14 and 3.14.7t (pydicom 3.0.2), one such file beside one
ordinary file:

- SOP Instance UID or Patient ID under OB, OW, PN, US, FD, AT, DS or IS:
  `ingest()` **raised** `ValueError` from the save that ends it (#721's
  sentence, #949's), the store held nothing, the ordinary file included,
  and both files' frames were left in the sidecar;
- Study or Series Instance UID under the same VRs: the file was
  **ingested, with no row**, linked under `str()` of what pydicom read --
  `"b'1.2.3.4\\x00'"`, `'7'`, `'1.5'`, `'[]'`;
- any of the four under SQ (SOP Instance UID, Patient ID): refused, with a
  row quoting the interpreter (`unhashable type: 'Sequence'` on 3.12,
  other words on 3.14t), the Patient ID's frame already in the sidecar;
- any of the four under IS holding `1e400`: refused in pydicom's words,
  `OverflowError: cannot convert float infinity to integer`.

All of them are now refused by `isocenter.io_handlers.ingest_worker`,
before anything converts the value, with one sentence that names the
element and the VR the file states and nothing else: never a value, a
Python type or an interpreter's words. Every expected row is a literal
built here, which is what makes it one row on both interpreters.

**The rule reads the VR, not the value.** A DS or IS element whose text
pydicom cannot convert (`abc`, `1.2.3.4`) is handed over as a `str`; a
rule on "is the value a `str`" would ingest `(0020,000D) DS '1.2.3.4'`
and refuse `(0020,000D) DS '1.5'`. Both are refused.
"""
import os
import sqlite3
from pathlib import Path

import pydicom
import pytest
from pydicom.dataset import Dataset
from pydicom.multival import MultiValue
from pydicom.sequence import Sequence
from pydicom.uid import ImplicitVRLittleEndian

from isocenter import Session
from isocenter import io_handlers

from support.ct_small_files import row_counts, study_uid, write_ct

IGNORE = pydicom.config.IGNORE
REPO = Path(__file__).resolve().parents[1]

#: keyword -> (tag, the name the sentence uses, the `meta` key).
KEYS = {
    "SOPInstanceUID": (0x00080018, "SOP Instance UID (0008,0018)", "sop"),
    "StudyInstanceUID": (0x0020000D, "Study Instance UID (0020,000D)", "sid"),
    "SeriesInstanceUID": (0x0020000E, "Series Instance UID (0020,000E)", "ser_id"),
    "PatientID": (0x00100020, "Patient ID (0010,0020)", "pid"),
}
KEY_IDS = list(KEYS)

#: The nine VRs of the issue and the spec, with a value pydicom writes.
REFUSED = {
    "OB": lambda: b"1.2.3.4\x00",
    "OW": lambda: b"1.2.3.4\x00",
    "PN": lambda: "1.2.3.4",
    "US": lambda: 7,
    "FD": lambda: 1.5,
    "AT": lambda: 0x00100020,
    "DS": lambda: "1.5",
    "IS": lambda: "7",
    "SQ": lambda: Sequence([Dataset()]),
}

#: More of the same rule: the other VRs pydicom does not read as `str`,
#: and the values that are falsy, empty or several.
ALSO_REFUSED = [
    ("OB", lambda: b"", "empty"),
    ("US", lambda: 0, "zero"),
    ("US", lambda: None, "empty"),
    ("US", lambda: [1, 2], "two"),
    ("FD", lambda: 0.0, "zero"),
    ("IS", lambda: "0", "zero"),
    ("SQ", lambda: Sequence([]), "empty"),
    ("SS", lambda: -2, "one"), ("SL", lambda: -1, "one"),
    ("UL", lambda: 5, "one"), ("FL", lambda: 1.5, "one"),
    ("SV", lambda: -5, "one"), ("UV", lambda: 5, "one"),
    ("OF", lambda: b"\x00" * 4, "one"), ("OD", lambda: b"\x00" * 8, "one"),
    ("OL", lambda: b"\x00" * 4, "one"), ("OV", lambda: b"\x00" * 8, "one"),
]

#: Text the file holds under DS or IS, written past pydicom (`_patched`).
#: `1e400` is the one whose read raises; `abc` and `1.2.3.4` are the ones
#: pydicom hands over as a `str`; `1\\2` is a `MultiValue` of numbers.
RAW = [("IS", "1e400"), ("IS", "abc"), ("IS", "1\\2"),
       ("DS", "abc"), ("DS", "1.2.3.4"), ("DS", "inf")]

#: The VRs pydicom reads as `str`, each with a value it accepts.
ADMITTED = {
    "UI": "1.2.826.1022.5", "LO": "1.2.826.1022.5", "SH": "1.2.826.1022.5",
    "UC": "1.2.826.1022.5", "UT": "1.2.826.1022.5", "LT": "1.2.826.1022.5",
    "ST": "1.2.826.1022.5", "UR": "1.2.826.1022.5", "CS": "A1022",
    "AE": "A1022", "DA": "20200101", "TM": "1200", "DT": "2020", "AS": "010Y",
}


def _sentence(name, vr):
    return (f"ValueError: {name} is written as {vr}, not as text; the file "
            f"is linked by it, and no text is chosen for it.")


def _bad(tmp_path, keyword, vr, value, name="bad.dcm"):
    """CT_small of patient BAD with `keyword` written under `vr`."""
    tag = KEYS[keyword][0]
    path = write_ct(tmp_path / "in" / name, "BAD", 1022)
    ds = pydicom.dcmread(path)
    ds[tag] = pydicom.DataElement(tag, vr, value, validation_mode=IGNORE)
    ds.save_as(path)
    # The premise, read back raw: the file states this VR for the key.
    assert pydicom.dcmread(path).get_item(tag).VR == vr
    return path


def _patched(tmp_path, keyword, vr, text, name="bad.dcm"):
    """The same, for text pydicom's writer refuses under DS or IS: written
    under SH and the two VR bytes then replaced in the file."""
    tag = KEYS[keyword][0]
    path = write_ct(tmp_path / "in" / name, "BAD", 1022)
    ds = pydicom.dcmread(path)
    ds[tag] = pydicom.DataElement(tag, "SH", text, validation_mode=IGNORE)
    ds.save_as(path)
    with open(path, "rb") as handle:
        data = handle.read()
    head = ((tag >> 16).to_bytes(2, "little")
            + (tag & 0xFFFF).to_bytes(2, "little"))
    assert data.count(head + b"SH") == 1
    with open(path, "wb") as handle:
        handle.write(data.replace(head + b"SH", head + vr.encode("ascii")))
    assert pydicom.dcmread(path).get_item(tag).VR == vr
    return path


def _wire_un(tmp_path, keyword, text, name="bad.dcm"):
    """The same key stated `UN` on the wire, holding `text`.

    pydicom's writer will not do it: handed a `UN` element of a tag it
    knows, it writes the dictionary's VR (measured: the file reads back
    `UI`). So the element is written under SH and its header rewritten
    from the short form (VR, 2-byte length) to `UN`'s long form (VR, two
    reserved bytes, 4-byte length).
    """
    tag = KEYS[keyword][0]
    path = write_ct(tmp_path / "in" / name, "BAD", 1022)
    ds = pydicom.dcmread(path)
    ds[tag] = pydicom.DataElement(tag, "SH", text, validation_mode=IGNORE)
    ds.save_as(path)
    with open(path, "rb") as handle:
        data = handle.read()
    head = ((tag >> 16).to_bytes(2, "little")
            + (tag & 0xFFFF).to_bytes(2, "little"))
    assert data.count(head + b"SH") == 1
    at = data.index(head + b"SH")
    length = int.from_bytes(data[at + 6:at + 8], "little")
    with open(path, "wb") as handle:
        handle.write(data[:at] + head + b"UN\x00\x00"
                     + length.to_bytes(4, "little") + data[at + 8:])
    assert pydicom.dcmread(path).get_item(tag).VR == "UN"
    return path


def _good(tmp_path):
    return write_ct(tmp_path / "in" / "good.dcm", "GOOD", 1023)


def _rows(db, *kinds):
    with sqlite3.connect(str(db)) as conn:
        rows = conn.execute(
            "SELECT action_type, entity_uid, details FROM audit_log").fetchall()
    return [row for row in rows if row[0] in kinds]


def _sidecar_bytes(db):
    """The sidecar's size, 0 when the store never created one."""
    sidecar = os.path.splitext(str(db))[0] + "_pixels.bin"
    return os.path.getsize(sidecar) if os.path.exists(sidecar) else 0


_ONE_FRAME = []


def _one_frame(tmp_path):
    """The sidecar of a session that ingested the ordinary file alone."""
    if not _ONE_FRAME:
        folder = tmp_path / "control"
        write_ct(folder / "in" / "good.dcm", "GOOD", 1023)
        db = folder / "c.db"
        with Session(str(db)) as session:
            assert session.ingest(str(folder / "in")).ingested == 1
        _ONE_FRAME.append(_sidecar_bytes(db))
        assert _ONE_FRAME[0] > 0
    return _ONE_FRAME[0]


def _instances(session):
    return [i for p in session.store.patients for st in p.studies
            for se in st.series for i in se.instances]


# ---------------------------------------------------------------------------
# 1. The folder: one file refused, the file beside it ingested
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("vr", list(REFUSED))
@pytest.mark.parametrize("keyword", KEY_IDS)
def test_the_file_is_refused_and_the_file_beside_it_is_ingested(
        tmp_path, keyword, vr):
    """Red on main in all 36 cells: 16 by `ingest()` raising, 18 by a
    silent ingest, 2 by a row in the interpreter's words."""
    bad = _bad(tmp_path, keyword, vr, REFUSED[vr]())
    _good(tmp_path)
    reason = _sentence(KEYS[keyword][1], vr)
    db = tmp_path / "s.db"
    with Session(str(db)) as session:
        summary = session.ingest(str(tmp_path / "in"))
        assert summary.ingested == 1
        assert summary.failures == [(bad, reason)]
        assert [p.patient_id for p in session.store.patients] == ["GOOD"]
        [instance] = _instances(session)
        assert instance.sop_instance_uid == f"{study_uid(1023)}.1.1"
    assert _rows(db, "ERROR", "WARNING") == [
        ("ERROR", bad, f"Ingest failed for {bad}: {reason}")]
    assert row_counts(db) == (1, 1, 1, 1)
    # The refusal is the worker's, so the parent appended one frame: the
    # ordinary file's.
    assert _sidecar_bytes(db) == _one_frame(tmp_path)


@pytest.mark.parametrize("keyword", KEY_IDS)
def test_a_key_whose_read_raises_is_refused_in_the_same_words(tmp_path, keyword):
    """`IS 1e400`: pydicom raises `OverflowError` converting it, on any
    read of the element. Red on main with the row `Ingest failed for …:
    OverflowError: cannot convert float infinity to integer`."""
    bad = _patched(tmp_path, keyword, "IS", "1e400")
    with pytest.raises(OverflowError):
        pydicom.dcmread(bad).get(keyword)
    _good(tmp_path)
    reason = _sentence(KEYS[keyword][1], "IS")
    db = tmp_path / "s.db"
    with Session(str(db)) as session:
        summary = session.ingest(str(tmp_path / "in"))
        assert (summary.ingested, summary.failures) == (1, [(bad, reason)])
    assert _rows(db, "ERROR", "WARNING") == [
        ("ERROR", bad, f"Ingest failed for {bad}: {reason}")]
    assert _sidecar_bytes(db) == _one_frame(tmp_path)


# ---------------------------------------------------------------------------
# 2. The worker: the reason whole, for every VR that is not text
# ---------------------------------------------------------------------------

def _refusal(result, path):
    """The reason of a refused result, having checked its shape."""
    assert len(result) == 8
    assert result[0] == {"path": path}
    assert result[1:7] == (None,) * 6
    return result[7]


@pytest.mark.parametrize("vr", list(REFUSED))
@pytest.mark.parametrize("keyword", KEY_IDS)
def test_the_worker_returns_the_reason_and_never_raises(tmp_path, keyword, vr):
    path = _bad(tmp_path, keyword, vr, REFUSED[vr]())
    reason = _refusal(io_handlers.ingest_worker(path), path)
    assert reason == _sentence(KEYS[keyword][1], vr)


@pytest.mark.parametrize("vr, value, _what", ALSO_REFUSED,
                         ids=[f"{vr}-{what}" for vr, _v, what in ALSO_REFUSED])
@pytest.mark.parametrize("keyword", KEY_IDS)
def test_the_rule_is_the_vr_whatever_the_value(tmp_path, keyword, vr, value, _what):
    """A zero, an empty element and two values are refused as any other
    value under that VR is. On main a SOP Instance UID of `US 0` read
    `Missing SOPInstanceUID`, an empty `OB` Study Instance UID was a file
    with no Study Instance UID, and an empty `SQ` one linked under `[]`."""
    path = _bad(tmp_path, keyword, vr, value())
    reason = _refusal(io_handlers.ingest_worker(path), path)
    assert reason == _sentence(KEYS[keyword][1], vr)


@pytest.mark.parametrize("vr, text", RAW, ids=[f"{vr}-{text}" for vr, text in RAW])
@pytest.mark.parametrize("keyword", KEY_IDS)
def test_a_number_string_vr_is_refused_whatever_its_text(tmp_path, keyword, vr, text):
    """pydicom hands `DS 'abc'` and `DS '1.2.3.4'` over as a `str`, so a
    rule on the value would ingest them and refuse `DS '1.5'`."""
    path = _patched(tmp_path, keyword, vr, text)
    reason = _refusal(io_handlers.ingest_worker(path), path)
    assert reason == _sentence(KEYS[keyword][1], vr)


def test_pydicom_reads_unconvertible_number_text_as_a_str(tmp_path):
    """The premise of the test above, so it cannot pass for nothing."""
    path = _patched(tmp_path, "StudyInstanceUID", "DS", "1.2.3.4")
    assert type(pydicom.dcmread(path).get("StudyInstanceUID")) is str


def test_the_reason_holds_no_value_no_type_and_no_interpreter_text(tmp_path):
    told = []
    for keyword in KEYS:
        for vr, value in (("OB", b"SECRET-1022\x00"), ("PN", "SECRET-1022"),
                          ("SQ", Sequence([Dataset()]))):
            path = _bad(tmp_path, keyword, vr, value, name=f"{keyword}-{vr}.dcm")
            told.append(_refusal(io_handlers.ingest_worker(path), path))
        path = _patched(tmp_path, keyword, "IS", "1e400", name=f"{keyword}-raw.dcm")
        told.append(_refusal(io_handlers.ingest_worker(path), path))
    assert len(set(told)) == 16
    for reason in told:
        for word in ("SECRET", "TypeError", "OverflowError", "unhashable",
                     "bytes", "PersonName", "Sequence", "infinity", "b'"):
            assert word not in reason, (word, reason)


# ---------------------------------------------------------------------------
# 3. Controls: text is ingested, and #747's sentence is #747's
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("vr", list(ADMITTED))
@pytest.mark.parametrize("keyword", KEY_IDS)
def test_a_key_under_a_vr_pydicom_reads_as_text_is_handed_on(tmp_path, keyword, vr):
    """Green on main. The key is whatever text the file holds."""
    path = _bad(tmp_path, keyword, vr, ADMITTED[vr])
    meta, error = (lambda r: (r[0], r[7]))(io_handlers.ingest_worker(path))
    assert error is None
    assert meta[KEYS[keyword][2]] == ADMITTED[vr]
    assert isinstance(meta[KEYS[keyword][2]], str)


@pytest.mark.parametrize("keyword", KEY_IDS)
def test_a_key_stated_un_is_read_under_the_dictionarys_vr(tmp_path, keyword):
    """Green on main. `UN` is the file stating no VR: pydicom reads the
    key under the dictionary's `UI` or `LO`, as for an Implicit VR file."""
    path = _wire_un(tmp_path, keyword, "1.2.826.1022.5")
    meta, error = (lambda r: (r[0], r[7]))(io_handlers.ingest_worker(path))
    assert error is None
    assert meta[KEYS[keyword][2]] == "1.2.826.1022.5"
    assert isinstance(meta[KEYS[keyword][2]], str)


def test_a_file_of_pydicoms_own_that_states_un_for_all_four_keys_is_ingested():
    """Green on main, and the file the output fingerprint caught the rule
    on: as first written it refused this one, `rtdose_rle.dcm` and
    pydicom-data's `explicit_VR-UN.dcm`, three files that export today."""
    path = pydicom.data.get_testdata_file("rtdose_rle_1frame.dcm")
    raw = pydicom.dcmread(path)
    assert [raw.get_item(tag).VR for tag, _n, _k in KEYS.values()] == ["UN"] * 4
    meta, error = (lambda r: (r[0], r[7]))(io_handlers.ingest_worker(path))
    assert error is None
    assert all(isinstance(meta[key], str) and meta[key]
               for _t, _n, key in KEYS.values())


@pytest.mark.parametrize("keyword", KEY_IDS)
def test_two_values_stated_un_still_get_the_multi_valued_sentence(tmp_path, keyword):
    path = _wire_un(tmp_path, keyword, "1.2.826.1022.5\\1.2.826.1022.6 ")
    assert isinstance(pydicom.dcmread(path).get(keyword), MultiValue)
    assert _refusal(io_handlers.ingest_worker(path), path) == (
        f"ValueError: {KEYS[keyword][1]} holds 2 values and takes one; the "
        f"file is linked by it, and none is chosen for it.")


@pytest.mark.parametrize("keyword", KEY_IDS)
def test_un_is_refused_when_pydicom_is_told_to_leave_it_as_bytes(
        tmp_path, monkeypatch, keyword):
    """`pydicom.config.replace_un_with_known_vr = False` is the one way a
    `UN` key reaches the graph as `bytes`. Then it is a VR that is not
    text, like `OB`. In this process: the switch does not travel to a
    spawned worker."""
    path = _wire_un(tmp_path, keyword, "1.2.826.1022.5")
    monkeypatch.setattr(pydicom.config, "replace_un_with_known_vr", False)
    assert type(pydicom.dcmread(path).get(keyword)) is bytes
    assert _refusal(io_handlers.ingest_worker(path), path) == _sentence(
        KEYS[keyword][1], "UN")


def test_an_implicit_vr_file_states_no_vr_and_is_ingested(tmp_path):
    """Green on main. The raw element's VR is None there, which is not a
    VR that is not text."""
    path = write_ct(tmp_path / "in" / "implicit.dcm", "BAD", 1022)
    ds = pydicom.dcmread(path)
    ds.file_meta.TransferSyntaxUID = ImplicitVRLittleEndian
    ds.save_as(path, implicit_vr=True, little_endian=True)
    raw = pydicom.dcmread(path)
    assert raw.file_meta.TransferSyntaxUID == ImplicitVRLittleEndian
    assert [raw.get_item(tag).VR for tag, _n, _k in KEYS.values()] == [None] * 4
    meta, error = (lambda r: (r[0], r[7]))(io_handlers.ingest_worker(path))
    assert error is None
    assert (meta["sop"], meta["pid"]) == (f"{study_uid(1022)}.1.1", "BAD")


@pytest.mark.parametrize("vr", ["UN", "LO", "SH"])
@pytest.mark.parametrize("keyword", KEY_IDS)
def test_a_folder_with_a_key_under_un_lo_or_sh_is_ingested_whole(tmp_path, keyword, vr):
    """Green on main: the issue's and the spec's three ordinary VRs."""
    if vr == "UN":
        _wire_un(tmp_path, keyword, "1.2.826.1022.5")
    else:
        _bad(tmp_path, keyword, vr, "1.2.826.1022.5")
    _good(tmp_path)
    db = tmp_path / "s.db"
    with Session(str(db)) as session:
        summary = session.ingest(str(tmp_path / "in"))
        assert (summary.ingested, summary.failures) == (2, [])
    assert _rows(db, "ERROR") == []
    assert row_counts(db) == (2, 2, 2, 2)


@pytest.mark.parametrize("keyword", KEY_IDS)
def test_two_values_under_a_text_vr_still_get_the_multi_valued_sentence(
        tmp_path, keyword):
    """#747's refusal is untouched: a text VR passes this rule, and two
    values are refused in #747's words."""
    tag, name, _key = KEYS[keyword]
    path = write_ct(tmp_path / "in" / "two.dcm", "BAD", 1022)
    ds = pydicom.dcmread(path)
    ds[tag] = pydicom.DataElement(tag, ds[tag].VR, "1.2.826.1022.5\\1.2.826.1022.6",
                                  validation_mode=IGNORE)
    ds.save_as(path)
    assert isinstance(pydicom.dcmread(path).get(keyword), MultiValue)
    assert _refusal(io_handlers.ingest_worker(path), path) == (
        f"ValueError: {name} holds 2 values and takes one; the file is "
        f"linked by it, and none is chosen for it.")


def test_a_file_with_no_sop_instance_uid_is_still_missing_one(tmp_path):
    """Green on main: an absent key states no VR."""
    path = write_ct(tmp_path / "in" / "none.dcm", "BAD", 1022)
    ds = pydicom.dcmread(path)
    del ds.SOPInstanceUID
    ds.save_as(path)
    assert _refusal(io_handlers.ingest_worker(path), path) == (
        "ValueError: Missing SOPInstanceUID. Likely not a valid DICOM file.")


def test_a_file_holding_a_file_meta_and_no_dataset_is_still_missing_one(tmp_path):
    """Green on main: none of the four keys is there to state a VR."""
    source = pydicom.dcmread(write_ct(tmp_path / "in" / "a.dcm", "BAD", 1022))
    empty = pydicom.dataset.FileDataset(
        None, {}, file_meta=source.file_meta, preamble=b"\0" * 128)
    path = str(tmp_path / "in" / "meta-only.dcm")
    empty.save_as(path)
    assert len(pydicom.dcmread(path)) == 0
    assert _refusal(io_handlers.ingest_worker(path), path) == (
        "ValueError: Missing SOPInstanceUID. Likely not a valid DICOM file.")


# ---------------------------------------------------------------------------
# 4. The first key in the walk speaks, and each key is walked
# ---------------------------------------------------------------------------

def test_a_file_with_two_such_keys_is_refused_for_the_first_of_the_four(tmp_path):
    path = _bad(tmp_path, "PatientID", "US", 7)
    ds = pydicom.dcmread(path)
    ds[0x0020000E] = pydicom.DataElement(0x0020000E, "OB", b"1.2\x00\x00")
    ds.save_as(path)
    assert _refusal(io_handlers.ingest_worker(path), path) == _sentence(
        "Series Instance UID (0020,000E)", "OB")


ORDER = ["SOPInstanceUID", "StudyInstanceUID", "SeriesInstanceUID", "PatientID"]


@pytest.mark.parametrize("first", range(4), ids=ORDER)
def test_the_walk_is_sop_study_series_patient_id(tmp_path, first):
    """Every key from `first` on states `OB`; the one named is `first`.
    A walk in any other order names another for at least one case."""
    path = write_ct(tmp_path / "in" / "bad.dcm", "BAD", 1022)
    ds = pydicom.dcmread(path)
    for keyword in ORDER[first:]:
        tag = KEYS[keyword][0]
        ds[tag] = pydicom.DataElement(tag, "OB", b"1.2\x00\x00")
    ds.save_as(path)
    assert _refusal(io_handlers.ingest_worker(path), path) == _sentence(
        KEYS[ORDER[first]][1], "OB")


@pytest.mark.parametrize("keyword", KEY_IDS)
def test_each_key_is_named_by_its_own_name_and_tag(tmp_path, keyword):
    """The name a row carries, spelled here and not read from `KEYS`."""
    spelled = {"SOPInstanceUID": "SOP Instance UID (0008,0018)",
               "StudyInstanceUID": "Study Instance UID (0020,000D)",
               "SeriesInstanceUID": "Series Instance UID (0020,000E)",
               "PatientID": "Patient ID (0010,0020)"}[keyword]
    path = _bad(tmp_path, keyword, "OB", b"1.2\x00\x00")
    reason = _refusal(io_handlers.ingest_worker(path), path)
    assert reason.startswith(f"ValueError: {spelled} is written as OB, ")
    assert [n for n in ("SOP Instance", "Study Instance", "Series Instance",
                        "Patient ID") if n in reason] == [spelled.split(" UID")[0].split(" (")[0]]


# ---------------------------------------------------------------------------
# 4b. What the file states is read without converting it (review of #1046)
# ---------------------------------------------------------------------------

def _header(tmp_path, keyword, vr, payload, long=False, length=None,
            name="bad.dcm"):
    """CT_small of patient BAD whose `keyword` header is these bytes.

    `vr` is two bytes, whatever they are. `long` writes the 12-byte header
    (VR, two reserved bytes, 4-byte length) that `OB`, `UN` and `SQ` take;
    `length` overrides the length written, for an undefined one.
    """
    tag = KEYS[keyword][0]
    path = write_ct(tmp_path / "in" / name, "BAD", 1022)
    ds = pydicom.dcmread(path)
    ds[tag] = pydicom.DataElement(tag, "SH", "X", validation_mode=IGNORE)
    ds.save_as(path)
    with open(path, "rb") as handle:
        data = handle.read()
    head = ((tag >> 16).to_bytes(2, "little")
            + (tag & 0xFFFF).to_bytes(2, "little"))
    assert data.count(head + b"SH") == 1
    at = data.index(head + b"SH")
    old = int.from_bytes(data[at + 6:at + 8], "little")
    stated = len(payload) if length is None else length
    size = (b"\x00\x00" + stated.to_bytes(4, "little") if long
            else stated.to_bytes(2, "little"))
    with open(path, "wb") as handle:
        handle.write(data[:at] + head + vr + size + payload + data[at + 8 + old:])
    return path


@pytest.mark.parametrize("keyword", KEY_IDS)
def test_an_empty_key_under_a_vr_pydicom_does_not_know_gets_the_rules_sentence(
        tmp_path, keyword):
    """Red at e4c21e85: `get_item` without `keep_deferred` converted the
    zero-length element, and the reason was pydicom's
    `NotImplementedError: Unknown Value Representation 'ZZ'`."""
    path = _header(tmp_path, keyword, b"ZZ", b"")
    raw = pydicom.dcmread(path).get_item(KEYS[keyword][0], keep_deferred=True)
    assert (raw.VR, raw.length) == ("ZZ", 0)
    assert _refusal(io_handlers.ingest_worker(path), path) == _sentence(
        KEYS[keyword][1], "ZZ")


@pytest.mark.parametrize("keyword", KEY_IDS)
def test_the_same_vr_with_a_value_gets_the_same_sentence(tmp_path, keyword):
    path = _header(tmp_path, keyword, b"ZZ", b"1.2.3 ")
    assert _refusal(io_handlers.ingest_worker(path), path) == _sentence(
        KEYS[keyword][1], "ZZ")


@pytest.mark.parametrize("vr", [b"Z\n", b"Z ", b"Z\x00", b"Z\xe9", b"Zz", b"Z1"],
                         ids=["newline", "space", "nul", "latin-1", "lower", "digit"])
@pytest.mark.parametrize("keyword", KEY_IDS)
def test_two_bytes_that_are_not_two_capital_letters_are_never_quoted(
        tmp_path, keyword, vr):
    """Red at e4c21e85 for the newline: `… is written as Z\\n, not as text`
    reached `IngestSummary.failures` with the newline in it."""
    path = _header(tmp_path, keyword, vr, b"1.2.3 ")
    try:
        stated = pydicom.dcmread(path).get_item(KEYS[keyword][0], keep_deferred=True).VR
    except Exception:  # pydicom may not take these two bytes as a VR at all
        stated = None
    reason = _refusal(io_handlers.ingest_worker(path), path)
    if stated is not None and len(str(stated)) == 2 and str(stated)[0] == "Z":
        # pydicom took the two bytes as the VR: the rule speaks, quoting none.
        assert reason == (
            f"ValueError: {KEYS[keyword][1]} is written under a VR the "
            f"standard does not define, not as text; the file is linked by "
            f"it, and no text is chosen for it.")
    for fragment in ("\n", "\x00", "Z ", "Zz", "Z1", "\xe9"):
        assert fragment not in reason, (fragment, reason)


@pytest.mark.parametrize("stated, words", [
    ("OB", "as OB"), ("ZZ", "as ZZ"), (pydicom.valuerep.VR.SQ, "as SQ"),
    ("Zz", None), ("ob", None), ("Z\n", None), ("Z ", None), ("Z1", None),
    ("É" * 2, None), ("O", None), ("OBX", None), ("", None)])
def test_a_vr_is_named_only_when_it_is_two_capital_ascii_letters(stated, words):
    """The helper itself, for the shapes pydicom 3.0.2 never hands over as
    a VR (it takes `AA` to `ZZ` by the first byte's range, so lower case
    does not arrive through a file): the rule does not lean on that."""
    assert io_handlers._vr_as_stated(stated) == (
        words or "under a VR the standard does not define")


def test_pydicom_takes_z_newline_as_a_vr(tmp_path):
    """The premise of the newline case, so it cannot pass for nothing."""
    path = _header(tmp_path, "StudyInstanceUID", b"Z\n", b"1.2.3 ")
    assert pydicom.dcmread(path).get_item(0x0020000D, keep_deferred=True).VR == "Z\n"


@pytest.mark.parametrize("keyword", KEY_IDS)
def test_un_one_byte_under_pydicoms_limit_is_read_as_text(tmp_path, keyword):
    """Green at e4c21e85. 65,534 bytes of `UN`: pydicom reads it under the
    dictionary's VR, and the key is that text."""
    path = _header(tmp_path, keyword, b"UN", b"1" * 65534, long=True)
    meta, error = (lambda r: (r[0], r[7]))(io_handlers.ingest_worker(path))
    assert error is None
    assert isinstance(meta[KEYS[keyword][2]], str)
    assert meta[KEYS[keyword][2]] == "1" * 65534


@pytest.mark.parametrize("size", [65535, 65536])
@pytest.mark.parametrize("keyword", KEY_IDS)
def test_un_at_or_past_pydicoms_limit_is_refused_like_ob(tmp_path, keyword, size):
    """Red at e4c21e85 (owner ruling on #1046, finding 1 = A): pydicom
    leaves a `UN` this long as `bytes` with the switch on, and the rule
    admitted it."""
    path = _header(tmp_path, keyword, b"UN", b"1" * size, long=True)
    assert pydicom.config.replace_un_with_known_vr
    assert type(pydicom.dcmread(path).get(keyword)) is bytes
    assert _refusal(io_handlers.ingest_worker(path), path) == _sentence(
        KEYS[keyword][1], "UN")


@pytest.mark.parametrize("keyword", KEY_IDS)
def test_a_folder_with_a_long_un_key_keeps_the_file_beside_it(tmp_path, keyword):
    """Red at e4c21e85: for a SOP Instance UID or Patient ID `ingest()`
    raised and stored nothing with both frames in the sidecar; a Study or
    Series Instance UID was linked under the text of a `bytes`."""
    bad = _header(tmp_path, keyword, b"UN", b"1" * 65535, long=True)
    _good(tmp_path)
    db = tmp_path / "s.db"
    with Session(str(db)) as session:
        summary = session.ingest(str(tmp_path / "in"))
        assert summary.ingested == 1
        assert summary.failures == [(bad, _sentence(KEYS[keyword][1], "UN"))]
        assert [i.sop_instance_uid for i in _instances(session)] == [
            f"{study_uid(1023)}.1.1"]
    assert len(_rows(db, "ERROR")) == 1
    assert row_counts(db) == (1, 1, 1, 1)
    assert _sidecar_bytes(db) == _one_frame(tmp_path)


#: An empty item and a sequence delimiter: what an undefined length holds.
_UNDEFINED = b"\xfe\xff\x00\xe0\x00\x00\x00\x00" + b"\xfe\xff\xdd\xe0\x00\x00\x00\x00"


@pytest.mark.parametrize("keyword", KEY_IDS)
def test_un_of_undefined_length_is_refused_as_the_sequence_pydicom_reads(
        tmp_path, keyword):
    """Green at e4c21e85, pinned. pydicom reads a `UN` of undefined length
    as a sequence while it parses the file, before any element is asked
    for: `keep_deferred` or not, the element is already `SQ`, and the
    file's own `UN` is gone. So this one file is named by what pydicom
    read, not by what it states."""
    path = _header(tmp_path, keyword, b"UN", _UNDEFINED, long=True,
                   length=0xFFFFFFFF)
    raw = pydicom.dcmread(path).get_item(KEYS[keyword][0], keep_deferred=True)
    assert raw.VR == "SQ" and not hasattr(raw, "length")
    assert _refusal(io_handlers.ingest_worker(path), path) == _sentence(
        KEYS[keyword][1], "SQ")


@pytest.mark.parametrize("keyword", KEY_IDS)
def test_an_empty_un_key_is_still_read_as_absent(tmp_path, keyword):
    """Green at e4c21e85: `UN` states no VR, so an empty one is the absent
    key it was, on each key's own path."""
    path = _header(tmp_path, keyword, b"UN", b"", long=True)
    meta, error = (lambda r: (r[0], r[7]))(io_handlers.ingest_worker(path))
    if keyword == "SOPInstanceUID":
        assert error == "ValueError: Missing SOPInstanceUID. Likely not a valid DICOM file."
    else:
        assert error is None
        assert isinstance(meta[KEYS[keyword][2]], str) and meta[KEYS[keyword][2]]


# ---------------------------------------------------------------------------
# 5. The doors a file does not come through are as they were
# ---------------------------------------------------------------------------

def test_a_hand_assigned_sop_instance_uid_that_is_not_text_is_still_the_saves(tmp_path):
    """#721's refusal is for a graph a caller built, and stays."""
    _good(tmp_path)
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        [instance] = _instances(session)
        instance.sop_instance_uid = b"1.2.3"
        with pytest.raises(ValueError, match="hold a SOP Instance UID that is not a str"):
            session.save(sync=True)
        instance.sop_instance_uid = f"{study_uid(1023)}.1.1"


# ---------------------------------------------------------------------------
# 6. What a run over such a folder exports
# ---------------------------------------------------------------------------

def _grade(session, folder):
    path = os.path.join(str(folder), "report.md")
    session.generate_report(path)
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    return [g for g in ("PASS", "REVIEW_REQUIRED", "FAIL") if f"**{g}**" in text]


@pytest.mark.parametrize("keyword, vr", [
    (None, None), ("StudyInstanceUID", "OB"), ("SeriesInstanceUID", "IS"),
    ("PatientID", "SQ"), ("SOPInstanceUID", "US")],
    ids=["control", "study-ob", "series-is", "patient-id-sq", "sop-us"])
def test_a_run_over_a_refused_file_exports_the_file_beside_it_and_does_not_pass(
        tmp_path, keyword, vr):
    """On main: `study-ob` exported two files, one carrying `(0020,000D)`
    as the two values `b'1.2.3.4` and `x00'`; `series-is` exported two
    files graded PASS, the UID `7` replaced like any other; the other two
    stored nothing. Now the ordinary file is exported and stamped, and the
    `ERROR` row grades the run REVIEW_REQUIRED. The control grades PASS."""
    _good(tmp_path)
    if keyword:
        _bad(tmp_path, keyword, vr, REFUSED[vr]())
    db = tmp_path / "s.db"
    with Session(str(db)) as session:
        session.ingest(str(tmp_path / "in"))
        session.anonymize(session.audit())
        session.export(str(tmp_path / "out"), use_compression=False,
                       show_progress=False)
        written = [os.path.join(r, f) for r, _d, fs in os.walk(tmp_path / "out")
                   for f in fs if f.endswith(".dcm")]
        assert len(written) == 1
        ds = pydicom.dcmread(written[0])
        assert str(ds[0x0012, 0x0062].value) == "YES"
        for tag in (0x00080018, 0x0020000D, 0x0020000E):
            assert bytes(ds.get_item(tag).value).startswith(b"2.25.")
        assert _grade(session, tmp_path) == ["REVIEW_REQUIRED" if keyword else "PASS"]
    assert len(_rows(db, "ERROR")) == (1 if keyword else 0)


# ---------------------------------------------------------------------------
# 7. The golden-cohort member holds what its docstring says
# ---------------------------------------------------------------------------

MEMBER = {
    "linkage_key_not_text-good.dcm": None,
    "linkage_key_not_text-sop-uid-ob.dcm": ("SOP Instance UID (0008,0018)", "OB"),
    "linkage_key_not_text-patient-id-us.dcm": ("Patient ID (0010,0020)", "US"),
    "linkage_key_not_text-study-uid-ob.dcm": ("Study Instance UID (0020,000D)", "OB"),
    "linkage_key_not_text-series-uid-is.dcm": ("Series Instance UID (0020,000E)", "IS"),
    "linkage_key_not_text-patient-id-sq.dcm": ("Patient ID (0010,0020)", "SQ"),
}


@pytest.mark.parametrize("name", list(MEMBER))
def test_each_file_of_the_cohort_member_is_refused_for_the_key_it_names(name):
    """Each of the five is refused for its own element and not for an
    earlier reason, and each holds exactly one key that is not text."""
    path = str(REPO / "fingerprint" / "cohort" / "linkage_key_not_text" / name)
    raw = pydicom.dcmread(path)
    stated = {keyword: raw.get_item(tag).VR for keyword, (tag, _n, _k) in KEYS.items()}
    result = io_handlers.ingest_worker(path)
    if MEMBER[name] is None:
        assert result[7] is None
        assert set(stated.values()) <= {"UI", "LO"}
        return
    element, vr = MEMBER[name]
    assert _refusal(result, path) == _sentence(element, vr)
    assert sorted(v for v in stated.values() if v not in ("UI", "LO")) == [vr]


def test_the_cohort_member_is_the_six_files_listed():
    folder = REPO / "fingerprint" / "cohort" / "linkage_key_not_text"
    assert sorted(p.name for p in folder.iterdir()) == sorted(MEMBER)
