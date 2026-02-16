.DEFAULT_GOAL := dev
BUMP ?= patch

.PHONY: dev test lint fmt check link unlink bump publish release clean

dev:
	uv sync

test:
	uv run pytest -v

lint:
	uv run ruff check taskmux/ tests/
	uv run basedpyright taskmux/

fmt:
	uv run ruff format taskmux/ tests/
	uv run ruff check --fix taskmux/ tests/

check: fmt lint test

link:
	ln -sf $(shell uv run which taskmux) ~/.local/bin/taskmux

unlink:
	rm -f ~/.local/bin/taskmux

bump:
	uv version --bump $(BUMP)

publish:
	rm -rf dist/
	uv build
	uv publish

release: check
	@if [ -n "$$(git status --porcelain)" ]; then echo "ERROR: dirty working tree" && exit 1; fi
	uv version --bump $(BUMP)
	$(eval VERSION := $(shell uv version --short))
	git add pyproject.toml uv.lock
	git commit -m "chore(release): v$(VERSION)"
	git tag "v$(VERSION)"
	git push && git push --tags
	$(MAKE) publish

clean:
	rm -rf dist/ build/ *.egg-info/ .pytest_cache/
