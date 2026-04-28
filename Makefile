.DEFAULT_GOAL := dev
BUMP ?= patch

.PHONY: dev test lint fmt check link unlink bump bump-skill publish release clean

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

# Sync skills/taskmux/SKILL.md frontmatter version to whatever pyproject says.
# Idempotent — diff-clean when the version field is already correct. Errors
# loudly if the skill file lacks a `version:` line so a typo doesn't silently
# pin it forever.
bump-skill:
	@set -e; \
	VERSION=$$(uv version --short); \
	SKILL=skills/taskmux/SKILL.md; \
	if ! grep -q '^version:' $$SKILL; then \
		echo "ERROR: $$SKILL has no 'version:' frontmatter line" >&2; exit 1; \
	fi; \
	uv run python -c "import re,pathlib; p=pathlib.Path('$$SKILL'); t=p.read_text(); n=re.sub(r'^version:.*$$', 'version: $$VERSION', t, count=1, flags=re.M); p.write_text(n)"; \
	echo "Skill version → $$VERSION"

publish:
	rm -rf dist/
	uv build
	uv publish

release: check
	@if [ -n "$$(git status --porcelain)" ]; then echo "ERROR: dirty working tree" && exit 1; fi
	@set -e; \
	uv version --bump $(BUMP); \
	$(MAKE) --no-print-directory bump-skill; \
	VERSION=$$(uv version --short); \
	git add pyproject.toml uv.lock skills/taskmux/SKILL.md; \
	git commit -m "chore(release): v$$VERSION"; \
	git tag "v$$VERSION"; \
	git push && git push --tags
	$(MAKE) publish

clean:
	rm -rf dist/ build/ *.egg-info/ .pytest_cache/
