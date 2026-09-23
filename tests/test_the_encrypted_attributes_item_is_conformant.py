"""The identity token goes in Encrypted Content `(0400,0520)`, and a token
in the layout releases before 1.0 wrote is refused by name (#790).

Every release through 0.9.8 had the two Encrypted Attributes item tags
swapped: the Fernet token went into `(0400,0510)`, which PS3.6 defines as
Encrypted Content Transfer Syntax UID (VR UI, at most 64 characters), and
the transfer syntax UID went into `(0400,0520)`, Encrypted Content (VR OB).
Measured on 3fbf8480: an uncompressed export is Implicit VR Little Endian,
so a reader takes the VRs from the dictionary, reads a 248-character UID
and warns `The value length (248) exceeds the maximum length of 64 allowed
for VR UI`; a compressed export is explicit VR and carries OB on
`(0400,0510)` and UI on `(0400,0520)` on disk. Either way a conformant
reader cannot use the item.

The owner's ruling: 1.x writes and reads the conformant layout only --
compatibility promises begin at 1.0. An item in the earlier layout is
recognised by shape (no key, no decrypt) only so that every read refuses
it by name: recovery raises, the lock refuses rather than replacing it as
foreign (#399, #617's silent loss), the key-creation sniff counts it as
ours, the tolerant read answers None with one ERROR, and the export
passes it through as the graph holds it with a WARNING row.

**The earlier-layout files here are real ones.** `_as_0_9_x` rewrites a
1.0 export's item with pydicom the way 0.9.x's exporter wrote it --
`add_new(0x04000510, 'OB', token)`, `add_new(0x04000520, 'UI', syntax)` --
and nothing else in the file. Measured on 3fbf8480, where the exporter
itself still wrote that layout: the rebuilt file is byte-identical to the
one the exporter wrote, uncompressed (Implicit VR) and compressed
(explicit VR) alike. So the files below are what a 0.9.x user holds, and
what is under test is the real ingest, store, lock, recovery and export.

**Why this file imports what it does.** `isocenter.session` and
`isocenter.reversibility` are named, so both probe rows are charged.
"""
import logging
import sqlite3
import warnings

import pydicom
import pytest
from pydicom.dataset import Dataset
from pydicom.sequence import Sequence

from isocenter.entities import DicomItem
from isocenter.reversibility import ReversibilityService
from isocenter.session import DicomSession

from support.ct_small_files import write_ct

PID, NAME = "PAT-790", "Secret^Seven"
SEQ, SYNTAX, CONTENT = "0400,0500", "0400,0510", "0400,0520"
PAYLOAD_SYNTAX = "1.2.840.10008.1.2"
LAYOUTS = ("uncompressed", "compressed")

#: `recover_patient_identity()`'s refusal (owner-approved wording).
EARLIER = ("this patient's identity token is in the layout Isocenter wrote "
           "before 1.0 (the token in (0400,0510), where DICOM puts the "
           "Encrypted Content Transfer Syntax UID), which 1.x does not read; "
           "recover it with Isocenter 0.9.x and the key it was locked with")

#: The lock's refusal of the same item (owner-approved wording).
LOCK_EARLIER = ("lock_identities: this patient carries an identity token in "
                "the layout Isocenter wrote before 1.0, which 1.x does not "
                "read, and this lock would replace it; recover it with "
                "Isocenter 0.9.x and the key it was locked with. The token "
                "this call would have written is unchanged.")


def export_row(n, m):
    """The export's WARNING row for `n` of `m` earlier-layout instances."""
    return (f"{n} of {m} exported instances carry an identity token in the "
            "layout Isocenter wrote before 1.0, which 1.x cannot recover; "
            "Isocenter 0.9.x recovers it with its key.")


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _only_instance(session):
    [patient] = session.store.patients
    return patient.studies[0].series[0].instances[0]


def _locked_export(tmp_path, compressed, anonymized=True):
    """One CT_small, locked, audited, anonymized and exported from store A.
    Returns (the exported file, key path, Patient ID as exported, token).
    `anonymized=False` exports it locked and nothing more, for a store B
    that will lock it again: a lock over an anonymized patient is refused
    before any token is read (there is no original left to stash)."""
    write_ct(tmp_path / "src" / "a" / "x.dcm", PID, "7901", name=NAME)
    key = str(tmp_path / "k.key")
    with DicomSession(str(tmp_path / "a.db")) as session:
        session.enable_reversible_anonymization(key)
        session.ingest(str(tmp_path / "src"))
        session.lock_identities(PID)
        token = session.reversibility_service.token_of_ours(_only_instance(session))
        assert token is not None, "setup: the lock wrote a token"
        if anonymized:
            session.anonymize(session.audit())
        session.save(sync=True)
        patient_id = session.store.patients[0].patient_id
        session.export(str(tmp_path / "exp"), use_compression=compressed,
                       show_progress=False)
    [path] = list((tmp_path / "exp").rglob("*.dcm"))
    return path, key, patient_id, token


