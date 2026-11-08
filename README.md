# compaction-eval

A Claude Code skill that checks what a compaction lost, on your own sessions, so you can choose your compaction settings from evidence.

> **Draft, not yet reviewed.** Claude (Anthropic's model) wrote this README, and Maxime Delavergne has not reviewed it yet. Publishing it unreviewed is the first step of his publishing process: the work is public while it is still being checked. The skill is also early: it has scored five real compactions and passed a fair self-test on each of them, all on one person's sessions and all idle compactions. Treat its numbers as a starting point.
>
> **Who did what.** The idea is Maxime's. The design is a collaboration between Maxime and Claude. Claude built it, with Maxime supervising.

## Why this exists

When a Claude Code session gets long, Claude Code compacts it: it replaces the conversation history with a summary, so the session can go on with room to spare. It can also compact a long session that has sat idle for a while. A summary is shorter than the history, so something is always left out. Sometimes it doesn't matter. Sometimes it is the one file path, decision or "never do X" that the rest of the work depended on.

Compaction is a trade-off, and you can move it. Claude Code exposes the settings, more openly than most tools, though not always clearly: how large the context can grow before it compacts (`autoCompactWindow`), whether idle sessions get compacted (`idleCompaction`) and from what size (`CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS`). A larger window keeps more history, and every message then carries that larger history, which uses more of your plan or your budget. Turning idle compaction off keeps the full history after a break, and the whole of it is processed again when you come back. Which balance suits you depends on how you work: long single sessions or many short ones, work that leans on exact ids and paths or on broad direction.

From inside a session, you can't see what a compaction lost. The agent that lives on the summary doesn't know what is missing, and asking it to grade itself gives a guess. This skill measures it from the outside, on the transcript Claude Code keeps on disk, and logs each result with what can be known of the settings at that compaction. Over several sessions, the log and its overview show what your compactions gave up and what they saved.

**What it is not.** It is not a scientific benchmark of compaction methods, and it is not meant to improve how compaction works: that is the work of the teams who build it. It is a decision tool for one user's settings. Until compaction stops needing trade-offs, choosing the trade-off is yours, and this gives you something to choose with.

**A starting point.** Your way of working may call for changes to the skill itself: different questions, different weights, more turns replayed. The prompts and the scoring are plain files you can edit (see [Adapting it](#adapting-it)).

## What you get

For each compaction you score, a readable report and one line in a log file.

| Number | What it tells you |
|---|---|
| Weighted retention (%) | Of the things that mattered before the compaction, how much the agent could still answer after it. Important items count more. |
| Critical losses | How many items whose loss would cause a wrong action came back lost or distorted. |
| Divergence minus noise (optional) | Replaying the next few messages, how differently the agent acted with the summary than with the full history, beyond the difference you get anyway from running the same thing twice. |
| Incidents | Later messages where you had to restate or correct something the summary dropped. |
| Net saved ($, estimated) | What the calls after the compaction would have cost more with the full history, at API list prices, minus an estimate of what writing the summary cost. |

**What a real score measures.** The agent after a compaction has more than the summary: Claude Code also keeps the last few messages word for word (often the agent's last reply and your answer to it) and re-attaches the files used most recently. A real score counts everything the agent had, so when the kept reply is a recap or the re-attached files hold the session's facts, part of the score is theirs, not the summary's. `plan` warns when the kept reply is long, and every report lists the kept messages and re-attached files.

**What the saving measures.** It takes the calls that really followed the compaction and adds back the history the compaction removed, so you see what the compaction lost beside what it saved. It is an estimate, not a bill: a session that kept its full history might have gone differently, and would have reached its auto-compact limit and compacted anyway. On a Claude subscription nothing is billed per call, so read it as how much more of your usage limits the same work would have used.

**Across compactions.** `overview` writes a plain-language report from the log, at any point and for free: what your compactions gave up and saved, what survived by kind, the critical items, what this means for each setting, and what the data cannot support. It counts a real score only when a fair self-test vouches for it on the same questions.

The report adds a short note on what kind of thing was lost. The log line records what the transcript proves about that compaction, so later you can compare like with like: the trigger, the token counts before and after, the model and effort, the Claude Code version, the idle-compaction notice, and bounds on the window and on the idle threshold. It also records your settings file as it stood when the eval ran, labelled as such. Claude Code writes no snapshot of the settings into the transcript, so that file can differ from what applied at the compaction.

**First results (five compactions on one person's sessions, read as examples only).** Of 174 scored items, the agent kept 147, blurred 9 and lost 18; 4 of those 27 would have led to a wrong action. Weighted retention ran from 79.3% to 100%. Rulings held best (15 of 15) and facts worst (44 of 58). At API list prices, the compactions saved an estimated $2.63 to $10.92 each on the calls that followed, about $31 in all. All five were idle compactions and nothing was compared under two settings, so these results do not say which setting is better, and they say nothing about `autoCompactWindow`.

The first of the five: a 429,000-token session compacted to 54,000. Weighted retention 94.6%, with 1 critical loss among 33 scored items: the things lost were exact identifiers, while rulings, decisions and open tasks were kept. In the replay, the agent with the summary acted differently from the agent with the full history on all three turns (divergence 2 on a 0 to 3 scale, against a noise floor of 1), but the judge found the summary side's plan the better fit on two of them. The first self-test on that session, with only the summary gutted, scored 42.9%. It was not a fair test: the session still reached the agent through the kept messages and the re-attached files, and on other compactions the same leak gave a gutted summary 41% to 72%. With every route scrubbed, gutted summaries scored between 0.0% and 2.2% on each of the five compactions (33 to 36 scored items each, against a bar of 10%), so the eval does tell an empty summary from a full one. On the 429,000-token session itself, the fair self-test scored 0.0% of 33.

## Scope: where it was tested

- **Claude Code only.** It reads Claude Code's transcript files and replays them with the `claude` command-line tool. It was built and tested on macOS with Claude Code.
- **Not tested with Claude Cowork** (Anthropic's desktop app for knowledge work). Whether Cowork compacts the same way, and whether its sessions can be read and replayed like this, has not been checked.
- **Not tested with other assistants** (ChatGPT, Codex, Gemini and others). The skill itself won't run there.
- **The method should carry over.** Wherever you can get the full history and the summary, and can restart an agent from each, you can ask both the same questions and compare.

## How it works

1. **Find the compaction.** The skill reads the session's transcript file and finds where the summary replaced the history. The transcript keeps both.
2. **Rebuild two agents.** It writes two temporary copies of the session: one as it stood right after the compaction (summary plus the few messages Claude Code keeps), one with the full history up to the compaction. It restarts each one headlessly, with tools, MCP servers and hooks turned off, so the only difference between the two is the compaction.
3. **Probe.** A separate agent reads the full history and lists 20 to 40 things a colleague would need to carry on: rulings, decisions, open tasks, exact ids and paths, constraints. Each becomes a question. Three agents answer: the summary side, the full side and an agent with no session at all. "UNKNOWN" is an allowed answer, so a gap shows as a gap and not as a guess. Questions the full side fails, or the no-context agent passes, are dropped as bad questions.
4. **Grade blind.** Exact answers (paths, ids, URLs) are checked by string match. The rest go to a judge on a different model, who sees the three answers under shuffled labels.
5. **Replay (optional, asks first).** For the first few messages you sent after the compaction, both sides reply to the real message and say what they would do. A judge rates how far the two plans diverge. Two full-history replies are also compared, to measure the noise you get from running the same thing twice.
6. **Report and log.** The temporary copies are deleted, and the report and the log line are written.

The details, and the method's limits, are in [references/method.md](references/method.md).

## Install

You need Python 3 and the `claude` command-line tool (Claude Code). Nothing else.

```bash
git clone https://github.com/maxswinguy/compaction-eval ~/.claude/skills/compaction-eval
```

Claude Code finds skills in `~/.claude/skills/` on its own; there is nothing to register.

## Use

In a Claude Code session, ask "run the compaction eval" or "what did that compaction lose?", or type `/compaction-eval`. The skill:

1. lists your sessions that have a compaction, or uses the one you name;
2. shows what the compaction was and a priced estimate, before spending anything;
3. runs the self-test on each compaction before its real score: a deliberately emptied summary must score low on that compaction before its real score is trusted, since what leaks through depends on the session. The self-test also removes every other route by which the session reaches the agent (the kept messages, re-attached files, notes naming files, the skills invoked with their arguments, the session id), and a free leak check confirms nothing slips through before anything is spent;
4. runs the probe, and asks before the more expensive replay;
5. shows the headline numbers and where the report is.

The script also runs on its own:

```bash
python3 ~/.claude/skills/compaction-eval/scripts/compaction_eval.py list
python3 ~/.claude/skills/compaction-eval/scripts/compaction_eval.py plan --session <session id>
python3 ~/.claude/skills/compaction-eval/scripts/compaction_eval.py run --session <session id> --counterfactual no
python3 ~/.claude/skills/compaction-eval/scripts/compaction_eval.py leak-check --session <session id>
python3 ~/.claude/skills/compaction-eval/scripts/compaction_eval.py self-test-status --session <session id>
python3 ~/.claude/skills/compaction-eval/scripts/compaction_eval.py history
python3 ~/.claude/skills/compaction-eval/scripts/compaction_eval.py tradeoff --session <session id>
python3 ~/.claude/skills/compaction-eval/scripts/compaction_eval.py overview
```

`leak-check` is free: Claude Code builds the self-test's request against a local catcher that answers with an error, so nothing reaches the API, and the script searches it for session content. `self-test-status` exits 0 only when a self-test has passed on that compaction with every route scrubbed; `run` refuses a real score without one unless you pass `--unvalidated`. `history` lists every logged run by compaction, with what each result can be trusted for today. `tradeoff` shows one compaction's logged score beside what it saved, and `overview` writes the report across all of them; both are free.

Results go to `~/.claude/compaction-evals/` unless you set another folder in `~/.claude/compaction-evals/config.json`.

## Cost

Each run sends your session, at full size, to the model several times, so it is not cheap on a large session. `plan` prints an estimate at API list prices before anything runs, with the date those prices were taken. It also prices a run that reuses a self-test's questions, the one the normal flow pays (on the Substack Reader compaction: $1.69 for the probe, $8.74 with the replay), and warns when the replay would be a single short message. `check-pricing` compares them with Anthropic's current pricing page.

Measured on the first session (425,000 tokens of context, answers on Opus 5.5): the self-test probe cost $7.39, the real probe $1.30 (reusing the self-test's questions), and the probe plus the three-turn replay $13.82. Re-running a self-test on its earlier questions (`--reuse`) cost $1.09 to $1.21, with a summary side of 102,000 to 113,000 tokens. The cost grows with the size of the session.

Each report gives two cost figures: one from the price table, and Claude Code's own. Before 2026-11-07, Claude Code's own figure could be far too high (a $10.47 run reported $108.56), because the temporary copies carried the session's saved running cost, which Claude Code adds back on resume. The copies now leave it out; the price-table figure was right throughout.

On a Claude subscription, a run is not billed separately; it counts against your plan's usage limits. If usage credits are enabled, usage beyond your limit is billed at API rates.

## Safety and privacy

- **Your transcripts are never modified.** The skill reads them and writes temporary copies to its own folder under `~/.claude/projects/`, deleted at the end of each run. It records a checksum of what it read.
- **Reports quote your sessions.** Keep them private unless you mean to share them. The numbers in the log can be shared without the notes.

## Adapting it

Everything that decides the score is a plain file:

- `prompts/` holds the instructions given to the extractor, the answering agents and the judges. Change what counts as "must carry" in `prompts/extract.md`.
- The severity weights (1, 2, 4) and the credit for a distorted answer (0.5) are at the top of `scripts/compaction_eval.py`.
- `config.json` sets the extractor and judge models, the number of turns replayed and the results folder.

If you change the scoring, start a new log file: scores from different rules don't compare.

## Related work

- Factory's probe-based evaluation of context compression (recall, artifact, continuation and decision probes, judged blind): https://factory.com/news/evaluating-compression
- CompactBench, an open benchmark of compaction methods with oracle and null controls: https://github.com/compactbench/compactbench
- compact-handoff-plugin, which A/B tests compaction prompts against an answer key: https://github.com/AnExiledDev/compact-handoff-plugin
- Mayur Agnani's fork-and-probe test, with UNKNOWN allowed: https://dev.to/theaicommit/how-to-tell-what-claude-code-and-codex-forget-when-they-compact-a-session-532a

What this skill adds: it scores the compactions you actually had, on your own work, and logs them with what can be known of your settings, so they can be compared over time.

## Credits and licence

Idea: Maxime Delavergne. Design: Maxime Delavergne and Claude. Build: Claude (Opus 5.5, in Claude Code), supervised by Maxime Delavergne. MIT licence, see [LICENSE](LICENSE).
