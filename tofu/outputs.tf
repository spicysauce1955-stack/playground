output "vm_ips" {
  description = "Map of VM domain name -> first NIC IP address. Keys match var.vm_names when set, or pg-node-N otherwise. Consumed by `playground inventory render` to pair lab VMs with tofu IPs by name."
  value = {
    for idx, name in local.effective_vm_names :
    name => libvirt_domain.playground_node[idx].network_interface[0].addresses[0]
  }
}

output "ssh_commands" {
  description = "Map of VM domain name -> ssh command to reach it."
  value = {
    for idx, name in local.effective_vm_names :
    name => "ssh ubuntu@${libvirt_domain.playground_node[idx].network_interface[0].addresses[0]}"
  }
}
