# Playground requirements for the cross-VM ship-and-deploy test

This document specifies the changes you need to make in `~/Workspace/playground`
to wire the cross-VM ship-and-deploy test described in
[Cookbook Recipe 8](cookbook.md#recipe-8-cross-vm-ship-and-deploy). It is
written against your playground's current layout (KVM/libvirt + OpenTofu +
Ansible, YAML-driven labs under `config/labs/`, Python control layer in
`src/playground/`, Ansible roles in `ansible/roles/`).

Everything listed here lives **outside this repo** — barak-deploy's side is
shipped (see `examples/cross-vm/`, `packaging/`, `docs/install.md`). What
follows is the spec for what the playground needs to grow to host the test.

## Implementation status

The six required additions below have been implemented across the
playground's roadmap §10 sequence (commits `8d3266c` → `cf9e49c`). The
table below summarizes; see `docs/roadmap.md` §10 for the slice-by-slice
ledger and `docs/developer_guide.md` "Multi-VM integration tests" for the
test-runner workflow.

| Spec § | Item | Status |
|---|---|---|
| §1 | `config/labs/barak-deploy-cross-vm.yaml` | ✅ Shipped |
| §1 (implicit) | Lab YAML schema: `vms[*].networks[*].ip` | ✅ Shipped (legacy `list[str]` shape kept for back-compat) |
| §1 (implicit) | Lab YAML schema: `vms[*].extra_hosts` | ✅ Shipped |
| §2 | Two new VM roles | ✅ Shipped — uses the playground's `spec.extends:` (not `inherits:`) and full `apiVersion/kind/metadata/spec` envelope |
| §3a | `docker_tunneler` ansible role | ✅ Shipped |
| §3b | `ssh_keypair_generator` ansible role | ✅ Shipped (platform-generic; promoted from this test's needs) |
| §3c | `ssh_keypair_receiver` ansible role | ✅ Shipped (platform-generic) |
| §3d | `barak_deploy_staging` ansible role | ✅ Shipped |
| §3e | `barak_deploy_agent` ansible role | ✅ Shipped |
| §4 | Inter-VM hostname resolution via `extra_hosts` | ✅ Shipped — new generic `extra_hosts` role + `pg_extra_hosts` host var |
| §5 | Site playbook ordering (central → target) | ✅ Shipped via per-host-class plays in `ansible/site.yml` |
| §6 (option A) | `playground exec --on <vm> <cmd>` CLI helper | ❌ Deferred — tracked in `docs/roadmap.md` backlog |
| §6 (option B) | pytest-style multi-VM integration test | ✅ Shipped — `tests/integration/multi_vm/test_cross_vm_deploy.py`, gated on `PLAYGROUND_LIVE_INFRA=1` |

**Things this spec didn't list that were required to make `ip:`
actually work** — also done:

- Tofu module rewrite for multi-network + DHCP-pinned IPs (`tofu/main.tf`,
  `tofu/variables.tf`). Replaced the hardcoded single `playground_net`
  with `for_each` over `var.networks`; per-VM `network_interface` blocks
  built dynamically with pinned `addresses`.
- `render_tfvars` extension to emit `networks` / `vm_networks` /
  `vm_network_ips` from the resolved lab.
- Two new validator diagnostics: `config.network.ip_not_in_cidr` and
  `config.network.duplicate_ip` (run during `playground validate` /
  `playground apply` pre-flight).
- Root `Makefile` with the `sync-from-barak-deploy` target the spec
  recommended.
- `ansible/files/` directory + `.gitignore` rule for the wheel only.

**Deliberately not done** (with reasons):

- The spec's example `provisioning: ansible_roles: [common, docker, ...]`
  lists a `common` baseline role. **Skipped** — the playground's VmRoles
  list only the roles that actually do work, and `docker` is inherited
  via `extends: docker-host` rather than re-listed. A real baseline-
  hardening role can land as its own feature if a use case appears.
- `playground exec --on <vm> <cmd>` CLI subcommand — the pytest harness
  uses plain `subprocess` + `ssh`, which is enough for the smoke test.
  Promoting it to a CLI command is in the backlog.
- A file-transfer primitive between VMs — the spec explicitly notes
  it isn't needed for this test (the test exercises that path itself
  via `tunneler ship` + scp).

