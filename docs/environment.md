
# Environment Variables

You can tune the system performance and behavior using environment variables. These can be set in your shell or in a `.env` file in the project root.

Every flag below whose default is `0` is read by `_env_is`, which compares against the literal string `1`. `true`, `yes` and `on` do nothing at all — they are not rejected, they are simply not the value being looked for.

| Variable | Default | Description |
| :--- | :--- | :--- |
| **`ISOCENTER_LOG_LEVEL`** | `DEBUG` | Logging verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`). |
| **`ISOCENTER_LOG_FILE`** | `isocenter.log` | Path the file handler writes to (DEBUG and above; the console handler is WARNING and above and is unaffected). An explicit `log_file` argument to `configure_logger()` wins over it. |
| **`ISOCENTER_DB_PATH`** | `isocenter.db` | Path to the SQLite session database. |
| **`ISOCENTER_MAX_WORKERS`** | *Auto* | Override the number of parallel worker processes. Default is `CPU_COUNT * 1.5`. |
| **`ISOCENTER_CHUNKSIZE`** | `1` | Batch size for inter-process communication. Increasing this (e.g. to 5 or 10) can improve performance for very small items. |
| **`ISOCENTER_MAX_TASKS_PER_CHILD`** | *Unlimited* | Restart worker processes after N tasks to release memory. Useful if you suspect memory leaks in underlying libraries. |
| **`ISOCENTER_DISABLE_GC`** | `0` | Set to `1` to disable Garbage Collection in worker processes. This can speed up processing significantly but increases memory usage. |
| **`ISOCENTER_FORCE_THREADS`** | `0` | Set to `1` to force using Threads instead of Processes (bypass multiprocessing). Useful for debugging or when running in environments that don't support `fork`. |
| **`ISOCENTER_FORCE_PROCESSES`** | `0` | Set to `1` to force using Processes instead of Threads. The mirror of the row above, and it exists for the free-threaded default: on a free-threaded build `run_parallel()` picks threads, because there is no GIL to escape and pickling every item across a pipe is pure cost. This pins processes anyway. **The three levers have a fixed order.** `ISOCENTER_MAX_TASKS_PER_CHILD` (or a `maxtasksperchild` argument) beats both and always means processes — only `multiprocessing.Pool` implements worker recycling. Then `ISOCENTER_FORCE_THREADS`, which **wins** when both force variables are set. Then this one. Then the free-threaded default. |
| **`ISOCENTER_SHOW_PROGRESS`** | `1` | Set to `0` to globally disable all progress bars (tqdm). Useful for cleaner logs in CI/CD environments. |
| **`ISOCENTER_WORKER_FAULTHANDLER`** | `0` | Diagnostic. Set to `1` to have every worker *process* arm `faulthandler.dump_traceback_later(240, exit=False)` at start: a worker still alive after 240 seconds dumps every one of its threads' tracebacks to stderr and keeps running. This is how a stall *inside* a pool child becomes a stack trace instead of a silent hang — the parent's faulthandler cannot see into children. Isocenter's own CI sets it; production runs should leave it off. |
