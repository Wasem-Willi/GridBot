# OpenAI Decision Spec for GridBot

Use this spec when generating live decisions for GridBot.

## Goal

Return **one action** for a symbol:

- `BUY_ONLY`
- `SELL_ONLY`
- `BOTH`
- `PAUSE`

## Hard rules

1. Output must be **valid JSON only** (no markdown, no prose outside JSON).
2. JSON must include exactly these keys:
   - `action` (string)
   - `confidence` (number between `0.0` and `1.0`)
   - `reason` (short string)
3. `action` must be one of: `BUY_ONLY`, `SELL_ONLY`, `BOTH`, `PAUSE`.
4. Keep `reason` short and operational (one sentence).
5. If uncertain, prefer safer actions:
   - high uncertainty -> `BOTH` or `PAUSE`

## Input payload (from bot)

Typical fields:

- `symbol` (e.g. `BTCUSDT`)
- `price` (current price)
- `regime_verdict` (`ranging`, `trending`, or `unknown`)
- `symbol_is_paused` (`true/false`)
- `instructions` (policy hint)

## Decision policy

- If market appears dangerous/unstable/trending strongly -> prefer `PAUSE`.
- If bullish bias with controlled risk -> `BUY_ONLY`.
- If bearish bias with controlled risk -> `SELL_ONLY`.
- If neutral/range market -> `BOTH`.
- Never output unsupported values.

## Output examples

```json
{"action":"PAUSE","confidence":0.82,"reason":"trend strength is high and risk is elevated"}
```

```json
{"action":"BOTH","confidence":0.66,"reason":"range-like behavior with moderate volatility"}
```

```json
{"action":"BUY_ONLY","confidence":0.61,"reason":"upside bias with manageable volatility"}
```

```json
{"action":"SELL_ONLY","confidence":0.64,"reason":"downside pressure with controlled spread conditions"}
```

## Invalid outputs (do not do)

- Markdown code fences around JSON
- Explanations before/after JSON
- Missing keys
- `confidence` outside `[0,1]`
- Unknown action values
