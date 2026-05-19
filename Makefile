# Top-level helpers. The playground itself is driven by `playground`
# (the CLI installed from `pyproject.toml`); this Makefile only carries
# operator workflows that span repos.

BARAK_REPO ?= $(HOME)/Workspace/barak-deploy

.PHONY: sync-from-barak-deploy
sync-from-barak-deploy:  ## Copy barak-deploy artifacts into ansible/files/ for the cross-VM lab.
	mkdir -p ansible/files/cross-vm/pipelines
	cp $(BARAK_REPO)/dist/barak_deploy-1.0.0-py3-none-any.whl ansible/files/
	cp $(BARAK_REPO)/packaging/barak-deploy.service           ansible/files/
	cp $(BARAK_REPO)/packaging/barak-deploy.env.example       ansible/files/
	cp $(BARAK_REPO)/examples/cross-vm/bundles.yaml           ansible/files/cross-vm/
	cp $(BARAK_REPO)/examples/cross-vm/triggers.yaml          ansible/files/cross-vm/
	cp $(BARAK_REPO)/examples/cross-vm/identity.yaml          ansible/files/cross-vm/
	cp $(BARAK_REPO)/examples/cross-vm/pipelines/deploy-demo.yaml \
	   ansible/files/cross-vm/pipelines/

.PHONY: test
test:  ## Run the full unit + CLI test suite.
	PYTHONPATH=src uv run --no-project \
	  --with pytest --with pytest-asyncio --with pydantic \
	  --with ruamel.yaml --with jsonschema --with typer --with textual \
	  pytest tests

.PHONY: help
help:
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  %-30s %s\n", $$1, $$2}' $(MAKEFILE_LIST)
