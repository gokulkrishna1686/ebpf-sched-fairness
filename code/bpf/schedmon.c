// SPDX-License-Identifier: MIT
/*
 * schedmon - loads the run-queue telemetry programs, streams wait events, dumps per-thread
 * statistics and (optionally) runs the WaitBoost policy.
 *
 * WaitBoost (user space, eBPF-informed). Every --interval-ms it reads each traced thread's
 * cumulative wait and run time from the BPF map and maintains, per thread:
 *   ewma_wait  - EWMA of the mean run-queue wait per scheduling event (plus any ongoing wait)
 *   ewma_sleep - EWMA of the fraction of time spent sleeping (neither running nor waiting)
 *   ewma_cpu   - EWMA of the fraction of time spent running
 * A thread is latency-like when ewma_sleep >= --min-sleep and ewma_cpu <= --cpu-cap (a
 * ULE-style interactivity test; no application cooperation needed). Rules, in order:
 *   guard  - a thread runnable but not running for >= --guard-ms is raised to max level
 *            whatever its class (starvation guard);
 *   revoke - a boosted thread whose ewma_cpu exceeds --cpu-cap loses its boost (bounded);
 *   boost  - a latency-like thread with ewma_wait > --theta-us gains one level per tick,
 *            up to --max-level;
 *   decay  - otherwise a boosted thread loses one level every --decay-ms (aging).
 * Actuators map a level L to a kernel knob:
 *   nice     - nice = original - L * --nice-step                         (changes weight)
 *   slice    - EEVDF slice request = max(0.1 ms, base_slice >> L)       (changes deadline)
 *   cgweight - per-cgroup variant: the interactive class's aggregate wait drives
 *              cpu.weight = original << L of the interactive cgroup.
 * On exit every changed knob is restored.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <inttypes.h>
#include <math.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#include <linux/types.h>
#include <bpf/bpf.h>
#include <bpf/libbpf.h>

#include "sched_attr.h"
#include "schedmon.h"
#include "schedmon.skel.h"

#define NSEC_PER_MSEC 1000000ULL
#define NSEC_PER_USEC 1000ULL
#define MAX_TRACKED 4096
#define ALPHA_CLASS 0.2 /* smoothing of sleep/cpu fractions (~50 ms horizon at 10 ms ticks) */
#define MIN_SLICE_NS 100000ULL

enum policy { POL_NONE, POL_WAITBOOST };
enum actuator { ACT_NICE, ACT_SLICE, ACT_CGWEIGHT };

static struct opts {
	const char *cg_interactive, *cg_batch, *out;
	double duration_s;
	bool events, selftest, verbose;
	int cpu;
	enum policy policy;
	enum actuator actuator;
	double interval_ms, theta_us, alpha, min_sleep, cpu_cap, decay_ms, guard_ms;
	int max_level, nice_step;
	bool no_aging, no_feedback;
} o = {
	.cpu = -1, .policy = POL_NONE, .actuator = ACT_NICE, .interval_ms = 10, .theta_us = 500,
	.alpha = 0.3, .min_sleep = 0.5, .cpu_cap = 0.5, .decay_ms = 50, .guard_ms = 100,
	.max_level = 3, .nice_step = 3,
};

/* Policy-side view of one thread. */
struct pstate {
	__u32 tid;
	__u8 cls;
	__u64 start_ns;
	__u64 prev_t, prev_wait, prev_wait_done, prev_n, prev_run;
	bool have_frac, have_wait;
	double ewma_wait, ewma_sleep, ewma_cpu;
	int level;
	__u64 last_change, guard_until;
	bool touched;
	bool gone; /* the thread has exited: never act on it again */
	int orig_nice;
	uint64_t orig_slice;
};

static struct pstate ps[MAX_TRACKED];
static int n_ps;

/* Class-level state for the per-cgroup (cgweight) variant. */
static struct {
	int level;
	bool have_wait;
	double ewma_wait;
	__u64 last_change;
	long orig_weight;
	bool touched;
} cgstate;

static volatile sig_atomic_t exiting;
static FILE *events_f, *plog_f;
static uint64_t base_slice_ns = 2800000ULL;
static __u64 ticks, actuator_errors, restore_errors;
static __u64 n_actions[5]; /* boost, decay, guard, revoke, restore */
static const char *action_names[] = { "boost", "decay", "guard", "revoke", "restore" };
enum { A_BOOST, A_DECAY, A_GUARD, A_REVOKE, A_RESTORE };

static inline __u64 now_ns(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (__u64)ts.tv_sec * 1000000000ULL + (__u64)ts.tv_nsec;
}

static void sleep_until(__u64 t)
{
	struct timespec ts = { .tv_sec = (time_t)(t / 1000000000ULL), .tv_nsec = (long)(t % 1000000000ULL) };

	while (clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &ts, NULL) == EINTR && !exiting)
		;
}

static void on_signal(int sig)
{
	(void)sig;
	exiting = 1;
}

static int libbpf_print(enum libbpf_print_level level, const char *fmt, va_list args)
{
	if (level == LIBBPF_DEBUG && !o.verbose)
		return 0;
	return vfprintf(stderr, fmt, args);
}

