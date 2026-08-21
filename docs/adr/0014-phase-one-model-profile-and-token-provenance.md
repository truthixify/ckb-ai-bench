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
| benchmark-owned attempts per model turn | `2` maximum: one first attempt plus one provider-fault recovery |
| provider request timeout | `300` seconds per Responses request |
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

## Why accepted phase-one turns use one counted recovery attempt

A failed provider attempt can be billed without returning usage. Retrying would add unmeasured cost
to a run whose recorded total came only from the attempts that answered. Profile v5 therefore keeps
LiteLLM's internal retries at zero and permits exactly one benchmark-owned recovery attempt only
after the sanitized boundary positively classifies a provider or transport fault. Internal harness
errors, agent errors, MCP calls, grading and whole cells are never retried.

Every attempt remains counted. If the recovery succeeds, the cell may still be graded for
correctness because every requested model turn ultimately received a usable response under the
pinned model identity. Its token status remains `incomplete`, its recorded token sum is only a lower
bound, and it is excluded from every efficiency delta. If the recovery also fails, the model turn is
unanswered and the cell remains `infra_fail`. This separates effectiveness evidence from a billing
denominator the provider did not supply instead of throwing both away or pretending both are known.

## Why provider requests have a finite timeout

The agent's 900-second wall limit is checked between actions. It cannot stop a provider request that
is already blocked in an HTTP receive. Task 35 demonstrated this boundary when one HTTPS receive
continued beyond the configured agent limit until the operator interrupted the exact worker.

Profile v3 introduced `provider_request_timeout_seconds: 60`, passed to LiteLLM as `timeout` on
every Responses request. That closed Task 35's unbounded receive, but Task 42 proved the limit was
too tight for this model path: eight requests returned, then the ninth entered HTTPX's transport and
timed out before a response object existed. LiteLLM's pinned adapter converts that
`httpx.TimeoutException` into `litellm.Timeout` with synthetic status 408.

Profile v4 raised the inactivity bound to 300 seconds. Five minutes remains below the 900-second
agent limit and still bounds a silent socket operation, while allowing a slow provider call to
remain eligible beyond one minute. It is part of the profile digest because changing network wait
policy changes execution behavior. Profile v5 retains that bound and may make one counted recovery
attempt. The usage ledger records no fabricated response; a second failure remains `infra_fail`.

This does not turn the agent limit into an exact process deadline. HTTPX timeouts limit inactivity
within each I/O operation, not total response duration; a peer that continuously trickles data could
still keep a call alive beyond 300 seconds. Shell and MCP actions retain their own existing
60-second bounds.

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

## Why correctness and efficiency completeness are separate

An unanswered model turn, harness error or returned-model drift makes the cell `infra_fail`. A cell
whose every turn ultimately received a usable response may be graded even when an earlier attempt
failed or a response omitted usage. That row contributes correctness but never token efficiency;
its known lower-bound tokens and failure category stay visible in the raw JSON and report. A
model-generated **format error** is ordinary agent behavior because the provider answered; its
usage is complete when the response carried all three valid usage fields.

`validate_results()` refuses, before aggregation or rendering: a missing, blank, unknown or
malformed profile ID/digest; a row whose `model` is not the profile's requested model; a digest that
is not the tracked profile's; malformed metric fields, counts or status; negative, boolean, float,
numeric-string or partial token triples; a broken token identity; `not_started` carrying activity;
`complete` with zero attempts, unequal counts, null tokens or no returned model; attempts beyond the
reviewed two-per-call ceiling; a scored `incomplete` row with an unanswered model turn or no returned
model identity; and B/C drift in profile digest or returned model identity.

## Why a failure category, and why a fixed vocabulary

Result schema `1.4.0` added one nullable string, `metrics.provider_failure_category`. When an accepted
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

