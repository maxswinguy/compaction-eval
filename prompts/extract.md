[Compaction evaluation, extraction step. This is not a request to continue the work above, and nothing here changes it.]

You have the full history of this Claude Code session above. Act now as an evaluator. Another agent will continue this session from a shorter context. List the items it must know to continue the work correctly: what a capable colleague taking over tomorrow would need, and would get wrong without.

Cover the whole history evenly, from the first message to the last, not only the recent part.

Categories:
- ruling: an instruction or decision the user gave in their own words (a preference, an approval, a "never do X")
- decision: a choice settled during the work, with its reason
- open_task: work still pending or promised, and its next step
- id_path: an exact identifier the work depends on (a file path, URL, id, branch, command or number)
- constraint: a limit or rule that applies to future actions
- fact: a measured result or a state that later steps rely on

Severity: 2 = forgetting it would cause a wrong action; 1 = forgetting it would cost time or a question to the user; 0 = harmless if forgotten.

For each item, write a question that a successor could answer only if it knows the item. The question must not contain or hint at the answer, and must make sense to someone who has not seen this history: name the project, file or topic it is about. Give the expected answer. Set answer_type to "exact" when the answer is one or two literals that a string match can check (a path, file name, id, URL or hash); the answer is then those literals only, with no sentence around them. Everything else, numbers and names included, is "judged". Add a quote of at most 25 words from the history as evidence.

Aim for 20 to 40 items, at least half of them severity 1 or 2. Leave out small talk.

Do not use tools. Reply with JSON only, in this shape:
{"items": [{"category": "ruling", "severity": 2, "question": "...", "answer": "...", "answer_type": "judged", "evidence": "..."}]}
