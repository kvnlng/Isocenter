"""`configure_logger()` closes the handlers it replaces (#611).

Every `Session()` calls `configure_logger()`, which reset the `isocenter`
logger's handler list to stop duplicates -- by assigning `[]` over it,
never calling `close()` on what it dropped. Each `FileHandler` so dropped
kept its file open: N sessions in one process were N open descriptors
on `isocenter.log`, which 3.14t reports as a `ResourceWarning` at GC.

Driven through `configure_logger()` itself rather than `Session()`,
which is all `Session.__init__` does with the logger, so this file
imports only `logger.py` -- `NOT_PROBED` in `scripts/mutation_probe.py`,
whose entry names this test -- and needs no `TARGETS` row.
"""
import logging
import sys

import pytest

from isocenter.logger import configure_logger

LOGGER = "isocenter"


def _file_handlers():
    return [h for h in logging.getLogger(LOGGER).handlers
            if isinstance(h, logging.FileHandler)]


def test_reconfiguring_the_logger_closes_the_old_file_handler(tmp_path,
                                                              monkeypatch):
    """The replaced handler's stream is closed; stdout's is not.

    Killer for the mutant that drops the `close()` loop: `stream.closed`
    stays False. `sys.stdout.closed` is asserted beside it because the
    console handler is a `StreamHandler` over `sys.stdout`, and a close
    that reached the stream would take the interpreter's stdout with it.
    The handler count is asserted after three configurations, one per
    session a process might open.
    """
    monkeypatch.setenv("ISOCENTER_LOG_FILE", str(tmp_path / "isocenter.log"))
    try:
        configure_logger()
        [first] = _file_handlers()
        stream = first.stream
        assert stream is not None and not stream.closed, "setup"

        configure_logger()
        configure_logger()

        assert stream.closed, (
            "the FileHandler configure_logger() replaced still holds its "
            "file open (#611)")
        assert sys.stdout.closed is False
        assert len(_file_handlers()) == 1, [
            type(h).__name__ for h in logging.getLogger(LOGGER).handlers]
        assert len(logging.getLogger(LOGGER).handlers) == 2

        # The live handler is the last one configured and still writes.
        logging.getLogger(LOGGER).warning("still logging after #611")
        [live] = _file_handlers()
        live.flush()
        assert "still logging after #611" in (
            tmp_path / "isocenter.log").read_text(encoding="utf-8")
    finally:
        # Put the suite's handler back where the environment says, not
        # in this test's tmp directory.
        monkeypatch.undo()
        configure_logger()


class _FailsToClose(logging.Handler):
    """A handler whose first `close()` raises, as a failed flush does.

    Later calls close for real, so the `finally` below that reconfigures
    the logger again, and `logging.shutdown` at exit, do not raise.
    """

    def __init__(self, exc=None):
        super().__init__()
        self.exc = exc if exc is not None else OSError(
            28, "No space left on device")
        self.raised = False

    def emit(self, record):
        pass

    def close(self):
        if not self.raised:
            self.raised = True
            raise self.exc
        super().close()


def test_a_handler_that_raises_on_close_does_not_abort_the_reset(tmp_path,
                                                                 monkeypatch):
    """One `close()` raising neither escapes nor stops the others (review of #637).

    Every `Session()` calls `configure_logger()`, so a raise here is a
    session that cannot open. The raising handler is put *first*, ahead
    of the package's own `FileHandler`: the mutant with one `try` around
    the whole loop never reaches that handler, and its stream stays
    open. The failure is not swallowed either -- it is logged, after the
    reset, into the new file.
    """
    monkeypatch.setenv("ISOCENTER_LOG_FILE", str(tmp_path / "isocenter.log"))
    logger = logging.getLogger(LOGGER)
    bad = _FailsToClose()
    try:
        configure_logger()
        [first] = _file_handlers()
        stream = first.stream
        logger.handlers.insert(0, bad)

        configure_logger()

        assert bad.raised, "setup: the raising close() was never called"
        assert stream.closed, (
            "a handler after the one whose close() raised was left open")
        assert [type(h).__name__ for h in logger.handlers] == [
            "FileHandler", "StreamHandler"]
        logger.warning("still logging after a failed close")
        [live] = _file_handlers()
        live.flush()
        text = (tmp_path / "isocenter.log").read_text(encoding="utf-8")
        assert ("A replaced _FailsToClose did not close cleanly, so lines "
                "it held may not have reached its stream: OSError: "
                "[Errno 28] No space left on device") in text, text
        assert "still logging after a failed close" in text, text
    finally:
        monkeypatch.undo()
        configure_logger()


def test_every_failed_close_is_logged_not_only_the_first(tmp_path,
                                                         monkeypatch):
    """Two handlers whose `close()` raises give two WARNING lines.

    Distinct errors, so each line is asserted by its own text rather
    than counted: the mutant that logs `close_failures[:1]` keeps the
    first and drops the second. Neither is left on the logger.
    """
    monkeypatch.setenv("ISOCENTER_LOG_FILE", str(tmp_path / "isocenter.log"))
    logger = logging.getLogger(LOGGER)
    full = _FailsToClose(OSError(28, "No space left on device"))
    broken = _FailsToClose(OSError(5, "Input/output error"))
    try:
        configure_logger()
        logger.handlers[0:0] = [full, broken]

        configure_logger()

        assert full.raised and broken.raised, "setup: a close() was not called"
        assert [type(h).__name__ for h in logger.handlers] == [
            "FileHandler", "StreamHandler"]
        [live] = _file_handlers()
        live.flush()
        text = (tmp_path / "isocenter.log").read_text(encoding="utf-8")
        for message in ("OSError: [Errno 28] No space left on device",
                        "OSError: [Errno 5] Input/output error"):
            assert ("A replaced _FailsToClose did not close cleanly, so "
                    "lines it held may not have reached its stream: "
                    + message) in text, text
    finally:
        monkeypatch.undo()
        configure_logger()


def test_an_interrupt_out_of_close_is_not_swallowed(tmp_path, monkeypatch):
    """`KeyboardInterrupt` from a `close()` propagates out of the reset.

    The guard is `except Exception`, so Ctrl+C during `Session()` still
    stops it; widened to `BaseException`, the interrupt would be logged
    as a close failure and the session would open. An interrupt leaves
    the reset where it stopped -- every replaced handler stays attached,
    those ahead of the raising one already closed, and no new handler
    is added -- which is ordinary
    interrupt semantics (review of #637), and the `finally` below
    completes the reset.
    """
    monkeypatch.setenv("ISOCENTER_LOG_FILE", str(tmp_path / "isocenter.log"))
    logger = logging.getLogger(LOGGER)
    interrupting = _FailsToClose(KeyboardInterrupt())
    try:
        configure_logger()
        logger.handlers.insert(0, interrupting)

        with pytest.raises(KeyboardInterrupt):
            configure_logger()

        assert interrupting.raised, (
            "setup: the raising close() was never called")
        assert interrupting in logger.handlers
    finally:
        monkeypatch.undo()
        configure_logger()
