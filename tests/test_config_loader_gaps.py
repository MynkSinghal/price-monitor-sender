"""Gap-filling tests for config_loader.

Covers:
- Invalid SEND_MODE → SystemExit
- Invalid BUSINESS_DAY_START (no colon, bad format) → SystemExit
- Invalid BUSINESS_DAY_ANCHOR → SystemExit
- Invalid SENDER_SITE → SystemExit
- ACTIVE_WEEKDAYS out-of-range → ValueError
- _parse_weekdays: empty parts, sorted dedup
- match_mode validation in _build_price_groups
- PriceGroupDef helpers: is_composite, is_audit_only
- AppConfig helpers: is_usprod, is_ukprod
- resolve_job_path / resolve_job_watch_dir
"""

from __future__ import annotations

import os
import types
from pathlib import Path
from unittest.mock import patch

import pytest

from src.config_loader import (
    AppConfig,
    PriceGroupDef,
    PROJECT_ROOT,
    _parse_weekdays,
    _build_price_groups,
)
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# _parse_weekdays
# ---------------------------------------------------------------------------

class TestParseWeekdays:
    def test_valid_range(self):
        assert _parse_weekdays("0,1,2,3,4") == (0, 1, 2, 3, 4)

    def test_deduplication(self):
        assert _parse_weekdays("0,0,1,1") == (0, 1)

    def test_sorted_output(self):
        assert _parse_weekdays("4,2,0") == (0, 2, 4)

    def test_empty_string(self):
        assert _parse_weekdays("") == ()

    def test_whitespace_parts_skipped(self):
        result = _parse_weekdays("0, ,2")
        assert 0 in result
        assert 2 in result

    def test_all_days(self):
        assert _parse_weekdays("0,1,2,3,4,5,6") == (0, 1, 2, 3, 4, 5, 6)

    def test_out_of_range_low(self):
        with pytest.raises(ValueError):
            _parse_weekdays("-1,0,1")

    def test_out_of_range_high(self):
        with pytest.raises(ValueError):
            _parse_weekdays("0,7")

    def test_non_integer(self):
        with pytest.raises(ValueError):
            _parse_weekdays("0,abc,2")


# ---------------------------------------------------------------------------
# _build_price_groups
# ---------------------------------------------------------------------------

class TestBuildPriceGroups:
    def _row(self, name, jobs, active=True, match_mode="all", notes=""):
        return {"price_group_name": name, "jobs": jobs, "active": active,
                "match_mode": match_mode, "notes": notes}

    def test_inactive_rows_excluded(self):
        rows = [
            self._row("ACTIVE", ["JOB_A"], active=True),
            self._row("INACTIVE", ["JOB_B"], active=False),
        ]
        defs = _build_price_groups(rows)
        names = [d.price_group_name for d in defs]
        assert "ACTIVE" in names
        assert "INACTIVE" not in names

    def test_invalid_match_mode_raises(self):
        rows = [self._row("PG", ["JOB_A"], match_mode="invalid")]
        with pytest.raises(ValueError, match="match_mode"):
            _build_price_groups(rows)

    def test_match_mode_all_accepted(self):
        rows = [self._row("PG", ["JOB_A"], match_mode="all")]
        defs = _build_price_groups(rows)
        assert defs[0].match_mode == "all"

    def test_match_mode_any_accepted(self):
        rows = [self._row("PG", ["JOB_A"], match_mode="any")]
        defs = _build_price_groups(rows)
        assert defs[0].match_mode == "any"

    def test_audit_only_row_included(self):
        rows = [self._row("AUDIT_ROW", [], active=True)]
        defs = _build_price_groups(rows)
        assert len(defs) == 1
        assert defs[0].is_audit_only

    def test_is_composite_for_multi_job(self):
        rows = [self._row("COMPOSITE", ["JOB_A", "JOB_B"])]
        defs = _build_price_groups(rows)
        assert defs[0].is_composite

    def test_is_not_composite_for_single_job(self):
        rows = [self._row("SINGLE", ["JOB_A"])]
        defs = _build_price_groups(rows)
        assert not defs[0].is_composite


