.PHONY: check test-real check-instruments

check:
	uv run ruff format --check .
	uv run ruff check .
	uv run ty check
	uv run pytest

test-real:
	uv run pytest -m "real_plugins or real_scsynth"

check-instruments:
	uv run check-instruments
