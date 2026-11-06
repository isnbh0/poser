.PHONY: check test-real check-plugins

check:
	uv run ruff format --check .
	uv run ruff check .
	uv run ty check
	uv run pytest

test-real:
	uv run pytest -m real_plugins

check-plugins:
	uv run check-plugins
