"""A redacted instance's SOP Instance UID is derived, not drawn (#544).

Until 1.0 `Instance.regenerate_uid()` called `pydicom.uid.generate_uid()`:
a random UID under pydicom's own registered root, different on every run,
so the golden cohort's redacted member carried a `VARIES` mark. Since #544
the parent derives it from the instance's **source** SOP Instance UID, the
redaction configuration's hash and the project secret
(`privacy._redaction_uid_for`), and hands it to the worker on the task:
the worker never holds the secret.
"""
import json
import hashlib
import shutil
import sqlite3

import numpy as np
import pydicom
import pytest
from pydicom.dataset import FileDataset, FileMetaDataset

from isocenter.entities import Instance, SOURCE_SOP_UID_ATTR
from isocenter.privacy import _redaction_uid_for, _replacement_uid_for, _uid_is_minted
from isocenter.services import RedactionService
from isocenter.session import DicomSession

from support.project_secret import FIXED_A, FIXED_B, load_fixed_secret

SOURCE_SOP = "1.2.3.99.30"
SERIAL = "L10-REDACT"
ZONES = [[0, 4, 0, 4]]
CT_CLASS = "1.2.840.10008.5.1.4.1.1.2"


def _hash(zones, serial=SERIAL):
    """The configuration hash redaction keys its attestation on, spelled as
    `prepare_redaction_tasks` spells it (the input #237 froze)."""
    return hashlib.md5(json.dumps({"serial": serial, "rois": sorted(zones)},
                                  sort_keys=True).encode("utf-8")).hexdigest()


#: The pinned redacted UID of `SOURCE_SOP` under `ZONES` and FIXED_A,
#: computed once from the implementation and pasted.
PINNED = "2.25.13829334667436020127039293678563367191"


def _write_ct(path, sop=SOURCE_SOP):
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = CT_CLASS
    meta.MediaStorageSOPInstanceUID = sop
    meta.TransferSyntaxUID = "1.2.840.10008.1.2.1"
    ds = FileDataset(str(path), {}, file_meta=meta, preamble=b"\0" * 128)
    ds.SOPClassUID = CT_CLASS
    ds.SOPInstanceUID = sop
    ds.StudyInstanceUID = "1.2.3.99.31"
    ds.SeriesInstanceUID = "1.2.3.99.32"
    ds.FrameOfReferenceUID = "1.2.3.99.33"
    ds.PatientID = "L10-R"
    ds.PatientName = "Doe^R"
    ds.Modality = "CT"
    ds.StudyDate = "20200101"
    ds.DeviceSerialNumber = SERIAL
    ds.Manufacturer = "L10"
    ds.ManufacturerModelName = "Probe"
    ds.ImagePositionPatient = [0, 0, 0]
    ds.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
    ds.PixelSpacing = [1, 1]
    ds.Rows = ds.Columns = 32
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.PixelData = (np.arange(1024, dtype=np.uint16) % 200 + 50).tobytes()
    ds.save_as(str(path), enforce_file_format=True)


def _config(tmp_path, zones=ZONES, name="c.yaml"):
    path = tmp_path / name
    path.write_text(
        "privacy_profile: basic\nmachines:\n"
        f"- serial_number: {SERIAL}\n"
        f"  redaction_zones: [{', '.join('{roi: ' + str(z) + '}' for z in zones)}]\n",
        encoding="utf-8")
    return str(path)


@pytest.fixture(params=["default", "threads"])
def executor(request, monkeypatch):
    """R1-R3 run under both executors: 3.14t takes threads."""
    if request.param == "threads":
        monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    return request.param


@pytest.fixture
def src(tmp_path):
    folder = tmp_path / "src"
    folder.mkdir()
    _write_ct(folder / "a.dcm")
    return folder


def _exported_sops(out):
    return [pydicom.dcmread(p).SOPInstanceUID for p in sorted(out.rglob("*.dcm"))]


def _run(tmp_path, src, order, name):
    with DicomSession(str(tmp_path / f"{name}.db")) as session:
        load_fixed_secret(session)
        session.ingest(str(src))
        session.load_config(_config(tmp_path))
        if order == "redact-first":
            session.redact(show_progress=False)
            session.audit()
            session.anonymize()
        else:
            session.audit()
            session.anonymize()
            session.redact(show_progress=False)
        inst = session.store.patients[0].studies[0].series[0].instances[0]
        source = inst.attributes.get(SOURCE_SOP_UID_ATTR)
        session.export(str(tmp_path / f"{name}-out"), use_compression=False)
    return _exported_sops(tmp_path / f"{name}-out"), source


