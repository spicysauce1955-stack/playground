# Automated Local/Remote Infrastructure Playground

This repository contains fully automated Infrastructure-as-Code (IaC) to provision a KVM/libvirt playground, configure it via Ansible, and run containerized Android (Redroid) inside the nested VMs.

## Prerequisites
- **Ubuntu Host** with KVM/libvirt enabled.
- **OpenTofu** installed.
- **Ansible** installed.
- **SSH Keys** generated in `~/.ssh/id_rsa.pub`.

## Deployment Pipeline

### 1. Provision Infrastructure (OpenTofu)
Navigate to the `tofu/` directory to create the isolated bridge network and virtual machines.

```bash
cd tofu
tofu init
tofu plan
tofu apply -auto-approve
```

### 2. Configure Instances (Ansible)
Once OpenTofu outputs the IP addresses of your VMs, inject them into `ansible/inventory.ini`. Then, run the configuration playbooks:

```bash
cd ../ansible
ansible-playbook -i inventory.ini site.yml
```

### 3. Connect to Android Emulator
The Android ADB port is exposed on port `5555` on the VM IPs. You can connect from the host:

```bash
adb connect <VM_IP>:5555
```

## Teardown
To destroy the VMs and network:

```bash
cd tofu
tofu destroy -auto-approve
```
