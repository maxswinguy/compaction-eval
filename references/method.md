# How compaction-eval scores a compaction

## Two sides, rebuilt exactly

Claude Code keeps the whole transcript after a compaction: the messages before it stay in the file, followed by a `compact_boundary` record and the summary. The eval rebuilds two contexts from it, as headless `claude -p` resumes of temporary copies:

- **Summary side:** the transcript as it stands after the compaction, cut just before the agent's first reply. This is exactly what the compacted agent had: the summary, the preserved messages and the re-attached context.
- **Full side:** the transcript up to the compaction, without the boundary, so the agent sees the full history it had just before.

Both run with tools off, no MCP servers, hooks off and the auto-compact window at 1M, on the session's own model and effort (read from the transcript). They load the same current system prompt, CLAUDE.md files and skills, so the only difference between the two sides is the compaction. Each side is one copy reused for all its calls, so calls after the first read the prompt cache.

## Probe: what the agent could answer

1. An extractor (default Opus 5.5, high effort) reads the full side and lists 20 to 40 must-carry items: rulings, decisions, open tasks, exact ids and paths, constraints, facts. Each has a severity (2 = forgetting it causes a wrong action, 1 = it costs time, 0 = harmless), a question that does not give the answer away, and a reference answer.
2. The same questions go to three agents: the summary side, the full side (the ceiling) and an agent with no session at all (the floor). UNKNOWN is a valid answer.
3. Exact items (a path, an id, a number) are graded by string match. The rest go to a judge on a different model, who sees the reference and the three answers under shuffled letters, and grades each kept, distorted or lost.
4. An item is dropped if the full-side agent missed it (a bad question) or the no-context agent got it (it does not depend on the session).
5. **Weighted retention** = the summary side's credit (kept 1, distorted 0.5, lost 0) weighted by severity (1, 2, 4), over the scored items. **Critical losses** = severity-2 items the summary side lost or distorted.

## Counterfactual: what the agent would do

For each of the first N user turns after the compaction (default 3), both sides reply to the real message with the real history before it, describing the actions they would take instead of running tools. Turn k's full side is the full history plus the real turns 1 to k−1, re-attached after the last message before the compaction. A judge rates the divergence between the two replies from 0 (same plan) to 3 (one is wrong for the situation) and says which fits the session better.

On turn 1, two full-side replies are also compared: the **noise floor**, how far two runs with the same context drift apart anyway. Divergence below the noise floor means no detectable loss.

## Incidents

Later user messages that restate or correct something established before the compaction, counted by the judge against the item list, per message.

## Self-test

`plant` writes a damaged summary (`gut`: content removed; `drop`: lines matching patterns removed) and `run --summary-override` scores it. A gutted summary must score low. Until it has, no real score is trusted.

The summary is not the only route by which the session's content reaches the compacted agent, so the self-test scrubs the others too. A self-test that leaves one open is not a fair control: it tests the route, not the eval. The routes, all measured on 2026-11-07:

- **Kept messages.** Claude Code keeps the last messages word for word after the summary (`compactMetadata.preservedMessages`). They sit before the boundary in the file and are re-linked when the session loads. On four compactions they held the agent's last reply (1,505 to 5,842 characters, a closing recap three times) and the user's answer to it ("Yes, close them" answered an item exactly). With them and the next routes kept (only re-attached files blanked), gutted summaries scored 41.3 to 51.1% on three of them.
- **Re-attached files** (recently read or written files). A session that had written its own handoff files kept 14 of 15 checked facts in them, and a gutted summary scored 71.9%.
- **Notes naming a file read before** (`compact_file_reference`), and the **names of the re-attached files**. A blanked file's path still answered an item exactly.
- **The skills invoked before the compaction**, with the arguments they were called with (an Outliner node id answered an item).
- **The original session id**, in the stored environment block (the scratchpad path). It answered an item asking for the session's id prefix.

The self-test scrubs the text of the kept messages (their thinking included), every attachment after the boundary that is not setup (the instructions, tool and skill listings and environment stay), and replaces the original session id by the copy's. A record that holds no text where the scrub looks stops the run, because the format may have changed. `--keep-reattached` keeps all routes, for a diagnosis only; such a run never passes.

**The leak check.** Before anything is spent, Claude Code builds the summary-side request three times with `ANTHROPIC_BASE_URL` pointing at a catcher on 127.0.0.1, which keeps the request and answers with an error, so nothing reaches the API: from an unscrubbed copy, from the scrubbed copy, and with no session. The removed text's longest lines must be found in the first (the control: the search can see) and not in the second; text that the no-session request or the setup records also carry is not counted. Once the items exist, any item whose literal answer is still in the scrubbed request is listed and fails the verdict. `leak-check` runs the check alone. On the four compactions above, the old self-test's request still held 17 to 28 of the removed lines and the answers of 4 or 5 items each; the scrubbed request held none.

The verdict is written to the run, the report and the log (`self_test_passed`). The no-context side scores 0 by construction (items it answers are dropped), so the bar is near 0: retention at or below `self_test_max_retention_pct` (default 10) with at least `self_test_min_items` scored items (default 10). The bar was 25 until 2026-11-07; that day four fair self-tests scored 0.0% and five with routes left open scored 25.0 to 71.9%, so 25 let a leaking self-test pass and 10 fails all five. `self-test-status --session <id>` reads the log and exits 0 only when a self-test passed on that compaction with every route scrubbed (its log line has `kept_messages`) and at or below the current bar.

