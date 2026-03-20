.PHONY: test install lint fmt

test:
	uv run pytest tests/ -v

install:
	uv tool install --reinstall --editable .

lint:
	uv run ruff check gax/ tests/

fmt:
	uv run ruff format gax/ tests/
	uv run ruff check --fix gax/ tests/