def _as_0_9_x(path, token):
    """Rewrite `path`'s Encrypted Attributes item as every release through
    0.9.8 wrote it (see the module docstring for the byte-identity
    measurement)."""
    ds = pydicom.dcmread(str(path))
    item = Dataset()
    item.add_new(0x04000510, "OB", token)
    item.add_new(0x04000520, "UI", PAYLOAD_SYNTAX)
    ds[0x04000500].value = Sequence([item])
    ds.save_as(str(path), enforce_file_format=True)


def _0_9_x_export(tmp_path, compressed=False, anonymized=True):
    path, key, patient_id, token = _locked_export(tmp_path, compressed, anonymized)
    _as_0_9_x(path, token)
    return path, key, patient_id, token


def _read_strictly(path):
    """`dcmread` the file and touch both item elements with every warning
    an error: pydicom validates a value as it is read."""
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        ds = pydicom.dcmread(str(path))
        item = ds[0x04000500].value[0]
        return item[0x04000510], item[0x04000520]


def _ingested(tmp_path, folder, key, db="b.db"):
    session = DicomSession(str(tmp_path / db))
    session.enable_reversible_anonymization(key)
    session.ingest(str(folder))
    return session


def _audit_rows(db, action):
    with sqlite3.connect(str(db)) as conn:
        return conn.execute("SELECT entity_uid, details FROM audit_log "
                            "WHERE action_type=?", (action,)).fetchall()


# --- what the lock writes ----------------------------------------------------

def test_the_lock_puts_the_token_in_encrypted_content(tmp_path):
    """In the graph: the token in `(0400,0520)`, the UID in `(0400,0510)`.
    Red before #790: the two were the other way round."""
    write_ct(tmp_path / "src" / "a" / "x.dcm", PID, "7902", name=NAME)
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.enable_reversible_anonymization(str(tmp_path / "k.key"))
        session.ingest(str(tmp_path / "src"))
        session.lock_identities(PID)
        [item] = _only_instance(session).sequences[SEQ].items
        assert sorted(item.attributes) == [SYNTAX, CONTENT]
        assert isinstance(item.attributes[CONTENT], bytes)
        assert ReversibilityService.is_one_of_ours(item.attributes[CONTENT])
        assert item.attributes[SYNTAX] == PAYLOAD_SYNTAX


@pytest.mark.parametrize("layout", LAYOUTS)
def test_the_export_writes_the_token_as_ob_and_the_uid_as_ui(tmp_path, layout):
    """On disk, read back with pydicom under warnings-as-errors: `(0400,0510)`
    is a UI holding the transfer syntax, `(0400,0520)` an OB holding the
    token byte for byte. The uncompressed export is Implicit VR, so its
    VRs are the dictionary's -- which is the point: the dictionary is now
    right about what each element holds, and no VR-length warning fires.
    Red before #790: the UserWarning for the 248-character UI
    (uncompressed), and the values swapped (both)."""
    path, _, _, token = _locked_export(tmp_path, layout == "compressed")
    syntax, content = _read_strictly(path)
    assert (syntax.VR, str(syntax.value)) == ("UI", PAYLOAD_SYNTAX)
    assert content.VR == "OB"
    assert bytes(content.value) == token


@pytest.mark.parametrize("layout", LAYOUTS)
def test_a_1_0_export_is_ingested_with_its_token_intact_and_recovered(
        tmp_path, layout):
    """Re-ingested into a fresh store under the key: the token comes back
    byte for byte into `(0400,0520)`, and recovery answers with the
    locked identity."""
    path, key, pseudonym, token = _locked_export(tmp_path, layout == "compressed")
    with _ingested(tmp_path, path.parent, key) as session:
        instance = _only_instance(session)
        [item] = instance.sequences[SEQ].items
        assert bytes(item.attributes[CONTENT]) == token
        assert session.reversibility_service.token_of_ours(instance) == token
        [values] = session.recover_patient_identity(pseudonym).values()
    assert values["0010,0010"] == NAME
    assert values["0010,0020"] == PID


# --- detection ---------------------------------------------------------------

def _item(**attrs):
    item = DicomItem()
    for tag, value in attrs.items():
        item.set_attr(tag, value)
    return item


