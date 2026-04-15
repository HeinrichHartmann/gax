.PHONY: test test-e2e install lint fmt hooks readme man

test:
	pytest tests/ -v -m "not e2e"

test-e2e:
	pytest tests/test_e2e.py -v

install:
	uv tool install --reinstall --editable .

lint:
	uv run ruff check gax/ tests/

fmt:
	uv run ruff format gax/ tests/
	uv run ruff check --fix gax/ tests/

hooks:
	uv run pre-commit install

man: man/gax.1

man/gax.1: gax/*.py
	@mkdir -p man
	@uv run gax man --md | pandoc -s -t man -o man/gax.1
	@echo "Generated man/gax.1"

readme: README.md

README.md: gax/*.py
	@echo "Updating README.md with gax man output..."
	@{ \
		sed -n '1,/<!-- BEGIN GAX MAN -->/p' README.md; \
		echo '```'; \
		uv run gax man; \
		echo '```'; \
		sed -n '/<!-- END GAX MAN -->/,$$p' README.md; \
	} > README.md.tmp && mv README.md.tmp README.md
