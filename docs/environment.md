# Environment Variables

These variables tune logging, the session store and parallelism. Set them in your shell or in your process's environment before you import `isocenter`. `isocenter` does not read a `.env` file; to use one, call `dotenv.load_dotenv()` yourself before importing `isocenter`.

Flags whose default is `0` are on only when set to exactly `1`; `true`, `yes` and `on` are ignored, without a warning.

Each variable's name, default and documented behaviour are part of the 1.0 freeze ([API stability](api/stability.md#frozen-at-10)). The table is the index; each variable has its own section below, with its default, what it does, what it does not reach, and its floor.

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

`:memory:` is accepted, here or as `Session(":memory:")`:

- the index lives in memory, and the pixel sidecar in a temporary file the store removes on `close()`;
- `redact()` runs in threads on every interpreter, or refuses if worker recycling is asked for (see [`ISOCENTER_FORCE_PROCESSES`](#isocenter_force_processes) and [`ISOCENTER_MAX_TASKS_PER_CHILD`](#isocenter_max_tasks_per_child)).

## `ISOCENTER_MAX_WORKERS`

**Default:** one worker per CPU (`os.cpu_count()`, or `1` when it cannot answer), except `redact()`, below.

**Floor:** 1. A value **below 1**, `0` or any negative, is not a worker count: it is reported with a warning naming the variable and the value, and the default is used instead. `1` is how you ask for a single worker.

How many workers a parallel run uses, **processes or threads**, whichever the [three levers](#which-levers-win) resolve to. The same number sizes all three pools:

- **The pool of `audit()`, `scan_pixel_content()` and `export()`.** Its default is one per CPU.
- **The session's shared pool**, the process pool that `ingest()` runs on. It has the same default.
    - The variable is read at `Session()` **and again at the start of every `ingest()`**, which rebuilds the pool when the width has changed. A value set after the session opens reaches the next `ingest()`. An unchanged width rebuilds nothing, so an ordinary session keeps one pool for its lifetime.
    - The one exception is a **second `ingest()` running on another thread of the same session**. The pool is shared between them, and shutting it down would cancel the queued files of the call already running, so the resize is skipped and one `WARNING` names this variable, the pool's width and the width asked for. The next `ingest()` that starts with no other in flight is built at the new width.
    - It uses processes on every build, free-threaded ones included.
    - **When a worker process of that pool ends during an `ingest()`** (the out-of-memory killer, say), the files not yet returned are read again on fresh process pools: one worker while the file that ends it is found, then this many. See [Architecture](architecture.md#5-when-a-worker-process-dies).
- **`redact()`'s pool.** Its default is **half the CPUs, capped at eight, never below one**: `max(1, min(cpu_count // 2, 8))`. Each redaction worker holds a decoded frame, so that number is a memory ceiling, not a throughput choice. `redact()` reads this variable too, and rejects a value below 1 the same way, falling back to its own default.

This variable overrides both defaults, with the same floor. The below-1 warning also appears once when `Session()` opens, because building the shared pool reads the variable, and once per `ingest()`, because each one re-resolves it.

## `ISOCENTER_CHUNKSIZE`

**Default:** `1`.

**Floor:** 1. A value **below 1**, `0` or any negative, is not a batch size: it is reported with a warning naming the variable and the value, and the default of `1` is used instead. `ISOCENTER_MAX_WORKERS` and `ISOCENTER_MAX_TASKS_PER_CHILD` have the same floor.

The batch size for inter-process communication. Increasing it (to 5 or 10, say) can improve performance for very small items.

**The retry rounds `ingest()` runs after a worker process ends ignore this variable and send one file per task.** A chunk is one task, so a death anywhere in a chunk loses every result in it, and the file that ended the worker cannot be told from the others in its chunk. The first round of each call uses the value set here, as every other parallel step does.

## `ISOCENTER_MAX_TASKS_PER_CHILD`

**Default:** *Unlimited* (workers are not recycled).

**Floor:** 1. A value **below 1**, `0` or any negative, is not a recycling interval: it is reported with a warning naming the variable and the value, and no recycling is used.

Restart worker processes after N tasks to release memory. Useful if you suspect memory leaks in underlying libraries.

- **Setting it at all turns threads off** for every call site it reaches: only a process can be recycled. See [Which levers win](#which-levers-win).
- **`redact()` on a `:memory:` store refuses it.** That store's redaction asks for threads, and this variable would override that request. Instead `redact()` raises `RuntimeError` naming the store, this variable, why processes cannot work for an in-memory database, and two remedies: unset it, or use a file-backed store. It raises before any work starts, so nothing has happened when it arrives. `except RedactionError` does not catch the refusal; `except RuntimeError` does.
- **It cannot reach `session.export()` in either direction.** An export always recycles each worker after 25 tasks, so neither raising nor lowering this changes what an export does.
- **Nor does it reach `ingest()`**, which runs on the session's own process pool and never recycles a worker. That pool is rebuilt when `ISOCENTER_MAX_WORKERS` changes, and never for this variable. Each `ingest()` that has files to read logs one `WARNING` saying the variable had no effect on it and that its result is unaffected (one line, naming both variables, when `ISOCENTER_FORCE_THREADS` is also set). An `ingest()` with nothing new to read is silent.

## `ISOCENTER_DISABLE_GC`

**Default:** `0`.

Set to `1` to disable garbage collection in worker processes. This can speed up processing significantly but increases memory usage.

## `ISOCENTER_FORCE_THREADS`

**Default:** `0`.

Set to `1` to run in threads instead of processes. Useful for debugging `audit()`, `scan_pixel_content()` and `redact()` in the calling process.

**It reaches `audit()`, `scan_pixel_content()` and `redact()`. It reaches neither `export()` nor `ingest()`, for different reasons:**

- **`export()`** recycles each worker after 25 tasks and therefore runs in processes on every interpreter, free-threaded builds included. Recycling reclaims memory leaked by the imaging C libraries, and a thread pool has no process to recycle. The ignored request is reported with a `WARNING`.
- **`ingest()`** runs on the session's own process pool. **The variable is ignored there, and each `ingest()` that dispatches work says so** with one `WARNING` naming the variable (one line, naming both variables, when worker recycling is also set). An `ingest()` with nothing new to read is silent.

`discover_redaction_zones()` asks for threads itself, so this variable changes nothing there; it runs in processes only under `ISOCENTER_MAX_TASKS_PER_CHILD`, which outranks that request.

So `export()` and `ingest()` are the two paths you cannot step through with a debugger in the calling process.

## `ISOCENTER_FORCE_PROCESSES`

**Default:** `0`.

Set to `1` to force processes instead of threads. It is the mirror of `ISOCENTER_FORCE_THREADS`, and it exists for the free-threaded default: on a free-threaded build (Python 3.14t) a parallel pass picks threads, because there is no GIL to escape and pickling every item across a pipe is pure cost. This pins processes anyway.

**Except `redact()` on a `:memory:` store**, which runs in threads on every interpreter, this variable notwithstanding: the redaction worker writes redacted frames back to the store, and a process cannot share an in-memory database. It says so with a `WARNING` naming this variable, once per `redact()` call. The pass is correct; the variable is the one thing about it that did not apply, and it still applies to every other parallel pass in the process.

## Which levers win

The three threads-or-processes levers have a fixed order:

1. `ISOCENTER_MAX_TASKS_PER_CHILD` beats both force variables and always means processes (see [`ISOCENTER_MAX_TASKS_PER_CHILD`](#isocenter_max_tasks_per_child)).
2. Then `ISOCENTER_FORCE_THREADS`, which **wins** when both force variables are set.
3. Then `ISOCENTER_FORCE_PROCESSES`.
4. Then the free-threaded default.

## `ISOCENTER_SHOW_PROGRESS`

**Default:** `1`.

Set to `0` (or `false`, `off`, `no`) to disable every progress bar the library draws: the parallel passes (ingest, audit, pixel scan, redaction and export) and the bars of `anonymize()`, `release_memory()` (which every `export()` runs) and `lock_identities()`. Useful for cleaner logs in CI/CD environments.

It can only switch a bar off: an explicit `show_progress=False`, where a call takes one, is honoured whatever this says, and `export(show_progress=False)` silences the memory-release bar too.

## `ISOCENTER_WORKER_FAULTHANDLER`

**Default:** `0`.

Diagnostic. Set to `1` to have every worker *process* arm `faulthandler.dump_traceback_later(240, exit=False)` at start: a worker still alive after 240 seconds dumps every one of its threads' tracebacks to stderr and keeps running. This is how a stall *inside* a pool child becomes a stack trace instead of a silent hang, because the parent's faulthandler cannot see into children. Leave it off in production runs.
