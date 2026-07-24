"""
Logging module
"""

import logging
import sys
from datetime import datetime

from .config import LOG_DIR, LOG_LEVEL, LOG_FORMAT


def get_logger(name: str, to_file: bool = True) -> logging.Logger:
    """Create a logger with console and file output"""
    logger = logging.getLogger(name)
    logger.setLevel(LOG_LEVEL)

    if logger.handlers:
        return logger

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(LOG_LEVEL)
    console_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logger.addHandler(console_handler)

    if to_file:
        log_file = LOG_DIR / f"{datetime.now():%Y-%m-%d}.log"
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(LOG_LEVEL)
        file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logger.addHandler(file_handler)

    return logger