/* ---------------------------------------------------------------- helpers */

static FILE *open_out(const char *name)
{
	char path[4096];
	FILE *f;

	snprintf(path, sizeof(path), "%s/%s", o.out, name);
	f = fopen(path, "w");
	if (!f) {
		fprintf(stderr, "schedmon: cannot write %s: %s\n", path, strerror(errno));
		exit(1);
	}
	return f;
}

static int add_cgroup(int map_fd, const char *path, __u8 cls)
{
	struct stat sb;
	__u64 id;

	if (stat(path, &sb)) {
		fprintf(stderr, "schedmon: cgroup %s: %s\n", path, strerror(errno));
		return -1;
	}
	id = (__u64)sb.st_ino; /* cgroup v2 id == kernfs node id == directory inode number */
	return bpf_map_update_elem(map_fd, &id, &cls, BPF_ANY);
}

static long read_long_file(const char *path)
{
	FILE *f = fopen(path, "r");
	long v = -1;

	if (f) {
		if (fscanf(f, "%ld", &v) != 1)
			v = -1;
		fclose(f);
	}
	return v;
}

static int write_long_file(const char *path, long v)
{
	FILE *f = fopen(path, "w");
	int r;

	if (!f)
		return -1;
	r = fprintf(f, "%ld\n", v) < 0 ? -1 : 0;
	if (fclose(f))
		r = -1;
	return r;
}

static __u64 percpu_sum(int fd, __u32 idx)
{
	int ncpu = libbpf_num_possible_cpus();
	__u64 sum = 0;
	__u64 *vals;

	if (ncpu <= 0)
		return 0;
	vals = calloc((size_t)ncpu, sizeof(*vals));
	if (vals && !bpf_map_lookup_elem(fd, &idx, vals))
		for (int i = 0; i < ncpu; i++)
			sum += vals[i];
	free(vals);
	return sum;
}

/* Calls fn for every entry of the tasks map. */
static void for_each_task(int fd, void (*fn)(const struct task_stats *, void *), void *arg)
{
	__u32 key, next, *prev = NULL;
	struct task_stats st;

	while (bpf_map_get_next_key(fd, prev, &next) == 0) {
		if (bpf_map_lookup_elem(fd, &next, &st) == 0)
			fn(&st, arg);
		key = next;
		prev = &key;
	}
}

static int on_event(void *ctx, void *data, size_t len)
{
	const struct wait_event *e = data;

	(void)ctx;
	if (len < sizeof(*e) || !events_f)
		return 0;
	fprintf(events_f, "%llu,%u,%u,%u,%llu,%u\n", (unsigned long long)e->ts_ns, e->tid, e->cls, e->cpu,
		(unsigned long long)e->wait_ns, e->kind);
	return 0;
}

/* ---------------------------------------------------------------- policy */

static struct pstate *find_ps(__u32 tid, __u64 start, __u8 cls, bool *fresh)
{
	*fresh = false;
	for (int i = 0; i < n_ps; i++) {
		if (ps[i].tid != tid)
			continue;
		if (ps[i].start_ns != start) { /* tid reused */
			memset(&ps[i], 0, sizeof(ps[i]));
			ps[i].tid = tid;
			ps[i].start_ns = start;
			ps[i].cls = cls;
			*fresh = true;
		}
		return &ps[i];
	}
	if (n_ps == MAX_TRACKED)
		return NULL;
	memset(&ps[n_ps], 0, sizeof(ps[n_ps]));
	ps[n_ps].tid = tid;
	ps[n_ps].start_ns = start;
	ps[n_ps].cls = cls;
	*fresh = true;
	return &ps[n_ps++];
}

static void log_action(__u64 now, __u32 tid, int cls, int action, int from, int to, double wait, double sleep,
		       double cpu)
{
	n_actions[action]++;
	if (plog_f)
		fprintf(plog_f, "%llu,%u,%d,%s,%d,%d,%.0f,%.4f,%.4f\n", (unsigned long long)now, tid, cls,
			action_names[action], from, to, wait, sleep, cpu);
}

static int apply_level(struct pstate *p)
{
	int r = 0;

	if (!p->touched) {
		if (fx_get_fair_params((pid_t)p->tid, &p->orig_nice, &p->orig_slice)) {
			if (errno == ESRCH)
				p->gone = true; /* exited between the map read and now: not an error */
			else
				actuator_errors++;
			return -1;
		}
		if (p->orig_slice == base_slice_ns)
			p->orig_slice = 0; /* default slice: restore with "no custom slice" */
		p->touched = true;
	}
	if (o.actuator == ACT_NICE) {
		int nice = p->orig_nice - p->level * o.nice_step;

		if (nice < -20)
			nice = -20;
		r = setpriority(PRIO_PROCESS, (id_t)p->tid, nice);
	} else if (o.actuator == ACT_SLICE) {
		__u64 slice = p->orig_slice;

		if (p->level > 0) {
			slice = base_slice_ns >> p->level;
			if (slice < MIN_SLICE_NS)
				slice = MIN_SLICE_NS;
		}
		r = fx_set_fair_params((pid_t)p->tid, p->orig_nice, slice);
	}
	if (r && errno == ESRCH)
		p->gone = true;
	else if (r)
		actuator_errors++;
	return r;
}

