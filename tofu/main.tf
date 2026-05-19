terraform {
  required_providers {
    libvirt = {
      source  = "dmacvicar/libvirt"
      version = "~> 0.7.1"
    }
  }
}

provider "libvirt" {
  uri = "qemu:///system"
}

# Effective VM names. When var.vm_names is set, use it verbatim — the
# operator is opting into name-keyed pairing with a lab. Otherwise fall
# back to the legacy `pg-node-${index+1}` scheme driven by var.vm_count
# so existing setups keep working.
locals {
  effective_vm_names = (
    var.vm_names != null
    ? var.vm_names
    : [for i in range(var.vm_count) : "pg-node-${i + 1}"]
  )

  # Per-VM list of {net, ip} interface descriptors. When a VM has no
  # explicit vm_networks entry, default to attaching it to the first
  # declared network (back-compat). When a network has no pinned IP for
  # this VM, the empty string sentinel means "let libvirt assign via DHCP".
  vm_interfaces = {
    for vm in local.effective_vm_names :
    vm => [
      for net in lookup(var.vm_networks, vm, [var.networks[0].name]) : {
        net = net
        ip  = lookup(lookup(var.vm_network_ips, vm, {}), net, "")
      }
    ]
  }
}

# One libvirt_network per entry in var.networks. NAT mode, DHCP enabled.
# Domain name defaults to <network_name>.lab so different labs don't
# collide on the same libvirt host.
resource "libvirt_network" "lab" {
  for_each = { for n in var.networks : n.name => n }

  name      = each.value.name
  mode      = "nat"
  domain    = "${each.value.name}.lab"
  addresses = [each.value.cidr]

  dhcp {
    enabled = true
  }
}

# Fetch the Ubuntu Cloud Image
resource "libvirt_volume" "ubuntu_image" {
  name   = "ubuntu-noble.qcow2"
  pool   = "default"
  source = var.ubuntu_image_url
  format = "qcow2"
}

# Create a volume per VM based on the base image
resource "libvirt_volume" "vm_disk" {
  count          = length(local.effective_vm_names)
  name           = "${local.effective_vm_names[count.index]}.qcow2"
  pool           = "default"
  base_volume_id = libvirt_volume.ubuntu_image.id
  format         = "qcow2"
  size           = 20 * 1024 * 1024 * 1024 # 20GB
}

# Generate Cloud-Init ISO for user data
resource "libvirt_cloudinit_disk" "commoninit" {
  count = length(local.effective_vm_names)
  name  = "commoninit-${local.effective_vm_names[count.index]}.iso"
  pool  = "default"
  user_data = templatefile("${path.module}/cloud_init.cfg", {
    ssh_public_key = file(var.ssh_public_key_path)
  })
}

# Define the Guest VMs. Each VM gets one network_interface per entry in
# var.vm_networks[vm_name], with addresses pinned when var.vm_network_ips
# has an entry for (vm, network).
resource "libvirt_domain" "playground_node" {
  count  = length(local.effective_vm_names)
  name   = local.effective_vm_names[count.index]
  memory = var.vm_memory
  vcpu   = var.vm_vcpu

  # Enable nested virtualization by passing host CPU flags
  # This is critical for running Docker/Redroid efficiently inside the VM
  cpu {
    mode = "host-passthrough"
  }

  dynamic "network_interface" {
    for_each = local.vm_interfaces[local.effective_vm_names[count.index]]
    content {
      network_id     = libvirt_network.lab[network_interface.value.net].id
      addresses      = network_interface.value.ip != "" ? [network_interface.value.ip] : null
      wait_for_lease = true
    }
  }

  disk {
    volume_id = libvirt_volume.vm_disk[count.index].id
  }

  cloudinit = libvirt_cloudinit_disk.commoninit[count.index].id

  console {
    type        = "pty"
    target_port = "0"
    target_type = "serial"
  }

  console {
    type        = "pty"
    target_type = "virtio"
    target_port = "1"
  }

  graphics {
    type        = "spice"
    listen_type = "address"
    autoport    = true
  }
}
