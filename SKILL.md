---
name: compaction-eval
description: Score how well a Claude Code compaction kept what mattered, on the user's own past sessions, and log the result with the settings in force so compactions can be compared over time. Use whenever the user asks to evaluate, score, test or check a compaction or its summary ("was that compaction any good", "what did the summary lose", "did auto-compact drop anything", "compare idle compaction on and off", "run the compaction eval", "/compaction-eval"), wants evidence for choosing compaction settings, or asks for the compaction benchmark or log. Not for compacting a session, and not for summarising a transcript.
---

# Compaction eval

Scores one compaction of a Claude Code session on disk. Fresh headless agents answer questions and replay the next turns twice: once from what the compacted agent really had (summary plus preserved messages) and once from the full history before the compaction. A judge on a different model compares the answers. The result is a per-session report and one line in a longitudinal log.

You, the agent running this skill, only dispatch and relay. Never score from your own memory of the session, even when you are the session that was compacted: you are the side being measured.

The script is `scripts/compaction_eval.py` in this skill's folder (standard library Python 3). Read `references/method.md` when the user asks how the scoring works or what a number means.

## Steps

1. **Find the compaction.** If the user gave a session id or a transcript path, use it. Otherwise run `python3 <skill>/scripts/compaction_eval.py list` and ask which session. A session can hold several compactions; `plan` takes the latest unless `--boundary-line N` names another (`plan` prints the line numbers).

2. **Plan, which spends nothing.** Run `plan --session <id>` (plus `--boundary-line` if needed). Relay to the user, in plain words:
   - what the compaction was: trigger, tokens before and after, whether it looks like an idle compaction (`idle_signature`), the turns available after it;
   - the summary gate result (if it failed, the "summary" is not a summary, and scoring it would measure the wrong thing: say so and stop unless the user insists, then pass `--force`);
   - the two cost estimates, with the date of the price table, and the subscription sentence exactly as printed. If the table is flagged stale, offer `check-pricing` first.

3. **First run on this machine: the self-test.** If `log.jsonl` in the results folder (default `~/.claude/compaction-evals/`) has no line labelled `planted`, run the self-test before any real score, and tell the user why: a check that has never been seen to fail has not been shown to work.
   - `plant --session <id> [--boundary-line N] --mode gut --out <results>/planted-gut.txt` writes a summary with the content removed.
   - `run --session <id> [--boundary-line N] --summary-override <results>/planted-gut.txt --counterfactual no` scores it.
   - Its retention must come out low, near what the no-context agent knows. If it does not, stop and report: the eval is not discriminating.

4. **The counterfactual asks first.** The probe runs without asking. The counterfactual (replaying the next turns on both sides) costs more: ask the user, giving the difference between the two estimates, unless `counterfactual_auto_below_usd` in the config is set and the estimate is below it.

5. **Run.** `run --session <id> [--boundary-line N] --counterfactual yes|no`. After a self-test, add `--reuse <self-test run folder>` so the real run reuses the same items and full-side answers (comparable, and cheaper). Runs take several minutes: start the command in the background and wait for it to finish.

6. **Report.** Show the headline (weighted retention, critical losses, divergence minus noise floor, incident rate), the note, the actual cost against the estimate, and the path of `report.md`. Read the report before summarising it. Say which numbers rest on few items or few turns.

## Rules

- **The transcript is never written.** The script reads it and writes temporary copies under `~/.claude/projects/<work folder>/`, deleted at the end of the run. If anything else would need to write to a transcript, stop and ask.
- **A live session's transcript keeps growing.** The run records the md5 and line count of what it read; do not report that a transcript "changed" without comparing that prefix.
- **Two numbers are never averaged together.** Retention (what the agent could answer) and divergence (what it would do) answer different questions.
- **Compare like with like.** When reading the log, group by trigger, idle signature and context size before comparing; say when a comparison mixes groups.
- **Numbers are shareable, notes are not.** The report and the note quote the user's session. Do not paste them anywhere public without the user's say.

## Configuration

`~/.claude/compaction-evals/config.json` (all keys optional): `results_dir`, `work_dir`, `claude_bin`, `extractor_model`, `extractor_effort`, `judge_model`, `judge_model_alt`, `judge_effort`, `turns`, `noise` (`first` or `all`), `counterfactual_auto_below_usd`, `price_max_age_days`, `call_timeout_s`. Defaults are at the top of the script.