Result schema `1.5.0` keeps the same fields and records the new relationship explicitly:
`provider_attempts` may exceed `model_calls`, while a scored row requires
`provider_responses == model_calls`. This is the proof that every model turn recovered. Complete
usage still requires all three counts to be equal.

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
- **The Responses conversation is explicitly stateless.** The profile requires `store: false`, and
  both the controlled request and production send it. The harness owns the conversation and replays
  every output item plus each function result; it does not combine that history with provider-side
  response storage. This became profile v2 after a live diagnostic showed that stateful continuation
  is available only on this provider's WebSocket-v2 route, not the synchronous HTTP route used by
  the benchmark.
- **Replay removes only output-only `status` metadata.** A bounded HTTP reproduction established
  the provider's exact rejection as `unknown_parameter` for a prior output item's `status`; the
  identical replay succeeded after removing that field alone. The benchmark preserves item type,
  order, content, encrypted reasoning, IDs, call IDs, tool names and arguments. Completed-call
  status is validated before the item enters history, so removing it from the next request does not
  weaken executable-action validation.
- **Production sends no per-turn output ceiling.** A `max_output_tokens` cap would truncate a real
  coding turn and bias the five-task result, so its absence is the phase-one behavior. The
  controlled probe carries a probe-only ceiling of 4096: it bounds one compatibility request while
  leaving room for medium reasoning plus a completed tool call.
- The controlled request proves endpoint, Responses/tool-call, returned-model and usage
  compatibility. It is **not** a byte-identical benchmark turn, and the profile does not claim it
  is: model, temperature, reasoning, stream mode, request timeout and the exact tool schema are
  shared; the output ceiling is deliberately probe-only.

## Controlled evidence contract

`configs/phase1-gpt.json` is the reviewed profile. Profile v5 has SHA-256
`ed9f7fa538d0f823fc2352c9c24f9a1cd1c36016d6c1b313a9b04e1c4ca804ab`. It preserves profile v4's
request shape and 300-second inactivity limit and changes only the benchmark-owned attempt ceiling
from one to two. The repair follows repeated fresh-cohort evidence in which otherwise identical
cells succeeded while one transient, varying-turn provider fault excluded adjacent cells. Profile
v4 has historical SHA-256
`0dcedaf346ccaac47ddd070dd27aedc12c5011e0b0b7bda69b1b1999f7ad8390`; profile v3 has historical SHA-256
`67544290765bdab32de1abbea48d20561abb74e90046c88d32cd27cffdf1fa1a`; profile v2 has historical SHA-256
`117f5d35d699e6200b4d9fb96fce724947b57bfc63c3a5620467f088c90f4ade`. The current profile is bound
to these retained checks:

- **One catalog request succeeded** — `GET https://share-ai.ckbdev.com/models`, 2xx, 12 sanitized
  GPT candidates in `research/handoff/17-catalog-evidence.json`. `gpt-5.6-sol` was selected from
  that list by the user and is recorded `moving_alias`.
- **Five historical chat attempts** are recorded in `research/handoff/17-provider-request-log.md`.
  The last of them refuted the chat contract and produced
  `research/handoff/17-completion-diagnostic.json`.
- **The original Responses compatibility request succeeded** and established the v1 model,
  endpoint, tool-call and usage shape. Task 25 repeated the same one-request contract with
  `store: false`; `research/handoff/25-stateless-responses-evidence.json` binds the successful
  response to profile v2 and its digest. Both calls returned `gpt-5.6-sol` with one completed bash
  call and native usage satisfying `input_tokens + output_tokens = total_tokens`; neither returned
  call was executed.
- **The Task 25 request proved the protocol under a 60-second timeout.** Profile v3 made that bound
  mandatory in production. Task 42 then captured the exact limitation of that policy: eight normal
  Responses calls followed by a transport timeout before any ninth response existed. Profile v4
  changes only the maximum inactivity wait; it reuses the established protocol evidence and does not
  claim a separate compatibility request ran with the v4 bytes.
