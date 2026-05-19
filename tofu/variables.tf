variable "vm_count" {
  description = "Number of playground VMs to provision. Ignored when var.vm_names is set."
  type        = number
  default     = 1
}

variable "vm_names" {
  description = "Optional list of VM names. When set, overrides var.vm_count and gives each libvirt_domain a name matching the corresponding entry in lab.spec.vms (so `playground inventory render` can pair lab VMs with tofu IPs by name instead of by index). When null, falls back to legacy `pg-node-N` naming."
  type        = list(string)
  default     = null
}

variable "networks" {
  description = "Lab networks to create. Each entry becomes a `libvirt_network` resource. Defaults to a single NAT network on 10.0.10.0/24 so legacy single-network runs keep working without a var-file. Set via `playground tofu render` from the lab's `spec.networks`."
  type = list(object({
    name = string
    cidr = string
  }))
  default = [
    { name = "playground_net", cidr = "10.0.10.0/24" }
  ]
}

variable "vm_networks" {
  description = "Which networks each VM joins, keyed by VM name. Each VM gets one `network_interface` per entry. Defaults to the first network when unset for a given VM — back-compat with the legacy single-network behavior."
  type        = map(list(string))
  default     = {}
}

variable "vm_network_ips" {
  description = "Per-VM static IP pins, keyed by VM name then by network name. Each pinned IP becomes the interface's `addresses` so libvirt assigns it deterministically. Networks without a pin fall back to DHCP."
  type        = map(map(string))
  default     = {}
}

variable "vm_memory" {
  description = "Amount of RAM per VM in MB"
  type        = number
  default     = 4096
}

variable "vm_vcpu" {
  description = "Number of vCPUs per VM"
  type        = number
  default     = 2
}

variable "ssh_public_key_path" {
  description = "Path to the public SSH key to inject via cloud-init"
  type        = string
  default     = "~/.ssh/id_rsa.pub"
}

variable "ubuntu_image_url" {
  description = "Source for the Ubuntu Cloud Image. Accepts an https:// URL or an absolute local file path (file:///...). PRD section 5 prefers a pre-downloaded local path for air-gap readiness; override the default in terraform.tfvars when running offline."
  type        = string
  default     = "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"
}
