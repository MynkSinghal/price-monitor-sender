from __future__ import annotations

from src.config_loader import _build_price_groups


def test_overlap_is_allowed() -> None:
    """A single job can feed multiple price groups.

    Real-world example: ATHISIN feeds both the PATH composite
    (ATHFIX + ATHVCT + ATHISIN) and the IATH standalone group.
    """
    rows = [
        {"price_group_name": "PATH", "jobs": ["ATHFIX", "ATHVCT", "ATHISIN"], "active": True},
        {"price_group_name": "IATH", "jobs": ["ATHISIN"], "active": True},
    ]
    defs = _build_price_groups(rows)
    assert len(defs) == 2
    names = {d.price_group_name for d in defs}
    assert names == {"PATH", "IATH"}
    path = next(d for d in defs if d.price_group_name == "PATH")
    iath = next(d for d in defs if d.price_group_name == "IATH")
    assert "ATHISIN" in path.jobs
    assert "ATHISIN" in iath.jobs


def test_inactive_rows_are_dropped_but_audit_only_kept() -> None:
    """active=false rows are dropped; active=true rows are kept even if jobs=[]
    (audit-only rows for KSE clients / manual-fill prices)."""
    rows = [
        {"price_group_name": "ACT", "jobs": ["J1"], "active": True},
        {"price_group_name": "INACT", "jobs": ["J2"], "active": False},
        {"price_group_name": "AUDIT", "jobs": [], "active": True},
    ]
    result = _build_price_groups(rows)
    names = {pg.price_group_name for pg in result}
    assert names == {"ACT", "AUDIT"}
    audit = next(pg for pg in result if pg.price_group_name == "AUDIT")
    assert audit.is_audit_only is True
    assert audit.jobs == ()


def test_match_mode_validation() -> None:
    import pytest
    rows = [{"price_group_name": "BAD", "jobs": ["J1"], "active": True, "match_mode": "fuzzy"}]
    with pytest.raises(ValueError, match="match_mode"):
        _build_price_groups(rows)


def test_match_mode_default_is_all() -> None:
    rows = [{"price_group_name": "C", "jobs": ["A", "B"], "active": True}]
    defs = _build_price_groups(rows)
    assert defs[0].match_mode == "all"


def test_match_mode_any_parsed() -> None:
    rows = [{"price_group_name": "JSE1/JSE", "jobs": ["JSE", "JSE1"],
             "active": True, "match_mode": "any"}]
    defs = _build_price_groups(rows)
    assert defs[0].match_mode == "any"


def test_composite_marker() -> None:
    rows = [{"price_group_name": "C", "jobs": ["A", "B"], "active": True}]
    defs = _build_price_groups(rows)
    assert defs[0].is_composite is True
