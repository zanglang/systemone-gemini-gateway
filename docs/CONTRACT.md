# Shared contract between workstreams

The full spec is `docs/BRIEF.md`. This file pins the names and interfaces that the three workstreams (app, chart, build/CI) depend on. If you need to change anything here, say so in your final report. Don't silently diverge.

## Names
- GitHub repo: `zanglang/systemone-gemini-gateway`
- Image: `ghcr.io/zanglang/systemone-gemini-gateway`
- Helm chart: `charts/systemone-gemini-gateway`, chart `version: 0.1.0`, `appVersion: "0.1.0"`
- Python distribution name: `systemone-gemini-gateway`, version `0.1.0`. Import package: `s1gw` under `src/s1gw/` (src layout).
- Metric prefix: `s1gw.`

## Python project
- `pyproject.toml` at repo root, build backend `hatchling`, `requires-python = ">=3.12"`, managed with `uv` (`uv.lock` committed).
- Dev tools are in `[dependency-groups] dev` (uv default group): pytest, pytest-asyncio, pytest-cov, respx, ruff, mypy.
- pytest markers: `live`. Default `addopts` excludes nothing; CI runs `uv run pytest -m "not live"`.
- Coverage gates are in pytest/coverage config (`--cov=s1gw --cov-fail-under=90`), with a separate 100% check for `translate.py` (`scripts/` or a coverage config `[tool.coverage.report]`; app workstream owns this).
- Lint: `uv run ruff check .`, `uv run ruff format --check .`. Types: `uv run mypy --strict src/`.

## Runtime interface (the image and chart rely on these)
- ASGI entrypoint: `s1gw.main:app`. Run with `uvicorn s1gw.main:app --host 0.0.0.0 --port 8080 --workers 1 --no-access-log`.
- Importing `s1gw.main` must NOT fail when env vars are missing. Settings are validated in the app lifespan, so a missing `GEMINI_API_KEY` makes startup fail with a clear error.
- HTTP port `8080` (env `PORT`): `POST /v1/systemone`, `GET /v1/models`, `GET /healthz`, `GET /readyz`.
- Metrics port `9464` (env `METRICS_PORT`): Prometheus text at `GET /metrics` when `OTEL_METRICS_EXPORTER` contains `prometheus` (the default).
- After one request has been served, metric families named `s1gw_*` appear in `/metrics`. At idle, at least the process/target info appears. The image smoke test checks `/metrics` returns 200 and contains `target_info` or `s1gw_`.
- Env vars: exactly as in BRIEF §5 (`GEMINI_API_KEY`, `GEMINI_BASE_URL`, `GEMINI_MODEL`, `GEMINI_IMAGE_RESOLUTION`, `INBOUND_API_KEYS`, `UPSTREAM_TIMEOUT_SECONDS`, `UPSTREAM_MAX_RETRIES`, `MAX_BODY_BYTES`, `ADVERTISED_MODELS`, `LOG_LEVEL`, `LOG_FORMAT`, `OTEL_SERVICE_NAME`, `OTEL_METRICS_EXPORTER`, `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_PROTOCOL`, `METRICS_PORT`, `PORT`, `POD_NAME`, `POD_NAMESPACE`).
- Runs as UID 10001 with a read-only root filesystem. Only `/tmp` is writable.

## Version consistency (release check)
`scripts/check_versions.py <tag>` verifies tag == `pyproject.toml` `[project].version` == `Chart.yaml` `appVersion`. The build/CI workstream owns it and its tests in `tests/scripts/test_check_versions.py`.

## File ownership (only write inside your own paths)
- App: `src/`, `tests/` (except `tests/scripts/`), `pyproject.toml`, `uv.lock`, `.python-version`, `docs/UPSTREAM_NOTES.md`, `scripts/bench.py`
- Chart: `charts/`
- Build/CI: `Dockerfile`, `.dockerignore`, `.gitignore`, `Makefile`, `.github/`, `scripts/` (except `bench.py`), `tests/scripts/`, `README.md`, `LICENSE` (MIT)

## Environment limits in this workspace
- PyPI works (`uv`). `uv python install 3.12` works.
- **No container registry access** (Docker Hub, ghcr, gcr, ecr are blocked), so `docker build` can't pull base images here. The image is built and verified in GitHub Actions.
- **No helm, kubectl, or kubeconform binaries**, and their download hosts are blocked. Chart rendering is verified in CI. Write templates carefully.
- `actionlint` and `yamllint` are installed (`/usr/local/bin`).
- `git ls-remote https://github.com/<owner>/<repo> refs/tags/<tag>` works for resolving action SHAs.
- No network calls to Gemini. All upstream tests are mocked.
