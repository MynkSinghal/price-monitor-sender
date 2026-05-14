"""Tests for heartbeat.Heartbeat."""

from __future__ import annotations

import threading
import time
from unittest.mock import patch

import pytest

from src.heartbeat import Heartbeat


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

class TestHeartbeatConstruction:
    def test_thread_is_daemon(self):
        h = Heartbeat(max_silence_seconds=300, check_interval_seconds=60)
        assert h._thread.daemon is True

    def test_thread_name(self):
        h = Heartbeat(max_silence_seconds=300)
        assert h._thread.name == "heartbeat-watchdog"

    def test_initial_last_tick_recent(self):
        before = time.monotonic()
        h = Heartbeat(max_silence_seconds=300)
        after = time.monotonic()
        assert before <= h._last_tick <= after


# ---------------------------------------------------------------------------
# tick()
# ---------------------------------------------------------------------------

class TestHeartbeatTick:
    def test_tick_updates_last_tick(self):
        h = Heartbeat(max_silence_seconds=300)
        old = h._last_tick
        time.sleep(0.01)
        h.tick()
        assert h._last_tick > old

    def test_tick_thread_safe(self):
        """Multiple threads calling tick simultaneously must not corrupt state."""
        h = Heartbeat(max_silence_seconds=300)
        errors = []

        def do_tick():
            try:
                for _ in range(100):
                    h.tick()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=do_tick) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []


# ---------------------------------------------------------------------------
# stop()
# ---------------------------------------------------------------------------

class TestHeartbeatStop:
    def test_stop_signals_event(self):
        h = Heartbeat(max_silence_seconds=300)
        h.stop()
        assert h._stop.is_set()

    def test_stop_before_start_is_safe(self):
        h = Heartbeat(max_silence_seconds=300)
        h.stop()  # must not raise


# ---------------------------------------------------------------------------
# Self-kill behavior
# ---------------------------------------------------------------------------

class TestHeartbeatSelfKill:
    def test_does_not_exit_when_ticking_regularly(self):
        """Ticking within max_silence: no os._exit(99)."""
        exit_called = threading.Event()

        with patch("src.heartbeat.os._exit", side_effect=lambda c: exit_called.set()):
            h = Heartbeat(max_silence_seconds=10, check_interval_seconds=0.02)
            h.start()
            for _ in range(5):
                time.sleep(0.02)
                h.tick()
            h.stop()
            h._thread.join(timeout=0.5)

        assert not exit_called.is_set()

    def test_exits_99_when_silent_too_long(self):
        """No tick for longer than max_silence → os._exit(99) must be called."""
        exit_codes = []
        exit_event = threading.Event()

        def fake_exit(code):
            exit_codes.append(code)
            exit_event.set()

        with patch("src.heartbeat.os._exit", side_effect=fake_exit):
            h = Heartbeat(max_silence_seconds=0, check_interval_seconds=0.02)
            h.start()
            triggered = exit_event.wait(timeout=2.0)

        assert triggered, "os._exit was not called"
        assert exit_codes[0] == 99

    def test_exit_not_called_after_stop(self):
        """After stop(), the watchdog thread exits cleanly even if silence > max."""
        exit_called = threading.Event()

        with patch("src.heartbeat.os._exit", side_effect=lambda c: exit_called.set()):
            h = Heartbeat(max_silence_seconds=300, check_interval_seconds=0.02)
            h.start()
            h.stop()
            h._thread.join(timeout=0.5)

        assert not exit_called.is_set()


# ---------------------------------------------------------------------------
# start()
# ---------------------------------------------------------------------------

class TestHeartbeatStart:
    def test_thread_starts_and_is_alive(self):
        h = Heartbeat(max_silence_seconds=300, check_interval_seconds=60)
        h.start()
        assert h._thread.is_alive()
        h.stop()
        h._thread.join(timeout=0.5)
