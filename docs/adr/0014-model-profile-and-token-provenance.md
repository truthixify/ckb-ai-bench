# Models use reviewed profiles, and their tokens are provider-attested

## Context

The B/C treatment was fixed, but the model path was still open:

- the launch CLI accepted any `--models` string, so two rows could name different models;
- the LLM endpoint came from `CKBBENCH_LLM_API_BASE`, an environment default, so a row could be
  produced against a host nobody reviewed;
- mini-swe-agent retried a failed provider call up to an environment-controlled 10 attempts;
- token collection walked retained messages, derived a missing `total_tokens` from its components,
  and returned `None` when usage was absent — a run with no usage and a run with partial usage were
  indistinguishable in the artifact.

The benchmark decision can depend on token efficiency. A denominator that might be a full billable
total, a partial observation, or nothing at all cannot support that.

## Decision

Each tracked, schema-validated, **non-secret** JSON file under `configs/models/` names one supported
model configuration: exact requested model, safe API base, API style, bounded request-body
extensions, model settings, retry policy, the identity a compatibility completion returned, an
honest stability classification and the usage contract. Profiles are separate from the frozen
suite: they record model paths, not tasks. The task-order correction is independently identified by
suite `3.0.0`; model profiles remain valid only when each result records the active suite freeze.
`./bench models` is the authoritative operator catalog.

Fixed matrix-runner values:

| Item | Value |
| --- | --- |
| provider path | selected profile's reviewed OpenAI-compatible API base |
| requested model | selected profile's exact catalog ID |
| request extensions | selected profile's exact bounded JSON object; empty for a direct-compatible endpoint |
| API style | OpenAI **Responses** (`openai-responses`), root `/responses`, with the flat production bash tool schema |
| temperature | selected profile's supported value or an explicit omission |
| unsupported parameters | `drop_params=True` |
| LiteLLM internal retries | `0` |
| benchmark-owned attempts per model turn | `4` maximum: one first attempt plus three transient-fault recoveries |
| benchmark retry delays | fixed `4`, `8`, `16` seconds before attempts 2, 3 and 4 |
| retryable categories | `rate_limit`, `timeout`, `connection`, `server`, `protocol`, `other_provider` |
| provider request timeout | `300` seconds per Responses request |
| token source | the provider response `usage` object |
| required provider fields | native `input_tokens`, `output_tokens`, `total_tokens` |
| public result fields | unchanged `prompt_tokens`, `completion_tokens`, `total_tokens` |
| native-to-public mapping | `input`→`prompt`, `output`→`completion`, at one boundary: `_read_usage()` |
| token identity | all three non-negative integers, `total_tokens = input_tokens + output_tokens` |
| reasoning | selected profile's explicit effort, `provider-default` or `unsupported`; absent states omit the request field; local replay policy: `prefix-tail-groups-v1`, 131,072-byte prepared-input ceiling |
| observation replay | rendered shell/MCP text keeps a deterministic head and tail within 32,768 UTF-8 bytes per turn |
| provider truncation | explicitly disabled or omitted as selected by the profile; the harness owns deterministic local compaction |
| per-turn output ceiling | none in production; probe-only `max_output_tokens: 4096` |
| endpoint credential | `CKBBENCH_LLM_API_KEY`; never in a profile or result |

`--profile` is the accepted matrix launch path and derives the one model from the profile.
`--models` remains for development and dry runs, is labelled as such in the CLI help and the grid
summary, and cannot produce an accepted artifact. A non-dry run of the scored registry
is refused without a profile, in the launcher and again in the operator wrapper before it takes the
project lock or preflights any endpoint; `smoke --model` is refused outright because smoke is
hardwired to that registry and always spends a real cell. The two are mutually exclusive. An
profile endpoint is authoritative and cannot be retargeted by an ambient base variable. B and C
receive the same immutable profile object.

The digest is taken from the exact tracked file bytes, so a reformatted profile is a different
profile even when it parses identically.

## Why accepted turns use tightly bounded transient recovery

A failed provider attempt can be billed without returning usage. Retrying adds potentially
unmeasured cost to a run whose recorded total comes only from attempts that answered. Profile v6
therefore keeps LiteLLM's internal retries at zero and permits at most three benchmark-owned
recoveries, after fixed 4, 8 and 16 second waits, only when the sanitized boundary classifies the
failure as `rate_limit`, `timeout`, `connection`, `server`, `protocol` or `other_provider`.
Authentication, authorization, invalid requests, unsupported parameters, context-window failures,
internal harness errors, agent errors, MCP calls, grading and whole cells are never retried.

