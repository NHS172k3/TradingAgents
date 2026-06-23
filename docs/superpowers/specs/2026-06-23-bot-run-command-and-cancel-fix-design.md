# Telegram bot: explicit `/run` command + cancel fix

## Context

The on-demand Telegram bot (`automation/bot.py`, `automation/job_queue.py`)
currently treats any message that isn't a recognized `/command` as an
implicit ticker request: `_handle_update` falls through to
`_handle_ticker_message`, which regex-matches 1-3 ticker-like tokens out of
free text and queues them. `/cancel` is also incomplete: it only removes a
job still waiting in the in-memory queue (`JobQueue.cancel_last_for_user`),
so canceling a job that's already running silently no-ops, and canceling a
*queued* job never refunds the daily-cap usage that was charged when it was
enqueued.

This spec covers two fixes:

1. Require an explicit command to run an analysis; reject everything else.
2. Make `/cancel` behave correctly whether the user's most recent job is
   queued or already running, and refund usage in both cases.

Out of scope: a serverless architecture analysis (tracked separately, not a
build).

## 1. Explicit `/run` command

- Add `/run TICKER [TICKER ...]` (registered via `setMyCommands`), accepting
  1-3 symbols — same parsing/limit as today (`_parse_symbols`,
  `MAX_SYMBOLS_PER_MESSAGE`) and the same allowlist/daily-cap/burst-rate
  checks as `_enqueue_symbols` already performs.
- `/watchlist run` is unchanged (already command-gated).
- Remove the bare-ticker fallback in `_handle_update`: any message that does
  not match a known command (including a plain ticker like `NVDA` with no
  `/run`) gets a single rejection reply pointing at `/run` and `/help`,
  instead of being silently parsed as a ticker.
- `_handle_ticker_message` is renamed/repurposed as the body of the `/run`
  handler (parses the remainder of the command text instead of the whole
  message).
- `_help_text` and the `_COMMANDS` list gain the new command.

## 2. Cancel fix

Both cases are handled by one `JobQueue` method,
`cancel_for_user(user_id) -> CancelResult`, replacing
`cancel_last_for_user`:

- **Queued case** (job still in `self._jobs`): pop it, same as today, and
  additionally refund the daily-cap usage charged at enqueue time (new
  `Store.decrement_usage(user_id)`, mirroring `check_and_increment_usage`).
- **Active case** (job is `self._active_job` and belongs to the calling
  user): set a `cancel_requested` flag on the job (the `Job` dataclass needs
  a mutable cancellation marker — e.g. a `threading.Event` field, since
  `Job` is otherwise frozen). The run itself is **not** interrupted — there
  is no interruption point inside `runner.run_one_ticker`'s blocking call
  into the upstream pipeline, and building one (subprocess execution +
  kill) is a disproportionate lift for a single-worker queue where the next
  job has to wait for the current one to finish regardless. Instead:
  - `_process` checks the event after the run completes; if set, it skips
    `on_complete` (no result message sent) and refunds usage instead of
    recording a report.
  - The bot replies immediately to the `/cancel` itself: "stopping
    notifications for this run — it's already executing so it can't be
    killed early, but you won't be charged for it and won't get a result
    message."
- If the user has neither an active nor a queued job, reply as today:
  "you don't have anything queued to cancel."
- If a user has multiple queued jobs (e.g. from `/watchlist run` with
  several tickers), `/cancel` continues to cancel only the most recently
  queued one (LIFO), matching current behavior — canceling all of a user's
  queued jobs at once is not requested and out of scope.

## Testing

- Unit tests for `JobQueue.cancel_for_user`: queued-job cancel refunds
  usage; active-job cancel suppresses `on_complete` and refunds usage;
  no-job case returns nothing to cancel.
- Unit tests for `bot._handle_update` dispatch: `/run NVDA` enqueues;
  bare `NVDA` (no command) is rejected with the help pointer; unrecognized
  `/foo` is rejected.
- Existing `Store` tests get a case for `decrement_usage` (floor at 0,
  doesn't go negative if called without a prior increment same day).
