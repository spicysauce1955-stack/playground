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
# `domain` is the lab-scoped DNS domain so dnsmasq on EACH network
# serves the same suffix, letting any VM in the lab resolve any other
# by short name (search domain) or FQDN. Per-VM hostname records
# come from var.vm_dns_hosts — see the dynamic `dns` block below.
resource "libvirt_network" "lab" {
  for_each = { for n in var.networks : n.name => n }

  name      = each.value.name
  mode      = "nat"
  domain    = var.dns_domain
  addresses = [each.value.cidr]

  dhcp {
    enabled = true
  }

  # Authoritative DNS records — set when the lab pinned IPs for VMs
  # on this network. Belt-and-braces with cloud-init's self-registered
  # hostname (cloud_init.cfg sets hostname/fqdn from the same data).
  dynamic "dns" {
    for_each = length(lookup(var.vm_dns_hosts, each.key, [])) > 0 ? [1] : []
    content {
      # Without this, the provider's getDNSEnableFromResource emits
      # <dns enable='no'> and libvirt disables dnsmasq DNS for the
      # network entirely — silently ignoring the host records below.
      enabled = true
      dynamic "hosts" {
        for_each = var.vm_dns_hosts[each.key]
        content {
          hostname = hosts.value.hostname
          ip       = hosts.value.ip
        }
      }
    }
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
    vm_name        = local.effective_vm_names[count.index]
    dns_domain     = var.dns_domain
  })
}

# Define the Guest VMs. Each VM gets one network_interface per entry in
# var.vm_networks[vm_name], with addresses pinned when var.vm_network_ips
# has an entry for (vm, network).
resource "libvirt_domain" "playground_node" {
  count  = length(local.effective_vm_names)
  name   = local.effective_vm_names[count.index]
  type   = var.domain_type
  memory = var.vm_memory
  vcpu   = var.vm_vcpu

  # CPU mode is configurable per lab (via spec.providers.local-libvirt.cpu_mode).
  # Default `host-passthrough` is required for the redroid-host lab — Redroid
  # containers need binderfs, which needs full CPU feature passthrough. On
  # hosts where the L0 hypervisor doesn't tolerate VMX passthrough (symptom:
  # QEMU pauses or crashes the guest right after `virsh start` and the kernel
  # logs `kvm_intel: vmread/vmwrite failed`), a non-Redroid lab can override
  # to `host-model`.
  cpu {
    mode = var.cpu_mode
  }

  # Inject `<feature policy='disable' name='X'/>` children into the
  # generated <cpu> block via the provider's xslt escape hatch. The block
  # is emitted only when var.cpu_features_disable is non-empty so default
  # operation is unchanged. Pair with `cpu_mode: host-model` for reliable
  # masking — `host-passthrough` can leak the underlying flag despite the
  # disable (Ubuntu bug #1830268).
  dynamic "xml" {
    for_each = length(var.cpu_features_disable) > 0 ? [1] : []
    content {
      xslt = templatefile(
        "${path.module}/cpu_features_disable.xslt.tftpl",
        { features = var.cpu_features_disable },
      )
    }
  }

  dynamic "network_interface" {
    for_each = local.vm_interfaces[local.effective_vm_names[count.index]]
    content {
      network_id     = libvirt_network.lab[network_interface.value.net].id
      addresses      = network_interface.value.ip != "" ? [network_interface.value.ip] : null
      wait_for_lease = network_interface.key == 0
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
