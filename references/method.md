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

## Limits

- One extraction decides what counts as must-carry; a different extractor may pick different items.
- A judge can be wrong; repeated grading is not built in.
- The replay runs on the current Claude Code version and system prompt, not the ones the session ran on; both are logged.
- Model switches drop thinking blocks, and both sides run without tools, so neither is the live agent exactly; the comparison is between two equally reduced agents.
- Scores rest on a few dozen items and a few turns per compaction: read single runs as indications, and the log across many runs as the evidence.
