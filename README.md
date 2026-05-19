# Playground

A local-first lab platform for defining, operating, and inspecting
infrastructure experiments. The operator describes lab intent in a YAML
config tree, validates and resolves it through a Python control layer, and
eventually applies it through visible OpenTofu, Ansible, and Docker
modules. Today the platform provisions KVM/libvirt VMs that run
containerized Android (Redroid) for nested-virt experiments; the design
leaves room for cloud backends, traffic capture, and security workflows.

## Two layers

The repo deliberately keeps two layers side by side:

- **Runtime baseline** under `tofu/` and `ansible/` — works today via a
  manual two-step pipeline. Provisions libvirt VMs and installs Docker +
  Redroid.
- **Python control layer** under `src/playground/` — under active
  development. Read-only today (`playground validate`, `playground lab
  list`, `playground lab show`). Future slices will bridge to the runtime
  baseline.

The split is intentional and documented in
[`docs/architecture_decisions.md`](docs/architecture_decisions.md). The
Python layer must not hide or rewrite OpenTofu/Ansible prematurely; both
stay visible and editable.

## Quick start — Python control layer

Validate the committed example config and inspect a resolved lab:

```bash
# Tests + lint + type-check
PYTHONPATH=src uv run --no-project \
  --with pytest --with pydantic --with ruamel.yaml --with jsonschema --with typer \
  pytest tests -q

uv run --no-project --with ruff ruff check src tests
uv run --no-project \
  --with mypy --with pydantic --with ruamel.yaml --with jsonschema --with typer \
  mypy src

# CLI
PYTHONPATH=src uv run --no-project \
  --with pydantic --with ruamel.yaml --with jsonschema --with typer \
  python -m playground.cli.main validate
PYTHONPATH=src uv run --no-project \
  --with pydantic --with ruamel.yaml --with jsonschema --with typer \
  python -m playground.cli.main lab list
PYTHONPATH=src uv run --no-project \
  --with pydantic --with ruamel.yaml --with jsonschema --with typer \
  python -m playground.cli.main lab show generic-infra
```

Or install once with `pip install -e ".[dev]"` and run `playground …`,
`pytest`, `mypy src`, `ruff check src tests` directly.

## Quick start — runtime baseline (manual pipeline)

Requires Ubuntu host with KVM/libvirt, OpenTofu, Ansible, and `~/.ssh/id_rsa.pub`.

```bash
# 1. Provision VMs
cd tofu && tofu init && tofu apply -auto-approve

# 2. Inject IPs from `tofu output vm_ips` into ansible/inventory.ini, then:
cd ../ansible
ansible-galaxy collection install -r requirements.yml
ansible-playbook -i inventory.ini site.yml

# 3. Connect to Android
adb connect <VM_IP>:5555

# Teardown
cd ../tofu && tofu destroy -auto-approve
```

`var.vm_count`, `var.vm_memory`, `var.vm_vcpu`, and `var.ssh_public_key_path`
let you tune the deployment; override via `-var` or `terraform.tfvars`.
Don't hardcode secrets.

## Repository layout

```text
src/playground/    Python control layer
config/            User-authored lab intent (YAML, committed)
tofu/              OpenTofu module (libvirt provider)
ansible/           Ansible site.yml + roles (docker, redroid)
tests/             Pytest suites (unit + CLI)
docs/              Product, architecture, config, roadmap docs
.playground/       Generated runtime state (git-ignored, not yet populated)
```

## Where to read next

| You want to… | Read |
|---|---|
| Get a visual map of the system | [`docs/system_overview.md`](docs/system_overview.md) |
| Dive into the code and contribute | [`docs/developer_guide.md`](docs/developer_guide.md) |
| Understand product intent | [`docs/product/requirements.md`](docs/product/requirements.md) |
| See what's planned and in flight | [`docs/roadmap.md`](docs/roadmap.md) |
| Understand the full intended architecture | [`docs/system_design.md`](docs/system_design.md) |
| Understand the YAML config tree | [`docs/config_design.md`](docs/config_design.md) |
| Know the non-negotiable design constraints | [`docs/architecture_decisions.md`](docs/architecture_decisions.md), [`docs/engineering_principles.md`](docs/engineering_principles.md), [`PRD.md`](PRD.md) |
| Understand the agent workflow | [`docs/workflow.md`](docs/workflow.md), [`AGENTS.md`](AGENTS.md), [`CLAUDE.md`](CLAUDE.md), [`CODEX.md`](CODEX.md) |

## Status

All nine roadmap phases have shipped a first slice:

- §1 Baseline Cleanup — done
- §2 Read-Only CLI — done
- §3 Validation Hardening — done
- §4 OpenTofu/Ansible Bridge — done (tfvars + inventory + name-keyed pairing + per-role groups)
- §5 Plan Rendering — done (state-observation slice queued)
- §6 Apply / Status / Destroy — done
- §7 Operation Runs + Events — done (live `log_line` streaming + JSONL log)
- §8 Docker Workloads — done (container + compose + swarm)
- §9 TUI — done (read-only + mutating actions + runs viewer)

See [`docs/roadmap.md`](docs/roadmap.md) for the slice-by-slice ledger and
the queued follow-ups (lab-network → docker-network mapping, retention
enforcement, runtime overrides, lab-scoped DNS, etc.).

## End-to-end operator workflow

```bash
playground doctor                                # probe host prereqs (run this first)
playground validate                              # cross-reference check
playground plan generic-infra                    # preview what apply would do
playground apply generic-infra                   # tofu + ansible + record run
playground status generic-infra                  # what's provisioned now
playground runs list                             # browse past operations
playground runs show <run-id>                    # inspect events.jsonl timeline
playground destroy generic-infra                 # tear down via tofu
playground reset generic-infra                   # scrub-by-name when destroy fails
playground tui                                   # Textual UI over everything above
```

## License

MIT. See `pyproject.toml`.
