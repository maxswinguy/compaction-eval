---
name: compaction-eval
description: Score how well a Claude Code compaction kept what mattered, on the user's own past sessions, and log the result with what is known of the settings at that compaction, so compactions can be compared over time. Use whenever the user asks to evaluate, score, test or check a compaction or its summary ("was that compaction any good", "what did the summary lose", "did auto-compact drop anything", "compare idle compaction on and off", "run the compaction eval", "/compaction-eval"), wants evidence for choosing compaction settings, asks for an overview or report across the compactions scored so far, or asks for the compaction benchmark or log. Not for compacting a session, and not for summarising a transcript.
---

# Compaction eval

Scores one compaction of a Claude Code session on disk. Fresh headless agents answer questions and replay the next turns twice: once from what the compacted agent really had (summary plus preserved messages) and once from the full history before the compaction. A judge on a different model compares the answers. The result is a per-session report and one line in a longitudinal log.

You, the agent running this skill, only dispatch and relay. Never score from your own memory of the session, even when you are the session that was compacted: you are the side being measured.

The script is `scripts/compaction_eval.py` in this skill's folder (standard library Python 3). Read `references/method.md` when the user asks how the scoring works or what a number means.

## Steps

1. **Find the compaction.** If the user gave a session id or a transcript path, use it. Otherwise run `python3 <skill>/scripts/compaction_eval.py list` and ask which session. A session can hold several compactions; `plan` takes the latest unless `--boundary-line N` names another (`plan` prints the line numbers).

2. **Plan, which spends nothing.** Run `plan --session <id>` (plus `--boundary-line` if needed). Relay to the user, in plain words:
   - what the compaction was: trigger, tokens before and after, whether it looks like an idle compaction (`idle_signature`), the turns available after it;
   - the warning, when `plan` prints one, that Claude Code kept the agent's last reply word for word: when that reply is a recap, a real score credits what it carries to the compaction, not to the summary alone;
   - the summary gate result (if it failed, the "summary" is not a summary, and scoring it would measure the wrong thing: say so and stop unless the user insists, then pass `--force`);
   - the two cost estimates and the price of a run that reuses a self-test's questions, with the date of the price table, and the subscription sentence exactly as printed. If the table is flagged stale, offer `check-pricing` first;
   - the warning, when `plan` prints one, that the replay has only one short message: the extra cost buys little;
   - what the compaction saved (`What the compaction saved`), with its limit in the same breath: it is an estimate over the calls that really followed, not a bill. Losses without savings are half of the trade-off the user is here to set;
   - what the transcript proves about the settings at that compaction (`Known from the transcript`). Do not present the current settings file as the settings in force then.

3. **On every compaction, the self-test first.** Run `self-test-status --session <id> [--boundary-line N]`. Exit 1 means no fair self-test has passed on this compaction (a pass on another compaction does not count, nor a failed one, one logged before the verdict existed, before every route was scrubbed, or above today's bar): run the self-test before the real score, and tell the user why: whether the eval can tell an empty summary from a full one depends on the session (its kept reply, its re-attached files), and a check that has never been seen to fail has not been shown to work. `run` refuses a real score on a compaction without a fair pass unless `--unvalidated` is given, and the log and report then mark it. On 2026-11-07, real runs on two compactions went ahead on another compaction's pass, and their own self-tests then failed (48.9%, 51.1%).
   - `plant --session <id> [--boundary-line N] --mode gut --out <results>/planted-gut.txt` writes a summary with the content removed.
   - `run --session <id> [--boundary-line N] --summary-override <results>/planted-gut.txt --counterfactual no` scores it. The summary is not the only way the session's content reaches the compacted agent, so with an override the run scrubs every other route too: the messages Claude Code keeps word for word after the summary (the agent's last reply and the user's answer), re-attached files, notes naming files read before, the invoked skills with their arguments, and the original session id. With those kept, gutted summaries scored 41 to 72% (2026-11-07). `plan` prints them (`kept_messages`, `kept_reply_chars`, `reattached_files`, `session_attachments`).
   - Before spending anything, the run checks for leaks for free: Claude Code builds the summary-side request against a local catcher that answers with an error, so nothing reaches the API, and the script searches it for the scrubbed text. If anything is still there, the run stops and names it. `leak-check --session <id> [--boundary-line N]` runs this check alone.
   - The run prints `Self-test: PASSED` or `FAILED` with the reasons (retention above `self_test_max_retention_pct`, default 10; fewer than `self_test_min_items` scored items; an item's answer still reachable by another route). On FAILED, stop and report: the eval is not discriminating on this compaction. A pass on a compaction with few scored items can be retried on another session.

4. **The counterfactual asks first.** The probe runs without asking. The counterfactual (replaying the next turns on both sides) costs more: ask the user, giving the difference between the two estimates, unless `counterfactual_auto_below_usd` in the config is set and the estimate is below it.

5. **Run.** `run --session <id> [--boundary-line N] --counterfactual yes|no --reuse <self-test run folder>` (`self-test-status` prints the folder), so the real run reuses the self-test's items and full-side answers: the self-test then vouches for the same questions, and the run is cheaper. Runs take several minutes: start the command in the background and wait for it to finish.

6. **Report.** Show the headline (weighted retention, critical losses, divergence minus noise floor, incident rate), what the compaction saved beside what it lost (`tradeoff --session <id> [--boundary-line N]` prints both, free, for any compaction with a logged real score), the note, the actual cost against the estimate, and the path of `report.md`. Read the report before summarising it. Say which numbers rest on few items or few turns.

## Overview across compactions

When the user asks how compaction has gone overall, or what to make of the compactions scored so far, run `overview` (add `--out <file>` to save it) and relay it as it stands. It is built from the log, spends nothing and works at any point. It counts only real scores a fair self-test vouches for on the same questions; the rest are named as left out, because an unvalidated score could not be told from an empty summary. Read the report before summarising it, and keep its section on what the data cannot support.

## Rules

- **The transcript is never written.** The script reads it and writes temporary copies under `~/.claude/projects/<work folder>/`, deleted at the end of the run. If anything else would need to write to a transcript, stop and ask.
- **A live session's transcript keeps growing.** The run records the md5 and line count of what it read; do not report that a transcript "changed" without comparing that prefix.
- **Two numbers are never averaged together.** Retention (what the agent could answer) and divergence (what it would do) answer different questions.
- **Compare like with like.** When reading the log, group by trigger, idle signature and context size before comparing; say when a comparison mixes groups.
- **Read past results through `history`, not the raw log.** It groups the runs by compaction (by session name where one exists) and says, for each, whether a fair self-test vouches for it, how long the kept reply was, and which report carries a wrong cost figure. The log lines stay as they were written.
- **The log's `settings` field is the settings file when the eval ran, not the compaction's.** Claude Code writes no settings snapshot into the transcript. `at_compaction` holds what the transcript does prove (trigger, model, effort, version, an idle compaction's notice, a lower bound on the window). Say which of the two you are quoting.
- **Numbers are shareable, notes are not.** The report, the note and the overview quote the user's session. Do not paste them anywhere public without the user's say.

## Configuration

`~/.claude/compaction-evals/config.json` (all keys optional): `results_dir`, `work_dir`, `claude_bin`, `extractor_model`, `extractor_effort`, `judge_model`, `judge_model_alt`, `judge_effort`, `turns`, `noise` (`first` or `all`), `counterfactual_auto_below_usd`, `price_max_age_days`, `call_timeout_s`, `self_test_max_retention_pct`, `self_test_min_items`, `kept_reply_warn_chars`, `replay_short_chars`, `capture_timeout_s`. Defaults are at the top of the script.