static void decide(struct pstate *p, __u64 pend_wait, __u64 now)
{
	__u64 interval = (__u64)(o.interval_ms * NSEC_PER_MSEC), decay = (__u64)(o.decay_ms * NSEC_PER_MSEC);
	__u64 guard = (__u64)(o.guard_ms * NSEC_PER_MSEC);
	double theta = o.theta_us * NSEC_PER_USEC;
	int old = p->level, action = -1;
	bool eligible;

	if (p->gone)
		return;
	if (pend_wait >= guard && p->level < o.max_level) {
		p->level = o.max_level;
		p->guard_until = now + guard;
		action = A_GUARD;
	} else if (o.no_feedback) {
		/* Ablation: static class-based boost with the same bound, no wait feedback. */
		int target = p->cls == CLASS_INTERACTIVE ? o.max_level : 0;

		if (now >= p->guard_until && target != p->level) {
			p->level = target;
			action = target > old ? A_BOOST : A_DECAY;
		}
	} else {
		eligible = p->have_frac && p->ewma_sleep >= o.min_sleep && p->ewma_cpu <= o.cpu_cap;
		if (p->level > 0 && p->ewma_cpu > o.cpu_cap && now >= p->guard_until) {
			p->level = 0;
			action = A_REVOKE;
		} else if (eligible && p->have_wait && p->ewma_wait > theta) {
			if (p->level < o.max_level && now - p->last_change >= interval) {
				p->level++;
				action = A_BOOST;
			}
		} else if (p->level > 0 && !o.no_aging && now >= p->guard_until && now - p->last_change >= decay) {
			p->level--;
			action = A_DECAY;
		}
	}
	if (action < 0)
		return;
	p->last_change = now;
	if (apply_level(p) && p->gone) {
		p->level = old; /* nothing was changed on a thread that no longer exists */
		return;
	}
	log_action(now, p->tid, p->cls, action, old, p->level, p->ewma_wait, p->ewma_sleep, p->ewma_cpu);
}

/* Per-tick accumulators for the per-cgroup variant. */
struct tick_ctx {
	__u64 now;
	__u64 inter_dwait, inter_dn, inter_pend_max, batch_pend_max;
};

static inline double ewma(double old, double x, double a)
{
	return (1.0 - a) * old + a * x;
}

static void tick_task(const struct task_stats *st, void *arg)
{
	struct tick_ctx *c = arg;
	__u64 now = c->now, pend_wait = 0, pend_run = 0, wait, run, dt, dwait, drun, dn, dwait_done;
	struct pstate *p;
	bool fresh;

	if (st->runnable_ts && !st->oncpu_ts && now > st->runnable_ts)
		pend_wait = now - st->runnable_ts;
	if (st->oncpu_ts && now > st->oncpu_ts)
		pend_run = now - st->oncpu_ts;
	wait = st->wait_sum_ns + pend_wait;
	run = st->run_ns + pend_run;

	p = find_ps(st->tid, st->start_ns, st->cls, &fresh);
	if (!p)
		return;
	if (fresh) {
		p->prev_t = now;
		p->prev_wait = wait;
		p->prev_wait_done = st->wait_sum_ns;
		p->prev_n = st->n_wait;
		p->prev_run = run;
		return;
	}
	dt = now - p->prev_t;
	if (dt < 1000)
		return;
	dwait = wait > p->prev_wait ? wait - p->prev_wait : 0;
	drun = run > p->prev_run ? run - p->prev_run : 0;
	dn = st->n_wait - p->prev_n;
	dwait_done = st->wait_sum_ns - p->prev_wait_done;

	{
		double wf = (double)dwait / (double)dt, cf = (double)drun / (double)dt, sf = 1.0 - wf - cf;

		if (sf < 0)
			sf = 0;
		if (cf > 1)
			cf = 1;
		if (!p->have_frac) {
			p->ewma_sleep = sf;
			p->ewma_cpu = cf;
			p->have_frac = true;
		} else {
			p->ewma_sleep = ewma(p->ewma_sleep, sf, ALPHA_CLASS);
			p->ewma_cpu = ewma(p->ewma_cpu, cf, ALPHA_CLASS);
		}
	}
	{
		double m = -1;

		if (dn > 0) {
			m = (double)dwait_done / (double)dn;
			if ((double)pend_wait > m)
				m = (double)pend_wait;
		} else if ((double)pend_wait > o.theta_us * NSEC_PER_USEC) {
			m = (double)pend_wait;
		}
		if (m >= 0) {
			p->ewma_wait = p->have_wait ? ewma(p->ewma_wait, m, o.alpha) : m;
			p->have_wait = true;
		}
	}
	p->prev_t = now;
	p->prev_wait = wait;
	p->prev_wait_done = st->wait_sum_ns;
	p->prev_n = st->n_wait;
	p->prev_run = run;

	if (st->cls == CLASS_INTERACTIVE) {
		c->inter_dwait += dwait_done;
		c->inter_dn += dn;
		if (pend_wait > c->inter_pend_max)
			c->inter_pend_max = pend_wait;
	} else if (st->cls == CLASS_BATCH && pend_wait > c->batch_pend_max) {
		c->batch_pend_max = pend_wait;
	}

	if (o.policy == POL_WAITBOOST && o.actuator != ACT_CGWEIGHT)
		decide(p, pend_wait, now);
}