**One self-test per compaction.** A pass counts only for the compaction it ran on, because whether the scrub closes every route depends on the session: how long the kept reply is, which files come back, which skills ran. Until 2026-11-07 one pass anywhere opened the gate for every later compaction; that day, real runs on two compactions went ahead on another compaction's pass, and their own self-tests, run afterwards, failed. `run` now refuses a real score on a compaction with no fair pass (`--unvalidated` overrides it, and the log line's `validated_by` is then empty). A real run made with `--reuse` of the passing self-test answers the same questions the self-test vouched for.

**Reading past results.** `history` groups the log by compaction and gives each run's reading today: self-test counted or not and why; real score validated on the same questions, validated on other questions, or not validated; the kept reply's length when any run on that compaction recorded it; a report whose Claude Code cost figure is wrong. The log lines are never rewritten. A self-test that failed because routes were left open still says something: it is roughly what those routes alone carry, so it bounds how much of the same compaction's real score may come from them rather than from the summary.

**Reading a real score.** A real run keeps every route, because the compacted agent had them, and the report lists the kept messages and re-attached files. Its retention therefore measures the summary plus those routes, not the summary alone. When the kept reply is a recap or the re-attached files hold most of the session's facts, say so beside the number; `plan` warns when the kept reply is longer than `kept_reply_warn_chars` (default 1000).

**Cost figures.** The report gives the cost at the price table and Claude Code's own figure. Before 2026-11-07 the copies carried the session's `cost-state` records, which Claude Code restores on resume and adds to every call's total, so its own figure could be far too high (a $10.47 run reported $108.56). The copies now leave those records out; the price-table figure was right throughout.

## What the compaction saved

`plan` and `tradeoff` print it, free, from the token counts the transcript records for each API call; new runs add it to the report and the log (`cost_side`).

1. Take every call of the main conversation after the compaction, up to the next compaction, the end of the session, or the point where the session starts evaluating itself (`eval_started_line`: those calls are the eval's, not the session's). A call streamed over several records counts once; a subagent's call is left out, because the compaction does not touch it. Price each at the API table: that is what the calls cost.
2. **History removed** = context size on the last call before the compaction, minus on the first call after it. Without the compaction, every one of those calls would have carried that much more.
3. That extra was either read from the prompt cache (cache-read price) or, when the gap since the previous call exceeds the cache's lifetime (read from the calls: 1 hour if they wrote 1-hour cache entries, otherwise 5 minutes), written again (cache-write price).
4. **Saved** = the sum of those extras. **Net saved** subtracts the cost of writing the summary, which the transcript does not record: it is estimated as one cached read of the history plus the summary's length as output (4 characters a token). On the one compaction where a running-cost record brackets that call (Substack Reader, 2026-10-28), the unexplained remainder was $0.29 against an estimate of $0.23.

**The limit, printed wherever the number appears.** It is an estimate, not a bill: the calls are the ones that really followed. A session that had kept its full history might have gone differently, and would have reached its auto-compact limit and compacted anyway (the `peak_context_without_tokens` field shows how far it would have gone). The prices are API list prices; on a subscription nothing is billed per call.

## What is known about the settings at compaction time

Claude Code writes no settings snapshot into the transcript, so the log's `settings` field is the settings file when the eval ran. It can be wrong for the compaction: on the Substack Reader compaction (2026-10-28) the log says `autoCompactWindow: 1000000`, while that setting first appears in Maxime's settings history on 2026-11-02. `at_compaction` holds only what the transcript itself proves:

| Setting | From the transcript | Otherwise |
|---|---|---|
| Trigger (auto or manual) | exact: `compactMetadata.trigger` | |
| Model, effort, Claude Code version | exact: the calls before the compaction | |
| `idleCompaction` | on, when Claude Code's "written while you were away" notice follows the compaction | off or unknown otherwise |
| `autoCompactWindow` | a lower bound (an idle compaction had not compacted for size at that context), or about the compaction's size (one that compacted for size) | not recorded |
| `CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS` | an upper bound (the compaction's own size) | not recorded |
| `autoCompactEnabled`, `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` | nothing | not recorded |

`settings_file_modified_after_compaction` says whether the settings file changed after the compaction, so the current values may not be the ones in force.

## The overview across compactions

`overview` builds a plain-language report from the log. For each compaction it takes the latest real score that a fair self-test vouches for on the same questions, leaves the others out by name, and sums the kept, blurred and lost items, the saving (from the log line, or else recomputed from the transcript) and the critical losses. Maxime approved the shape on 2026-11-08 from a hand-written draft (`overview-draft-2026-11-08.md` in his trials folder). It has one difference from the draft: the critical items quote their question, because the script writes no prose of its own.

## Limits

- One extraction decides what counts as must-carry; a different extractor may pick different items.
- A judge can be wrong; repeated grading is not built in.
- The replay runs on the current Claude Code version and system prompt, not the ones the session ran on; both are logged.
- Model switches drop thinking blocks, and both sides run without tools, so neither is the live agent exactly; the comparison is between two equally reduced agents.
- Scores rest on a few dozen items and a few turns per compaction: read single runs as indications, and the log across many runs as the evidence.
- An item the no-context agent half answered stays scored, and its half credit counts in the retention (0.8 points on one of the five real scores to date, 2.2% on one self-test). Dropping those items would change what the score means across the log, so they stay.
