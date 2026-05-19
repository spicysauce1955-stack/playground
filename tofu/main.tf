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
}

# Define the isolated playground network
resource "libvirt_network" "playground_net" {
  name      = "playground_net"
  mode      = "nat"
  domain    = "playground.local"
  addresses = ["10.0.10.0/24"]

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

# Define the Guest VMs
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

  network_interface {
    network_id     = libvirt_network.playground_net.id
    wait_for_lease = true
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
