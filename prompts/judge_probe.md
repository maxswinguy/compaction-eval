You are grading answers in an evaluation. For each item you get a question, the reference answer, and candidate answers labelled with letters. Grade each candidate against the reference:

- kept: matches the reference in substance; wording may differ, and extra correct detail is fine
- distorted: partly right, or right with a wrong detail that would mislead (a wrong number, a changed condition, a reversed decision)
- lost: UNKNOWN, missing, generic, or wrong

Judge only against the reference answer. Do not reward length or confidence. The letters carry no meaning and change from item to item.

Reply with JSON only, in this shape:
{"grades": [{"id": "I01", "A": "kept", "B": "lost", "C": "distorted", "why": "one short sentence"}]}

Items:
{{ITEMS}}
