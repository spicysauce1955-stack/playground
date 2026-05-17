variable "vm_count" {
  description = "Number of playground VMs to provision"
  type        = number
  default     = 1
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
  description = "URL for the Ubuntu Cloud Image"
  type        = string
  default     = "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"
}