def test_the_pinned_literal_is_the_derivation():
    assert _redaction_uid_for(SOURCE_SOP, _hash(ZONES), FIXED_A) == PINNED


def test_redaction_gives_a_pinned_uid_in_both_orders(tmp_path, src, executor):
    """Kills: `generate_uid()` restored; the current UID used in place of
    the source (redaction after anonymize would derive from M(source))."""
    first, source_a = _run(tmp_path, src, "redact-first", "a")
    second, source_b = _run(tmp_path, src, "anonymize-first", "b")
    assert first == second == [PINNED]
    assert _uid_is_minted(PINNED, FIXED_A)
    assert source_a == source_b == SOURCE_SOP


def test_a_redacted_instance_never_shares_a_uid_with_its_unredacted_export(
        tmp_path, src, executor):
    """The same instance exported before and after redaction carries two
    UIDs, and a redaction under other zones (`force=True`) a third.
    Kills: the redacted UID derived under the replacement's label (R = M);
    the configuration hash dropped from its input."""
    with DicomSession(str(tmp_path / "s.db")) as session:
        load_fixed_secret(session)
        session.ingest(str(src))
        session.load_config(_config(tmp_path))
        session.audit()
        session.anonymize()
        session.export(str(tmp_path / "before"), use_compression=False)
        session.redact(show_progress=False)
        session.export(str(tmp_path / "after"), use_compression=False)
        session.load_config(_config(tmp_path, [[0, 6, 0, 6]], "c2.yaml"))
        session.redact(show_progress=False, force=True)
        session.export(str(tmp_path / "again"), use_compression=False)
    before = _exported_sops(tmp_path / "before")
    after = _exported_sops(tmp_path / "after")
    again = _exported_sops(tmp_path / "again")
    assert before == [_replacement_uid_for(SOURCE_SOP, FIXED_A)]
    assert after == [PINNED]
    assert again == [_redaction_uid_for(SOURCE_SOP, _hash([[0, 6, 0, 6]]), FIXED_A)]
    assert len({before[0], after[0], again[0]}) == 3


def test_the_worker_is_handed_the_uid_and_not_the_secret(tmp_path, src, executor):
    """Kills: the secret carried in the task (pickled to every worker
    under processes); a default for `regenerate_uid`'s argument."""
    with DicomSession(str(tmp_path / "s.db")) as session:
        load_fixed_secret(session)
        session.ingest(str(src))
        service = RedactionService(session.store, session.store_backend)
        tasks = service.prepare_redaction_tasks(
            {"serial_number": SERIAL, "redaction_zones": ZONES})
    (task,) = tasks
    assert task["new_sop_uid"] == PINNED
    for key, value in task.items():
        assert value != FIXED_A and value != FIXED_A.hex(), key
    assert not any(isinstance(v, (bytes, bytearray)) for v in vars(service).values()), \
        "the service is pickled with its bound method; it must not hold the secret"
    with pytest.raises(TypeError):
        Instance(SOURCE_SOP, CT_CLASS, 1).regenerate_uid()


def test_redact_on_a_store_without_a_secret_mints_one(tmp_path, src):
    """`redact()` is the third caller that may create the project secret,
    beside `audit()` and `anonymize()`. Kills redaction reading the secret
    only if present (None: a crash, or a skipped derivation)."""
    db = tmp_path / "s.db"
    with DicomSession(str(db)) as session:
        session.ingest(str(src))
        session.load_config(_config(tmp_path))
        session.redact(show_progress=False)
        session.export(str(tmp_path / "out"), use_compression=False)
    with sqlite3.connect(str(db)) as conn:
        (secret_hex, origin), = conn.execute(
            "SELECT secret_hex, origin FROM project_secret").fetchall()
    assert origin == "generated"
    (sop,) = _exported_sops(tmp_path / "out")
    assert sop == _redaction_uid_for(SOURCE_SOP, _hash(ZONES), bytes.fromhex(secret_hex))


