"""Tests for logger_setup.rotate_log_file and get_logger."""

from __future__ import annotations

import logging
import logging.handlers
import types
from datetime import date
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from src.logger_setup import rotate_log_file, get_logger, _ROOT_LOGGER_NAME


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cfg(tmp_path: Path) -> types.SimpleNamespace:
    log_dir = tmp_path / "logs"
    log_dir.mkdir(exist_ok=True)
    return types.SimpleNamespace(
        log_dir=log_dir,
        log_filename_pattern="sender-%Y-%m-%d.log",
        log_max_bytes_per_file=10_000_000,
        log_backup_count_within_day=3,
    )


TODAY = date(2026, 5, 14)
TOMORROW = date(2026, 5, 15)


def _clean_logger_handlers():
    """Remove all handlers from the root price_sender logger."""
    logger = logging.getLogger(_ROOT_LOGGER_NAME)
    for h in list(logger.handlers):
        try:
            h.close()
        except Exception:
            pass
        logger.removeHandler(h)


# ---------------------------------------------------------------------------
# rotate_log_file — no-op when already on correct file
# ---------------------------------------------------------------------------

class TestRotateLogFileNoOp:
    def test_no_op_when_handler_already_on_correct_file(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        _clean_logger_handlers()

        # Manually attach a handler pointing at today's file
        today_name = TODAY.strftime(cfg.log_filename_pattern)
        log_path = cfg.log_dir / today_name
        fh = logging.handlers.RotatingFileHandler(str(log_path), encoding="utf-8")
        logger = logging.getLogger(_ROOT_LOGGER_NAME)
        logger.addHandler(fh)

        # Calling rotate for the same day should be a no-op
        rotate_log_file(cfg, TODAY)

        handlers_after = [h for h in logger.handlers
                          if isinstance(h, logging.handlers.RotatingFileHandler)]
        # Still the same single handler
        assert len(handlers_after) == 1
        assert Path(handlers_after[0].baseFilename).resolve() == log_path.resolve()

        _clean_logger_handlers()

    def test_rotates_when_handler_on_different_file(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        _clean_logger_handlers()

        # Attach handler for today
        today_name = TODAY.strftime(cfg.log_filename_pattern)
        log_path = cfg.log_dir / today_name
        fh = logging.handlers.RotatingFileHandler(str(log_path), encoding="utf-8")
        logger = logging.getLogger(_ROOT_LOGGER_NAME)
        logger.addHandler(fh)

        # Rotate for TOMORROW
        rotate_log_file(cfg, TOMORROW)

        handlers_after = [h for h in logger.handlers
                          if isinstance(h, logging.handlers.RotatingFileHandler)]
        assert len(handlers_after) == 1
        tomorrow_name = TOMORROW.strftime(cfg.log_filename_pattern)
        expected = cfg.log_dir / tomorrow_name
        assert Path(handlers_after[0].baseFilename).resolve() == expected.resolve()

        _clean_logger_handlers()

    def test_new_log_file_created_on_rotate(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        _clean_logger_handlers()

        today_name = TODAY.strftime(cfg.log_filename_pattern)
        log_path = cfg.log_dir / today_name
        fh = logging.handlers.RotatingFileHandler(str(log_path), encoding="utf-8")
        logger = logging.getLogger(_ROOT_LOGGER_NAME)
        logger.addHandler(fh)

        rotate_log_file(cfg, TOMORROW)

        tomorrow_name = TOMORROW.strftime(cfg.log_filename_pattern)
        tomorrow_path = cfg.log_dir / tomorrow_name
        # The new handler should have been opened (file may or may not exist yet on disk
        # depending on whether we've written to it, but the handler should point there)
        handlers_after = [h for h in logger.handlers
                          if isinstance(h, logging.handlers.RotatingFileHandler)]
        assert Path(handlers_after[0].baseFilename).resolve() == tomorrow_path.resolve()

        _clean_logger_handlers()

    def test_old_handler_removed_after_rotate(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        _clean_logger_handlers()

        today_name = TODAY.strftime(cfg.log_filename_pattern)
        log_path = cfg.log_dir / today_name
        fh = logging.handlers.RotatingFileHandler(str(log_path), encoding="utf-8")
        logger = logging.getLogger(_ROOT_LOGGER_NAME)
        logger.addHandler(fh)

        rotate_log_file(cfg, TOMORROW)

        # Old handler should be gone
        current_paths = {
            Path(h.baseFilename).resolve()
            for h in logger.handlers
            if isinstance(h, logging.handlers.RotatingFileHandler)
        }
        assert log_path.resolve() not in current_paths

        _clean_logger_handlers()

    def test_no_existing_handler_adds_new_one(self, tmp_path):
        """rotate_log_file with no existing RotatingFileHandler just adds one."""
        cfg = _make_cfg(tmp_path)
        _clean_logger_handlers()

        rotate_log_file(cfg, TODAY)

        logger = logging.getLogger(_ROOT_LOGGER_NAME)
        handlers = [h for h in logger.handlers
                    if isinstance(h, logging.handlers.RotatingFileHandler)]
        assert len(handlers) == 1

        _clean_logger_handlers()

    def test_non_file_handlers_preserved(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        _clean_logger_handlers()

        logger = logging.getLogger(_ROOT_LOGGER_NAME)
        console = logging.StreamHandler()
        logger.addHandler(console)

        today_name = TODAY.strftime(cfg.log_filename_pattern)
        log_path = cfg.log_dir / today_name
        fh = logging.handlers.RotatingFileHandler(str(log_path), encoding="utf-8")
        logger.addHandler(fh)

        rotate_log_file(cfg, TOMORROW)

        assert console in logger.handlers

        _clean_logger_handlers()
        logger.removeHandler(console)

    def test_exception_in_close_swallowed(self, tmp_path):
        """If h.close() raises, rotate_log_file must not propagate the exception."""
        cfg = _make_cfg(tmp_path)
        _clean_logger_handlers()

        today_name = TODAY.strftime(cfg.log_filename_pattern)
        log_path = cfg.log_dir / today_name
        fh = logging.handlers.RotatingFileHandler(str(log_path), encoding="utf-8")
        logger = logging.getLogger(_ROOT_LOGGER_NAME)
        logger.addHandler(fh)

        with patch.object(fh, "close", side_effect=Exception("boom")):
            rotate_log_file(cfg, TOMORROW)  # must not raise

        _clean_logger_handlers()


# ---------------------------------------------------------------------------
# get_logger
# ---------------------------------------------------------------------------

class TestGetLogger:
    def test_returns_logger_with_namespaced_name(self):
        lg = get_logger("mymodule")
        assert lg.name == f"{_ROOT_LOGGER_NAME}.mymodule"

    def test_same_name_returns_same_instance(self):
        a = get_logger("same")
        b = get_logger("same")
        assert a is b

    def test_different_names_return_different_loggers(self):
        a = get_logger("module_a")
        b = get_logger("module_b")
        assert a is not b