Every attempt remains counted. Retry count, scheduled waiting and allowlisted failure counts are
retained in the result schema. If recovery succeeds, the cell may still be graded for
correctness because every requested model turn ultimately received a usable response under the
pinned model identity. Its token status remains `incomplete`, its recorded token sum is only a lower
bound, and it is excluded from every token and wall-time efficiency delta. Its raw elapsed time and
scheduled retry delay remain retained for operational diagnosis. If the recovery also fails, the
model turn is unanswered and the cell remains `infra_fail`. This separates effectiveness evidence
from a billing denominator the provider did not supply instead of throwing both away or pretending
both are known.

## Why provider requests have a finite timeout

The agent's 1200-second wall limit is checked between actions. It cannot stop a provider request that
is already blocked in an HTTP receive. A live diagnostic demonstrated this boundary when one HTTPS
receive continued beyond the configured agent limit until the operator interrupted the exact worker.

Profile v3 introduced `provider_request_timeout_seconds: 60`, passed to LiteLLM as `timeout` on
every Responses request. That closed the unbounded receive, but a later cohort proved the limit was
too tight for this model path: eight requests returned, then the ninth entered HTTPX's transport and
timed out before a response object existed. LiteLLM's pinned adapter converts that
`httpx.TimeoutException` into `litellm.Timeout` with synthetic status 408.

Profile v4 raised the inactivity bound to 300 seconds. Five minutes was below the original
900-second agent limit and remains below the current 1200-second limit, while still bounding a
silent socket operation and allowing a slow provider call to remain eligible beyond one minute. It
is part of the profile digest because changing network wait policy changes execution behavior.
Profile v5 retains that bound and may make one counted recovery attempt. Profile v6 retains the same
bound; its fixed retry waits count against the agent wall budget. The budget was later raised
symmetrically from 900 to 1200 seconds after a matched cohort exhausted 900 seconds in every C cell
and one B cell. Only fresh rows use the new policy. The usage ledger records no fabricated response;
exhausting four attempts remains `infra_fail`.

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
  "provider_retry_count": 0,
  "provider_retry_delay_seconds": 0,
  "prompt_tokens": null,
  "completion_tokens": null,
  "total_tokens": null,
  "token_usage_status": "not_started | complete | incomplete",
  "provider_failure_category": null,
  "provider_failure_counts": {}
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
failed or a response omitted usage. That row contributes correctness but never token or wall-time
efficiency; its known lower-bound tokens, raw elapsed time, retry delay and failure category stay
visible in the raw JSON, while the report keeps the incomplete-usage gap visible. Token and wall-time
deltas use the same matched complete-usage rows, so fixed retry waiting cannot enter one efficiency
comparison while the corresponding unknown token cost is excluded from the other. A model-generated
**format error** is ordinary agent behavior because the provider answered; its usage is complete when
the response carried all three valid usage fields.

`validate_results()` refuses, before aggregation or rendering: a missing, blank, unknown or
malformed profile ID/digest; a row whose `model` is not the profile's requested model; a digest that
is not the tracked profile's; malformed metric fields, counts or status; negative, boolean, float,
numeric-string or partial token triples; a broken token identity; `not_started` carrying activity;
`complete` with zero attempts, unequal counts, null tokens or no returned model; attempts beyond the
reviewed four-per-call ceiling; a scored `incomplete` row with an unanswered model turn or no returned
model identity; and returned-model or budget drift inside an exact model variant. B/C rows from
different profile digests remain separate variants and are never paired.

## Why a failure category, and why a fixed vocabulary

Result schema `1.4.0` added one nullable string, `metrics.provider_failure_category`. When an accepted
attempt fails before returning a usable response, the run records **why**, so an operator can
distinguish an expired key from a rate limit or a dropped connection without a rerun. An early pilot
ended with two `infra_fail` cells whose rows said only that something failed.

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

