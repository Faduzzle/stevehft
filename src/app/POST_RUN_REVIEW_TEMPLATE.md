# Post-Run Review Template

Use this after each read-only dry run or small live-order smoke.

## Run Metadata

- Date/time:
- Mode: dry-run / live-orders
- Symbols:
- Cycles / duration:
- Session dir:
- Git / code snapshot note:

## Validation Commands

```bash
python3 -m src.telemetry.dry_run_validator <session_dir>/events.jsonl --tick-size 0.01
python3 -m src.telemetry.dry_run_summary <session_dir>/events.jsonl
```

## Lifecycle And Failure Events

- Did we see `app_started -> bootstrap_complete -> strategy_trace/session_metrics -> app_stopping`?
- Any `*_failed`, `*_parse_failed`, or `safe_mode_transition` events?
- If safe mode changed, what was the broker-state cause?

## Market-Data And Feature Sanity

- Does local-vs-global signal separation look correct?
- Are NBBO spread/mid, local microprice, local imbalance, and global drift finite and sensible?
- Do queue-share / queue-ahead move in the expected direction?
- Do z-scores, CUSUM, Page-Hinkley, and toxicity values avoid constant-zero or runaway behavior?

## Strategy And Order Intent

- Any crossed or non-tick-aligned quotes?
- Are disabled sides size-zero?
- Are flatten targets one-sided and risk-reducing?
- Are same-price good-queue orders being preserved instead of churned?

## Execution Economics

- Passive fill ratio:
- Estimated rebates / fees / net fees:
- VWAP / TWAP:
- Arrival / decision shortfall:
- Any evidence normal replace/cancel activity is suppressing allocation?
- Any unexpected short-cover BP constraints or oversized flatten chunks?

## Ledger Consistency

- Local order ledger vs broker waiting list:
- Local fill ledger vs broker executions:
- Local portfolio ledger vs broker portfolio:
- Any stale live orders left after shutdown?

## Decisions Before Next Run

- Parameters to keep frozen:
- Heuristic constants to recalibrate:
- Risk limits / symbols / cadence adjustments:
- Bugs or edge cases to fix before the next run:
