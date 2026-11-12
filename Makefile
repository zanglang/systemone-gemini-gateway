# Local parity with CI (.github/workflows). Each target maps to a CI step.
CHART      := charts/systemone-gemini-gateway
IMAGE_NAME := systemone-gemini-gateway
KUBECONFORM_SCHEMAS := -schema-location default \
	-schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'

.DEFAULT_GOAL := help
.PHONY: help all lint typecheck test test-live image image-load image-smoke \
        chart-lint chart-test actionlint

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-14s %s\n", $$1, $$2}'

all: lint typecheck test chart-lint chart-test actionlint image-smoke ## Everything CI runs

lint: ## ruff check + ruff format --check
	uv run ruff check .
	uv run ruff format --check .

typecheck: ## mypy --strict src/
	uv run mypy --strict src/

test: ## pytest, excluding live tests (coverage gates apply)
	uv run pytest -m "not live"

test-live: ## Live Gemini smoke test (needs GEMINI_API_KEY)
	RUN_LIVE=1 uv run pytest -m live

image: ## Multi-arch image build (amd64+arm64), no push
	docker buildx build --platform linux/amd64,linux/arm64 -t $(IMAGE_NAME):dev .

image-load: ## Build amd64 image and load it as systemone-gemini-gateway:dev
	docker buildx build --platform linux/amd64 --load -t $(IMAGE_NAME):dev .

image-smoke: image-load ## Run scripts/image_smoke.sh against the dev image
	scripts/image_smoke.sh $(IMAGE_NAME):dev

chart-lint: ## helm lint --strict for every ci/*.yaml values file
	@set -e; for f in $(CHART)/ci/*-values.yaml; do \
		echo "==> helm lint $$f"; helm lint --strict $(CHART) -f "$$f"; \
	done

chart-test: ## helm unittest + kubeconform on every ci/*.yaml values file
	helm unittest $(CHART)
	@set -e; for f in $(CHART)/ci/*-values.yaml; do \
		echo "==> kubeconform $$f"; \
		helm template gw $(CHART) -f "$$f" --api-versions monitoring.coreos.com/v1 | kubeconform $(KUBECONFORM_SCHEMAS) -strict -summary -; \
	done

actionlint: ## Lint GitHub Actions workflows
	actionlint
