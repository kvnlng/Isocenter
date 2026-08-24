# DICOM Waveform → WFDB Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make DICOM waveform IODs a first-class data type in Gantry — ingested, persisted, de-identified, and exported as PhysioNet WFDB records that Murmur Studio reads directly.

**Architecture:** Waveforms become a peer of pixels rather than a special case. The sidecar generalizes from pixel-only to `(instance_uid, kind)` blob storage; a new `gantry/waveform.py` models the DICOM Waveform Module; a new `gantry/exporters/` package introduces an `Exporter` seam that both the existing `DicomExporter` and the new `WfdbExporter` implement, dispatched by `session.export(folder, format=...)`.

**Tech Stack:** Python 3.14, pydicom 3.x, NumPy, SQLite. Test-only: `wfdb` (PhysioNet's own reader, used for conformance validation) and `jsonschema`.

**Spec:** `docs/superpowers/specs/2026-08-23-wfdb-export-design.md`
**Issues:** #9–#16 · [v0.7.0 milestone](https://github.com/kvnlng/Gantry/milestone/1)

## Global Constraints

- **Python floor:** `python_requires=">=3.9"`. Do not use syntax newer than 3.9 in `gantry/` (no `match`, no PEP 604 `X | Y` in annotations evaluated at runtime).
- **No new runtime dependencies.** `wfdb` and `jsonschema` are **test-only** — add them to a `tests` extra in `setup.py`, never to `install_requires`.
- **Never break the pixel path.** Every existing test under `tests/` must pass unchanged after every task. This is the regression gate for the sidecar refactor.
- **WFDB conformance over convenience.** Gantry writes `header(5)`-conformant output (`gain(baseline)/units`, e.g. `200(0)/mV`). Murmur's parser currently reads this swapped — that is tracked as [kvnlng/Murmur#360](https://github.com/kvnlng/Murmur/issues/360) and is **not** worked around here.
- **Never emit `gain 0`** for a calibrated signal. WFDB reads 0 as *uncalibrated* and substitutes 200 adu/mV.
- **No PHI in WFDB output.** No `#` comment lines, ever. Record names derive from anonymized identifiers only.
- **Existing sidecar filename is frozen.** Blobs of all kinds live in the existing `*_pixels.bin`. Renaming it would break every existing session.
- **Format 16 only** in v1. No format 212, no multi-rate records, no WFDB ingest, no `.atr`.
- **Commit after every task.** Use the issue number in the commit footer (e.g. `Refs #10`).

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `gantry/waveform.py` | *(new)* `Waveform` / `WaveformChannel` model, DICOM parsing, sample decoding | 4 |
| `gantry/exporters/__init__.py` | *(new)* `Exporter` protocol, format registry | 6 |
| `gantry/exporters/dicom.py` | *(new)* Thin adapter wrapping the existing `DicomExporter` | 6 |
| `gantry/exporters/wfdb.py` | *(new)* `WfdbExporter` — `.hea` and `.dat` writer | 7 |
| `gantry/murmur.py` | *(new)* DICOM annotations → Murmur `annotations.json` | 10 |
| `gantry/persistence.py` | Blob-kind storage, migration, compaction | 3 |
| `gantry/io_handlers.py` | Waveform extraction at ingest | 5 |
| `gantry/entities.py` | `Instance` waveform accessor triad | 5 |
| `gantry/session.py` | `export(format=...)` dispatch | 6 |
| `scripts/generate_waveform_test_data.py` | *(new)* Synthetic 12-lead ECG DICOM generator | 2 |
| `tests/fixtures/annotations.schema.json` | *(new)* Pinned copy of Murmur's schema | 10 |

---

## Task 1: Restore the development environment and unblock CI

Nothing in this plan is verifiable until the test suite can run. `.venv/bin/python` symlinks to a pyenv `3.14.0t` that no longer exists, and `tests.yml` will not fire on the `setup.py` change this task makes.

Closes #19.

**Files:**
- Modify: `.github/workflows/tests.yml`
- Modify: `.github/workflows/docs.yml`
- Modify: `setup.py`

**Interfaces:**
- Consumes: nothing
- Produces: a working `.venv/bin/python`; a `tests` extra installable via `pip install -e ".[tests]"`

- [ ] **Step 1: Confirm the venv is actually broken**

```bash
ls -l .venv/bin/python; pyenv versions
```

Expected: the symlink points at `3.14.0t`, which is absent from the `pyenv versions` list.

- [ ] **Step 2: Rebuild the venv on an installed interpreter**

The original was free-threaded (`3.14.0t`) and no `t` build remains installed. Use standard 3.14.7 locally; CI still covers `3.14t` in its matrix, so free-threaded coverage is not lost.

```bash
rm -rf .venv
~/.pyenv/versions/3.14.7/bin/python -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install pytest
.venv/bin/python -m pip install -e .
```

- [ ] **Step 3: Record a baseline test result**

```bash
.venv/bin/python -m pytest -q 2>&1 | tail -20
```

Write the pass/fail counts into the commit message. **Do not fix unrelated pre-existing failures in this task** — you need a known baseline to prove later tasks caused no regressions. If anything fails, note it and move on.

- [ ] **Step 4: Add the test-only dependency extra**

In `setup.py`, inside `extras_require`, alongside the existing `docs` and `nlp` entries:

```python
        "tests": [
            "pytest>=7.0.0",
            "wfdb>=4.1.0",
            "jsonschema>=4.0.0"
        ],
```

- [ ] **Step 5: Install the extra and verify the conformance reader imports**

```bash
.venv/bin/python -m pip install -e ".[tests]"
.venv/bin/python -c "import wfdb, jsonschema; print(wfdb.__version__, jsonschema.__version__)"
```

Expected: two version strings, no traceback.

- [ ] **Step 6: Widen the CI path filters**

In `.github/workflows/tests.yml`, replace the `paths:` block under `on: push:` with:

```yaml
on:
  push:
    paths:
      - 'gantry/**'
      - 'tests/**'
      - 'scripts/**'
      - 'setup.py'
      - 'requirements.txt'
      - 'pytest.ini'
      - '.github/workflows/tests.yml'
  workflow_dispatch:
```

In `.github/workflows/docs.yml`, do the same for the docs trigger:

```yaml
on:
  push:
    paths:
      - 'docs/**'
      - 'mkdocs.yml'
      - '.github/workflows/docs.yml'
  workflow_dispatch:
```

- [ ] **Step 7: Install the test extra in CI**

In `tests.yml`, in the `Install Dependencies` step, replace `pip install pytest` with:

```bash
        pip install -e ".[tests]"
```

and delete the now-redundant `pip install -e .` line below it.

- [ ] **Step 8: Commit**

```bash
git add setup.py .github/workflows/tests.yml .github/workflows/docs.yml
git commit -m "build: add tests extra, widen CI path filters

Rebuilt local venv on 3.14.7 (3.14.0t no longer installed).
Baseline suite: <N> passed, <M> failed.

Refs #19"
```

---

## Task 2: Synthetic waveform DICOM fixtures

Every downstream task needs real waveform DICOM to test against, and the repo has none. Building the generator first means tasks 4–10 are testable the moment they are written.

Part of #16.

**Files:**
- Create: `scripts/generate_waveform_test_data.py`
- Create: `tests/test_waveform_fixtures.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `build_ecg_dataset(num_samples=5000, sampling_frequency=500.0, channels=None, baseline_uv=0.0, units="uV") -> pydicom.Dataset`
  - `LEADS: List[Tuple[str, str]]` — 8 `(code_value, code_meaning)` pairs
  - `write_fixture(path: str, **kwargs) -> str`

- [ ] **Step 1: Write the failing test**

Create `tests/test_waveform_fixtures.py`:

```python
import numpy as np
import pytest

from scripts.generate_waveform_test_data import build_ecg_dataset, LEADS


def test_dataset_has_waveform_sequence():
    ds = build_ecg_dataset(num_samples=1000)
    assert ds.Modality == "ECG"
    assert ds.SOPClassUID == "1.2.840.10008.5.1.4.1.1.9.1.1"
    assert len(ds.WaveformSequence) == 1


def test_waveform_item_declares_expected_geometry():
    ds = build_ecg_dataset(num_samples=1000)
    item = ds.WaveformSequence[0]
    assert item.NumberOfWaveformChannels == len(LEADS)
    assert item.NumberOfWaveformSamples == 1000
    assert float(item.SamplingFrequency) == 500.0
    assert item.WaveformBitsAllocated == 16
    assert item.WaveformSampleInterpretation == "SS"


def test_waveform_data_length_matches_geometry():
    ds = build_ecg_dataset(num_samples=1000)
    item = ds.WaveformSequence[0]
    expected_bytes = 1000 * len(LEADS) * 2
    assert len(item.WaveformData) == expected_bytes


def test_channel_definitions_carry_calibration():
    ds = build_ecg_dataset(num_samples=100)
    chdefs = ds.WaveformSequence[0].ChannelDefinitionSequence
    assert len(chdefs) == len(LEADS)
    first = chdefs[0]
    assert float(first.ChannelSensitivity) == pytest.approx(1.0)
    assert float(first.ChannelSensitivityCorrectionFactor) == pytest.approx(1.0)
    assert first.ChannelSensitivityUnitsSequence[0].CodeValue == "uV"
    assert first.ChannelSourceSequence[0].CodeValue == LEADS[0][0]


def test_nonzero_baseline_is_recorded():
    ds = build_ecg_dataset(num_samples=100, baseline_uv=250.0)
    chdef = ds.WaveformSequence[0].ChannelDefinitionSequence[0]
    assert float(chdef.ChannelBaseline) == pytest.approx(250.0)
```

- [ ] **Step 2: Run it to confirm it fails**

```bash
.venv/bin/python -m pytest tests/test_waveform_fixtures.py -q
```

Expected: collection error — `ModuleNotFoundError: No module named 'scripts.generate_waveform_test_data'`.

- [ ] **Step 3: Write the generator**

Create `scripts/generate_waveform_test_data.py`:

```python
"""Generate synthetic 12-lead ECG DICOM files for Gantry's waveform tests.

Signals are deterministic and analytically known, so a round-trip through
WFDB export can be asserted against exact expected physical values.
"""
import os

import numpy as np
import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

# 12-Lead ECG Waveform Storage
ECG_SOP_CLASS_UID = "1.2.840.10008.5.1.4.1.1.9.1.1"

# (CodeValue, CodeMeaning) from the MDC coding scheme.
LEADS = [
    ("MDC_ECG_LEAD_I", "Lead I"),
    ("MDC_ECG_LEAD_II", "Lead II"),
    ("MDC_ECG_LEAD_V1", "Lead V1"),
    ("MDC_ECG_LEAD_V2", "Lead V2"),
    ("MDC_ECG_LEAD_V3", "Lead V3"),
    ("MDC_ECG_LEAD_V4", "Lead V4"),
    ("MDC_ECG_LEAD_V5", "Lead V5"),
    ("MDC_ECG_LEAD_V6", "Lead V6"),
]


def _code(value, meaning, scheme):
    """Build a single-item coded sequence Dataset."""
    item = Dataset()
    item.CodeValue = value
    item.CodingSchemeDesignator = scheme
    item.CodeMeaning = meaning
    return item


def _synthetic_signal(num_samples, num_channels):
    """Deterministic ramp-plus-offset, distinct per channel.

    Channel c holds sample values ``(i % 1000) + c * 1000`` so any
    channel mix-up or transposition during export is immediately visible.
    """
    idx = np.arange(num_samples, dtype=np.int32)
    out = np.empty((num_samples, num_channels), dtype=np.int16)
    for c in range(num_channels):
        out[:, c] = ((idx % 1000) + c * 1000).astype(np.int16)
    return out


def build_ecg_dataset(num_samples=5000,
                      sampling_frequency=500.0,
                      channels=None,
                      baseline_uv=0.0,
                      units="uV",
                      patient_id="WFTEST001",
                      patient_name="Waveform^Test"):
    """Build an in-memory 12-Lead ECG Waveform Storage Dataset.

    Args:
        num_samples (int): Samples per channel.
        sampling_frequency (float): Hz.
        channels (list, optional): (code, meaning) pairs. Defaults to LEADS.
        baseline_uv (float): Channel Baseline, in `units`.
        units (str): UCUM unit code for Channel Sensitivity Units.
        patient_id (str): PatientID value.
        patient_name (str): PatientName value.

    Returns:
        pydicom.Dataset: A complete, writable ECG dataset.
    """
    channels = channels or LEADS
    n_ch = len(channels)

    ds = Dataset()
    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = ECG_SOP_CLASS_UID
    ds.file_meta.MediaStorageSOPInstanceUID = generate_uid()
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.is_little_endian = True
    ds.is_implicit_VR = False

    ds.SOPClassUID = ECG_SOP_CLASS_UID
    ds.SOPInstanceUID = ds.file_meta.MediaStorageSOPInstanceUID
    ds.StudyInstanceUID = generate_uid()
    ds.SeriesInstanceUID = generate_uid()
    ds.Modality = "ECG"
    ds.SeriesNumber = 1
    ds.InstanceNumber = 1

    ds.PatientID = patient_id
    ds.PatientName = patient_name
    ds.PatientBirthDate = "19570314"
    ds.StudyDate = "20260101"
    ds.StudyTime = "101530"
    ds.AcquisitionDateTime = "20260101101530.000000"
    ds.Manufacturer = "GantryTest"
    ds.ManufacturerModelName = "SyntheticCart"
    ds.DeviceSerialNumber = "SN-ECG-001"

    samples = _synthetic_signal(num_samples, n_ch)

    chdefs = []
    for code_value, code_meaning in channels:
        chdef = Dataset()
        chdef.ChannelSensitivity = "1.0"
        chdef.ChannelSensitivityCorrectionFactor = "1.0"
        chdef.ChannelBaseline = str(baseline_uv)
        chdef.ChannelSensitivityUnitsSequence = [_code(units, units, "UCUM")]
        chdef.ChannelSourceSequence = [_code(code_value, code_meaning, "MDC")]
        chdef.ChannelLabel = code_meaning
        chdef.WaveformBitsStored = 16
        chdefs.append(chdef)

    wf = Dataset()
    wf.WaveformOriginality = "ORIGINAL"
    wf.NumberOfWaveformChannels = n_ch
    wf.NumberOfWaveformSamples = num_samples
    wf.SamplingFrequency = str(sampling_frequency)
    wf.ChannelDefinitionSequence = chdefs
    wf.WaveformBitsAllocated = 16
    wf.WaveformSampleInterpretation = "SS"
    wf.WaveformData = samples.tobytes()

    ds.WaveformSequence = [wf]
    return ds


def write_fixture(path, **kwargs):
    """Write a generated ECG dataset to `path` and return the path."""
    ds = build_ecg_dataset(**kwargs)
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    pydicom.dcmwrite(path, ds, write_like_original=False)
    return path


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", help="Output .dcm path")
    parser.add_argument("--samples", type=int, default=5000)
    parser.add_argument("--fs", type=float, default=500.0)
    parser.add_argument("--baseline", type=float, default=0.0)
    args = parser.parse_args()

    written = write_fixture(args.output,
                            num_samples=args.samples,
                            sampling_frequency=args.fs,
                            baseline_uv=args.baseline)
    print(f"Wrote {written}")
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_waveform_fixtures.py -q
```

Expected: 5 passed.

- [ ] **Step 5: Verify a written file round-trips through pydicom**

```bash
.venv/bin/python -c "
from scripts.generate_waveform_test_data import write_fixture
import pydicom
p = write_fixture('/tmp/ecg.dcm', num_samples=1000)
ds = pydicom.dcmread(p)
print(ds.Modality, len(ds.WaveformSequence[0].WaveformData))
"
```

Expected: `ECG 16000` (1000 samples × 8 channels × 2 bytes).

- [ ] **Step 6: Commit**

```bash
git add scripts/generate_waveform_test_data.py tests/test_waveform_fixtures.py
git commit -m "test: add synthetic 12-lead ECG DICOM generator

Refs #16"
```

---

## Task 3: Blob-kind sidecar storage

Generalize sidecar bookkeeping from pixel-only to `(instance_uid, kind)` so waveforms reuse one integrity, compaction, and hashing implementation. `SidecarManager` itself already has no notion of pixels and is not modified.

Closes #10.

**Files:**
- Modify: `gantry/persistence.py` — `SCHEMA` (~line 75), `_init_db` (~line 237), `persist_pixel_data` (~line 747), `compact_sidecar` (~line 1325)
- Test: `tests/test_blob_storage.py`

**Interfaces:**
- Consumes: `SidecarManager.write_frame(data, compression) -> (offset, length)` from `gantry/sidecar.py`
- Produces:
  - `SqliteStore.record_blob_ref(instance_uid, kind, offset, length, hash, compress_alg) -> None` — DB-only write, no sidecar I/O. Used by the ingest path, which writes to the sidecar itself.
  - `SqliteStore.persist_blob(instance, kind, data) -> None` — `data` is `bytes` or `np.ndarray`; `kind` is `'pixels'` or `'waveform'`
  - `SqliteStore.get_blob_ref(instance_uid, kind) -> Optional[dict]` with keys `offset`, `length`, `hash`, `compress_alg`
  - `SqliteStore.persist_pixel_data(instance)` — unchanged signature
  - Table `instance_blobs`

> **Critical:** the ingest path writes pixels via `sidecar_manager.write_frame()` directly and never calls `persist_pixel_data()`. If `compact_sidecar()` reads only `instance_blobs`, every ingested pixel blob looks orphaned and gets discarded. Step 7 guards against this by re-running the back-fill inside compaction, and Step 8's test proves it.

- [ ] **Step 1: Write the failing test**

Create `tests/test_blob_storage.py`:

```python
import numpy as np
import pytest

from gantry.persistence import SqliteStore
from gantry.entities import Instance


@pytest.fixture
def store(tmp_path):
    s = SqliteStore(str(tmp_path / "blobs.db"))
    yield s
    s.stop()


def _instance(uid="1.2.3.4"):
    inst = Instance(uid, "1.2.840.10008.5.1.4.1.1.9.1.1", 1)
    inst.set_attr("0028,0010", 4)
    inst.set_attr("0028,0011", 4)
    return inst


def test_persist_and_read_back_waveform_blob(store):
    inst = _instance()
    payload = b"\x01\x02\x03\x04" * 32

    store.persist_blob(inst, "waveform", payload)

    ref = store.get_blob_ref(inst.sop_instance_uid, "waveform")
    assert ref is not None
    assert ref["length"] > 0
    raw = store.sidecar.read_frame(ref["offset"], ref["length"], ref["compress_alg"])
    assert raw == payload


def test_kinds_are_independent(store):
    inst = _instance()
    store.persist_blob(inst, "waveform", b"WAVE" * 16)
    store.persist_blob(inst, "pixels", b"PIXL" * 16)

    wave = store.get_blob_ref(inst.sop_instance_uid, "waveform")
    pixels = store.get_blob_ref(inst.sop_instance_uid, "pixels")
    assert wave["offset"] != pixels["offset"]
    assert store.sidecar.read_frame(wave["offset"], wave["length"], wave["compress_alg"]) == b"WAVE" * 16
    assert store.sidecar.read_frame(pixels["offset"], pixels["length"], pixels["compress_alg"]) == b"PIXL" * 16


def test_repersisting_a_kind_replaces_its_reference(store):
    inst = _instance()
    store.persist_blob(inst, "waveform", b"first" * 10)
    first = store.get_blob_ref(inst.sop_instance_uid, "waveform")

    store.persist_blob(inst, "waveform", b"second" * 10)
    second = store.get_blob_ref(inst.sop_instance_uid, "waveform")

    assert second["offset"] != first["offset"]
    assert store.sidecar.read_frame(second["offset"], second["length"], second["compress_alg"]) == b"second" * 10


def test_missing_kind_returns_none(store):
    assert store.get_blob_ref("nope", "waveform") is None


def test_blob_hash_is_sha256_of_raw_bytes(store):
    import hashlib
    inst = _instance()
    payload = b"integrity" * 8
    store.persist_blob(inst, "waveform", payload)
    ref = store.get_blob_ref(inst.sop_instance_uid, "waveform")
    assert ref["hash"] == hashlib.sha256(payload).hexdigest()


def test_legacy_pixel_columns_backfill_into_blob_table(tmp_path):
    """A 0.6.x-era DB has pixel_* columns and no instance_blobs rows."""
    import sqlite3
    db_path = str(tmp_path / "legacy.db")

    s = SqliteStore(db_path)
    s.stop()

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO instances (sop_instance_uid, sop_class_uid, instance_number,"
            " pixel_offset, pixel_length, pixel_hash, compress_alg)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("legacy.1", "1.2.840.10008.5.1.4.1.1.2", 1, 0, 128, "deadbeef", "zlib"))
        conn.execute("DELETE FROM instance_blobs")
        conn.commit()

    reopened = SqliteStore(db_path)
    try:
        ref = reopened.get_blob_ref("legacy.1", "pixels")
        assert ref is not None
        assert ref["offset"] == 0
        assert ref["length"] == 128
        assert ref["hash"] == "deadbeef"
        assert ref["compress_alg"] == "zlib"
    finally:
        reopened.stop()


def test_compaction_preserves_both_kinds(store):
    """Regression guard: compaction must not treat either kind as orphaned.

    The ingest path writes pixel frames through SidecarManager and records
    only instances.pixel_offset, so a compaction that reads instance_blobs
    alone would discard them.
    """
    import sqlite3

    pixel_payload = b"PIXELS" * 64
    wave_payload = b"WAVEFORM" * 64

    p_off, p_len = store.sidecar.write_frame(pixel_payload, 'zlib')
    w_off, w_len = store.sidecar.write_frame(wave_payload, 'zlib')

    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "INSERT INTO instances (sop_instance_uid, sop_class_uid,"
            " instance_number, pixel_offset, pixel_length, pixel_hash, compress_alg)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("inst.pixels", "1.2.840.10008.5.1.4.1.1.2", 1,
             p_off, p_len, "pixhash", "zlib"))
        conn.execute(
            "INSERT INTO instances (sop_instance_uid, sop_class_uid, instance_number)"
            " VALUES (?, ?, ?)",
            ("inst.wave", "1.2.840.10008.5.1.4.1.1.9.1.1", 1))
        conn.commit()

    store.record_blob_ref("inst.wave", "waveform", w_off, w_len, "wavhash", "zlib")

    store.compact_sidecar()

    pixels = store.get_blob_ref("inst.pixels", "pixels")
    wave = store.get_blob_ref("inst.wave", "waveform")
    assert pixels is not None, "ingested pixel blob was dropped by compaction"
    assert wave is not None, "waveform blob was dropped by compaction"

    assert store.sidecar.read_frame(
        pixels["offset"], pixels["length"], pixels["compress_alg"]) == pixel_payload
    assert store.sidecar.read_frame(
        wave["offset"], wave["length"], wave["compress_alg"]) == wave_payload
