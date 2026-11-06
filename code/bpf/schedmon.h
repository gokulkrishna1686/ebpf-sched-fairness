/* SPDX-License-Identifier: (GPL-2.0 OR MIT) */
/*
 * Types shared between the schedmon eBPF programs and the user-space loader.
 * Include after the integer types are defined (vmlinux.h in BPF, <linux/types.h> in user space).
 */
#ifndef SCHEDMON_H
#define SCHEDMON_H

#define HIST_BUCKETS 27 /* log2(µs): bucket 0 = <1 µs, bucket k = [2^(k-1), 2^k) µs */
#define COMM_LEN 16

enum task_class {
	CLASS_OTHER = 0,
	CLASS_INTERACTIVE = 1,
	CLASS_BATCH = 2,
};

enum wait_kind {
	KIND_WAKEUP = 0,  /* became runnable through a wake-up */
	KIND_PREEMPT = 1, /* switched out while still runnable */
	KIND_NEW = 2,	  /* newly forked task */
};

enum stat_idx {
	STAT_EMITTED = 0,
	STAT_DROPPED = 1,
	STAT_MAX,
};

/*
 * Per-thread scheduling statistics, keyed by the kernel's global thread id (task->pid).
 * `tid`/`tgid` are the ids as seen from the thread's own PID namespace, i.e. what gettid()
 * returns and what setpriority()/sched_setattr() expect inside a container or a WSL distro.
 * Outside such namespaces tid == ktid.
 */
struct task_stats {
	__u64 start_ns;	   /* task->start_time; a mismatch means the tid was reused */
	__u64 runnable_ts; /* when the task last became runnable; 0 if not waiting */
	__u64 oncpu_ts;	   /* when the task last got a CPU; 0 if off-CPU */
	__u64 n_wakeups;
	__u64 n_wait;
	__u64 wait_sum_ns;
	__u64 wait_max_ns;
	__u64 run_ns;
	__u64 nvcsw;
	__u64 nivcsw;
	__u64 migrations;
	__u64 hist[HIST_BUCKETS];
	__u32 tid;  /* namespace-local thread id */
	__u32 tgid; /* namespace-local process id */
	__u32 ktid; /* kernel (initial-namespace) thread id: the map key */
	__u8 cls;
	__u8 runnable_kind;
	__u8 pad[2];
	char comm[COMM_LEN];
};

/* One completed run-queue wait, streamed through the ring buffer. */
struct wait_event {
	__u64 ts_ns;   /* when the task got the CPU */
	__u64 wait_ns;
	__u32 tid; /* namespace-local thread id */
	__u16 cpu;
	__u8 cls;
	__u8 kind;
};

#endif /* SCHEDMON_H */
