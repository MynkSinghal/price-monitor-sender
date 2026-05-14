"""HTTP transmitter (Q1 / Q2 / Q3).

Design choice per Q16:
    The caller (1-minute main loop) decides when to send. We do NOT loop
    with retries inside a single cycle — a failed send is simply logged
    and the NEXT cycle (60 seconds later) will send the same full snapshot
    (payloads are deltaless per spec). This keeps the execution model
    simple and self-healing without piling retry storms onto a network
    that may be degraded.

Success criterion: HTTP 200 (Q3).
Any non-2xx or transport error is logged and treated as failure.
"""

from __future__ import annotations

from dataclasses import dataclass

import requests

from .config_loader import AppConfig
from .logger_setup import get_logger

log = get_logger("transport")


@dataclass(frozen=True)
class TransmissionResult:
    ok: bool
    status_code: int | None
    reason: str
    bytes_sent: int
    elapsed_ms: float


class Transmitter:
    def __init__(self, cfg: AppConfig) -> None:
        self._cfg = cfg
        self._session = requests.Session()
        self._headers = {
            "Content-Type": cfg.content_type,
            "X-Sender-Host": cfg.sender_hostname,
            "X-Sender-Env": cfg.sender_env,
            "X-Sender-Site": cfg.sender_site,
            **cfg.extra_headers,
        }

    def send(self, payload: str, *, cycle_id: str) -> TransmissionResult:
        payload_bytes = payload.encode("utf-8")
        headers = dict(self._headers)
        headers["X-Cycle-Id"] = cycle_id

        try:
            resp = self._session.request(
                method=self._cfg.http_method,
                url=self._cfg.receiver_url,
                data=payload_bytes,
                headers=headers,
                timeout=self._cfg.receiver_timeout_seconds,
            )
        except requests.RequestException as exc:
            log.error("Transmission failed (network) cycle=%s err=%s", cycle_id, exc)
            return TransmissionResult(
                ok=False, status_code=None, reason=str(exc),
                bytes_sent=len(payload_bytes), elapsed_ms=0.0,
            )

        ok = resp.status_code == 200
        level = log.info if ok else log.warning
        level(
            "Transmission %s | cycle=%s | status=%d | bytes=%d | %.1fms",
            "OK" if ok else "FAIL", cycle_id, resp.status_code,
            len(payload_bytes), resp.elapsed.total_seconds() * 1000,
        )
        return TransmissionResult(
            ok=ok, status_code=resp.status_code,
            reason=f"HTTP {resp.status_code}",
            bytes_sent=len(payload_bytes),
            elapsed_ms=resp.elapsed.total_seconds() * 1000,
        )

    def close(self) -> None:
        self._session.close()
