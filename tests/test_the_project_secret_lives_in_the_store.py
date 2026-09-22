"""Each store keeps its own project secret, and it never leaves the store.

The secret that keys every pseudonym and date offset is 32 random bytes
in a one-row `project_secret` table inside the SQLite store, created on
first need by `audit()` or `anonymize()`. The store already records each
original Patient ID beside its pseudonym, and each offset, in its audit
log, so holding the secret there exposes nothing the store did not; a
file beside the database would make "store copied without its secret"
the common failure, and that failure splits a patient across two
offsets.

There is no way to carry it to another store (#716): 0.9.7's
`write_project_secret(path)` and `load_project_secret(path)` were deleted
before the 1.0 freeze, and `test_a_project_secret_stays_in_its_store.py`
holds what the documentation says instead.

**What these tests hold.** A fresh store makes its own secret and keeps
it across reopening; two sessions racing first use converge; nothing an
export, report, manifest, dataframe or log writes contains it; the
secret reaches process workers by value; a store whose secret was
removed after it shifted dates refuses to shift more; and the log file
names no patient and no offset.

**Why this file imports what it does.** The table and the refusal live
in `isocenter.persistence`, the wiring in
`isocenter.session`, and the expected pseudonyms are recomputed through
`isocenter.privacy`; see `test_mutation_probe_targets.py`.
"""
import base64
import os
import re
import sqlite3
import threading
from datetime import date

import numpy as np
import pydicom
import pytest

from isocenter.entities import Instance, Patient, Series, Study
from isocenter.persistence import SqliteStore
from isocenter.privacy import JITTER_SCHEME_KEYED, PhiFinding, PhiRemediation
from isocenter.remediation import RemediationService
from isocenter.session import DicomSession

from support.project_secret import FIXED_A, load_fixed_secret

SC_SOP_CLASS = "1.2.840.10008.5.1.4.1.1.7"


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _stored_secret(db_path):
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT secret_hex FROM project_secret").fetchall()
    return [bytes.fromhex(r[0]) for r in rows]


def _patient(pid, suffix, study_date=date(2023, 1, 1)):
    patient = Patient(pid, "Orig^Name")
    _add_study(patient, suffix, study_date)
    return patient


def _add_study(patient, suffix, study_date):
    study = Study(f"1.2.826.0.1.97.{suffix}", study_date)
    series = Series(f"1.2.826.0.1.97.{suffix}.1", "OT", 1)
    series.instances.append(
        Instance(f"1.2.826.0.1.97.{suffix}.1.0", SC_SOP_CLASS, 1))
    study.series.append(series)
    patient.studies.append(study)
    return study


# ---------------------------------------------------------------------------
# T7
# ---------------------------------------------------------------------------

def test_each_store_makes_its_own_secret_and_keeps_it(tmp_path):
    """T7. Two fresh stores differ; a reopened store has the same one.

    Red on: a constant secret (the two stores agree); regeneration on
    open (the reopened store differs).
    """
    secrets_seen = []
    for name in ("one.db", "two.db"):
        db = str(tmp_path / name)
        with DicomSession(db) as session:
            assert _stored_secret(db) == [], "created before first need"
            session.store.patients.append(_patient("P1", name))
            session.anonymize(session.audit())
            pseudonym = session.store.patients[0].patient_id
            session.save(sync=True)
        [secret] = _stored_secret(db)
        assert len(secret) == 32
        secrets_seen.append((secret, pseudonym))

        with DicomSession(db) as reopened:
            _add_study(reopened.store.patients[0], name + ".b", date(2023, 6, 1))
            reopened.anonymize(reopened.audit())
        assert _stored_secret(db) == [secret], "the secret changed on reopen"

    (first, p_first), (second, p_second) = secrets_seen
    assert first != second
    assert p_first != p_second, "one patient, two projects, one pseudonym"


