.PHONY: setup hooks quality lint test audit browser package
setup:
	uv sync --locked
	npm ci
	uv run --locked python scripts/install-gitleaks.py
	npx playwright install chromium
hooks:
	git config core.hooksPath .githooks
quality:
	uv run --locked python scripts/quality.py all
lint:
	uv run --locked python scripts/quality.py lint
	uv run --locked python scripts/quality.py types
	uv run --locked python scripts/quality.py rules
test:
	uv run --locked python scripts/quality.py test
audit:
	uv run --locked python scripts/quality.py audit
browser:
	npm test
package:
	uv run --locked python scripts/quality.py package
