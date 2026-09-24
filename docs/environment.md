# Environment Variables

You can tune the system performance and behavior using environment variables. Set them in your shell or in your process's environment. `isocenter` does not read a `.env` file: until 0.9.8 importing it loaded one, from wherever the package happened to be installed, and since [#543](https://github.com/kvnlng/Isocenter/issues/543) it changes nothing about your environment. To use a `.env`, call `dotenv.load_dotenv()` yourself before importing `isocenter`.

Every flag below whose default is `0` is read by `_env_is`, which compares against the literal string `1`. `true`, `yes` and `on` do nothing at all — they are not rejected, they are simply not the value being looked for.

Each variable's name, default and documented behaviour are part of the 1.0 freeze ([API stability](api/stability.md#frozen-at-10)). The table is the index; each variable has its own section below, with its default, what it does, what it does not reach, its floor, and the issues behind it.

| Variable | Default | Summary |
| :--- | :--- | :--- |
| **`ISOCENTER_LOG_LEVEL`** | `DEBUG` | Logging verbosity, for the console and the log file. |
| **`ISOCENTER_LOG_FILE`** | `isocenter.log` | Where the log file is written. |
| **`ISOCENTER_DB_PATH`** | `isocenter.db` | The store `Session()` opens when given none; `:memory:` is accepted. |
| **`ISOCENTER_MAX_WORKERS`** | `os.cpu_count()` | How many workers a parallel pass uses; `redact()` has its own default. |
| **`ISOCENTER_CHUNKSIZE`** | `1` | How many items each worker task carries. |
| **`ISOCENTER_MAX_TASKS_PER_CHILD`** | *Unlimited* | Recycle each worker process after N tasks; always means processes. |
| **`ISOCENTER_DISABLE_GC`** | `0` | `1` disables garbage collection in worker processes. |
| **`ISOCENTER_FORCE_THREADS`** | `0` | `1` runs `audit()`, `scan_pixel_content()` and `redact()` in threads. |
| **`ISOCENTER_FORCE_PROCESSES`** | `0` | `1` runs in processes on a free-threaded build too. |
| **`ISOCENTER_SHOW_PROGRESS`** | `1` | `0` turns every progress bar off. |
| **`ISOCENTER_WORKER_FAULTHANDLER`** | `0` | Diagnostic: `1` makes a worker process that is still alive after 240 s dump its stacks. |

## `ISOCENTER_LOG_LEVEL`

**Default:** `DEBUG`.

