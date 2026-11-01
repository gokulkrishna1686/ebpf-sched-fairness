// SPDX-License-Identifier: GPL-2.0
/*
 * schedmon: per-thread run-queue wait telemetry for the tasks in a set of cgroups.
 *
 * A thread's run-queue wait starts when it becomes runnable (sched_wakeup / sched_wakeup_new,
 * or sched_switch-out while still runnable, i.e. preemption) and ends when sched_switch
 * selects it as `next`. Only threads whose cgroup id is in `tracked_cgroups` are recorded;
 * all others cost one hash miss per event.
 */
#include "vmlinux.h"
#include <bpf/bpf_core_read.h>
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>

#include "schedmon.h"

char LICENSE[] SEC("license") = "GPL";

const volatile bool emit_events = false;

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__uint(max_entries, 16);
	__type(key, u64);  /* cgroup v2 id (= inode number of the cgroup directory) */
	__type(value, u8); /* enum task_class */
} tracked_cgroups SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__uint(max_entries, 8192);
	__type(key, u32); /* tid */
	__type(value, struct task_stats);
} tasks SEC(".maps");

/* Zeroed template for new entries; struct task_stats is too large for the BPF stack. */
struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(max_entries, 1);
	__type(key, u32);
	__type(value, struct task_stats);
} scratch SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_RINGBUF);
	__uint(max_entries, 16 << 20);
} events SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(max_entries, STAT_MAX);
	__type(key, u32);
	__type(value, u64);
} stats SEC(".maps");

static __always_inline void stat_inc(u32 idx)
{
	u64 *v = bpf_map_lookup_elem(&stats, &idx);

	if (v)
		(*v)++;
}

static __always_inline u32 log2_u64(u64 v)
{
	u32 r, shift;

	shift = (v > 0xFFFFFFFFULL) << 5;
	v >>= shift;
	r = shift;
	shift = (v > 0xFFFF) << 4;
	v >>= shift;
	r |= shift;
	shift = (v > 0xFF) << 3;
	v >>= shift;
	r |= shift;
	shift = (v > 0xF) << 2;
	v >>= shift;
	r |= shift;
	shift = (v > 0x3) << 1;
	v >>= shift;
	r |= shift;
	r |= (u32)(v >> 1);
	return r;
}

static __always_inline u32 hist_bucket(u64 wait_ns)
{
	u64 us = wait_ns / 1000;
	u32 b;

	if (!us)
		return 0;
	b = log2_u64(us) + 1;
	if (b >= HIST_BUCKETS)
		b = HIST_BUCKETS - 1;
	return b;
}

static __always_inline u8 task_class(struct task_struct *p)
{
	u64 cgid = BPF_CORE_READ(p, cgroups, dfl_cgrp, kn, id);
	u8 *cls = bpf_map_lookup_elem(&tracked_cgroups, &cgid);

	return cls ? *cls : CLASS_OTHER;
}

/* Stats entry of a tracked thread, created on first sight; NULL for untracked threads. */
static __always_inline struct task_stats *get_stats(struct task_struct *p)
{
	u32 tid = BPF_CORE_READ(p, pid);
	u64 start = BPF_CORE_READ(p, start_time);
	struct task_stats *st, *init;
	u32 zero = 0;
	u8 cls;

	if (!tid)
		return NULL; /* idle task */
	st = bpf_map_lookup_elem(&tasks, &tid);
	if (st && st->start_ns == start)
		return st;

	cls = task_class(p);
	if (cls == CLASS_OTHER)
		return NULL;
	init = bpf_map_lookup_elem(&scratch, &zero);
	if (!init)
		return NULL;
	__builtin_memset(init, 0, sizeof(*init));
	init->start_ns = start;
	init->tid = tid;
	init->tgid = BPF_CORE_READ(p, tgid);
	init->cls = cls;
	bpf_probe_read_kernel_str(init->comm, sizeof(init->comm), p->comm);
	bpf_map_update_elem(&tasks, &tid, init, BPF_ANY);
	return bpf_map_lookup_elem(&tasks, &tid);
}

static __always_inline void mark_runnable(struct task_struct *p, u8 kind)
{
	struct task_stats *st = get_stats(p);

	if (!st)
		return;
	st->n_wakeups++;
	/* Keep the earliest timestamp: a spurious wake-up must not shorten an ongoing wait. */
	if (!st->runnable_ts) {
		st->runnable_ts = bpf_ktime_get_ns();
		st->runnable_kind = kind;
	}
}

SEC("tp_btf/sched_wakeup")
int BPF_PROG(on_wakeup, struct task_struct *p)
{
	mark_runnable(p, KIND_WAKEUP);
	return 0;
}

SEC("tp_btf/sched_wakeup_new")
int BPF_PROG(on_wakeup_new, struct task_struct *p)
{
	mark_runnable(p, KIND_NEW);
	return 0;
}

SEC("tp_btf/sched_switch")
int BPF_PROG(on_switch, bool preempt, struct task_struct *prev, struct task_struct *next,
	     unsigned int prev_state)
{
	u64 now = bpf_ktime_get_ns();
	struct task_stats *st;

	st = get_stats(prev);
	if (st) {
		if (st->oncpu_ts) {
			st->run_ns += now - st->oncpu_ts;
			st->oncpu_ts = 0;
		}
		if (preempt || prev_state == 0 /* TASK_RUNNING */) {
			/* Still runnable: it waits in the run queue from now on. */
			st->nivcsw++;
			st->runnable_ts = now;
			st->runnable_kind = KIND_PREEMPT;
		} else {
			st->nvcsw++;
			st->runnable_ts = 0;
		}
	}

	st = get_stats(next);
	if (st) {
		if (st->runnable_ts) {
			u64 wait = now - st->runnable_ts;
			u32 b = hist_bucket(wait);

			st->n_wait++;
			st->wait_sum_ns += wait;
			if (wait > st->wait_max_ns)
				st->wait_max_ns = wait;
			if (b < HIST_BUCKETS)
				st->hist[b]++;

			if (emit_events) {
				struct wait_event *e = bpf_ringbuf_reserve(&events, sizeof(*e), 0);

				if (e) {
					e->ts_ns = now;
					e->wait_ns = wait;
					e->tid = st->tid;
					e->cpu = (u16)bpf_get_smp_processor_id();
					e->cls = st->cls;
					e->kind = st->runnable_kind;
					/* User space drains the buffer every tick; skip per-event wake-ups. */
					bpf_ringbuf_submit(e, BPF_RB_NO_WAKEUP);
					stat_inc(STAT_EMITTED);
				} else {
					stat_inc(STAT_DROPPED);
				}
			}
			st->runnable_ts = 0;
		}
		st->oncpu_ts = now;
	}
	return 0;
}

SEC("tp_btf/sched_migrate_task")
int BPF_PROG(on_migrate, struct task_struct *p, int dest_cpu)
{
	u32 tid = BPF_CORE_READ(p, pid);
	struct task_stats *st = bpf_map_lookup_elem(&tasks, &tid);

	if (st && st->start_ns == BPF_CORE_READ(p, start_time))
		st->migrations++;
	return 0;
}
