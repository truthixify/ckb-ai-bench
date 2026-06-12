# DeepSWE Reference (captured 2026-06-12)

Source: https://deepswe.datacurve.ai (Datacurve). Captured live via browser.
Screenshot: `docs/assets/deepswe-home.png`.

This is the reference design the user pointed at ("a little bit more simple" than this).

## Tagline / framing

> Measuring frontier coding agents on original, long-horizon engineering tasks.

## Site structure (what we want a simpler version of)

- Clean top nav: Blog / Run / Data / GitHub / theme toggle.
- Hero: logo, one-line description, two CTAs ("Read the blog", "Run DeepSWE").
- **One interactive chart** with a tab toggle: **Cost | Time | Output tokens** (x-axis switches; y-axis is always the score).
  - Scatter plot: x = Avg cost per task ($0–$15), y = DeepSWE score (Pass@1, 0–80%).
  - Points colored per model; labeled with `model [effort]`. A "most efficient ↗" annotation marks the efficiency frontier.
  - A `Models (17/20)` dropdown to filter which models are plotted, plus a "Best / All effort levels" toggle (pick the best effort per model, or show every effort point).
- **Ranked table** below the chart. Columns: `MODEL` | `PASS@1` | `AVG COST` | `AVG TIME` | `OUT TOK`. Sorted descending by Pass@1.
- "Updated <date>" stamp.
- Footnote: **"All models run on mini-swe-agent for consistency. Read why."** (key methodology choice — see below).
- Methodology prose + task examples + a canary GUID line to keep data out of training corpora.

## Captured leaderboard data (snapshot)

| MODEL | EFFORT | PASS@1 | AVG COST | AVG TIME | OUT TOK |
|---|---|---|---|---|---|
| gpt-5.5 | xhigh | 70%±3% | $6.61 | 21m | 47k |
| claude-opus-4.8 | max | 58%±2% | $12.58 | 43m | 136k |
| gpt-5.4 | xhigh | 56%±2% | $4.38 | 27m | 71k |
| claude-opus-4.7 | max | 54%±5% | $18.19 | 39m | 103k |
| claude-sonnet-4.6 | high | 32%±2% | $5.52 | 42m | 76k |
| gemini-3.5-flash | medium | 28%±4% | $7.42 | 17m | 189k |
| claude-opus-4.6 | max | 28%±4% | $5.39 | 30m | 44k |
| gpt-5.4-mini | xhigh | 24%±3% | $2.08 | 33m | 135k |
| kimi-k2.6 | — | 24%±2% | $3.16 | 56m | 84k |
| minimax-m3 | — | 20%±4% | $5.57 | 57m | 98k |
| mimo-v2.5-pro | — | 19%±2% | $1.99 | 28m | 49k |
| qwen3.7-max | — | 18%±1% | $2.12 | 17m | 42k |
| glm-5.1 | — | 18%±1% | $7.46 | 35m | 49k |
| grok-build-0.1 | — | 13%±2% | $6.60 | 44m | 52k |
| gemini-3.1-pro | — | 10%±3% | $1.84 | 36m | 53k |
| deepseek-v4-pro | — | 8%±3% | $4.22 | 37m | 50k |
| gemini-3-flash | — | 5%±2% | $1.53 | 39m | 233k |

## DeepSWE methodology (four claimed advances) — directly relevant to our design

1. **Contamination free**: Tasks written from scratch, not adapted from existing commits/PRs, so no model saw the solution in pretraining.
2. **High diversity**: 113 tasks across 91 repositories, 5 languages.
3. **Real-world complexity**: Prompts ~half the length of SWE-bench Pro's, yet solutions require 5.5x more code and ~2x more output tokens.
4. **Reliable verification**: Verifiers hand-written to test *software behavior*, not implementation details.

## KEY TAKEAWAY for our benchmark

DeepSWE deliberately runs **every** model on **mini-swe-agent** "for consistency" — i.e. they chose the simplest possible harness on purpose to remove harness variance as a confound. This is a direct, authoritative data point for the user's open question (Claude Code / Codex vs mini-swe-agent). When the variable under test is the model (or, for us, the MCP server), you want the harness held constant and minimal so it does not become a confound.