```

- [ ] **Step 2: Run it to confirm it fails**

```bash
.venv/bin/python -m pytest tests/test_blob_storage.py -q
```

Expected: FAIL — `AttributeError: 'SqliteStore' object has no attribute 'persist_blob'`.

- [ ] **Step 3: Add the table to the schema**

In `gantry/persistence.py`, inside the `SCHEMA` string, immediately after the `instance_attributes` table definition:

```sql
    CREATE TABLE IF NOT EXISTS instance_blobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        instance_uid TEXT NOT NULL,
        kind TEXT NOT NULL,
        file_id INTEGER DEFAULT 0,
        offset INTEGER,
        length INTEGER,
        hash TEXT,
        compress_alg TEXT,
        UNIQUE(instance_uid, kind)
    );
```

And with the other index definitions at the bottom of `SCHEMA`:

```sql
    CREATE INDEX IF NOT EXISTS idx_blobs_uid_kind ON instance_blobs(instance_uid, kind);
```

- [ ] **Step 4: Back-fill legacy pixel references on open**

In `gantry/persistence.py`, replace the body of `_init_db` with:

```python
    def _init_db(self):
        with self._get_connection() as conn:
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA auto_vacuum = FULL;")
            conn.execute("PRAGMA synchronous = NORMAL;")
            conn.executescript(self.SCHEMA)
            self._backfill_legacy_blobs(conn)

    def _backfill_legacy_blobs(self, conn):
        """Migrate 0.6.x pixel_* columns into instance_blobs.

        Idempotent: INSERT OR IGNORE means rows already migrated, or written
        by the current code path, are left untouched. The legacy columns are
        deliberately left in place so a downgrade still reads correctly.
        """
        conn.execute("""
            INSERT OR IGNORE INTO instance_blobs
                (instance_uid, kind, file_id, offset, length, hash, compress_alg)
            SELECT sop_instance_uid, 'pixels', COALESCE(pixel_file_id, 0),
                   pixel_offset, pixel_length, pixel_hash, compress_alg
            FROM instances
            WHERE pixel_offset IS NOT NULL AND pixel_length IS NOT NULL
        """)
