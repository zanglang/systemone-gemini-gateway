# Implementation brief: `systemone-gemini-gateway`

You are implementing a small, production-quality HTTP service in **Python 3.12 + FastAPI**. It accepts requests in **TypeSafe's System One API format** (the "Jev" API, `POST /v1/systemone`), translates them to **Google's Gemini Decisions API** (`gemini-decision-1`, `POST /v1beta/decisions`), and translates the answers back. It exposes **basic OpenTelemetry metrics** and will eventually run on a homelab Kubernetes cluster.

### The project has two parts. You are building Part 1 only.

| | Part 1: this brief | Part 2: NOT in scope |
|---|---|---|
| What | The wrapper service, its tests, a **GitHub Actions pipeline**, and a **Helm chart** | Deploying to the homelab cluster |
| Delivers | A **multi-arch Docker image** published to a container registry. The chart lives in the repo, linted and tested. | A running release |
| Includes | Code, unit/API/metrics tests, Dockerfile, chart templates, chart tests, CI/CD workflows | Cluster-specific values files, the `SecretStore`/`ClusterSecretStore` and the secret's value in the backing vault (the chart only renders the `ExternalSecret` that points at them), `helm install` against a real cluster, GitOps (Argo CD/Flux), ingress/DNS/TLS for the homelab, collector or Prometheus setup |

Don't write anything that targets a real cluster. A throwaway `kind` cluster inside CI, used only to test the chart, is fine (`chart-install` job, §10.1).

Work through the phases in order. Do not skip Phase 0.

---

## Phase 0: Check the upstream contract before coding

The Gemini Decisions API launched on **2026-11-11** and is `v1beta`. Field names may have changed since this brief was written. Before writing the translator:

1. Read https://aistudio.google.com/docs/decisions-api and confirm every Gemini-side field name in the mapping tables below.
2. Confirm these points, which the docs leave unclear:
   - Does each answer object include a `type` field?
   - Are `legend` and score `probabilities` keyed by string indices (`"0"`, `"1"`) or by integers?
   - What is the exact JSON shape of a 400 error body (`{"error": {"code", "message"}}` or something else)?
   - What are the `usage` field names?
3. If anything differs from this brief, follow the live docs, write down each difference in `docs/UPSTREAM_NOTES.md`, and keep going.

The System One reference is https://docs.typesafe.ai/api.

---

## 1. Scope

### In scope (v1)
- `POST /v1/systemone`: translate the request, call Gemini, translate the response.
- `GET /v1/models`: return the models the gateway serves, in TypeSafe's list format.
- Question types `noul`, `choice`, `score`.
- Image `assets` sent as **JSON** (base64), mapped to Gemini inline image blocks.
- Optional inbound bearer-token auth. The Gemini key lives only in the gateway.
- Upstream retries with backoff on 429, 503, and timeouts.
- OTEL metrics through OTLP push and/or a Prometheus pull endpoint.
- `/healthz`, `/readyz`.
- Dockerfile (multi-arch: amd64 and arm64).
- Helm chart (§9.2).
- GitHub Actions pipeline that tests everything and publishes the image (§10).
- Tests: unit, API-level with a mocked upstream, metrics, chart, image smoke, and an opt-in live smoke test.

