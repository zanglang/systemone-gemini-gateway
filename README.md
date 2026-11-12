# systemone-gemini-gateway

A small HTTP service (Python 3.12, FastAPI) that accepts requests in
**TypeSafe's System One API format** (`POST /v1/systemone`) and answers them
with **Google's Gemini Decisions API** (`gemini-decision-1`,
`POST /v1beta/decisions`). Clients keep speaking System One; the gateway
translates the request, calls Gemini, and translates the answers back.

It exposes OpenTelemetry metrics (OTLP push and/or a Prometheus scrape
endpoint), ships as a multi-arch container image (`linux/amd64`,
`linux/arm64`), and includes a Helm chart.

## What it does

```
System One client --> gateway (:8080) --> Gemini Decisions API
                          |
                          +--> metrics (:9464, Prometheus) / OTLP push
```

- `POST /v1/systemone`: translate, call Gemini, translate back. Question types
  `noul`, `choice`, `score`. Image `assets` sent as JSON (base64).
- `GET /v1/models`: the models the gateway advertises (`ADVERTISED_MODELS`), in
  TypeSafe's list format.
- `GET /healthz` (liveness), `GET /readyz` (readiness; never calls Gemini).
- Optional inbound bearer-token auth (`INBOUND_API_KEYS`). The Gemini key lives
  only in the gateway.
- Upstream retries with exponential backoff and full jitter on 429, 503,
  timeouts and connect errors.

Every request is routed to `GEMINI_MODEL`, whatever `model` the client sends.

### Request mapping (System One to Gemini)

| System One | Gemini | Rule |
|---|---|---|
| `model` (any) | `model` = `GEMINI_MODEL` | Inbound value only appears in debug logs. |
| `state` string | `input` string | Unchanged. Empty string gives 400. |
| `state` object/array | `input` string | Compact JSON, key order kept, non-ASCII kept. |
| question keys | `q0`, `q1`, ... | Always remapped in insertion order and mapped back on the response. |
| non-string `instructions` / criteria | string | JSON-serialized. |
| noul `criteria.true` / `.false` | `true_description` / `false_description` | Omitted when null or missing; `criteria` omitted when both are. |
| choice criterion `null` | `""` | Non-strings serialized. At least 2 options; option key at most 128 chars / 512 bytes. |
| score `criteria` levels | same array | 2 to 10 non-empty levels, lowest first. |
| `assets` (base64 images) | image content blocks | Referenced in `state`/instructions/criteria; relabelled `<image N>`; every asset must be referenced. |
| `box` question | n/a | 400 |
| more than 1000 questions | n/a | 400 |
| `TypeSafe-Danger-Zone-*` headers | n/a | Accepted and ignored. |

### Response mapping (Gemini to System One)

| Gemini | System One | Rule |
|---|---|---|
| `answers.qN` | `answers.<client id>` | Original ids and order restored. |
| noul `{noul, confidence}` | `{type:"noul", noul}` | `confidence` dropped. |
| choice | `{type:"choice", choice, confidence, probabilities}` | Probabilities unrounded. |
| score | `{type:"score", score, confidence, legend, probabilities}` | `legend`/`probabilities` keys normalized to strings `"0"`..`"N"`. |
| missing answer or type mismatch | 502 | |
| `usage.total_input_tokens` | `usage.input_tokens` | `output_tokens` is always `0`. |

Every response carries `X-Request-Id` (the inbound header if present, otherwise a
UUID4).

### Error mapping

| Condition | Status |
|---|---|
| Invalid JSON/schema | 422 (FastAPI default) |
| Translation rule violated | 400 `{"detail": "..."}` |
| `multipart/form-data` | 415 |
| Body larger than `MAX_BODY_BYTES` | 413 |
| Missing/wrong bearer token (when enabled) | 401 + `WWW-Authenticate: Bearer` |
| Upstream 400, safety-type code | 400 `{"detail": {"code", "message"}}` |
| Upstream 400, other | 400 `upstream rejected request: ...` |
| Upstream 401/403/404 | 502 `upstream configuration error` (body and key never echoed) |
| Upstream 429 after retries | 429 (`Retry-After` passed through) |
| Upstream 503 after retries | 529 |
| Other upstream 5xx / malformed 200 | 502 |
| Upstream timeout / connect error after retries | 504 |

## Unsupported

- `box` questions (Gemini has no bounding-box type): 400.
- `multipart/form-data` requests: 415. Send assets as JSON.
- Audio, video and PDF input.
- Vertex AI as the upstream (Gemini Developer API only).
- Streaming, batching, caching, traces, an admin UI.
- Kustomize or raw YAML manifests (the Helm chart is the only packaging).

## Quick start

```bash
docker run --rm -p 8080:8080 -p 9464:9464 \
  -e GEMINI_API_KEY="$GEMINI_API_KEY" \
  ghcr.io/zanglang/systemone-gemini-gateway:main
```