```

- [ ] **Step 5: Add the blob accessors**

In `gantry/persistence.py`, immediately above `persist_pixel_data` (~line 747):

```python
    def persist_blob(self, instance, kind: str, data) -> None:
        """Write a binary blob to the sidecar and record its reference.

        Args:
            instance (Instance): Owning instance.
            kind (str): 'pixels' or 'waveform'.
            data (bytes | np.ndarray): Payload. Arrays are passed to the
                sidecar directly to avoid a full copy.

        Raises:
            ValueError: If `kind` is not a recognised blob kind.
        """
        import hashlib

        if kind not in ("pixels", "waveform"):
            raise ValueError(f"Unknown blob kind: {kind!r}")

        if data is None:
            return

        raw = data.tobytes() if hasattr(data, "tobytes") else data
        digest = hashlib.sha256(raw).hexdigest()

        c_alg = 'zlib'
        offset, length = self.sidecar.write_frame(data, c_alg)

        self.record_blob_ref(
            instance.sop_instance_uid, kind, offset, length, digest, c_alg)

        instance._mod_count += 1

    def record_blob_ref(self, instance_uid: str, kind: str, offset: int,
                        length: int, blob_hash: str, compress_alg: str) -> None:
        """Record a sidecar reference without writing to the sidecar.

        The ingest path writes frames itself via SidecarManager, so it needs
        to register the resulting reference separately. Without this, the
        blob is invisible to `compact_sidecar` and would be reclaimed as
        dead space.
        """
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO instance_blobs
                    (instance_uid, kind, file_id, offset, length, hash, compress_alg)
                VALUES (?, ?, 0, ?, ?, ?, ?)
                ON CONFLICT(instance_uid, kind) DO UPDATE SET
                    offset=excluded.offset,
                    length=excluded.length,
                    hash=excluded.hash,
                    compress_alg=excluded.compress_alg
            """, (instance_uid, kind, offset, length, blob_hash, compress_alg))

    def get_blob_ref(self, instance_uid: str, kind: str):
        """Return the sidecar reference for a blob, or None if absent.

        Returns:
            Optional[dict]: Keys `offset`, `length`, `hash`, `compress_alg`.
        """
        with self._get_connection() as conn:
            row = conn.execute("""
                SELECT offset, length, hash, compress_alg
                FROM instance_blobs
                WHERE instance_uid = ? AND kind = ?
            """, (instance_uid, kind)).fetchone()

        if row is None:
            return None
        return {
            "offset": row["offset"],
            "length": row["length"],
            "hash": row["hash"],
            "compress_alg": row["compress_alg"],
        }
```

- [ ] **Step 6: Record the pixel blob reference from the existing pixel path**

`persist_pixel_data` keeps its current behavior (it must — the pixel loader and `_pixel_hash` wiring depend on it). Add the blob-table write at the end. In `gantry/persistence.py`, in `persist_pixel_data`, immediately before the line `instance._mod_count += 1`:

```python
            # Mirror the reference into the kind-keyed blob table so
            # compaction and waveform storage share one index.
            self.record_blob_ref(
                instance.sop_instance_uid, 'pixels', offset, length, p_hash, c_alg)
```

- [ ] **Step 7: Make compaction walk every kind**

In `gantry/persistence.py`, in `compact_sidecar`, replace the `SELECT` query with one over the blob table so waveform blobs are not dropped as orphans. **Re-run the back-fill first** — the ingest path writes pixel frames through `SidecarManager` directly, so `instances.pixel_offset` can hold references that `instance_blobs` has never seen. Without this line, compaction silently discards every pixel blob ingested during the current session:

```python
                self._backfill_legacy_blobs(conn)

                rows = cur.execute("""
                    SELECT b.id AS id,
                           b.instance_uid AS sop_instance_uid,
                           b.kind AS kind,
                           b.offset AS pixel_offset,
                           b.length AS pixel_length
                    FROM instance_blobs b
                    WHERE b.offset IS NOT NULL
                    ORDER BY b.offset ASC
                """).fetchall()
```

Then find the `UPDATE instances SET pixel_offset=...` statement later in the same method and replace it with an update against the blob table:

```python
                conn.executemany("""
                    UPDATE instance_blobs SET offset = ? WHERE id = ?
                """, updates)
                conn.executemany("""
                    UPDATE instances SET pixel_offset = ?
                    WHERE sop_instance_uid = (
                        SELECT instance_uid FROM instance_blobs
                        WHERE id = ? AND kind = 'pixels'
                    )
                """, updates)
```

Keep `updates` as a list of `(new_offset, row_id)` tuples. The second statement keeps the legacy columns consistent for downgrade safety; it is a no-op for waveform rows.

- [ ] **Step 8: Run the new tests**

```bash
.venv/bin/python -m pytest tests/test_blob_storage.py -q
```

Expected: 7 passed. If `test_compaction_preserves_both_kinds` fails, the back-fill call in Step 7 is missing or is placed after the `SELECT`.

- [ ] **Step 9: Run the full suite as the regression gate**

```bash
.venv/bin/python -m pytest -q 2>&1 | tail -20
```

Expected: identical pass/fail counts to the Task 1 baseline. **If any previously-passing test now fails, stop and fix it before committing** — the pixel path is the thing this task must not break. Pay particular attention to `tests/test_compaction.py`, `tests/test_sidecar.py`, and `tests/test_pixel_integrity.py`.

- [ ] **Step 10: Commit**

```bash
git add gantry/persistence.py tests/test_blob_storage.py
git commit -m "feat: generalize sidecar storage to blob-kind

Adds instance_blobs keyed by (instance_uid, kind) with an idempotent
back-fill from the legacy pixel_* columns. Compaction now walks all
kinds. Pixel path behavior is unchanged.

Refs #10"
```

---

## Task 4: Waveform object model

Model the DICOM Waveform Module and decode samples. Pure parsing — no I/O, no persistence — so it is fast to test exhaustively.

Closes #11 (model half).

**Files:**
- Create: `gantry/waveform.py`
- Test: `tests/test_waveform_model.py`

**Interfaces:**
- Consumes: `build_ecg_dataset()` from Task 2; `DicomItem` from `gantry/entities.py`
- Produces:
  - `WaveformChannel(label, source_code, source_scheme, sensitivity, correction_factor, units, baseline)` — all keyword-constructible
  - `Waveform(sampling_frequency, num_channels, num_samples, bits_allocated, sample_interpretation, channels, samples=None)`
  - `Waveform.from_dicom_item(item) -> Waveform` — `item` is a `DicomItem` holding a Waveform Sequence item's attributes
  - `decode_samples(data, interpretation, num_samples, num_channels) -> np.ndarray` — returns `int16`, shape `(num_samples, num_channels)`
  - `UnsupportedInterpretation(ValueError)`
  - `WaveformChannel.gain(self) -> float` — ADC units per physical unit
  - `WaveformChannel.wfdb_baseline(self) -> int` — ADC value for zero physical units

- [ ] **Step 1: Write the failing test**

Create `tests/test_waveform_model.py`:

```python
import numpy as np
import pytest

from gantry.waveform import (
    Waveform,
    WaveformChannel,
    decode_samples,
    UnsupportedInterpretation,
)


def test_decode_signed_16_bit_roundtrips():
    original = np.array([[1, 2], [3, 4], [-5, -6]], dtype=np.int16)
    decoded = decode_samples(original.tobytes(), "SS", 3, 2)
    assert decoded.dtype == np.int16
    assert decoded.shape == (3, 2)
    np.testing.assert_array_equal(decoded, original)


def test_decode_unsigned_16_bit_shifts_to_signed_range():
    raw = np.array([[0, 65535]], dtype=np.uint16)
    decoded = decode_samples(raw.tobytes(), "US", 1, 2)
    assert decoded.dtype == np.int16
    assert decoded[0, 0] == -32768
    assert decoded[0, 1] == 32767


def test_decode_signed_8_bit_widens():
    raw = np.array([[-128, 127]], dtype=np.int8)
    decoded = decode_samples(raw.tobytes(), "SB", 1, 2)
    assert decoded.dtype == np.int16
    np.testing.assert_array_equal(decoded, np.array([[-128, 127]], dtype=np.int16))


def test_companded_audio_is_rejected():
    with pytest.raises(UnsupportedInterpretation):
        decode_samples(b"\x00\x01", "MB", 1, 2)
    with pytest.raises(UnsupportedInterpretation):
        decode_samples(b"\x00\x01", "AB", 1, 2)


def test_decode_rejects_wrong_length_payload():
    with pytest.raises(ValueError):
        decode_samples(b"\x00\x01\x02", "SS", 4, 2)


def test_gain_is_reciprocal_of_effective_sensitivity():
    ch = WaveformChannel(label="II", source_code="MDC_ECG_LEAD_II",
                         source_scheme="MDC", sensitivity=0.005,
                         correction_factor=1.0, units="mV", baseline=0.0)
    assert ch.gain() == pytest.approx(200.0)


def test_correction_factor_participates_in_gain():
    ch = WaveformChannel(label="II", source_code="MDC_ECG_LEAD_II",
                         source_scheme="MDC", sensitivity=0.01,
                         correction_factor=0.5, units="mV", baseline=0.0)
    assert ch.gain() == pytest.approx(200.0)


def test_zero_baseline_maps_to_zero_adc():
    ch = WaveformChannel(label="II", source_code="MDC_ECG_LEAD_II",
                         source_scheme="MDC", sensitivity=0.005,
                         correction_factor=1.0, units="mV", baseline=0.0)
    assert ch.wfdb_baseline() == 0


def test_nonzero_baseline_maps_to_negated_adc_offset():
    # physical = adc/gain + baseline  =>  physical == 0 at adc = -baseline*gain
    ch = WaveformChannel(label="II", source_code="MDC_ECG_LEAD_II",
                         source_scheme="MDC", sensitivity=0.005,
                         correction_factor=1.0, units="mV", baseline=0.5)
    assert ch.wfdb_baseline() == -100


def test_from_dicom_item_reads_the_generated_fixture():
    from gantry.io_handlers import populate_attrs
    from gantry.entities import DicomItem
    from scripts.generate_waveform_test_data import build_ecg_dataset, LEADS

    ds = build_ecg_dataset(num_samples=200, baseline_uv=0.0)
    item = DicomItem()
    populate_attrs(ds.WaveformSequence[0], item)

    wf = Waveform.from_dicom_item(item)
    assert wf.num_channels == len(LEADS)
    assert wf.num_samples == 200
    assert wf.sampling_frequency == pytest.approx(500.0)
    assert wf.bits_allocated == 16
    assert wf.sample_interpretation == "SS"
    assert len(wf.channels) == len(LEADS)
    assert wf.channels[0].source_code == "MDC_ECG_LEAD_I"
    assert wf.channels[0].units == "uV"
```

- [ ] **Step 2: Run it to confirm it fails**

```bash
.venv/bin/python -m pytest tests/test_waveform_model.py -q
```

Expected: collection error — `ModuleNotFoundError: No module named 'gantry.waveform'`.

- [ ] **Step 3: Write the model**

Create `gantry/waveform.py`:

```python
"""DICOM Waveform Module object model.

Models the Waveform Sequence (5400,0100) and its Channel Definition
Sequence (003A,0200), and decodes Waveform Data (5400,1010) into a
NumPy array. Parsing only — no I/O.
"""
from dataclasses import dataclass, field
from typing import Any, List, Optional

import numpy as np

# Waveform Sequence item tags
TAG_NUM_CHANNELS = "003a,0005"
TAG_NUM_SAMPLES = "003a,0010"
TAG_SAMPLING_FREQUENCY = "003a,001a"
TAG_CHANNEL_DEFINITION_SEQ = "003a,0200"
TAG_BITS_ALLOCATED = "5400,1004"
TAG_SAMPLE_INTERPRETATION = "5400,1006"

# Channel Definition item tags
TAG_CHANNEL_SOURCE_SEQ = "003a,0208"
TAG_CHANNEL_SENSITIVITY = "003a,0210"
TAG_CHANNEL_SENSITIVITY_UNITS_SEQ = "003a,0211"
TAG_CHANNEL_SENSITIVITY_CORRECTION = "003a,0212"
TAG_CHANNEL_BASELINE = "003a,0213"
TAG_CHANNEL_LABEL = "003a,0203"

# Coded entry tags
TAG_CODE_VALUE = "0008,0100"
TAG_CODING_SCHEME = "0008,0102"
TAG_CODE_MEANING = "0008,0104"

_DTYPES = {
    "SS": "<i2",
    "US": "<u2",
    "SB": "i1",
    "UB": "u1",
}

_COMPANDED = {"MB", "AB"}


class UnsupportedInterpretation(ValueError):
    """Raised for Waveform Sample Interpretations Gantry cannot decode."""


def _as_float(value, default=0.0):
    """Coerce a DICOM DS/attribute value to float, tolerating None and lists."""
    if value is None:
        return default
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
        if value is None:
            return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value, default=0):
    return int(_as_float(value, default))


def decode_samples(data: bytes,
                   interpretation: str,
                   num_samples: int,
                   num_channels: int) -> np.ndarray:
    """Decode raw Waveform Data into an int16 array.

    Args:
        data (bytes): Raw Waveform Data (5400,1010).
        interpretation (str): Waveform Sample Interpretation (5400,1006).
        num_samples (int): Samples per channel.
        num_channels (int): Channel count.

    Returns:
        np.ndarray: int16, shape (num_samples, num_channels).

    Raises:
        UnsupportedInterpretation: For mu-law/A-law companded audio.
        ValueError: If the payload length does not match the geometry.
    """
    interp = (interpretation or "SS").strip().upper()

    if interp in _COMPANDED:
        raise UnsupportedInterpretation(
            f"Sample Interpretation {interp!r} is companded audio "
            "(mu-law/A-law), which Gantry does not decode.")

    dtype = _DTYPES.get(interp)
    if dtype is None:
        raise UnsupportedInterpretation(
            f"Unknown Waveform Sample Interpretation: {interp!r}")

    expected = num_samples * num_channels
    arr = np.frombuffer(data, dtype=dtype)
    if arr.size != expected:
        raise ValueError(
            f"Waveform payload has {arr.size} samples, expected {expected} "
            f"({num_samples} samples x {num_channels} channels).")

    arr = arr.reshape(num_samples, num_channels)

    if interp == "US":
        # Rebase unsigned onto the signed 16-bit range so one .dat format
        # (WFDB 16) serves every input interpretation.
        return (arr.astype(np.int32) - 32768).astype(np.int16)

    return arr.astype(np.int16, copy=False)


@dataclass
class WaveformChannel:
    """One channel's identity and calibration."""

    label: str = ""
    source_code: str = ""
    source_scheme: str = ""
    sensitivity: float = 1.0
    correction_factor: float = 1.0
    units: str = "mV"
    baseline: float = 0.0

    def gain(self) -> float:
        """ADC units per physical unit, as WFDB defines gain.

        DICOM stores sensitivity as physical units per ADC unit, so this is
        its reciprocal. Returns 1.0 rather than raising when sensitivity is
        absent or zero — a zero gain would be read by WFDB as *uncalibrated*.
        """
        effective = self.sensitivity * self.correction_factor
        if not effective:
            return 1.0
        return 1.0 / effective

    def wfdb_baseline(self) -> int:
        """ADC value corresponding to zero physical units.

        DICOM: physical = adc / gain + baseline
        Setting physical = 0 gives adc = -baseline * gain.
        """
        return int(round(-self.baseline * self.gain()))

    @classmethod
    def from_dicom_item(cls, item: Any) -> "WaveformChannel":
        """Build from a Channel Definition Sequence item."""
        attrs = item.attributes
        seqs = item.sequences

        source_code = ""
        source_scheme = ""
        src = seqs.get(TAG_CHANNEL_SOURCE_SEQ)
        if src is not None and src.items:
            source_code = str(src.items[0].attributes.get(TAG_CODE_VALUE, "") or "")
            source_scheme = str(src.items[0].attributes.get(TAG_CODING_SCHEME, "") or "")

        units = "mV"
        unit_seq = seqs.get(TAG_CHANNEL_SENSITIVITY_UNITS_SEQ)
        if unit_seq is not None and unit_seq.items:
            units = str(unit_seq.items[0].attributes.get(TAG_CODE_VALUE, "mV") or "mV")

        return cls(
            label=str(attrs.get(TAG_CHANNEL_LABEL, "") or ""),
            source_code=source_code,
            source_scheme=source_scheme,
            sensitivity=_as_float(attrs.get(TAG_CHANNEL_SENSITIVITY), 1.0),
            correction_factor=_as_float(attrs.get(TAG_CHANNEL_SENSITIVITY_CORRECTION), 1.0),
            units=units,
            baseline=_as_float(attrs.get(TAG_CHANNEL_BASELINE), 0.0),
        )

    def wfdb_description(self) -> str:
        """Signal description for the .hea signal line.

        Prefers the coded channel source over the free-text label: a coded
        value cannot contain an operator-typed patient name.
        """
        return self.source_code or self.label or "signal"