static void apply_cgweight(void)
{
	char path[4096];
	long w;

	snprintf(path, sizeof(path), "%s/cpu.weight", o.cg_interactive);
	if (!cgstate.touched) {
		cgstate.orig_weight = read_long_file(path);
		if (cgstate.orig_weight <= 0) {
			actuator_errors++;
			return;
		}
		cgstate.touched = true;
	}
	w = cgstate.orig_weight << cgstate.level;
	if (w > 10000)
		w = 10000;
	if (write_long_file(path, w))
		actuator_errors++;
}

static void decide_cgroup(const struct tick_ctx *c)
{
	__u64 interval = (__u64)(o.interval_ms * NSEC_PER_MSEC), decay = (__u64)(o.decay_ms * NSEC_PER_MSEC);
	__u64 guard = (__u64)(o.guard_ms * NSEC_PER_MSEC);
	double theta = o.theta_us * NSEC_PER_USEC;
	int old = cgstate.level, action = -1;

	if (c->inter_dn > 0 || (double)c->inter_pend_max > theta) {
		double m = c->inter_dn ? (double)c->inter_dwait / (double)c->inter_dn : 0;

		if ((double)c->inter_pend_max > m)
			m = (double)c->inter_pend_max;
		cgstate.ewma_wait = cgstate.have_wait ? ewma(cgstate.ewma_wait, m, o.alpha) : m;
		cgstate.have_wait = true;
	}
	if (c->batch_pend_max >= guard && cgstate.level > 0) {
		cgstate.level = 0; /* class boost is starving batch work: drop it */
		action = A_GUARD;
	} else if (o.no_feedback) {
		if (cgstate.level != o.max_level) {
			cgstate.level = o.max_level;
			action = A_BOOST;
		}
	} else if (cgstate.have_wait && cgstate.ewma_wait > theta) {
		if (cgstate.level < o.max_level && c->now - cgstate.last_change >= interval) {
			cgstate.level++;
			action = A_BOOST;
		}
	} else if (cgstate.level > 0 && !o.no_aging && c->now - cgstate.last_change >= decay) {
		cgstate.level--;
		action = A_DECAY;
	}
	if (action < 0)
		return;
	cgstate.last_change = c->now;
	apply_cgweight();
	log_action(c->now, 0, CLASS_INTERACTIVE, action, old, cgstate.level, cgstate.ewma_wait, -1, -1);
}

static void policy_tick(int tasks_fd)
{
	struct tick_ctx c = { .now = now_ns() };

	for_each_task(tasks_fd, tick_task, &c);
	if (o.policy == POL_WAITBOOST && o.actuator == ACT_CGWEIGHT)
		decide_cgroup(&c);
	ticks++;
}

static void restore_all(void)
{
	__u64 now = now_ns();

	for (int i = 0; i < n_ps; i++) {
		struct pstate *p = &ps[i];
		int r;

		if (!p->touched)
			continue;
		if (o.actuator == ACT_NICE)
			r = setpriority(PRIO_PROCESS, (id_t)p->tid, p->orig_nice);
		else
			r = fx_set_fair_params((pid_t)p->tid, p->orig_nice, p->orig_slice);
		if (r && errno != ESRCH)
			restore_errors++;
		if (p->level)
			log_action(now, p->tid, p->cls, A_RESTORE, p->level, 0, p->ewma_wait, p->ewma_sleep,
				   p->ewma_cpu);
		p->level = 0;
	}
	if (cgstate.touched) {
		char path[4096];

		snprintf(path, sizeof(path), "%s/cpu.weight", o.cg_interactive);
		if (write_long_file(path, cgstate.orig_weight))
			restore_errors++;
		if (cgstate.level)
			log_action(now, 0, CLASS_INTERACTIVE, A_RESTORE, cgstate.level, 0, cgstate.ewma_wait, -1, -1);
		cgstate.level = 0;
	}
}

/* ---------------------------------------------------------------- output */

static void dump_task(const struct task_stats *st, void *arg)
{
	FILE *f = arg;
	char comm[COMM_LEN + 1];

	memcpy(comm, st->comm, COMM_LEN);
	comm[COMM_LEN] = 0;
	for (char *c = comm; *c; c++)
		if (*c == ',' || *c == '"' || *c == '\n')
			*c = '_';
	fprintf(f, "%u,%u,%u,%s,%u,%llu,%llu,%llu,%llu,%llu,%llu,%llu,%llu", st->tid, st->ktid, st->tgid, comm, st->cls,
		(unsigned long long)st->n_wakeups, (unsigned long long)st->n_wait,
		(unsigned long long)st->wait_sum_ns, (unsigned long long)st->wait_max_ns,
		(unsigned long long)st->run_ns, (unsigned long long)st->nvcsw, (unsigned long long)st->nivcsw,
		(unsigned long long)st->migrations);
	for (int i = 0; i < HIST_BUCKETS; i++)
		fprintf(f, ",%llu", (unsigned long long)st->hist[i]);
	fputc('\n', f);
}

