"""Synthetic-workbook tests. No real account data is used here."""
import datetime as dt
import json
import openpyxl
import pytest

from monitor import monitor as M


def build_workbook(path, positions, cash=10_000.0, snapshot="Snapshot: Jan 05, 2026, 09:00 PM ET",
                   plan=None, total_override=None, count_override=None):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Positions"
    sec = sum(p[4] for p in positions)
    ugain = sum(p[5] for p in positions)
    day = sum(p[7] for p in positions)
    cost = sum(p[1] * p[3] for p in positions)
    ws.append([None])
    ws.append(["Brokerage positions"])
    ws.append([f"All brokerage accounts. {snapshot}. All monetary amounts in USD."])
    ws.append([None])
    ws.append(["Positions", count_override or len(positions), None, "Cost basis (PDF)", cost])
    ws.append(["Securities value", sec, None, "Unrealized gain", ugain])
    ws.append(["Cash", cash, None, "Unrealized gain %", ugain / cost if cost else 0])
    ws.append(["Total assets (PDF)", total_override if total_override is not None else sec + cash,
               None, "Day's gain", day])
    ws.append([None])
    ws.append(["Symbol / CUSIP", "Quantity", "Last price (USD)", "Price paid / share (USD)", "Market value (USD)",
               "Total gain (USD)", "Total gain %", "Day's gain (USD)", "Price change (USD)", "Price change %",
               "Special event"])
    for p in positions:
        ws.append(list(p))
    ws.append([None])
    ws.append(["Securities total", None, None, None, sec, ugain, None, day])
    if plan:
        pw = wb.create_sheet("Plan")
        bal = sum(f[5] for f in plan)
        pw.append(["Plan holdings"])
        pw.append(["Account balance", bal])
        pw.append(["Account YTD return", 0.1])
        pw.append(["Symbol", "Investment name (as shown)", "Asset class", "Initial purchase date", "% invested",
                   "Balance (USD)", "Cost basis (USD)", "YTD return", "Returns as of"])
        for f in plan:
            pw.append(list(f))
        pw.append(["Account total", None, None, None, 1, bal])
    wb.save(path)


def pos(sym, qty, last, paid, day=0.0, pcp=0.0, flag=None):
    mv = qty * last
    gain = mv - qty * paid
    gpct = gain / (qty * paid) if paid else 0.0
    return (sym, qty, last, paid, round(mv, 2), round(gain, 2), gpct, day, round(last * pcp, 4), pcp, flag)


BASE = [
    pos("TQQQ", 1000, 50.0, 20.0, day=500, pcp=0.01, flag="Yes"),
    pos("SPY", 100, 500.0, 400.0, day=-100, pcp=-0.002),
    pos("QQQ", 50, 400.0, 300.0),
    pos("VUG", 200, 80.0, 60.0),
    pos("NVDA", 100, 150.0, 50.0, flag="Yes"),
    pos("BIGCO", 100, 100.0, 90.0),
    pos("LOSER", 100, 10.0, 40.0),
    pos("DUST", 100, 0.5, 50.0),
    pos("ESCROW1", 240, 0.0, 0.0),
]

PLAN = [
    ("VIIIX", "INDEX FUND", "Stock Investments", dt.datetime(2024, 1, 1), 0.6, 60_000.0, 50_000.0, 0.08,
     dt.datetime(2026, 1, 4)),
    (None, "HY BOND", "Bond Investments", dt.datetime(2024, 1, 1), 0.4, 40_000.0, 41_000.0, -0.01,
     dt.datetime(2026, 1, 4)),
]


@pytest.fixture
def cfg():
    return M.load_config(None)


def run(path, cfg, state=None, today=dt.date(2026, 1, 6)):
    s = M.load_snapshot(str(path))
    rec = M.reconcile(s, cfg)
    m = M.metrics(s, cfg)
    state = state or M.load_state(str(path) + ".nostate.json")
    alerts, ctx, notes = M.evaluate_rules(s, rec, m, state, cfg, today)
    report = M.render_report(s, rec, m, alerts, ctx, notes, cfg)
    return s, rec, m, alerts, ctx, report


def test_parse_and_reconcile(tmp_path, cfg):
    f = tmp_path / "a.xlsx"
    build_workbook(f, BASE, plan=PLAN)
    s = M.load_snapshot(str(f))
    assert len(s.positions) == 9
    assert s.header.snapshot == dt.datetime(2026, 1, 5, 21, 0)
    assert s.plan and len(s.plan.funds) == 2
    rec = M.reconcile(s, cfg)
    assert rec["total_ok"] and rec["count_ok"] and not rec["row_issues"]
    assert rec["unpriced"] == ["ESCROW1"]


def test_metrics_leverage_and_buckets(tmp_path, cfg):
    f = tmp_path / "a.xlsx"
    build_workbook(f, BASE)
    s = M.load_snapshot(str(f))
    m = M.metrics(s, cfg)
    T = m["T"]
    assert m["lev"] == pytest.approx(50_000.0)
    assert m["lev_pct"] == pytest.approx(50_000.0 / T)
    # 3x counted at its multiple in the Nasdaq-100 bucket
    assert m["ndx_pct"] == pytest.approx((3 * 50_000.0 + 20_000.0) / T)
    assert m["netlong"] == pytest.approx((T + 2 * 50_000.0) / m["TA"])
    assert [p.sym for p in m["losers"]] == ["LOSER", "DUST"]