@dataclass
class Waveform:
    """One Waveform Sequence item: geometry, calibration, and samples."""

    sampling_frequency: float = 0.0
    num_channels: int = 0
    num_samples: int = 0
    bits_allocated: int = 16
    sample_interpretation: str = "SS"
    channels: List[WaveformChannel] = field(default_factory=list)
    samples: Optional[np.ndarray] = None

    @classmethod
    def from_dicom_item(cls, item: Any) -> "Waveform":
        """Build from a Waveform Sequence item (metadata only, no samples)."""
        attrs = item.attributes
        seqs = item.sequences

        channels = []
        chdefs = seqs.get(TAG_CHANNEL_DEFINITION_SEQ)
        if chdefs is not None:
            channels = [WaveformChannel.from_dicom_item(i) for i in chdefs.items]

        return cls(
            sampling_frequency=_as_float(attrs.get(TAG_SAMPLING_FREQUENCY)),
            num_channels=_as_int(attrs.get(TAG_NUM_CHANNELS)),
            num_samples=_as_int(attrs.get(TAG_NUM_SAMPLES)),
            bits_allocated=_as_int(attrs.get(TAG_BITS_ALLOCATED), 16) or 16,
            sample_interpretation=str(attrs.get(TAG_SAMPLE_INTERPRETATION, "SS") or "SS"),
            channels=channels,
        )

    def decode(self, data: bytes) -> np.ndarray:
        """Decode raw Waveform Data using this item's geometry."""
        self.samples = decode_samples(
            data, self.sample_interpretation, self.num_samples, self.num_channels)
        return self.samples
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_waveform_model.py -q
```

Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add gantry/waveform.py tests/test_waveform_model.py
git commit -m "feat: add DICOM waveform object model

Refs #11"
```

---

## Task 5: Waveform ingest and Instance accessors

Stop discarding waveform samples at ingest, offload them to the sidecar, and give `Instance` a waveform accessor triad mirroring the pixel one.

Closes #9, closes #11 (accessor half).

**Files:**
- Modify: `gantry/io_handlers.py:102-180` (`ingest_worker`), `gantry/io_handlers.py:229-270` (`DicomImporter.import_files` signature and result unpack)
- Modify: `gantry/entities.py:131` (`Instance`)
- Modify: `gantry/session.py:344-349` (`ingest` passes the store backend through)
- Test: `tests/test_waveform_ingest.py`

**Interfaces:**
- Consumes: `Waveform`, `decode_samples` from Task 4; `SqliteStore.record_blob_ref` from Task 3
- Produces:
  - `ingest_worker(fp)` now returns an 8-tuple: `(meta, inst, p_bytes, p_hash, p_alg, w_bytes, w_hash, err)`
  - `Instance.waveform_array: Optional[np.ndarray]`
  - `Instance.get_waveform_data() -> Optional[np.ndarray]`
  - `Instance.unload_waveform_data() -> bool`
  - `Instance._waveform_loader`, `Instance._waveform_hash`

- [ ] **Step 1: Write the failing test**

Create `tests/test_waveform_ingest.py`:

```python
import numpy as np
import pytest

from gantry.io_handlers import ingest_worker
from scripts.generate_waveform_test_data import write_fixture, LEADS


@pytest.fixture
def ecg_file(tmp_path):
    return write_fixture(str(tmp_path / "ecg.dcm"), num_samples=500)


def test_ingest_worker_returns_waveform_bytes(ecg_file):
    meta, inst, p_bytes, p_hash, p_alg, w_bytes, w_hash, err = ingest_worker(ecg_file)
    assert err is None
    assert meta["modality"] == "ECG"
    assert w_bytes is not None
    assert len(w_bytes) == 500 * len(LEADS) * 2


def test_waveform_hash_is_sha256_of_raw_bytes(ecg_file):
    import hashlib
    _, _, _, _, _, w_bytes, w_hash, _ = ingest_worker(ecg_file)
    assert w_hash == hashlib.sha256(w_bytes).hexdigest()


def test_waveform_metadata_survives_ingest(ecg_file):
    _, inst, _, _, _, _, _, _ = ingest_worker(ecg_file)
    from gantry.waveform import Waveform
    seq = inst.sequences.get("5400,0100")
    assert seq is not None and seq.items
    wf = Waveform.from_dicom_item(seq.items[0])
    assert wf.num_channels == len(LEADS)
    assert wf.num_samples == 500
    assert wf.channels[0].source_code == "MDC_ECG_LEAD_I"


def test_waveform_data_element_is_not_kept_in_attributes(ecg_file):
    """Bulk samples belong in the sidecar, not the JSON core attributes."""
    _, inst, _, _, _, _, _, _ = ingest_worker(ecg_file)
    seq = inst.sequences["5400,0100"]
    assert "5400,1010" not in seq.items[0].attributes


def test_pixel_only_file_yields_no_waveform(tmp_path, dummy_patient):
    """A CT instance must still ingest with waveform fields empty."""
    import pydicom
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian, generate_uid

    ds = Dataset()
    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.2"
    ds.file_meta.MediaStorageSOPInstanceUID = generate_uid()
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.2"
    ds.SOPInstanceUID = ds.file_meta.MediaStorageSOPInstanceUID
    ds.StudyInstanceUID = generate_uid()
    ds.SeriesInstanceUID = generate_uid()
    ds.Modality = "CT"
    ds.PatientID = "CT1"
    ds.Rows = 4
    ds.Columns = 4
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelData = np.zeros((4, 4), dtype=np.uint16).tobytes()

    path = str(tmp_path / "ct.dcm")
    pydicom.dcmwrite(path, ds, write_like_original=False)

    _, _, p_bytes, _, _, w_bytes, w_hash, err = ingest_worker(path)
    assert err is None
    assert p_bytes is not None
    assert w_bytes is None
    assert w_hash is None


def test_instance_waveform_accessors_roundtrip():
    from gantry.entities import Instance
    inst = Instance("1.2.3", "1.2.840.10008.5.1.4.1.1.9.1.1", 1)
    arr = np.arange(12, dtype=np.int16).reshape(6, 2)

    inst.waveform_array = arr
    assert inst.get_waveform_data() is arr

    # No loader and no file path: unloading would lose data.
    assert inst.unload_waveform_data() is False
    assert inst.waveform_array is arr

    inst._waveform_loader = lambda: arr
    assert inst.unload_waveform_data() is True
    assert inst.waveform_array is None
    np.testing.assert_array_equal(inst.get_waveform_data(), arr)


def test_ingest_registers_the_waveform_blob_reference(tmp_path):
    """Without a blob-table row, compaction reclaims the waveform."""
    from gantry.session import DicomSession

    src = tmp_path / "src"
    src.mkdir()
    write_fixture(str(src / "ecg.dcm"), num_samples=300)

    session = DicomSession(persistence_file=str(tmp_path / "ref.db"))
    try:
        session.ingest(str(src))
        inst = session.store.patients[0].studies[0].series[0].instances[0]
        ref = session.store_backend.get_blob_ref(inst.sop_instance_uid, "waveform")
        assert ref is not None
        assert ref["length"] > 0
    finally:
        session.close()


def test_waveform_survives_a_session_reload(tmp_path):
    """Gantry's pause/resume promise must hold for waveforms, not just pixels."""
    from gantry.session import DicomSession

    src = tmp_path / "src"
    src.mkdir()
    write_fixture(str(src / "ecg.dcm"), num_samples=300)
    db = str(tmp_path / "reload.db")

    session = DicomSession(persistence_file=db)
    try:
        session.ingest(str(src))
        original = session.store.patients[0].studies[0].series[0].instances[0]
        expected = original.get_waveform_data().copy()
    finally:
        session.close()

    reopened = DicomSession(persistence_file=db)
    try:
        inst = reopened.store.patients[0].studies[0].series[0].instances[0]
        assert inst.waveform_array is None, "should be lazy, not eagerly loaded"
        np.testing.assert_array_equal(inst.get_waveform_data(), expected)
    finally:
        reopened.close()
```

- [ ] **Step 2: Run it to confirm it fails**

```bash
.venv/bin/python -m pytest tests/test_waveform_ingest.py -q
```

Expected: FAIL — `ValueError: not enough values to unpack (expected 8, got 6)`.

- [ ] **Step 3: Add the waveform fields and accessors to `Instance`**

In `gantry/entities.py`, in the `Instance` class, immediately after the `_pixel_hash` field declaration:

```python
    # Transient: Decoded waveform samples, shape (num_samples, num_channels)
    waveform_array: Optional[np.ndarray] = field(default=None, repr=False)

    # Transient: Lazy loader for waveform samples (sidecar-backed)
    _waveform_loader: Optional[Callable[[], np.ndarray]] = field(default=None, repr=False)

    # Transient: Integrity hash for the raw waveform bytes
    _waveform_hash: Optional[str] = field(default=None, repr=False)
```

Then, immediately after the `get_pixel_data` method, add:

```python
    def unload_waveform_data(self) -> bool:
        """Clear cached waveform samples to free memory.

        Mirrors `unload_pixel_data`: only unloads when the data can be
        recovered, so `release_memory()` can never silently discard
        unexported waveforms.

        Returns:
            bool: True if unloaded (or already absent), False if unsafe.
        """
        if self.waveform_array is None:
            return True

        if self.file_path or self._waveform_loader:
            self.waveform_array = None
            return True
        return False

    def get_waveform_data(self) -> Optional[np.ndarray]:
        """Return decoded waveform samples, loading from the sidecar if needed.

        Returns:
            Optional[np.ndarray]: int16 array of shape
            (num_samples, num_channels), or None if this instance has no
            waveform.
        """
        if self.waveform_array is not None:
            return self.waveform_array

        if self._waveform_loader is not None:
            self.waveform_array = self._waveform_loader()
            return self.waveform_array

        return None
```

- [ ] **Step 4: Extract waveform data in `ingest_worker`**

In `gantry/io_handlers.py`, in `ingest_worker`, immediately before the `return (meta, inst, p_bytes, p_hash, p_alg, None)` line:

```python
        # Extract Waveform Data
        # populate_attrs skips all OB/OW VRs, so (5400,1010) never reaches the
        # object graph on its own. Pull it out explicitly, exactly as PixelData
        # is handled above, and offload the bytes to the sidecar.
        w_bytes = None
        w_hash = None

        if "WaveformSequence" in ds and len(ds.WaveformSequence) > 0:
            wf_item = ds.WaveformSequence[0]
            raw = getattr(wf_item, "WaveformData", None)
            if raw:
                w_bytes = bytes(raw)
                w_hash = hashlib.sha256(w_bytes).hexdigest()
```

Then change the return statement to:

```python
        return (meta, inst, p_bytes, p_hash, p_alg, w_bytes, w_hash, None)
```

And change the exception handler's return to match:

```python
    except Exception as e:
        return (None, None, None, None, None, None, None, str(e))
```

Finally, update the type annotation on the `ingest_worker` signature (line 102) to a plain `Tuple` — the existing 6-element annotation is now wrong:

```python
def ingest_worker(fp: str) -> Tuple:
```

- [ ] **Step 5: Update the result consumer and register the blob reference**

In `gantry/io_handlers.py`, add a `store_backend` parameter to `DicomImporter.import_files`:

```python
    @staticmethod
    def import_files(file_paths: List[str], store: DicomStore, executor=None,
                     sidecar_manager=None, store_backend=None):
```

Document it in that method's docstring, under the existing `sidecar_manager` line:

```
            store_backend (optional): SqliteStore used to register sidecar
                blob references. Waveform blobs are invisible to compaction
                unless recorded here.
```

Change the unpack (~line 254):

```python
        for meta, inst, p_bytes, p_hash, p_alg, w_bytes, w_hash, err in results:
```

Then, immediately after the existing pixel sidecar block (the one that sets `inst._pixel_hash = p_hash`), add:

```python
                    if w_bytes and sidecar_manager:
                        w_off, w_len = sidecar_manager.write_frame(w_bytes, 'zlib')
                        inst._waveform_hash = w_hash
                        inst._waveform_loader = SidecarWaveformLoader(
                            sidecar_manager.filepath, w_off, w_len, 'zlib',
                            instance=inst, waveform_hash=w_hash)

                        # Unlike pixels, waveform offsets have no column on
                        # `instances`, so the blob table is their only record.
                        # Skipping this makes compaction reclaim them.
                        if store_backend is not None:
                            store_backend.record_blob_ref(
                                inst.sop_instance_uid, 'waveform',
                                w_off, w_len, w_hash, 'zlib')
```

Finally, in `gantry/session.py`, in `ingest` (~line 344), pass the backend through:

```python
        DicomImporter.import_files(
            [directory],
            self.store,
            executor=self._executor,
            sidecar_manager=self.store_backend.sidecar,
            store_backend=self.store_backend)
```

- [ ] **Step 6: Add the waveform loader**

In `gantry/io_handlers.py`, immediately after the `SidecarPixelLoader` class definition, add:

```python
class SidecarWaveformLoader:
    """Functor for lazy loading of waveform samples from the sidecar.

    Top-level class so it stays picklable across process boundaries.
    Stores primitive geometry rather than an Instance reference, which
    avoids a reference cycle and keeps IPC payloads small.
    """

    def __init__(self, sidecar_path, offset, length, alg,
                 instance=None, metadata=None, waveform_hash=None):
        self.sidecar_path = sidecar_path
        self.offset = offset
        self.length = length
        self.alg = alg

        if metadata:
            self.num_samples = metadata.get("num_samples", 0)
            self.num_channels = metadata.get("num_channels", 0)
            self.interpretation = metadata.get("interpretation", "SS")
            self.waveform_hash = metadata.get("waveform_hash")
        elif instance is not None:
            from .waveform import Waveform
            seq = instance.sequences.get("5400,0100")
            if seq is None or not seq.items:
                raise ValueError(
                    "SidecarWaveformLoader requires a Waveform Sequence on the instance")
            wf = Waveform.from_dicom_item(seq.items[0])
            self.num_samples = wf.num_samples
            self.num_channels = wf.num_channels
            self.interpretation = wf.sample_interpretation
            self.waveform_hash = waveform_hash or getattr(instance, "_waveform_hash", None)
        else:
            raise ValueError(
                "SidecarWaveformLoader requires either 'instance' or 'metadata'")

    def __call__(self):
        from .waveform import decode_samples

        mgr = SidecarManager(self.sidecar_path)
        raw = mgr.read_frame(self.offset, self.length, self.alg)

        if self.waveform_hash:
            actual = hashlib.sha256(raw).hexdigest()
            if actual != self.waveform_hash:
                raise ValueError(
                    f"Waveform integrity check failed: expected "
                    f"{self.waveform_hash}, got {actual}")

        return decode_samples(raw, self.interpretation,
                              self.num_samples, self.num_channels)
```

- [ ] **Step 7: Rehydrate the waveform loader on session load**