static void count_task(const struct task_stats *st, void *arg)
{
	(void)st;
	(*(int *)arg)++;
}

static void write_tasks_csv(int fd)
{
	FILE *f = open_out("tasks.csv");

	fprintf(f, "tid,ktid,tgid,comm,class,n_wakeups,n_wait,wait_sum_ns,wait_max_ns,run_ns,nvcsw,nivcsw,migrations");
	for (int i = 0; i < HIST_BUCKETS; i++)
		fprintf(f, ",h%02d", i);
	fputc('\n', f);
	for_each_task(fd, dump_task, f);
	fclose(f);
}

static const char *policy_name(void)
{
	return o.policy == POL_NONE ? "none" : "waitboost";
}

static const char *actuator_name(void)
{
	return o.actuator == ACT_NICE ? "nice" : o.actuator == ACT_SLICE ? "slice" : "cgweight";
}

static void write_stats_json(struct schedmon_bpf *skel, __u64 t_start, __u64 t_end)
{
	FILE *f = open_out("schedmon_stats.json");
	int stats_fd = bpf_map__fd(skel->maps.stats), tracked = 0;
	struct bpf_program *prog;
	bool first = true;

	for_each_task(bpf_map__fd(skel->maps.tasks), count_task, &tracked);
	fprintf(f, "{\n");
	fprintf(f, "  \"events_enabled\": %s,\n", o.events ? "true" : "false");
	fprintf(f, "  \"events_emitted\": %llu,\n", (unsigned long long)percpu_sum(stats_fd, STAT_EMITTED));
	fprintf(f, "  \"events_dropped\": %llu,\n", (unsigned long long)percpu_sum(stats_fd, STAT_DROPPED));
	fprintf(f, "  \"tracked_tasks\": %d,\n", tracked);
	fprintf(f, "  \"t_start_ns\": %llu,\n  \"t_end_ns\": %llu,\n  \"duration_ns\": %llu,\n",
		(unsigned long long)t_start, (unsigned long long)t_end, (unsigned long long)(t_end - t_start));
	fprintf(f, "  \"ticks\": %llu,\n", (unsigned long long)ticks);
	fprintf(f, "  \"base_slice_ns\": %llu,\n", (unsigned long long)base_slice_ns);
	fprintf(f, "  \"actuator_errors\": %llu,\n  \"restore_errors\": %llu,\n", (unsigned long long)actuator_errors,
		(unsigned long long)restore_errors);
	fprintf(f, "  \"actions\": {");
	for (int i = 0; i < 5; i++)
		fprintf(f, "%s\"%s\": %llu", i ? ", " : "", action_names[i], (unsigned long long)n_actions[i]);
	fprintf(f, "},\n");
	fprintf(f, "  \"policy\": {\"name\": \"%s\", \"actuator\": \"%s\", \"interval_ms\": %g, \"theta_us\": %g, "
		   "\"alpha\": %g, \"min_sleep\": %g, \"cpu_cap\": %g, \"max_level\": %d, \"decay_ms\": %g, "
		   "\"guard_ms\": %g, \"nice_step\": %d, \"no_aging\": %s, \"no_feedback\": %s},\n",
		policy_name(), actuator_name(), o.interval_ms, o.theta_us, o.alpha, o.min_sleep, o.cpu_cap,
		o.max_level, o.decay_ms, o.guard_ms, o.nice_step, o.no_aging ? "true" : "false",
		o.no_feedback ? "true" : "false");
	fprintf(f, "  \"bpf_progs\": [");
	bpf_object__for_each_program(prog, skel->obj) {
		struct bpf_prog_info info;
		__u32 len = sizeof(info);

		memset(&info, 0, sizeof(info));
		if (bpf_prog_get_info_by_fd(bpf_program__fd(prog), &info, &len))
			continue;
		fprintf(f, "%s\n    {\"name\": \"%s\", \"run_cnt\": %llu, \"run_time_ns\": %llu}", first ? "" : ",",
			bpf_program__name(prog), (unsigned long long)info.run_cnt,
			(unsigned long long)info.run_time_ns);
		first = false;
	}
	fprintf(f, "\n  ]\n}\n");
	fclose(f);
}

/* ---------------------------------------------------------------- selftest */

static volatile int spin_stop;
static __u64 spin_delay_at_exit; /* the spinner's own schedstat run_delay, read just before it exits */

static void pin_to(int cpu)
{
	cpu_set_t set;

	CPU_ZERO(&set);
	CPU_SET(cpu, &set);
	sched_setaffinity(0, sizeof(set), &set);
}

static __u64 schedstat_run_delay(pid_t tid)
{
	char path[128];
	unsigned long long on = 0, delay = 0;
	FILE *f;

	snprintf(path, sizeof(path), "/proc/self/task/%d/schedstat", tid);
	f = fopen(path, "r");
	if (!f)
		return 0;
	if (fscanf(f, "%llu %llu", &on, &delay) != 2)
		delay = 0;
	fclose(f);
	return delay;
}

static void *spinner(void *arg)
{
	volatile __u64 x = 0;

	pin_to(*(int *)arg);
	while (!spin_stop)
		x++;
	spin_delay_at_exit = schedstat_run_delay(gettid());
	return NULL;
}

