# Performance

This page carries the one benchmark that has a recorded run behind it, the machine it ran on, and the architecture that produced the numbers. Larger runs are planned; they will be added here with their own machine and date when they exist, and nothing on this page describes a run that has not happened.

## The recorded run

**January 2026, Google Cloud `n2-highmem-16`, Ubuntu 22.04, 1 TB `pd-ssd` boot disk.** The stress harness in the repository (`tests/benchmarks/run_stress_test.py`) generated multi-frame instances with frame counts from 1 to 100, in three phases of one order of magnitude each, and ran the full pipeline on each phase: ingest, examine, audit, backup (locking identities), anonymize, redact, export with JPEG 2000 compression.

### Peak memory

| Phase | Files | Raw data | Peak RSS | Change |
| :--- | :--- | :--- | :--- | :--- |
| 0 | 1 | ~0.5 GB | ~0.5 GB | |
| 1 | 10 | ~5 GB | ~3.8 GB | ~7.6x for 10x data |
| 2 | 100 | ~50 GB | ~11.3 GB | ~3x for 10x data |

From phase 1 to phase 2 the data grew tenfold and peak memory grew about threefold. That is the measurement. It is consistent with the design below, in which resident memory is bounded by the working set of the workers rather than by the size of the cohort, and it is the only scaling claim this page makes.

### Timing

Seconds per step.

| Phase | Instances | Ingest | Examine | Audit | Backup | Anonymize | Redact | Export | Total |
|:---|:---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0 | 1 | 2.20 | 0.0001 | 0.0014 | 0.0061 | 0.0066 | 1.85 | 2.97 | 7.04 |
| 1 | 10 | 22.45 | 0.0001 | 0.0024 | 0.0060 | 0.0064 | 9.33 | 9.94 | 41.74 |
| 2 | 100 | 177.36 | 0.0002 | 0.0042 | 0.0124 | 0.0244 | 74.21 | 58.13 | 309.74 |

Ingest, redact, and export are the steps that touch pixels, and they scale with the data. Examine, audit, anonymize, and backup work on the metadata index and stay small.

### What the run does not tell you

- It used generated multi-frame files, not a clinical archive. Real cohorts have more instances per gigabyte and more metadata per instance.
- It is one machine, one run. Repeat it before using the numbers for sizing, and record the machine.
- A larger, 412 GB three-phase run is planned and has not been completed; there are no numbers for it.

## The architecture behind the numbers

### Pixel data lives in a sidecar, not in memory

At ingest, pixel and waveform bytes are appended to a binary sidecar beside the SQLite index and referenced by offset and length. An `Instance` holds a loader, not an array. `get_pixel_data()` reads from the sidecar on demand and `unload_pixel_data()` releases the array, so a cohort's pixels are never resident as a whole. `unload_pixel_data()` refuses to drop an array that was replaced in memory and not yet written, because the loader would bring back the old frame; `discard_pixel_data()` is the explicit form of that decision.

### Redaction runs in worker processes

Pixel redaction loads full arrays and is the most memory-intensive step. It runs through a pool of half the CPUs, at most eight -- a memory ceiling, not a throughput choice: each worker loads the instance's pixels, applies the zones, writes the result, and returns. On a free-threaded interpreter the same dispatcher uses threads instead, because there is no GIL to escape, and on a `:memory:` store it always uses threads, because a process cannot share an in-memory database. The worker count, chunk size, and strategy are set by the environment variables in [Environment Variables](environment.md). Export is the exception: it always runs in processes and recycles each worker after 25 tasks, on every interpreter, because the imaging C libraries leak and a thread has no process to recycle. That bounds any growth inside an export worker to 25 tasks, and it is why `ISOCENTER_FORCE_THREADS` does not reach `export()`.

### Ingest streams results into the index

Files are scanned by workers and their results are written to the index as they arrive rather than collected and written at the end, so ingest memory does not grow with the number of files scanned. The `ISOCENTER_CHUNKSIZE` variable trades per-task overhead against how many results are in flight at once.

### Metadata is loaded in one pass

Standard tags are stored as one JSON document per instance and read back whole; private tags live in a sparse table. Reopening a session loads metadata for the cohort from the index in one pass rather than one file at a time, and pixels stay in the sidecar until asked for.

## Sizing guidance

- **Memory**: 2 GB RAM per vCPU as a floor. 8 GB per vCPU for heavy multi-frame JPEG 2000 export, which holds a decoded and an encoded copy of a frame at once.
- **Concurrency**: one worker per CPU by default for ingest, audit and export; `redact()` defaults to half the CPUs and at most eight. Set `ISOCENTER_MAX_WORKERS` to limit both if a worker is killed for memory.
- **Disk**: the sidecar holds the cohort's pixels, so budget roughly the raw data size for it plus the export.

## Running it yourself

The harness is not part of the installed package. From a clone of the repository, with the development dependencies installed (see [Contributing](developer_guide.md)):

```bash
python -m tests.benchmarks.run_stress_test --input <dicom-dir> --output <out-dir>
```

It runs the pipeline against the directory you name. If you run it, open an issue with the machine, the date, and the table; this page is where it belongs.
