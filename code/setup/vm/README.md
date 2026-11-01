# Experiment VM (VirtualBox + Ubuntu 26.04 LTS Server)

Every final result comes from this disposable VM, so its settings are part of the experiment. Record any deviation in
`meta.json`. `sysinfo.py` captures most of these settings automatically.

## 1. Create the VM (Windows PowerShell)

First download the Ubuntu 26.04 LTS **Server** ISO (amd64) from https://ubuntu.com/download/server. Then run:

```powershell
$VBM = "C:\Program Files\Oracle\VirtualBox\VBoxManage.exe"
$VM  = "fairexp"
$ISO = "$env:USERPROFILE\Downloads\ubuntu-26.04-live-server-amd64.iso"   # adjust to the real file name

& $VBM createvm --name $VM --ostype Ubuntu_64 --register
& $VBM modifyvm $VM --cpus 4 --memory 6144 --vram 16 --ioapic on --pae on `
    --paravirtprovider kvm --nested-paging on --audio-enabled off --usb off `
    --graphicscontroller vmsvga --nic1 nat --natpf1 "ssh,tcp,127.0.0.1,2222,,22"
& $VBM createmedium disk --filename "$env:USERPROFILE\VirtualBox VMs\$VM\$VM.vdi" --size 30720
& $VBM storagectl $VM --name SATA --add sata --controller IntelAhci
& $VBM storageattach $VM --storagectl SATA --port 0 --device 0 --type hdd --medium "$env:USERPROFILE\VirtualBox VMs\$VM\$VM.vdi"
& $VBM storageattach $VM --storagectl SATA --port 1 --device 0 --type dvddrive --medium $ISO
& $VBM startvm $VM
```

On older VirtualBox versions, `--audio-enabled off` is spelled `--audio none`.

| Setting | Value | Why |
|---|---|---|
| vCPUs | 4 | CPU 0 is for housekeeping. Experiments use CPUs 1..k (k ≤ 3) through a cpuset |
| RAM / disk | 6 GB / 30 GB | Enough for the toolchain plus raw results |
| Paravirtualization | KVM | Gives the guest kvm-clock, a stable clocksource |
| Network | NAT, host 127.0.0.1:2222 → guest 22 | SSH from Windows (yours and Claude's) |

## 2. Install Ubuntu Server

- Choose a minimal install and tick **Install OpenSSH server**. Skip all snaps.
- Use the username `fair`, or any other. If you pick another, swap it into the commands below.
- After the first boot, run:

```bash
sudo apt update && sudo apt full-upgrade -y && sudo reboot
```

## 3. SSH key from Windows

```powershell
ssh-keygen -t ed25519            # accept the defaults if you have no key yet
type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh -p 2222 fair@127.0.0.1 "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys"
ssh -p 2222 fair@127.0.0.1       # should log in without a password
```

## 4. GitHub access from the VM (deploy key, so the VM can push results)

```bash
ssh-keygen -t ed25519 -f ~/.ssh/github_deploy -N ""
cat ~/.ssh/github_deploy.pub     # paste into GitHub → repo → Settings → Deploy keys (tick "Allow write access")
printf 'Host github.com\n  IdentityFile ~/.ssh/github_deploy\n' >> ~/.ssh/config
git clone git@github.com:<org>/<repo>.git ~/fairexp
```

## 5. Provision and snapshot

```bash
cd ~/fairexp
sudo ./code/setup/provision.sh --vm     # packages, kernel hold, passwordless sudo, noise reduction, checks
make -C code                            # builds wlgen + schedmon
sudo ./code/build/schedmon --selftest   # should print non-zero event counts
```

Then shut the VM down and take the rollback snapshot, from PowerShell:

```powershell
& $VBM snapshot fairexp take clean-provisioned --description "after provision.sh"
```

## 6. Day-0 checks (paste the output into the team chat)

```bash
uname -r
grep -E 'CONFIG_SCHED_CLASS_EXT|CONFIG_HZ=|CONFIG_PREEMPT' /boot/config-$(uname -r)
sudo cat /sys/kernel/debug/sched/base_slice_ns
sudo ./code/build/wlgen calibrate
python3 code/setup/sysinfo.py | head -40
```

- `CONFIG_SCHED_CLASS_EXT=y` enables the optional sched_ext baseline. If it is missing, we stay on the tracing-only track, which is the plan anyway.
- `wlgen interactive --slice-us 100` writes `slice_readback_ns` into its summary. A value of 100000 means the kernel accepts custom EEVDF slices.

## 7. Running experiments: host hygiene

- Keep the laptop on AC power with the Windows power mode set to **Best performance**. Close browsers, IDEs and other heavy apps, and leave WSL and Docker idle.
- Run experiments over SSH. Do not leave the VM console window in focus.
- The Windows hypervisor (used by WSL2 and Docker) stays on, so VirtualBox runs on Hyper-V. The harness measures an idle noise floor first, and that number goes into the paper.
- If the noise floor turns out too high, a team member may switch the hypervisor off for the golden run. This needs an admin PowerShell plus a reboot, and it breaks WSL2/Docker until reverted:
  - Off: `bcdedit /set hypervisorlaunchtype off`
  - Revert: `bcdedit /set hypervisorlaunchtype auto`

## 8. Rollback

If anything goes wrong (a kernel or scheduler experiment, a broken package), restore the snapshot:

```powershell
& $VBM snapshot fairexp restore clean-provisioned
```

schedmon restores the nice values, slices and cgroup weights it changed, and the harness checks this after every run.