def test_a_memory_store_makes_a_secret_the_same_way(tmp_path):
    """T7, `:memory:`. Same table, same lazy generation."""
    with DicomSession(":memory:") as session:
        patient = _patient("P1", "mem")
        session.store.patients.append(patient)
        session.anonymize(session.audit())
        assert len(patient.patient_id) == 29
        assert patient.studies[0].study_date != date(2023, 1, 1)
        with session.store_backend._get_connection() as conn:  # pylint: disable=protected-access
            rows = conn.execute("SELECT secret_hex FROM project_secret").fetchall()
    assert [len(bytes.fromhex(r[0])) for r in rows] == [32]


def test_an_audit_whose_config_raises_makes_no_secret(tmp_path):
    """T7, #456. `audit(config_path=)` that raises leaves the store as it
    was, and a fresh store holds no secret afterwards: one generated and
    committed first would be a change made by a call that did nothing.

    Red on: the secret fetched before the config is resolved.
    """
    broken = tmp_path / "broken_rule.yaml"
    broken.write_text("phi_tags:\n  '0018,1030': {action: REMOVE, name: P}\n"
                      "machines:\n  - model_name: X\n", encoding="utf-8")
    db = str(tmp_path / "s.db")
    with DicomSession(db) as session:
        session.store.patients.append(_patient("P1", "cfg"))
        with pytest.raises(ValueError, match="serial_number"):
            session.audit(config_path=str(broken))
        assert _stored_secret(db) == []
        load_fixed_secret(session, tmp_path)
        assert _stored_secret(db) == [FIXED_A]


def test_two_sessions_reaching_first_use_converge(tmp_path):
    """T7, the race. Both take the row, never replace it.

    Deterministic half first: a store that read "no row" and then inserts
    after another store already did gets the winner back. Red on
    `INSERT OR REPLACE`.
    """
    db = str(tmp_path / "race.db")
    first, second = SqliteStore(db), SqliteStore(db)
    try:
        winner = first._project_secret_for_use()
        inserted, held = second._insert_project_secret(b"\x07" * 32, "generated")
        assert not inserted
        assert held == winner
        assert _stored_secret(db) == [winner]
    finally:
        first.stop()
        second.stop()

    db = str(tmp_path / "race2.db")
    stores = [SqliteStore(db) for _ in range(4)]
    barrier = threading.Barrier(len(stores))
    results = [None] * len(stores)

    def use(index):
        barrier.wait()
        results[index] = stores[index]._project_secret_for_use()

    threads = [threading.Thread(target=use, args=(i,)) for i in range(len(stores))]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        assert len(set(results)) == 1 and results[0] is not None, results
        assert _stored_secret(db) == [results[0]]
    finally:
        for store in stores:
            store.stop()


# ---------------------------------------------------------------------------
# T8
# ---------------------------------------------------------------------------

def _ecg_patient():
    from isocenter.entities import DicomItem
    from isocenter.io_handlers import populate_attrs
    from scripts.generate_waveform_test_data import build_ecg_dataset

    ds = build_ecg_dataset(num_samples=50, patient_id="LEAK01")
    n_channels = len(ds.WaveformSequence[0].ChannelDefinitionSequence)
    patient = Patient("LEAK01", "Leak^Test")
    study = Study("1.2.826.0.1.97.leak", date(2026, 1, 1))
    series = Series("1.2.826.0.1.97.leak.1", "ECG", 3)
    instance = Instance("1.2.826.0.1.97.leak.1.0", ds.SOPClassUID, 1)
    instance.set_pixel_data(np.zeros((1, 1), dtype=np.uint8))
    item = DicomItem()
    populate_attrs(ds.WaveformSequence[0], item)
    instance.add_sequence_item("5400,0100", item)
    instance.waveform_array = np.frombuffer(
        ds.WaveformSequence[0].WaveformData, dtype="<i2"
    ).reshape(50, n_channels).copy()
    series.instances.append(instance)
    study.series.append(series)
    patient.studies.append(study)
    return patient


