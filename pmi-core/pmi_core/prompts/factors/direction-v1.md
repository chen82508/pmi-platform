# Factor prompt: direction (v1)

Determine whether a YES resolution on this market would indicate **escalation**, **de-escalation**, or **neither** of armed conflict.

Return JSON:
```json
{ "value": -1 | 0 | 1, "confidence": 0.0..1.0, "reasoning": "<one sentence>" }
```

- `+1` = YES means conflict is more likely / has escalated
- `-1` = YES means conflict is less likely / has de-escalated
- `0`  = direction unclear or market is symmetric

Market title: {market_title}
Market description: {market_description}
