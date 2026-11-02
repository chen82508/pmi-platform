# Factor: high_severity (v2)

`value=1` ONLY if the market is about an **armed conflict or military action between human parties** AND a YES resolution implies **mass-casualty** or **strategic-asset** outcomes (nuclear use, capital city seizure, > 10k casualties, regime change).

`value=0` for everything else, including:
- skirmishes / boundary incidents;
- **any market not about human armed conflict** — natural disasters (meteor strikes, earthquakes, hurricanes), pandemics, sports, entertainment, elections, finance — **no matter how severe the event would be**.

<!-- v2: v1 keyed on severity alone, so "Will a major meteor strike hit Earth?"
     scored 1 — a meteor IS mass-casualty, but it is not war. Severity now
     explicitly requires the armed-conflict precondition. -->

Return JSON: `{ "value": 0|1, "confidence": 0.0..1.0, "reasoning": "..." }`.

Title: {market_title}
Description: {market_description}
