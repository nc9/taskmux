.DEFAULT_GOAL := dev
BUMP ?= patch

.PHONY: dev test lint fmt check link unlink bump publish release clean

dev:
	uv sync

test:
	uv run pytest -v

lint:
	uv run ruff check src/taskmux/ tests/
	uv run basedpyright src/taskmux/

fmt:
	uv run ruff format src/taskmux/ tests/
	uv run ruff check --fix src/taskmux/ tests/

check: fmt lint test

link:
	ln -sf $(shell uv run which taskmux) ~/.local/bin/taskmux

unlink:
	rm -f ~/.local/bin/taskmux

bump:
	uv version --bump $(BUMP)

publish:
	uv build
	uv publish

release: check
	@if [ -n "$$(git status --porcelain)" ]; then echo "dirty tree"; exit 1; fi
	uv version --bump $(BUMP)
	git add pyproject.toml
	git commit -m "chore(release): v$$(uv version)"
	git tag "v$$(uv version)"
	git push --follow-tags
	uv build
	uv publish

clean:
	rm -rf dist/ build/ *.egg-info/ .pytest_cache/
