# The phase-one model is a reviewed profile, and its tokens are provider-attested

## Context

Task 16 fixed the last methodology mismatch in the B/C treatment. The model path was still open:

- the launch CLI accepted any `--models` string, so two rows could name different models;
- the LLM endpoint came from `CKBBENCH_LLM_API_BASE`, an environment default, so a row could be
  produced against a host nobody reviewed;
- mini-swe-agent retried a failed provider call up to an environment-controlled 10 attempts;
- token collection walked retained messages, derived a missing `total_tokens` from its components,
  and returned `None` when usage was absent — a run with no usage and a run with partial usage were
  indistinguishable in the artifact.

Phase one's decision can depend on token efficiency. A denominator that might be a full billable
total, a partial observation, or nothing at all cannot support that.

## Decision

One tracked, schema-validated, **non-secret** profile at `configs/phase1-gpt.json` names the
provider, exact requested GPT model, safe API base, API style, model settings, retry policy, the
model identity the authorized completion actually returned, an honest stability classification, and
the usage contract. It is separate from the frozen suite: it records the model path, not the tasks,
so nothing about `2.0.0` changes.

Fixed phase-one values:

| Item | Value |
| --- | --- |
| provider path | CKBuilders OpenAI-compatible proxy |
| API style | OpenAI **Responses** (`openai-responses`), root `/responses`, with the flat production bash tool schema |
| temperature | `0` |
| unsupported parameters | `drop_params=True` |
| LiteLLM internal retries | `0` |
| mini-swe-agent attempts per model turn | `1` |
| token source | the provider response `usage` object |
| required provider fields | native `input_tokens`, `output_tokens`, `total_tokens` |
| public result fields | unchanged `prompt_tokens`, `completion_tokens`, `total_tokens` |
| native-to-public mapping | `input`→`prompt`, `output`→`completion`, at one boundary: `_read_usage()` |
| token identity | all three non-negative integers, `total_tokens = input_tokens + output_tokens` |
| reasoning | `effort: medium`, `context: all_turns`, pinned as profile fields |
| per-turn output ceiling | none in production; probe-only `max_output_tokens: 4096` |
| endpoint credential | `CKBBENCH_LLM_API_KEY`, never in the profile or a result |

`--model-profile` is the accepted phase-one launch path and derives the one model from the profile.
`--models` remains for development and dry runs, is labelled as such in the CLI help and the grid
summary, and cannot produce an accepted phase-one artifact. A non-dry run of the phase-one registry
is refused without a profile, in the launcher and again in the operator wrapper before it takes the
project lock or preflights any endpoint; `smoke --model` is refused outright because smoke is
hardwired to that registry and always spends a real cell. The two are mutually exclusive. An
exported `CKBBENCH_LLM_API_BASE` that differs from the profile's endpoint fails the launch instead of
silently retargeting it. B and C receive the same immutable profile object.

The digest is taken from the exact tracked file bytes, so a reformatted profile is a different
profile even when it parses identically.

## Why accepted phase-one turns disable automatic retries

A failed provider attempt can be billed without returning usage. Retrying would add unmeasured cost
to a run whose recorded total came only from the attempts that answered, making the efficiency
denominator unknowable while the row still looked complete. One attempt keeps the failure visible:
it becomes infrastructure evidence. A later operator may rerun a cell under the declared
pilot/matrix policy, but the failed cell is never averaged as a complete token observation.

## The usage contract

The production model keeps a sanitized in-memory ledger. Every raw provider attempt is recorded at
the provider-call boundary, and a successful response is recorded **before** cost calculation and
action parsing — a response that later raises `FormatError` consumed tokens, so dropping it would
understate the run.

Each result carries:

```json
"metrics": {
  "total_wall_seconds": 0.0,
  "model_calls": 0,
  "provider_attempts": 0,
  "provider_responses": 0,
  "prompt_tokens": null,
  "completion_tokens": null,
  "total_tokens": null,
  "token_usage_status": "not_started | complete | incomplete",
  "provider_failure_category": null
}
```

- **`not_started`** — no model call, attempt or response; all token fields null.
- **`complete`** — at least one attempt, every attempt returned a response, every response carried
  valid usage, every response reported the same non-empty model identity, the totals are the sums of
  the provider fields, and `model_calls == provider_attempts == provider_responses`.