def test_first_run_alerts(tmp_path, cfg):
    f = tmp_path / "a.xlsx"
    build_workbook(f, BASE, plan=PLAN)
    s, rec, m, alerts, ctx, report = run(f, cfg)
    rules = {a.rule for a in alerts}
    assert "R15" in rules            # 50% leveraged is well over 18%
    assert "R17" in rules            # NVDA 15,000 of ~105,050 is > 6%
    assert "R26" in rules
    assert "R1" not in rules
    assert "BASELINE CHECK" in report
    assert "No RED or AMBER alerts." not in report


def test_row_tolerance_fires_r1(tmp_path, cfg):
    bad = list(BASE)
    sym, qty, last, paid, mv, gain, gpct, day, pc, pcp, flag = bad[1]
    bad[1] = (sym, qty, last, paid, mv, gain - 4.48, gpct, day, pc, pcp, flag)
    f = tmp_path / "a.xlsx"
    build_workbook(f, bad)
    s, rec, m, alerts, ctx, report = run(f, cfg)
    assert [a for a in alerts if a.rule == "R1" and a.level == "RED"]


def test_stale_export_r2(tmp_path, cfg):
    f = tmp_path / "a.xlsx"
    build_workbook(f, BASE)
    s, rec, m, alerts, ctx, report = run(f, cfg, today=dt.date(2026, 1, 20))
    assert any(a.rule == "R2" for a in alerts)


def test_second_run_diff_and_drawdown(tmp_path, cfg):
    f1 = tmp_path / "a.xlsx"
    build_workbook(f1, BASE)
    s, rec, m, alerts, ctx, report = run(f1, cfg)
    state = ctx["state"]
    second = [
        pos("TQQQ", 800, 40.0, 20.0, day=-8000, pcp=-0.2, flag="Yes"),   # sold 200 and fell 20%
        pos("SPY", 100, 500.0, 400.0),
        pos("QQQ", 50, 400.0, 300.0),
        pos("VUG", 200, 80.0, 60.0),
        pos("NVDA", 100, 150.0, 50.0),                                   # flag cleared
        pos("BIGCO", 100, 85.0, 90.0),                                   # crossed into loss
        pos("LOSER", 100, 8.0, 40.0),                                    # further -20%
        pos("DUST", 100, 1.2, 50.0),                                     # price doubled
        pos("ESCROW1", 240, 0.0, 0.0),
        pos("NEWCO", 10, 10.0, 10.0),
    ]
    f2 = tmp_path / "b.xlsx"
    build_workbook(f2, second, snapshot="Snapshot: Jan 06, 2026, 09:00 PM ET")
    s, rec, m, alerts, ctx, report = run(f2, cfg, state=state, today=dt.date(2026, 1, 7))
    rules = {a.rule for a in alerts}
    d = ctx["diff"]
    assert d["added"] == ["NEWCO"]
    assert any(q["sym"] == "TQQQ" and q["kind"] == "sell" for q in d["qty"])
    assert "NVDA" in d["flags_off"]
    assert "R9" in rules      # TQQQ -20% on a >2% weight
    assert "R13" in rules     # BIGCO crossed into loss
    assert "R22" in rules     # LOSER fell further
    assert "R23" in rules     # DUST doubled
    assert "R26" in rules     # position count changed
    assert "R6" in rules or "R7" in rules   # total assets drawdown
    assert "CHANGES SINCE LAST SNAPSHOT" in report and "TQQQ sell 1000 to 800" in report


def test_stale_snapshot_is_flagged(tmp_path, cfg):
    f1 = tmp_path / "a.xlsx"
    build_workbook(f1, BASE)
    s, rec, m, alerts, ctx, report = run(f1, cfg)
    s, rec, m, alerts, ctx, report = run(f1, cfg, state=ctx["state"])
    assert any(a.rule == "R1" and "Stale export" in a.text for a in alerts)


def test_render_prompt_has_no_placeholders(tmp_path, cfg):
    f = tmp_path / "a.xlsx"
    build_workbook(f, BASE, plan=PLAN)
    s = M.load_snapshot(str(f))
    m = M.metrics(s, cfg)
    out = M.render_prompt("prompt/PORTFOLIO_MONITOR_PROMPT.md", s, m, cfg)
    assert "{{" not in out
    assert "TQQQ" in out and "ESCROW1" in out


def test_state_roundtrip(tmp_path, cfg):
    f = tmp_path / "a.xlsx"
    build_workbook(f, BASE)
    s, rec, m, alerts, ctx, report = run(f, cfg)
    p = tmp_path / "state.json"
    M.save_state(str(p), ctx["state"])
    st = M.load_state(str(p))
    assert len(st["runs"]) == 1 and st["hwm_total"] == pytest.approx(m["TA"])
    json.loads(p.read_text())
