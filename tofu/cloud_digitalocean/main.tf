# DigitalOcean provider authenticates via the DIGITALOCEAN_TOKEN environment
# variable. The block is intentionally empty — no token attribute anywhere in
# any .tf file (design risk #2: token leak into a committed or logged artifact).
provider "digitalocean" {}

locals {
  # When no source CIDRs are specified, the firewall opens SSH to the entire
  # internet. Doctor (Slice 5) emits a warning for this case; it is valid for
  # scratch / smoke labs where the exact client IP is unknown.
  ssh_inbound = length(var.firewall_ssh_cidrs) > 0 ? var.firewall_ssh_cidrs : ["0.0.0.0/0", "::/0"]

  # Outbound destinations for the lab firewall. Normally the whole internet;
  # `playground isolate` flips var.egress_isolated so the only reachable
  # destination is the lab's own VPC (VM-to-VM). DO Cloud Firewalls are
  # stateful, so replies to inbound SSH still flow while isolated.
  egress_destinations = var.egress_isolated ? [var.vpc_cidr] : ["0.0.0.0/0", "::/0"]
}

# One VPC per lab, sized by the lab network's CIDR. Droplets get a private
# address in it; the firewall below allows lab-internal traffic on it. The
# VPC is destroyed with the lab (tofu orders it after the Droplets).
resource "digitalocean_vpc" "lab" {
  name        = "${var.name_prefix}-vpc"
  region      = var.region
  ip_range    = var.vpc_cidr
  description = "playground lab ${var.name_prefix}"
}

# One Droplet per entry in var.vm_names. The Droplet name is prefixed so
# resources from multiple concurrent labs are distinct in the DO console.
resource "digitalocean_droplet" "node" {
  count = length(var.vm_names)

  name   = "${var.name_prefix}-${var.vm_names[count.index]}"
  region = var.region
  size   = lookup(var.vm_sizes, var.vm_names[count.index], var.size)
  image  = var.image

  # DO-registered keys attached at creation time (optional belt-and-braces;
  # the cloud-init ssh_authorized_keys injection below is the primary path).
  ssh_keys = var.ssh_key_fingerprints

  tags = var.tags

  # Private network for VM-to-VM traffic. Changing vpc_uuid on an existing
  # Droplet forces replacement.
  vpc_uuid = digitalocean_vpc.lab.id

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
# Inbound: SSH from local.ssh_inbound, plus all TCP/UDP/ICMP from the lab VPC
# CIDR (VM-to-VM on any port). The VPC CIDR is used rather than source_tags
# because it is exactly the set the isolated outbound rule can still reach.
# Outbound: TCP/UDP/ICMP to local.egress_destinations (open so VMs can reach
# apt/docker registries, or VPC-only while the lab is isolated).
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

  inbound_rule {
    protocol         = "tcp"
    port_range       = "1-65535"
    source_addresses = [var.vpc_cidr]
  }

  inbound_rule {
    protocol         = "udp"
    port_range       = "1-65535"
    source_addresses = [var.vpc_cidr]
  }

  inbound_rule {
    protocol         = "icmp"
    source_addresses = [var.vpc_cidr]
  }

  outbound_rule {
    protocol              = "tcp"
    port_range            = "1-65535"
    destination_addresses = local.egress_destinations
  }

  outbound_rule {
    protocol              = "udp"
    port_range            = "1-65535"
    destination_addresses = local.egress_destinations
  }

  outbound_rule {
    protocol              = "icmp"
    destination_addresses = local.egress_destinations
  }
}
