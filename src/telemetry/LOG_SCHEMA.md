# Telemetry Log Schema Checklist

## Purpose

This is the minimum event-schema checklist we should verify before trusting a
dry-run or live-smoke session analysis.

All JSONL events are emitted as:

```json
{"kind":"event_name","ts_ns":123,"payload":{...}}
```

So post-run tooling should always read `kind`, `ts_ns`, and `payload`.

## Required Event Families

### Session / App Lifecycle

- `app_started`
  - `payload.session_dir`
  - `payload.event_logging_enabled`
  - `payload.runtime_config`
- `bootstrap_complete`
  - `payload.symbols`
  - `payload.connected`
  - `payload.subscribed_symbols`
  - `payload.trading_stack_ready`
- `bootstrap_failed`
  - `payload.error`
  - `payload.connected`
  - `payload.subscribed_symbols`
- `startup_position_baseline_applied`
  - `payload.baseline_offsets_shares`
- `startup_reconciliation_ready`
  - `payload.attempts`
  - `payload.safe_mode`
- `startup_reconciliation_wait`
  - `payload.attempts`
  - `payload.safe_mode`
- `app_stopping`
  - `payload.session_dir`

### Market Data

- `market_data_cycle`
  - `payload.symbols`
  - `payload.iterations`
  - `payload.cycle_started_ns`
  - `payload.cycle_completed_ns`
  - `payload.event_write_seq`
  - `payload.event_overwritten`
- `book_cache_refresh`
  - `payload.symbol`
  - `payload.best_bid_px`
  - `payload.best_ask_px`
  - `payload.global_depth_levels`
  - `payload.local_depth_levels`
- `app_market_data_event_consumed`
  - `payload.event_write_seq`
  - `payload.event_iterations`
  - `payload.symbols`
  - `payload.app_iterations`

### Strategy

- `default_strategy_attached`
  - `payload.strategy_name`
- `strategy_trace`
  - `payload.trace.symbol`
  - `payload.trace.strategy_name`
  - `payload.trace.model_version`
  - `payload.trace.decision_reason`
  - `payload.trace.diagnostics`
  - `payload.trace.quote_target`
- `strategy_target`
  - `payload.target.symbol`
  - `payload.target.bid_px`
  - `payload.target.ask_px`
  - `payload.target.bid_size`
  - `payload.target.ask_size`
  - `payload.target.enable_bid`
  - `payload.target.enable_ask`
  - `payload.target.flatten_mode`
  - `payload.target.reason`
- `strategy_cycle_complete`
  - `payload.targets`
  - `payload.traces`
  - `payload.commands`
  - `payload.executed_orders`
- `strategy_cycle_skipped_session_closed`
  - `payload.now_local`
  - `payload.session_open_local`
  - `payload.session_close_local`
  - `payload.minutes_to_close`

### Reconciliation / Execution

- `server_poll_complete`
  - `payload.tracked_orders`
  - `payload.tracked_symbols`
  - `payload.tracked_positions`
- `reconciliation_action`
  - `payload.command.action`
  - `payload.command.symbol`
  - `payload.command.side`
  - `payload.command.price`
  - `payload.command.size`
  - `payload.command.order_id`
  - `payload.command.reason`
- `order_command`
  - `payload.command.action`
  - `payload.command.symbol`
  - `payload.command.side`
  - `payload.command.price`
  - `payload.command.size`
  - `payload.command.order_id`
  - `payload.command.reason`
- `order_submitted`
  - `payload.symbol`
  - `payload.side`
  - `payload.order_id`
  - `payload.price`
  - `payload.size`
  - `payload.reason`
- `order_cancel_requested`
  - `payload.symbol`
  - `payload.side`
  - `payload.order_id`
  - `payload.reason`
- `order_replace_requested`
  - `payload.symbol`
  - `payload.side`
  - `payload.order_id`
  - `payload.new_price`
  - `payload.new_size`
  - `payload.reason`
- `order_flatten_submitted`
  - `payload.symbol`
  - `payload.side`
  - `payload.order_id`
  - `payload.size`
  - `payload.reason`