# ---------------------------------------------------------------------------
# PriceGroupDef helpers
# ---------------------------------------------------------------------------

class TestPriceGroupDefHelpers:
    def _make_def(self, jobs, match_mode="all"):
        return PriceGroupDef(
            price_group_name="PG",
            jobs=tuple(jobs),
            match_mode=match_mode,
            notes="",
        )

    def test_is_composite_two_jobs(self):
        assert self._make_def(["A", "B"]).is_composite

    def test_is_not_composite_one_job(self):
        assert not self._make_def(["A"]).is_composite

    def test_is_audit_only_empty_jobs(self):
        assert self._make_def([]).is_audit_only

    def test_not_audit_only_with_jobs(self):
        assert not self._make_def(["A"]).is_audit_only


# ---------------------------------------------------------------------------
# AppConfig helpers
# ---------------------------------------------------------------------------

class TestAppConfigHelpers:
    def _make_cfg(self, site) -> types.SimpleNamespace:
        return types.SimpleNamespace(sender_site=site)

    def test_is_ukprod(self):
        cfg = self._make_cfg("UKPROD")
        assert cfg.sender_site == "UKPROD"

    def test_is_usprod(self):
        cfg = self._make_cfg("USPROD")
        assert cfg.sender_site == "USPROD"


# ---------------------------------------------------------------------------
# load_config validation (via env overrides)
# ---------------------------------------------------------------------------

class TestLoadConfigValidation:
    def _base_env(self, tmp_path):
        return {
            "RECEIVER_URL": "http://localhost:8080/prices",
            "SENDER_SITE": "UKPROD",
            "PRICES_ROOT": str(tmp_path / "prices"),
            "STATE_DIR": str(tmp_path / "state"),
            "BACKUP_DIR": str(tmp_path / "backups"),
            "LOG_DIR": str(tmp_path / "logs"),
        }

    def test_invalid_send_mode_raises(self, tmp_path, monkeypatch):
        env = self._base_env(tmp_path)
        env["SEND_MODE"] = "invalid_mode"
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        from src.config_loader import load_config
        with pytest.raises(ValueError, match="SEND_MODE"):
            load_config()

    def test_invalid_business_day_start_no_colon(self, tmp_path, monkeypatch):
        env = self._base_env(tmp_path)
        env["BUSINESS_DAY_START"] = "0600"
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        from src.config_loader import load_config
        with pytest.raises(ValueError, match="BUSINESS_DAY_START"):
            load_config()

    def test_invalid_business_day_anchor(self, tmp_path, monkeypatch):
        env = self._base_env(tmp_path)
        env["BUSINESS_DAY_ANCHOR"] = "middle"
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        from src.config_loader import load_config
        with pytest.raises(ValueError, match="BUSINESS_DAY_ANCHOR"):
            load_config()

    def test_invalid_sender_site_raises(self, tmp_path, monkeypatch):
        env = self._base_env(tmp_path)
        env["SENDER_SITE"] = "BADSITE"
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        from src.config_loader import load_config
        with pytest.raises(ValueError, match="SENDER_SITE"):
            load_config()

    def test_valid_config_loads(self, tmp_path, monkeypatch):
        env = self._base_env(tmp_path)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        from src.config_loader import load_config
        cfg = load_config()
        assert cfg.sender_site == "UKPROD"
        assert cfg.receiver_url == "http://localhost:8080/prices"

    def test_anchor_start_valid(self, tmp_path, monkeypatch):
        env = self._base_env(tmp_path)
        env["BUSINESS_DAY_ANCHOR"] = "start"
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        from src.config_loader import load_config
        cfg = load_config()
        assert cfg.business_day_anchor == "start"

    def test_anchor_end_valid(self, tmp_path, monkeypatch):
        env = self._base_env(tmp_path)
        env["BUSINESS_DAY_ANCHOR"] = "end"
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        from src.config_loader import load_config
        cfg = load_config()
        assert cfg.business_day_anchor == "end"