Pixels are rewired from the `instances.pixel_offset` columns during hydration; waveforms have no such columns, so without this they vanish on reopen and `test_waveform_survives_a_session_reload` fails.

In `gantry/persistence.py`, in `load_all`, immediately **before** the `for r in i_rows:` loop, fetch every waveform reference in one query rather than querying per instance:

```python
                wave_refs = {
                    row['instance_uid']: row
                    for row in cur.execute(
                        "SELECT instance_uid, offset, length, compress_alg, hash"
                        " FROM instance_blobs WHERE kind = 'waveform'").fetchall()
                }
```

Then inside that loop, immediately after the existing `inst._pixel_loader = ...` block, add:

```python
                    wref = wave_refs.get(r['sop_instance_uid'])
                    if wref is not None:
                        from .io_handlers import SidecarWaveformLoader
                        inst._waveform_hash = wref['hash']
                        try:
                            inst._waveform_loader = SidecarWaveformLoader(
                                self.sidecar_path, wref['offset'], wref['length'],
                                wref['compress_alg'], instance=inst,
                                waveform_hash=wref['hash'])
                        except ValueError:
                            # Geometry lives in the Waveform Sequence, which is
                            # restored from attributes_json above. A missing
                            # sequence means a corrupt row, not a fatal error.
                            self.logger.warning(
                                f"Waveform blob for {r['sop_instance_uid']} has no "
                                "Waveform Sequence; skipping loader.")
```

Apply the same two additions to `load_patient`, which has its own hydration loop with the same shape.

- [ ] **Step 8: Run the new tests**

```bash
.venv/bin/python -m pytest tests/test_waveform_ingest.py -q
```

Expected: 8 passed.

- [ ] **Step 9: Run the full suite**

```bash
.venv/bin/python -m pytest -q 2>&1 | tail -20
```

Expected: matches the Task 1 baseline. `ingest_worker`'s arity changed, so any other caller would surface here — check `gantry/session.py:27` (`scan_worker`) is genuinely independent and untouched.

- [ ] **Step 10: Commit**

```bash
git add gantry/io_handlers.py gantry/entities.py gantry/persistence.py \
        gantry/session.py tests/test_waveform_ingest.py
git commit -m "fix: stop discarding waveform data at ingest

populate_attrs skips all OB/OW VRs, so Waveform Data (5400,1010) was
silently dropped for every waveform IOD. Extract it explicitly and
offload to the sidecar, with an Instance accessor triad mirroring the
pixel path.

Closes #9
Refs #11"
```

---

## Task 6: Exporter protocol and format dispatch

Introduce the seam before a second format lands on `session.export()`. Behavior for existing callers must not change.

Closes #12.

**Files:**
- Create: `gantry/exporters/__init__.py`
- Create: `gantry/exporters/dicom.py`
- Modify: `gantry/session.py:1610` (`export` signature and dispatch)
- Test: `tests/test_exporter_registry.py`

**Interfaces:**
- Consumes: `DicomExporter` from `gantry/io_handlers.py`
- Produces:
  - `gantry.exporters.Exporter` — protocol with `export(session, folder, **options) -> List[str]`
  - `gantry.exporters.register(name, exporter_cls) -> None`
  - `gantry.exporters.get_exporter(name) -> Exporter`
  - `gantry.exporters.available_formats() -> List[str]`
  - `gantry.exporters.dicom.DicomFormatExporter`
  - `session.export(folder, ..., format="dicom")`

- [ ] **Step 1: Write the failing test**

Create `tests/test_exporter_registry.py`:

```python
import pytest

from gantry import exporters


def test_dicom_is_registered_by_default():
    assert "dicom" in exporters.available_formats()


def test_get_exporter_returns_a_callable_exporter():
    exp = exporters.get_exporter("dicom")
    assert hasattr(exp, "export")


def test_unknown_format_raises_with_a_helpful_message():
    with pytest.raises(ValueError) as excinfo:
        exporters.get_exporter("nifti")
    message = str(excinfo.value)
    assert "nifti" in message
    assert "dicom" in message


def test_registration_is_idempotent_for_the_same_class():
    class Dummy:
        def export(self, session, folder, **options):
            return []

    exporters.register("dummy", Dummy)
    exporters.register("dummy", Dummy)
    assert "dummy" in exporters.available_formats()


def test_registering_a_class_without_export_is_rejected():
    class NotAnExporter:
        pass

    with pytest.raises(TypeError):
        exporters.register("bogus", NotAnExporter)
```

- [ ] **Step 2: Run it to confirm it fails**

```bash
.venv/bin/python -m pytest tests/test_exporter_registry.py -q
```

Expected: collection error — `ImportError: cannot import name 'exporters' from 'gantry'`.

- [ ] **Step 3: Write the registry**

Create `gantry/exporters/__init__.py`:

```python
"""Export format registry.

Each exporter turns a `DicomSession`'s in-memory object graph into files
on disk in one output format. Formats register themselves here and are
selected via `DicomSession.export(folder, format=...)`.
"""
from typing import Any, Dict, List

_REGISTRY: Dict[str, Any] = {}


class Exporter:
    """Interface every export format implements.

    Implementations must not mutate the session's object graph — export is
    a read operation over already-de-identified data.
    """

    def export(self, session, folder: str, **options) -> List[str]:
        """Write the session to `folder`.

        Args:
            session (DicomSession): The active session.
            folder (str): Output directory. Created if absent.
            **options: Format-specific options.

        Returns:
            List[str]: Paths written.
        """
        raise NotImplementedError


def register(name: str, exporter_cls) -> None:
    """Register an export format under `name`.

    Raises:
        TypeError: If `exporter_cls` has no `export` attribute.
    """
    if not hasattr(exporter_cls, "export"):
        raise TypeError(
            f"{exporter_cls!r} cannot be registered as the {name!r} exporter: "
            "it has no 'export' method.")
    _REGISTRY[name] = exporter_cls


def get_exporter(name: str):
    """Instantiate the exporter registered under `name`.

    Raises:
        ValueError: If no such format is registered.
    """
    cls = _REGISTRY.get(name)
    if cls is None:
        known = ", ".join(sorted(_REGISTRY)) or "none"
        raise ValueError(
            f"Unknown export format {name!r}. Available formats: {known}.")
    return cls()


def available_formats() -> List[str]:
    """Return the registered format names, sorted."""
    return sorted(_REGISTRY)


from . import dicom  # noqa: E402,F401  (registers the built-in format)
```

- [ ] **Step 4: Write the DICOM adapter**

Create `gantry/exporters/dicom.py`:

```python
"""Built-in DICOM export format.

A thin adapter over the existing `DicomExporter` so the established
export path keeps its exact behavior while gaining a registry entry.
"""
from typing import List

from . import Exporter, register


class DicomFormatExporter(Exporter):
    """Writes cleaned DICOM files, preserving the legacy export behavior."""

    def export(self, session, folder: str, **options) -> List[str]:
        """Delegate to the session's existing DICOM export implementation."""
        return session._export_dicom(folder, **options)


register("dicom", DicomFormatExporter)
```

- [ ] **Step 5: Split the session's export method**

In `gantry/session.py`, rename the existing `def export(self, folder: str, version=None, ...)` at line 1610 to `_export_dicom`, leaving its entire body untouched. Then add a new dispatching `export` immediately above it:

```python
    def export(self, folder: str, format: str = "dicom", **options):
        """Export the session to a directory in the requested format.

        Args:
            folder (str): Output directory.
            format (str): Registered format name. "dicom" (default) writes
                cleaned DICOM files; "wfdb" writes PhysioNet WFDB records.
            **options: Passed through to the selected exporter. See
                `_export_dicom` for the DICOM format's options.

        Returns:
            The exporter's return value. The DICOM exporter returns None
            for backward compatibility.

        Raises:
            ValueError: If `format` is not a registered export format.
        """
        from . import exporters

        exporter = exporters.get_exporter(format)
        return exporter.export(self, folder, **options)
```

Because `_export_dicom` keeps every existing parameter as a keyword, calls like `session.export(folder, use_compression=False)` and `session.export(folder, safe=True)` continue to work unchanged.

- [ ] **Step 6: Run the new tests**

```bash
.venv/bin/python -m pytest tests/test_exporter_registry.py -q
```

Expected: 5 passed.

- [ ] **Step 7: Run the export regression tests specifically, then the full suite**

```bash
.venv/bin/python -m pytest tests/test_safe_export.py tests/test_pixel_export.py \
    tests/test_parallel_export.py tests/test_redaction_export.py \
    tests/test_query_export.py tests/test_structured_export.py -q
.venv/bin/python -m pytest -q 2>&1 | tail -20
```

Expected: both match the Task 1 baseline. One risk to watch: any existing caller passing the second positional argument (`version`) now binds it to `format`. Grep for it:

```bash
grep -rn "\.export(" tests/ gantry/ scripts/ | grep -v "export_dataframe\|export_to_parquet\|export_batch"
```

Fix any positional second argument by making it keyword.

- [ ] **Step 8: Commit**

```bash
git add gantry/exporters/ gantry/session.py tests/test_exporter_registry.py
git commit -m "refactor: extract Exporter seam with format dispatch

session.export(folder, format=...) now dispatches through a registry.
The DICOM path is unchanged, moved behind a thin adapter.

Closes #12"
```

---

## Task 7: WFDB writer

Emit `.hea` and `.dat`. One record per waveform-bearing Instance, written into the same `Patient/Study/Series` tree the DICOM exporter builds.

Closes #13.

**Files:**
- Create: `gantry/exporters/wfdb.py`
- Test: `tests/test_wfdb_writer.py`

**Interfaces:**
- Consumes: `Waveform`, `WaveformChannel` from Task 4; `Instance.get_waveform_data()` from Task 5; `register`, `Exporter` from Task 6
- Produces:
  - `WfdbExporter` (registered as `"wfdb"`)
  - `format_header(record_name, waveform, samples, dat_filename, start_datetime=None) -> str`
  - `signal_checksum(channel_samples) -> int`
  - `record_name_for(patient, study, series, instance) -> str`

- [ ] **Step 1: Write the failing test**

Create `tests/test_wfdb_writer.py`:

```python
import numpy as np
import pytest

from gantry.exporters.wfdb import format_header, signal_checksum
from gantry.waveform import Waveform, WaveformChannel


def _waveform(n_samples=100, n_channels=2, baseline=0.0, units="mV"):
    channels = [
        WaveformChannel(label=f"L{i}", source_code=f"MDC_ECG_LEAD_{i}",
                        source_scheme="MDC", sensitivity=0.005,
                        correction_factor=1.0, units=units, baseline=baseline)
        for i in range(n_channels)
    ]
    return Waveform(sampling_frequency=500.0, num_channels=n_channels,
                    num_samples=n_samples, bits_allocated=16,
                    sample_interpretation="SS", channels=channels)


def test_checksum_is_a_signed_16_bit_sum():
    assert signal_checksum(np.array([1, 2, 3], dtype=np.int16)) == 6
    # Wraps into the negative half of the 16-bit range.
    assert signal_checksum(np.array([32767, 1], dtype=np.int16)) == -32768


def test_checksum_of_empty_signal_is_zero():
    assert signal_checksum(np.array([], dtype=np.int16)) == 0


def test_record_line_carries_geometry():
    wf = _waveform(n_samples=250, n_channels=3)
    samples = np.zeros((250, 3), dtype=np.int16)
    header = format_header("REC001", wf, samples, "REC001.dat")
    first = header.splitlines()[0].split()
    assert first[0] == "REC001"
    assert first[1] == "3"
    assert first[2] == "500"
    assert first[3] == "250"


def test_signal_lines_use_spec_conformant_gain_field():
    """header(5): gain(baseline)/units. Not Murmur's current reading."""
    wf = _waveform(n_channels=1)
    samples = np.zeros((100, 1), dtype=np.int16)
    line = format_header("REC", wf, samples, "REC.dat").splitlines()[1].split()
    assert line[0] == "REC.dat"
    assert line[1] == "16"
    assert line[2] == "200(0)/mV"


def test_nonzero_baseline_appears_in_the_gain_field():
    wf = _waveform(n_channels=1, baseline=0.5)
    samples = np.zeros((100, 1), dtype=np.int16)
    line = format_header("REC", wf, samples, "REC.dat").splitlines()[1].split()
    assert line[2] == "200(-100)/mV"


def test_signal_line_reports_adcres_zero_initval_and_checksum():
    wf = _waveform(n_channels=1)
    samples = np.array([[5], [7], [9]], dtype=np.int16)
    wf.num_samples = 3
    line = format_header("REC", wf, samples, "REC.dat").splitlines()[1].split()
    assert line[3] == "16"        # adcres
    assert line[4] == "0"         # adczero
    assert line[5] == "5"         # initval
    assert line[6] == "21"        # checksum


def test_description_uses_the_coded_source_not_the_free_text_label():
    wf = _waveform(n_channels=1)
    wf.channels[0].label = "Smith, John - Lead II"
    samples = np.zeros((100, 1), dtype=np.int16)
    line = format_header("REC", wf, samples, "REC.dat").splitlines()[1]
    assert "MDC_ECG_LEAD_0" in line
    assert "Smith" not in line


def test_header_never_contains_comment_lines():
    wf = _waveform()
    samples = np.zeros((100, 2), dtype=np.int16)
    header = format_header("REC", wf, samples, "REC.dat")
    assert not any(line.startswith("#") for line in header.splitlines())


def test_start_datetime_is_rendered_in_murmur_and_wfdb_order():
    from datetime import datetime
    wf = _waveform()
    samples = np.zeros((100, 2), dtype=np.int16)
    header = format_header("REC", wf, samples, "REC.dat",
                           start_datetime=datetime(2026, 3, 14, 9, 26, 53))
    first = header.splitlines()[0].split()
    assert first[4] == "09:26:53"
    assert first[5] == "14/03/2026"


def test_gain_is_never_zero_for_a_calibrated_channel():
    """WFDB reads gain 0 as uncalibrated and substitutes 200 adu/mV."""
    wf = _waveform(n_channels=1)
    wf.channels[0].sensitivity = 0.0
    samples = np.zeros((100, 1), dtype=np.int16)
    line = format_header("REC", wf, samples, "REC.dat").splitlines()[1].split()
    assert not line[2].startswith("0(")
```

- [ ] **Step 2: Run it to confirm it fails**