## Test outcome

When the test passes:

- Two VMs (`central` and `target`) are running under libvirt.
- `central` runs `/usr/local/bin/ship-deploy.sh` on demand. The script wraps
  `/opt/staging/` into a single image tar via `docker-tunneler wrap`, ships
  it to `target` via SCP atomic-rename, exits 0.
- `target` runs `barak-deploy` as a systemd service. The filesystem trigger
  picks the tar up within ~5 seconds, runs the `deploy-demo.yaml` pipeline,
  and starts a `hello` container backed by the shipped image with the shipped
  config file mounted in.
- Re-running `ship-deploy.sh` on `central` produces a second pipeline run
  on `target` where every step has `skipped: true` (idempotency).

Concrete verification commands at the bottom of this document.

## Architecture

```
   ┌────────────────────────────────┐                  ┌────────────────────────────────┐
   │  central  (10.20.40.20)        │                  │  target   (10.20.40.21)        │
   │  role: deployment-source       │  tunneler ship   │  role: deployment-target       │
   │                                │ ───────────────▶ │                                │
   │  docker-tunneler installed     │  --transport scp │  docker-tunneler installed     │
   │  /opt/staging/                 │                  │  barak-deploy installed        │
   │    ├─ images/hello.tar         │  (atomic         │  systemd unit running          │
   │    └─ configs/hello.conf       │   .partial       │                                │
   │  /usr/local/bin/ship-deploy.sh │   rename)        │  /var/spool/deploys/  ◀────────│
   │  ~/.ssh/id_ed25519 (private)   │                  │  /etc/barak-deploy/   (configs)│
   │                                │                  │  ~ubuntu/.ssh/authorized_keys  │
   │                                │                  │   contains central's pubkey    │
   └────────────────────────────────┘                  └────────────────────────────────┘
            │                                                       │
            └─────────────  isolated network 10.20.40.0/24  ─────────┘
                            (extra_hosts: central / target)
```

Both VMs sit on a single isolated libvirt network. Static DHCP reservations
pin them to `.20` (central) and `.21` (target). Since the playground does
not yet have lab-scoped DNS, the lab uses `extra_hosts` entries to give each
VM a `central`/`target` hostname for the other.

## Required playground additions

Six pieces, in roughly the order you'd build them:

### 1. Lab definition ✅ Shipped

**Added:** `config/labs/barak-deploy-cross-vm.yaml`. The committed file
follows the playground's actual `apiVersion / kind / metadata / spec`
envelope (the example below uses a slightly simpler shape); see the
committed file for the exact form.

Mirror the shape of `config/labs/generic-infra.yaml`. Concrete content:

```yaml
name: barak-deploy-cross-vm
description: |
  Two-VM lab for the barak-deploy cross-VM ship-and-deploy test.
  VM `central` stages a wrapper bundle and ships it via docker-tunneler.
  VM `target` runs barak-deploy and deploys the bundle on receipt.

spec:
  networks:
    - name: deploy-net
      profile: isolated
      cidr: 10.20.40.0/24

  vms:
    - name: central
      role: deployment-source
      networks:
        - name: deploy-net
          ip: 10.20.40.20
      extra_hosts:
        - "target = 10.20.40.21"

    - name: target
      role: deployment-target
      networks:
        - name: deploy-net
          ip: 10.20.40.21
      extra_hosts:
        - "central = 10.20.40.20"
```

If your lab YAML schema doesn't currently support `ip:` per-network or
`extra_hosts:`, add them — both are minimum requirements for cross-VM tests
without DNS. (Both already appear on your backlog per the playground README's
"Roadmap §10: Lab-scoped DNS"; treat this as the forcing function.)

