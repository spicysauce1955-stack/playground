# DigitalOcean provider authenticates via the DIGITALOCEAN_TOKEN environment
# variable. The block is intentionally empty — no token attribute anywhere in
# any .tf file (design risk #2: token leak into a committed or logged artifact).
provider "digitalocean" {}

locals {
  # When no source CIDRs are specified, the firewall opens SSH to the entire
  # internet. Doctor (Slice 5) emits a warning for this case; it is valid for
  # scratch / smoke labs where the exact client IP is unknown.
  ssh_inbound = length(var.firewall_ssh_cidrs) > 0 ? var.firewall_ssh_cidrs : ["0.0.0.0/0", "::/0"]
}

# One Droplet per entry in var.vm_names. The Droplet name is prefixed so
# resources from multiple concurrent labs are distinct in the DO console.
resource "digitalocean_droplet" "node" {
  count = length(var.vm_names)

  name   = "${var.name_prefix}-${var.vm_names[count.index]}"
  region = var.region
  size   = var.size
  image  = var.image

  # DO-registered keys attached at creation time (optional belt-and-braces;
  # the cloud-init ssh_authorized_keys injection below is the primary path).
  ssh_keys = var.ssh_key_fingerprints

  tags = var.tags

  # Portable cloud-config consumed by cloud-init on first boot. Injects the
  # ubuntu user, SSH key, and hostname/fqdn — identical template variables to
  # the libvirt cloud_init.cfg so the same Ansible roles apply unchanged.
  user_data = templatefile("${path.module}/cloud_init.cfg", {
    vm_name        = var.vm_names[count.index]
    dns_domain     = var.dns_domain
    ssh_public_key = var.ssh_public_key
  })
}

# Single firewall shared by all Droplets in the lab, matched by tag.
# Inbound: SSH only, from local.ssh_inbound.
# Outbound: unrestricted TCP/UDP/ICMP so VMs can reach apt/docker registries.
resource "digitalocean_firewall" "lab" {
  name = "${var.name_prefix}-fw"
  tags = var.tags

  # Attach to every Droplet in this lab by ID so the firewall tracks new
  # Droplets created in subsequent applies.
  droplet_ids = digitalocean_droplet.node[*].id

  inbound_rule {
    protocol         = "tcp"
    port_range       = "22"
    source_addresses = local.ssh_inbound
  }

  outbound_rule {
    protocol              = "tcp"
    port_range            = "1-65535"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }

  outbound_rule {
    protocol              = "udp"
    port_range            = "1-65535"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }

  outbound_rule {
    protocol              = "icmp"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }
}