- **`incomplete`** — at least one attempt occurred but a provider attempt failed, a response omitted
  or malformed its usage, or the returned model identity was missing or drifted.

A missing `total_tokens` is never derived and a missing component is never replaced with zero.
Numeric strings, floats and booleans are not integers. Hidden reasoning or cached tokens are
included only as far as the provider includes them in those three totals.

## Why incomplete usage is infrastructure evidence

If the ledger cannot establish the run's usage, the cell is `infra_fail`: it contributes no
correctness and no efficiency, while its known lower-bound tokens and health counts stay in the raw
JSON as evidence. A model-generated **format error** is not infrastructure — the provider answered
and its usage was valid, so that is ordinary agent behavior with complete tokens.

`validate_results()` refuses, before aggregation or rendering: a missing, blank, unknown or
malformed profile ID/digest; a row whose `model` is not the profile's requested model; a digest that
is not the tracked profile's; malformed metric fields, counts or status; negative, boolean, float,
numeric-string or partial token triples; a broken token identity; `not_started` carrying activity;
`complete` with zero attempts, unequal counts, null tokens or no returned model; `incomplete` on a
correctness-scored outcome; and B/C drift in profile digest or returned model identity.

## Why a failure category, and why a fixed vocabulary

Result schema `1.4.0` adds one nullable string, `metrics.provider_failure_category`. When an accepted
attempt fails before returning a usable response, the run records **why**, so an operator can
distinguish an expired key from a rate limit or a dropped connection without a rerun. Task 20 ended
with two `infra_fail` cells whose rows said only that something failed.

The value is derived from the exception **type** at the in-memory provider boundary, never from
exception text, and reduced to exactly one of:

```text
authentication  authorization  rate_limit  timeout   connection  server
request         protocol       unsupported context_window        other_provider  multiple
```

- `null` — no accepted attempt failed (including every `not_started` run).
- one category — every failed attempt in the run mapped to it.
- `multiple` — failed attempts mapped to more than one category.

Provider text is the single most likely place for a key, URL, prompt or completion to leak into a
tracked artifact, so it is never a source. The vocabulary is closed: `validate_results()` rejects any
other value, rejects a category on any row whose attempts were all answered — which covers every
`not_started` and every `complete` row — requires one on a row with an unanswered attempt, requires
that row to be `incomplete`, and requires at least two unanswered attempts before accepting
`multiple`. An `incomplete` row still carries `null` when its cause was malformed usage or model
drift rather than a failed attempt. A rejected value is provider- or file-controlled, so diagnostics
name the field and the allowed literals and never echo what was found.

## Why only run-level tokens

The composed single-agent run emits no reliable per-task completion event, so per-task token
attribution would be invented. Cost is also deliberately out of scope here: the proxy may not expose
a stable monetary price, and the phase-one hypothesis is answerable with correctness, tokens and
time.

## Why the Responses API, recorded after attempt 5

The chat contract was not a preference; it was an assumption, and one controlled request refuted it.
On 2026-08-16 exactly one authorized `POST https://share-ai.ckbdev.com/chat/completions` with
`gpt-5.6-sol` returned **HTTP 2xx with a gzipped 3,201-byte `text/html` body** — not JSON, not SSE,
not an error status. The sanitized ten-field record is `research/handoff/17-completion-diagnostic.json`
(SHA-256 `ce91ad20…1d402`), retained unchanged as the negative evidence for this decision.

The replacement is grounded rather than guessed: the same base and model serve a working Responses
client at root `/responses`, the vendored agent fork already carries a LiteLLM Responses client and
the flat `BASH_TOOL_RESPONSE_API` schema, and the model reference lists Responses with function
calling. `api_base` stays exactly `https://share-ai.ckbdev.com`; the operation is root `/responses`,
not a guessed `/v1/chat/completions`.

This is an independent harness implementation of the provider protocol. No external agent runtime
becomes a benchmark dependency.

Consequences recorded honestly:

