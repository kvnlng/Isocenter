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

    # Reset handlers to prevent duplicates on reload
    if logger.handlers:
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

    return logger


def get_logger():
    """
    Retrieves the configured 'isocenter' logger.

    Returns:
        logging.Logger: The isocenter logger.
    """
    return logging.getLogger("isocenter")