Result schema `1.6.0` adds `provider_retry_count`, `provider_retry_delay_seconds` and
`provider_failure_counts`. The validator requires every retry to be backed by an allowlisted
retryable provider failure, permits failed attempts not followed by retries only for unresolved model
calls, and allows a completed retry wait to end in an internal exception that is deliberately not
misreported as a provider attempt. It also requires the delay total to be achievable by distributing
the fixed 4/8/16 schedule across model calls, requires the failure-count sum to equal unanswered
attempts, and requires the old summary category to exactly summarize the map.

Result schema `1.7.0` adds `history_compaction_count`, `history_dropped_groups`,
`history_dropped_items` and `history_max_prepared_bytes`. The validator binds them to the profile's
prepared-input ceiling, rejects impossible count relationships, and keeps content out of every
field.

## Why only run-level tokens

The composed single-agent run emits no reliable per-task completion event, so per-task token
attribution would be invented. Cost is also deliberately out of scope here: the proxy may not expose
a stable monetary price, and the matrix hypothesis is answerable with correctness, tokens and
time.

## Historical direct-proxy Responses decision, recorded after attempt 5

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
- The accepted matrix model is `CkbLitellmResponseModel`, benchmark-owned. Upstream's
  `LitellmResponseModel` is protocol-correct but retention-wrong: it stores the whole response in the
  returned message and in `FormatError`. Only the protocol is inherited.
- A Responses turn is replayed by sending its output items back, so the returned `function_call`
  items are preserved as protocol. Nothing else about the response survives: no text content, no
  response ID, no status, no raw body.
- **Reasoning is pinned, not inherited.** `reasoning_effort` and
  `reasoning_context: "prefix_tail_groups"` are profile fields, so each profile digest binds both
  the wire setting and local replay policy. Its controlled request and production model send the
  same selected effort; the context field describes local stateless replay and is not sent as an
  unsupported nested reasoning parameter. A moving alias must not choose reasoning for an accepted
  run.
- **The Responses conversation is explicitly stateless.** The profile requires `store: false`, and
  both the controlled request and production send it. OpenRouter documents its Responses API as
  stateless, so the harness owns the conversation and sends prepared history on every turn rather
  than combining local replay with provider-side response storage.
- **Long history is compacted locally and deterministically.** Every current profile pins
  `prefix-tail-groups-v1` and a 131,072-byte serialized-input ceiling. The harness preserves the
  initial instruction prefix and newest contiguous complete response/tool-observation groups,
  inserts one fixed compaction notice, and drops whole old groups only. A function call is never
  separated from its output. The same prepared bytes are deep-copied for every retry of that turn.
  Before a tool observation enters this history, its rendered text keeps a deterministic UTF-8 head
  and tail within the profile's 32,768-byte per-turn observation ceiling. This prevents arbitrary
  shell or MCP output from making the next request irreducible while retaining both the beginning
  and the usually diagnostic end. Unknown history fields, malformed pairs and a provider response
  that cannot fit still fail before the first provider request. Provider truncation remains
  disabled rather than delegating context loss to undocumented router behavior.
- **Replay removes only output-only `status` metadata.** A bounded HTTP reproduction established
  the provider's exact rejection as `unknown_parameter` for a prior output item's `status`; the
  identical replay succeeded after removing that field alone. The benchmark preserves item type,
  order, content, encrypted reasoning, IDs, call IDs, tool names and arguments. Completed-call
  status is validated before the item enters history, so removing it from the next request does not
  weaken executable-action validation.
- **Production sends no per-turn output ceiling.** A `max_output_tokens` cap would truncate a real
  coding turn and bias the five-task result, so its absence is the accepted matrix behavior. The
  controlled probe carries a probe-only ceiling of 4096: it bounds one compatibility request while
  leaving room for the configured reasoning effort plus a completed tool call.
- The controlled request proves endpoint, Responses/tool-call, returned-model and usage
  compatibility. It is **not** a byte-identical benchmark turn, and the profile does not claim it
  is: model, supported model settings, reasoning, stream mode, request timeout and the exact tool
  schema are shared; the output ceiling is deliberately probe-only.

## Why historical profile v7 changed API endpoints

The original shared proxy produced repeated request-specific `other_provider` failures even after
the bounded transient retry policy was added. That made clean matched cohorts unreliable, so the
project owner authorized moving the matrix model path to a more stable endpoint. Profile v7
selected the `openai/gpt-5-mini` alias and constrained its route to OpenAI only, with fallbacks
disabled and parameter support required. The alias was recorded honestly as moving; the catalog
exposed a dated canonical slug, but the requested alias itself was not immutable.