- `api_style` is `openai-responses` and `usage_contract` is `openai-responses-usage-v1`.
- Usage is read from the provider-native `input_tokens`, `output_tokens` and `total_tokens`. The
  public result fields are unchanged; `input`→`prompt_tokens` and `output`→`completion_tokens` are
  mapped at exactly one boundary, `_read_usage()` in `agent/ckb_model.py`. Local provider evidence
  keeps the native names so the wire shape is not obscured.
- The accepted phase-one model is `CkbLitellmResponseModel`, benchmark-owned. Upstream's
  `LitellmResponseModel` is protocol-correct but retention-wrong: it stores the whole response in the
  returned message and in `FormatError`. Only the protocol is inherited.
- A Responses turn is replayed by sending its output items back, so the returned `function_call`
  items are preserved as protocol. Nothing else about the response survives: no text content, no
  response ID, no status, no raw body.
- **Reasoning is pinned, not inherited.** `reasoning_effort: "medium"` and
  `reasoning_context: "all_turns"` are profile fields, so the profile digest binds them, and both
  the controlled request and the production model send
  `reasoning: {"effort": "medium", "context": "all_turns"}`. A moving alias must not choose
  reasoning for an accepted run.
- **Production sends no per-turn output ceiling.** A `max_output_tokens` cap would truncate a real
  coding turn and bias the five-task result, so its absence is the phase-one behavior. The
  controlled probe carries a probe-only ceiling of 4096: it bounds one compatibility request while
  leaving room for medium reasoning plus a completed tool call.
- The controlled request proves endpoint, Responses/tool-call, returned-model and usage
  compatibility. It is **not** a byte-identical benchmark turn, and the profile does not claim it
  is: model, temperature, reasoning, stream mode and the exact tool schema are shared; the output
  ceiling is deliberately probe-only.

## Controlled evidence contract

`configs/phase1-gpt.json` does not exist: the completion evidence it needs has not been obtained.

What has happened, once and completely:

- **One catalog request succeeded** — `GET https://share-ai.ckbdev.com/models`, 2xx, 12 sanitized
  GPT candidates in `research/handoff/17-catalog-evidence.json`. `gpt-5.6-sol` was selected from
  that list by the user and is recorded `moving_alias`.
- **Five historical chat attempts** are recorded in `research/handoff/17-provider-request-log.md`.
  The last of them refuted the chat contract and produced
  `research/handoff/17-completion-diagnostic.json`.
- **No Responses request has been made.** Its evidence and the profile remain absent.

The remaining request is one bounded, separately authorized Responses call, one-use and never
retried:

**Completion** — one authenticated Responses request to root `/responses` with the selected model,
the Responses `input`, the flat production bash tool, temperature 0, `stream: false`,
`reasoning: {"effort": "medium", "context": "all_turns"}`, a probe-only `max_output_tokens: 4096`
and zero retries. It is certifiable only if the response status is `completed` and it carries
exactly one `completed` `function_call` for `bash` with a non-empty `call_id` and the exact fixed
arguments. That call is counted, never executed.

Retained evidence is limited to UTC time, the safe API base, requested and returned model, HTTP
success, an explicit response-completed boolean, an exactly-one-tool-call boolean, the three native
`input_tokens`/`output_tokens`/`total_tokens` integers, the token-identity boolean, the request
count, and the final profile digest. No key, header, raw body, prompt text, completion
content, tool argument, response ID or exception text may be printed or kept. The digest is added
afterwards by the offline `finalize` mode, which rebuilds the document from exactly these fields and
accepts it only when it records one successful request, the expected tool call, complete
non-negative usage satisfying `total = prompt + completion`, and identities matching the tracked
profile.

## Limitations

- **Provider billing outside the returned usage cannot be independently audited.** The harness
  records what the response reports; a provider that bills for an attempt it did not report is
  invisible here. The one-attempt policy bounds, but does not eliminate, that gap.
- **A moving alias remains a risk when no dated snapshot exists.** The profile records the
  classification honestly, and every run re-checks the returned model identity and fails closed on
  drift, but an alias that changes behavior without changing its name is not detectable from usage
  alone.
- **No benchmark effectiveness result exists.** This ADR fixes the model path and its evidence. It
  makes no claim about token cost, model quality, or CKB AI benefit.
