"""Logging setup — rotating daily file + styled console output.

File handler  →  plain structured text (grep-friendly, unchanged).
Console handler →  styled output, only when stdout is a real TTY.

Console visual design
─────────────────────
Every line type has its own distinct look so you never have to squint:

  CYCLE (hero line) ── surrounded by separators, coloured badge:
  ────────────────────────────────────────────────────────────────────
    15:25:27  USPROD  CYCLE 7a3cc7b4  ·  183 rows  ·  ✓ 7  ·  ⏳ 136  [ ✓ SENT ]  2585b  61ms
  ────────────────────────────────────────────────────────────────────

  RECORDED (job file landed on disk):
    15:25:27  ●  CME_SPAN2A  →  03/05/2026 15:25:22

  IGNORED (duplicate, already locked):
    15:25:27  ·  CME_SPAN2I  (already recorded, skipping)

  Banner (=== ... ===):
  ══════════════════════════════════════════════════════════════════════
    ▶  PRICE MONITOR SENDER START  |  site=USPROD  ...
  ══════════════════════════════════════════════════════════════════════

  WARNING:  15:25:27  ⚠  WARNING  backup  │  message
  ERROR:    15:25:27  ✗  ERROR    transport │  message
  Regular:  15:25:27  state      │  message (dimmed logger name)
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config_loader import AppConfig

# Set by setup_logging(); used by _ColourFormatter to render timestamps in
# the configured business timezone regardless of the host OS clock.
_BUSINESS_TZ: ZoneInfo | None = None

_ROOT_LOGGER_NAME = "price_sender"

# ─────────────────────────────────────────────────────────────────────
# ANSI palette
# ─────────────────────────────────────────────────────────────────────
_R  = "\033[0m"    # reset
_B  = "\033[1m"    # bold
_D  = "\033[2m"    # dim

_GRAY   = "\033[90m"
_RED    = "\033[91m"
_GREEN  = "\033[92m"
_YELLOW = "\033[93m"
_BLUE   = "\033[94m"
_PURPLE = "\033[95m"
_CYAN   = "\033[96m"
_WHITE  = "\033[97m"

_BG_RED    = "\033[41m"
_BG_GREEN  = "\033[42m"
_BG_YELLOW = "\033[43m"
_BLK       = "\033[30m"    # black fg (for use on light backgrounds)

_W = 82   # separator width

_SEP_THIN  = f"{_GRAY}{'─' * _W}{_R}"
_SEP_THICK = f"{_CYAN}{'═' * _W}{_R}"

# ─────────────────────────────────────────────────────────────────────
# Colour availability
# ─────────────────────────────────────────────────────────────────────

def _init_colorama() -> bool:
    try:
        import colorama
        colorama.init(autoreset=False, strip=False)
        return True
    except Exception:
        return False


def _console_supports_colour() -> bool:
    if not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
        return False
    if os.name == "nt":
        return _init_colorama() or bool(
            os.environ.get("WT_SESSION") or os.environ.get("TERM")
        )
    return True


_USE_COLOUR: bool = _console_supports_colour()

# ─────────────────────────────────────────────────────────────────────
# Message patterns
# ─────────────────────────────────────────────────────────────────────

_CYCLE_RE = re.compile(
    r"^CYCLE\s+(?P<cid>[a-f0-9]+)"
    r".*?site=(?P<site>\S+)"
    r".*?rows=(?P<rows>\d+)"
    r".*?complete=(?P<complete>\d+)"
    r".*?pending=(?P<pending>\d+)"
    r".*?tx_ok=(?P<tx_ok>\w+)"
    r".*?bytes=(?P<bytes>\d+)"
    r"(?:.*?elapsed_ms=(?P<ms>\d+))?",
)

_RECORDED_RE = re.compile(
    r"^RECORDED\s+(?P<job>\S+)\s*\|\s*completed=(?P<dt>[^|]+?)(?:\s*\|.*)?$"
)

_IGNORED_RE = re.compile(
    r"^IGNORED\s+(?P<job>\S+)\s+\(already recorded at (?P<dt>[^)]+)\)"
)

# ─────────────────────────────────────────────────────────────────────
# Per-type formatters
# ─────────────────────────────────────────────────────────────────────

def _short_name(name: str) -> str:
    """price_sender.state → state"""
    return name.split(".")[-1]


def _fmt_cycle(ts: str, msg: str) -> str:
    m = _CYCLE_RE.match(msg)
    if not m:
        return f"  {_GRAY}{ts}{_R}  {msg}"

    cid      = m.group("cid")[:8]
    site     = m.group("site")
    rows     = m.group("rows")
    complete = int(m.group("complete"))
    pending  = int(m.group("pending"))
    ok       = m.group("tx_ok") == "True"
    byt      = m.group("bytes")
    ms       = m.group("ms") or "—"

    # Site badge
    site_colour = _YELLOW if "UK" in site else _PURPLE
    site_str = f"{_B}{site_colour}{site}{_R}"

    # Completion counts
    done_str = (
        f"{_GREEN}{_B}✓ {complete}{_R}" if complete > 0
        else f"{_GRAY}✓ 0{_R}"
    )
    pend_str = (
        f"{_YELLOW}{_B}⏳ {pending}{_R}" if pending > 0
        else f"{_GREEN}⏳ 0{_R}"
    )

    # TX result badge  (coloured background)
    if ok:
        badge = f"{_BG_GREEN}{_BLK}{_B} ✓ SENT {_R}"
        sep   = _SEP_THIN
    else:
        badge = f"{_BG_RED}{_WHITE}{_B} ✗ FAILED {_R}"
        sep   = f"{_RED}{'─' * _W}{_R}"

    line = (
        f"  {_GRAY}{ts}{_R}  {site_str}  "
        f"{_B}CYCLE {_GRAY}{cid}{_R}  "
        f"{_D}·{_R}  {rows} rows  "
        f"{_D}·{_R}  {done_str}  "
        f"{_D}·{_R}  {pend_str}  "
        f"{_D}·{_R}  {badge}  "
        f"{_GRAY}{byt}b  {ms}ms{_R}"
    )
    return f"\n{sep}\n{line}\n{sep}"


def _fmt_recorded(ts: str, msg: str) -> str:
    m = _RECORDED_RE.match(msg)
    if not m:
        return f"  {_GRAY}{ts}{_R}  {_GREEN}●{_R}  {msg}"
    job = m.group("job")
    dt  = m.group("dt").strip()
    return (
        f"  {_GRAY}{ts}{_R}  "
        f"{_GREEN}●{_R}  "
        f"{_B}{job}{_R}  "
        f"{_GRAY}→{_R}  "
        f"{_GREEN}{dt}{_R}"
    )


def _fmt_ignored(ts: str, msg: str) -> str:
    m = _IGNORED_RE.match(msg)
    if not m:
        return f"  {_D}{_GRAY}{ts}  ·  {msg}{_R}"
    job = m.group("job")
    dt  = m.group("dt")
    return f"  {_D}{_GRAY}{ts}  ·  {job}  (already recorded at {dt}){_R}"


def _fmt_banner(msg: str) -> str:
    inner = msg.strip("= ").strip()
    return (
        f"\n{_SEP_THICK}\n"
        f"  {_B}{_CYAN}▶  {inner}{_R}\n"
        f"{_SEP_THICK}"
    )


def _fmt_error(ts: str, level: str, name: str, msg: str) -> str:
    short = _short_name(name)
    return (
        f"  {_GRAY}{ts}{_R}  "
        f"{_B}{_RED}✗ {level:<8}{_R}  "
        f"{_D}{short}{_R}  "
        f"{_RED}{msg}{_R}"
    )


def _fmt_warning(ts: str, name: str, msg: str) -> str:
    short = _short_name(name)
    return (
        f"  {_GRAY}{ts}{_R}  "
        f"{_B}{_YELLOW}⚠ WARNING {_R}  "
        f"{_D}{short}{_R}  "
        f"{_YELLOW}{msg}{_R}"
    )


def _fmt_info(ts: str, name: str, msg: str) -> str:
    short = _short_name(name)
    return f"  {_GRAY}{ts}{_R}  {_D}{short:<12}{_R}  {msg}"


# ─────────────────────────────────────────────────────────────────────
# Formatter class
# ─────────────────────────────────────────────────────────────────────

class _ColourFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts    = datetime.fromtimestamp(record.created, tz=_BUSINESS_TZ).strftime("%H:%M:%S")
        msg   = record.getMessage()
        name  = record.name
        level = record.levelname

        if msg.startswith("CYCLE "):
            return _fmt_cycle(ts, msg)

        stripped = msg.strip()
        if stripped.startswith("===") and stripped.endswith("==="):
            return _fmt_banner(msg)

        if msg.startswith("RECORDED "):
            return _fmt_recorded(ts, msg)

        if msg.startswith("IGNORED "):
            return _fmt_ignored(ts, msg)

        if level in ("ERROR", "CRITICAL"):
            return _fmt_error(ts, level, name, msg)

        if level == "WARNING":
            return _fmt_warning(ts, name, msg)

        if level == "DEBUG":
            return f"  {_D}{_GRAY}{ts}  {_short_name(name):<12}  {msg}{_R}"

        return _fmt_info(ts, name, msg)


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────

_PLAIN_FMT = "%(asctime)s | %(levelname)-7s | %(threadName)-18s | %(name)s | %(message)s"
_DATE_FMT  = "%Y-%m-%d %H:%M:%S"


def setup_logging(cfg: AppConfig) -> logging.Logger:
    global _BUSINESS_TZ
    _BUSINESS_TZ = ZoneInfo(cfg.timezone_business)

    logger = logging.getLogger(_ROOT_LOGGER_NAME)
    logger.setLevel(cfg.log_level)
    logger.handlers.clear()
    logger.propagate = False

    # Render asctime in business TZ too so file logs match the console.
    # NOTE: must be a staticmethod — Python functions assigned to
    # Formatter.converter (a class attribute) would otherwise be treated as
    # bound methods and called with `self` as the first arg, breaking the
    # stdlib's `self.converter(record.created)` call site. The default
    # `time.localtime` doesn't hit this because it's a C builtin (not a
    # descriptor).
    def _business_tz_converter(secs):
        return datetime.fromtimestamp(secs, tz=_BUSINESS_TZ).timetuple()
    logging.Formatter.converter = staticmethod(_business_tz_converter)

    plain_fmt = logging.Formatter(fmt=_PLAIN_FMT, datefmt=_DATE_FMT)

    # File handler — always plain (grep/Splunk/audit friendly).
    # Filename uses the BUSINESS-day calendar date so a sender that wakes up
    # at 03:00 UK doesn't open a fresh log file an hour before the business
    # day actually starts.
    from .scheduler import now as _clock_now
    today_business_day = _clock_now(cfg).business_day
    today_name = today_business_day.strftime(cfg.log_filename_pattern)
    log_path: Path = cfg.log_dir / today_name
    file_handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=cfg.log_max_bytes_per_file,
        backupCount=cfg.log_backup_count_within_day,
        encoding="utf-8",
    )
    file_handler.setFormatter(plain_fmt)
    logger.addHandler(file_handler)

    # Console handler — styled when TTY supports colour
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(_ColourFormatter() if _USE_COLOUR else plain_fmt)
    logger.addHandler(console)

    logger.info(
        "Logging initialised | level=%s | colour=%s | file=%s",
        cfg.log_level, _USE_COLOUR, log_path,
    )
    return logger


def rotate_log_file(cfg: AppConfig, new_business_day) -> None:  # new_business_day: date
    """Replace the file handler on the root logger with one for new_business_day.

    Called by perform_daily_reset() at every business-day rollover so the log
    file transitions cleanly from sender-<old-date>.log to sender-<new-date>.log
    without a process restart.

    The console handler (StreamHandler to stdout) is left untouched.
    Any other non-file handlers are also preserved.
    """
    logger = logging.getLogger(_ROOT_LOGGER_NAME)

    new_name = new_business_day.strftime(cfg.log_filename_pattern)
    log_path: Path = cfg.log_dir / new_name

    # Nothing to do if we are already writing to the correct file
    # (happens on the startup reset when setup_logging just opened it).
    existing = [h for h in logger.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
    if existing and Path(existing[0].baseFilename).resolve() == log_path.resolve():
        return

    # Flush + close every RotatingFileHandler currently attached, then remove it.
    old_handlers = existing
    for h in old_handlers:
        try:
            h.flush()
            h.close()
        except Exception:
            pass
        logger.removeHandler(h)
    plain_fmt = logging.Formatter(fmt=_PLAIN_FMT, datefmt=_DATE_FMT)
    new_fh = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=cfg.log_max_bytes_per_file,
        backupCount=cfg.log_backup_count_within_day,
        encoding="utf-8",
    )
    new_fh.setFormatter(plain_fmt)
    logger.addHandler(new_fh)

    logger.info(
        "Log file rotated for new business day | new_file=%s",
        log_path,
    )


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"{_ROOT_LOGGER_NAME}.{name}")
