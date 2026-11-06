# Build wlgen (workload generator) and schedmon (eBPF collector + WaitBoost policy).
#
#   make                 build everything into build/
#   make check-kernel    print the kernel features the experiments depend on
#   make selftest        sudo ./build/schedmon --selftest
#   make test            all automatic tests (T2-T6, T4) in one go, ~5 min, PASS/FAIL summary (needs root)
#   make quick           2-minute end-to-end smoke test: runs -> metrics -> figures (needs root)
#   make pilot           ~1 h dress rehearsal of the whole study: every config, 4 s runs (needs root)
#   make golden          ~9 h real study: tune+freeze, all evaluation batches, figures, tables (needs root)
#                        resume an interrupted one with: make golden TS=<timestamp>

#   make validate-trace  schedmon vs raw ftrace capture for the hand check (needs root)
#   make run CONFIG=main run one experiment config (needs root)
#   make analyze BATCH=<batch id under results/raw>
#   make fake            synthetic data to exercise the analysis without root
#   make clean
#
# BPF objects are compiled with clang against a vmlinux.h generated from the running
# kernel's BTF (CO-RE), so no kernel headers are needed.

CC       ?= gcc
CLANG    ?= clang
BPFTOOL  ?= bpftool
PYTHON   ?= python3
BUILD    := build
ARCH     := $(shell uname -m | sed -e 's/x86_64/x86/' -e 's/aarch64/arm64/')

# Extra include/library paths (e.g. a locally unpacked libbpf) can be passed in.
LIBBPF_CFLAGS ?= $(shell pkg-config --cflags libbpf 2>/dev/null)
LIBBPF_LIBS   ?= $(shell pkg-config --libs libbpf 2>/dev/null || echo -lbpf)
EXTRA_LIBS    ?= -lelf -lz

CFLAGS   ?= -O2 -g -Wall -Wextra
CFLAGS   += -Icommon -I$(BUILD) -Ibpf
BPF_CFLAGS := -O2 -g -Wall -target bpf -D__TARGET_ARCH_$(ARCH) -I$(BUILD) -Ibpf $(LIBBPF_CFLAGS)

QUICK_ID := quick_$(shell date +%Y%m%d-%H%M%S)

.PHONY: all clean check-kernel selftest test quick pilot golden validate-trace run analyze fake

all: $(BUILD)/wlgen $(BUILD)/schedmon

$(BUILD):
	mkdir -p $@

$(BUILD)/wlgen: workload/wlgen.c common/sched_attr.h | $(BUILD)
	$(CC) $(CFLAGS) -pthread $< -lm -o $@

$(BUILD)/vmlinux.h: | $(BUILD)
	$(BPFTOOL) btf dump file /sys/kernel/btf/vmlinux format c > $@

$(BUILD)/schedmon.bpf.o: bpf/schedmon.bpf.c bpf/schedmon.h $(BUILD)/vmlinux.h
	$(CLANG) $(BPF_CFLAGS) -c $< -o $@

$(BUILD)/schedmon.skel.h: $(BUILD)/schedmon.bpf.o
	$(BPFTOOL) gen skeleton $< name schedmon_bpf > $@

$(BUILD)/schedmon: bpf/schedmon.c bpf/schedmon.h common/sched_attr.h $(BUILD)/schedmon.skel.h
	$(CC) $(CFLAGS) $(LIBBPF_CFLAGS) -pthread $< $(LIBBPF_LIBS) $(EXTRA_LIBS) -lm -o $@

check-kernel:
	@echo "kernel: $$(uname -r)"
	@(cat /boot/config-$$(uname -r) 2>/dev/null || zcat /proc/config.gz 2>/dev/null) | \
		grep -E '^(# )?CONFIG_(BPF_SYSCALL|DEBUG_INFO_BTF|SCHEDSTATS|SCHED_CLASS_EXT|HZ|PREEMPT_DYNAMIC)[= ]' || true
	@test -r /sys/kernel/btf/vmlinux && echo "BTF: present" || echo "BTF: MISSING"
	@echo "cgroup controllers: $$(cat /sys/fs/cgroup/cgroup.controllers)"

selftest: $(BUILD)/schedmon
	sudo ./$(BUILD)/schedmon --selftest

quick: all
	-sudo $(PYTHON) harness/run_matrix.py harness/configs/quick.json --batch-id $(QUICK_ID)
	sudo chown -R $$(id -u):$$(id -g) results
	@test ! -f results/raw/$(QUICK_ID)/failed_runs.csv || (echo "FAILED RUNS:"; cat results/raw/$(QUICK_ID)/failed_runs.csv)
	$(MAKE) analyze BATCH=$(QUICK_ID)
	$(PYTHON) analysis/plots.py --main $(QUICK_ID) --out results/quick_figures/$(QUICK_ID)
	@echo "figures: results/quick_figures/$(QUICK_ID)"

test: all
	sudo ./tests/run_all.sh

pilot: all
	sudo ./tests/pilot.sh

golden: all
	sudo ./tests/golden.sh $(TS)

validate-trace: all
	sudo $(PYTHON) harness/validate_trace.py
	sudo chown -R $$(id -u):$$(id -g) results

run: all
	@test -n "$(CONFIG)" || (echo "usage: make run CONFIG=<name in harness/configs>"; exit 2)
	sudo $(PYTHON) harness/run_matrix.py harness/configs/$(CONFIG).json --pack; \
		status=$$?; sudo chown -R $$(id -u):$$(id -g) results; exit $$status

analyze:
	@test -n "$(BATCH)" || (echo "usage: make analyze BATCH=<batch id>"; exit 2)
	$(PYTHON) analysis/metrics.py results/raw/$(BATCH)
	$(PYTHON) analysis/stats.py results/summary/$(BATCH)_runs.csv

fake:
	$(PYTHON) analysis/fake_data.py

clean:
	rm -rf $(BUILD)
