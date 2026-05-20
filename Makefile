# Top-level helpers. The playground itself is driven by `playground`
# (the CLI installed from `pyproject.toml`); this Makefile only carries
# operator workflows that span repos.

.PHONY: test
test:  ## Run the full unit + CLI test suite.
	PYTHONPATH=src uv run --no-project \
	  --with pytest --with pytest-asyncio --with pydantic \
	  --with ruamel.yaml --with jsonschema --with typer --with textual \
	  pytest tests

.PHONY: help
help:
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  %-30s %s\n", $$1, $$2}' $(MAKEFILE_LIST)
