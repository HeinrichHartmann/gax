.PHONY: test test-e2e install lint fmt

test:
	uv run pytest tests/ -v -m "not e2e"

test-e2e:
	uv run pytest tests/test_e2e.py -v

install:
	uv tool install --reinstall --editable .

lint:
	uv run ruff check gax/ tests/

fmt:
	uv run ruff format gax/ tests/
	uv run ruff check --fix gax/ tests/
