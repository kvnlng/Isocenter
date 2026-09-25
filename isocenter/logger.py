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
    # dropping a `FileHandler` without closing it leaves its file open, so
    # N sessions in one process would hold N descriptors on the log.
    # `close()` on the console `StreamHandler` flushes and leaves
    # `sys.stdout` open -- only a `FileHandler` owns its stream.
    #
    # One `try` per handler, not one around the loop: `close()` flushes,
    # and a flush that fails (a full disk) raises from it. Unguarded, it
    # would raise out of `Session()` before the reset, leaving every old
    # handler attached and the rest unclosed. The failure is logged once
    # the new handlers are in place, so it reaches the log it is about.
    close_failures = []
    for handler in list(logger.handlers):
        try:
            handler.close()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            close_failures.append((type(handler).__name__, exc))
    logger.handlers = []

    # 1. File Handler
    fh = logging.FileHandler(log_file, mode='w')  # Overwritten per session
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
    `__context__` (an exception raised while handling another, without
    `from`) is not followed.

    Every site that turns an exception into audit text, a summary reason
    or a report failure calls this. Never raises.

    Args:
        exc (BaseException): The exception to describe.

    Returns:
        str: The one-line description.
    """
    # Why the type leads: `str()` is `''` for `KeyError()`, `OSError()` and
    # most bare raises, so a reason built from the message alone can be
    # empty, and a caller testing it for truth would drop the failure; a
    # message alone also cannot tell `KeyError('x')` from the string 'x'.
    # Why the direct cause: `get_pixel_data()` wraps a loader's error in
    # `RuntimeError(...) from e`, and without the cause a sidecar `OSError`
    # would reach `PhiReport.failures` named only as a `RuntimeError`. It
    # lives here because `logger` is a leaf every caller already imports.
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
    `<Patient ID>_<series>_<instance>` for a WFDB record. An `OSError`
    with no `strerror` -- `OSError("cannot open <path>")` -- is its type
    alone.

    Every other exception keeps its message, exactly as
    `describe_exception` spells it, so an exception type that writes a
    path into its own message is not covered: name the instance at the
    raise instead.

    Args:
        exc (BaseException): The exception to describe.

    Returns:
        str: The one-line description, with no `OSError` path in it.
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
    # it gets the bare type too.
    try:
        message = str(exc)
    except Exception:  # pylint: disable=broad-exception-caught
        return name
    return f"{name}: {message}" if message.strip() else name