def test_the_layouts_are_told_apart_by_where_the_token_is(tmp_path):
    """Conformant: the token. Earlier: the sentinel, never the content.
    Foreign (no token of ours in either element, a conformant CMS-shaped
    blob or the #399 fixture's text): None."""
    from isocenter.crypto import KeyManager
    manager = KeyManager(str(tmp_path / "k.key"))
    manager.load_or_generate_key()
    token = ReversibilityService(manager).generate_identity_token(
        {"0010,0010": NAME})
    read = ReversibilityService._token_content
    assert read(_item(**{CONTENT: token, SYNTAX: PAYLOAD_SYNTAX})) == token
    assert read(_item(**{SYNTAX: token, CONTENT: PAYLOAD_SYNTAX})) \
        is ReversibilityService.EARLIER_LAYOUT
    assert read(_item(**{SYNTAX: token.decode(), CONTENT:
                         PAYLOAD_SYNTAX.encode() + b"\x00"})) \
        is ReversibilityService.EARLIER_LAYOUT
    assert read(_item(**{CONTENT: b"0\x82\x01\x00CMS", SYNTAX: PAYLOAD_SYNTAX})) is None
    assert read(_item(**{SYNTAX: b"NOT-OUR-TOKEN", CONTENT: PAYLOAD_SYNTAX})) is None
    assert read(_item()) is None


@pytest.mark.parametrize("uid", (PAYLOAD_SYNTAX, PAYLOAD_SYNTAX.encode() + b"\x00"))
def test_the_transfer_syntax_is_never_taken_for_a_token(uid):
    """Detection is by which element holds a token of ours, so the UID --
    as a string, or as the OB bytes an Implicit VR 0.9.x file reads back
    as -- must never pass the shape test."""
    assert not ReversibilityService.is_one_of_ours(uid)


# --- what a 0.9.x item gets --------------------------------------------------

@pytest.mark.parametrize("layout", LAYOUTS)
def test_recovering_a_0_9_x_export_raises_the_layout_by_name(tmp_path, layout):
    """Re-ingested under the key it was locked with, a 0.9.x export is not
    recovered: `recover_patient_identity()` raises the layout refusal,
    with nothing chained, whether pydicom read the token as a UI string
    (Implicit VR) or as OB bytes (explicit VR). Red on the conformant
    writer without the detection: `no encrypted identity token on this
    patient's instances` -- the wrong cause."""
    path, key, pseudonym, _ = _0_9_x_export(tmp_path, layout == "compressed")
    with _ingested(tmp_path, path.parent, key) as session:
        with pytest.raises(RuntimeError) as raised:
            session.recover_patient_identity(pseudonym)
    assert str(raised.value) == EARLIER
    assert raised.value.__suppress_context__


def test_a_0_9_x_item_is_refused_after_a_reopen(tmp_path):
    """The store path: a 0.9.x item saved and hydrated is refused the same
    way."""
    path, key, pseudonym, _ = _0_9_x_export(tmp_path)
    with _ingested(tmp_path, path.parent, key) as session:
        session.save(sync=True)
    with DicomSession(str(tmp_path / "b.db")) as session:
        session.enable_reversible_anonymization(key)
        with pytest.raises(RuntimeError) as raised:
            session.recover_patient_identity(pseudonym)
    assert str(raised.value) == EARLIER


def test_the_tolerant_read_answers_none_with_one_error_and_never_decrypts(
        tmp_path, caplog, monkeypatch):
    """`recover_original_data()` keeps its contract -- None for anything it
    cannot recover -- and its one ERROR names the cause. It never
    decrypts: the earlier layout is recognised, not read."""
    path, key, _, _ = _0_9_x_export(tmp_path)
    with _ingested(tmp_path, path.parent, key) as session:
        rs = session.reversibility_service
        session.key_manager.load_key()
        decrypted = []
        monkeypatch.setattr(rs.engine, "decrypt",
                            lambda content: decrypted.append(content))
        instance = _only_instance(session)
        caplog.clear()
        with caplog.at_level(logging.ERROR, logger="isocenter"):
            assert rs.recover_original_data(instance) is None
    errors = [r.getMessage() for r in caplog.records
              if r.name == "isocenter" and r.levelno == logging.ERROR]
    assert len(errors) == 1, errors
    assert EARLIER in errors[0]
    assert instance.sop_instance_uid in errors[0]
    assert decrypted == []


