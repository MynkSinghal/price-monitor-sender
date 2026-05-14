"""Audit the CSV payload produced by CsvBuilder against the standard csv module."""
import csv
import io
import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT_PATH = ROOT / "audit_output.txt"
_buf: list[str] = []

def w(msg: str = "") -> None:
    _buf.append(msg)

def section(title: str) -> None:
    w("")
    w("=" * 72)
    w("  " + title)
    w("=" * 72)

try:
    from src.config_loader import PriceGroupDef
    from src.csv_builder import CsvBuilder
    from src.state_manager import StateManager

    section("1) Scan every ACTIVE price-group name for risky characters")
    with open(ROOT / "config" / "price_groups.json") as f:
        raw = json.load(f)
    names = [g["price_group_name"] for g in raw["price_groups"] if g.get("active", True)]
    w(f"Active groups scanned: {len(names)}")
    for ch in ["|", '"', ",", "\n", "\r", "\t"]:
        hits = [n for n in names if ch in n]
        line = f"  contains {repr(ch):<6}: {len(hits):3d} names"
        if hits:
            line += f"   example: {hits[0]!r}"
        w(line)

    section("2) Build a real payload via CsvBuilder")
    cfg = SimpleNamespace(
        csv_delimiter="|",
        csv_header=("price_group_name", "timestamp"),
        csv_timestamp_format="%d/%m/%Y %H:%M:%S",
        csv_emit_header=False,
    )
    tmp = Path(tempfile.mkdtemp())
    state = StateManager(tmp / "state.json")
    state.record("NSE_IX_1",   datetime(2026, 4, 23, 9, 15, 42).timestamp(), "/x")
    state.record("ICE_GSPD",   datetime(2026, 4, 23, 11, 0, 12).timestamp(), "/x")
    state.record("ICE_GPDR",   datetime(2026, 4, 23, 11, 5, 30).timestamp(), "/x")
    state.record("ICEUS_GSPD", datetime(2026, 4, 23, 11, 3, 59).timestamp(), "/x")
    state.record("ATHFIX",     datetime(2026, 4, 23, 17, 0, 10).timestamp(), "/x")
    state.record("ATHVCT",     datetime(2026, 4, 23, 17, 2, 20).timestamp(), "/x")
    state.record("ATHISIN",    datetime(2026, 4, 23, 17, 5, 33).timestamp(), "/x")
    groups = (
        PriceGroupDef("NSE_IX_1", ("NSE_IX_1",)),
        PriceGroupDef("ICE_GSPD / ICE_GPDR / ICEUS_GSPD", ("ICE_GSPD", "ICE_GPDR", "ICEUS_GSPD")),
        PriceGroupDef("PATH", ("ATHFIX", "ATHVCT", "ATHISIN")),
        PriceGroupDef("IATH", ("ATHISIN",)),
    )
    payload = CsvBuilder(cfg, state).build(groups).payload
    w("Raw repr (shows the real bytes):")
    w("  " + repr(payload))
    w("")
    w("Pretty rendering:")
    for line in payload.splitlines():
        w("  " + line)

    section("3) Round-trip: parse with csv.reader(delimiter='|')")
    rows = list(csv.reader(io.StringIO(payload), delimiter="|"))
    for i, r in enumerate(rows, 1):
        w(f"  row {i}: {r}")
    w(f"Field-count consistency: {sorted({len(r) for r in rows})}")

    section("4) Write to .csv file, re-read")
    out = tmp / "sample.csv"
    out.write_text(payload, encoding="utf-8")
    with open(out, newline="", encoding="utf-8") as f:
        parsed = list(csv.reader(f, delimiter="|"))
    w(f"File: {out}")
    w(f"Parsed rows: {len(parsed)}")
    for r in parsed:
        w(f"  {r}")

    section("5) Third-party parser (pandas) sanity check")
    try:
        import pandas as pd
        df = pd.read_csv(io.StringIO(payload), sep="|", header=None,
                         names=["price_group_name", "timestamp"])
        w(df.to_string(index=False))
    except Exception as e:
        w(f"pandas not available or failed: {e!r}")

    w("\nDone.")
except Exception as exc:
    import traceback
    w("ERROR during audit:")
    w(traceback.format_exc())

OUT_PATH.write_text("\n".join(_buf) + "\n")
