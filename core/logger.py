"""Logging setup for JARVIS.

One console handler plus one bounded rotating file handler. Repeated calls
are safe: handlers are replaced rather than duplicated.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from core.config import PROJECT_ROOT

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
MAX_BYTES = 1_000_000
BACKUP_COUNT = 3


def _resolve_dir(log_dir):
  path = Path(log_dir)
  return path if path.is_absolute() else (PROJECT_ROOT / path)


def setup_logging(level="INFO", log_dir="logs", log_file="jarvis.log"):
  """Configure root logging once and return the log file path."""
  root = logging.getLogger()
  for handler in list(root.handlers):
    root.removeHandler(handler)
    handler.close()

  root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
  formatter = logging.Formatter(LOG_FORMAT, DATE_FORMAT)

  directory = _resolve_dir(log_dir)
  directory.mkdir(parents=True, exist_ok=True)
  file_path = directory / log_file

  file_handler = RotatingFileHandler(
      file_path,
      maxBytes=MAX_BYTES,
      backupCount=BACKUP_COUNT,
      encoding="utf-8",
  )
  file_handler.setFormatter(formatter)

  console_handler = logging.StreamHandler()
  console_handler.setFormatter(formatter)

  root.addHandler(console_handler)
  root.addHandler(file_handler)

  # Third-party request logs are noise at INFO; keep warnings and errors.
  for noisy in ("httpx", "httpcore"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
  return file_path


def get_logger(name):
  return logging.getLogger(name)
