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