```bash
curl -s localhost:8080/v1/systemone \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "jev-latest",
    "state": {"ticket": "Checkout is blank after I click Pay", "tier": "enterprise"},
    "questions": {
      "is it a bug?": {"type": "noul", "instructions": "Is this a bug report?"},
      "team": {"type": "choice", "instructions": "Which team owns it?",
               "criteria": {"payments": "Checkout and billing", "web": null}}
    }
  }'

curl -s localhost:8080/v1/models
curl -s localhost:9464/metrics | head
```

To require a bearer token, add `-e INBOUND_API_KEYS=secret1,secret2` and send
`Authorization: Bearer secret1`.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `GEMINI_API_KEY` | required | Startup fails with a clear error if missing. |
| `GEMINI_BASE_URL` | `https://generativelanguage.googleapis.com` | |
| `GEMINI_MODEL` | `gemini-decision-1` | |
| `GEMINI_IMAGE_RESOLUTION` | unset | `low\|medium\|high\|ultra_high` |
| `INBOUND_API_KEYS` | empty | Comma-separated bearer tokens. Empty disables auth (one WARNING at startup). |
| `UPSTREAM_TIMEOUT_SECONDS` | `30` | |
| `UPSTREAM_MAX_RETRIES` | `2` | |
| `MAX_BODY_BYTES` | `20971520` | 20 MiB |
| `ADVERTISED_MODELS` | `jev-latest,gemini-decision-1` | Returned by `GET /v1/models`. |
| `LOG_LEVEL` | `INFO` | |
| `LOG_FORMAT` | `json` | `json\|text` |
| `OTEL_SERVICE_NAME` | `systemone-gemini-gateway` | |
| `OTEL_METRICS_EXPORTER` | `prometheus` | `otlp`, `prometheus`, `otlp,prometheus`, `none` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | unset | e.g. `http://otel-collector.observability:4318` |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `http/protobuf` | |
| `METRICS_PORT` | `9464` | Prometheus scrape port. |
| `PORT` | `8080` | |
| `POD_NAME`, `POD_NAMESPACE` | unset | Become `k8s.pod.name` / `k8s.namespace.name` resource attributes (the chart sets them via the downward API). |

The gateway logs structured JSON to stdout and never logs state, instructions,
criteria, asset data, inbound tokens or the Gemini key. It runs one uvicorn
worker per pod; scale with replicas.

## Metrics

| Instrument | Kind / unit | Attributes |
|---|---|---|
| `http.server.request.duration` | histogram, s | FastAPI instrumentation; `/healthz`, `/readyz`, `/metrics` excluded |
| `gen_ai.client.operation.duration` | histogram, s | `gen_ai.operation.name`, `gen_ai.provider.name`, `gen_ai.request.model`, `error.type` (on failure) |
| `gen_ai.client.token.usage` | histogram, {token} | `gen_ai.token.type`, `gen_ai.provider.name`, `gen_ai.request.model` |
| `s1gw.requests` | counter | `outcome` (`ok`, `client_error`, `blocked`, `upstream_error`, `timeout`), `http.response.status_code` |
| `s1gw.questions` | counter | `question.type` (`noul`, `choice`, `score`) |
| `s1gw.questions_per_request` | histogram | none |
| `s1gw.images` | counter | none |
| `s1gw.upstream.retries` | counter | `reason` (`429`, `503`, `timeout`, `connect`) |

Attribute values come only from fixed enums or config, never from client input
(question ids, inbound model, state, asset keys, error messages). Prometheus
names are normalized (dots become underscores, unit suffix added), for example
`s1gw_requests_total`.

## Helm chart

`charts/systemone-gemini-gateway` (chart `version` and `appVersion` both start at
`0.1.0`). The chart is linted, unit-tested and install-tested on `kind` in CI. It
is not published to a chart registry; each release attaches the packaged `.tgz`
to the GitHub Release.

Main values (see `values.yaml` and `values.schema.json` for everything):

| Key | Default | Purpose |
|---|---|---|
| `image.repository` / `tag` / `digest` | `ghcr.io/zanglang/systemone-gemini-gateway` / `""` (= appVersion) / `""` | Digest wins over tag. |
| `gemini.model`, `gemini.baseUrl`, `gemini.imageResolution` | `gemini-decision-1`, Google endpoint, `""` | Upstream settings. |
| `externalSecret.*`, `existingSecret`, `secret.*` | see below | Secret source (exactly one). |
| `secretKeys.geminiApiKey` / `inboundApiKeys` | `GEMINI_API_KEY` / `INBOUND_API_KEYS` | Keys the pod reads from the Secret. |
| `config.*` | timeouts, retries, body limit, models, log level/format | Maps to the env vars above. |
| `otel.metricsExporter`, `otel.otlpEndpoint`, `otel.otlpProtocol` | `prometheus`, `""`, `http/protobuf` | Metrics export. |
| `service.port` / `metricsPort` | `8080` / `9464` | |
| `serviceMonitor.enabled` | `false` | Rendered only if the `monitoring.coreos.com/v1` API exists. |
| `networkPolicy.enabled` | `false` | Egress limited to DNS and TCP 443. |
| `ingress.*` | disabled | |
| `resources`, `podSecurityContext`, `securityContext` | small requests; non-root 10001, read-only root fs, drop ALL | `/tmp` is an `emptyDir`. |

