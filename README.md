# Portfolio Monitor

A standalone monitoring tool for a personal investment portfolio: a brokerage
positions export plus an optional retirement-plan balance sheet. It reconciles
the export, computes exposure and concentration metrics, evaluates 27 alert
rules and renders a fixed-format report. It also fills a data-free prompt
template so the same rules can be run by an LLM.

It never fetches prices, never places orders and never gives advice. Every
number comes from the file you hand it.

## Privacy

`data/`, `state/`, `reports/` and every spreadsheet or PDF are git-ignored.
Nothing in this repository contains account data. Keep your exports in `data/`
and they stay on your machine.

## Layout

```
prompt/PORTFOLIO_MONITOR_PROMPT.md   the operator prompt; {{BASELINE}} and bucket
                                     placeholders are filled from your export
monitor/monitor.py                   parser, reconciliation, metrics, rules, report
config.example.json                  thresholds and bucket definitions
tests/                               synthetic-data tests
data/  state/  reports/              local only, git-ignored
```

## Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json   # edit buckets and thresholds to taste
```

## Input format

A workbook with a `Positions` sheet laid out like an E*TRADE "Portfolios >
Positions" PDF converted to a sheet:

- a header block containing `Positions`, `Securities value`, `Cash`,
  `Total assets`, `Cost basis`, `Unrealized gain`, `Day's gain` as label/value
  pairs, and a cell containing `Snapshot: Mon DD, YYYY, HH:MM AM/PM`;
- a column-header row starting with `Symbol`, followed by one row per position:
  `Symbol | Quantity | Last price | Price paid / share | Market value |
  Total gain | Total gain % | Day's gain | Price change | Price change % |
  Special event`.

An optional second sheet holds plan holdings with a header row starting with
`Symbol`: `Symbol | Investment name | Asset class | Initial purchase date |
% invested | Balance | Cost basis | YTD return | Returns as of`, plus
`Account balance` and `Account YTD return` label/value pairs above it.

## Run

```bash
# reconcile only; exit code 1 if anything is outside tolerance
python -m monitor.monitor check data/positions.xlsx

# full report; appends this snapshot to state/history.json
python -m monitor.monitor run data/positions.xlsx --out reports/latest.txt

# same, without touching state
python -m monitor.monitor run data/positions.xlsx --dry-run

# fill the prompt template with your baseline for use in an LLM
python -m monitor.monitor render-prompt data/positions.xlsx
```

`--today YYYY-MM-DD` pins the date used for the staleness rules (R2) so runs
are reproducible.

## Rules

Rule ids, severities and thresholds are documented in
`prompt/PORTFOLIO_MONITOR_PROMPT.md` section 4 and configured in
`config.json`. Rules that need history (drawdown, rolling change, crossings,
holding-period anniversaries) report "insufficient history" until enough runs
have been recorded in `state/history.json`.

## Tests

```bash
pytest
```

The tests build synthetic workbooks in a temp directory; they do not read
anything from `data/`.

## Daily run

`scripts/daily.sh` is the one-command daily run. It takes the newest workbook
in `data/` (or a path you pass), writes `reports/<date>.txt` and
`reports/latest.txt`, and appends to `state/history.json` so the
history-based rules accumulate. Exit code 1 means reconciliation failed.

Local cron, every day at 06:53 (adjust the path):

```cron
53 6 * * * cd /path/to/portfolio-monitor && ./scripts/daily.sh >> reports/cron.log 2>&1
```

Claude Code on the web: a scheduled Routine can open a fresh session each
morning that runs the same script against an export you upload to that
session. Because sessions are ephemeral, also upload the previous
`state/history.json` if you want drawdown and crossing rules to keep their
history; the session sends the updated state back to you.