Logging verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`. It gates the console as well as the log file: at `ERROR` or `CRITICAL`, `close()`'s unsaved-instances warning reaches neither stdout nor `isocenter.log`.

## `ISOCENTER_LOG_FILE`

**Default:** `isocenter.log`.

The path the file handler writes to. The file handler takes `DEBUG` and above; the console handler takes `WARNING` and above and is unaffected by this variable. An explicit `log_file` argument to `configure_logger()` wins over it.

## `ISOCENTER_DB_PATH`

**Default:** `isocenter.db`.

The path to the SQLite session database, read when `Session()` is given no `persistence_file`.

`:memory:` is accepted, here or as `Session(":memory:")`, and is part of the frozen surface ([#379](https://github.com/kvnlng/Isocenter/issues/379)):

- the index lives in memory, and the pixel sidecar in a temporary file the store removes on `close()`;
- `redact()` runs in threads on every interpreter ([#381](https://github.com/kvnlng/Isocenter/issues/381)), or refuses if worker recycling is asked for ([#400](https://github.com/kvnlng/Isocenter/issues/400); see [`ISOCENTER_FORCE_PROCESSES`](#isocenter_force_processes) and [`ISOCENTER_MAX_TASKS_PER_CHILD`](#isocenter_max_tasks_per_child)).

## `ISOCENTER_MAX_WORKERS`

**Default:** one worker per CPU (`os.cpu_count()`, or `1` when it cannot answer), except `redact()`, below.

**Floor:** 1. A value **below 1**, `0` or any negative, is not a worker count: it is reported with a warning naming the variable and the value, and the default is used instead. `1` is how you ask for a single worker.

How many workers a parallel run uses, **processes or threads**, whichever the [three levers](#which-levers-win) resolve to. The same number sizes all three pools:

- **`run_parallel()`'s pool.** Its default is one per CPU. `resolve_max_workers` in `parallel.py` owns that expression and is the only place it is computed.
- **The session's shared pool**, the `ProcessPoolExecutor` that `ingest()` runs on. It is sized by the same helper, so it has the same default.
    - The variable is read at `Session()` **and again at the start of every `ingest()`**, which rebuilds the pool when the width has changed. A value set after the session opens reaches the next `ingest()`. An unchanged width rebuilds nothing, so an ordinary session keeps one pool for its lifetime.
    - The one exception is a **second `ingest()` running on another thread of the same session**. The pool is shared between them, and shutting it down would cancel the queued files of the call already running, so the resize is skipped and one `WARNING` names this variable, the pool's width and the width asked for. The next `ingest()` that starts with no other in flight is built at the new width.
    - It is processes on every build, 3.14t included, where only `run_parallel()`'s own pool takes threads.
    - **When a worker process of that pool ends during an `ingest()`** (the out-of-memory killer, say), the files not yet returned are read again on fresh process pools built by the call itself: one worker while the file that ends it is found, then this many, on every build, 3.14t included. The session's pool is replaced once the call has saved.
- **`redact()`'s pool.** Its default is **half the CPUs, capped at eight, never below one**: `max(1, min((os.cpu_count() or 1) // 2, 8))` in `_redaction_worker_count` (`session.py`). Each redaction worker holds a decoded frame, so that number is a memory ceiling, not a throughput choice: one per CPU exhausted memory on large studies. `redact()` reads this variable there too, and rejects a value below 1 the same way, falling back to its own default.

This variable overrides both defaults, with the same floor. The below-1 warning also appears once when `Session()` opens, because building the shared pool reads the variable, and once per `ingest()`, because each one re-resolves it.

`tests/test_parallel_contract.py` holds the first default and `tests/test_redaction_worker_count.py` the second, at fixed CPU counts so the cap is exercised on any machine.

**History.** An earlier version defaulted to `CPU_COUNT * 1.5`. It was **deliberately abandoned**, because predictable beats marginally faster when a run is hours long, so if you remember the 1.5x, it is gone rather than merely undocumented. This page stated the one-per-CPU default with no test behind it until [#333](https://github.com/kvnlng/Isocenter/issues/333). The shared pool ignored the variable until [#501](https://github.com/kvnlng/Isocenter/issues/501), and until [#511](https://github.com/kvnlng/Isocenter/issues/511) it read the variable only when the pool was built. Before [#335](https://github.com/kvnlng/Isocenter/issues/335), `0` was discarded silently and a negative raised the pool's own `ValueError` without naming the variable; `redact()` clamped both to one worker in silence until [#341](https://github.com/kvnlng/Isocenter/issues/341). The retry pools are [#654](https://github.com/kvnlng/Isocenter/issues/654).

## `ISOCENTER_CHUNKSIZE`

**Default:** `1`.

**Floor:** 1. A value **below 1**, `0` or any negative, is not a batch size: it is reported with a warning naming the variable and the value, and the default of `1` is used instead. It is the same floor as `ISOCENTER_MAX_WORKERS` and `ISOCENTER_MAX_TASKS_PER_CHILD`, and one floor: all three are read through `_env_int(..., minimum=1)`.

The batch size for inter-process communication. Increasing it (to 5 or 10, say) can improve performance for very small items.

**The retry rounds `ingest()` runs after a worker process ends ignore this variable and send one file per task.** A chunk is one task, so a death anywhere in a chunk loses every result in it, and the file that ended the worker cannot be told from the others in its chunk. With this variable at `3` and one worker, a good file beside the fatal one was rejected, as measured on the first version of that fix. The first round of each call uses the value set here, as every other parallel step does.

**History.** Until [#341](https://github.com/kvnlng/Isocenter/issues/341), `0` was discarded silently, a negative was carried into the map call and raised the pool's own `ValueError` without naming the variable, and the threads path ignored the variable altogether. The retry rounds are [#654](https://github.com/kvnlng/Isocenter/issues/654).

## `ISOCENTER_MAX_TASKS_PER_CHILD`

**Default:** *Unlimited* (workers are not recycled).

**Floor:** 1. A value **below 1**, `0` or any negative, is not a recycling interval: it is reported with a warning naming the variable and the value, and no recycling is used.

Restart worker processes after N tasks to release memory. Useful if you suspect memory leaks in underlying libraries.

- **Setting it at all turns threads off** for every call site it reaches, because on 3.12, the floor, only `multiprocessing.Pool` can recycle workers. `ProcessPoolExecutor` has had `max_tasks_per_child` since 3.11, but on 3.12 it deadlocks `map` the first time a worker is replaced (measured on 3.12.14; 3.14 and 3.14t are fine). See [Which levers win](#which-levers-win).
- **`redact()` on a `:memory:` store refuses it.** That store's redaction asks for threads ([#381](https://github.com/kvnlng/Isocenter/issues/381)), and this variable would override that request. Instead `redact()` raises `RuntimeError` naming the store, this variable, why processes cannot work for an in-memory database, and two remedies: unset it, or use a file-backed store. It raises before the pass-lock is taken and before any task is prepared, so nothing has happened when it arrives. `except RedactionError` does not catch the refusal; `except RuntimeError` does.
- **It cannot reach `session.export()` in either direction.** `_resolve_strategy` consults the environment only when the `maxtasksperchild` argument is `None`, and the export path always passes `25`, so neither raising nor lowering this changes what an export does.
- **Nor does it reach `ingest()`**, which runs on the session's own `ProcessPoolExecutor` and never recycles a worker. That pool is rebuilt when `ISOCENTER_MAX_WORKERS` changes, and never for this variable. Each `ingest()` that has files to read logs one `WARNING` saying the variable had no effect on it and that its result is unaffected, unless `ISOCENTER_FORCE_THREADS` is also set, in which case [#185](https://github.com/kvnlng/Isocenter/issues/185)'s warning, which already names `ingest()`, is the one line. An `ingest()` with nothing new to read is silent, and so is a direct `DicomImporter.import_files()` with no executor, where `run_parallel` builds the recycling pool itself and honours it.

**History.** 0.9.4 documented the `:memory:` combination as running in processes and failing with `RedactionError` naming `no such table: instance_blobs`, which it did, on every task; the refusal replaced that in [#400](https://github.com/kvnlng/Isocenter/issues/400). The `ingest()` warning is [#471](https://github.com/kvnlng/Isocenter/issues/471), in [#393](https://github.com/kvnlng/Isocenter/issues/393)'s shape. Until [#185](https://github.com/kvnlng/Isocenter/issues/185), `0` was carried into `multiprocessing.Pool`, which raised `ValueError: maxtasksperchild must be a positive int or None` without naming this variable.

## `ISOCENTER_DISABLE_GC`

**Default:** `0`.

Set to `1` to disable garbage collection in worker processes. This can speed up processing significantly but increases memory usage.

## `ISOCENTER_FORCE_THREADS`

**Default:** `0`.

Set to `1` to run in threads instead of processes. Useful for debugging `audit()`, `scan_pixel_content()` and `redact()` in the calling process.

**It reaches `audit()`, `scan_pixel_content()` and `redact()`. It reaches neither `export()` nor `ingest()`, for different reasons:**

- **`export()`** passes `maxtasksperchild=25` and therefore runs in processes on every interpreter, free-threaded builds included. That is a decision, not an oversight: recycling a worker every 25 tasks reclaims memory leaked by the imaging C libraries, and a thread pool has no process to recycle ([#185](https://github.com/kvnlng/Isocenter/issues/185)). The request is reported with a warning naming both levers.
- **`ingest()`** runs on the session's own `ProcessPoolExecutor`, handed to `run_parallel()` as `executor=`, and a caller-supplied executor is used as given. **The variable is read and ignored there, and each `ingest()` that dispatches work says so** with one `WARNING` naming the variable ([#390](https://github.com/kvnlng/Isocenter/issues/390), [#393](https://github.com/kvnlng/Isocenter/issues/393)), unless worker recycling is also set, in which case [#185](https://github.com/kvnlng/Isocenter/issues/185)'s warning, which already names `ingest()`, is the one line. An `ingest()` with nothing new to read, a free-threaded build with no lever set, and a direct `DicomImporter.import_files()` call with no executor or a thread pool are silent.

`discover_redaction_zones()` asks for threads itself, so this variable changes nothing there; it runs in processes only under `ISOCENTER_MAX_TASKS_PER_CHILD`, which outranks that request ([#458](https://github.com/kvnlng/Isocenter/issues/458)).

So `export()` and `ingest()` are the two paths you cannot debug, breakpoint or coverage-measure in the calling process. See `.coveragerc` for how the spawned workers are measured instead ([#380](https://github.com/kvnlng/Isocenter/issues/380)).

## `ISOCENTER_FORCE_PROCESSES`

**Default:** `0`.

Set to `1` to force processes instead of threads. It is the mirror of `ISOCENTER_FORCE_THREADS`, and it exists for the free-threaded default: on a free-threaded build `run_parallel()` picks threads, because there is no GIL to escape and pickling every item across a pipe is pure cost. This pins processes anyway.

**Except `redact()` on a `:memory:` store**, which runs in threads on every interpreter, this variable notwithstanding: the redaction worker writes redacted frames back to the store, and a process cannot share an in-memory database ([#381](https://github.com/kvnlng/Isocenter/issues/381)). It says so with a `WARNING` naming this variable, once per `redact()` call ([#400](https://github.com/kvnlng/Isocenter/issues/400)). The pass is correct; the variable is the one thing about it that did not apply, and it still applies to every other parallel pass in the process.

## Which levers win

The three threads-or-processes levers have a fixed order:

1. `ISOCENTER_MAX_TASKS_PER_CHILD` (or a `maxtasksperchild` argument) beats both force variables and always means processes: on 3.12, the floor, only `multiprocessing.Pool` recycles workers without deadlocking (see [`ISOCENTER_MAX_TASKS_PER_CHILD`](#isocenter_max_tasks_per_child)).
2. Then `ISOCENTER_FORCE_THREADS`, which **wins** when both force variables are set.
3. Then `ISOCENTER_FORCE_PROCESSES`.
4. Then the free-threaded default.

## `ISOCENTER_SHOW_PROGRESS`

**Default:** `1`.

Set to `0` (or `false`, `off`, `no`) to disable every progress bar the library draws: the parallel passes (ingest, audit, pixel scan, redaction and export) and the bars of `anonymize()`, `release_memory()` (which every `export()` runs) and `lock_identities()`. Useful for cleaner logs in CI/CD environments.

It can only switch a bar off: an explicit `show_progress=False`, where a call takes one, is honoured whatever this says, and `export(show_progress=False)` silences the memory-release bar too. `parallel.progress_enabled()` is the one spelling of the rule.

**History.** Until [#540](https://github.com/kvnlng/Isocenter/issues/540) the last three bars ignored the variable.

## `ISOCENTER_WORKER_FAULTHANDLER`

**Default:** `0`.

Diagnostic. Set to `1` to have every worker *process* arm `faulthandler.dump_traceback_later(240, exit=False)` at start: a worker still alive after 240 seconds dumps every one of its threads' tracebacks to stderr and keeps running. This is how a stall *inside* a pool child becomes a stack trace instead of a silent hang, because the parent's faulthandler cannot see into children. Isocenter's own CI sets it; production runs should leave it off.