/* Stats entry of a thread by its namespace-local tid (the map is keyed by kernel tid). */
static bool find_task(int fd, pid_t tid, struct task_stats *out)
{
	__u32 key, next, *prev = NULL;

	while (bpf_map_get_next_key(fd, prev, &next) == 0) {
		if (bpf_map_lookup_elem(fd, &next, out) == 0 && (pid_t)out->tid == tid)
			return true;
		key = next;
		prev = &key;
	}
	memset(out, 0, sizeof(*out));
	return false;
}

/*
 * schedstat stamps a wait's start and end at scheduler entry (rq_clock, updated at the top of
 * __schedule); schedmon stamps them at the sched_switch tracepoint, after the next task has been
 * picked. Call that gap delta. A wait that ENDS with a switch-in (the sleeper after a wake-up) is
 * longer in eBPF by delta; a wait that STARTS with a switch-out (the preempted spinner) is shorter
 * by delta. So both rows must imply the same small positive per-wait offset. Exact per-wait
 * agreement with the same tracepoints is checked against raw ftrace in T4.
 */
#define DELTA_MIN_US (-2.0)
#define DELTA_MAX_US 50.0

static bool print_row(const char *name, const struct task_stats *st, __u64 waits, __u64 ebpf_ns, __u64 ref_ns,
		      int sign)
{
	double delta_us = waits ? sign * ((double)ebpf_ns - (double)ref_ns) / (double)waits / 1e3 : 0.0;
	bool ok = waits && delta_us >= DELTA_MIN_US && delta_us <= DELTA_MAX_US;

	printf("  tid %-7u %-8s wakeups=%-5llu waits=%-5llu mean_wait=%7.1fus max_wait=%7.1fus | "
	       "eBPF %.2f ms vs schedstat %.2f ms -> offset %.1f us/wait %s\n",
	       st->tid, name, (unsigned long long)st->n_wakeups, (unsigned long long)waits,
	       waits ? (double)ebpf_ns / (double)waits / 1e3 : 0.0, st->wait_max_ns / 1e3, ebpf_ns / 1e6,
	       ref_ns / 1e6, delta_us, ok ? "OK" : "MISMATCH");
	return ok;
}

static int selftest(struct schedmon_bpf *skel)
{
	char line[4096], path[4200];
	struct task_stats base, st;
	struct ring_buffer *rb;
	__u64 delay0, ebpf, ref, waits;
	pid_t main_tid, spin_tid = -1;
	bool waits_seen = true, all_ok = true;
	pthread_t th;
	FILE *f;
	int cpu = sched_getcpu(), tasks_fd = bpf_map__fd(skel->maps.tasks);

	/* Track our own cgroup. */
	f = fopen("/proc/self/cgroup", "r");
	path[0] = 0;
	while (f && fgets(line, sizeof(line), f)) {
		if (!strncmp(line, "0::", 3)) {
			line[strcspn(line, "\n")] = 0;
			snprintf(path, sizeof(path), "/sys/fs/cgroup%s", line + 3);
		}
	}
	if (f)
		fclose(f);
	if (!path[0] || add_cgroup(bpf_map__fd(skel->maps.tracked_cgroups), path, CLASS_INTERACTIVE)) {
		fprintf(stderr, "selftest: cannot determine own cgroup\n");
		return 1;
	}
	if (schedmon_bpf__attach(skel)) {
		fprintf(stderr, "selftest: attach failed: %s\n", strerror(errno));
		return 1;
	}
	rb = ring_buffer__new(bpf_map__fd(skel->maps.events), on_event, NULL, NULL);

	pin_to(cpu);
	main_tid = gettid();
	usleep(20000); /* let the move to `cpu` finish before taking the baselines */
	/* Baselines of both sources at the same instant, so they cover the same window. */
	delay0 = schedstat_run_delay(main_tid);
	find_task(tasks_fd, main_tid, &base);
	pthread_create(&th, NULL, spinner, &cpu);
	usleep(20000);
	for (int i = 0; i < 200; i++) {
		struct timespec ts = { 0, 2 * 1000 * 1000 };

		nanosleep(&ts, NULL);
	}
	spin_stop = 1;
	pthread_join(th, NULL);
	ref = schedstat_run_delay(main_tid) - delay0;
	if (rb)
		ring_buffer__consume(rb);

	printf("schedmon selftest: tracking cgroup %s, pinned to CPU %d\n", path, cpu);
	printf("  events emitted=%llu dropped=%llu\n",
	       (unsigned long long)percpu_sum(bpf_map__fd(skel->maps.stats), STAT_EMITTED),
	       (unsigned long long)percpu_sum(bpf_map__fd(skel->maps.stats), STAT_DROPPED));

	/* Sleeper (main thread): deltas over [baseline, end]. */
	find_task(tasks_fd, main_tid, &st);
	ebpf = st.wait_sum_ns - base.wait_sum_ns;
	waits = st.n_wait - base.n_wait;
	all_ok &= print_row("sleeper", &st, waits, ebpf, ref, +1); /* waits end at switch-in */
	waits_seen &= waits >= 150;

	/* Spinner: whole life, against the run_delay it read itself just before exiting. */
	{
		__u32 key, next, *prev = NULL;

		while (bpf_map_get_next_key(tasks_fd, prev, &next) == 0) {
			if (!bpf_map_lookup_elem(tasks_fd, &next, &st) && st.tgid == (__u32)getpid() &&
			    (pid_t)st.tid != main_tid)
				spin_tid = (pid_t)st.tid;
			key = next;
			prev = &key;
		}
	}
	if (spin_tid > 0 && find_task(tasks_fd, spin_tid, &st)) {
		/* waits start at switch-out (preemption) */
		all_ok &= print_row("spinner", &st, st.n_wait, st.wait_sum_ns, spin_delay_at_exit, -1);
		waits_seen &= st.n_wait >= 150;
	} else {
		printf("  spinner not found in the map\n");
		waits_seen = false;
	}
	ring_buffer__free(rb);

	if (!waits_seen)
		printf("selftest FAIL (too few waits recorded)\n");
	else if (!all_ok)
		printf("selftest FAIL (eBPF vs schedstat offset outside %.0f..%.0f us per wait)\n", DELTA_MIN_US,
		       DELTA_MAX_US);
	else
		printf("selftest PASS\n");
	return waits_seen && all_ok ? 0 : 1;
}