### Out of scope (v1). Reject cleanly or leave out
- `box` questions: Gemini has no equivalent. Return **400**.
- `multipart/form-data` requests (TypeSafe's alternate asset upload): return **415** with a clear `detail`.
- Audio, video, and PDF input (Gemini supports them, System One does not).
- Vertex AI as the upstream. Only the Gemini Developer API is supported.
- Streaming, batching, caching, traces, and an admin UI.
- Kustomize manifests or raw YAML. The Helm chart is the only packaging.
- Everything listed under Part 2 above.

---

## 2. API contracts

### 2.1 Inbound: System One (what clients send)

```
POST /v1/systemone
Authorization: Bearer <token>        # checked only if INBOUND_API_KEYS is set
Content-Type: application/json
TypeSafe-Danger-Zone-Images: true    # accept and ignore
TypeSafe-Danger-Zone-Long-Context: true  # accept and ignore
```

Body:
| Field | Type | Notes |
|---|---|---|
| `model` | string | Any value is accepted (`jev-latest`, `typesafe/jev-1.14`, …). Every request is routed to `GEMINI_MODEL`. |
| `state` | string \| object \| array | The content to evaluate. |
| `questions` | object, ≥1 key | Key = client's id. Value = question. |
| `assets` | object, optional | `{ "<ref>": { "type": "image", "format": "png"\|"jpg"\|"jpeg", "data_base64": "..." } }` |

Question shapes (System One):
- `noul`: `{ "type": "noul", "instructions": X, "criteria"?: { "true"?: X|null, "false"?: X|null } }`
- `choice`: `{ "type": "choice", "instructions": X, "criteria": { "<option>": X|null, ... } }` (1–255 options)
- `score`: `{ "type": "score", "instructions": X, "criteria": [X, X, ...] }` (levels ordered lowest-first, index from 0)
- `box`: unsupported, return 400.

`X` = string, object, or array. Unknown top-level fields are ignored. Unknown question fields are ignored.

Response (200):
```json
{
  "model": "gemini-decision-1",
  "answers": {
    "<client id>": { "type": "noul", "noul": 0.87 },
    "<client id>": { "type": "choice", "choice": "billing", "confidence": 0.91, "probabilities": { "billing": 0.91, "bug": 0.09 } },
    "<client id>": { "type": "score", "score": 1.42, "confidence": 0.66, "legend": { "0": "...", "1": "...", "2": "..." }, "probabilities": { "0": 0.1, "1": 0.38, "2": 0.52 } }
  },
  "usage": { "input_tokens": 412, "output_tokens": 0 }
}
```

Error bodies must use TypeSafe's (FastAPI-style) shapes:
- 400: `{ "detail": "<string>" }` or `{ "detail": { "code": "...", "message": "..." } }`
- 422: FastAPI's default `HTTPValidationError` (`detail: [{loc, msg, type, input, ctx}]`). FastAPI/Pydantic produces this by default, so don't override it.

### 2.2 Outbound: Gemini Decisions API (what the gateway sends)

```
POST {GEMINI_BASE_URL}/v1beta/decisions
x-goog-api-key: {GEMINI_API_KEY}
Content-Type: application/json
```
| Field | Notes |
|---|---|
| `model` | `GEMINI_MODEL` (default `gemini-decision-1`) |
| `input` | string, `Content`, or `Content[]`. Must not be empty. No `state` field. |
| `questions` | map, 1–1000 entries. Key regex `^[A-Za-z0-9_-]{1,64}$`. |

Gemini question: `{ "type": "noul"|"choice"|"score", "instructions": "<text only>", "criteria": ... }`
- noul `criteria` (optional): `{ "true_description": "...", "false_description": "..." }`
- choice `criteria`: `{ "<option id>": "<description or empty string>" }`, 2–255 options. Option key ≤128 chars / ≤512 UTF-8 bytes.
- score `criteria`: array of **non-empty** strings, 2–255 levels, lowest first.

Content blocks: `{ "type": "text", "text": "..." }`, `{ "type": "image", "data": "<base64>", "mime_type": "image/png", "resolution"?: "low"|"medium"|"high"|"ultra_high" }`. **Adjacent text blocks are concatenated with no separator**, so add newlines yourself.

Gemini answers: noul `{noul (0.01–0.99), confidence}`, choice `{choice, probabilities (sum 1, unrounded), confidence}`, score `{score, legend, probabilities, confidence}`.
Usage: `total_input_tokens`, `input_tokens_by_modality`, `total_output_tokens` (always 0), `total_tokens`.
Errors: HTTP 400 with `error.code` in `safety | prohibited_content | blocklist | image_safety | content_blocked`, and HTTP 400 for limit or key violations.

**Use raw `httpx.AsyncClient`, not the `google-genai` SDK.** The SDK's `client.decisions.create` needs google-genai ≥3.2, which is one day old. Raw HTTP is easier to mock with `respx`, keeps the image small, and gives exact control over retries and error mapping. Isolate the upstream call behind a small `GeminiDecisionsClient` class so it can be swapped later.

---

## 3. Translation rules (the core of the service)

Put every rule in `translate.py` as pure functions with no I/O. Most of the tests target this module.

### 3.1 Request: System One → Gemini

| System One | Gemini | Rule |
|---|---|---|
| `model` (any) | `model` = `GEMINI_MODEL` | Record the inbound value only in debug logs. Never use it as a metric attribute. |
| `state` string | `input` string | Pass through unchanged. An empty string is rejected by Gemini, so return 400 `"state must not be empty"`. |
| `state` object/array | `input` string | `json.dumps(state, ensure_ascii=False, separators=(",", ":"))`. Keep key order. |
| question keys | `q0`, `q1`, … `qN` | **Always** remap in insertion order and keep an `id_map` for the reverse step. Client keys can be any string (spaces, unicode, >64 chars). |
| `instructions` (non-string) | string | JSON-serialize as above. |
| noul `criteria.true` / `.false` | `true_description` / `false_description` | Serialize non-strings. Omit when `null` or missing. Omit `criteria` entirely if both are omitted. |
| choice `criteria` value `null` | `""` | Serialize non-string values. |
| choice with 1 option | n/a | 400: `"choice question '<id>' needs at least 2 options for gemini-decision-1"` |
| choice option key >128 chars or >512 bytes | n/a | 400 that names the question id. |
| score levels | same array | Serialize non-strings. Empty or `null` level → 400. Fewer than 2 levels → 400. More than 10 → 400 (System One max). |
| `box` question | n/a | 400: `"box questions are not supported by this gateway (gemini-decision-1 has no bounding-box type)"` |
| questions > 1000 | n/a | 400 before calling upstream. |
| Danger-Zone headers | n/a | Accept and ignore. |

### 3.2 Assets (images)

System One references an asset by placing its key in `state` text, an instruction, or a criterion. The key can appear on its own or inside backticks within longer text. Every asset must be referenced. Algorithm:

1. Validate each asset: `type` ∈ {`image`, absent}, `format` ∈ {png, jpg, jpeg} (map to `image/png` or `image/jpeg`), `data_base64` decodes with `base64.b64decode(..., validate=True)`. Failures → 400 naming the asset key.
2. Give each asset a stable label `<image N>` (1-based, in `assets` insertion order).
3. Serialize state, instructions, and criteria to text (§3.1). Then replace every reference with its label. Replace **longest keys first** so `img` doesn't clobber `img2`, and when a reference is wrapped in backticks, replace the backticks too.
4. If any asset key appears nowhere → 400 `"asset '<key>' is not referenced in state, instructions, or criteria"`.
5. Build `input` as `Content[]`:
   `[{text: <state text>}, {text: "\n\n<image 1>:\n"}, {image 1}, {text: "\n\n<image 2>:\n"}, {image 2}, ...]`
6. Set `resolution` from `GEMINI_IMAGE_RESOLUTION` when it is set. Otherwise omit it.
7. If a non-empty `images` field is present (an aimlapi variant) → 400 `"use assets for images"`.

If there are no assets, `input` is a plain string.

### 3.3 Response: Gemini → System One

| Gemini | System One | Rule |
|---|---|---|
| `answers.qN` | `answers.<client id>` | Reverse `id_map`. Keep the client's original key order. |
| noul `{noul, confidence}` | `{type:"noul", noul}` | **Drop `confidence`.** System One noul answers don't have it. |
| choice | `{type:"choice", choice, confidence, probabilities}` | Pass probabilities through **unrounded**. |
| score | `{type:"score", score, confidence, legend, probabilities}` | Normalize `legend` and `probabilities` keys to strings `"0"`…`"N"`. |
| missing answer for any `qN` | n/a | 502 `"upstream returned no answer for '<client id>'"` |
| answer `type` mismatch | n/a | 502 |
| `usage.total_input_tokens` | `usage.input_tokens` | `output_tokens` is always `0`. |
| n/a | `model` | Use the upstream model actually called (`GEMINI_MODEL`). |

Add an `X-Request-Id` response header. Reuse the inbound header if present, otherwise generate a UUID4.

### 3.4 Worked example

Inbound:
```json
{
  "model": "jev-latest",
  "state": {"ticket": "Checkout is blank after I click Pay", "tier": "enterprise"},
  "questions": {
    "is it a bug?": {"type": "noul", "instructions": "Is this a bug report?", "criteria": {"true": "Describes broken behaviour", "false": null}},
    "severity": {"type": "score", "instructions": "How severe?", "criteria": ["Cosmetic", "Degraded", "Blocking"]},
    "team": {"type": "choice", "instructions": "Which team owns it?", "criteria": {"payments": "Checkout and billing", "web": null}}
  }
}
```
Outbound:
```json
{
  "model": "gemini-decision-1",
  "input": "{\"ticket\":\"Checkout is blank after I click Pay\",\"tier\":\"enterprise\"}",
  "questions": {
    "q0": {"type": "noul", "instructions": "Is this a bug report?", "criteria": {"true_description": "Describes broken behaviour"}},
    "q1": {"type": "score", "instructions": "How severe?", "criteria": ["Cosmetic", "Degraded", "Blocking"]},
    "q2": {"type": "choice", "instructions": "Which team owns it?", "criteria": {"payments": "Checkout and billing", "web": ""}}
  }
}
```
Use this pair as the first golden fixture.

---

## 4. Error mapping

| Condition | Status | Body |
|---|---|---|
| Inbound JSON or schema invalid | 422 | FastAPI default |
| Translation rule violated (§3) | 400 | `{"detail": "<message>"}` |
| `multipart/form-data` | 415 | `{"detail": "multipart requests are not supported; send assets as JSON"}` |
| Body > `MAX_BODY_BYTES` | 413 | `{"detail": "request body too large"}`. Check `Content-Length` first, then enforce while reading. |
| Inbound auth missing or wrong (when enabled) | 401 | `{"detail": "invalid or missing bearer token"}` + `WWW-Authenticate: Bearer` |
| Upstream 400 with safety-type `error.code` | 400 | `{"detail": {"code": "<code>", "message": "<upstream message>"}}` |
| Upstream 400 other | 400 | `{"detail": "upstream rejected request: <upstream message>"}` |
| Upstream 401 / 403 / 404 | 502 | `{"detail": "upstream configuration error"}`. Log the upstream status. **Never** echo the upstream body or the key. |
| Upstream 429 after retries | 429 | `{"detail": "rate limited by upstream"}`. Pass `Retry-After` through if present. |
| Upstream 503 after retries | 529 | `{"detail": "upstream overloaded"}` (matches TypeSafe's overloaded code) |
| Upstream other 5xx | 502 | `{"detail": "upstream error"}` |
| Upstream timeout / connect error after retries | 504 | `{"detail": "upstream timeout"}` |
| Upstream 200 but malformed | 502 | `{"detail": "invalid upstream response"}` |

**Retries:** at most `UPSTREAM_MAX_RETRIES` (default 2) on 429, 503, timeouts, and connect errors. Use exponential backoff with full jitter (base 0.25 s, cap 4 s). Honour `Retry-After` up to the cap. Never retry other 4xx. Make the sleep function injectable so tests run instantly.

---

## 5. Configuration (env vars, parsed with `pydantic-settings`)

| Var | Default | Notes |
|---|---|---|
| `GEMINI_API_KEY` | required | From a k8s Secret. Fail fast at startup if missing. |
| `GEMINI_BASE_URL` | `https://generativelanguage.googleapis.com` | Overridden in tests. |
| `GEMINI_MODEL` | `gemini-decision-1` | |
| `GEMINI_IMAGE_RESOLUTION` | unset | `low\|medium\|high\|ultra_high` |
| `INBOUND_API_KEYS` | empty | Comma-separated. Empty means auth is disabled; log one WARNING at startup. Compare with `hmac.compare_digest`. |
| `UPSTREAM_TIMEOUT_SECONDS` | `30` | |
| `UPSTREAM_MAX_RETRIES` | `2` | |
| `MAX_BODY_BYTES` | `20971520` (20 MiB) | |
| `ADVERTISED_MODELS` | `jev-latest,gemini-decision-1` | Returned by `GET /v1/models`. |
| `LOG_LEVEL` | `INFO` | |
| `LOG_FORMAT` | `json` | `json\|text` |
| `OTEL_SERVICE_NAME` | `systemone-gemini-gateway` | Standard OTEL env. |
| `OTEL_METRICS_EXPORTER` | `prometheus` | `otlp`, `prometheus`, `otlp,prometheus`, `none` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | unset | e.g. `http://otel-collector.observability:4318` |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `http/protobuf` | |
| `METRICS_PORT` | `9464` | Prometheus scrape port. Keep it separate from the API port. |
| `PORT` | `8080` | |

---

## 6. OpenTelemetry metrics

Set up the `MeterProvider` explicitly in `telemetry.py`. Do not use `opentelemetry-instrument` auto-config, so the exporter choice above is honoured and tests can inject an `InMemoryMetricReader`. Resource attributes: `service.name`, `service.version` (from package metadata), `k8s.pod.name` and `k8s.namespace.name` (from env, via the downward API).

| Instrument | Kind / unit | Attributes |
|---|---|---|
| `http.server.request.duration` | histogram, s | From `opentelemetry-instrumentation-fastapi`. Exclude `/healthz`, `/readyz`, `/metrics`. |
| `gen_ai.client.operation.duration` | histogram, s | `gen_ai.operation.name="decide"`, `gen_ai.provider.name="gcp.gemini"`, `gen_ai.request.model`, `error.type` (only on failure) |
| `gen_ai.client.token.usage` | histogram, {token} | `gen_ai.token.type="input"`, `gen_ai.provider.name`, `gen_ai.request.model` |
| `s1gw.requests` | counter | `outcome` ∈ {`ok`,`client_error`,`blocked`,`upstream_error`,`timeout`}, `http.response.status_code` |
| `s1gw.questions` | counter | `question.type` ∈ {`noul`,`choice`,`score`} |
| `s1gw.questions_per_request` | histogram | none. Use explicit buckets `[1,2,5,10,25,50,100,250,500,1000]`. |
| `s1gw.images` | counter | none |
| `s1gw.upstream.retries` | counter | `reason` ∈ {`429`,`503`,`timeout`,`connect`} |

**Cardinality rule (enforced by a test):** attribute values come only from fixed enums or from config. Never use a client question id, an inbound `model` string, state content, an asset key, or an error message as an attribute value.

Upstream duration covers the full call, retries included. Record it once per inbound request.

---

## 7. Logging and security

- Structured JSON logs to stdout. One access-style line per request: request id, status, duration, question count, image count, upstream status, retries.
- **Never log** `state`, instructions, criteria, asset data, inbound tokens, or `GEMINI_API_KEY`. Add a logging filter that redacts anything matching the configured keys, as defense in depth.
- Strip inbound `Authorization` before calling upstream. Only `x-goog-api-key` goes out.
- Run uvicorn with **one worker per pod** (several workers in one process break OTEL metric aggregation). Scale with replicas.
- Create one shared `httpx.AsyncClient` in the app lifespan and close it on shutdown.

---

## 8. Repository layout

```
systemone-gemini-gateway/
├── pyproject.toml            # uv-managed; ruff, mypy --strict, pytest config
├── uv.lock
├── Makefile                  # lint, typecheck, test, test-live, image, image-smoke, chart-lint, chart-test
├── Dockerfile
├── .dockerignore
├── .github/
│   ├── workflows/ci.yml      # PRs + main: lint, test, chart, image build (§10)
│   ├── workflows/release.yml # tags v*: publish image, attach chart to GitHub Release
│   ├── workflows/live.yml    # workflow_dispatch only: live Gemini smoke test
│   └── dependabot.yml        # github-actions, pip (uv), docker
├── README.md                 # what it does, mapping table, config, deploy, limitations
├── docs/UPSTREAM_NOTES.md    # Phase 0 findings
├── src/s1gw/
│   ├── __init__.py
│   ├── main.py               # app factory create_app(settings, meter_provider=None, transport=None)
│   ├── config.py
│   ├── models_systemone.py   # Pydantic v2 inbound/outbound schemas
│   ├── models_gemini.py
│   ├── translate.py          # pure functions (§3)
│   ├── upstream.py           # GeminiDecisionsClient + retry policy
│   ├── errors.py             # exception types → HTTP mapping (§4)
│   ├── auth.py
│   ├── telemetry.py
│   └── logging.py
├── tests/
│   ├── conftest.py
│   ├── fixtures/{systemone,gemini}/*.json   # golden request/response pairs
│   ├── unit/test_translate_request.py
│   ├── unit/test_translate_assets.py
│   ├── unit/test_translate_response.py
│   ├── unit/test_retry_policy.py
│   ├── api/test_systemone_endpoint.py
│   ├── api/test_error_mapping.py
│   ├── api/test_auth.py
│   ├── api/test_misc_endpoints.py
│   ├── telemetry/test_metrics.py
│   ├── test_logging_redaction.py
│   └── live/test_live_smoke.py              # @pytest.mark.live
├── scripts/
│   ├── image_smoke.sh                       # run image, curl /healthz and /metrics
│   └── bench.py
└── charts/systemone-gemini-gateway/
    ├── Chart.yaml
    ├── values.yaml
    ├── values.schema.json
    ├── ci/                                  # values files rendered in CI
    │   ├── externalsecret-values.yaml       # the primary path
    │   ├── existingsecret-values.yaml
    │   └── all-features-values.yaml
    ├── templates/
    │   ├── _helpers.tpl
    │   ├── deployment.yaml
    │   ├── service.yaml
    │   ├── serviceaccount.yaml
    │   ├── configmap.yaml
    │   ├── externalsecret.yaml              # only when externalSecret.enabled
    │   ├── secret.yaml                      # only when secret.create=true
    │   ├── servicemonitor.yaml              # only when serviceMonitor.enabled
    │   ├── networkpolicy.yaml               # only when networkPolicy.enabled
    │   ├── ingress.yaml                     # only when ingress.enabled
    │   ├── NOTES.txt
    │   └── tests/test-connection.yaml       # `helm test` hook: GET /healthz
    └── tests/                               # helm-unittest suites
        ├── deployment_test.yaml
        ├── externalsecret_test.yaml
        ├── secret_test.yaml
        ├── servicemonitor_test.yaml
        ├── networkpolicy_test.yaml
        └── ingress_test.yaml
```

Dependencies. Pin minimum versions and let `uv` resolve the rest:
- runtime: `fastapi`, `uvicorn[standard]`, `pydantic>=2`, `pydantic-settings`, `httpx`, `opentelemetry-api`, `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http`, `opentelemetry-exporter-prometheus`, `opentelemetry-instrumentation-fastapi`, `prometheus-client`
- dev: `pytest`, `pytest-asyncio`, `pytest-cov`, `respx`, `ruff`, `mypy`

---

## 9. Container image and Helm chart

### 9.1 Docker image (the deliverable)

- Multi-stage build. The builder stage uses `uv sync --frozen --no-dev`. Runtime is `python:3.12-slim`, pinned by digest and kept current by Dependabot.
- Run as non-root UID/GID 10001. `EXPOSE 8080 9464`.
- `CMD ["uvicorn", "s1gw.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--no-access-log"]`
- Must run with a read-only root filesystem. Set `PYTHONDONTWRITEBYTECODE=1` and `PYTHONUNBUFFERED=1`. Anything that needs scratch space uses `/tmp`, which the chart mounts as an `emptyDir`.
- OCI labels: `org.opencontainers.image.source`, `.revision`, `.version`, `.created`, `.licenses`. Set them through `docker/metadata-action` in CI.
- `HEALTHCHECK` is not needed (Kubernetes probes cover it).
- `.dockerignore` excludes tests, charts, `.github`, `.venv`, and caches.
- Platforms: `linux/amd64` and `linux/arm64`.

### 9.2 Helm chart (`charts/systemone-gemini-gateway`)

The chart ships in the repo and is fully tested in CI. It is **not** installed to any real cluster in Part 1.

**`Chart.yaml`:** `apiVersion: v2`, `type: application`, `version` (chart semver, starting at `0.1.0`), and `appVersion` (the image version). `kubeVersion: ">=1.27.0-0"`.

**`values.yaml`** (every key documented with a comment and enforced by `values.schema.json`):
```yaml
replicaCount: 1
image:
  repository: ghcr.io/zanglang/systemone-gemini-gateway
  tag: ""            # defaults to .Chart.AppVersion
  digest: ""         # if set, used instead of tag
  pullPolicy: IfNotPresent
imagePullSecrets: []
nameOverride: ""
fullnameOverride: ""

gemini:
  model: gemini-decision-1
  baseUrl: https://generativelanguage.googleapis.com
  imageResolution: ""          # low|medium|high|ultra_high

# Secret handling. Pick exactly ONE source:
#   1. externalSecret.enabled  (primary path: External Secrets Operator creates the Secret)
#   2. existingSecret          (a Secret created some other way)
#   3. secret.create           (dev/CI only: plain values in the chart)
# Whichever source is used, the pod reads these keys from the resulting Secret:
secretKeys:
  geminiApiKey: GEMINI_API_KEY
  inboundApiKeys: INBOUND_API_KEYS   # optional (secretKeyRef optional: true)

externalSecret:
  enabled: false
  apiVersion: external-secrets.io/v1   # ESO >= 0.17 serves only v1; override only for old clusters
  refreshInterval: 1h
  secretStoreRef:
    kind: ClusterSecretStore           # or SecretStore
    name: ""                           # required when enabled
  target:
    name: ""                           # defaults to the chart fullname
    creationPolicy: Owner
    deletionPolicy: Retain
  # Minimal path: fill in the remote key (and property, for JSON-valued secrets).
  geminiApiKey:
    remoteRef:
      key: ""                          # required when enabled
      property: ""
  inboundApiKeys:                      # optional; entry omitted when key is empty
    remoteRef:
      key: ""
      property: ""
  # Escape hatches for anything the shorthand can't express. Rendered verbatim, appended after the shorthand entries.
  extraData: []                        # list of ExternalSecret spec.data entries
  dataFrom: []                         # list of ExternalSecret spec.dataFrom entries
  labels: {}
  annotations: {}

existingSecret: ""                     # name of an existing Secret holding secretKeys
secret:
  create: false
  geminiApiKey: ""
  inboundApiKeys: ""

config:
  upstreamTimeoutSeconds: 30
  upstreamMaxRetries: 2
  maxBodyBytes: 20971520
  advertisedModels: "jev-latest,gemini-decision-1"
  logLevel: INFO
  logFormat: json

otel:
  metricsExporter: prometheus  # otlp|prometheus|otlp,prometheus|none
  otlpEndpoint: ""
  otlpProtocol: http/protobuf

service:
  type: ClusterIP
  port: 8080
  metricsPort: 9464

serviceMonitor:
  enabled: false
  interval: 30s
  labels: {}

networkPolicy:
  enabled: false
  ingressFrom: []              # list of NetworkPolicyPeer for port 8080
  metricsFrom: []              # list of NetworkPolicyPeer for port 9464
  # egress is always DNS (53/UDP+TCP) and TCP 443 when enabled

ingress:
  enabled: false
  className: ""
  annotations: {}
  hosts: []
  tls: []

resources:
  requests: { cpu: 50m, memory: 96Mi }
  limits:   { cpu: 500m, memory: 256Mi }
podSecurityContext:
  runAsNonRoot: true
  runAsUser: 10001
  runAsGroup: 10001
  fsGroup: 10001
  seccompProfile: { type: RuntimeDefault }
securityContext:
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
  capabilities: { drop: [ALL] }
podAnnotations: {}
podLabels: {}
nodeSelector: {}
tolerations: []
affinity: {}
extraEnv: []
serviceAccount:
  create: true
  name: ""
  automountServiceAccountToken: false
```

**Template requirements:**
- **Secret source:** a helper `s1gw.secretName` resolves the Secret name: `externalSecret.target.name` (or fullname), else `existingSecret`, else fullname (for `secret.create`). Rendering fails with a clear `fail` message if **zero or more than one** of the three sources is configured.
- `externalsecret.yaml` (when enabled):
  - Uses `apiVersion: {{ .Values.externalSecret.apiVersion }}`, `kind: ExternalSecret`, with the standard labels plus `externalSecret.labels` and `annotations`.
  - `spec.refreshInterval`, `spec.secretStoreRef`, and `spec.target` (`name`, `creationPolicy`, `deletionPolicy`) come from values.
  - `spec.data`: one entry with `secretKey: {{ secretKeys.geminiApiKey }}` and its `remoteRef`. A second entry for `inboundApiKeys` only when its `remoteRef.key` is set. `property` is rendered only when non-empty. `extraData` is appended verbatim.
  - `spec.dataFrom` is rendered only when non-empty.
  - Fails with clear messages when `secretStoreRef.name` or `geminiApiKey.remoteRef.key` is empty (unless `dataFrom` is set, which may supply the key).
  - Do **not** gate on `.Capabilities` for the ESO CRD. Silently skipping the resource would leave pods without a Secret; an install error naming the missing CRD is the better failure.
- Document in the README:
  - Pods sit in `CreateContainerConfigError` until ESO's first sync, then start on their own.
  - ESO refreshes the Secret's contents but does **not** restart pods. Users who rotate keys can set `podAnnotations` for Reloader or a similar tool.
  - The chart doesn't need ESO for the other two secret modes.
- `deployment.yaml`:
  - Env comes from the ConfigMap plus `secretKeyRef`s. Inject `POD_NAME` and `POD_NAMESPACE` through the downward API.
  - Liveness probe `GET /healthz`, readiness probe `GET /readyz`.
  - Mount an `emptyDir` at `/tmp`. `terminationGracePeriodSeconds: 20`.
  - Pod annotations: `checksum/config` (and `checksum/secret` when `secret.create`) so config changes roll the pods, plus `prometheus.io/scrape|port|path`.
  - Image reference is `repo@digest` when `digest` is set, otherwise `repo:tag`, otherwise `repo:appVersion`.
- `servicemonitor.yaml` renders only when it is enabled **and** `.Capabilities.APIVersions.Has "monitoring.coreos.com/v1"`.
- Standard labels come from `_helpers.tpl` (`app.kubernetes.io/name|instance|version|managed-by`, `helm.sh/chart`).
- `NOTES.txt` gives a port-forward command and a sample `curl`.
- `templates/tests/test-connection.yaml` is a `helm.sh/hook: test` Pod that runs `curl`/`wget` against `/healthz` and `/v1/models`.

`/readyz` returns 200 once settings are loaded and the HTTP client exists. It must **not** call Gemini, because readiness probes would burn quota.

---

## 10. GitHub Actions pipeline

General rules for all workflows:
- Pin every third-party action to a full commit SHA with a `# vX.Y.Z` comment. Dependabot keeps them updated.
- Set least-privilege `permissions:` per workflow (default `contents: read`). Add `packages: write` only in the publishing jobs, and `contents: write` only for the release job.
- `concurrency:` cancels superseded runs on the same ref for PRs.
- Use the registry `ghcr.io/${{ github.repository_owner }}/systemone-gemini-gateway`, kept in one `env: IMAGE` value so it is easy to change. Authenticate with `GITHUB_TOKEN`. **No long-lived registry credentials.**
- Python tooling: `astral-sh/setup-uv` with caching. Docker: `docker/setup-qemu-action`, `docker/setup-buildx-action`, `docker/metadata-action`, `docker/build-push-action` with `cache-from/to: type=gha`.
- No job may need `GEMINI_API_KEY` except `live.yml`.

### 10.1 `ci.yml`: on `pull_request` and `push` to `main`

| Job | Steps |
|---|---|
| `lint` | `uv sync --frozen`, `ruff check`, `ruff format --check`, `mypy --strict src/` |
| `test` | `pytest -m "not live"` with coverage gates (§11). Upload `coverage.xml` and a JUnit report as artifacts. Publish a coverage summary to `$GITHUB_STEP_SUMMARY`. |
| `chart` | `azure/setup-helm`. `helm lint --strict` for each file in `ci/*.yaml`. Install the `helm-unittest` plugin (pinned version) and run `helm unittest charts/systemone-gemini-gateway`. Run `helm template` with each `ci/*.yaml` and pipe to `kubeconform -strict -summary` (with the datreeio CRDs-catalog schema location so `ServiceMonitor` and `ExternalSecret` (`external-secrets.io/v1`) are validated, not skipped). Check that `Chart.yaml` `version` was bumped whenever files under `charts/` changed against the base branch (PRs only). |
| `image` | needs `lint`, `test`. Build `linux/amd64` with `load: true`. Run `scripts/image_smoke.sh`. Run Trivy (`aquasecurity/trivy-action`): fail on CRITICAL vulnerabilities that have a fix, and write SARIF to code scanning. Then build both platforms without loading, to prove the arm64 build works. On **push to `main` only**, push both platforms tagged `main` and `sha-<short>`. On PRs, push nothing. |
| `chart-install` | needs `image`, `chart`. Create a throwaway `kind` cluster (`helm/kind-action`). Load the amd64 image built in this run into kind with `kind load docker-image`. Matrix over two secret modes. **`secret-create`**: `helm install --wait` with `secret.create=true` and a dummy key. **`external-secret`**: install the External Secrets Operator chart (pinned version) into kind, create a `ClusterSecretStore` that uses ESO's built-in `fake` provider with a dummy `GEMINI_API_KEY` entry, then `helm install --wait` with `externalSecret.enabled=true` pointing at it. Assert that the ExternalSecret reaches `Ready=True` and that the pod gets the key. Both legs finish with `helm test`. This only proves the chart, image, and ESO wiring work together. It is **not** a deployment. |

### 10.2 `release.yml`: on `push` of tags `v*.*.*`

1. Fail if the tag (without the `v`) does not equal `Chart.yaml` `appVersion` **and** `pyproject.toml` `version`.
2. Run the same lint, test, and chart gates (reuse them with a `workflow_call` workflow so they aren't duplicated).
3. Build and push both platforms with tags `X.Y.Z`, `X.Y`, `X`, and `latest`. Enable `provenance: mode=max` and `sbom: true`.
4. Sign the image by digest with keyless `cosign` (`id-token: write`).
5. Run `helm package charts/systemone-gemini-gateway` and attach the `.tgz` to a GitHub Release along with generated release notes and the image digest. **Do not push the chart to any chart registry.** The image is the published deliverable.

### 10.3 `live.yml`: `workflow_dispatch` only

Runs `pytest -m live` with `RUN_LIVE=1` and `GEMINI_API_KEY` from repository secrets. Uploads any recorded fixtures as an artifact. It never runs on PRs, which also keeps the secret away from fork PRs.

### 10.4 `dependabot.yml`

Weekly updates for `github-actions`, `pip` (the uv lockfile), and `docker` (base image digest). Group minor and patch updates.

### 10.5 Local parity

Each CI step has a Makefile target so a developer can run the same thing locally: `lint`, `typecheck`, `test`, `test-live`, `image`, `image-smoke`, `chart-lint`, `chart-test` (unittest plus kubeconform). The workflows call these targets where practical, so CI and local runs stay in sync.

---

## 11. Tests (required)

Use `pytest` with `pytest-asyncio`. Mock the upstream with `respx` against `GEMINI_BASE_URL`. Use `httpx.AsyncClient(transport=ASGITransport(app))` for API tests. Default runs need no network. **Coverage gate: ≥90% overall and 100% of `translate.py` lines.** Enforce it with `--cov-fail-under`.

### Unit: request translation
1. String state passes through unchanged. Empty string → 400.
2. Object and array state → compact JSON. Order is preserved and non-ASCII is kept (`ensure_ascii=False`).
3. Question ids remap to `q0..qN` in insertion order. Cover ids containing spaces, `/`, emoji, and a 200-char string. Reverse mapping is exact.
4. noul: `true`/`false` → `*_description`. A `null` side is omitted. With no criteria, the `criteria` key is absent.
5. choice: `null` → `""`. Object description → JSON string. One option → 400. A 129-char option key → 400.
6. score: 3 levels pass through. One level → 400. 11 levels → 400. A `null` or `""` level → 400. Object level → JSON string.
7. Non-string instructions are serialized.
8. `box` → 400 with the documented message.
9. 1000 questions pass. 1001 → 400. 0 questions → 422.
10. Unknown fields are ignored. Danger-Zone headers are accepted.
11. Golden fixtures: every `fixtures/systemone/X.request.json` translates exactly to `fixtures/gemini/X.request.json`. The §3.4 example is fixture #1.

### Unit: assets
12. A ref in state as `` `photo` `` produces a label and an image block, and the state text contains `<image 1>`.
13. A ref only in an instruction still produces an image block, and the instruction text is relabelled.
14. Keys `img` and `img2` both present: longest-first replacement gives the right labels.
15. Unreferenced asset → 400. Bad format (`gif`) → 422 or 400 (pick one and document it). Invalid base64 → 400. `jpg` and `jpeg` both map to `image/jpeg`.
16. Text and image blocks alternate with explicit `\n\n` separators. No two text blocks are adjacent without a separator.
17. Non-empty `images` field → 400.
18. `GEMINI_IMAGE_RESOLUTION` is applied when set and omitted when unset.

### Unit: response translation
19. noul `confidence` is dropped. choice and score pass through unrounded.
20. Integer-keyed `legend` and `probabilities` are normalized to string keys.
21. Answers come back under the original client ids in the original order.
22. Missing `qN` → 502. Type mismatch → 502. Malformed JSON → 502.
23. Usage maps `total_input_tokens` → `input_tokens`, and `output_tokens` is 0.

### Unit: retry policy
24. 429 → 429 → 200 succeeds with 2 retries. `Retry-After: 1` is honoured up to the cap, and the sleep is injected and asserted.
25. 503 ×3 → 529. Timeout ×3 → 504. 400 is never retried. 500 → 502 without retry.

### API (respx-mocked)
26. Happy path end to end with fixture #1. Assert the outbound body, the `x-goog-api-key` header, the **absence** of inbound `Authorization` upstream, the `X-Request-Id` echo, and the response body.
27. Upstream 400 `{error:{code:"safety"}}` → 400 `detail.code == "safety"`. Repeat for each safety code (parametrized).
28. Upstream 401, 403, and 404 → 502, and the response body contains neither the API key nor the upstream body.
29. Multipart → 415. Oversized body → 413, tested both with a `Content-Length` header and with chunked transfer.
30. Auth: with `INBOUND_API_KEYS="a,b"`, a missing token → 401, a wrong token → 401, `b` → 200. With auth disabled, no header → 200.
31. `GET /v1/models` lists `ADVERTISED_MODELS`. `/healthz` and `/readyz` → 200, and `/readyz` makes zero upstream calls (respx asserts it).
32. Startup without `GEMINI_API_KEY` fails with a clear error.

### Telemetry (`InMemoryMetricReader`)
33. A success records `gen_ai.client.operation.duration`, `gen_ai.client.token.usage` (with the value from the fixture), `s1gw.questions` per type, `s1gw.questions_per_request`, and `s1gw.requests{outcome="ok"}`.
34. Safety block → `outcome="blocked"`, `error.type` set. Retries increment `s1gw.upstream.retries{reason}`.
35. **Cardinality guard:** after running every API test scenario, collect all attribute keys and values across all metrics. Assert the keys are a subset of an allowlist, and that no value equals any client question id, inbound model string, asset key, or state substring used in the tests.
36. With `OTEL_METRICS_EXPORTER=prometheus`, the metrics endpoint returns text containing the metric names (Prometheus-normalized: dots become underscores, plus unit suffix).

### Logging
37. Run the happy path and a safety block with `caplog` or a captured stdout handler. Assert that the state text, instruction text, base64 data, inbound token, and `GEMINI_API_KEY` never appear in any log record.

### Live smoke (opt-in, `live.yml`)
38. `@pytest.mark.live`, skipped unless `GEMINI_API_KEY` is set and `RUN_LIVE=1`. Send fixture #1 plus one small PNG asset to the real API through the app. Assert the response shape and that choice probabilities sum to 1 ± 1e-6. With `RECORD_FIXTURES=1`, write the raw upstream response to `tests/fixtures/gemini/live_*.json` so mocked fixtures can be refreshed from real data.

### Image
39. `scripts/image_smoke.sh <image>`: run the container with `--read-only --tmpfs /tmp --user 10001`, a dummy `GEMINI_API_KEY`, and `OTEL_METRICS_EXPORTER=prometheus`. Wait up to 15 s for `/healthz` to return 200. Assert `/readyz` → 200, `/v1/models` → 200 JSON, and `:9464/metrics` contains `s1gw_`. Assert the process runs as UID 10001 (`docker exec id -u`). Always clean up the container. Exit non-zero on any failure.
40. Image size is under 200 MB (checked by the script with `docker image inspect`).

### Helm chart (`helm-unittest` in `charts/systemone-gemini-gateway/tests/`)
41. Default values with `existingSecret: foo` render a Deployment, Service, ServiceAccount, and ConfigMap. No Secret, ExternalSecret, ServiceMonitor, NetworkPolicy, or Ingress is rendered.
42. No secret source → rendering fails. Any two sources set at once (all three pairs, parametrized) → rendering fails. Both cases use the documented message.
43. `secret.create=true` renders a Secret, and the Deployment's `secretKeyRef`s point at it. The `inboundApiKeys` ref is `optional: true`.
43a. ExternalSecret, minimal (`secretStoreRef.name` and `geminiApiKey.remoteRef.key` only): renders `external-secrets.io/v1`, `kind: ClusterSecretStore`, `target.name` = fullname, exactly one `data` entry with `secretKey: GEMINI_API_KEY`, no `property` field, no `dataFrom`. The Deployment's `secretKeyRef` name equals `target.name`. No plain `Secret` is rendered.
43b. ExternalSecret, full: `property`, the `inboundApiKeys` entry, custom `target.name`, `SecretStore` kind, `extraData` appended after the shorthand entries, `dataFrom` rendered, custom `apiVersion` honoured, labels and annotations merged.
43c. ExternalSecret, validation: empty `secretStoreRef.name` → fail. Empty `geminiApiKey.remoteRef.key` with no `dataFrom` → fail. Empty key **with** `dataFrom` → renders with no shorthand `data` entry.
43d. Custom `secretKeys.geminiApiKey` name flows to both the ExternalSecret `secretKey` and the Deployment `secretKeyRef.key`.
44. Image reference: empty tag → `appVersion`; explicit tag; digest takes precedence.
45. The container security context has `readOnlyRootFilesystem`, `runAsNonRoot`, `drop: [ALL]`, and seccomp `RuntimeDefault`. A `/tmp` emptyDir is mounted.
46. Both probe paths and both ports are correct. The `checksum/config` annotation changes when `config.logLevel` changes.
47. ServiceMonitor renders only with `enabled=true` **and** the API version present (use `capabilities.apiVersions` in the test). It targets port `metrics`.
48. NetworkPolicy, when enabled, allows egress to 53 and 443 only, and ingress on 8080 and 9464 from the configured peers.
49. Ingress renders the hosts and TLS from values.
50. `values.schema.json` rejects `otel.metricsExporter: foo` and a non-integer `replicaCount`.
51. `kubeconform -strict` passes on `helm template` output for every `ci/*.yaml`. The ExternalSecret is schema-validated, not skipped.
52. `chart-install` job (CI only): both matrix legs (`secret-create`, `external-secret`) pass `helm install --wait` and `helm test` on kind.

### Pipeline self-checks
53. `actionlint` runs on `.github/workflows/*.yml` in the `lint` job.
54. The release version-consistency check (§10.2 step 1) is a small script under `scripts/` with its own pytest cases: matching versions pass; a mismatch in any of the three sources fails.

---

## 12. Acceptance criteria

- `make lint typecheck test chart-lint chart-test image-smoke` passes from a clean clone. Ruff is clean and `mypy --strict` is clean on `src/`.
- Coverage gates in §11 are met.
- On a PR, `ci.yml` is green and pushes nothing. On `main`, it pushes `:main` and `:sha-<short>` for amd64 and arm64.
- Pushing tag `v0.1.0` produces a signed multi-arch image with SBOM and provenance at `X.Y.Z`/`X.Y`/`X`/`latest`, plus a GitHub Release with the packaged chart attached.
- Image size under 200 MB. Idle RSS under 120 MB.
- Gateway overhead is under 10 ms p50 for a 10-question text request against a mocked upstream that responds instantly (`scripts/bench.py`; not run in CI).
- The README documents: the mapping table (§3), unsupported features (§1), every config var, metric names, the chart's values table (or a pointer to `helm-docs` output), the pipeline and its tags, how to cut a release, and an example `curl` against `docker run`. It also has a short **"Deploying (Part 2)"** section that only lists what Part 2 must supply: a `ClusterSecretStore`/`SecretStore` and the remote key holding the Gemini API key (with a sample `externalSecret:` values block), a values file, and optionally a ServiceMonitor or OTLP endpoint. It contains no cluster-specific steps.
- `docs/UPSTREAM_NOTES.md` records the Phase 0 findings and any differences from this brief.

## 13. Ground rules

- Don't add features outside §1. If something seems missing, write it under "Future work" in the README instead of building it.
- Don't write anything that targets a real cluster: no kubeconfigs, no deploy jobs, no GitOps manifests, no cluster-specific values.
- If the live Gemini contract contradicts this brief, follow the live API and record the difference. Don't silently guess.
- Commit in logical steps: scaffold, translator and tests, upstream and retries, API, telemetry, Dockerfile and image smoke, Helm chart and chart tests, workflows. Each commit must pass the tests that exist at that point.
