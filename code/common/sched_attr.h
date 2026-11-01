/* SPDX-License-Identifier: MIT */
/*
 * Thin wrappers around sched_setattr(2)/sched_getattr(2) for fair-class tasks.
 *
 * Since Linux 6.12, a SCHED_OTHER/SCHED_BATCH task may request a custom EEVDF slice through
 * sched_attr.sched_runtime (clamped by the kernel to [0.1 ms, 100 ms]); a zero value restores
 * the default base slice. A shorter slice gives the task an earlier virtual deadline, so it is
 * picked sooner after waking, without changing its weight (long-run CPU share).
 */
#ifndef FAIREXP_SCHED_ATTR_H
#define FAIREXP_SCHED_ATTR_H

#include <errno.h>
#include <sched.h>
#include <stdint.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <unistd.h>

#ifndef SCHED_BATCH
#define SCHED_BATCH 3
#endif

/* Mirrors the kernel's struct sched_attr (SCHED_ATTR_SIZE_VER1). */
struct fx_sched_attr {
	uint32_t size;
	uint32_t sched_policy;
	uint64_t sched_flags;
	int32_t sched_nice;
	uint32_t sched_priority;
	uint64_t sched_runtime;
	uint64_t sched_deadline;
	uint64_t sched_period;
	uint32_t sched_util_min;
	uint32_t sched_util_max;
};

static inline int fx_sched_getattr(pid_t tid, struct fx_sched_attr *a)
{
	memset(a, 0, sizeof(*a));
	return (int)syscall(SYS_sched_getattr, tid, a, sizeof(*a), 0);
}

static inline int fx_sched_setattr(pid_t tid, struct fx_sched_attr *a)
{
	a->size = sizeof(*a);
	return (int)syscall(SYS_sched_setattr, tid, a, 0);
}

/*
 * Set nice and EEVDF slice request of a fair-class thread in one call.
 * slice_ns == 0 means "kernel default slice". Fails with EINVAL for non-fair policies.
 */
static inline int fx_set_fair_params(pid_t tid, int nice, uint64_t slice_ns)
{
	struct fx_sched_attr a;

	if (fx_sched_getattr(tid, &a))
		return -1;
	if (a.sched_policy != SCHED_OTHER && a.sched_policy != SCHED_BATCH) {
		errno = EINVAL;
		return -1;
	}
	a.sched_flags = 0;
	a.sched_nice = nice;
	a.sched_runtime = slice_ns;
	a.sched_deadline = 0;
	a.sched_period = 0;
	return fx_sched_setattr(tid, &a);
}

/* Current nice and slice of a thread; returns -1 on error. */
static inline int fx_get_fair_params(pid_t tid, int *nice, uint64_t *slice_ns)
{
	struct fx_sched_attr a;

	if (fx_sched_getattr(tid, &a))
		return -1;
	if (nice)
		*nice = a.sched_nice;
	if (slice_ns)
		*slice_ns = a.sched_runtime;
	return 0;
}

#endif /* FAIREXP_SCHED_ATTR_H */