- **The multi-turn repair was tested separately.** A production-shaped, no-command compatibility
  run received a response containing reasoning, an assistant message and a function call, then
  received a second usable function call after replay normalization. This proves the continuation
  shape that a one-turn profile request cannot cover; it is diagnostic evidence, not a score.

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
  invisible here. The two-attempt ceiling bounds that gap, and any recovered row is excluded from
  efficiency, but neither measure reveals the failed attempt's cost.
- **The 900-second agent wall value is a between-actions limit, not an exact process deadline.** A
  provider, MCP or shell action may finish after the limit is crossed. The model request now has
  300-second connect/read/write/pool inactivity limits, but not a 300-second total-duration deadline;
  an external supervisor remains responsible for an exact whole-process deadline if one is required.
- **A moving alias remains a risk when no dated snapshot exists.** The profile records the
  classification honestly, and every run re-checks the returned model identity and fails closed on
  drift, but an alias that changes behavior without changing its name is not detectable from usage
  alone.
- **This ADR makes no effectiveness claim.** It fixes the model path and evidence rules; benchmark
  results must still meet the report's declared cohort gates.

## The diagnostic artifact is not accepted evidence

`provider_failure_category` is the accepted triage signal and stays exactly as specified above. When
it is not enough — Task 22 ended with two `request` rows and no way to tell a `BadRequestError` from a
`NotFoundError`, or a pre-transport rejection from a dropped response — `./bench diagnose` runs one
isolated arm-B cell and writes a **separate** bounded artifact.

- Diagnostic schema `2.2.0` lives in `diagnostic/<run_id>.diag.json`, beside the run's artifacts.
- It records, per provider attempt: `outcome` (`responded`, `bad_request`, `not_found`,
  `request_other`, `other_failure`), `transport_state` (`not_started`,
  `handler_entered_no_response`, `response_seen`, `unobserved`), a nullable `http_status`, and a
  content-free `input_shape`. `http_status` is retained only as an integer in `100..599` from a
  positively identified LiteLLM API exception; every other value becomes null.
- The value is LiteLLM-carried provenance, not an independently captured wire status. It identifies
  a returned HTTP condition only when the same record says `transport_state: response_seen`; beside
  a no-response state it may be a client-assigned synthetic status.
- The input shape reports only whether every reasoning item carries non-empty encrypted replay state;
  it never retains that state. This distinguishes replayable manual history from an item whose server
  identity cannot safely cross an OpenAI-compatible proxy boundary.
- It is bounded to 16 records and 32 KiB, carries closed enums and bounded integers only, and
  contains no prompt, completion, command, arguments, identifier, exception text, response body,
  header, request, URL or content length.
- **No diagnostic-driven result-schema change.** Accepted rows use `1.5.0`; no report ever
  reads a diagnostic artifact.
- Running it changes nothing about the accepted path: the wire request is byte-identical with the
  mode on and off, and ordinary runs never install the transport observer.
- A parent-supervised diagnostic overlays workspace `target/` and `build/` with anonymous Docker
  volumes. The first covers Cargo's default output and the second covers the frozen hashlock task's
  declared `build/release/hashlock` proof. Cargo's internal hard links therefore never enter the
  host scrub tree, and the parent disposes both volumes through the ownership-proved agent container
  ID with one `docker rm -v`. The host scrub remains fail-closed for every hard link, and ordinary
  benchmark agents receive no such mounts.
- Historical schema-`2.1.0` artifacts remain bound to the code revision that produced them. The
  current strict validator intentionally accepts only `2.2.0`; no report consumes either version.

`./bench diagnose` is exceptional troubleshooting, **not a benchmark arm**. It grades nothing, writes
no `RunResult`, and a live execution requires separate explicit authorization. A successful run does
not establish a task score, a treatment effect, or a provider fix.

The diagnostic reuses the ordinary fixed networks so it can reach the already-running proxy. Its
containers carry a fresh diagnostic identity, but the networks retain the empty ordinary-operation
validation label. The integration gate remains different: it creates invocation-scoped networks and
labels them with its validation identity because it owns their full lifecycle.