OpenRouter accepts the OpenAI Responses shape at `/responses`. The installed LiteLLM 1.72.0 OpenAI
Responses adapter preserves the OpenRouter catalog ID when the internal model is
`openai/openai/gpt-5-mini`, but drops its `extra_body` argument before the HTTP handler. A narrow
benchmark-owned handler therefore validates the exact URL and model, requires that no competing
route reached the boundary, and inserts only the profile-bound `provider` object at the request root.
Offline integration tests exercise the real LiteLLM transformation through a mock HTTP transport.
Any dependency behavior that starts supplying a competing route fails closed.

## Why historical profile v11 used DeepSeek V4 Flash

Profile v11 selects the dated `deepseek/deepseek-v4-flash-0731` snapshot on OpenRouter. A bounded
compatibility diagnostic established that OpenRouter's Responses router considers `relace/fp4`
eligible for the benchmark's tool and reasoning request, while the direct `deepseek` endpoint is
not eligible under the same required-parameter contract. The profile therefore pins
`relace/fp4`, disables fallbacks, requires parameter support and sends `high` reasoning. The model,
endpoint and route are fixed together; changing only an API key cannot change any of them.

## Controlled evidence contract

The current runnable catalog lives under `configs/models/` and is selected by alias. It includes
direct GPT-5.6, Sol, Luna and Terra profiles, plus routed profiles for DeepSeek V4 Flash, DeepSeek
V4 Pro 0813, Gemini 3.7 Flash and Ox Alpha. Every profile uses the 300-second provider request timeout,
transient-only four-attempt policy, deterministic history compaction and 32,768-byte observation
bound. The active schema-9 profile aliases and exact byte digests are:

- `deepseek-v4-flash`: SHA-256
  `079fab389ebc92259d79e30682f7489a175bda74a598cff589eb242e7faed2da`, pinned to
  `open-inference/fp4` for the dated `deepseek/deepseek-v4-flash-0731` snapshot;
- `deepseek-v4-pro`: SHA-256
  `7c3984ee7f0a12fc4c2b1fda55a0efc9a28c6454c157839da545929642d2c652`, pinned to the `alibaba`
  route after the `deepseek` route returned HTTP 404 for the same request shape;
- `gemini-3.7-flash`: SHA-256
  `630f313ed8185dcfd889c9ac7325634f6f10a8e2dfff0e2dfdc7aafd77f63468`, pinned to
  `google-vertex/global`;
- `ox-alpha`: SHA-256
  `3bf6565b21e88561b17ec0dd827d4467942da8d0a4dc8e16db82112575d90ad3`, pinned to `stealth`;
- `gpt-5.6`: SHA-256
  `d9237af220e98cfb0e93a5c3dea82a45c3d63e78840e461e15384720fd124b7a`;
- `gpt-5.6-luna`: SHA-256
  `f1378a5a8052acc603ebad9cfdb9e61fa8f077421c7661ee76b9ec1eec8fac41`;
- `gpt-5.6-sol`: SHA-256
  `2f51050b67792db0c2648d37235c6bbdb12ac7958718bb9b781cdd020ca6ead5`;
- `gpt-5.6-terra`: SHA-256
  `8888a52641f94bd98cdb9529161539b1906483cf667ed9e2d7112203463ba169`.

The routed wire shapes were each qualified with a completed, non-executed bash tool call before
becoming selectable. Schema 9 changes their configuration representation, not the endpoint, model,
reasoning, temperature, truncation or request-extension semantics established by those checks. Each
schema-9 profile records the exact schema-8 profile digest and finalized evidence digest from which
that qualification is inherited, rather than presenting the migrated bytes as directly probed. A
new profile qualified under schema 9 instead uses `direct-evidence-v1`; its finalized evidence binds
the current profile digest, so the profile does not create a circular hash reference to that record.

