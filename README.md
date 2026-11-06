# compaction-eval

A Claude Code skill that checks what a compaction lost, on your own sessions, so you can choose your compaction settings from evidence.

> **Draft, not yet reviewed.** Claude (Anthropic's model) wrote this README, and Maxime Delavergne has not reviewed it yet. Publishing it unreviewed is the first step of his publishing process: the work is public while it is still being checked. The skill is also early: it has scored one real compaction and passed one self-test, on one person's sessions. Treat its numbers as a starting point.
>
> **Who did what.** The idea is Maxime's. The design is a collaboration between Maxime and Claude. Claude built it, with Maxime supervising.

## Why this exists

When a Claude Code session gets long, Claude Code compacts it: it replaces the conversation history with a summary, so the session can go on with room to spare. It can also compact a long session that has sat idle for a while. A summary is shorter than the history, so something is always left out. Sometimes it doesn't matter. Sometimes it is the one file path, decision or "never do X" that the rest of the work depended on.

Compaction is a trade-off, and you can move it. Claude Code exposes the settings, more openly than most tools, though not always clearly: how large the context can grow before it compacts (`autoCompactWindow`), whether idle sessions get compacted (`idleCompaction`) and from what size (`CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS`). A larger window keeps more history, and every message then carries that larger history, which uses more of your plan or your budget. Turning idle compaction off keeps the full history after a break, and the whole of it is processed again when you come back. Which balance suits you depends on how you work: long single sessions or many short ones, work that leans on exact ids and paths or on broad direction.

From inside a session, you can't see what a compaction lost. The agent that lives on the summary doesn't know what is missing, and asking it to grade itself gives a guess. This skill measures it from the outside, on the transcript Claude Code keeps on disk, and logs each result with the settings that were in force. Over several sessions, the log shows whether your settings lose things that matter to you.

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

The report adds a short note on what kind of thing was lost. The log line records the trigger, the token counts before and after, the model and effort, the Claude Code version and your compaction settings, so later you can compare like with like.

**First results (one session, read as an example only).** A 429,000-token session compacted to 54,000. Weighted retention 94.6%, with 1 critical loss among 33 scored items: the things lost were exact identifiers, while rulings, decisions and open tasks were kept. In the replay, the agent with the summary acted differently from the agent with the full history on all three turns (divergence 2 on a 0 to 3 scale, against a noise floor of 1), but the judge found the summary side's plan the better fit on two of them. The self-test, with the summary gutted on purpose, scored 42.9% and 9 critical losses, so the eval does tell a bad summary from a good one.

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
3. runs the self-test the first time (a deliberately damaged summary must score low before any real score is trusted);
4. runs the probe, and asks before the more expensive replay;
5. shows the headline numbers and where the report is.

The script also runs on its own:

```bash
python3 ~/.claude/skills/compaction-eval/scripts/compaction_eval.py list
python3 ~/.claude/skills/compaction-eval/scripts/compaction_eval.py plan --session <session id>
python3 ~/.claude/skills/compaction-eval/scripts/compaction_eval.py run --session <session id> --counterfactual no
```

Results go to `~/.claude/compaction-evals/` unless you set another folder in `~/.claude/compaction-evals/config.json`.

## Cost

Each run sends your session, at full size, to the model several times, so it is not cheap on a large session. `plan` prints an estimate at API list prices before anything runs, with the date those prices were taken. `check-pricing` compares them with Anthropic's current pricing page.

Measured on the first session (425,000 tokens of context, answers on Opus 5.5): the self-test probe cost $7.39, the real probe $1.30 (reusing the self-test's questions), and the probe plus the three-turn replay $13.82. The cost grows with the size of the session.

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

What this skill adds: it scores the compactions you actually had, on your own work, and logs them with your settings so they can be compared over time.

## Credits and licence

Idea: Maxime Delavergne. Design: Maxime Delavergne and Claude. Build: Claude (Opus 5.5, in Claude Code), supervised by Maxime Delavergne. MIT licence, see [LICENSE](LICENSE).