### Secret modes

Choose exactly one; rendering fails with a clear message if none or more than
one is configured.

1. **`externalSecret.enabled`** (primary). External Secrets Operator creates the
   Secret from a `SecretStore`/`ClusterSecretStore`:

   ```yaml
   externalSecret:
     enabled: true
     secretStoreRef:
       kind: ClusterSecretStore
       name: my-store
     geminiApiKey:
       remoteRef:
         key: gemini/gateway
         property: api_key
   ```

   `apiVersion` defaults to `external-secrets.io/v1`; `extraData` and `dataFrom`
   cover anything the shorthand cannot express.
2. **`existingSecret: <name>`**: a Secret created some other way, holding the
   keys named in `secretKeys`.
3. **`secret.create: true`**: plain values in the chart. Dev/CI only.

ExternalSecret notes:

- Pods sit in `CreateContainerConfigError` until External Secrets Operator's
  first sync has created the Secret, then start on their own.
- ESO refreshes the Secret's contents but does not restart pods. To roll pods on
  key rotation, set `podAnnotations` for Reloader or a similar tool (for
  example `reloader.stakater.com/auto: "true"`).
- The chart does not need ESO for the other two modes. With ESO missing, the
  ExternalSecret mode fails at install time naming the missing CRD (the chart
  deliberately does not skip it).

## CI/CD

Workflows live in `.github/workflows`. All third-party actions are pinned to
commit SHAs; Dependabot updates actions, uv dependencies and Docker base images
weekly.

| Workflow | Trigger | What it does |
|---|---|---|
| `_checks.yml` | called by `ci.yml` and `release.yml` | `lint` (ruff, ruff format, mypy --strict, actionlint), `test` (pytest, coverage gates, JUnit/coverage artifacts), `chart` (helm lint --strict, helm-unittest, kubeconform, chart version bump check on PRs) |
| `ci.yml` | PRs, push to `main` | the checks above, then `image` (amd64 build, `scripts/image_smoke.sh`, Trivy, multi-arch build) and `chart-install` (kind; `secret-create` and `external-secret` legs, then `helm test`) |
| `release.yml` | tags `v*.*.*` | version check, the same checks, multi-arch push with SBOM and provenance, keyless cosign signature by digest, chart `.tgz` and image digest on a GitHub Release |
| `live.yml` | manual | live Gemini smoke test using the `GEMINI_API_KEY` repository secret |

### Image tags

- Pull requests: nothing is pushed.
- Push to `main`: `ghcr.io/zanglang/systemone-gemini-gateway:main` and `:sha-<short>` (amd64 and arm64).
- Release tag `vX.Y.Z`: `:X.Y.Z`, `:X.Y`, `:X`, `:latest`.

Authentication is `GITHUB_TOKEN` only; there are no long-lived registry
credentials. Only the live workflow ever sees `GEMINI_API_KEY`.

### Cutting a release

1. Set `version` in `pyproject.toml` to the new version `X.Y.Z`.
2. In `charts/systemone-gemini-gateway/Chart.yaml`, set `appVersion` to `X.Y.Z` and
   bump the chart `version`.
3. Commit and merge to `main`.
4. Tag and push: `git tag vX.Y.Z && git push origin vX.Y.Z`.

`release.yml` fails fast if the tag, `pyproject.toml` version and Chart
`appVersion` disagree (`scripts/check_versions.py`).

## Development

```bash
uv sync                  # install runtime + dev dependencies
make help                # list targets
make lint typecheck test # ruff, mypy --strict, pytest (not live)
make test-live           # needs GEMINI_API_KEY; opt-in
make image-smoke         # build amd64 image and run scripts/image_smoke.sh
make chart-lint chart-test   # needs helm, helm-unittest, kubeconform
make actionlint
make all
```

`make image` builds both platforms without pushing. Test coverage gates:
at least 90% overall and 100% of `translate.py`.

## Deploying (Part 2)

This repository only builds the image and chart. A deployment must supply:

- A `ClusterSecretStore` or `SecretStore`, and the remote key holding the Gemini API key.
- A values file, for example:

  ```yaml
  externalSecret:
    enabled: true
    secretStoreRef:
      kind: ClusterSecretStore
      name: <your-store>
    geminiApiKey:
      remoteRef:
        key: <remote-key>
  ```
- Optionally, a ServiceMonitor (`serviceMonitor.enabled`) or an OTLP endpoint (`otel.otlpEndpoint`).

## Future work

- Pin base images by digest (Dependabot maintains the tags today).
- Audio, video and PDF inputs, and multipart asset uploads.
- Vertex AI as an alternative upstream, blocked until Google serves the Decisions API on Vertex. Plan and decisions: [#2](https://github.com/zanglang/systemone-gemini-gateway/issues/2).
- Traces.

## License

MIT. See [LICENSE](LICENSE).