The current Flash and Gemini records live under `benchmark-output/provider-qualifications/` and are
excluded from version control. The Pro and Ox records live under
`research/provider-qualifications/`. None are benchmark result rows. Older profile evidence remains
historical. Profile v10 has SHA-256
`eca03ca33054a4789b5195a84efcbe484ad06fedc2352c266f2d691f2da83447`; profile v9 has historical SHA-256
`7d7bca8d95ad655f6dd143373f4a8b5ca3bb0efd9486f2acd8b344bd6fc1617f`; profile v8 has historical
SHA-256 `d0021bed7ae2a885933ba11d009ca6f33fdf801dda4940d4844e3f496cdd1362`; profile v7 has historical SHA-256
`977fe21a3bb300aac464210dd8950d254aa58150e278f53d4c670ca35b43c355`; profile v6 has historical SHA-256
`266c77ef67d6954a0daf4d9dfdff87d8d788995930f54769c279dffc58e2a275`; profile v5 has historical SHA-256
`ed9f7fa538d0f823fc2352c9c24f9a1cd1c36016d6c1b313a9b04e1c4ca804ab`; profile v4 has historical SHA-256
`0dcedaf346ccaac47ddd070dd27aedc12c5011e0b0b7bda69b1b1999f7ad8390`; profile v3 has historical SHA-256
`67544290765bdab32de1abbea48d20561abb74e90046c88d32cd27cffdf1fa1a`; profile v2 has historical SHA-256
`117f5d35d699e6200b4d9fb96fce724947b57bfc63c3a5620467f088c90f4ade`.

DeepSeek Flash evidence is bound to these retained checks:

- **The current lower-cost OpenInference route succeeded** — at `2026-08-23T12:56:24Z`, exactly
  one authenticated OpenRouter Responses request requested and returned
  `deepseek/deepseek-v4-flash-0731`, completed one expected bash call without executing it, and
  reported `297 + 78 = 375` native tokens. Its finalized sanitized record is
  `benchmark-output/provider-qualifications/openrouter-deepseek-v4-flash-open-inference-v2.json`
  and carries the exact v2 profile digest.

- **One pinned OpenRouter Responses compatibility request succeeded** — at
  `2026-08-21T22:27:08Z`, exactly one authenticated `POST` to
  `https://openrouter.ai/api/v1/responses` requested and returned
  `deepseek/deepseek-v4-flash-0731`, completed one expected bash call without executing it, and
  reported `302 + 73 = 375` native tokens. The finalized sanitized evidence is
  `research/handoff/deepseek-v4-flash-relace-completion-evidence.json` (SHA-256
  `99c56f0b31a4d65ff2701869d9f10481adbdc21ff30d9b93076f547169d09c91`) and carries the exact v11
  profile digest. The observation limit is a local replay policy and is covered by deterministic
  offline tests, not by this one-turn wire check.

Other OpenRouter compatibility evidence remains retained:

- **The current lower-cost Google Vertex route succeeded** — at `2026-08-23T12:56:48Z`, exactly
  one authenticated OpenRouter Responses request requested and returned `google/gemini-3.7-flash`,
  completed one expected bash call without executing it, and reported `41 + 74 = 115` native
  tokens. Its finalized sanitized record is
  `benchmark-output/provider-qualifications/openrouter-gemini-3.7-flash-google-vertex-v2.json` and
  carries the exact v2 profile digest.

- **One OpenRouter Responses compatibility request succeeded** — at
  `2026-08-21T06:42:42Z`, exactly one authenticated `POST` to
  `https://openrouter.ai/api/v1/responses` requested and returned `openai/gpt-5-mini`, completed one
  expected bash call without executing it, and reported `63 + 151 = 214` native tokens. The
  finalized sanitized evidence is `research/handoff/56-openrouter-completion-evidence.json`
  (SHA-256 `9d0607b28b5495b3b17ab2157b539cf0c4b2c2cfd4be6da45dba9aa30b77408d`) and carries the exact v7
  profile digest. It proves the retained route and wire shape; profile v8's replay behavior is
  separately covered by deterministic offline tests and the bounded live qualification recorded
  with the cohort. No failure diagnostic was produced.

The current direct high-reasoning profile is bound to this retained check:

- **One direct Responses compatibility request succeeded** — at
  `2026-08-22T02:27:38Z`, exactly one authenticated `POST` to
  `https://share-ai.ckbdev.com/responses` requested and returned `gpt-5.6-sol`, completed one
  expected bash call without executing it, and reported `4,443 + 23 = 4,466` native tokens. The
  finalized sanitized evidence is retained with the originating qualification record and carries
  profile SHA-256 `be96fc5e42ea2e42b43c2b29687568fc13b9e891226fa71e22177b4cbd77db47`.

