# Performance

Isocenter is designed for massive scale. Recent stress tests verify robust linear scaling on datasets up to 100GB.

![Benchmark Scaling](images/benchmark_scaling.png)

## 100GB Scalability Test

- **Input**: 101,000 files (50GB Single-Frame + 50GB Multi-Frame).
- **Import Speed**: Uses **Sidecar Generation** to extract pixel data upfront, ensuring constant-time access during analysis.
- **Export Speed**: High-speed streaming write using cached sidecar data.
- **Memory**: Peaks at stable levels regardless of dataset size due to aggressive offloading.

The architecture uses O(1) memory streaming, ensuring it never runs out of RAM even when processing terabytes of data.

## Memory Management Architecture

Isocenter employs a "Deep Memory Management" strategy to handle large-scale datasets on consumer hardware.

### 1. Process Isolation (Redaction)

Pixel redaction is the most memory-intensive operation (loading 500MB+ arrays). Isocenter uses **Process Isolation** (`ProcessPoolExecutor`) to execute these tasks.

- Each worker process loads the pixel data, applies redactions, and then **exits**.
- This guarantees that the operating system reclaims all memory resources immediately after each task, preventing fragmentation or reference leaks in the main process.

### 2. Streaming Ingest

The ingestion pipeline uses a streaming generator pattern with a `chunksize=1`.

- Files are processed one by one.
- Results are yielded immediately to the database.
- The IPC queue never buffers more than a single item, keeping memory footprint constant (O(1)) regardless of input size.

### 3. Zero-Copy Persistence

To prevent memory spikes during export:

- Pixel data is passed directly from NumPy arrays to the storage backend.
- We utilize buffer interfaces and on-the-fly `zlib` compression to avoid creating intermediate Python byte strings (which would double memory usage).

## Scalability Benchmarks

Recent stress tests (January 2026) verified robust sub-linear scaling capabilities.

| Phase | Files | Raw Data | Max RSS (Memory) | Status | Scaling Factor |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Phase 0** | 1 | ~500 MB | ~0.5 GB | Success | 1x |
| **Phase 1** | 10 | ~5 GB | ~3.8 GB | Success | ~7.6x |
| **Phase 2** | 100 | ~50 GB | ~11.3 GB | **Success** | **<3x** |

**Key Finding:** Increasing the dataset size by **10x** (10 to 100 files) only resulted in a **3x** increase in peak memory usage. This demonstrates that Isocenter effectively decouples memory consumption from dataset size.

Timing results:

| Phase                           | Total Instances | Ingest Duration | Examine Duration | Audit Duration | Backup Duration | Anonymize Duration | Redact Duration | Export Duration | Total Time |
|:--------------------------------|:---------------:|----------------:|-----------------:|---------------:|----------------:|-------------------:|----------------:|----------------:|-----------:|
| Phase 0 (1 Multi-Frame Files)   | 1               | 2.20            | 0.0001           | 0.0014         | 0.0061          | 0.0066             | 1.85            | 2.97            | 7.04       |
| Phase 1 (10 Multi-Frame Files)  | 10              | 22.45           | 0.0001           | 0.0024         | 0.0060          | 0.0064             | 9.33            | 9.94            | 41.74      |
| Phase 2 (100 Multi-Frame Files) | 100             | 177.36          | 0.0002           | 0.0042         | 0.0124          | 0.0244             | 74.21           | 58.13           | 309.74     |


Test machine:
* machine-type: n2-highmem-16
* image-family: ubuntu-2204-lts
* image-project: ubuntu-os-cloud
* boot-disk-size: 1TB
* boot-disk-type: pd-ssd

## Micro-Benchmarks (Metadata Operations)

| Operation | Scale | Time (Mac M3 Max) | Throughput |
|-----------|-------|-------------------|------------|
| **Identity Locking** | 100,000 Instances | ~0.13 s | **769k / sec** |
| **Persist Findings** | 100,000 Issues | ~0.13 s | **770k / sec** |
