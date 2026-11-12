# Upstream notes (Phase 0)

Checked on 2026-11-12 against https://aistudio.google.com/docs/decisions-api (Gemini side) and
https://docs.typesafe.ai/api (System One side). Both pages were read through a summarising fetch
tool, not a raw HTML download, so the "still uncertain" items below matter.

## Confirmed Gemini field names (no differences from BRIEF section 2.2)

- `POST /v1beta/decisions`, header `x-goog-api-key`, JSON body `{model, input, questions}`.
- `input`: string, `Content`, or `Content[]`; must not be empty. Blocks: `text` (`text`),
  `image` (`data` base64 or `uri`, `mime_type`, optional `resolution` in
  `low|medium|high|ultra_high`). Audio, video and PDF (`document`) blocks exist but are out of scope.
- Questions: map, 1-1000 entries, keys `[A-Za-z0-9_-]`, up to 64 chars. Each has `type`
  (`noul|choice|score`), `instructions` (text only), `criteria`.
- choice criteria: `{option: description}`, empty string allowed. score criteria: array of
  non-empty strings, lowest first, 2-255 levels.
- Answers: noul `{noul, confidence}`; choice `{choice, probabilities, confidence}`; score
  `{score, legend, probabilities, confidence}`.
- `usage`: `total_input_tokens`, `input_tokens_by_modality` (array of `{modality, tokens}`),
  `total_output_tokens` (always 0), `total_tokens`.
- Errors: HTTP 400 with an `error` object holding `code` and `message`. Documented safety codes:
  `safety`, `prohibited_content`, `blocklist`, `image_safety`, `content_blocked`. Limit and key
  violations are also HTTP 400 with a message naming the rule.

## The four Phase 0 questions

| Question | Finding | How the code handles it |
|---|---|---|
| Does each answer carry `type`? | The reference lists an answer `type` per answer kind, so it appears to. Not verified against a raw response. | Accepted with or without `type`. If present it must match the question type (else 502); if absent the expected answer field must be present. |
| `legend`/`probabilities` keys: string or int? | Docs say strings (`"0"`, `"1"`). JSON object keys are always strings on the wire anyway. | Parsed as `str | int` and normalised to strings. |
| Exact 400 body shape? | `{"error": {"code", "message"}}`, shown in full only for `safety`. Google's generic errors may use an integer `code` (for example 400) with a `status` field. | Reads `error.code` and `error.message` when they are a dict/strings. Only a *string* code in the safety set maps to `detail.code`. Anything else (non-JSON, odd shapes, int codes) becomes `upstream rejected request: <message or empty>`. The API key is stripped from the message. |
| `usage` field names? | As in the table above. | `total_input_tokens` maps to `input_tokens`. A missing `usage` is tolerated and counts as 0. |

## Still uncertain (chose the BRIEF reading)

- **noul `criteria`.** The reference table says `criteria` is not used for noul, but the examples
  pass `true_description` / `false_description`. We follow the BRIEF and send them (omitted when
  both are null/missing). If upstream starts rejecting them, the fix is one function in
  `translate.py` (`_translate_noul`).
- **Option key limit.** 128 characters and 512 UTF-8 bytes are both stated. We enforce both. A
  128-character key can only exceed 512 bytes if code points average over 4 bytes, so in practice
  the character limit decides.
- **`instructions` as text blocks.** The reference allows `TextContent[]` but the limits section
  says text only. We always send a plain string.
- **Rate limits and retry guidance** are not documented (only a link to a rate-limits page). We assume
  standard Google behaviour: 429 and 503 are transient, `Retry-After` may be present.
- **Non-400 error bodies** (401/403/404/5xx) are not documented. We never parse or echo them.

## System One side (docs.typesafe.ai/api)

- Request and response shapes match BRIEF section 2.1. The reference allows 1-10 score levels and
  1-255 choice options; Gemini needs at least 2 of each, so the gateway returns 400 for a single
  level or a single option.
- `GET /v1/models` has **no documented response shape**. We return an OpenAI-style list:
  `{"object":"list","data":[{"id","object":"model","owned_by"}]}`. Clients that only read `id`s will work.
- `HTTP 529`, `Retry-After`, and the `images` field (aimlapi variant) are not mentioned by TypeSafe.
  They follow the BRIEF.
- The multipart alternative (`request` part plus one part per image) is rejected with 415.

## Design decisions where the docs could not settle things

- **Unsupported asset formats** (for example `gif`) fail request validation, so the client gets
  FastAPI's default 422. Invalid base64 and unreferenced assets are 400.
- **Asset references are rewritten in values only**, not in choice option keys. Option keys are ids
  that come back in the answer, so renaming them would break the reverse mapping.
- **Asset keys are matched against the serialised text.** A key containing a `"` or `\` would appear
  JSON-escaped inside an object `state` and would not match; the gateway then answers 400 "not
  referenced" rather than guessing.
- **Authentication applies to `POST /v1/systemone` only.** `/v1/models`, `/healthz` and `/readyz`
  stay open so probes and `helm test` work when `INBOUND_API_KEYS` is set.
- **Redaction ignores secrets shorter than 4 characters**, because replacing every "a" would make
  logs unreadable.