def test_the_secret_never_leaves_the_store(tmp_path, monkeypatch):
    """T8. No byte of any output carries the secret.

    DICOM and WFDB exports, the report, both manifests, the dataframe
    and the log file are searched for the raw 32 bytes, the hex in both
    cases, and both base64 alphabets. It proves absence in those
    encodings only.

    Red on: the secret logged when it is read; the secret written into a
    private tag.
    """
    from unittest.mock import patch
    from isocenter.validation import IODValidator

    log_file = str(tmp_path / "isocenter.log")
    monkeypatch.setenv("ISOCENTER_LOG_FILE", log_file)
    db = str(tmp_path / "leak.db")
    out = tmp_path / "out"
    with DicomSession(db) as session:
        session.store.patients.append(_ecg_patient())
        session.anonymize(session.audit())
        [secret] = _stored_secret(db)
        with patch("isocenter.io_handlers.run_parallel",
                   side_effect=lambda func, items, *a, **k: [func(i) for i in items]), \
                patch.object(IODValidator, "validate", lambda ds: []):
            session.export(str(out / "dicom"), format="dicom")
            hea = session.export(str(out / "wfdb"), format="wfdb")
        session.generate_report(str(out / "report.md"))
        session.generate_manifest(str(out / "manifest.html"), format="html")
        session.generate_manifest(str(out / "manifest.json"), format="json")
        session.export_dataframe(str(out / "frame.csv"), expand_metadata=True)
    assert hea, "the WFDB export wrote nothing, so it proves nothing"
    assert list((out / "dicom").rglob("*.dcm"))

    needles = {
        "raw": secret,
        "hex": secret.hex().encode(),
        "HEX": secret.hex().upper().encode(),
        "base64": base64.b64encode(secret),
        "base64url": base64.urlsafe_b64encode(secret),
    }
    searched = [log_file] + [str(p) for p in out.rglob("*") if p.is_file()]
    assert len(searched) >= 7, searched
    for path in searched:
        data = open(path, "rb").read()
        for label, needle in needles.items():
            assert needle not in data, f"{label} secret found in {path}"


#: What the sweep below removes from a text before it looks, and why each
#: is legitimate content that a number or date search would otherwise
#: trip on. Every one is a *location* of digits that cannot be an offset
#: or an original date on this path, never a relaxation of the search.
_LOG_TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} - ", re.M)
_PYTEST_TMP = re.compile(r"pytest-\d+")
_PROGRESS = re.compile(r"(it/s|s/it)\]")


def _sweepable(text, tmp_path):
    """`text` with what cannot be a leak removed (see `_LOG_TIMESTAMP`).

    - The log formatter's timestamp prefix: its milliseconds are any
      three digits, today's date is an ISO date, and neither is data.
    - The test's own temporary directory, and pytest's numbered base
      directory (`pytest-1523`) wherever else a path shows it.
    - Progress-bar fragments (`\\r`-separated, carrying `it/s` or `s/it`):
      their rates and counters are timings. The bars are also switched
      off for this test; this is the belt to that brace.
    """
    text = _LOG_TIMESTAMP.sub("", text)
    text = text.replace(str(tmp_path), "<tmp>")
    text = _PYTEST_TMP.sub("pytest-N", text)
    return "\n".join(chunk for chunk in re.split(r"[\r\n]", text)
                     if not _PROGRESS.search(chunk))


def _leaks(text, *, identities, original, offset):
    """Every line of `text` that names an identity, the original date in
    any spelling the pipeline can produce, or the offset.

    **Dates.** DA (`20040119`), ISO (`2004-01-19`, what `str()` of a
    `date` gives, which is how a Study's value renders), the argument
    spelling of a `date` repr (`2004, 1, 19`) and slashes. The shifted
    date is a different day, and today's date is stripped with the
    timestamp, so no legitimate line carries these.

    **The offset.** Its magnitude as a standalone number, signed or not:
    not part of a longer word, hex tag, UID, decimal or path component
    (`[\\w.,/]` before and `[\\w.]` after exclude `0019,a306`, `1.2.306.4`,
    `3060`, `306.12`, `/306/`; an unsigned match also refuses a
    preceding hyphen, so `pytest-306` or `claude-306` in a path is not a
    signed offset, while `(-306 days)` and `by -306` are). This path logs
    counts, and a count equal to it would fail every run the same way,
    because the offset is fixed by `FIXED_A`, never intermittently.

    **Any duration.** A number followed by days, weeks or months: this
    path legitimately logs no duration in those units (the configured
    jitter range is printed only by `load_config()`, which it does not
    drive), so such a number is an offset or an interval in another
    spelling -- `-43.7 weeks` is the offset.
    """
    patterns = [re.escape(value) for value in identities]
    patterns += [re.escape(original.strftime(fmt))
                 for fmt in ("%Y%m%d", "%Y-%m-%d", "%Y/%m/%d")]
    patterns.append(re.escape(
        f"{original.year}, {original.month}, {original.day}"))
    patterns.append(rf"(?<![\w.,/-]){abs(offset)}(?![\w.])")
    patterns.append(rf"(?<![\w.,/])-{abs(offset)}(?![\w.])")
    patterns.append(r"(?<![\w.])\d+(?:\.\d+)?\s*(?:days?|weeks?|months?)\b")
    found = re.compile("|".join(f"(?:{p})" for p in patterns), re.I)
    return [line for line in text.splitlines() if found.search(line)]


