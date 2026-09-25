"""
Logging configuration and helpers for the Isocenter application.
"""
import logging
import sys

import os


def configure_logger(log_file=None):
    """
    Configures the root logger for the application.

    Sets up two handlers:
    1. File Handler: Captures all DEBUG+ logs.
    2. Console Handler: Captures WARNING+ logs only (to keep CLI output/tqdm clean).

    Args:
        log_file (str, optional): Path to the log file. Defaults to env `ISOCENTER_LOG_FILE` or `isocenter.log`.

    Returns:
        logging.Logger: The configured logger instance.
    """
    if log_file is None:
        log_file = os.getenv("ISOCENTER_LOG_FILE", "isocenter.log")

    logger = logging.getLogger("isocenter")

    # helper for default level
    log_level_map = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL
    }
    default_level = log_level_map.get(os.getenv("ISOCENTER_LOG_LEVEL", "DEBUG").upper(), logging.DEBUG)

    logger.setLevel(default_level)

    # Reset handlers to prevent duplicates on reload. Close them first:
    # assigning `[]` over the list dropped each `FileHandler` with its
    # file still open, so N sessions in one process held N descriptors
    # on the log, a `ResourceWarning` at GC on 3.14t (#611). `close()`
    # on the console `StreamHandler` flushes and leaves `sys.stdout`
    # open -- only a `FileHandler` owns its stream.
    #
    # One `try` per handler, not one around the loop: `close()` flushes,
    # and a flush that fails (a full disk) raises from it. Unguarded,
    # that raised out of `Session()` before the reset, leaving every old
    # handler attached and the rest unclosed -- where before #611 nothing
    # here could raise at all. The failure is logged once the new
    # handlers are in place, so it reaches the log it is about.
    close_failures = []
    for handler in list(logger.handlers):
        try:
            handler.close()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            close_failures.append((type(handler).__name__, exc))
    logger.handlers = []

    # 1. File Handler
    fh = logging.FileHandler(log_file, mode='w')  # Overwrite mode for now per session
    fh.setLevel(default_level)
    file_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    fh.setFormatter(file_formatter)
    logger.addHandler(fh)

    # 2. Console Handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.WARNING)  # Keep console clean for tqdm
    console_formatter = logging.Formatter('%(levelname)s: %(message)s')
    ch.setFormatter(console_formatter)
    logger.addHandler(ch)

    for handler_type, exc in close_failures:
        logger.warning("A replaced %s did not close cleanly, so lines it "
                       "held may not have reached its stream: %s",
                       handler_type, describe_exception(exc))

    return logger


def get_logger():
    """
    Retrieves the configured 'isocenter' logger.

    Returns:
        logging.Logger: The isocenter logger.
    """
    return logging.getLogger("isocenter")


def describe_exception(exc: BaseException) -> str:
    """How an exception is spelled wherever it becomes a recorded reason.

    `Type: message`, or `Type` alone when the message is empty, blank,
    or cannot be rendered at all (its `__str__` raises), and the direct
    cause (`raise ... from`) in the same spelling:
    `RuntimeError: Pixel Loader failed ... (caused by OSError: EIO)`.

    **Why the type leads.** `str()` is `''` for `KeyError()`,
    `StopIteration()`, `OSError()`, `AssertionError()` and most bare
    raises, so a reason built from the message alone can be empty, and a
    caller testing it for truth would drop the failure. A message alone
    also cannot tell `KeyError('x')` from the string `'x'`.

    **Why the cause, and only the direct one.** `get_pixel_data()` wraps
    a loader's error in `RuntimeError("Pixel Loader failed ...") from e`,
    so without the cause a sidecar `OSError` would reach
    `PhiReport.failures` named only as a `RuntimeError`. `__context__` --
    an exception raised while handling another, without `from` -- is not
    followed: the raiser did not say the two were one failure.

    **One spelling.** Every site that turns an exception into audit text,
    a summary reason or a report failure calls this. It lives here
    because `logger` is a leaf every one of those modules already
    imports. Never raises: an exception whose `__str__` raises is
    spelled by its type.
    """
    text = _type_and_message(exc)
    cause = exc.__cause__
    if cause is not None:
        text += f" (caused by {_type_and_message(cause)})"
    return text


def describe_exception_without_paths(exc: BaseException) -> str:
    """`describe_exception`, for text that must not carry a filesystem path.

    An `OSError` -- and an `OSError` cause -- is spelled by its type and
    its `strerror` alone (`NotADirectoryError: Not a directory`), because
    its `str()` appends `filename` and `filename2`, and an export path is
    built from the graph: `Subject_<Patient ID>/...` for a DICOM file,
    `<Patient ID>_<series>_<instance>` for a WFDB record. The WFDB and
    DICOM export `ERROR` rows and the DICOM export worker's stderr use
    this. An `OSError` with no `strerror` -- `OSError("cannot open
    <path>")` -- is its type alone: its message is whatever the raiser
    wrote, and the one exception this exists for is the one whose
    message is built around a path.

    **The limit.** Every other exception keeps its message, exactly as
    `describe_exception` spells it: those messages are the reasons a
    report exists to show, and there is no general way to tell a path in
    one from prose. An exception type that writes a path into its own
    message is not caught by this, so the fix belongs at the raise:
    name the instance there, as the export worker's refusals
    (`io_handlers._export_instance_worker`), `get_pixel_data()`'s load
    and decompress failures, and `_verify_readback`'s inner exception do.
    """
    text = _type_and_reason(exc)
    cause = exc.__cause__
    if cause is not None:
        text += f" (caused by {_type_and_reason(cause)})"
    return text


def _type_and_reason(exc: BaseException) -> str:
    """`_type_and_message`, with an `OSError` spelled by `strerror`."""
    if not isinstance(exc, OSError):
        return _type_and_message(exc)
    name = type(exc).__name__
    reason = exc.strerror
    if isinstance(reason, str) and reason.strip():
        return f"{name}: {reason}"
    return name


def _type_and_message(exc: BaseException) -> str:
    name = type(exc).__name__
    # `str()` runs the exception's own `__str__`, which can raise. This
    # is called while a failure is being recorded, and raising here would
    # replace that failure with this one; the type is still a reason. A
    # whitespace-only message is no more a reason than an empty one, so
    # it gets the bare type too (review of #466).
    try:
        message = str(exc)
    except Exception:  # pylint: disable=broad-exception-caught
        return name
    return f"{name}: {message}" if message.strip() else name
