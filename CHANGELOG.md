# Changelog

Notable user-visible changes. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). This file was
started with the S19 fix below; earlier changes, including the rest of
what has landed since v0.1.0, are in the git history.

## [Unreleased]

### Fixed

- **cloud-digitalocean: `playground isolate` no longer cuts DHCP (S19).**
  Droplets from custom images (RHEL, Rocky, ...) lease both NICs'
  addresses by DHCP from DigitalOcean's server at `169.254.169.253`, and
  Cloud Firewall filters that traffic. An isolated RHEL guest therefore
  never came back from a reboot, and would have lost its addresses when
  its 24 h lease ran out. The isolated lab firewall now also allows
  outbound udp/67 to `169.254.169.253/32` and `255.255.255.255/32`, and
  nothing else. Outside TCP/UDP/ICMP, outside DNS and the VPC resolver
  stay blocked. Measured live, an isolated RHEL 8.8 guest reboots and is
  back in about 30 s with both NICs leased. Stock Ubuntu guests use a
  static network config and were never affected.
- `isolate` verifies the new rule through the DigitalOcean API: exactly
  one udp/67 rule to exactly those two addresses. A sibling lab's
  isolated firewall carrying the same rule does not count as foreign
  egress.

### Changed

- **Labs applied before this fix must be re-applied before `isolate`.**
  `isolate` refuses (`runtime.isolate.needs_reapply`) a lab whose
  per-lab OpenTofu root predates the DHCP rule, because isolating it
  would cut DHCP again. `connect` still works on such a lab. To adopt:
  `playground connect <lab>` (if isolated), `playground apply <lab>`
  (with an unchanged config, tofu reports no changes), then
  `playground isolate <lab>`.
- Python API: `outbound_is_droplets_only` in
  `backend/cloud_digitalocean/isolation.py` is renamed
  `outbound_is_isolated`, since the isolated rule set now includes the
  DHCP rule.