```bash
.venv/bin/python -m pytest tests/test_wfdb_writer.py -q
```

Expected: collection error — `ModuleNotFoundError: No module named 'gantry.exporters.wfdb'`.

- [ ] **Step 3: Write the writer**

Create `gantry/exporters/wfdb.py`:

```python
"""PhysioNet WFDB export format.

Writes format-16 WFDB records (`.hea` + `.dat`), one record per
waveform-bearing Instance. Output is `header(5)`-conformant so it stays
readable by PhysioNet's own tooling as well as by Murmur Studio.
"""
import os
import re
from typing import List, Optional

import numpy as np

from . import Exporter, register
from ..logger import get_logger
from ..waveform import Waveform

WAVEFORM_SEQUENCE_TAG = "5400,0100"

# WFDB format 16: 16-bit two's complement, little-endian, channel-interleaved.
WFDB_FORMAT = 16
WFDB_ADC_ZERO = 0


def signal_checksum(channel_samples) -> int:
    """16-bit signed sum of a signal's samples, as `header(5)` defines it.

    Args:
        channel_samples: 1-D array of int16 samples for one channel.

    Returns:
        int: Checksum in the range [-32768, 32767].
    """
    arr = np.asarray(channel_samples)
    if arr.size == 0:
        return 0
    total = int(np.sum(arr, dtype=np.int64)) & 0xFFFF
    if total >= 0x8000:
        total -= 0x10000
    return total


def _sanitize(name: str) -> str:
    """Reduce a string to characters safe in a WFDB record name."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", str(name or ""))
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned or "record"


def record_name_for(patient, study, series, instance) -> str:
    """Build a record name from already-anonymized identifiers.

    Called after anonymization, so `patient.patient_id` is the pseudonym,
    not the source MRN. The instance number disambiguates multiple
    acquisitions within one series.
    """
    return "_".join([
        _sanitize(patient.patient_id),
        _sanitize(series.series_number if series.series_number is not None else 0),
        _sanitize(instance.instance_number if instance.instance_number is not None else 0),
    ])


def _format_number(value) -> str:
    """Render a float without a trailing '.0', which WFDB readers dislike."""
    as_float = float(value)
    if as_float.is_integer():
        return str(int(as_float))
    return repr(round(as_float, 6))


def format_header(record_name: str,
                  waveform: Waveform,
                  samples: np.ndarray,
                  dat_filename: str,
                  start_datetime=None) -> str:
    """Render a WFDB `.hea` file.

    Emits no `#` comment lines. MIT-BIH convention puts age, sex, and
    diagnosis there, and readers render comments verbatim, so a comment
    line is a PHI escape route.

    Args:
        record_name (str): Record name (must match the .hea basename).
        waveform (Waveform): Geometry and per-channel calibration.
        samples (np.ndarray): int16, shape (num_samples, num_channels).
        dat_filename (str): Signal file basename referenced by each line.
        start_datetime (datetime, optional): Already date-shifted start
            time. Omitted from the record line when None.

    Returns:
        str: Complete header text, newline-terminated.
    """
    n_samples = int(samples.shape[0]) if samples.ndim == 2 else 0
    n_channels = int(samples.shape[1]) if samples.ndim == 2 else 0

    record_fields = [
        record_name,
        str(n_channels),
        _format_number(waveform.sampling_frequency),
        str(n_samples),
    ]
    if start_datetime is not None:
        record_fields.append(start_datetime.strftime("%H:%M:%S"))
        record_fields.append(start_datetime.strftime("%d/%m/%Y"))

    lines = [" ".join(record_fields)]

    for idx in range(n_channels):
        channel = (waveform.channels[idx]
                   if idx < len(waveform.channels)
                   else waveform.channels[-1])
        column = samples[:, idx]

        gain = channel.gain()
        baseline = channel.wfdb_baseline()
        units = channel.units or "mV"

        # header(5): <gain>(<baseline>)/<units>
        gain_field = f"{_format_number(gain)}({baseline})/{units}"

        lines.append(" ".join([
            dat_filename,
            str(WFDB_FORMAT),
            gain_field,
            str(int(waveform.bits_allocated or 16)),
            str(WFDB_ADC_ZERO),
            str(int(column[0]) if column.size else 0),
            str(signal_checksum(column)),
            "0",
            channel.wfdb_description(),
        ]))

    return "\n".join(lines) + "\n"


class WfdbExporter(Exporter):
    """Writes WFDB records for every waveform-bearing instance."""

    def export(self, session, folder: str, **options) -> List[str]:
        """Write WFDB records into `folder`.

        Args:
            session (DicomSession): Active session.
            folder (str): Output root.
            **options: `patient_ids` (list, optional) limits the export.

        Returns:
            List[str]: Paths of the `.hea` files written.
        """
        logger = get_logger()
        patient_ids = options.get("patient_ids")
        written = []

        for patient in session.store.patients:
            if patient_ids and patient.patient_id not in patient_ids:
                continue

            for study in patient.studies:
                for series in study.series:
                    for instance in series.instances:
                        path = self._write_instance(
                            folder, patient, study, series, instance, logger)
                        if path:
                            written.append(path)

        logger.info(f"WFDB export complete. {len(written)} records written.")
        return written

    def _write_instance(self, folder, patient, study, series, instance, logger):
        """Write one record. Returns the .hea path, or None if not a waveform."""
        seq = instance.sequences.get(WAVEFORM_SEQUENCE_TAG)
        if seq is None or not seq.items:
            return None

        samples = instance.get_waveform_data()
        if samples is None or samples.size == 0:
            logger.warning(
                f"Instance {instance.sop_instance_uid} declares a waveform "
                "but has no sample data; skipping.")
            return None

        waveform = Waveform.from_dicom_item(seq.items[0])

        out_dir = os.path.join(
            folder,
            _sanitize(patient.patient_id),
            _sanitize(study.study_instance_uid),
            _sanitize(series.series_instance_uid),
        )
        os.makedirs(out_dir, exist_ok=True)

        record_name = record_name_for(patient, study, series, instance)
        dat_filename = f"{record_name}.dat"

        # Format 16 is little-endian, channel-interleaved -- identical to the
        # DICOM layout -- so this is a direct write with no transcoding.
        dat_path = os.path.join(out_dir, dat_filename)
        with open(dat_path, "wb") as f:
            f.write(np.ascontiguousarray(samples, dtype="<i2").tobytes())

        header = format_header(
            record_name, waveform, samples, dat_filename,
            start_datetime=self._start_datetime(instance))

        hea_path = os.path.join(out_dir, f"{record_name}.hea")
        with open(hea_path, "w", encoding="utf-8") as f:
            f.write(header)

        return hea_path

    @staticmethod
    def _start_datetime(instance):
        """Record start time. Overridden with shifted timing in Task 9."""
        return None


register("wfdb", WfdbExporter)
```

- [ ] **Step 4: Register the format on package import**

In `gantry/exporters/__init__.py`, extend the trailing import line so the new format registers too:

```python
from . import dicom, wfdb  # noqa: E402,F401  (registers the built-in formats)
```

- [ ] **Step 5: Run the tests**

```bash
.venv/bin/python -m pytest tests/test_wfdb_writer.py tests/test_exporter_registry.py -q
```

Expected: 15 passed (10 new + 5 from Task 6).

- [ ] **Step 6: Run the full suite**

```bash
.venv/bin/python -m pytest -q 2>&1 | tail -20
```

Expected: matches the Task 1 baseline.

- [ ] **Step 7: Commit**

```bash
git add gantry/exporters/wfdb.py gantry/exporters/__init__.py tests/test_wfdb_writer.py
git commit -m "feat: add WFDB format-16 exporter

Writes header(5)-conformant .hea and channel-interleaved .dat, one
record per waveform-bearing instance. No # comment lines.

Closes #13"
```

---

## Task 8: Conformance round-trip against PhysioNet's reader

This is the task that makes the writer trustworthy. Reading back with PhysioNet's own `wfdb` package validates *conformance* rather than testing Gantry against itself — a self-consistent writer/reader pair agrees happily on the wrong answer.

**If the baseline assertions here fail, the sign in `WaveformChannel.wfdb_baseline()` is the thing to flip.** That formula is derived, not verified; this test is what verifies it.

Closes #16.

**Files:**
- Test: `tests/test_wfdb_conformance.py`

**Interfaces:**
- Consumes: everything from Tasks 2, 4, 5, 6, 7
- Produces: no production code

- [ ] **Step 1: Write the round-trip test**

Create `tests/test_wfdb_conformance.py`:

```python
"""Validate Gantry's WFDB output against PhysioNet's own reader.

Deliberately does NOT use any Gantry code to read the output back.
"""
import os

import numpy as np
import pytest

wfdb = pytest.importorskip("wfdb", reason="conformance tests need the wfdb package")

from gantry.session import DicomSession
from scripts.generate_waveform_test_data import write_fixture, LEADS


@pytest.fixture
def exported(tmp_path):
    """Ingest a synthetic ECG and export it as WFDB. Returns the record path."""
    src = tmp_path / "src"
    src.mkdir()
    write_fixture(str(src / "ecg.dcm"), num_samples=1000, baseline_uv=0.0)

    out = tmp_path / "out"
    session = DicomSession(persistence_file=str(tmp_path / "conf.db"))
    try:
        session.ingest(str(src))
        paths = session.export(str(out), format="wfdb")
        assert len(paths) == 1
        yield paths[0]
    finally:
        session.close()


def _read(hea_path):
    """Read a record with PhysioNet's wfdb, given its .hea path."""
    directory = os.path.dirname(hea_path)
    name = os.path.splitext(os.path.basename(hea_path))[0]
    return wfdb.rdrecord(os.path.join(directory, name))


def test_physionet_reader_accepts_the_record(exported):
    record = _read(exported)
    assert record.n_sig == len(LEADS)
    assert record.sig_len == 1000
    assert record.fs == 500


def test_digital_samples_survive_the_round_trip(exported):
    """Channel c was written as (i % 1000) + c*1000 by the generator."""
    record = _read(exported)
    digital = record.adc()
    for channel in range(record.n_sig):
        expected = ((np.arange(1000) % 1000) + channel * 1000).astype(np.int16)
        np.testing.assert_array_equal(digital[:, channel], expected)


def test_channels_are_not_transposed(exported):
    record = _read(exported)
    digital = record.adc()
    assert digital[0, 0] == 0
    assert digital[0, 1] == 1000
    assert digital[0, 2] == 2000


def test_units_are_reported_correctly(exported):
    record = _read(exported)
    assert set(record.units) == {"uV"}


def test_lead_descriptions_come_from_the_coded_source(exported):
    record = _read(exported)
    assert record.sig_name[0] == "MDC_ECG_LEAD_I"
    assert record.sig_name[1] == "MDC_ECG_LEAD_II"


def test_no_comment_lines_reach_the_output(exported):
    with open(exported, encoding="utf-8") as f:
        assert not any(line.startswith("#") for line in f)
    record = _read(exported)
    assert not record.comments


def test_physical_values_match_the_declared_calibration(exported):
    """The generator uses sensitivity 1.0 uV/adu, so physical == digital."""
    record = _read(exported)
    physical = record.p_signal
    digital = record.adc()
    np.testing.assert_allclose(physical[:, 0], digital[:, 0].astype(float),
                               rtol=1e-6, atol=1e-6)


def test_nonzero_baseline_round_trips_to_the_correct_physical_zero(tmp_path):
    """Baseline is the formula most likely to be sign-inverted.

    With sensitivity 1.0 uV/adu and baseline 250 uV, physical zero must sit
    at ADC -250. If this fails, flip the sign in
    WaveformChannel.wfdb_baseline().
    """
    src = tmp_path / "src"
    src.mkdir()
    write_fixture(str(src / "ecg.dcm"), num_samples=100, baseline_uv=250.0)

    out = tmp_path / "out"
    session = DicomSession(persistence_file=str(tmp_path / "bl.db"))
    try:
        session.ingest(str(src))
        paths = session.export(str(out), format="wfdb")
    finally:
        session.close()

    record = _read(paths[0])
    assert record.baseline[0] == -250

    # physical = (adc - baseline) / gain = (adc + 250) / 1.0
    digital = record.adc()
    expected = digital[:, 0].astype(float) + 250.0
    np.testing.assert_allclose(record.p_signal[:, 0], expected, rtol=1e-6, atol=1e-6)


def test_dat_file_sits_next_to_the_header(exported):
    directory = os.path.dirname(exported)
    name = os.path.splitext(os.path.basename(exported))[0]
    assert os.path.exists(os.path.join(directory, f"{name}.dat"))
```

- [ ] **Step 2: Run it**

```bash
.venv/bin/python -m pytest tests/test_wfdb_conformance.py -q
```

Expected: 9 passed.

- [ ] **Step 3: If `test_nonzero_baseline_round_trips_to_the_correct_physical_zero` fails, correct the formula**

This is the anticipated failure. If `record.baseline[0]` comes back `250` rather than `-250`, the derivation's sign is inverted. In `gantry/waveform.py`, change `WaveformChannel.wfdb_baseline` to:

```python
    def wfdb_baseline(self) -> int:
        """ADC value corresponding to zero physical units."""
        return int(round(self.baseline * self.gain()))
```

Then update the two affected unit tests to match — in `tests/test_waveform_model.py`, `test_nonzero_baseline_maps_to_negated_adc_offset` expects `-100`, and in `tests/test_wfdb_writer.py`, `test_nonzero_baseline_appears_in_the_gain_field` expects `200(-100)/mV`. Re-run both files plus the conformance test. **The conformance test is authoritative** — it is the one reading through an independent implementation.

- [ ] **Step 4: Run the full suite**

```bash
.venv/bin/python -m pytest -q 2>&1 | tail -20
```

Expected: matches the Task 1 baseline plus the new tests.

- [ ] **Step 5: Commit**

```bash
git add tests/test_wfdb_conformance.py gantry/waveform.py tests/test_waveform_model.py tests/test_wfdb_writer.py
git commit -m "test: validate WFDB output against PhysioNet's reader

Round-trips synthetic ECG through ingest and export, then reads it back
with the wfdb package to verify conformance rather than self-consistency.

