output "vm_ips" {
  description = "Map of VM name -> public IPv4 address. Keys are the bare VM names from var.vm_names (no prefix), matching the lab spec. This shape is a hard contract: `fetch_vm_ips` in local_libvirt/inventory.py parses it as dict[str,str] and the cloud backend reuses that function unchanged."
  value = {
    for idx, name in var.vm_names :
    name => digitalocean_droplet.node[idx].ipv4_address
  }
}

output "ssh_commands" {
  description = "Map of VM name -> ssh command to reach it. Mirrors the libvirt outputs.tf shape for operator convenience."
  value = {
    for idx, name in var.vm_names :
    name => "ssh ubuntu@${digitalocean_droplet.node[idx].ipv4_address}"
  }
}

output "droplet_ids" {
  description = "Map of VM name -> DigitalOcean Droplet ID. Used by the Slice-3 runner for status queries and tag-based orphan sweeps during suspend/destroy."
  value = {
    for idx, name in var.vm_names :
    name => digitalocean_droplet.node[idx].id
  }
}