> **Status:** Both schema additions shipped. `LabVm.networks` accepts the
> legacy `list[str]` shape **and** the new `list[{name, ip?}]` shape via a
> Pydantic before-validator — existing labs need no change.
> `LabVm.extra_hosts: list[str]` carries literal `/etc/hosts` lines.
> See `src/playground/models/kinds.py:LabVmNetwork` and the two new
> validator diagnostics (`config.network.ip_not_in_cidr`,
> `config.network.duplicate_ip`).

### 2. Two new VM roles ✅ Shipped

> **Note on schema:** the playground uses `spec.extends:` (not
> `inherits:`) for role inheritance and the full
> `apiVersion / kind / metadata / spec` envelope at the top level. See
> the committed `config/roles/deployment-source.yaml` and
> `deployment-target.yaml` for the exact form. The legacy `common`
> baseline role from this spec was intentionally **not** added — the
> committed VmRoles list only the roles that do real work; `docker` is
> inherited via `extends: docker-host`.

**Add:** `config/roles/deployment-source.yaml` (VM A's role)

```yaml
name: deployment-source
description: VM that stages and ships barak-deploy wrapper bundles.

inherits: docker-host        # gets docker engine pre-installed

provisioning:
  ansible_roles:
    - common                 # whatever your baseline role is
    - docker                 # docker engine (probably inherited; keep for clarity)
    - docker_tunneler        # NEW — see §3
    - ssh_keypair_generator  # NEW — see §3
    - barak_deploy_staging   # NEW — see §3 (stages /opt/staging + ship script)
```

**Add:** `config/roles/deployment-target.yaml` (VM B's role)

```yaml
name: deployment-target
description: VM that receives shipped bundles and deploys via barak-deploy.

inherits: docker-host

provisioning:
  ansible_roles:
    - common
    - docker
    - docker_tunneler        # NEW — same role as above
    - ssh_keypair_receiver   # NEW — see §3
    - barak_deploy_agent     # NEW — see §3 (installs barak-deploy + systemd unit)
```

### 3. Five new Ansible roles ✅ Shipped

All five roles shipped. The `ssh_keypair_*` pair was promoted to
platform-generic (not barak-deploy-specific) per the spec's
"Playground gaps this test surfaces" recommendation — future multi-host
tests can reuse them as-is. `ssh_keypair_receiver` was generalized
slightly to loop over the `deployment_source` inventory group instead of
hardcoding a single `central` host, so the same role works for any
N-source / M-target topology.

Each lives under `ansible/roles/<name>/` with the usual `tasks/main.yml` +
optional `defaults/`, `templates/`, `files/`.

#### 3a. `docker_tunneler` (used by both VMs) ✅ Shipped

Installs `docker-tunneler` from PyPI (assumes the playground has internet
egress during provisioning; the closed-network rule applies to the deployed
agent, not to provisioning).

```yaml
# ansible/roles/docker_tunneler/tasks/main.yml
- name: Ensure pip3 is installed
  apt:
    name: python3-pip
    state: present
    update_cache: yes

- name: Install docker-tunneler via pip
  pip:
    name: docker-tunneler
    state: present
    extra_args: "--break-system-packages"   # PEP 668; safe in a lab

- name: Verify tunneler binary is on PATH
  command: tunneler --help
  changed_when: false
```

#### 3b. `ssh_keypair_generator` (central only) ✅ Shipped

Generates an SSH keypair on `central`, stashes the public half where the
`ssh_keypair_receiver` role can read it via Ansible's `hostvars`.

```yaml
# ansible/roles/ssh_keypair_generator/tasks/main.yml
- name: Generate ed25519 keypair for ubuntu user
  community.crypto.openssh_keypair:
    path: /home/ubuntu/.ssh/id_ed25519
    type: ed25519
    owner: ubuntu
    group: ubuntu
    mode: "0600"
  register: ship_keypair

- name: Stash the public key on the controller for the receiver role
  set_fact:
    ship_pubkey: "{{ ship_keypair.public_key }}"
```

#### 3c. `ssh_keypair_receiver` (target only) ✅ Shipped

Pulls `central`'s public key from its `hostvars` and adds it to `ubuntu`'s
`authorized_keys` on `target`.

```yaml
# ansible/roles/ssh_keypair_receiver/tasks/main.yml
- name: Authorize central's pubkey for ubuntu
  ansible.posix.authorized_key:
    user: ubuntu
    key: "{{ hostvars['central'].ship_pubkey }}"
    state: present
```

This requires `central`'s play to run before `target`'s (so the fact exists).
If your `site.yml` runs plays per-host in parallel, either serialize them
(simplest) or use a two-pass approach (gather facts first, then apply).

#### 3d. `barak_deploy_staging` (central only) ✅ Shipped

Stages the demo artifacts under `/opt/staging/` and drops the ship script.
The artifacts here are pre-built so the test is deterministic; in a real
deploy the artifacts would come from a CI build or a manual hand-off.

```yaml
# ansible/roles/barak_deploy_staging/tasks/main.yml
- name: Ensure staging dirs exist
  file:
    path: "{{ item }}"
    state: directory
    owner: ubuntu
    group: ubuntu
    mode: "0755"
  loop:
    - /opt/staging
    - /opt/staging/images
    - /opt/staging/configs

- name: Pull alpine:3.19 (last network egress; subsequent steps work offline)
  community.docker.docker_image:
    name: alpine:3.19
    source: pull

- name: Retag alpine as hello:demo
  command: docker tag alpine:3.19 hello:demo
  changed_when: true

- name: Save hello:demo as a tar
  command: docker save hello:demo -o /opt/staging/images/hello.tar
  args:
    creates: /opt/staging/images/hello.tar

- name: Drop sample hello.conf
  copy:
    dest: /opt/staging/configs/hello.conf
    owner: ubuntu
    group: ubuntu
    mode: "0644"
    content: |
      # Sample hello.conf shipped from central → target.
      greeting = hello from {{ inventory_hostname }}
      shipped_at = {{ ansible_date_time.iso8601 }}

- name: Drop ship-deploy.sh
  copy:
    dest: /usr/local/bin/ship-deploy.sh
    owner: root
    group: root
    mode: "0755"
    content: |
      #!/bin/bash
      set -euo pipefail
      TARGET_HOST="${TARGET_HOST:-target}"
      TARGET_USER="${TARGET_USER:-ubuntu}"
      STAGING="/opt/staging"
      WORK=$(mktemp -d)
      trap 'rm -rf "$WORK"' EXIT

      tunneler wrap "$STAGING" -o "$WORK/demo-app.tar.gz" --payload-dir /payload
      tunneler ship "$WORK/demo-app.tar.gz" \
        --transport scp \
        --destination "scp://${TARGET_USER}@${TARGET_HOST}:/var/spool/deploys/demo-app.tar.gz.partial"
      ssh -o StrictHostKeyChecking=accept-new "${TARGET_USER}@${TARGET_HOST}" \
        "mv /var/spool/deploys/demo-app.tar.gz.partial /var/spool/deploys/demo-app.tar.gz"
```

#### 3e. `barak_deploy_agent` (target only) ✅ Shipped

Installs the barak-deploy wheel, creates the system user + state dirs, drops
the four YAML configs from this repo's `examples/cross-vm/`, drops the
systemd unit from this repo's `packaging/`, enables + starts the service.

Three approaches for getting the wheel onto the VM:

- **Local copy** (recommended for the playground): build the wheel on the
  Ansible controller (`cd ~/Workspace/barak-deploy && uv build`), copy
  `dist/barak_deploy-1.0.0-py3-none-any.whl` to the VM, `pip install` it.
- **PyPI / private index**: once you've published, swap the local copy for
  `pip install barak-deploy[redis,metrics]`.
- **HTTP file server on the Ansible controller**: useful if multiple VMs
  share a wheel.

```yaml
# ansible/roles/barak_deploy_agent/defaults/main.yml
barak_deploy_wheel_path: "{{ playbook_dir }}/../files/barak_deploy-1.0.0-py3-none-any.whl"
barak_deploy_examples_dir: "{{ playbook_dir }}/../files/cross-vm"
barak_deploy_systemd_unit: "{{ playbook_dir }}/../files/barak-deploy.service"
barak_deploy_env_example: "{{ playbook_dir }}/../files/barak-deploy.env.example"

# ansible/roles/barak_deploy_agent/tasks/main.yml
- name: Create barak-deploy system user
  user:
    name: barak-deploy
    system: yes
    create_home: yes
    home: /var/lib/barak-deploy
    shell: /usr/sbin/nologin
    groups: docker

- name: Ensure state + config dirs
  file:
    path: "{{ item.path }}"
    state: directory
    owner: "{{ item.owner }}"
    group: "{{ item.group }}"
    mode: "{{ item.mode }}"
  loop:
    - { path: /etc/barak-deploy, owner: root, group: root, mode: "0755" }
    - { path: /etc/barak-deploy/pipelines, owner: root, group: root, mode: "0755" }
    - { path: /var/lib/barak-deploy, owner: barak-deploy, group: barak-deploy, mode: "0755" }
    - { path: /var/lib/barak-deploy/runs, owner: barak-deploy, group: barak-deploy, mode: "0755" }
    - { path: /var/lock/barak-deploy, owner: barak-deploy, group: barak-deploy, mode: "0755" }
    - { path: /var/spool/deploys, owner: barak-deploy, group: barak-deploy, mode: "0755" }
    - { path: /var/spool/deploys/archive, owner: barak-deploy, group: barak-deploy, mode: "0755" }
    - { path: /etc/hello, owner: root, group: root, mode: "0755" }

- name: Allow ubuntu to write to the drop dir (so SCP from central succeeds)
  acl:
    path: /var/spool/deploys
    entity: ubuntu
    etype: user
    permissions: rwx
    state: present

- name: Copy the barak-deploy wheel
  copy:
    src: "{{ barak_deploy_wheel_path }}"
    dest: /tmp/barak_deploy.whl

- name: Install pip3 if not present
  apt:
    name: python3-pip
    state: present

- name: Install barak-deploy from wheel
  pip:
    name: file:///tmp/barak_deploy.whl
    extras: ["redis", "metrics"]      # optional — drop if you don't need them
    state: present
    extra_args: "--break-system-packages"

- name: Drop barak-deploy config files from examples/cross-vm/
  copy:
    src: "{{ barak_deploy_examples_dir }}/{{ item.src }}"
    dest: "/etc/barak-deploy/{{ item.dest }}"
    owner: root
    group: root
    mode: "0644"
  loop:
    - { src: bundles.yaml, dest: bundles.yaml }
    - { src: triggers.yaml, dest: triggers.yaml }
    - { src: identity.yaml, dest: identity.yaml }
    - { src: pipelines/deploy-demo.yaml, dest: pipelines/deploy-demo.yaml }

- name: Drop barak-deploy systemd unit
  copy:
    src: "{{ barak_deploy_systemd_unit }}"
    dest: /etc/systemd/system/barak-deploy.service
    owner: root
    group: root
    mode: "0644"
  notify: reload systemd

- name: Drop barak-deploy environment file
  copy:
    src: "{{ barak_deploy_env_example }}"
    dest: /etc/barak-deploy/barak.env
    owner: root
    group: barak-deploy
    mode: "0640"

- name: Enable + start barak-deploy
  systemd:
    name: barak-deploy
    state: started
    enabled: yes
    daemon_reload: yes

# ansible/roles/barak_deploy_agent/handlers/main.yml
- name: reload systemd
  systemd:
    daemon_reload: yes
```

You need to copy four files into `ansible/files/` from this repo:

```bash
# In ~/Workspace/playground:
mkdir -p ansible/files/cross-vm/pipelines
cp ~/Workspace/barak-deploy/dist/barak_deploy-1.0.0-py3-none-any.whl   ansible/files/
cp ~/Workspace/barak-deploy/packaging/barak-deploy.service             ansible/files/
cp ~/Workspace/barak-deploy/packaging/barak-deploy.env.example         ansible/files/
cp ~/Workspace/barak-deploy/examples/cross-vm/bundles.yaml             ansible/files/cross-vm/
cp ~/Workspace/barak-deploy/examples/cross-vm/triggers.yaml            ansible/files/cross-vm/
cp ~/Workspace/barak-deploy/examples/cross-vm/identity.yaml            ansible/files/cross-vm/
cp ~/Workspace/barak-deploy/examples/cross-vm/pipelines/deploy-demo.yaml ansible/files/cross-vm/pipelines/
```

(You can automate this with a `make sync-from-barak-deploy` target in the
playground's `Makefile` — recommended.)

> **Status:** The `Makefile` with `sync-from-barak-deploy` target ships at
> the repo root. `BARAK_REPO` defaults to `$(HOME)/Workspace/barak-deploy`;
> override on the command line if the sibling repo lives elsewhere. The
> wheel is `.gitignore`d; the cross-vm config set IS committed.
> `ansible/roles/barak_deploy_agent` pre-flights the wheel with a clear
> "run `make sync-from-barak-deploy`" error when it's missing.

### 4. Inter-VM hostname resolution (extra_hosts) ✅ Shipped

The lab YAML's `extra_hosts:` entries need to land in each VM's `/etc/hosts`
during provisioning. If your existing playground doesn't already do this,
add a tiny `extra_hosts` role:

```yaml
# ansible/roles/extra_hosts/tasks/main.yml
- name: Apply extra_hosts entries from lab YAML
  lineinfile:
    path: /etc/hosts
    line: "{{ item }}"
    state: present
  loop: "{{ extra_hosts | default([]) }}"
```

The `extra_hosts` Ansible variable per host comes from the lab YAML's
`vms[*].extra_hosts` — needs a tiny extension to the playground's resolver
(`src/playground/resolver.py` or wherever inventory generation happens) to
plumb that through as a host variable. The exact wiring depends on your
inventory rendering; the principle is "list-of-strings → per-host
`extra_hosts` var → role appends to /etc/hosts."

> **Status:** Shipped end-to-end. `LabVm.extra_hosts` → `ResolvedVm.extra_hosts`
> → `pg_extra_hosts='<json>'` host var in the rendered inventory (same
> shell-escape pattern as `pg_workloads`) → consumed by the new
> `ansible/roles/extra_hosts/` role via `from_json` + `lineinfile`.
> The role runs first in `site.yml` so every later play can resolve
> peers by name.

### 5. Site playbook ordering ✅ Shipped

The cross-VM SSH key distribution requires `central` to be provisioned before
`target` (so the public key fact exists when `target` runs
`ssh_keypair_receiver`). Either:

- Add `central` to a `[deployment_sources]` inventory group and `target` to
  `[deployment_targets]`, with `[deployment_sources]` listed first in
  `site.yml`'s `hosts:` directive (so plays run in order); or
- Run two separate plays: one that runs `ssh_keypair_generator` on
  `central`, then a second that runs everything else (including
  `ssh_keypair_receiver` on `target`).

The simplest form is two plays in `site.yml`:

```yaml
- name: Bootstrap deployment sources (generate SSH keys first)
  hosts: deployment_sources
  roles:
    - ssh_keypair_generator

- name: Provision everything else
  hosts: all
  roles:
    - common
    - docker
    - docker_tunneler
    - { role: barak_deploy_staging, when: "'deployment_sources' in group_names" }
    - { role: barak_deploy_agent, when: "'deployment_targets' in group_names" }
    - { role: ssh_keypair_receiver, when: "'deployment_targets' in group_names" }
    - extra_hosts
```

> **Status:** Implemented with **separate plays per host class** rather
> than one big `hosts: all` play with `when:` guards. The committed
> `site.yml` has:
> 1. `Bootstrap deployment sources` (`hosts: deployment_source`, runs
>    `ssh_keypair_generator`) — first so `ship_pubkey` host facts are
>    populated for the receiver below
> 2. `Apply lab-declared extra_hosts entries` (`hosts: playground`)
> 3. `Configure Playground Guests` (`hosts: playground`, runs `docker`
>    + `redroid`)
> 4. `Install docker-tunneler on deployment hosts`
> 5. `Wire SSH key distribution to deployment targets`
> 6. `Stage barak-deploy demo artifacts on sources`
> 7. `Install barak-deploy on targets`
> 8. + the existing workload plays (container / compose / swarm)
>
> The per-role-snake inventory groups (`deployment_source`,
> `deployment_target`) are emitted automatically by §4c per-role
> grouping in the inventory renderer.

### 6. Test orchestration helper (optional but recommended) ⚠️ Partial: pytest harness shipped, CLI helper deferred

Your playground currently has no "run command on VM A → wait → assert on VM B"
primitive. For repeated test runs this is painful by hand. Two options:

- **CLI subcommand**: `playground exec --on <vm> <cmd>` that wraps the
  inventory lookup + SSH. A 30-line Python wrapper around `subprocess.run
  (["ssh", ...])` covers the common case.
- **pytest-style integration test**: a `tests/integration/test_cross_vm_deploy.py`
  in the playground that uses the existing CLI integration test harness
  (`tests/cli/test_cli.py`) to bring up the lab, run the smoke test, and
  tear down. Closest match to how the playground already tests its own
  CLI; reuses fixtures.

Either is a substantial enough additions that you may want to defer until
after the first manual run proves the end-to-end flow works. The smoke-test
commands below run fine with plain `ssh` for the first pass.

> **Status:** Option B (pytest harness) shipped at
> `tests/integration/multi_vm/test_cross_vm_deploy.py`. Skipped by
> default; runs against real libvirt when `PLAYGROUND_LIVE_INFRA=1`
> is set in the environment. The harness asserts every one of the
> six pass/fail criteria below. Option A (`playground exec --on
> <vm> <cmd>` CLI subcommand) was **deferred** to the roadmap
> backlog — the harness uses plain `subprocess` + `ssh` instead.

## Bringing up the test

After all the above is in place:

```bash
cd ~/Workspace/playground
playground apply config/labs/barak-deploy-cross-vm.yaml
# or whatever your tofu+ansible orchestration command is

# Capture VM IPs (your playground likely has a `playground ips` or similar):
A_IP=$(playground ip central)
B_IP=$(playground ip target)

# Sanity check: both VMs reachable, both have docker + tunneler, B has barak-deploy:
ssh ubuntu@$A_IP 'tunneler --help && docker ps'
ssh ubuntu@$B_IP 'tunneler --help && docker ps && systemctl is-active barak-deploy'

# Trigger the deploy:
ssh ubuntu@$A_IP '/usr/local/bin/ship-deploy.sh'

# Verification (after ~10 seconds):
ssh ubuntu@$B_IP 'docker ps --filter name=hello'
ssh ubuntu@$B_IP 'cat /etc/hello/hello.conf'
ssh ubuntu@$B_IP 'sudo -u barak-deploy barak-deploy history --since "5 minutes ago"'
ssh ubuntu@$B_IP 'ls -la /var/spool/deploys/archive/ok/'

# Idempotency check — re-run on A, then verify the new history entry has
# skipped=true for every step:
ssh ubuntu@$A_IP '/usr/local/bin/ship-deploy.sh'
sleep 10
ssh ubuntu@$B_IP 'sudo -u barak-deploy barak-deploy history --since "2 minutes ago" --output json' | jq .

# Teardown:
playground destroy config/labs/barak-deploy-cross-vm.yaml
```

## Pass/fail criteria

The test passes iff all of:

1. **Container running on target.** `docker ps --filter name=hello` shows the
   `hello` container with `STATUS = Up`. Image digest matches the one
   `docker save`'d on central (cross-check with `docker images --digests`
   on both VMs).
2. **Config file in place.** `/etc/hello/hello.conf` on target exists,
   matches the source content with the templated `greeting` and `shipped_at`
   substituted by Ansible at provisioning time.
3. **Pipeline ran successfully.** `barak-deploy history --since 5m` on
   target shows one pipeline record with `status: ok` and four step records
   (`unwrap`, `load`, `place-config`, `run`), all `status: ok`.
4. **Tar archived correctly.** `/var/spool/deploys/archive/ok/demo-app.tar.gz`
   exists with non-zero size; matches the sha256 of the source on central.
5. **Manifest written.** `/var/lib/barak-deploy/extracts/demo-app/.bundle-manifest.json`
   exists, lists `images/hello.tar` and `configs/hello.conf` in its `files`
   field, has a `tar_sha256` matching the shipped tar.
6. **Idempotency check passes.** Second `ship-deploy.sh` produces a second
   history entry where the `unwrap`, `load`, `place-config`, and `run`
   steps all have `skipped: true` in their output. Total pipeline duration
   on the second run < 1 second.

## Playground gaps this test surfaces

Items the playground may want to address; not blocking for this test.
**Status as of implementation:**

- ~~**No lab-scoped DNS.**~~ Workaround via `extra_hosts` is shipped (§4).
  True lab-scoped DNS remains on the roadmap backlog.
- ~~**No `ip:` per-network specification in the lab YAML schema.**~~
  Shipped — `LabVmNetwork` accepts an optional `ip:` and the tofu module
  now pins it via `network_interface.addresses`. Legacy `list[str]`
  shape still works.
- **No multi-VM orchestration primitive.** Still no `playground exec
  --on <vm> <cmd>` subcommand. The pytest harness uses plain
  `subprocess` + `ssh` instead. Promoted to the roadmap backlog.
- ~~**No inter-VM SSH keypair distribution role.**~~ Shipped as
  platform-generic — `ssh_keypair_generator` + `ssh_keypair_receiver`
  live in `ansible/roles/` and consume the auto-emitted
  `deployment_source` / `deployment_target` inventory groups.
- **No file-transfer primitive between provisioned VMs.** Still
  absent. As the spec notes, not needed for this test (the test
  exercises that path itself via `tunneler ship`). Future multi-host
  tests may need it.
- ~~**No multi-VM integration test scaffolding.**~~ Shipped at
  `tests/integration/multi_vm/` with the cross-vm test as the first
  example. Future cross-host tests follow the same pattern (env-var
  gate, real-infra subprocess driver, finally-teardown).

## What this repo (barak-deploy) ships in support

For reference, the barak-deploy side is fully shipped:

| Artifact | Path in barak-deploy |
|---|---|
| Wheel + sdist | `dist/barak_deploy-1.0.0-py3-none-any.whl`, `dist/barak_deploy-1.0.0.tar.gz` |
| systemd unit | `packaging/barak-deploy.service` |
| Env-file template | `packaging/barak-deploy.env.example` |
| Receiver config set | `examples/cross-vm/` (5 files) |
| Operator install guide | `docs/install.md` |
| Pattern documentation | `docs/cookbook.md` — Recipe 8 |

If anything in `examples/cross-vm/` needs to change to fit a playground
constraint we didn't anticipate, edit that and report back — it's the
canonical receiver-side configuration and the playground should consume it
as-is whenever practical.
