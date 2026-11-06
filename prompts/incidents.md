Below are messages a user sent to a Claude Code agent after its context was compacted (replaced by a summary), and a reference list of what the session had established before the compaction.

For each message, say whether the user restates or corrects something the agent should already have known from before the compaction: repeating an instruction, "as I said", "we already decided", fixing a wrong assumption about an established fact. A new request, new information or a change of mind is not an incident.

Reply with JSON only, in this shape: {"messages": [{"n": 1, "incident": "restate", "why": "short"}]} with incident one of restate, correct, none.

Reference: what the session had established
{{ITEMS}}

Messages
{{MESSAGES}}