@pytest.mark.parametrize("mode", ["threads", "processes"])
def test_the_log_file_names_no_patient_and_no_offset(tmp_path, monkeypatch,
                                                      capfd, mode):
    """T8b. Neither `isocenter.log` at DEBUG nor the console names an
    original Patient ID, an original date, or a patient's date offset.

    The store records all three (its audit log pairs each original ID
    with its pseudonym, and each shift with its days), and that is
    documented: the store is as sensitive as the secret. The log file
    and the console are not guarded that way -- they are what a bug
    report or a CI artifact carries -- and until 0.9.7 the log held the
    same map line by line ("Remediated <PatientID>: patient_id ->
    <pseudonym>", "Date Shifted ... (N days)" at INFO), so a log shipped
    with an export undid the shift. Log lines name UIDs and counts.

    Driven through the identity-lock paths (single, batch, an absent ID)
    and identity recovery, which puts the original ID back into the graph
    and so is where a line naming "the patient" would name it; under
    threads and processes, because a worker's output reaches the console
    by another route. Captured at the file descriptor, so a `print` or a
    child process's stderr is searched as well as the log handler.

    How it avoids flagging legitimate content is `_sweepable`'s and
    `_leaks`'s docstrings: timestamps, temporary paths and progress bars
    are removed before the search, and the number search is bounded so a
    UID, a hex tag, a decimal or a longer number never matches.

    Red on: 0.9.7 as first committed (`fa7e36c`); the remediation log
    line restored to the audit row's text; and each of these, added as a
    new log line (the review's surviving mutants of `30ce915`'s T8b): the
    offset unsigned, the original date beside the shifted one, the offset
    in weeks, and recovery naming the restored Patient ID.
    """
    from datetime import datetime

    if mode == "processes":
        monkeypatch.delenv("ISOCENTER_FORCE_THREADS", raising=False)
        monkeypatch.setenv("ISOCENTER_FORCE_PROCESSES", "1")
    log_file = str(tmp_path / "isocenter.log")
    monkeypatch.setenv("ISOCENTER_LOG_FILE", log_file)
    monkeypatch.setenv("ISOCENTER_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("ISOCENTER_SHOW_PROGRESS", "0")
    monkeypatch.setenv("TQDM_DISABLE", "1")
    source = str(tmp_path / "in")
    original_id = "MRN0012345"
    original_date = date(2004, 1, 19)
    _synthetic_ct(source, original_id, original_date.strftime("%Y%m%d"))
    out = str(tmp_path / "out")
    capfd.readouterr()
    with DicomSession(str(tmp_path / "log.db")) as session:
        session.ingest(source)
        load_fixed_secret(session, tmp_path, FIXED_A)
        session.enable_reversible_anonymization(str(tmp_path / "log.key"))
        session.lock_identities(original_id, persist=True, verbose=True)
        session.lock_identities_batch([original_id, "NOT-IN-STORE"])
        session.lock_identities("NOT-IN-STORE")
        session.anonymize(session.audit())
        session.save(sync=True)
        session.export(out)
        session.generate_report(str(tmp_path / "report.md"))
        [patient] = session.store.patients
        pseudonym = patient.patient_id
        session.recover_patient_identity(pseudonym, restore=True)
        assert patient.patient_id == original_id, "recovery did not run"
    captured = capfd.readouterr()

    [path] = [os.path.join(root, name) for root, _d, files in os.walk(out)
              for name in files if name.endswith(".dcm")]
    shifted = datetime.strptime(str(pydicom.dcmread(path).StudyDate),
                                "%Y%m%d").date()
    offset = (shifted - original_date).days
    assert offset == -306, offset

    log = open(log_file, encoding="utf-8").read()
    console = captured.out + captured.err
    # Not vacuous: each text shows the path it is meant to cover.
    assert "Date Shifted" in log
    assert "Restored identity attributes" in log
    assert "encrypted original identities" in console

    for name, text in (("isocenter.log", log), ("console", console)):
        leaked = _leaks(_sweepable(text, tmp_path),
                        identities=(original_id, "NOT-IN-STORE"),
                        original=original_date, offset=offset)
        assert not leaked, f"{name} carries:\n" + "\n".join(leaked)


def test_a_patient_remediation_that_raised_names_no_patient_in_the_log(
        tmp_path, monkeypatch, capfd):
    """T8b's sweep over the `ERROR` line a raised remediation writes (#553).

    `RemediationService._raised` logs "Failed to apply remediation for
    <subject>", and for a patient finding the finding's `entity_uid` is
    the original Patient ID. The subject is `_log_subject`'s "a patient".
    Forced here on both patient fields, with an exception whose own text
    names nothing, so what is swept is the line this package composes.

    Red on: the subject spelled `{finding.entity_uid}`.
    """
    log_file = str(tmp_path / "isocenter.log")
    monkeypatch.setenv("ISOCENTER_LOG_FILE", log_file)
    monkeypatch.setenv("ISOCENTER_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("ISOCENTER_SHOW_PROGRESS", "0")
    monkeypatch.setenv("TQDM_DISABLE", "1")
    write = RemediationService._write_to_instances

    def refuse(self, entity, field):
        if field in ("patient_id", "patient_name"):
            raise RuntimeError("the store refused the write")
        return write(self, entity, field)

    monkeypatch.setattr(RemediationService, "_write_to_instances", refuse)
    source = str(tmp_path / "in")
    original_id = "MRN0012345"
    original_date = date(2004, 1, 19)
    _synthetic_ct(source, original_id, original_date.strftime("%Y%m%d"))
    capfd.readouterr()
    with DicomSession(str(tmp_path / "raised.db")) as session:
        session.ingest(source)
        load_fixed_secret(session, tmp_path, FIXED_A)
        session.anonymize(session.audit())
        [patient] = session.store.patients
        offset = (patient.studies[0].study_date - original_date).days
        declines = session.store_backend.get_audit_declines()
    captured = capfd.readouterr()

    log = open(log_file, encoding="utf-8").read()
    console = captured.out + captured.err
    # Not vacuous: both raises happened and were logged and recorded.
    assert log.count("Failed to apply remediation for a patient") == 2, log
    assert sum("the store refused the write" in d
               for _t, _u, d in declines) == 2, declines
    assert offset != 0

    for name, text in (("isocenter.log", log), ("console", console)):
        leaked = _leaks(_sweepable(text, tmp_path),
                        identities=(original_id,),
                        original=original_date, offset=offset)
        assert not leaked, f"{name} carries:\n" + "\n".join(leaked)


def test_the_leak_sweep_flags_leaks_and_nothing_legitimate(tmp_path):
    """T8b's matcher, on its own. Its green is only worth something if it
    flags every spelling above and if what it passes over is exactly the
    legitimate content a run produces; both halves are pinned here, on
    lines copied from real runs and from the review's mutants.
    """
    sweep = dict(identities=("MRN0012345",), original=date(2004, 1, 19),
                 offset=-306)
    legitimate = "\n".join([
        f"2026-09-12 19:50:34,306 - INFO - Saving 1 patients to {tmp_path}/log.db",
        "Date Shifted study_date on 1.2.826.0.1.97.4; 1 instance copy; "
        "1 instance-level finding folded into it",
        "Removed 0019,a306 on 1.2.306.4.1",
        "Flushing 200 audit logs...",
        "Saved to /private/var/folders/T/pytest-of-k/pytest-306/x0/out",
        "worker recycling (maxtasksperchild=25); 3060 bytes; 306.12 ms",
        "Anonymize:  100%|##########| 306/306 [00:00<00:00, 306.00it/s]",
        "Restored identity attributes to 1 instances.",
        "exported StudyDate 20030319",
        "Sidecar at /private/tmp/claude-306/scratch/log_pixels.bin",
    ])
    assert _leaks(_sweepable(legitimate, tmp_path), **sweep) == []
    # An offset small enough to be an hour or a minute: the log's own
    # timestamp must not read as one.
    assert _leaks(_sweepable("2026-09-12 19:50:34,306 - INFO - Update complete.",
                             tmp_path),
                  **dict(sweep, offset=-19)) == []

    for leak in ("jitter magnitude 306",
                 "shift (-306 days)",
                 "offset=-306",
                 "study_date: 2004-01-19 -> 2003-03-20",
                 "0008,0023: 20040119 -> 20030319",
                 "original datetime.date(2004, 1, 19)",
                 "shifted by -43.7 weeks",
                 "Restored identity attributes to 1 instances of MRN0012345."):
        assert _leaks(_sweepable(leak, tmp_path), **sweep) == [leak], leak


# ---------------------------------------------------------------------------
# T16
# ---------------------------------------------------------------------------

def test_the_secret_reaches_process_workers(tmp_path, monkeypatch):
    """T16. Under processes, the worker mints with the store's secret.

    Red on: the worker tuple dropping the secret; the worker not
    unpacking it.
    """
    monkeypatch.delenv("ISOCENTER_FORCE_THREADS", raising=False)
    monkeypatch.setenv("ISOCENTER_FORCE_PROCESSES", "1")
    db = str(tmp_path / "workers.db")
    with DicomSession(db) as session:
        load_fixed_secret(session, tmp_path, FIXED_A)
        for index, pid in enumerate(("P1", "1CT1", "MRN-00-77")):
            session.store.patients.append(_patient(pid, f"w{index}"))
        report = session.audit()
    proposed = {f.patient_id: f.remediation_proposal.new_value
                for f in report if f.field_name == "patient_id"}
    assert proposed == {
        "P1": "ANON_d932e13c0c2fa7dafbd43b77",
        "1CT1": "ANON_7daa99a99a8ebcacc72a2ac0",
        "MRN-00-77": "ANON_c2ec2e3dcec4c12d6dbd33d8",
    }, proposed


# ---------------------------------------------------------------------------
# T4
# ---------------------------------------------------------------------------

def _synthetic_ct(directory, patient_id, study_date):
    """CT_small with every UID replaced and Instance Creation Date
    removed: its own UIDs embed the acquisition date, and no shipped
    profile shifts (0008,0012), so either would make the date recoverable
    regardless of the offset -- and a test named for the file not
    revealing its date would export a file that does."""
    ds = pydicom.dcmread(pydicom.data.get_testdata_file("CT_small.dcm"))
    ds.PatientID = patient_id
    ds.StudyDate = study_date
    del ds.InstanceCreationDate
    for keyword, uid in (("StudyInstanceUID", "1.2.826.0.1.97.4"),
                         ("SeriesInstanceUID", "1.2.826.0.1.97.4.1"),
                         ("SOPInstanceUID", "1.2.826.0.1.97.4.1.1"),
                         ("FrameOfReferenceUID", "1.2.826.0.1.97.4.9")):
        setattr(ds, keyword, uid)
    ds.file_meta.MediaStorageSOPInstanceUID = "1.2.826.0.1.97.4.1.1"
    os.makedirs(directory, exist_ok=True)
    ds.save_as(os.path.join(directory, "ct.dcm"))


def test_the_exported_file_does_not_reveal_its_date(tmp_path):
    """T4, end to end, on a real exported file.

    With the jitter range but no secret, the 0.9.6 readback does not
    give back the original StudyDate; with the secret, the offset does.

    Red on: the export path writing the unkeyed pseudonym.
    """
    source = str(tmp_path / "in")
    _synthetic_ct(source, "MRN0012345", "20040119")
    out = str(tmp_path / "out")
    with DicomSession(str(tmp_path / "t4.db")) as session:
        session.ingest(source)
        load_fixed_secret(session, tmp_path, FIXED_A)
        session.anonymize(session.audit())
        session.export(out)

    [path] = [os.path.join(root, name) for root, _d, files in os.walk(out)
              for name in files if name.endswith(".dcm")]
    exported = pydicom.dcmread(path)
    pid, shifted = str(exported.PatientID), str(exported.StudyDate)
    assert pid == "ANON_62ef31e5cf3957e4b1cf54e9", pid
    assert shifted != "20040119"
    carrying = [element.tag for element in exported.iterall()
                if "20040119" in str(element.value)]
    assert not carrying, f"the original date is still in {carrying}"

    from datetime import datetime, timedelta
    day = datetime.strptime(shifted, "%Y%m%d").date()
    for readback in (int(pid[5:13], 16), int(pid[5:21], 16)):
        guess = day - timedelta(days=(readback % 365) - 365)
        assert guess != date(2004, 1, 19), "the offset was read out of the file"

    offset = RemediationService(project_secret=FIXED_A)._get_date_shift(
        pid, JITTER_SCHEME_KEYED)
    assert day - timedelta(days=offset) == date(2004, 1, 19)


# ---------------------------------------------------------------------------
# T12
# ---------------------------------------------------------------------------

def _lose_the_secret(db):
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM project_secret")


def test_a_store_that_lost_its_secret_refuses_to_shift(tmp_path):
    """T12, case D. Dates shifted under a secret the store no longer has:
    `audit()` and `anonymize()` (both spellings) refuse before any work,
    and no row is created.

    Red on: a new secret generated silently.
    """
    db = str(tmp_path / "lost.db")
    with DicomSession(db) as session:
        load_fixed_secret(session, tmp_path, FIXED_A)
        session.store.patients.append(_patient("P1", "lost"))
        session.anonymize(session.audit())
        session.save(sync=True)
    _lose_the_secret(db)

    with DicomSession(db) as reopened:
        new_study = _add_study(reopened.store.patients[0], "lost.b",
                               date(2023, 6, 1))
        with pytest.raises(RuntimeError, match="no longer has"):
            reopened.audit()
        with pytest.raises(RuntimeError, match="no longer has"):
            reopened.anonymize()
        finding = PhiFinding(
            entity_uid=new_study.study_instance_uid, entity_type="Study",
            field_name="study_date", value=new_study.study_date,
            reason="test", tag="0008,0020",
            patient_id=reopened.store.patients[0].patient_id,
            entity=new_study,
            remediation_proposal=PhiRemediation(
                action_type="SHIFT_DATE", target_attr="study_date",
                original_value=new_study.study_date,
                metadata={"patient_id": reopened.store.patients[0].patient_id,
                          "jitter_scheme": JITTER_SCHEME_KEYED}))
        with pytest.raises(RuntimeError, match="no longer has"):
            reopened.anonymize([finding])
        assert new_study.study_date == date(2023, 6, 1)
    assert _stored_secret(db) == []


def test_a_keyed_patient_shifted_under_its_raw_id_also_refuses(tmp_path):
    """T12b. The refusal reads "a keyed patient has a shifted date", not
    "a keyed pseudonym has a shifted date": `anonymize(findings=...)` can
    shift a patient's dates and leave its id as it was, and that patient's
    offset is lost with the secret all the same.

    Red on: the refusal narrowed to patients whose id is a keyed
    pseudonym.
    """
    db = str(tmp_path / "raw.db")
    with DicomSession(db) as session:
        load_fixed_secret(session, tmp_path, FIXED_A)
        session.store.patients.append(_patient("P1", "raw"))
        shifts = [f for f in session.audit()
                  if f.remediation_proposal.action_type == "SHIFT_DATE"]
        assert shifts
        session.anonymize(shifts)
        assert session.store.patients[0].patient_id == "P1"
        session.save(sync=True)
    _lose_the_secret(db)

    with DicomSession(db) as reopened:
        with pytest.raises(RuntimeError, match="no longer has"):
            reopened.audit()
    assert _stored_secret(db) == []