Closes #16"
```

---

## Task 9: De-identified header and waveform PHI surfaces

Wire date shifting into the record start time and prove the waveform text surfaces reach the existing PHI inspector.

Closes #14.

**Files:**
- Modify: `gantry/exporters/wfdb.py` (`_start_datetime`)
- Test: `tests/test_wfdb_privacy.py`

**Interfaces:**
- Consumes: `WfdbExporter` from Task 7; the session's existing `SHIFT_DATE` remediation
- Produces: `WfdbExporter._start_datetime(instance) -> Optional[datetime]` reading the post-remediation Acquisition DateTime

- [ ] **Step 1: Write the failing test**

Create `tests/test_wfdb_privacy.py`:

```python
import os

import pytest

from gantry.session import DicomSession
from scripts.generate_waveform_test_data import write_fixture


@pytest.fixture
def session_with_ecg(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    write_fixture(str(src / "ecg.dcm"), num_samples=200,
                  patient_id="MRN-12345678", patient_name="Doe^Jane")

    session = DicomSession(persistence_file=str(tmp_path / "phi.db"))
    session.ingest(str(src))
    yield session, tmp_path
    session.close()


def _header_text(paths):
    with open(paths[0], encoding="utf-8") as f:
        return f.read()


def test_channel_label_is_indexed_for_phi_scanning(session_with_ecg):
    """Free-text SH/UT inside the waveform sequence must reach the inspector."""
    session, _ = session_with_ecg
    instance = session.store.patients[0].studies[0].series[0].instances[0]
    indexed_tags = {tag for _, tag in instance.text_index}
    assert "003a,0203" in indexed_tags


def test_header_contains_no_comment_lines(session_with_ecg):
    session, tmp_path = session_with_ecg
    paths = session.export(str(tmp_path / "out"), format="wfdb")
    assert not any(line.startswith("#") for line in _header_text(paths).splitlines())


def test_record_name_excludes_the_source_patient_id(session_with_ecg):
    session, tmp_path = session_with_ecg
    paths = session.export(str(tmp_path / "out"), format="wfdb")
    header = _header_text(paths)
    assert "MRN-12345678" not in header
    assert "MRN" not in os.path.basename(paths[0])


def test_patient_name_never_appears_in_output(session_with_ecg):
    session, tmp_path = session_with_ecg
    paths = session.export(str(tmp_path / "out"), format="wfdb")
    header = _header_text(paths)
    assert "Doe" not in header
    assert "Jane" not in header


def test_start_datetime_reflects_the_shifted_acquisition_time(tmp_path):
    """The header must carry shifted timing, not the source timestamp."""
    src = tmp_path / "src"
    src.mkdir()
    write_fixture(str(src / "ecg.dcm"), num_samples=100)

    session = DicomSession(persistence_file=str(tmp_path / "shift.db"))
    try:
        session.ingest(str(src))
        instance = session.store.patients[0].studies[0].series[0].instances[0]
        # Simulate the remediation pipeline having shifted the acquisition time.
        instance.set_attr("0008,002a", "20250615081500.000000")

        paths = session.export(str(tmp_path / "out"), format="wfdb")
        record_line = _header_text(paths).splitlines()[0].split()
    finally:
        session.close()

    assert record_line[4] == "08:15:00"
    assert record_line[5] == "15/06/2025"
    assert "2026" not in " ".join(record_line)


def test_missing_acquisition_datetime_omits_timing_fields(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    write_fixture(str(src / "ecg.dcm"), num_samples=100)

    session = DicomSession(persistence_file=str(tmp_path / "nodate.db"))
    try:
        session.ingest(str(src))
        instance = session.store.patients[0].studies[0].series[0].instances[0]
        instance.set_attr("0008,002a", "")
        paths = session.export(str(tmp_path / "out"), format="wfdb")
        record_line = _header_text(paths).splitlines()[0].split()
    finally:
        session.close()

    assert len(record_line) == 4
```

- [ ] **Step 2: Run it to confirm it fails**

```bash
.venv/bin/python -m pytest tests/test_wfdb_privacy.py -q
```

Expected: the two timing tests fail — `_start_datetime` returns `None` unconditionally, so the record line has 4 fields where 6 are expected.

- [ ] **Step 3: Read the shifted acquisition time**

In `gantry/exporters/wfdb.py`, replace the `_start_datetime` stub with:

```python
    @staticmethod
    def _start_datetime(instance):
        """Record start time, read after de-identification.

        Uses Acquisition DateTime (0008,002A), falling back to Study Date +
        Study Time. This runs post-remediation, so the values are already
        shifted by the per-patient offset -- the header carries shifted
        timing, never a source timestamp.

        Returns None when no usable value exists, which omits the timing
        fields from the record line entirely.
        """
        from datetime import datetime

        raw = str(instance.attributes.get("0008,002a", "") or "").strip()
        if raw:
            stamp = raw.split("+")[0].split("-")[0].strip()
            for fmt in ("%Y%m%d%H%M%S.%f", "%Y%m%d%H%M%S", "%Y%m%d%H%M"):
                try:
                    return datetime.strptime(stamp, fmt)
                except ValueError:
                    continue

        date_part = str(instance.attributes.get("0008,0020", "") or "").strip()
        time_part = str(instance.attributes.get("0008,0030", "") or "").strip()
        if date_part:
            combined = date_part + (time_part.split(".")[0] or "000000")
            try:
                return datetime.strptime(combined, "%Y%m%d%H%M%S")
            except ValueError:
                return None

        return None
```

- [ ] **Step 4: Run the tests**

```bash
.venv/bin/python -m pytest tests/test_wfdb_privacy.py -q
```

Expected: 6 passed.

- [ ] **Step 5: Run the full suite**

```bash
.venv/bin/python -m pytest -q 2>&1 | tail -20
```

Expected: matches the Task 1 baseline plus the new tests.

- [ ] **Step 6: Commit**

```bash
git add gantry/exporters/wfdb.py tests/test_wfdb_privacy.py
git commit -m "feat: carry shifted timing into the WFDB header

Reads Acquisition DateTime post-remediation so the record line carries
shifted timing, never a source timestamp. Omits timing fields entirely
when no usable value exists.

Closes #14"
```

---

## Task 10: Murmur annotations bridge

Map the DICOM Waveform Annotation Sequence into Murmur's external-producer JSON, validated against a pinned copy of the published schema.

Closes #15.

**Files:**
- Create: `gantry/murmur.py`
- Create: `tests/fixtures/annotations.schema.json`
- Modify: `gantry/exporters/wfdb.py` (`_write_instance`)
- Modify: `scripts/generate_waveform_test_data.py` (annotation fixture support)
- Test: `tests/test_murmur_annotations.py`

**Interfaces:**
- Consumes: `Waveform` from Task 4; `WfdbExporter._write_instance` from Task 7
- Produces:
  - `build_annotations(instance, waveform, source) -> dict` — returns the full `schemaVersion: 1` document
  - `write_annotations(path, document) -> Optional[str]` — returns the path, or None when there are no findings
  - `SCHEMA_VERSION = 1`

- [ ] **Step 1: Vendor the schema**

```bash
mkdir -p tests/fixtures
curl -fsSL https://kvnlng.github.io/Murmur/annotations.schema.json \
    -o tests/fixtures/annotations.schema.json
.venv/bin/python -c "import json; json.load(open('tests/fixtures/annotations.schema.json')); print('valid json')"
```

Expected: `valid json`. Pinning it keeps CI hermetic; a drift check against the live schema is added in step 8.

- [ ] **Step 2: Add annotation support to the fixture generator**

In `scripts/generate_waveform_test_data.py`, add this function immediately after `build_ecg_dataset`:

```python
def add_annotation(ds, start_sample=100, end_sample=None,
                   code_value="164889003", code_meaning="Atrial fibrillation",
                   scheme="SCT", text=None, channel=1):
    """Attach a Waveform Annotation Sequence item to `ds`.

    Args:
        ds (Dataset): Dataset to modify in place.
        start_sample (int): 1-based Referenced Sample Position, per DICOM.
        end_sample (int, optional): Second position, making this a SEGMENT.
        code_value (str): Concept Name code value.
        code_meaning (str): Concept Name code meaning.
        scheme (str): Coding scheme designator.
        text (str, optional): Unformatted Text Value.
        channel (int): 1-based Referenced Waveform Channel.

    Returns:
        Dataset: The same dataset, for chaining.
    """
    ann = Dataset()
    ann.ReferencedWaveformChannels = [0, channel]
    ann.ConceptNameCodeSequence = [_code(code_value, code_meaning, scheme)]

    if end_sample is None:
        ann.TemporalRangeType = "POINT"
        ann.ReferencedSamplePositions = [start_sample]
    else:
        ann.TemporalRangeType = "SEGMENT"
        ann.ReferencedSamplePositions = [start_sample, end_sample]

    if text is not None:
        ann.UnformattedTextValue = text

    existing = list(getattr(ds, "WaveformAnnotationSequence", []))
    existing.append(ann)
    ds.WaveformAnnotationSequence = existing
    return ds
```

- [ ] **Step 3: Write the failing test**

Create `tests/test_murmur_annotations.py`:

```python
import json
import os

import pytest

jsonschema = pytest.importorskip("jsonschema")

from gantry.entities import DicomItem
from gantry.io_handlers import populate_attrs
from gantry.murmur import build_annotations, SCHEMA_VERSION
from gantry.waveform import Waveform
from scripts.generate_waveform_test_data import build_ecg_dataset, add_annotation


SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "fixtures",
                           "annotations.schema.json")


def _instance_from(ds):
    """Build a Gantry Instance-like item carrying ds's sequences."""
    from gantry.entities import Instance
    inst = Instance(str(ds.SOPInstanceUID), str(ds.SOPClassUID), 1)
    populate_attrs(ds, inst, inst.text_index)
    return inst


def _waveform_from(ds):
    item = DicomItem()
    populate_attrs(ds.WaveformSequence[0], item)
    return Waveform.from_dicom_item(item)


def test_point_annotation_maps_to_a_point_finding():
    ds = build_ecg_dataset(num_samples=1000)
    add_annotation(ds, start_sample=101)
    doc = build_annotations(_instance_from(ds), _waveform_from(ds), "gantry/test")

    assert doc["schemaVersion"] == SCHEMA_VERSION
    assert len(doc["findings"]) == 1
    finding = doc["findings"][0]
    assert finding["kind"] == "point"
    # DICOM sample positions are 1-based; Murmur's are 0-based.
    assert finding["startSample"] == 100


def test_segment_annotation_maps_to_a_range_finding():
    ds = build_ecg_dataset(num_samples=1000)
    add_annotation(ds, start_sample=101, end_sample=301)
    doc = build_annotations(_instance_from(ds), _waveform_from(ds), "gantry/test")

    finding = doc["findings"][0]
    assert finding["kind"] == "range"
    assert finding["startSample"] == 100
    assert finding["endSample"] == 300


def test_category_is_the_scheme_qualified_code_and_label_is_the_meaning():
    ds = build_ecg_dataset(num_samples=500)
    add_annotation(ds, code_value="164889003",
                   code_meaning="Atrial fibrillation", scheme="SCT")
    finding = build_annotations(_instance_from(ds), _waveform_from(ds),
                                "gantry/test")["findings"][0]

    assert finding["category"] == "SCT:164889003"
    assert finding["label"] == "Atrial fibrillation"


def test_lead_comes_from_the_coded_channel_source():
    ds = build_ecg_dataset(num_samples=500)
    add_annotation(ds, channel=2)
    finding = build_annotations(_instance_from(ds), _waveform_from(ds),
                                "gantry/test")["findings"][0]
    assert finding["lead"] == "MDC_ECG_LEAD_II"


def test_free_text_becomes_the_note():
    ds = build_ecg_dataset(num_samples=500)
    add_annotation(ds, text="Onset preceded by R-on-T")
    finding = build_annotations(_instance_from(ds), _waveform_from(ds),
                                "gantry/test")["findings"][0]
    assert finding["note"] == "Onset preceded by R-on-T"


def test_absolute_time_is_never_emitted():
    ds = build_ecg_dataset(num_samples=500)
    add_annotation(ds, start_sample=10)
    finding = build_annotations(_instance_from(ds), _waveform_from(ds),
                                "gantry/test")["findings"][0]
    assert "startUnixMS" not in finding
    assert "endUnixMS" not in finding


def test_no_annotation_sequence_yields_no_findings():
    ds = build_ecg_dataset(num_samples=500)
    doc = build_annotations(_instance_from(ds), _waveform_from(ds), "gantry/test")
    assert doc["findings"] == []


def test_output_validates_against_murmurs_published_schema():
    ds = build_ecg_dataset(num_samples=1000)
    add_annotation(ds, start_sample=101, end_sample=301,
                   text="Range finding")
    add_annotation(ds, start_sample=500, channel=3)
    doc = build_annotations(_instance_from(ds), _waveform_from(ds), "gantry/1.0")

    with open(SCHEMA_PATH, encoding="utf-8") as f:
        schema = json.load(f)

    jsonschema.validate(instance=doc, schema=schema)
```

- [ ] **Step 4: Run it to confirm it fails**

```bash
.venv/bin/python -m pytest tests/test_murmur_annotations.py -q
```

Expected: collection error — `ModuleNotFoundError: No module named 'gantry.murmur'`.

- [ ] **Step 5: Write the bridge**

Create `gantry/murmur.py`:

```python
"""Bridge DICOM Waveform Annotations to Murmur Studio's producer JSON.

Maps Waveform Annotation Sequence (0040,B020) into the
`<record>.annotations.json` format documented at
https://kvnlng.github.io/Murmur/annotation-schema

Gantry transcribes; it does not interpret. Coded concepts are passed
through scheme-qualified rather than normalized into a clinical
vocabulary of Gantry's own, so a finding says exactly what the
originating cart said.
"""
import json
import os
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = 1

TAG_ANNOTATION_SEQ = "0040,b020"
TAG_REFERENCED_CHANNELS = "0040,a0b0"
TAG_TEMPORAL_RANGE_TYPE = "0040,a130"
TAG_REFERENCED_SAMPLE_POSITIONS = "0040,a132"
TAG_REFERENCED_TIME_OFFSETS = "0040,a138"
TAG_CONCEPT_NAME_CODE_SEQ = "0040,a043"
TAG_UNFORMATTED_TEXT = "0070,0006"

TAG_CODE_VALUE = "0008,0100"
TAG_CODING_SCHEME = "0008,0102"
TAG_CODE_MEANING = "0008,0104"

_RANGE_TYPES = {"SEGMENT", "MULTISEGMENT"}


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _first_int(values) -> Optional[int]:
    for v in values:
        try:
            return int(v)
        except (TypeError, ValueError):
            continue
    return None


def _lead_for(waveform, referenced_channels) -> Optional[str]:
    """Resolve a Referenced Waveform Channels pair to a coded lead name.

    The attribute is a (multiplex group, channel) pair with a 1-based
    channel number.
    """
    values = _as_list(referenced_channels)
    if len(values) < 2:
        return None
    try:
        channel_number = int(values[1])
    except (TypeError, ValueError):
        return None

    index = channel_number - 1
    if 0 <= index < len(waveform.channels):
        return waveform.channels[index].wfdb_description()
    return None


def _sample_positions(item, waveform) -> List[int]:
    """Return 0-based sample positions for an annotation item.

    Prefers Referenced Sample Positions (1-based in DICOM). Falls back to
    Referenced Time Offsets, converted via the sampling frequency.
    """
    positions = _as_list(item.attributes.get(TAG_REFERENCED_SAMPLE_POSITIONS))
    resolved = []
    for value in positions:
        try:
            resolved.append(max(0, int(value) - 1))
        except (TypeError, ValueError):
            continue
    if resolved:
        return resolved

    offsets = _as_list(item.attributes.get(TAG_REFERENCED_TIME_OFFSETS))
    fs = waveform.sampling_frequency or 0.0
    if not fs:
        return []
    for value in offsets:
        try:
            resolved.append(max(0, int(round(float(value) * fs))))
        except (TypeError, ValueError):
            continue
    return resolved


def _concept(item):
    """Return (category, label) from the Concept Name Code Sequence."""
    seq = item.sequences.get(TAG_CONCEPT_NAME_CODE_SEQ)
    if seq is None or not seq.items:
        return None, None

    attrs = seq.items[0].attributes
    code = str(attrs.get(TAG_CODE_VALUE, "") or "")
    scheme = str(attrs.get(TAG_CODING_SCHEME, "") or "")
    meaning = str(attrs.get(TAG_CODE_MEANING, "") or "")

    if not code:
        return None, meaning or None

    category = f"{scheme}:{code}" if scheme else code
    return category, (meaning or None)


def build_annotations(instance, waveform, source: str) -> Dict[str, Any]:
    """Build a Murmur annotations document from an instance's annotations.

    Args:
        instance (Instance): The waveform instance, post-remediation.
        waveform (Waveform): Parsed geometry, used for lead and time lookup.
        source (str): Producer identifier written to the document.

    Returns:
        dict: A `schemaVersion: 1` document. `findings` is empty when the
        instance carries no annotations.
    """
    findings: List[Dict[str, Any]] = []

    seq = instance.sequences.get(TAG_ANNOTATION_SEQ)
    items = seq.items if seq is not None else []

    for item in items:
        category, label = _concept(item)
        if not category:
            # Without a category there is nothing for Murmur to colour or
            # group by, and the schema requires it.
            continue

        positions = _sample_positions(item, waveform)
        if not positions:
            continue

        range_type = str(item.attributes.get(TAG_TEMPORAL_RANGE_TYPE, "") or "").upper()
        is_range = range_type in _RANGE_TYPES and len(positions) >= 2

        finding: Dict[str, Any] = {
            "kind": "range" if is_range else "point",
            "startSample": positions[0],
            "category": category,
        }
        if is_range:
            finding["endSample"] = positions[1]
        if label:
            finding["label"] = label

        lead = _lead_for(waveform, item.attributes.get(TAG_REFERENCED_CHANNELS))
        if lead:
            finding["lead"] = lead

        note = item.attributes.get(TAG_UNFORMATTED_TEXT)
        if note:
            finding["note"] = str(note)

        findings.append(finding)

    return {
        "schemaVersion": SCHEMA_VERSION,
        "source": source,
        "findings": findings,
    }


def write_annotations(path: str, document: Dict[str, Any]) -> Optional[str]:
    """Write an annotations document, skipping empty ones.

    Returns:
        Optional[str]: The path written, or None if there were no findings.
    """
    if not document.get("findings"):
        return None

    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(document, f, indent=2)
        f.write("\n")

    return path
```

- [ ] **Step 6: Run the tests**

```bash
.venv/bin/python -m pytest tests/test_murmur_annotations.py -q
```

Expected: 8 passed.

- [ ] **Step 7: Emit the file during WFDB export**

In `gantry/exporters/wfdb.py`, in `_write_instance`, immediately before `return hea_path`:

```python
        from ..murmur import build_annotations, write_annotations

        try:
            from .. import __version__ as gantry_version
        except ImportError:
            gantry_version = "0.0.0"

        manufacturer = str(instance.attributes.get("0008,0070", "") or "").strip()
        source = f"gantry/{gantry_version}"
        if manufacturer:
            source = f"{source} ({manufacturer})"

        write_annotations(
            os.path.join(out_dir, f"{record_name}.annotations.json"),
            build_annotations(instance, waveform, source))
```

Then add a test to `tests/test_murmur_annotations.py`:

```python
def test_annotations_file_lands_beside_the_header(tmp_path):
    import pydicom
    from gantry.session import DicomSession
    from scripts.generate_waveform_test_data import build_ecg_dataset, add_annotation

    src = tmp_path / "src"
    src.mkdir()
    ds = add_annotation(build_ecg_dataset(num_samples=500), start_sample=101)
    pydicom.dcmwrite(str(src / "ecg.dcm"), ds, write_like_original=False)

    session = DicomSession(persistence_file=str(tmp_path / "ann.db"))
    try:
        session.ingest(str(src))
        paths = session.export(str(tmp_path / "out"), format="wfdb")
    finally:
        session.close()

    base = os.path.splitext(paths[0])[0]
    ann_path = f"{base}.annotations.json"
    assert os.path.exists(ann_path)

    with open(ann_path, encoding="utf-8") as f:
        doc = json.load(f)
    assert doc["schemaVersion"] == 1
    assert doc["findings"][0]["startSample"] == 100
    assert doc["source"].startswith("gantry/")
```

- [ ] **Step 8: Add the non-blocking schema drift check**

Create `.github/workflows/schema-drift.yml`:

```yaml
name: Murmur Schema Drift

on:
  schedule:
    - cron: '0 6 * * 1'
  workflow_dispatch:

permissions:
  contents: read

jobs:
  check:
    runs-on: ubuntu-latest
    continue-on-error: true
    steps:
      - uses: actions/checkout@v4

      - name: Compare pinned schema against the published one
        run: |
          curl -fsSL https://kvnlng.github.io/Murmur/annotations.schema.json \
            -o /tmp/live.json
          if diff -q tests/fixtures/annotations.schema.json /tmp/live.json; then
            echo "Pinned schema matches upstream."
          else
            echo "::warning::Murmur's published annotations schema has changed."
            diff -u tests/fixtures/annotations.schema.json /tmp/live.json || true
            exit 1
          fi
```

`continue-on-error: true` keeps this advisory — schema drift should surface as a warning to act on, not a broken build.

- [ ] **Step 9: Run the full suite**

```bash
.venv/bin/python -m pytest -q 2>&1 | tail -20
```

Expected: matches the Task 1 baseline plus every test added across tasks 2–10.

- [ ] **Step 10: Commit**

```bash
git add gantry/murmur.py gantry/exporters/wfdb.py scripts/generate_waveform_test_data.py \
        tests/test_murmur_annotations.py tests/fixtures/annotations.schema.json \
        .github/workflows/schema-drift.yml
git commit -m "feat: bridge DICOM waveform annotations to Murmur JSON

Maps (0040,B020) to <record>.annotations.json with scheme-qualified
codes as category and Code Meaning as label. Sample indices only, never
absolute time. Validated against a pinned copy of Murmur's schema, with
a weekly advisory drift check.

Closes #15"
```

---

## Task 11: Documentation and changelog

**Files:**
- Create: `docs/waveforms.md`
- Modify: `mkdocs.yml` (nav)
- Modify: `CHANGELOG.md`
- Modify: `README.md`

- [ ] **Step 1: Write the user guide**

Create `docs/waveforms.md`:

```markdown
# Waveforms and WFDB Export

Gantry ingests DICOM waveform IODs — 12-Lead ECG, General ECG,
Hemodynamic, and related SOP classes — alongside image data, and exports
them as PhysioNet WFDB records.

## Quick start

```python
from gantry import Session

session = Session("ecg_study.db")
session.ingest("/data/ecg")
session.audit()
session.anonymize()
session.export("/out", format="wfdb")
```

Each waveform instance becomes one WFDB record:

```
out/<patient>/<study>/<series>/
├─ <record>.hea               header
├─ <record>.dat               format-16 samples
└─ <record>.annotations.json  cart findings, when present
```

## What is exported

| WFDB field | DICOM source |
|---|---|
| `fs` | Sampling Frequency `(003A,001A)` |
| `gain` | Derived from Channel Sensitivity `(003A,0210)` and its correction factor |
| `units` | Channel Sensitivity Units Sequence `(003A,0211)` |
| signal description | Channel Source Sequence `(003A,0208)` |

Signals are written as WFDB format 16 (16-bit, little-endian,
channel-interleaved).

## Privacy

- Record names derive from anonymized identifiers only.
- `basedate` / `basetime` carry the same per-patient date shift applied
  to the DICOM metadata.
- **No `#` comment lines are ever written.** WFDB readers render header
  comments verbatim, and MIT-BIH convention places age, sex, and
  diagnosis there.
- Lead identity uses the coded channel source rather than the free-text
  Channel Label, which is operator-entered and can contain PHI.

## Limitations

Format 16 only; multi-rate records, WFDB ingest, `.atr` output, and
mu-law/A-law companded audio are not supported.
```

- [ ] **Step 2: Add it to the nav**

In `mkdocs.yml`, under the `Guides:` section, after the `'Analytics & Reporting': analytics.md` line:

```yaml
      - 'Waveforms & WFDB': waveforms.md
```

- [ ] **Step 3: Update the changelog**

In `CHANGELOG.md`, under `## [Unreleased]` → `### Added`:

```markdown
- **DICOM Waveform Support**: Waveform IODs are now ingested, persisted, and de-identified as a first-class data type alongside pixel data.
- **WFDB Export**: `session.export(folder, format="wfdb")` writes `header(5)`-conformant PhysioNet WFDB records (format 16).
- **Murmur Annotation Bridge**: Waveform Annotation Sequence `(0040,B020)` is exported as `<record>.annotations.json` for [Murmur Studio](https://github.com/kvnlng/Murmur).
- **Export Format Registry**: `gantry.exporters` provides a pluggable `Exporter` seam; `session.export()` dispatches on `format`.
```

And under `### Fixed`, creating the section if absent:

```markdown
- **Waveform Data Loss**: Waveform Data `(5400,1010)` was silently discarded at ingest because `populate_attrs` skips all `OB`/`OW` VRs. Waveform IODs now round-trip intact.
```

- [ ] **Step 4: Update the README feature list**

In `README.md`, in the `## Features` list, after the `**Codecs**` bullet:

```markdown
- **Waveforms**: Ingest DICOM waveform IODs (ECG, hemodynamic) and export PhysioNet WFDB records, bridging to [Murmur Studio](https://github.com/kvnlng/Murmur).
```

- [ ] **Step 5: Verify the docs build**

```bash
.venv/bin/python -m pip install -e ".[docs]"
.venv/bin/mkdocs build --strict
```

Expected: build succeeds. `--strict` turns nav and link warnings into errors, which catches a mistyped filename. Confirm `docs/superpowers/` is excluded from the output:

```bash
test ! -d site/superpowers && echo "specs correctly excluded"
```

- [ ] **Step 6: Commit**

```bash
git add docs/waveforms.md mkdocs.yml CHANGELOG.md README.md
git commit -m "docs: document waveform ingest and WFDB export"
```

---

## Self-Review Notes

**Spec coverage:** Every section of the spec maps to a task — architecture (6, 7), storage (3), WFDB writer (7), de-identification (9), annotation bridge (10), testing (2, 8), prerequisites (1). The spec's "Implementation order" diagram matches this task sequence, with #12's exporter seam pulled to Task 6 so Task 7 has something to register into.

**Deliberate deviation from the spec:** the spec declines to state the baseline conversion formula, deferring it to a test. This plan *does* state it (`wfdb_baseline() = -baseline * gain`, derived in Task 4) because the implementer needs runnable code — and Task 8 Step 3 makes the conformance test authoritative, with the exact remediation to apply if the derived sign is wrong. This is the honest version of "verify, don't derive": commit to a testable claim, then test it against an independent implementation.

**Known risk carried into Task 6:** `session.export()` gains `format` as its second parameter, displacing the legacy `version` argument. Step 7 greps for positional callers. If any exist in user code outside this repo, this is a breaking change worth noting in the changelog — flag it if the grep finds hits.

**Two defects found and fixed during self-review**, both in the seam between the ingest path and the new blob table:

1. **Compaction would have destroyed ingested pixel data.** `DicomImporter.import_files` writes pixel frames through `SidecarManager` directly and never calls `persist_pixel_data()`, so `instance_blobs` stays empty for anything ingested in the current session. A `compact_sidecar()` reading only that table would have judged every ingested pixel blob orphaned and reclaimed its bytes. Fixed by re-running the back-fill inside compaction (Task 3 Step 7) and proven by `test_compaction_preserves_both_kinds`. Waveforms need the complementary fix — they have no legacy column to back-fill from — so ingest registers their reference explicitly via `record_blob_ref` (Task 5 Step 5).

2. **Waveforms would not have survived a session reload.** Hydration rewires `_pixel_loader` from the `instances.pixel_offset` columns, but waveforms have no such columns and nothing read `instance_blobs` back. Ingest-then-export in one session would have passed every conformance test while pause/resume — Gantry's core promise for large jobs — silently lost the data. Fixed in Task 5 Step 7, covered by `test_waveform_survives_a_session_reload`.

Both were invisible from the spec and only surfaced from tracing the actual ingest call path. Worth re-checking in review that the back-fill call precedes the `SELECT` in `compact_sidecar`, since ordering is what makes the first fix work.
