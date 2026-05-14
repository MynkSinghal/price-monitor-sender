"""Sanity check: build a CSV snapshot using the real config under each site flag.

Run:  .venv/bin/python scripts/verify_two_site.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _bootstrap_env(site: str, tmp: Path) -> None:
    os.environ["RECEIVER_URL"] = "http://127.0.0.1:9999/test"
    os.environ["PRICES_ROOT"] = str(tmp / "prices")
    os.environ["SENDER_HOSTNAME"] = f"verify-{site}"
    os.environ["SENDER_ENV"] = "TEST"
    os.environ["SENDER_SITE"] = site
    os.environ["STATE_DIR"] = str(tmp / f"state-{site}")
    os.environ["BACKUP_DIR"] = str(tmp / f"backups-{site}")
    os.environ["LOG_DIR"] = str(tmp / f"logs-{site}")


def _run(site: str) -> None:
    tmp = Path(tempfile.mkdtemp(prefix=f"verify-{site}-"))
    _bootstrap_env(site, tmp)
    # NB: re-import to pick up fresh env each call
    for m in [m for m in list(sys.modules) if m.startswith("src.")]:
        del sys.modules[m]
    from src.config_loader import load_config
    from src.csv_builder import CsvBuilder
    from src.state_manager import StateManager

    cfg = load_config()
    state = StateManager(cfg.state_dir / "state.json")

    # Pretend ONE local job has completed today
    sample_local = next(
        j for pg in cfg.active_price_groups for j in pg.jobs
        if j not in cfg.cross_site_jobs
    )
    state.record(sample_local, datetime(2026, 4, 23, 14, 30, 42).timestamp(), "/x")

    snapshot = CsvBuilder(cfg, state).build(cfg.active_price_groups)
    rows = snapshot.payload.splitlines()
    print(f"\n=== {site} ===")
    print(f"active groups: {len(cfg.active_price_groups)}")
    print(f"rows on wire : {len(rows)}")
    print(f"complete     : {snapshot.complete_count}")
    print(f"pending      : {snapshot.pending_count}")
    print(f"local job recorded: {sample_local}")
    print("first 6 rows:")
    for r in rows[:6]:
        print(f"  {r}")
    print("rows mentioning USPROD jobs (CME_SPAN2A, OCC_CPM, OCCS …):")
    for r in rows:
        if any(j in r for j in ("CME_SPAN2A", "OCC_CPM", "OCCS|", "OCCSSTD", "OCCP_NON")):
            print(f"  {r}")


if __name__ == "__main__":
    _run("UKPROD")
    _run("USPROD")
