.PHONY: test install lint

test:
	uv run pytest tests/ -v

install:
	uv tool install --reinstall --editable .

lint:
	uv run python -m py_compile gax/*.py
