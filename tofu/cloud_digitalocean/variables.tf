variable "name_prefix" {
  description = "Prefix prepended to every Droplet and firewall name so lab resources are identifiable in the DigitalOcean console. The Slice-3 runner passes the lab name here."
  type        = string
}

variable "vm_names" {
  description = "Ordered list of VM names from the lab spec. Each entry produces one Droplet named '<name_prefix>-<vm>'. The `vm_ips` output is keyed by the bare VM name so `fetch_vm_ips` / the inventory renderer can pair by lab VM name without stripping the prefix."
  type        = list(string)
}

variable "region" {
  description = "DigitalOcean region slug where all Droplets are created (e.g. 'nyc3', 'sfo3'). Set via the provider config or per-lab override."
  type        = string
}

variable "size" {
  description = "DigitalOcean Droplet size slug (e.g. 's-1vcpu-1gb'). Set via the provider config or per-lab override."
  type        = string
}

variable "image" {
  description = "DigitalOcean image slug or ID used as the Droplet base image (e.g. 'ubuntu-24-04-x64'). Set via the provider config or per-lab override."
  type        = string
}

variable "ssh_public_key" {
  description = "SSH public key content (not a path) injected into the ubuntu user's `ssh_authorized_keys` via cloud-init. This is the primary SSH access mechanism and works regardless of whether the key is pre-registered in DigitalOcean."
  type        = string
}

variable "ssh_key_fingerprints" {
  description = "Optional list of DigitalOcean-registered SSH key fingerprints attached to each Droplet via the `ssh_keys` parameter. Belt-and-braces with the cloud-init injection; both may be set independently. Defaults to empty so the cloud-init path works without any pre-registration."
  type        = list(string)
  default     = []
}

variable "tags" {
  description = "Ownership tags applied to every Droplet and the firewall. Used by status / suspend / destroy to tag-sweep for orphaned, still-billing resources."
  type        = list(string)
  default     = []
}

variable "firewall_ssh_cidrs" {
  description = "Source CIDR blocks allowed to reach port 22 on all Droplets. Empty list means allow all (0.0.0.0/0 + ::/0) — doctor warns about this in Slice 5. Restrict to a known IP range in production."
  type        = list(string)
  default     = []
}

variable "dns_domain" {
  description = "Per-lab DNS domain used for the cloud-init hostname/fqdn, mirroring the `dns_domain` variable in the libvirt root. Set via `spec.dns_domain` in the lab YAML (default: <lab-name>.lab)."
  type        = string
  default     = "playground.lab"
}