/* ---------------------------------------------------------------- main */

static void usage(void)
{
	fprintf(stderr,
		"usage: schedmon --cg-interactive PATH --cg-batch PATH --out DIR [--duration-s D] [--events]\n"
		"                [--policy none|waitboost] [--actuator nice|slice|cgweight] [--interval-ms 10]\n"
		"                [--theta-us 500] [--alpha 0.3] [--min-sleep 0.5] [--cpu-cap 0.5]\n"
		"                [--max-level 3] [--decay-ms 50] [--guard-ms 100] [--nice-step 3]\n"
		"                [--no-aging] [--no-feedback] [--cpu N] [--verbose]\n"
		"       schedmon --selftest\n");
	exit(2);
}

static void parse_args(int argc, char **argv)
{
	enum {
		O_CGI = 256, O_CGB, O_OUT, O_DUR, O_EVENTS, O_POLICY, O_ACT, O_INTERVAL, O_THETA, O_ALPHA,
		O_MINSLEEP, O_CPUCAP, O_MAXLEVEL, O_DECAY, O_GUARD, O_NICESTEP, O_NOAGING, O_NOFEEDBACK,
		O_SELFTEST, O_CPU, O_VERBOSE,
	};
	static const struct option opts[] = {
		{ "cg-interactive", required_argument, 0, O_CGI }, { "cg-batch", required_argument, 0, O_CGB },
		{ "out", required_argument, 0, O_OUT },		   { "duration-s", required_argument, 0, O_DUR },
		{ "events", no_argument, 0, O_EVENTS },		   { "policy", required_argument, 0, O_POLICY },
		{ "actuator", required_argument, 0, O_ACT },	   { "interval-ms", required_argument, 0, O_INTERVAL },
		{ "theta-us", required_argument, 0, O_THETA },	   { "alpha", required_argument, 0, O_ALPHA },
		{ "min-sleep", required_argument, 0, O_MINSLEEP }, { "cpu-cap", required_argument, 0, O_CPUCAP },
		{ "max-level", required_argument, 0, O_MAXLEVEL }, { "decay-ms", required_argument, 0, O_DECAY },
		{ "guard-ms", required_argument, 0, O_GUARD },	   { "nice-step", required_argument, 0, O_NICESTEP },
		{ "no-aging", no_argument, 0, O_NOAGING },	   { "no-feedback", no_argument, 0, O_NOFEEDBACK },
		{ "selftest", no_argument, 0, O_SELFTEST },	   { "cpu", required_argument, 0, O_CPU },
		{ "verbose", no_argument, 0, O_VERBOSE },	   { 0, 0, 0, 0 },
	};
	int c;

	while ((c = getopt_long(argc, argv, "", opts, NULL)) != -1) {
		switch (c) {
		case O_CGI: o.cg_interactive = optarg; break;
		case O_CGB: o.cg_batch = optarg; break;
		case O_OUT: o.out = optarg; break;
		case O_DUR: o.duration_s = atof(optarg); break;
		case O_EVENTS: o.events = true; break;
		case O_POLICY:
			if (!strcmp(optarg, "none")) o.policy = POL_NONE;
			else if (!strcmp(optarg, "waitboost")) o.policy = POL_WAITBOOST;
			else usage();
			break;
		case O_ACT:
			if (!strcmp(optarg, "nice")) o.actuator = ACT_NICE;
			else if (!strcmp(optarg, "slice")) o.actuator = ACT_SLICE;
			else if (!strcmp(optarg, "cgweight")) o.actuator = ACT_CGWEIGHT;
			else usage();
			break;
		case O_INTERVAL: o.interval_ms = atof(optarg); break;
		case O_THETA: o.theta_us = atof(optarg); break;
		case O_ALPHA: o.alpha = atof(optarg); break;
		case O_MINSLEEP: o.min_sleep = atof(optarg); break;
		case O_CPUCAP: o.cpu_cap = atof(optarg); break;
		case O_MAXLEVEL: o.max_level = atoi(optarg); break;
		case O_DECAY: o.decay_ms = atof(optarg); break;
		case O_GUARD: o.guard_ms = atof(optarg); break;
		case O_NICESTEP: o.nice_step = atoi(optarg); break;
		case O_NOAGING: o.no_aging = true; break;
		case O_NOFEEDBACK: o.no_feedback = true; break;
		case O_SELFTEST: o.selftest = true; break;
		case O_CPU: o.cpu = atoi(optarg); break;
		case O_VERBOSE: o.verbose = true; break;
		default: usage();
		}
	}
	if (o.selftest)
		return;
	if (!o.cg_interactive || !o.cg_batch || !o.out)
		usage();
	if (o.interval_ms <= 0 || o.alpha <= 0 || o.alpha > 1 || o.max_level < 0 || o.max_level > 10)
		usage();
}

