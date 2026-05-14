"""Integration tests for the SENDER_SITE / cross_site_jobs plumbing."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.config_loader import load_config


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _baseline_env(tmp_path: Path) -> dict[str, str]:
    return {
        "RECEIVER_URL": "http://127.0.0.1:9999/test",
        "PRICES_ROOT": str(tmp_path / "prices"),
        "SENDER_HOSTNAME": "test-host",
        "SENDER_ENV": "TEST",
        "STATE_DIR": str(tmp_path / "state"),
        "BACKUP_DIR": str(tmp_path / "backups"),
        "LOG_DIR": str(tmp_path / "logs"),
    }


def test_invalid_sender_site_is_rejected(monkeypatch, tmp_path: Path) -> None:
    for k, v in _baseline_env(tmp_path).items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("SENDER_SITE", "FRANKFURT")
    with pytest.raises(ValueError, match="SENDER_SITE"):
        load_config()


def test_missing_sender_site_is_rejected(monkeypatch, tmp_path: Path) -> None:
    for k, v in _baseline_env(tmp_path).items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("SENDER_SITE", "")
    with pytest.raises(ValueError, match="SENDER_SITE"):
        load_config()


def test_ukprod_treats_usprod_jobs_as_cross_site(monkeypatch, tmp_path: Path) -> None:
    for k, v in _baseline_env(tmp_path).items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("SENDER_SITE", "UKPROD")
    cfg = load_config()
    assert cfg.is_ukprod is True
    assert cfg.is_usprod is False
    assert cfg.usprod_jobs, "config/config.json must list usprod_jobs"
    cross = cfg.cross_site_jobs
    for job in cfg.usprod_jobs:
        assert job in cross
    # at least one UK-side job must NOT be in the cross-site set
    sample_uk = next(iter({j for pg in cfg.active_price_groups for j in pg.jobs} - cross))
    assert sample_uk


def test_usprod_treats_everything_else_as_cross_site(monkeypatch, tmp_path: Path) -> None:
    for k, v in _baseline_env(tmp_path).items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("SENDER_SITE", "USPROD")
    cfg = load_config()
    assert cfg.is_usprod is True
    cross = cfg.cross_site_jobs
    for job in cfg.usprod_jobs:
        assert job not in cross
    assert "NSE_IX_1" in cross  # canonical UK-side job