def test_a_service_with_no_store_is_given_the_secret_or_refuses(tmp_path, src):
    """`redact_machine_instances` is public, and a `RedactionService` built
    with no store backend has nowhere to read a secret: it takes
    `project_secret=`, and without one it raises rather than draw a random
    UID (owner ruling Q-A on #544). Kills a random fallback."""
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(str(src))
        inst = session.store.patients[0].studies[0].series[0].instances[0]
        service = RedactionService(session.store)
        with pytest.raises(RuntimeError, match="No project secret"):
            service.redact_machine_instances(SERIAL, ZONES)
        assert inst.sop_instance_uid == SOURCE_SOP
        with pytest.raises(RuntimeError, match="No project secret"):
            service.prepare_redaction_tasks(
                {"serial_number": SERIAL, "redaction_zones": ZONES})
        service.redact_machine_instances(SERIAL, ZONES, project_secret=FIXED_A)
        assert inst.sop_instance_uid == PINNED
        assert inst.attributes[SOURCE_SOP_UID_ATTR] == SOURCE_SOP


def test_a_service_reading_the_store_secret_writes_no_diagnostic(tmp_path, src):
    """A service on a store backend reads the secret as `anonymize()` does,
    without `audit()`'s diagnostics: reading a key to derive a UID is not
    a scan, and a `WARNING` naming another project's pseudonym belongs to
    the audit that looks at patients. Kills `diagnose=True` in
    `_redaction_secret`."""
    with DicomSession(str(tmp_path / "a.db")) as session:
        load_fixed_secret(session)
        session.ingest(str(src))
        session.load_config(_config(tmp_path))
        session.anonymize()
        session.export(str(tmp_path / "a-out"), use_compression=False)
    db = tmp_path / "b.db"
    with DicomSession(str(db)) as session:
        load_fixed_secret(session, secret=FIXED_B)
        session.ingest(str(src))
        session.ingest(str(tmp_path / "a-out"))
        service = RedactionService(session.store, session.store_backend)
        (task,) = service.prepare_redaction_tasks(
            {"serial_number": SERIAL, "redaction_zones": ZONES})
        assert task["new_sop_uid"] == _redaction_uid_for(SOURCE_SOP, _hash(ZONES), FIXED_B)
        session.save(sync=True)
    with sqlite3.connect(str(db)) as conn:
        warnings = [d for (d,) in conn.execute(
            "SELECT details FROM audit_log WHERE action_type='WARNING'")]
    assert not [w for w in warnings if "different project secret" in w], warnings


# --------------------------------------------------------------------
# A store that lost its secret after minting UIDs (owner ruling Q-B)
# --------------------------------------------------------------------

def _drop_secret(db):
    with sqlite3.connect(str(db)) as conn:
        conn.execute("DELETE FROM project_secret")


@pytest.mark.parametrize("step", ["audit", "anonymize", "redact"])
def test_a_store_holding_minted_uids_refuses_a_new_secret(tmp_path, src, step):
    """A new secret would replace every UID this store already minted a
    second time -- a second UID scheme for the same instances -- so the
    store refuses as it does for a shifted date. Under `basic` Study Date
    is emptied, so no shifted date is left to say so. Kills the minted-UID
    evidence missing from the refusal."""
    db = tmp_path / "s.db"
    with DicomSession(str(db)) as session:
        load_fixed_secret(session)
        session.ingest(str(src))
        session.load_config(_config(tmp_path))
        session.anonymize()
        session.save(sync=True)
    _drop_secret(db)
    with DicomSession(str(db)) as session:
        session.load_config(_config(tmp_path))
        with pytest.raises(RuntimeError, match=r"replaced UIDs .*\(1 instance\)"):
            getattr(session, step)(**({"show_progress": False} if step == "redact" else {}))
    with sqlite3.connect(str(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM project_secret").fetchone()[0] == 0


@pytest.mark.parametrize("held", [
    "1.2.826.0.1.3680043.8.498.12345678901234567890",
    # A `2.25.` UID from a UUID version 4, the other tools' shape: the
    # prefix alone is not the evidence, the version-8 UUID is.
    "2.25.147690549933208948189828919899148148576",
], ids=["pydicom-root", "uuid4"])
def test_a_0_9_x_redacted_store_is_not_refused(tmp_path, src, held):
    """0.9.x redaction drew a pydicom-root UID and recorded the source
    beside it, and a 0.9.x store that never audited has no secret. That is
    not a UID this library minted, so the store still gets a secret.
    Kills: the evidence read as "any instance carrying a source UID"; the
    minted shape dropped from it, leaving the `2.25.` prefix."""
    db = tmp_path / "s.db"
    with DicomSession(str(db)) as session:
        session.ingest(str(src))
        inst = session.store.patients[0].studies[0].series[0].instances[0]
        inst._take_sop_uid(held, pixels_changed=True)
        session.save(sync=True)
    with DicomSession(str(db)) as session:
        session.audit()
    with sqlite3.connect(str(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM project_secret").fetchone()[0] == 1
