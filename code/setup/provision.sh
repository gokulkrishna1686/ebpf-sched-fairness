#!/usr/bin/env bash
# Provision an Ubuntu 26.04 machine for the fairness experiments.
#
#   sudo ./provision.sh          # WSL2 / dev box: packages + checks only
#   sudo ./provision.sh --vm     # disposable experiment VM: also pins the kernel,
#                                # enables passwordless sudo and disables noisy timers
set -euo pipefail

VM=0
[[ "${1:-}" == "--vm" ]] && VM=1

if [[ $EUID -ne 0 ]]; then
	echo "provision.sh: run with sudo" >&2
	exit 1
fi

PKGS=(
	build-essential make pkg-config git zstd jq
	clang libbpf-dev libelf-dev zlib1g-dev bpftool
	bpftrace trace-cmd stress-ng
	python3 python3-numpy python3-pandas python3-scipy python3-matplotlib
)

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends "${PKGS[@]}"

# Exact package versions, for the reproducibility table.
dpkg-query -W -f='${Package}=${Version}\n' "${PKGS[@]}" > /var/log/fairexp-packages.txt
echo "package versions written to /var/log/fairexp-packages.txt"

if [[ $VM -eq 1 ]]; then
	# Pin the tested kernel: a silent kernel upgrade would invalidate comparisons.
	KREL="$(uname -r)"
	for p in "linux-image-$KREL" "linux-modules-$KREL" linux-image-generic linux-generic linux-headers-generic; do
		dpkg -s "$p" >/dev/null 2>&1 && apt-mark hold "$p"
	done

	# The harness and schedmon need root; this VM is disposable and only reachable from 127.0.0.1.
	if [[ -n "${SUDO_USER:-}" ]]; then
		echo "$SUDO_USER ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/90-fairexp
		chmod 440 /etc/sudoers.d/90-fairexp
	fi

	# Background jobs that would wake up mid-run and perturb measurements.
	for u in unattended-upgrades.service apt-daily.timer apt-daily-upgrade.timer \
		 man-db.timer fstrim.timer motd-news.timer snapd.service; do
		systemctl disable --now "$u" >/dev/null 2>&1 || true
	done
fi

echo "--- checks"
fail=0
check() { if eval "$2"; then echo "ok    $1"; else echo "FAIL  $1"; fail=1; fi; }

kconfig() {
	if [[ -r /boot/config-$(uname -r) ]]; then cat "/boot/config-$(uname -r)";
	elif [[ -r /proc/config.gz ]]; then zcat /proc/config.gz; fi
}
CFG="$(kconfig || true)"

check "kernel $(uname -r) >= 6.12 (EEVDF custom slices)" \
	'[[ "$(printf "%s\n6.12\n" "$(uname -r | cut -d- -f1)" | sort -V | head -1)" == "6.12" ]]'
check "BTF at /sys/kernel/btf/vmlinux" '[[ -r /sys/kernel/btf/vmlinux ]]'
check "CONFIG_BPF_SYSCALL=y"   'grep -q "^CONFIG_BPF_SYSCALL=y" <<<"$CFG"'
check "CONFIG_DEBUG_INFO_BTF=y" 'grep -q "^CONFIG_DEBUG_INFO_BTF=y" <<<"$CFG"'
check "CONFIG_SCHEDSTATS=y"    'grep -q "^CONFIG_SCHEDSTATS=y" <<<"$CFG"'
check "cgroup v2 with cpu+cpuset" 'grep -qw cpu /sys/fs/cgroup/cgroup.controllers && grep -qw cpuset /sys/fs/cgroup/cgroup.controllers'
check "bpftool runs" 'bpftool version >/dev/null 2>&1'
check "clang runs"   'clang --version >/dev/null 2>&1'

if grep -q "^CONFIG_SCHED_CLASS_EXT=y" <<<"$CFG"; then
	echo "info  sched_ext available: optional scx baseline possible"
else
	echo "info  sched_ext NOT available: tracing-only track (expected)"
fi

exit $fail
