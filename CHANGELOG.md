# Changelog

Notable user-visible changes. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). For v0.1.0 and
earlier, see the git history.

## [Unreleased]

## [0.2.0] - 2026-10-26

### Added

- **cloud-digitalocean: per-VM images.**
  `spec.vms[].provider_overrides.cloud-digitalocean.image` takes a slug
  or a numeric image ID. It overrides the lab `image`, which overrides
  `ubuntu-24-04-x64`.
- **cloud-digitalocean: custom images from a URL.** Use per-VM
  `image_url`, with an optional `image_distribution`. Each URL is
  imported once per lab as a `digitalocean_custom_image`; `destroy` and
  `suspend` delete it, and a best-effort `image-sweep` teardown step
  catches leftovers. The import takes about 17 minutes on every fresh
  apply and resume. For labs you re-create often, import the image once
  and reference its numeric ID instead.
- **cloud-digitalocean: per-VM `package_upgrade`.** Set it to `false` to
  keep a pinned point release (e.g. Rocky 8.8) from being upgraded on
  first boot.
- **cloud-digitalocean: a DO-registered SSH key for custom and
  non-Ubuntu images.** DigitalOcean refuses to create those Droplets
  without one, so apply and resume reuse an account key with the lab
  key's fingerprint or upload it as `playground-<fp8>`. `destroy` keeps
  the key.
- **Validation:** `config.backend.do_vm_image_invalid` and
  `config.backend.do_package_upgrade_invalid`.
- **Ansible: RedHat-family hosts (Rocky, Alma, RHEL 8 and 9) in the
  `common` and `docker` roles.** These paths need PyPI access, a new
  internet dependency for them. The Ubuntu-only roles (`redroid`,
  `capture`, `lab_scheduler`) now fail fast, with a clear message, on a
  non-Debian host.

### Fixed

- **cloud-digitalocean: `playground isolate` no longer cuts DHCP (S19).**
  Droplets from custom images (RHEL, Rocky, ...) lease both NICs'
  addresses by DHCP from DigitalOcean's server at `169.254.169.253`, and
  Cloud Firewall filters that traffic. An isolated RHEL guest therefore
  never came back from a reboot, and would have lost its addresses when
  its 24 h lease ran out.
  - While isolated, the lab firewall now also allows outbound udp/67 to
    `169.254.169.253/32` and `255.255.255.255/32`, and nothing else.
  - Outside TCP/UDP/ICMP, outside DNS and the VPC resolver stay blocked.
  - Measured live, an isolated RHEL 8.8 guest reboots and is back in
    about 30 s with both NICs leased.
  - Stock Ubuntu guests use a static network config and were never
    affected.
  - `isolate` verifies the new rule through the DigitalOcean API:
    exactly one udp/67 rule to exactly those two addresses.
- **cloud-init readiness gate:** it runs under `sudo`, because Rocky 8's
  cloud-init crashes as a non-root user. It retries when the status
  command itself crashes during a first-boot upgrade, and its budget is
  now 30 minutes (was 10).
- **`verify-lab`:** it lets a still-booting systemd settle (EL8's
  systemd has no `--wait`).
- **`plan`:** it shows the image each DigitalOcean VM really boots.
- **cloud-digitalocean:** drift on a custom image's distribution label
  is ignored. Before, it planned a change on every apply and made
  `isolate` refuse the lab.
- **cloud-digitalocean:** a change to a Droplet's SSH keys never
  replaces the Droplet.
- **Test suite:** it no longer calls the real DigitalOcean API with an
  exported token.

### Changed

- **Labs applied before the S19 fix must be re-applied before
  `isolate`.** `isolate` refuses (`runtime.isolate.needs_reapply`) a lab
  whose per-lab OpenTofu root predates the DHCP rule, because isolating
  it would cut DHCP again. `connect` still works on such a lab. To
  adopt: `playground connect <lab>` (if isolated), `playground apply
  <lab>` (with an unchanged config, tofu reports no changes), then
  `playground isolate <lab>`.
- **EL8 targets need controller ansible-core 2.16 or older.** Nothing
  enforces this; see `docs/architecture/CONTRACTS.md`.
- **Python API:** `outbound_is_droplets_only` in
  `backend/cloud_digitalocean/isolation.py` is renamed
  `outbound_is_isolated`, since the isolated rule set now includes the
  DHCP rule.

[Unreleased]: https://github.com/spicysauce1955-stack/playground/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/spicysauce1955-stack/playground/compare/v0.1.0...v0.2.0
