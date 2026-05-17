output "vm_ips" {
  description = "The dynamically assigned IP addresses of the guest VMs"
  value       = libvirt_domain.playground_node[*].network_interface[0].addresses[0]
}

output "ssh_commands" {
  description = "Commands to SSH into the VMs"
  value       = [for ip in libvirt_domain.playground_node[*].network_interface[0].addresses[0] : "ssh ubuntu@${ip}"]
}