The Luna and Terra profiles use the same direct Responses contract. Their bounded
compatibility checks requested and returned `gpt-5.6-luna` at `2026-08-22T20:15:34Z` and
`gpt-5.6-terra` at `2026-08-22T16:49:39Z`, respectively. Both completed the expected non-executed
bash call with valid native token identities. The tracked profile SHA-256 values are
`eb56b9b4a70c70afdbc5062bf41a70ea1ae88d76c82d2f1267bc6cc974782f3c` for Luna and
`7d2820b0196f834580d8c7d0ed8354504a952ee2faf3105759ef65da192f6343` for Terra.

The base GPT-5.6 profile uses that same direct contract. Its bounded compatibility check requested
and returned `gpt-5.6` at `2026-08-26T05:26:10Z`, completed the expected non-executed bash call and
reported `4,443 + 23 = 4,466` native tokens. The finalized sanitized record is
retained with the originating qualification output and carries profile SHA-256
`0cc40c12924b73c3eccb2a198ea97ce1d85b5625322aba5088fa30024f0646e4`.

Historical direct-endpoint compatibility evidence also remains retained:

- **One Responses compatibility request succeeded** — at
  `2026-08-21T16:29:57Z`, exactly one authenticated `POST` to
  `https://share-ai.ckbdev.com/responses` requested and returned `gpt-5.6-sol`, completed one
  expected bash call without executing it, and reported `4,443 + 23 = 4,466` native tokens. The
  finalized sanitized evidence is retained with the originating handoff (SHA-256
  `7acc0f80f4bfa1a4ea6518061616dd52ef7bdd481d9878553fe3c75ce68597b8`) and carries the exact v10
  profile digest.

Earlier direct-endpoint evidence also remains retained:

- **One catalog request succeeded** — `GET https://share-ai.ckbdev.com/models`, 2xx, 12 sanitized
  GPT candidates in `research/handoff/17-catalog-evidence.json`. `gpt-5.6-sol` was selected from
  that list by the user and is recorded `moving_alias`.
- **Five historical chat attempts** are recorded in `research/handoff/17-provider-request-log.md`.
  The last of them refuted the chat contract and produced
  `research/handoff/17-completion-diagnostic.json`.
- **The original Responses compatibility request succeeded** and established the v1 model,
  endpoint, tool-call and usage shape. A later qualification repeated the same one-request contract with
  `store: false`; `research/handoff/25-stateless-responses-evidence.json` binds the successful
  response to profile v2 and its digest. Both calls returned `gpt-5.6-sol` with one completed bash
  call and native usage satisfying `input_tokens + output_tokens = total_tokens`; neither returned
  call was executed.
- **The follow-up request proved the protocol under a 60-second timeout.** Profile v3 made that bound
  mandatory in production. A later cohort then captured the exact limitation of that policy: eight normal
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
  invisible here. The four-attempt ceiling bounds that gap, and any recovered row is excluded from
  token and wall-time efficiency deltas, but neither measure reveals the failed attempt's cost. Raw
  elapsed time and scheduled retry delay remain retained as operational evidence.
- **The 1200-second agent wall value is a between-actions limit, not an exact process deadline.** A
  provider, MCP or shell action may finish after the limit is crossed. The model request now has
  300-second connect/read/write/pool inactivity limits, but not a 300-second total-duration deadline;
  an external supervisor remains responsible for an exact whole-process deadline if one is required.
- **A moving alias remains a risk when no dated snapshot exists.** The profile records the
  classification honestly, and every run re-checks the returned model identity and fails closed on
  drift, but an alias that changes behavior without changing its name is not detectable from usage
  alone.
- **Local compaction changes what the model can see.** The policy is symmetric across B and C and
  its exact drop counts are retained, but a long run can still lose old conversational detail. A
  comparison must disclose asymmetric compaction between arms rather than treating it as invisible.
- **This ADR makes no effectiveness claim.** It fixes the model path and evidence rules; benchmark
  results must still meet the report's declared cohort gates.

## The diagnostic artifact is not accepted evidence

`provider_failure_category` is the accepted triage signal and stays exactly as specified above. When
it is not enough — an early pilot ended with two `request` rows and no way to tell a `BadRequestError` from a
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
- **No diagnostic field enters accepted evidence.** Accepted rows use the current schema; its retry and replay
  fields come from the ordinary sanitized usage ledger, and no report ever
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