@pytest.mark.parametrize("form", ("single", "batch"))
def test_a_lock_over_a_0_9_x_item_refuses_it_and_keeps_it(tmp_path, form):
    """Under the key it was locked with, a lock over a 0.9.x item is
    refused by name, and the item is left as it was. Read as foreign it
    would be replaced (#399) and the identity lost under a lock that
    reported success, #617's failure one layout over. The batch numbers
    it like any refusal."""
    path, key, pid, token = _0_9_x_export(tmp_path, anonymized=False)
    with _ingested(tmp_path, path.parent, key) as session:
        before = dict(_only_instance(session).sequences[SEQ].items[0].attributes)
        with pytest.raises(RuntimeError) as raised:
            if form == "single":
                session.lock_identities(pid)
            else:
                session.lock_identities([pid])
        [item] = _only_instance(session).sequences[SEQ].items
        assert item.attributes == before
    if form == "single":
        assert str(raised.value) == LOCK_EARLIER
    else:
        assert str(raised.value) == (
            "lock_identities: 1 of 1 patients cannot be locked as asked, so no "
            "patient was locked. Each is numbered by its place among the "
            "patients found, in Patient ID order. Lock the others without "
            "these, and each of these as its message says:\n"
            f"[1 of 1] {LOCK_EARLIER}")
    assert raised.value.__suppress_context__ or form == "batch"


def test_with_no_key_file_a_0_9_x_item_counts_as_ours(tmp_path):
    """The key-creation sniff (#617, Q8): a key created here would open a
    0.9.x token no better than a 1.0 one, so the lock refuses with the
    no-key text and creates nothing -- and does not raise the layout
    refusal, which would be about one patient in a sniff over every
    patient in the session."""
    path, _, pid, _ = _0_9_x_export(tmp_path, anonymized=False)
    missing = tmp_path / "missing.key"
    with _ingested(tmp_path, path.parent, str(missing)) as session:
        with pytest.raises(RuntimeError) as raised:
            session.lock_identities(pid)
    assert str(raised.value) == (
        f"lock_identities: there is no key file at {missing}, and this session "
        "holds an identity token this library wrote, which a key created here "
        "could not open and a lock would replace. Enable reversible "
        "anonymization with the key the identities were locked with; no key "
        "was created, and the token this call would have written is unchanged.")
    assert not missing.exists()


@pytest.mark.parametrize("source, again", (
    ("uncompressed", "uncompressed"), ("uncompressed", "compressed"),
    ("compressed", "uncompressed")))
def test_an_export_passes_a_0_9_x_item_through_and_says_so(tmp_path, source,
                                                           again):
    """Owner ruling (a): the export writes the item as the graph holds it
    -- each element's bytes as the graph has them, the token as OB
    bytes even where an Implicit VR read gave the graph a `str` -- and
    writes one WARNING row with the count, which grades the report
    REVIEW_REQUIRED. It is counted in REVERSIBLE_EXPORT too: 0.9.x
    recovers it with the key, so the file is re-identifiable.

    `uncompressed -> compressed` is the arm that puts a VR on disk for
    the token an Implicit VR read handed back as a `str`: OB, not a
    248-character UI. On 3fbf8480 every re-export of an uncompressed
    0.9.x export failed: `TypeError: ... a bytes-like object is
    required, not 'UID'`."""
    path, key, _, _ = _0_9_x_export(tmp_path, source == "compressed")
    with _ingested(tmp_path, path.parent, key) as session:
        attrs = dict(_only_instance(session).sequences[SEQ].items[0].attributes)
        session.export(str(tmp_path / "again"),
                       use_compression=again == "compressed",
                       show_progress=False)
    [written] = list((tmp_path / "again").rglob("*.dcm"))
    item = pydicom.dcmread(str(written))[0x04000500].value[0]
    if again == "compressed":
        assert item.get_item(0x04000510).VR == "OB"

    def padded(value):
        raw = value.encode() if isinstance(value, str) else bytes(value)
        return raw + b"\x00" * (len(raw) % 2)

    for tag, element in ((SYNTAX, 0x04000510), (CONTENT, 0x04000520)):
        assert item.get_item(element).value == padded(attrs[tag]), tag
    assert ReversibilityService.is_one_of_ours(attrs[SYNTAX])
    db = tmp_path / "b.db"
    assert [d for _, d in _audit_rows(db, "WARNING")
            if "layout Isocenter wrote before 1.0" in d] == [export_row(1, 1)]
    assert len(_audit_rows(db, "REVERSIBLE_EXPORT")) == 1


def test_an_export_of_1_0_items_writes_no_layout_row(tmp_path):
    """The row counts earlier-layout items only: a 1.0 export re-exported
    writes REVERSIBLE_EXPORT and no layout WARNING."""
    path, key, _, _ = _locked_export(tmp_path, False)
    with _ingested(tmp_path, path.parent, key) as session:
        session.export(str(tmp_path / "again"), use_compression=False,
                       show_progress=False)
    db = tmp_path / "b.db"
    assert not [d for _, d in _audit_rows(db, "WARNING")
                if "layout Isocenter wrote before 1.0" in d]
    assert len(_audit_rows(db, "REVERSIBLE_EXPORT")) == 1
