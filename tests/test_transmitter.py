"""Tests for transmitter.Transmitter."""

from __future__ import annotations

import types
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
import requests

from src.transmitter import Transmitter, TransmissionResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cfg(**overrides) -> types.SimpleNamespace:
    base = dict(
        receiver_url="http://localhost:8080/prices",
        receiver_timeout_seconds=10,
        http_method="POST",
        content_type="text/csv",
        sender_hostname="testhost",
        sender_env="TEST",
        sender_site="UKPROD",
        extra_headers={"X-Sender-Source": "price_monitor"},
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


def _mock_response(status_code=200, elapsed_seconds=0.05) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.elapsed = timedelta(seconds=elapsed_seconds)
    return resp


def _make_tx(**cfg_overrides) -> Transmitter:
    return Transmitter(_make_cfg(**cfg_overrides))


# ---------------------------------------------------------------------------
# Success (HTTP 200)
# ---------------------------------------------------------------------------

class TestTransmitterSuccess:
    def test_returns_ok_true_on_200(self):
        tx = _make_tx()
        with patch.object(tx._session, "request", return_value=_mock_response(200)):
            result = tx.send("payload", cycle_id="abc123")
        assert result.ok is True

    def test_status_code_200(self):
        tx = _make_tx()
        with patch.object(tx._session, "request", return_value=_mock_response(200)):
            result = tx.send("payload", cycle_id="abc123")
        assert result.status_code == 200

    def test_reason_contains_200(self):
        tx = _make_tx()
        with patch.object(tx._session, "request", return_value=_mock_response(200)):
            result = tx.send("payload", cycle_id="abc123")
        assert "200" in result.reason

    def test_bytes_sent_matches_payload_length(self):
        tx = _make_tx()
        payload = "col1|col2\nval1|val2\n"
        with patch.object(tx._session, "request", return_value=_mock_response(200)):
            result = tx.send(payload, cycle_id="x")
        assert result.bytes_sent == len(payload.encode("utf-8"))

    def test_elapsed_ms_from_response(self):
        tx = _make_tx()
        with patch.object(tx._session, "request", return_value=_mock_response(200, elapsed_seconds=0.123)):
            result = tx.send("p", cycle_id="x")
        assert abs(result.elapsed_ms - 123.0) < 1.0

    def test_headers_include_cycle_id(self):
        tx = _make_tx()
        captured = {}

        def capture(**kwargs):
            captured.update(kwargs.get("headers", {}))
            return _mock_response(200)

        with patch.object(tx._session, "request", side_effect=capture):
            tx.send("p", cycle_id="cycle-xyz")

        assert captured.get("X-Cycle-Id") == "cycle-xyz"

    def test_headers_include_sender_site(self):
        tx = _make_tx()
        captured = {}

        def capture(**kwargs):
            captured.update(kwargs.get("headers", {}))
            return _mock_response(200)

        with patch.object(tx._session, "request", side_effect=capture):
            tx.send("p", cycle_id="c")

        assert captured.get("X-Sender-Site") == "UKPROD"

    def test_post_method_used(self):
        tx = _make_tx()
        calls = []

        def capture(**kwargs):
            calls.append(kwargs.get("method"))
            return _mock_response(200)

        with patch.object(tx._session, "request", side_effect=capture):
            tx.send("p", cycle_id="c")

        assert calls[0] == "POST"


# ---------------------------------------------------------------------------
# Non-200 HTTP responses
# ---------------------------------------------------------------------------

class TestTransmitterNon200:
    @pytest.mark.parametrize("status", [201, 204, 400, 401, 403, 404, 500, 502, 503])
    def test_non_200_returns_ok_false(self, status):
        tx = _make_tx()
        with patch.object(tx._session, "request", return_value=_mock_response(status)):
            result = tx.send("payload", cycle_id="c")
        assert result.ok is False

    def test_non_200_status_code_preserved(self):
        tx = _make_tx()
        with patch.object(tx._session, "request", return_value=_mock_response(503)):
            result = tx.send("payload", cycle_id="c")
        assert result.status_code == 503

    def test_non_200_bytes_still_counted(self):
        tx = _make_tx()
        payload = "x" * 100
        with patch.object(tx._session, "request", return_value=_mock_response(500)):
            result = tx.send(payload, cycle_id="c")
        assert result.bytes_sent == 100


# ---------------------------------------------------------------------------
# Network errors
# ---------------------------------------------------------------------------

class TestTransmitterNetworkError:
    def test_connection_error_returns_ok_false(self):
        tx = _make_tx()
        with patch.object(tx._session, "request", side_effect=requests.ConnectionError("refused")):
            result = tx.send("payload", cycle_id="c")
        assert result.ok is False

    def test_timeout_returns_ok_false(self):
        tx = _make_tx()
        with patch.object(tx._session, "request", side_effect=requests.Timeout("timed out")):
            result = tx.send("payload", cycle_id="c")
        assert result.ok is False

    def test_ssl_error_returns_ok_false(self):
        tx = _make_tx()
        with patch.object(tx._session, "request", side_effect=requests.exceptions.SSLError("bad cert")):
            result = tx.send("payload", cycle_id="c")
        assert result.ok is False

    def test_network_error_status_code_is_none(self):
        tx = _make_tx()
        with patch.object(tx._session, "request", side_effect=requests.ConnectionError("no route")):
            result = tx.send("payload", cycle_id="c")
        assert result.status_code is None

    def test_network_error_elapsed_ms_is_zero(self):
        tx = _make_tx()
        with patch.object(tx._session, "request", side_effect=requests.ConnectionError("no route")):
            result = tx.send("payload", cycle_id="c")
        assert result.elapsed_ms == 0.0

    def test_network_error_bytes_still_counted(self):
        tx = _make_tx()
        payload = "a" * 50
        with patch.object(tx._session, "request", side_effect=requests.ConnectionError()):
            result = tx.send(payload, cycle_id="c")
        assert result.bytes_sent == 50

    def test_network_error_reason_contains_exception_text(self):
        tx = _make_tx()
        with patch.object(tx._session, "request", side_effect=requests.ConnectionError("connection refused")):
            result = tx.send("p", cycle_id="c")
        assert "connection refused" in result.reason.lower()


# ---------------------------------------------------------------------------
# Empty payload
# ---------------------------------------------------------------------------

class TestTransmitterEdgeCases:
    def test_empty_payload_sends_zero_bytes(self):
        tx = _make_tx()
        with patch.object(tx._session, "request", return_value=_mock_response(200)):
            result = tx.send("", cycle_id="c")
        assert result.bytes_sent == 0

    def test_unicode_payload_encoded_utf8(self):
        tx = _make_tx()
        payload = "héllo wörld"
        with patch.object(tx._session, "request", return_value=_mock_response(200)) as mock_req:
            tx.send(payload, cycle_id="c")
        sent_data = mock_req.call_args.kwargs.get("data") or mock_req.call_args[1].get("data")
        assert sent_data == payload.encode("utf-8")

    def test_large_payload(self):
        tx = _make_tx()
        payload = "row|data\n" * 1000
        with patch.object(tx._session, "request", return_value=_mock_response(200)):
            result = tx.send(payload, cycle_id="big")
        assert result.ok is True
        assert result.bytes_sent == len(payload.encode("utf-8"))


# ---------------------------------------------------------------------------
# close()
# ---------------------------------------------------------------------------

class TestTransmitterClose:
    def test_close_calls_session_close(self):
        tx = _make_tx()
        with patch.object(tx._session, "close") as mock_close:
            tx.close()
        mock_close.assert_called_once()

    def test_close_idempotent(self):
        """Calling close twice should not raise."""
        tx = _make_tx()
        tx.close()
        tx.close()