int main(int argc, char **argv)
{
	struct schedmon_bpf *skel;
	struct ring_buffer *rb = NULL;
	__u64 t_start, t_end, deadline = 0, next_tick, interval;
	int tasks_fd, err = 0;
	int own_nice;

	parse_args(argc, argv);
	libbpf_set_print(libbpf_print);

	skel = schedmon_bpf__open();
	if (!skel) {
		fprintf(stderr, "schedmon: failed to open BPF skeleton\n");
		return 1;
	}
	skel->rodata->emit_events = o.events || o.selftest;
	if (schedmon_bpf__load(skel)) {
		fprintf(stderr, "schedmon: failed to load BPF programs (root? BTF?)\n");
		schedmon_bpf__destroy(skel);
		return 1;
	}
	if (o.selftest) {
		err = selftest(skel);
		schedmon_bpf__destroy(skel);
		return err;
	}

	if (o.cpu >= 0)
		pin_to(o.cpu);
	if (fx_get_fair_params(0, &own_nice, &base_slice_ns) || base_slice_ns == 0)
		base_slice_ns = 2800000ULL; /* our own (default) slice equals the base slice */
	if (mkdir(o.out, 0755) && errno != EEXIST) {
		fprintf(stderr, "schedmon: cannot create %s: %s\n", o.out, strerror(errno));
		return 1;
	}
	if (add_cgroup(bpf_map__fd(skel->maps.tracked_cgroups), o.cg_interactive, CLASS_INTERACTIVE) ||
	    add_cgroup(bpf_map__fd(skel->maps.tracked_cgroups), o.cg_batch, CLASS_BATCH)) {
		schedmon_bpf__destroy(skel);
		return 1;
	}

	if (o.events) {
		static char buf[1 << 20];

		events_f = open_out("events.csv");
		setvbuf(events_f, buf, _IOFBF, sizeof(buf));
		fprintf(events_f, "ts_ns,tid,class,cpu,wait_ns,kind\n");
		rb = ring_buffer__new(bpf_map__fd(skel->maps.events), on_event, NULL, NULL);
		if (!rb) {
			fprintf(stderr, "schedmon: ring buffer setup failed\n");
			return 1;
		}
	}
	if (o.policy == POL_WAITBOOST) {
		plog_f = open_out("policy_log.csv");
		fprintf(plog_f, "t_ns,tid,class,action,level_from,level_to,wait_ewma_ns,sleep_frac,cpu_frac\n");
	}

	signal(SIGINT, on_signal);
	signal(SIGTERM, on_signal);
	if (schedmon_bpf__attach(skel)) {
		fprintf(stderr, "schedmon: attach failed: %s\n", strerror(errno));
		schedmon_bpf__destroy(skel);
		return 1;
	}

	tasks_fd = bpf_map__fd(skel->maps.tasks);
	interval = (__u64)(o.interval_ms * NSEC_PER_MSEC);
	t_start = now_ns();
	if (o.duration_s > 0)
		deadline = t_start + (__u64)(o.duration_s * 1e9);
	next_tick = t_start + interval;
	fprintf(stderr, "schedmon: attached (policy=%s actuator=%s events=%d base_slice=%lluns)\n", policy_name(),
		actuator_name(), o.events, (unsigned long long)base_slice_ns);
	/* Ready marker for the harness. */
	fprintf(stdout, "READY\n");
	fflush(stdout);

	while (!exiting && (!deadline || now_ns() < deadline)) {
		sleep_until(next_tick);
		next_tick += interval;
		if (rb)
			ring_buffer__consume(rb);
		policy_tick(tasks_fd);
	}
	t_end = now_ns();

	if (o.policy == POL_WAITBOOST)
		restore_all();
	if (rb) {
		ring_buffer__consume(rb);
		ring_buffer__free(rb);
	}
	if (events_f)
		fclose(events_f);
	if (plog_f)
		fclose(plog_f);
	write_tasks_csv(tasks_fd);
	write_stats_json(skel, t_start, t_end);
	schedmon_bpf__destroy(skel);
	fprintf(stderr, "schedmon: done (%llu ticks, %llu actuator errors, %llu restore errors)\n",
		(unsigned long long)ticks, (unsigned long long)actuator_errors, (unsigned long long)restore_errors);
	return restore_errors ? 3 : 0;
}