- `order_seen_live`
  - `payload.symbol`
  - `payload.side`
  - `payload.order_id`
  - `payload.price`
  - `payload.size`
  - `payload.executed_size`
  - `payload.status`
- `order_state_update`
  - `payload.symbol`
  - `payload.side`
  - `payload.order_id`
  - `payload.price`
  - `payload.size`
  - `payload.executed_size`
  - `payload.status`
- `order_inactive`
  - `payload.symbol`
  - `payload.side`
  - `payload.order_id`
- `order_fill`
  - `payload.fill.order_id`
  - `payload.fill.symbol`
  - `payload.fill.side`
  - `payload.fill.executed_size`
  - `payload.fill.executed_price`
  - `payload.fill.status`
  - `payload.fill.broker_timestamp`
  - `payload.fill.execution_index`
  - `payload.level_index`
  - `payload.liquidity`
  - `payload.slippage`

### Portfolio / Metrics / Risk

- `position_update`
  - `payload.position.symbol`
  - `payload.position.long_shares`
  - `payload.position.short_shares`
  - `payload.position.long_price`
  - `payload.position.short_price`
  - `payload.position.realized_pl`
  - `payload.position.last_update_ts_ns`
- `portfolio_snapshot`
  - `payload.total_bp`
  - `payload.total_shares`
  - `payload.total_realized_pl`
  - `payload.positions`
- `session_metrics`
  - `payload.metrics.executed_trades`
  - `payload.metrics.executed_shares`
  - `payload.metrics.passive_fills`
  - `payload.metrics.aggressive_fills`
  - `payload.metrics.passive_fill_ratio`
  - `payload.metrics.estimated_rebates`
  - `payload.metrics.estimated_fees`
  - `payload.metrics.estimated_net_fees`
  - `payload.metrics.fill_vwap`
  - `payload.metrics.fill_twap`
  - `payload.metrics.arrival_shortfall`
  - `payload.metrics.decision_shortfall`
- `safe_mode_transition`
  - `payload.previous_mode`
  - `payload.next_mode`
  - `payload.waiting_list_stale_ms`
  - `payload.portfolio_stale_ms`
  - `payload.position_mismatch_lots`
  - `payload.broker_connected`

### Failure / Degradation Events

- `execution_sync_failed`
  - `payload.order_id`
  - `payload.failures`
  - `payload.error`
- `waiting_list_sync_failed`
  - `payload.failures`
  - `payload.error`
- `portfolio_sync_failed`
  - `payload.failures`
  - `payload.error`
- `waiting_order_parse_failed`
  - `payload.raw_order_type`
  - `payload.raw_order_id`
  - `payload.error`
- `executed_order_parse_failed`
  - `payload.order_id`
  - `payload.raw_order_type`
  - `payload.error`
- `order_submit_failed`
  - `payload.symbol`
  - `payload.side`
  - `payload.price`
  - `payload.size`
  - `payload.reason`
  - `payload.error`
- `order_cancel_failed`
  - `payload.symbol`
  - `payload.side`
  - `payload.order_id`
  - `payload.reason`
  - `payload.error`
- `order_flatten_failed`
  - `payload.symbol`
  - `payload.side`
  - `payload.size`
  - `payload.reason`
  - `payload.error`
- `order_blocked_safe_mode`
  - `payload.action`
  - `payload.symbol`
  - `payload.side`
  - `payload.order_id`
  - `payload.mode`
  - `payload.reduces_risk`
- `order_blocked_risk`
  - `payload.action`
  - `payload.symbol`
  - `payload.side`
  - `payload.order_id`
  - `payload.reason`

## Post-Run Review Checklist

For each live dry-run or live-order smoke run, inspect at least:

- did we get `app_started -> bootstrap_complete -> strategy_trace/session_metrics -> app_stopping`
- if startup had existing inventory, did `startup_position_baseline_applied` appear
- did any `*_failed` or `*_parse_failed` events appear
- did safe mode stay `normal`, or if not, why did `safe_mode_transition` fire
- for fills, do `order_fill`, `position_update`, `portfolio_snapshot`, and
  `session_metrics` agree on direction and magnitude
- are slippage and fee fields finite and in the expected units/sign convention
- are strategy decisions explainable from `strategy_trace.diagnostics.extra`
