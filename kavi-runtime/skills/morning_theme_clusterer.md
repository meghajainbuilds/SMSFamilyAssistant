# Morning theme clusterer

Your task: look at one household member's OPEN task titles and decide whether they cluster around ONE effort worth naming as today's top-of-mind theme. Output zero or one theme.

You will receive structured input:

- `today_date` — today's date (ISO). Use it for seasonality: camp tasks cluster harder in late spring ("walking into summer"), school-enrollment tasks in late summer, travel-prep tasks before school breaks.
- `open_task_titles` — the open task titles (owner-abbreviation prefixes like `MJ ` / `MM ` and `[?] ` markers may lead a title; ignore them when clustering). Titles may be truncated.

<!-- The minimum cluster size is owned by THEME_MIN_CLUSTER in
capabilities/kavi_persona/selection.py; the calling code enforces it on
your output as well, and also caps/truncates the input titles. This skill
owns the CLUSTERING JUDGMENT only. -->

## Judgment

A theme is a real shared effort, not a grammatical similarity:

- YES shapes: several tasks that all serve the same upcoming life event or project — "Register for Cascade summer camp", "Pay camp deposit", "Order swim gear for camp", "Book camp carpool" → "Summer camp planning".
- NO shapes: tasks that merely share a word ("Pay Boonli", "Pay cleaner", "Pay BCBA" is bill-paying noise, not a theme); a grab-bag label ("Household tasks", "Errands", "To-dos") that would cover anything; two related tasks plus a stretch third.

Rules:

- At most ONE theme. If two clusters compete, pick the one with more supporting tasks; on a tie, the more time-pressing one given `today_date`.
- Every supporting title must appear VERBATIM in `open_task_titles` (copy them exactly, including any prefix). A theme needs at least the minimum cluster size of genuinely-supporting tasks — do not pad with weak members to reach it.
- The label is a short, specific noun phrase the household would recognize ("Summer camp planning", "Kindergarten enrollment"), not a category ("Admin", "Kids stuff").

## Conservative bias

Zero themes is a valid and COMMON output — most mornings have no dominant thread. A forced weak theme is worse than none: it frames the recipient's whole morning around something that isn't real. When unsure whether a cluster is a genuine shared effort, output null.

## Output format

JSON object with exactly one field, nothing else:

```json
{"theme": {"label": "Summer camp planning", "supporting_task_titles": ["MJ Register for Cascade summer camp", "MJ Pay camp deposit", "MJ Order swim gear for camp"]}}
```

or, the common case:

```json
{"theme": null}
```

## Examples

Input: today 2026-06-10, titles ["MJ Register for Cascade summer camp", "MJ Pay camp deposit", "MJ Order swim gear for camp", "MJ Pay Boonli invoice", "MJ Book dentist"]
> Output: `{"theme": {"label": "Summer camp planning", "supporting_task_titles": ["MJ Register for Cascade summer camp", "MJ Pay camp deposit", "MJ Order swim gear for camp"]}}`

<!-- private:mtc-001 -->Input: today 2026-06-10, titles ["MJ Pay Boonli invoice", "MJ Book dentist", "MJ Reply to Erin", "MJ Sign field trip slip", "MJ Order diapers"]<!-- /private -->
> Output: `{"theme": null}` (five unrelated tasks; nothing clusters)

Input: today 2026-06-10, titles ["MJ Pay Boonli invoice", "MJ Pay cleaner", "MJ Pay BCBA invoice", "MJ Book dentist"]
> Output: `{"theme": null}` (shared verb, not a shared effort — bill-paying is not a theme)
