#!/usr/bin/env python3
"""Portfolio monitor: reconcile a brokerage positions export, compute exposure
metrics, evaluate alert rules and render the report defined in
prompt/PORTFOLIO_MONITOR_PROMPT.md.

Usage:
  python -m monitor.monitor run <positions.xlsx> [--config config.json] [--state state/history.json]
  python -m monitor.monitor render-prompt <positions.xlsx> [--config config.json] [--out reports/prompt.md]
  python -m monitor.monitor check <positions.xlsx>

The workbook layout is the one produced by the E*TRADE PDF-to-sheet export:
a "Positions" sheet with a header block followed by a column-header row that
starts with "Symbol", and an optional second sheet with retirement-plan
holdings whose column-header row also starts with "Symbol".

Nothing here fetches prices or places orders. All numbers come from the file.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

try:
    import openpyxl
except ImportError:  # pragma: no cover
    sys.exit("openpyxl is required: pip install -r requirements.txt")


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

DEFAULT_CONFIG: dict[str, Any] = {
    "row_tolerance_usd": 1.00,
    "total_tolerance_usd": 0.05,
    "export_max_age_days": 3,
    "plan_max_age_days": 10,
    "leverage_multiple": 3,
    "index_etfs": ["SPY", "VOO", "QQQ", "QQQM", "VUG", "DIA", "SPXL", "TQQQ"],
    "leveraged_etfs": ["TQQQ", "SPXL"],
    "sp500_bucket": ["SPY", "VOO", "SPXL"],
    "ndx_bucket": ["QQQ", "QQQM", "TQQQ"],
    "growth_bucket": ["VUG"],
    "megacap_bucket": ["AAPL", "MSFT", "AMZN", "GOOG", "GOOGL", "META", "NVDA"],
    "semis_bucket": ["NVDA", "AMD", "MU", "AVGO", "DRAM", "ANET", "CRWV", "NBIS"],
    "thresholds": {
        "day_red_down": -0.02, "day_red_up": 0.03, "day_amber_down": -0.01,
        "dd_red": 0.10, "dd_amber": 0.05,
        "roll5_down": -0.05, "roll5_up": 0.07,
        "pos_big_weight": 0.02, "pos_red_move": 0.08, "pos_amber_move": 0.05,
        "pos_small_move": 0.15, "pos_dd_red": 0.20,
        "lev_red": 0.18, "netlong_red": 1.25, "lev_amber": 0.15,
        "single_name_amber": 0.06, "top10_amber": 0.80, "ndx_amber": 0.50,
        "cash_low": 0.05, "cash_high": 0.15,
        "plan_equity_drift": 0.05, "plan_fund_drift": 0.03,
        "loser_pct": -0.20, "loser_further_drop": -0.10,
        "tiny_weight": 0.005, "near_zero_usd": 500.0,
    },
}


def load_config(path: str | None) -> dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if path and Path(path).exists():
        user = json.loads(Path(path).read_text())
        for k, v in user.items():
            if k == "thresholds":
                cfg["thresholds"].update(v)
            else:
                cfg[k] = v
    return cfg


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class Position:
    sym: str
    qty: float
    last: float
    paid: float
    mv: float
    gain: float
    gpct: float
    day: float
    pc: float
    pcp: float
    flag: bool

    @property
    def unpriced(self) -> bool:
        return self.qty > 0 and self.last == 0


@dataclass
class Header:
    count: int | None = None
    securities: float | None = None
    cash: float | None = None
    total: float | None = None
    cost: float | None = None
    ugain: float | None = None
    day: float | None = None
    snapshot: dt.datetime | None = None
    snapshot_text: str = ""


@dataclass
class PlanFund:
    sym: str
    name: str
    asset_class: str
    pct: float
    balance: float
    cost: float
    ytd: float
    as_of: dt.date | None


@dataclass
class Plan:
    funds: list[PlanFund] = field(default_factory=list)
    balance: float | None = None
    ytd: float | None = None
    as_of: dt.date | None = None


@dataclass
class Snapshot:
    header: Header
    positions: list[Position]
    plan: Plan | None


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

_MONTHS = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"
_SNAP_RE = re.compile(
    rf"Snapshot:\s*(({_MONTHS})\s+\d{{1,2}},\s+\d{{4}})(?:,\s*(\d{{1,2}}:\d{{2}}\s*[AP]M))?",
    re.I,
)


def _num(v: Any) -> float:
    if v is None or v == "":
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", "").replace("$", "").strip()
    try:
        return float(s)
    except ValueError:
        return 0.0


def _scan_header_values(rows: list[tuple]) -> dict[str, float]:
    """Pick label/value pairs out of the free-form header block."""
    out: dict[str, float] = {}
    for r in rows:
        for i, cell in enumerate(r):
            if isinstance(cell, str) and i + 1 < len(r) and isinstance(r[i + 1], (int, float)):
                out[cell.strip().lower()] = float(r[i + 1])
    return out


def parse_positions_sheet(ws) -> tuple[Header, list[Position]]:
    rows = list(ws.iter_rows(values_only=True))
    hdr_idx = next(
        (i for i, r in enumerate(rows) if r and isinstance(r[0], str) and r[0].strip().lower().startswith("symbol")),
        None,
    )
    if hdr_idx is None:
        raise ValueError("Positions sheet: no column-header row starting with 'Symbol'")

    header = Header()
    labels = _scan_header_values(rows[:hdr_idx])
    header.count = int(labels.get("positions", 0)) or None
    header.securities = labels.get("securities value")
    header.cash = labels.get("cash")
    header.total = next((v for k, v in labels.items() if k.startswith("total assets")), None)
    header.cost = next((v for k, v in labels.items() if k.startswith("cost basis")), None)
    header.ugain = labels.get("unrealized gain")
    header.day = labels.get("day's gain")
    for r in rows[:hdr_idx]:
        for cell in r:
            if isinstance(cell, str):
                m = _SNAP_RE.search(cell)
                if m:
                    header.snapshot_text = m.group(0)
                    date_part = m.group(1)
                    time_part = m.group(3)
                    fmt = "%b %d, %Y %I:%M %p" if time_part else "%b %d, %Y"
                    txt = f"{date_part} {time_part}" if time_part else date_part
                    try:
                        header.snapshot = dt.datetime.strptime(txt, fmt)
                    except ValueError:
                        header.snapshot = None

    positions: list[Position] = []
    for r in rows[hdr_idx + 1:]:
        if not r or r[0] is None:
            continue
        sym = str(r[0]).strip()
        if sym.lower() in ("securities total", "cash", "calculated assets") or sym.lower().startswith("rounding"):
            continue
        if not isinstance(r[1], (int, float)):
            continue
        flag_cell = r[10] if len(r) > 10 else None
        positions.append(Position(
            sym=sym, qty=_num(r[1]), last=_num(r[2]), paid=_num(r[3]), mv=_num(r[4]),
            gain=_num(r[5]), gpct=_num(r[6]), day=_num(r[7]), pc=_num(r[8]), pcp=_num(r[9]),
            flag=bool(flag_cell) and str(flag_cell).strip().lower() in ("yes", "y", "true", "x"),
        ))
    return header, positions


def parse_plan_sheet(ws) -> Plan:
    rows = list(ws.iter_rows(values_only=True))
    hdr_idx = next(
        (i for i, r in enumerate(rows) if r and isinstance(r[0], str) and r[0].strip().lower() == "symbol"),
        None,
    )
    plan = Plan()
    labels = _scan_header_values(rows[: (hdr_idx or 0)])
    plan.balance = labels.get("account balance")
    plan.ytd = labels.get("account ytd return")
    if hdr_idx is None:
        return plan
    for r in rows[hdr_idx + 1:]:
        if not r or r[1] is None or not isinstance(r[5], (int, float)):
            continue
        if isinstance(r[0], str) and r[0].strip().lower() == "account total":
            continue
        as_of = r[8].date() if isinstance(r[8], dt.datetime) else (r[8] if isinstance(r[8], dt.date) else None)
        plan.funds.append(PlanFund(
            sym=str(r[0]).strip() if r[0] else "",
            name=str(r[1]).strip(), asset_class=str(r[2] or "").strip(),
            pct=_num(r[4]), balance=_num(r[5]), cost=_num(r[6]), ytd=_num(r[7]), as_of=as_of,
        ))
    plan.as_of = max((f.as_of for f in plan.funds if f.as_of), default=None)
    return plan


def load_snapshot(path: str) -> Snapshot:
    wb = openpyxl.load_workbook(path, data_only=True)
    pos_ws = wb["Positions"] if "Positions" in wb.sheetnames else wb.worksheets[0]
    header, positions = parse_positions_sheet(pos_ws)
    plan = None
    for ws in wb.worksheets:
        if ws is pos_ws:
            continue
        try:
            p = parse_plan_sheet(ws)
        except Exception:  # pragma: no cover - tolerate unrelated sheets
            continue
        if p.funds:
            plan = p
            break
    return Snapshot(header=header, positions=positions, plan=plan)


# --------------------------------------------------------------------------
# Reconciliation and metrics
# --------------------------------------------------------------------------

def reconcile(s: Snapshot, cfg: dict[str, Any]) -> dict[str, Any]:
    tol_row = cfg["row_tolerance_usd"]
    tol_tot = cfg["total_tolerance_usd"]
    row_issues = []
    for p in s.positions:
        mv = p.qty * p.last
        g = mv - p.qty * p.paid
        basis = p.qty * p.paid
        gp = g / basis if basis else 0.0
        if abs(mv - p.mv) > tol_row or abs(g - p.gain) > tol_row or abs(gp - p.gpct) > 0.001:
            row_issues.append({"sym": p.sym, "mv_calc": mv, "mv": p.mv, "gain_calc": g, "gain": p.gain,
                               "gpct_calc": gp, "gpct": p.gpct})
    sec = sum(p.mv for p in s.positions)
    ug = sum(p.gain for p in s.positions)
    day = sum(p.day for p in s.positions)
    cost_rows = sum(p.qty * p.paid for p in s.positions)
    h = s.header
    diffs = {
        "securities": None if h.securities is None else sec - h.securities,
        "total": None if (h.total is None or h.cash is None) else sec + h.cash - h.total,
        "ugain": None if h.ugain is None else ug - h.ugain,
        "day": None if h.day is None else day - h.day,
        "cost": None if h.cost is None else cost_rows - h.cost,
    }
    total_ok = all(d is None or abs(d) <= tol_tot for k, d in diffs.items() if k != "cost")
    count_ok = h.count is None or h.count == len(s.positions)
    return {"row_issues": row_issues, "diffs": diffs, "total_ok": total_ok, "count_ok": count_ok,
            "securities_calc": sec, "ugain_calc": ug, "day_calc": day, "cost_calc": cost_rows,
            "unpriced": [p.sym for p in s.positions if p.unpriced]}


def metrics(s: Snapshot, cfg: dict[str, Any]) -> dict[str, Any]:
    h = s.header
    T = h.securities if h.securities else sum(p.mv for p in s.positions)
    cash = h.cash or 0.0
    TA = h.total if h.total else T + cash
    plan_bal = (s.plan.balance if s.plan and s.plan.balance else
                (sum(f.balance for f in s.plan.funds) if s.plan else 0.0))
    CB = TA + plan_bal
    mv = {p.sym: p.mv for p in s.positions}
    by_mv = sorted(s.positions, key=lambda p: -p.mv)
    mult = cfg["leverage_multiple"]

    def bucket(syms: list[str]) -> float:
        tot = 0.0
        for sym in syms:
            v = mv.get(sym, 0.0)
            tot += v * mult if sym in cfg["leveraged_etfs"] else v
        return tot

    lev = sum(mv.get(sym, 0.0) for sym in cfg["leveraged_etfs"])
    sp = bucket(cfg["sp500_bucket"])
    ndx = bucket(cfg["ndx_bucket"])
    growth = bucket(cfg["growth_bucket"])
    mega = sum(mv.get(sym, 0.0) for sym in cfg["megacap_bucket"])
    semis = sum(mv.get(sym, 0.0) for sym in cfg["semis_bucket"])
    named = set(cfg["index_etfs"]) | set(cfg["megacap_bucket"]) | set(cfg["semis_bucket"]) | \
        set(cfg["sp500_bucket"]) | set(cfg["ndx_bucket"]) | set(cfg["growth_bucket"])
    other = sum(p.mv for p in s.positions if p.sym not in named)

    plan_mix = {"equity": 0.0, "blended": 0.0, "bond": 0.0, "other": 0.0}
    if s.plan and plan_bal:
        for f in s.plan.funds:
            ac = f.asset_class.lower()
            key = "equity" if "stock" in ac or "equity" in ac else "blended" if "blend" in ac else \
                "bond" if "bond" in ac or "fixed" in ac else "other"
            plan_mix[key] += f.balance / plan_bal

    return {
        "T": T, "TA": TA, "cash": cash, "plan_bal": plan_bal, "CB": CB,
        "weights": {p.sym: (p.mv / T if T else 0.0) for p in s.positions},
        "top5": sum(p.mv for p in by_mv[:5]) / T if T else 0.0,
        "top10": sum(p.mv for p in by_mv[:10]) / T if T else 0.0,
        "top10_syms": [p.sym for p in by_mv[:10]],
        "lev": lev, "lev_pct": lev / T if T else 0.0, "lev_notional": lev * mult,
        "netlong": (T + (mult - 1) * lev) / TA if TA else 0.0,
        "sp_pct": sp / T if T else 0.0, "ndx_pct": ndx / T if T else 0.0,
        "ndx_growth_pct": (ndx + growth) / T if T else 0.0,
        "growth_pct": growth / T if T else 0.0, "mega_pct": mega / T if T else 0.0,
        "semis_pct": semis / T if T else 0.0, "other_pct": other / T if T else 0.0,
        "cash_pct": cash / TA if TA else 0.0,
        "day": h.day if h.day is not None else sum(p.day for p in s.positions),
        "day_pct": (h.day if h.day is not None else sum(p.day for p in s.positions)) / T if T else 0.0,
        "ugain_pct": (h.ugain / h.cost) if (h.ugain is not None and h.cost) else None,
        "losers": [p for p in by_mv if p.gpct <= cfg["thresholds"]["loser_pct"] and not p.unpriced],
        "tiny": [p for p in by_mv if T and p.mv / T < cfg["thresholds"]["tiny_weight"]],
        "plan_mix": plan_mix,
        "movers_up": sorted(s.positions, key=lambda p: -p.day)[:5],
        "movers_down": sorted(s.positions, key=lambda p: p.day)[:5],
    }


# --------------------------------------------------------------------------
# History / state
# --------------------------------------------------------------------------

def load_state(path: str) -> dict[str, Any]:
    p = Path(path)
    if p.exists():
        return json.loads(p.read_text())
    return {"runs": [], "hwm_total": None, "hwm_pos": {}, "first_seen": {}, "fired": {}}


def save_state(path: str, state: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2, default=str))


def snapshot_record(s: Snapshot, m: dict[str, Any]) -> dict[str, Any]:
    return {
        "snapshot": s.header.snapshot.isoformat() if s.header.snapshot else None,
        "snapshot_text": s.header.snapshot_text,
        "total": m["TA"], "securities": m["T"], "cash": m["cash"], "plan_bal": m["plan_bal"],
        "ugain": s.header.ugain, "cost": s.header.cost,
        "positions": {p.sym: {"qty": p.qty, "last": p.last, "paid": p.paid, "mv": p.mv,
                              "gain": p.gain, "gpct": p.gpct, "flag": p.flag} for p in s.positions},
        "plan_funds": {f.name: {"pct": f.pct, "balance": f.balance} for f in (s.plan.funds if s.plan else [])},
        "plan_equity": m["plan_mix"]["equity"] + m["plan_mix"]["blended"],
    }


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------

@dataclass
class Alert:
    level: str   # RED / AMBER / INFO
    rule: str
    text: str


def diff_snapshots(prev: dict[str, Any] | None, cur: dict[str, Any]) -> dict[str, Any]:
    if not prev:
        return {"first_run": True, "added": [], "removed": [], "qty": [], "flags_on": [], "flags_off": [],
                "cash_delta": None, "total_delta": None}
    pp, cp = prev["positions"], cur["positions"]
    added = sorted(set(cp) - set(pp))
    removed = sorted(set(pp) - set(cp))
    qty, flags_on, flags_off = [], [], []
    for sym in sorted(set(cp) & set(pp)):
        a, b = pp[sym], cp[sym]
        if abs(a["qty"] - b["qty"]) > 1e-9:
            ratio = b["qty"] / a["qty"] if a["qty"] else None
            paid_ratio = (a["paid"] / b["paid"]) if (b["paid"] and a["paid"]) else None
            kind = "split/reorg" if (ratio and paid_ratio and abs(ratio - paid_ratio) < 0.02) else \
                ("buy" if b["qty"] > a["qty"] else "sell")
            qty.append({"sym": sym, "from": a["qty"], "to": b["qty"], "kind": kind})
        if b["flag"] and not a["flag"]:
            flags_on.append(sym)
        if a["flag"] and not b["flag"]:
            flags_off.append(sym)
    return {"first_run": False, "added": added, "removed": removed, "qty": qty, "flags_on": flags_on,
            "flags_off": flags_off, "cash_delta": cur["cash"] - prev["cash"],
            "total_delta": cur["total"] - prev["total"]}


def evaluate_rules(s: Snapshot, rec: dict[str, Any], m: dict[str, Any], state: dict[str, Any],
                   cfg: dict[str, Any], today: dt.date) -> tuple[list[Alert], dict[str, Any], list[str]]:
    th = cfg["thresholds"]
    alerts: list[Alert] = []
    notes: list[str] = []
    runs = state["runs"]
    prev = runs[-1] if runs else None
    cur = snapshot_record(s, m)
    d = diff_snapshots(prev, cur)
    T = m["T"]
    w = m["weights"]

    def add(level: str, rule: str, text: str) -> None:
        alerts.append(Alert(level, rule, text))

    # --- Data integrity
    for ri in rec["row_issues"]:
        add("RED", "R1", f"{ri['sym']} row does not recompute: gain {ri['gain_calc']:,.2f} calc vs "
                         f"{ri['gain']:,.2f} export (diff {ri['gain_calc'] - ri['gain']:,.2f}); "
                         f"mv {ri['mv_calc']:,.2f} vs {ri['mv']:,.2f}")
    if not rec["total_ok"]:
        dd = {k: v for k, v in rec["diffs"].items() if v is not None and k != "cost"}
        add("RED", "R1", "Header totals outside tolerance: " + ", ".join(f"{k} {v:+,.2f}" for k, v in dd.items()))
    if not rec["count_ok"]:
        add("RED", "R1", f"Header says {s.header.count} positions, found {len(s.positions)} rows")
    if s.header.snapshot:
        age = (today - s.header.snapshot.date()).days
        if age > cfg["export_max_age_days"]:
            add("RED", "R2", f"Export is {age} days old (snapshot {s.header.snapshot:%b %d, %Y})")
    else:
        notes.append("Export snapshot timestamp not found; R2 age check skipped.")
    if s.plan and s.plan.as_of:
        page = (today - s.plan.as_of).days
        if page > cfg["plan_max_age_days"]:
            add("RED", "R2", f"Plan returns are {page} days old (as of {s.plan.as_of:%b %d, %Y})")
    if prev and len(cur["positions"]) != len(prev["positions"]) and not (d["added"] or d["removed"]):
        add("RED", "R3", "Position count changed without an added/removed symbol")
    stale = bool(prev and prev.get("snapshot") and cur.get("snapshot") and cur["snapshot"] <= prev["snapshot"])
    if stale:
        add("RED", "R1", f"Stale export: snapshot {cur['snapshot']} is not newer than {prev['snapshot']}; "
                         "this run is not recorded in history")

    # --- Portfolio-level moves
    dp = m["day_pct"]
    if dp <= th["day_red_down"] or dp >= th["day_red_up"]:
        add("RED", "R4", f"Day's gain {m['day']:+,.2f} = {dp:+.1%} of securities")
    elif th["day_red_down"] < dp <= th["day_amber_down"]:
        add("AMBER", "R5", f"Day's gain {m['day']:+,.2f} = {dp:+.1%} of securities")

    hwm_total = state.get("hwm_total")
    if hwm_total is None:
        hwm_total = m["TA"]
    hwm_total = max(hwm_total, m["TA"])
    if len(runs) >= 1:
        dd_total = 1 - m["TA"] / hwm_total if hwm_total else 0.0
        if dd_total >= th["dd_red"]:
            add("RED", "R6", f"Total assets drawdown {dd_total:.1%} from high-water mark {hwm_total:,.2f}")
        elif dd_total >= th["dd_amber"]:
            add("AMBER", "R7", f"Total assets drawdown {dd_total:.1%} from high-water mark {hwm_total:,.2f}")
    if len(runs) >= 5:
        base = runs[-5]["total"]
        r5 = m["TA"] / base - 1 if base else 0.0
        if r5 <= th["roll5_down"] or r5 >= th["roll5_up"]:
            add("AMBER", "R8", f"Rolling 5-run change in total assets {r5:+.1%}")
    else:
        notes.append("R6-R8, R12-R14, R22, R27: insufficient history." if not runs else
                     "R8, R14, R22, R27: insufficient history for rolling metrics.")

    # --- Position-level moves
    for p in s.positions:
        if p.unpriced:
            continue
        big = w[p.sym] >= th["pos_big_weight"]
        if big and abs(p.pcp) >= th["pos_red_move"]:
            add("RED", "R9", f"{p.sym} moved {p.pcp:+.1%} on the day (weight {w[p.sym]:.1%})")
        elif big and abs(p.pcp) >= th["pos_amber_move"]:
            add("AMBER", "R10", f"{p.sym} moved {p.pcp:+.1%} on the day (weight {w[p.sym]:.1%})")
        elif not big and abs(p.pcp) >= th["pos_small_move"] and p.mv >= th["near_zero_usd"]:
            add("AMBER", "R11", f"{p.sym} moved {p.pcp:+.1%} on the day (weight {w[p.sym]:.1%})")

    hwm_pos = dict(state.get("hwm_pos", {}))
    for p in s.positions:
        hwm_pos[p.sym] = max(hwm_pos.get(p.sym, p.mv), p.mv)
        if prev and p.sym in m["top10_syms"] and hwm_pos[p.sym]:
            ddp = 1 - p.mv / hwm_pos[p.sym]
            if ddp >= th["pos_dd_red"] and p.qty == prev["positions"].get(p.sym, {}).get("qty"):
                add("RED", "R12", f"{p.sym} is {ddp:.1%} below its high-water mark {hwm_pos[p.sym]:,.2f}")
    if prev:
        for p in s.positions:
            a = prev["positions"].get(p.sym)
            if not a or p.unpriced:
                continue
            if (a["gain"] >= 0) != (p.gain >= 0):
                add("AMBER", "R13", f"{p.sym} crossed {'into loss' if p.gain < 0 else 'into gain'} "
                                    f"({a['gpct']:+.1%} to {p.gpct:+.1%})")
            for lvl in (0.25, 0.5, 1.0, 2.0, 4.0, 8.0):
                for sign in (1, -1):
                    t = lvl * sign
                    if (a["gpct"] < t <= p.gpct) or (p.gpct < t <= a["gpct"]):
                        add("INFO", "R14", f"{p.sym} crossed {t:+.0%} (now {p.gpct:+.1%})")

    # --- Concentration and structure
    lev_pct, netlong = m["lev_pct"], m["netlong"]
    if lev_pct >= th["lev_red"] or netlong >= th["netlong_red"]:
        add("RED", "R15", f"Leveraged ETFs {m['lev']:,.2f} = {lev_pct:.1%} of securities; net-long {netlong:.1%}")
    elif lev_pct >= th["lev_amber"] - 0.001:
        qual = "" if lev_pct >= th["lev_amber"] else " (at threshold, not strictly over)"
        add("AMBER", "R16", f"Leveraged ETFs {m['lev']:,.2f} / {T:,.2f} = {lev_pct:.2%} of securities{qual}")
    for p in sorted(s.positions, key=lambda p: -p.mv):
        if p.sym not in cfg["index_etfs"] and w[p.sym] >= th["single_name_amber"]:
            add("AMBER", "R17", f"{p.sym} is {w[p.sym]:.1%} of securities (single-name limit {th['single_name_amber']:.0%})")
    if m["top10"] >= th["top10_amber"]:
        add("AMBER", "R18", f"Top-10 concentration {m['top10']:.1%}")
    if m["ndx_growth_pct"] >= th["ndx_amber"]:
        add("AMBER", "R19", f"Nasdaq-100 look-through incl. growth ETF = {m['ndx_growth_pct']:.1%} of securities "
                            f"(threshold {th['ndx_amber']:.0%})")
    if m["cash_pct"] < th["cash_low"] or m["cash_pct"] > th["cash_high"]:
        add("INFO", "R20", f"Cash {m['cash_pct']:.1%} of brokerage assets")
    if prev and s.plan and runs:
        base_eq = runs[0].get("plan_equity")
        if base_eq is not None and abs(cur["plan_equity"] - base_eq) > th["plan_equity_drift"]:
            add("INFO", "R21", f"Plan equity share {cur['plan_equity']:.1%} vs baseline {base_eq:.1%}")
        for name, f in cur["plan_funds"].items():
            b = runs[0].get("plan_funds", {}).get(name)
            if b and abs(f["pct"] - b["pct"]) > th["plan_fund_drift"]:
                add("INFO", "R21", f"{name} at {f['pct']:.1%} vs baseline {b['pct']:.1%}")

    # --- Watchlist and hygiene
    if prev:
        for p in m["losers"]:
            a = prev["positions"].get(p.sym)
            if a and a["last"] and p.last / a["last"] - 1 <= th["loser_further_drop"]:
                add("AMBER", "R22", f"{p.sym} (loser list) fell a further {p.last / a['last'] - 1:.1%}")
        for p in s.positions:
            a = prev["positions"].get(p.sym)
            if a and a["mv"] < th["near_zero_usd"] and a["last"] and p.last >= 2 * a["last"]:
                add("INFO", "R23", f"{p.sym} price doubled from {a['last']:.3f} to {p.last:.3f}")
        for sym in d["removed"]:
            if prev["positions"][sym]["mv"] < th["near_zero_usd"]:
                add("INFO", "R23", f"{sym} position closed")
    for p in s.positions:
        if p.qty > 0 and p.paid == 0 and (p.last > 0 or p.mv > 0):
            add("AMBER", "R24", f"{p.sym} (unpriced instrument) now shows price {p.last} / value {p.mv:,.2f}")
    if prev and d["cash_delta"] and d["cash_delta"] > 0 and not d["qty"] and not d["removed"]:
        add("AMBER", "R24", f"Cash rose {d['cash_delta']:+,.2f} with no trade in the export (dividend, deposit or escrow distribution; verify)")
    flagged_top = [sym for sym in m["top10_syms"] if any(p.sym == sym and p.flag for p in s.positions)]
    flagged_other = [p.sym for p in s.positions if p.flag and p.sym not in flagged_top]
    if flagged_top:
        add("INFO", "R25", f"Broker special-event flags on top-10 names {', '.join(flagged_top)}"
                           + (f" (also {', '.join(flagged_other)})" if flagged_other else "") + "; reason not in export")
    if prev and len(cur["positions"]) != len(prev["positions"]):
        add("INFO", "R26", f"Position count {len(prev['positions'])} to {len(cur['positions'])}")
    elif not prev:
        add("INFO", "R26", f"Position count baseline set at {len(cur['positions'])}")
    first_seen = dict(state.get("first_seen", {}))
    snap_date = s.header.snapshot.date() if s.header.snapshot else today
    for p in s.positions:
        fs = first_seen.get(p.sym)
        if fs is None:
            first_seen[p.sym] = snap_date.isoformat()
        else:
            fsd = dt.date.fromisoformat(fs)
            anniversary = fsd.replace(year=fsd.year + 1)
            prev_date = dt.date.fromisoformat(prev["snapshot"][:10]) if (prev and prev.get("snapshot")) else None
            if prev_date and prev_date < anniversary <= snap_date:
                add("AMBER", "R27", f"{p.sym} first seen {fs}; one year in the file as of this snapshot")
    if not prev:
        notes.append("First run: first-seen dates start at this snapshot; R27 cannot distinguish earlier purchases.")

    # Persisting labels
    fired_prev = state.get("fired", {})
    fired_now: dict[str, int] = {}
    for a in alerts:
        key = f"{a.rule}:{a.text.split(' ')[0]}"
        n = fired_prev.get(key, 0) + 1
        fired_now[key] = n
        if n > 1:
            a.text += f" (persisting, {n} runs)"

    order = {"RED": 0, "AMBER": 1, "INFO": 2}
    alerts.sort(key=lambda a: (order[a.level], int(a.rule[1:])))
    if stale:
        new_state = dict(state)
    else:
        new_state = {"runs": runs + [cur], "hwm_total": hwm_total, "hwm_pos": hwm_pos,
                     "first_seen": first_seen, "fired": fired_now}
    return alerts, {"state": new_state, "diff": d, "cur": cur, "prev": prev}, notes


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def money(v: float | None) -> str:
    return "not provided" if v is None else f"{v:,.2f}"


def smoney(v: float | None) -> str:
    return "not provided" if v is None else f"{v:+,.2f}"


def near_threshold_rules(m: dict[str, Any], cfg: dict[str, Any]) -> list[str]:
    th = cfg["thresholds"]
    out = []
    checks = [
        ("R15 net-long", m["netlong"], th["netlong_red"]),
        ("R15 leveraged", m["lev_pct"], th["lev_red"]),
        ("R16", m["lev_pct"], th["lev_amber"]),
        ("R18", m["top10"], th["top10_amber"]),
        ("R19", m["ndx_growth_pct"], th["ndx_amber"]),
    ]
    for name, val, thr in checks:
        if thr * 0.9 <= val < thr:
            out.append(f"{name} ({val:.1%} vs {thr:.0%})")
    for sym, wt in sorted(m["weights"].items(), key=lambda kv: -kv[1]):
        if sym not in cfg["index_etfs"] and th["single_name_amber"] * 0.9 <= wt < th["single_name_amber"]:
            out.append(f"R17 {sym} ({wt:.1%} vs {th['single_name_amber']:.0%})")
    return out


def render_report(s: Snapshot, rec: dict[str, Any], m: dict[str, Any], alerts: list[Alert],
                  ctx: dict[str, Any], notes: list[str], cfg: dict[str, Any]) -> str:
    h, w, prev, d = s.header, m["weights"], ctx["prev"], ctx["diff"]
    L: list[str] = []
    snap = h.snapshot.strftime("%b %d, %Y %I:%M %p") if h.snapshot else (h.snapshot_text or "snapshot date not provided")
    prev_txt = prev["snapshot"][:10] if (prev and prev.get("snapshot")) else "no prior snapshot"
    by_mv = sorted(s.positions, key=lambda p: -p.mv)

    if not prev:
        near = near_threshold_rules(m, cfg)
        L += ["BASELINE CHECK (first run)",
              f"Reconciliation {'passes' if rec['total_ok'] and not rec['row_issues'] else 'has issues (see R1)'}: "
              f"{len(s.positions)} positions ({len(s.positions) - len(rec['unpriced'])} priced, "
              f"{len(rec['unpriced'])} unpriced). Largest: " +
              ", ".join(f"{p.sym} {w[p.sym]:.1%}" for p in by_mv[:3]) +
              f". Leveraged ETFs {m['lev_pct']:.2%} of securities; cash {m['cash_pct']:.1%} of brokerage assets. "
              + ("Rules at or within 10% of threshold: " + "; ".join(near) + "." if near else
                 "No rule is within 10% of its threshold."),
              ""]

    L.append(f"PORTFOLIO MONITOR — {snap} (vs {prev_txt})")
    L.append("")
    L.append("ALERTS")
    ra = [a for a in alerts if a.level in ("RED", "AMBER")]
    if not ra:
        L.append("  No RED or AMBER alerts.")
    for a in ra:
        L.append(f"  {a.level:5} {a.rule:3} {a.text}")
    infos = [a for a in alerts if a.level == "INFO"]
    if infos or notes:
        L.append("  INFO  " + " | ".join([f"{a.rule} {a.text}" for a in infos] + notes))
    L.append("")

    cb_change = (f"{m['CB'] - (prev['total'] + prev['plan_bal']):+,.2f} / "
                 f"{(m['CB'] / (prev['total'] + prev['plan_bal']) - 1):+.1%} vs prior") if prev else "no prior"
    L += ["HEADLINE",
          f"  Combined assets ............ {money(m['CB'])} ({cb_change})",
          f"  Brokerage total assets ..... {money(m['TA'])} ({smoney(m['TA'] - prev['total']) if prev else 'no prior'})",
          f"  Brokerage day's gain ....... {smoney(m['day'])} ({m['day_pct']:+.1%} of securities)",
          f"  Brokerage unrealized gain .. {smoney(h.ugain)}"
          + (f" ({m['ugain_pct']:+.1%} on {money(h.cost)} cost)" if m["ugain_pct"] is not None else ""),
          f"  Plan balance ............... {money(m['plan_bal']) if s.plan else 'not provided'}"
          + (f" ({(smoney(m['plan_bal'] - prev['plan_bal']) if prev else 'no prior')}, YTD {s.plan.ytd:+.1%})"
             if s.plan and s.plan.ytd is not None else ""),
          f"  Cash ....................... {money(m['cash'])} ({m['cash_pct']:.1%} of brokerage assets)",
          "",
          "STRUCTURE",
          f"  Leveraged ETFs ............. {money(m['lev'])} ({m['lev_pct']:.1%} of securities), net-long {m['netlong']:.1%}",
          f"  Top 5 / Top 10 ............. {m['top5']:.1%} / {m['top10']:.1%}",
          f"  Buckets (look-through) ..... S&P {m['sp_pct']:.1%} | Nasdaq-100 {m['ndx_pct']:.1%} | Growth {m['growth_pct']:.1%} |",
          f"                               Direct mega-cap {m['mega_pct']:.1%} | Semis/AI {m['semis_pct']:.1%} | Other {m['other_pct']:.1%}",
          "                               (buckets overlap by design; they do not sum to 100%)"]
    if s.plan:
        pm = m["plan_mix"]
        L.append(f"  Plan mix ................... Equity {pm['equity']:.1%} | Blended {pm['blended']:.1%} | Bond {pm['bond']:.1%}")
    L.append("")
    L.append("MOVERS (day)")
    for p in m["movers_up"] + m["movers_down"]:
        L.append(f"  {p.sym:6} {p.day:+10,.2f}  {p.pcp:+.1%}  {w[p.sym]:.1%}")
    L.append("")
    L.append("CHANGES SINCE LAST SNAPSHOT")
    if d["first_run"]:
        L.append(f"  None detected. First run; this snapshot becomes the baseline and the high-water mark "
                 f"for total assets ({money(m['TA'])}) and each position.")
    else:
        items = []
        items += [f"{q['sym']} {q['kind']} {q['from']:g} to {q['to']:g}" for q in d["qty"]]
        items += [f"new {sym}" for sym in d["added"]] + [f"removed {sym}" for sym in d["removed"]]
        items += [f"flag set {sym}" for sym in d["flags_on"]] + [f"flag cleared {sym}" for sym in d["flags_off"]]
        if d["cash_delta"]:
            items.append(f"cash {d['cash_delta']:+,.2f}")
        L.append("  " + ("; ".join(items) if items else "None detected."))
    L.append("")
    L.append(f"LOSER LIST (gain % <= {cfg['thresholds']['loser_pct']:.0%})")
    if m["losers"]:
        for p in m["losers"]:
            note = []
            if p.flag:
                note.append("special-event flag set; possible corporate action, verify")
            if p.day == 0 and p.pc == 0:
                note.append("day's gain 0.00, price change 0.00 (check for stale quote)")
            if p.mv < cfg["thresholds"]["near_zero_usd"]:
                note.append(f"{p.mv:,.2f} of value; daily moves suppressed per R23")
            L.append(f"  {p.sym:6} {p.gain:+10,.2f}  {p.gpct:+.1%}  {w[p.sym]:.1%}  {'; '.join(note)}")
        L.append(f"  Loser list total: {sum(p.gain for p in m['losers']):+,.2f} across "
                 f"{sum(w[p.sym] for p in m['losers']):.1%} of securities.")
    else:
        L.append("  None.")
    L.append("")
    L.append("DATA NOTES")
    dd = rec["diffs"]
    L.append("  Reconciliation: " + "; ".join(
        f"{k} {'exact' if (v is not None and abs(v) < 0.005) else ('not provided' if v is None else f'{v:+,.2f}')}"
        for k, v in dd.items() if k != "cost") +
        (f"; row-summed cost basis vs header {dd['cost']:+,.2f}" if dd["cost"] is not None else "") + ".")
    if rec["unpriced"]:
        L.append("  Unpriced: " + ", ".join(
            f"{p.sym} ({p.qty:g} units, no price, treated as escrow not loss)"
            for p in s.positions if p.sym in rec["unpriced"]))
    if s.plan:
        nosym = [f.name for f in s.plan.funds if not f.sym]
        pg = sum(f.balance - f.cost for f in s.plan.funds)
        pc = sum(f.cost for f in s.plan.funds)
        L.append(f"  Plan: {len(s.plan.funds)} funds; {len(nosym)} without ticker; quantities and unit prices not provided; "
                 f"percent-invested sums to {sum(f.pct for f in s.plan.funds):.1%}. Cost-basis gain {pg:+,.2f} "
                 f"({(pg / pc if pc else 0):+.1%}), kept separate from brokerage unrealized gain.")
        neg = [f"{f.name} ({f.ytd:+.1%})" for f in s.plan.funds if f.ytd < 0]
        if neg:
            L.append("  Plan funds negative YTD: " + ", ".join(neg) + ".")
    tiny = m["tiny"]
    if tiny:
        L.append(f"  {len(tiny)} positions under {cfg['thresholds']['tiny_weight']:.1%} of securities; "
                 f"{sum(1 for p in tiny if p.gain < 0)} of those are losers.")
    for n in notes:
        if "insufficient history" not in n:
            L.append("  " + n)
    return "\n".join(L) + "\n"


def render_baseline(s: Snapshot, m: dict[str, Any], cfg: dict[str, Any]) -> str:
    """The data-bearing block that fills {{BASELINE}} in the prompt template."""
    h, w = s.header, m["weights"]
    L = [f"Account A: brokerage, USD (snapshot {h.snapshot_text or 'not provided'})",
         f"  Positions ........................ {len(s.positions)}",
         f"  Securities value ................. {money(m['T'])}",
         f"  Cash ............................. {money(m['cash'])}  ({m['cash_pct']:.1%} of brokerage assets)",
         f"  Total assets ..................... {money(m['TA'])}",
         f"  Cost basis ....................... {money(h.cost)}",
         f"  Unrealized gain .................. {money(h.ugain)}"
         + (f"  ({m['ugain_pct']:+.1%})" if m["ugain_pct"] is not None else ""),
         f"  Day's gain (baseline day) ........ {smoney(m['day'])}", ""]
    if s.plan:
        L += [f"Account B: retirement plan, USD (returns as of {s.plan.as_of or 'not provided'})",
              f"  Holdings ......................... {len(s.plan.funds)} funds",
              f"  Balance .......................... {money(m['plan_bal'])}",
              f"  Cost basis ....................... {money(sum(f.cost for f in s.plan.funds))}",
              f"  YTD return ....................... {s.plan.ytd:+.2%}" if s.plan.ytd is not None else
              "  YTD return ....................... not provided", ""]
    L += [f"Combined assets .................... {money(m['CB'])}", "",
          "Brokerage positions (symbol, qty, last, paid/share, market value, total gain, gain %, special event)"]
    for p in sorted(s.positions, key=lambda p: -p.mv):
        L.append(f"  {p.sym:10}{p.qty:>8g} {p.last:>9.3f} {p.paid:>10.4f} {p.mv:>13,.2f} {p.gain:>+13,.2f} "
                 f"{p.gpct:>+8.1%}  {'Yes' if p.flag else ''}")
    if s.plan:
        L += ["", "Plan holdings (symbol, name, asset class, % invested, balance, cost basis, YTD)"]
        for f in s.plan.funds:
            L.append(f"  {(f.sym or '(none)'):8}{f.name:24}{f.asset_class:18}{f.pct:>7.2%} {f.balance:>12,.2f} "
                     f"{f.cost:>12,.2f} {f.ytd:>+7.2%}")
    by_mv = sorted(s.positions, key=lambda p: -p.mv)
    pm = m["plan_mix"]
    L += ["", "Structural facts derived from the table above (share of brokerage securities unless stated):",
          f"  - Broad index ETFs = {sum(w.get(x, 0) for x in cfg['index_etfs']):.1%}.",
          f"  - Leveraged ETFs ({', '.join(cfg['leveraged_etfs'])}) = {money(m['lev'])} = {m['lev_pct']:.1%} of securities, "
          f"{(m['lev'] / m['CB'] if m['CB'] else 0):.1%} of combined assets; {cfg['leverage_multiple']}x notional "
          f"~{m['lev_notional']:,.0f}; effective net-long {m['netlong']:.0%}.",
          f"  - Top 10 = {m['top10']:.1%}; top 5 = {m['top5']:.1%}.",
          f"  - Direct mega-cap = {m['mega_pct']:.1%}, on top of the same names inside the index ETFs. "
          f"Nasdaq-100 look-through incl. growth ETF = {m['ndx_growth_pct']:.1%}; S&P 500 look-through = {m['sp_pct']:.1%}.",
          f"  - Semis / AI basket = {m['semis_pct']:.1%}.",
          f"  - {len(m['tiny'])} positions under 0.5%; {len(m['losers'])} down more than 20% from cost "
          f"({', '.join(p.sym for p in m['losers'])}).",
          f"  - Unpriced instruments: {', '.join(p.sym for p in s.positions if p.unpriced) or 'none'}.",
          f"  - Largest three: {', '.join(f'{p.sym} {w[p.sym]:.1%}' for p in by_mv[:3])}."]
    if s.plan:
        L.append(f"  - Plan mix: equity {pm['equity']:.1%}, blended {pm['blended']:.1%}, bond {pm['bond']:.1%}. "
                 f"Plan YTD figures are fund performance, not unrealized gain; never sum them with brokerage gains.")
    return "\n".join(L)


def render_prompt(template_path: str, s: Snapshot, m: dict[str, Any], cfg: dict[str, Any]) -> str:
    tpl = Path(template_path).read_text()
    subs = {
        "{{BASELINE}}": render_baseline(s, m, cfg),
        "{{ROW_TOLERANCE_USD}}": f"${cfg['row_tolerance_usd']:.2f}",
        "{{TOTAL_TOLERANCE_USD}}": f"${cfg['total_tolerance_usd']:.2f}",
        "{{INDEX_ETFS}}": ", ".join(cfg["index_etfs"]),
        "{{LEVERAGED_ETFS}}": ", ".join(cfg["leveraged_etfs"]),
        "{{LEVERAGE_MULTIPLE}}": str(cfg["leverage_multiple"]),
        "{{SP500_BUCKET}}": ", ".join(cfg["sp500_bucket"]),
        "{{NDX_BUCKET}}": ", ".join(cfg["ndx_bucket"]),
        "{{GROWTH_BUCKET}}": ", ".join(cfg["growth_bucket"]),
        "{{MEGACAP_BUCKET}}": ", ".join(cfg["megacap_bucket"]),
        "{{SEMIS_BUCKET}}": ", ".join(cfg["semis_bucket"]),
    }
    for k, v in subs.items():
        tpl = tpl.replace(k, v)
    return tpl


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "check", "render-prompt"):
        sp = sub.add_parser(name)
        sp.add_argument("workbook")
        sp.add_argument("--config", default="config.json")
        sp.add_argument("--today", default=None, help="YYYY-MM-DD, defaults to today")
        if name == "run":
            sp.add_argument("--state", default="state/history.json")
            sp.add_argument("--out", default=None, help="write report here as well as stdout")
            sp.add_argument("--dry-run", action="store_true", help="do not update state")
        if name == "render-prompt":
            sp.add_argument("--template", default="prompt/PORTFOLIO_MONITOR_PROMPT.md")
            sp.add_argument("--out", default="reports/PORTFOLIO_MONITOR_PROMPT.filled.md")
    a = ap.parse_args(argv)

    cfg = load_config(a.config)
    today = dt.date.fromisoformat(a.today) if a.today else dt.date.today()
    s = load_snapshot(a.workbook)
    rec = reconcile(s, cfg)
    m = metrics(s, cfg)

    if a.cmd == "check":
        print(json.dumps({k: v for k, v in rec.items()}, indent=2, default=str))
        return 0 if rec["total_ok"] and not rec["row_issues"] else 1

    if a.cmd == "render-prompt":
        out = render_prompt(a.template, s, m, cfg)
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(out)
        print(f"wrote {a.out}")
        return 0

    state = load_state(a.state)
    alerts, ctx, notes = evaluate_rules(s, rec, m, state, cfg, today)
    report = render_report(s, rec, m, alerts, ctx, notes, cfg)
    sys.stdout.write(report)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(report)
    if not a.dry_run:
        save_state(a.state, ctx["state"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
