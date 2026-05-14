"""Tests for main.py — entry point and Sender lifecycle."""

from __future__ import annotations

import os
import signal
import sys
import types
from datetime import date
from pathlib import Path
from threading import Event
from unittest.mock import MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# main() function
# ---------------------------------------------------------------------------

class TestMainFunction:
    def _patched_main(self, receiver_url="http://localhost:8080/prices", sender_exit=0):
        """Run main() with all heavy deps mocked. Returns exit code."""
        from src.main import main

        mock_cfg = MagicMock()
        mock_cfg.receiver_url = receiver_url
        mock_cfg.log_level = "INFO"

        mock_sender = MagicMock()
        mock_sender.start.return_value = sender_exit

        with patch("src.main.load_config", return_value=mock_cfg):
            with patch("src.main.setup_logging"):
                with patch("src.main.Sender", return_value=mock_sender):
                    return main([])

    def test_returns_2_when_receiver_url_missing(self):
        from src.main import main
        mock_cfg = MagicMock()
        mock_cfg.receiver_url = None
        with patch("src.main.load_config", return_value=mock_cfg):
            with patch("src.main.setup_logging"):
                code = main([])
        assert code == 2

    def test_returns_2_when_receiver_url_is_placeholder(self):
        from src.main import main
        mock_cfg = MagicMock()
        mock_cfg.receiver_url = "http://CHANGE_ME:8080/prices"
        with patch("src.main.load_config", return_value=mock_cfg):
            with patch("src.main.setup_logging"):
                code = main([])
        assert code == 2

    def test_returns_0_on_clean_sender_start(self):
        code = self._patched_main(receiver_url="http://real.server/prices", sender_exit=0)
        assert code == 0

    def test_returns_1_on_sender_error(self):
        code = self._patched_main(receiver_url="http://real.server/prices", sender_exit=1)
        assert code == 1

    def test_sender_constructed_when_url_valid(self):
        from src.main import main
        mock_cfg = MagicMock()
        mock_cfg.receiver_url = "http://real.server/prices"
        mock_sender = MagicMock()
        mock_sender.start.return_value = 0

        with patch("src.main.load_config", return_value=mock_cfg):
            with patch("src.main.setup_logging"):
                with patch("src.main.Sender", return_value=mock_sender) as MockSender:
                    main([])
        MockSender.assert_called_once_with(mock_cfg)


# ---------------------------------------------------------------------------
# Sender._install_signal_handlers
# ---------------------------------------------------------------------------

class TestSignalHandlers:
    def _make_sender(self):
        from src.main import Sender
        mock_cfg = MagicMock()
        mock_cfg.sender_site = "UKPROD"
        mock_cfg.sender_env = "TEST"
        mock_cfg.sender_hostname = "testhost"
        mock_cfg.send_mode = "every_minute"
        mock_cfg.active_price_groups = []
        mock_cfg.cross_site_jobs = set()
        mock_cfg.heartbeat_max_silence_seconds = 300
        mock_cfg.timezone_business = "Europe/London"
        mock_cfg.state_dir = MagicMock()
        mock_cfg.state_dir.__truediv__ = lambda s, o: Path("/tmp") / o
        mock_cfg.config_dir = Path("/tmp")
        mock_cfg.first_detection_locked = True

        with patch("src.main.StateManager"):
            with patch("src.main.BackupManager"):
                with patch("src.main.CsvBuilder"):
                    with patch("src.main.Transmitter"):
                        with patch("src.main.Scheduler"):
                            with patch("src.main.WatchdogMonitor"):
                                with patch("src.main.Heartbeat"):
                                    with patch("src.main.ConfigWatcher"):
                                        return Sender(mock_cfg)

    def test_signal_handler_sets_stop_event(self):
        sender = self._make_sender()
        sender._install_signal_handlers()
        # Simulate SIGINT
        sender._stop.clear()
        # directly invoke the handler as the OS would
        signal.raise_signal(signal.SIGINT)
        assert sender._stop.is_set()

    def test_signal_handler_safe_in_non_main_thread(self):
        """ValueError from non-main thread must be swallowed."""
        sender = self._make_sender()
        with patch("src.main.signal.signal", side_effect=ValueError("not main")):
            sender._install_signal_handlers()  # must not raise


# ---------------------------------------------------------------------------
# Sender._log_site_summary
# ---------------------------------------------------------------------------

class TestLogSiteSummary:
    def test_no_cross_site_returns_early(self):
        from src.main import Sender
        mock_cfg = MagicMock()
        mock_cfg.cross_site_jobs = set()
        mock_cfg.active_price_groups = []
        mock_cfg.heartbeat_max_silence_seconds = 300
        mock_cfg.timezone_business = "Europe/London"
        mock_cfg.first_detection_locked = True
        mock_cfg.state_dir = MagicMock()
        mock_cfg.state_dir.__truediv__ = lambda s, o: Path("/tmp") / o
        mock_cfg.config_dir = Path("/tmp")

        with patch("src.main.StateManager"):
            with patch("src.main.BackupManager"):
                with patch("src.main.CsvBuilder"):
                    with patch("src.main.Transmitter"):
                        with patch("src.main.Scheduler"):
                            with patch("src.main.WatchdogMonitor"):
                                with patch("src.main.Heartbeat"):
                                    with patch("src.main.ConfigWatcher"):
                                        sender = Sender(mock_cfg)
        # Should just return without logging
        sender._log_site_summary()  # must not raise


# ---------------------------------------------------------------------------
# Sender._shutdown
# ---------------------------------------------------------------------------

class TestShutdown:
    def test_shutdown_stops_all_components(self):
        from src.main import Sender
        mock_cfg = MagicMock()
        mock_cfg.cross_site_jobs = set()
        mock_cfg.active_price_groups = []
        mock_cfg.heartbeat_max_silence_seconds = 300
        mock_cfg.timezone_business = "Europe/London"
        mock_cfg.first_detection_locked = True
        mock_cfg.state_dir = MagicMock()
        mock_cfg.state_dir.__truediv__ = lambda s, o: Path("/tmp") / o
        mock_cfg.config_dir = Path("/tmp")

        mock_heart = MagicMock()
        mock_monitor = MagicMock()
        mock_tx = MagicMock()
        mock_cwatcher = MagicMock()

        with patch("src.main.StateManager"):
            with patch("src.main.BackupManager"):
                with patch("src.main.CsvBuilder"):
                    with patch("src.main.Transmitter", return_value=mock_tx):
                        with patch("src.main.Scheduler"):
                            with patch("src.main.WatchdogMonitor", return_value=mock_monitor):
                                with patch("src.main.Heartbeat", return_value=mock_heart):
                                    with patch("src.main.ConfigWatcher", return_value=mock_cwatcher):
                                        sender = Sender(mock_cfg)

        sender._shutdown()

        mock_cwatcher.stop.assert_called_once()
        mock_heart.stop.assert_called_once()
        mock_monitor.stop.assert_called_once()
        mock_tx.close.assert_called_once()
