// SPDX-License-Identifier: MIT
/*
 * wlgen - reproducible mixed-workload generator for the fairness experiments.
 *
 *   wlgen calibrate              measure spin iterations per microsecond of CPU time
 *   wlgen interactive ...        N threads serving open-loop, seeded request arrivals
 *   wlgen batch ...              N threads running fixed-size CPU work units
 *
 * Work is expressed in calibrated spin iterations, not wall time, so a request that is
 * preempted takes longer to complete; this is exactly the delay we want to observe.
 * The whole arrival schedule is generated up front from the seed, so runs with the same
 * seed see the identical schedule regardless of policy (paired comparisons). A hash of
 * the schedule is written to the summary to verify this.
 *
 * See ../INTERFACES.md for the command line and output formats.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <inttypes.h>
#include <math.h>
#include <pthread.h>
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

#include "sched_attr.h"

#define NSEC_PER_SEC 1000000000ULL
#define NSEC_PER_USEC 1000ULL
#define MAX_WORK_US 50000.0	 /* clamp lognormal tails at 50 ms of work */
#define WIN_NS (100ULL * 1000000) /* batch progress is also recorded per 100 ms window */

enum role { ROLE_CALIBRATE, ROLE_INTERACTIVE, ROLE_BATCH };
enum arrival { ARR_POISSON, ARR_ONOFF, ARR_PERIODIC };
enum workdist { WD_FIXED, WD_LOGNORMAL };

static struct config {
	enum role role;
	/* interactive */
	int tasks;
	enum arrival arrival;
	double rate;
	double on_ms, off_ms;
	double period_us, jitter_us;
	double work_us;
	enum workdist work_dist;
	double work_sigma;
	/* batch */
	int threads;
	double unit_us;
	/* common */
	uint64_t seed;
	uint64_t start_at;
	double warmup_s, duration_s;
	const char *out;
	const char *cgroup;
	int nice, nice_set;
	uint64_t slice_ns;
	const char *cpus;
	double iters_per_us;
	int trials;
} cfg = {
	.tasks = 2, .arrival = ARR_POISSON, .rate = 100, .on_ms = 200, .off_ms = 800,
	.period_us = 16667, .jitter_us = 0, .work_us = 1000, .work_dist = WD_FIXED,
	.work_sigma = 0.5, .threads = 2, .unit_us = 1000, .seed = 1, .warmup_s = 5,
	.duration_s = 30, .trials = 7,
};

static volatile uint64_t sink; /* defeats dead-code elimination of spin() */
static pthread_barrier_t start_barrier;
static uint64_t t0, t_meas_start, t_meas_end;
static int calibrated_inline;

/* ---------------------------------------------------------------- time */

static inline uint64_t now_ns(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (uint64_t)ts.tv_sec * NSEC_PER_SEC + (uint64_t)ts.tv_nsec;
}

static inline uint64_t thread_cpu_ns(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_THREAD_CPUTIME_ID, &ts);
	return (uint64_t)ts.tv_sec * NSEC_PER_SEC + (uint64_t)ts.tv_nsec;
}

static void sleep_until(uint64_t t)
{
	struct timespec ts = { .tv_sec = (time_t)(t / NSEC_PER_SEC), .tv_nsec = (long)(t % NSEC_PER_SEC) };

	while (clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &ts, NULL) == EINTR)
		;
}

/* ---------------------------------------------------------------- work */

static inline uint64_t spin(uint64_t iters)
{
	uint64_t x = 0x9E3779B97F4A7C15ULL;

	for (uint64_t i = 0; i < iters; i++) {
		x ^= x << 13;
		x ^= x >> 7;
		x ^= x << 17;
		__asm__ volatile("" : "+r"(x));
	}
	return x;
}

static int cmp_double(const void *a, const void *b)
{
	double x = *(const double *)a, y = *(const double *)b;

	return (x > y) - (x < y);
}

/* Median spin rate over several trials, measured in thread CPU time (robust to preemption). */
static double calibrate(int trials, uint64_t iters, double *min_out, double *max_out)
{
	double rates[64];

	if (trials > 64)
		trials = 64;
	spin(iters / 4); /* warm up caches and frequency */
	for (int i = 0; i < trials; i++) {
		uint64_t c0 = thread_cpu_ns();

		sink ^= spin(iters);
		uint64_t c1 = thread_cpu_ns();
		rates[i] = (double)iters / ((double)(c1 - c0) / NSEC_PER_USEC);
	}
	qsort(rates, (size_t)trials, sizeof(rates[0]), cmp_double);
	if (min_out)
		*min_out = rates[0];
	if (max_out)
		*max_out = rates[trials - 1];
	return rates[trials / 2];
}

/* ---------------------------------------------------------------- rng */

static inline uint64_t splitmix64(uint64_t *s)
{
	uint64_t z = (*s += 0x9E3779B97F4A7C15ULL);

	z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ULL;
	z = (z ^ (z >> 27)) * 0x94D049BB133111EBULL;
	return z ^ (z >> 31);
}

static inline double urand(uint64_t *s)
{
	return (double)(splitmix64(s) >> 11) * 0x1.0p-53; /* [0, 1) */
}

static inline double expo(uint64_t *s, double mean)
{
	return -mean * log1p(-urand(s));
}

static inline double gauss(uint64_t *s)
{
	double u1 = urand(s), u2 = urand(s);

	if (u1 < 1e-300)
		u1 = 1e-300;
	return sqrt(-2.0 * log(u1)) * cos(2.0 * M_PI * u2);
}

static inline uint64_t fnv1a(uint64_t h, uint64_t v)
{
	for (int i = 0; i < 8; i++) {
		h ^= (v >> (8 * i)) & 0xff;
		h *= 0x100000001B3ULL;
	}
	return h;
}

/* ---------------------------------------------------------------- threads */

struct worker {
	int idx;
	pid_t tid;
	int setup_errno;
	int nice_rb;		/* read back after setup */
	uint64_t slice_rb;	/* read back after setup (0 if unknown) */
	/* interactive */
	uint64_t n;
	uint64_t *arr;		/* arrival offsets from t0 */
	uint64_t *iters;
	uint64_t *t_start, *t_end;
	/* batch */
	uint64_t unit_iters;
	uint64_t units, cpu_ns, wall_ns;
	uint32_t *win; /* units completed per WIN_NS window since t0 */
	uint64_t n_win;
	/* the kernel's own accounting at thread exit (/proc/thread-self/schedstat) */
	uint64_t ss_run_ns, ss_delay_ns, ss_slices;
};

static void read_schedstat(struct worker *w)
{
	unsigned long long run = 0, delay = 0, slices = 0;
	FILE *f = fopen("/proc/thread-self/schedstat", "r");

	if (!f)
		return;
	if (fscanf(f, "%llu %llu %llu", &run, &delay, &slices) == 3) {
		w->ss_run_ns = run;
		w->ss_delay_ns = delay;
		w->ss_slices = slices;
	}
	fclose(f);
}

static int parse_cpus(const char *list, cpu_set_t *set)
{
	char *dup = strdup(list), *save = NULL;

	CPU_ZERO(set);
	for (char *tok = strtok_r(dup, ",", &save); tok; tok = strtok_r(NULL, ",", &save)) {
		int a, b;

		if (sscanf(tok, "%d-%d", &a, &b) == 2) {
			for (int c = a; c <= b; c++)
				CPU_SET(c, set);
		} else if (sscanf(tok, "%d", &a) == 1) {
			CPU_SET(a, set);
		} else {
			free(dup);
			return -1;
		}
	}
	free(dup);
	return 0;
}

/* Per-thread knobs used by the static baselines (B1 nice, B3 slice) and optional affinity. */
static void thread_setup(struct worker *w)
{
	w->tid = gettid();
	if (cfg.cpus) {
		cpu_set_t set;

		if (parse_cpus(cfg.cpus, &set) == 0 && sched_setaffinity(0, sizeof(set), &set))
			w->setup_errno = errno;
	}
	if (cfg.slice_ns) {
		int nice = cfg.nice_set ? cfg.nice : getpriority(PRIO_PROCESS, (id_t)w->tid);

		if (fx_set_fair_params(w->tid, nice, cfg.slice_ns))
			w->setup_errno = errno;
	} else if (cfg.nice_set) {
		if (setpriority(PRIO_PROCESS, (id_t)w->tid, cfg.nice))
			w->setup_errno = errno;
	}
	if (fx_get_fair_params(w->tid, &w->nice_rb, &w->slice_rb))
		w->nice_rb = getpriority(PRIO_PROCESS, (id_t)w->tid);
}

static void *interactive_main(void *arg)
{
	struct worker *w = arg;

	thread_setup(w);
	pthread_barrier_wait(&start_barrier);
	for (uint64_t i = 0; i < w->n; i++) {
		uint64_t a = t0 + w->arr[i];

		if (now_ns() < a)
			sleep_until(a);
		w->t_start[i] = now_ns();
		sink ^= spin(w->iters[i]);
		w->t_end[i] = now_ns();
	}
	read_schedstat(w);
	return NULL;
}

static void *batch_main(void *arg)
{
	struct worker *w = arg;
	uint64_t cpu0 = 0, wall0 = 0;
	int in_window = 0;

	thread_setup(w);
	pthread_barrier_wait(&start_barrier);
	sleep_until(t0);
	for (;;) {
		uint64_t now = now_ns();

		if (now >= t_meas_end)
			break;
		if (!in_window && now >= t_meas_start) {
			in_window = 1;
			cpu0 = thread_cpu_ns();
			wall0 = now;
		}
		sink ^= spin(w->unit_iters);
		now = now_ns();
		if (in_window && now < t_meas_end)
			w->units++;
		if (now >= t0 && (now - t0) / WIN_NS < w->n_win)
			w->win[(now - t0) / WIN_NS]++;
	}
	if (in_window) {
		w->cpu_ns = thread_cpu_ns() - cpu0;
		w->wall_ns = now_ns() - wall0;
	}
	read_schedstat(w);
	return NULL;
}

/* ---------------------------------------------------------------- schedule */

static double draw_work_us(uint64_t *s)
{
	double w = cfg.work_us;

	if (cfg.work_dist == WD_LOGNORMAL)
		w = cfg.work_us * exp(cfg.work_sigma * gauss(s));
	if (w < 1.0)
		w = 1.0;
	if (w > MAX_WORK_US)
		w = MAX_WORK_US;
	return w;
}

static void push(struct worker *w, uint64_t *cap, uint64_t off, uint64_t iters)
{
	if (w->n == *cap) {
		*cap = *cap ? *cap * 2 : 1024;
		w->arr = realloc(w->arr, *cap * sizeof(uint64_t));
		w->iters = realloc(w->iters, *cap * sizeof(uint64_t));
		if (!w->arr || !w->iters) {
			perror("realloc");
			exit(1);
		}
	}
	w->arr[w->n] = off;
	w->iters[w->n] = iters;
	w->n++;
}

/* Arrival offsets (ns from t0) over [0, warmup + duration), deterministic in (seed, task). */
static void gen_schedule(struct worker *w)
{
	uint64_t s = cfg.seed * 0x100000001B3ULL + (uint64_t)w->idx + 1;
	double horizon = (cfg.warmup_s + cfg.duration_s) * 1e9;
	uint64_t cap = 0;
	double t;

	for (int i = 0; i < 4; i++)
		splitmix64(&s);

	switch (cfg.arrival) {
	case ARR_POISSON:
		for (t = expo(&s, 1e9 / cfg.rate); t < horizon; t += expo(&s, 1e9 / cfg.rate))
			push(w, &cap, (uint64_t)t, (uint64_t)llround(draw_work_us(&s) * cfg.iters_per_us));
		break;
	case ARR_ONOFF: {
		/* Markov-modulated Poisson: exponential ON/OFF periods, Poisson arrivals while ON. */
		double on_end = expo(&s, cfg.on_ms * 1e6);

		t = 0;
		while (t < horizon) {
			double gap = expo(&s, 1e9 / cfg.rate);

			if (t + gap <= on_end) {
				t += gap;
				if (t < horizon)
					push(w, &cap, (uint64_t)t,
					     (uint64_t)llround(draw_work_us(&s) * cfg.iters_per_us));
			} else {
				t = on_end + expo(&s, cfg.off_ms * 1e6);
				on_end = t + expo(&s, cfg.on_ms * 1e6);
			}
		}
		break;
	}
	case ARR_PERIODIC: {
		double period = cfg.period_us * 1e3, phase = urand(&s) * period;

		for (uint64_t k = 0;; k++) {
			double jit = (urand(&s) * 2.0 - 1.0) * cfg.jitter_us * 1e3;

			t = phase + (double)k * period + jit;
			if (t >= horizon)
				break;
			push(w, &cap, (uint64_t)(t < 0 ? 0 : t),
			     (uint64_t)llround(draw_work_us(&s) * cfg.iters_per_us));
		}
		break;
	}
	}
	w->t_start = calloc(w->n ? w->n : 1, sizeof(uint64_t));
	w->t_end = calloc(w->n ? w->n : 1, sizeof(uint64_t));
	if (!w->t_start || !w->t_end) {
		perror("calloc");
		exit(1);
	}
}

/* ---------------------------------------------------------------- output */

static FILE *open_out(const char *name)
{
	char path[4096];
	FILE *f;

	snprintf(path, sizeof(path), "%s/%s", cfg.out, name);
	f = fopen(path, "w");
	if (!f) {
		fprintf(stderr, "wlgen: cannot write %s: %s\n", path, strerror(errno));
		exit(1);
	}
	return f;
}

static const char *arrival_name(void)
{
	return cfg.arrival == ARR_POISSON ? "poisson" : cfg.arrival == ARR_ONOFF ? "onoff" : "periodic";
}

static void write_common_json(FILE *f, struct worker *ws, int n)
{
	fprintf(f, "  \"seed\": %" PRIu64 ",\n", cfg.seed);
	fprintf(f, "  \"iters_per_us\": %.6f,\n", cfg.iters_per_us);
	fprintf(f, "  \"calibrated_inline\": %s,\n", calibrated_inline ? "true" : "false");
	fprintf(f, "  \"t0_ns\": %" PRIu64 ",\n", t0);
	fprintf(f, "  \"t_meas_start_ns\": %" PRIu64 ",\n", t_meas_start);
	fprintf(f, "  \"t_meas_end_ns\": %" PRIu64 ",\n", t_meas_end);
	fprintf(f, "  \"warmup_s\": %.3f,\n  \"duration_s\": %.3f,\n", cfg.warmup_s, cfg.duration_s);
	fprintf(f, "  \"cgroup\": \"%s\",\n", cfg.cgroup ? cfg.cgroup : "");
	fprintf(f, "  \"nice_requested\": %s,\n", cfg.nice_set ? "true" : "false");
	fprintf(f, "  \"nice\": %d,\n", cfg.nice);
	fprintf(f, "  \"slice_ns_requested\": %" PRIu64 ",\n", cfg.slice_ns);
	fprintf(f, "  \"cpus\": \"%s\",\n", cfg.cpus ? cfg.cpus : "");
	fprintf(f, "  \"threads\": [\n");
	for (int i = 0; i < n; i++)
		fprintf(f, "    {\"idx\": %d, \"tid\": %d, \"nice_readback\": %d, \"slice_readback_ns\": %" PRIu64
			   ", \"setup_errno\": %d, \"schedstat_run_ns\": %" PRIu64 ", \"schedstat_delay_ns\": %" PRIu64
			   ", \"schedstat_slices\": %" PRIu64 "}%s\n",
			ws[i].idx, ws[i].tid, ws[i].nice_rb, ws[i].slice_rb, ws[i].setup_errno, ws[i].ss_run_ns,
			ws[i].ss_delay_ns, ws[i].ss_slices, i + 1 < n ? "," : "");
	fprintf(f, "  ],\n");
}

static void write_interactive(struct worker *ws, int n)
{
	FILE *f = open_out("requests.csv");
	uint64_t total = 0, measured = 0, late_start = 0, hash = 0xcbf29ce484222325ULL;

	fprintf(f, "task,tid,req,t_arrival_ns,t_start_ns,t_end_ns,work_iters,measured\n");
	for (int i = 0; i < n; i++) {
		struct worker *w = &ws[i];

		for (uint64_t r = 0; r < w->n; r++) {
			uint64_t a = t0 + w->arr[r];
			int m = a >= t_meas_start && a < t_meas_end;

			hash = fnv1a(fnv1a(hash, w->arr[r]), w->iters[r]);
			fprintf(f, "%d,%d,%" PRIu64 ",%" PRIu64 ",%" PRIu64 ",%" PRIu64 ",%" PRIu64 ",%d\n", w->idx,
				w->tid, r, a, w->t_start[r], w->t_end[r], w->iters[r], m);
			total++;
			measured += m;
			if (r == 0 && w->t_start[r] > a + 10 * NSEC_PER_SEC / 1000)
				late_start++;
		}
	}
	fclose(f);

	f = open_out("interactive_summary.json");
	fprintf(f, "{\n  \"role\": \"interactive\",\n");
	fprintf(f, "  \"tasks\": %d,\n  \"arrival\": \"%s\",\n  \"rate\": %.3f,\n", cfg.tasks, arrival_name(), cfg.rate);
	fprintf(f, "  \"on_ms\": %.3f,\n  \"off_ms\": %.3f,\n", cfg.on_ms, cfg.off_ms);
	fprintf(f, "  \"period_us\": %.3f,\n  \"jitter_us\": %.3f,\n", cfg.period_us, cfg.jitter_us);
	fprintf(f, "  \"work_us\": %.3f,\n  \"work_dist\": \"%s\",\n  \"work_sigma\": %.3f,\n", cfg.work_us,
		cfg.work_dist == WD_FIXED ? "fixed" : "lognormal", cfg.work_sigma);
	write_common_json(f, ws, n);
	fprintf(f, "  \"requests_total\": %" PRIu64 ",\n  \"requests_measured\": %" PRIu64 ",\n", total, measured);
	fprintf(f, "  \"late_first_request_tasks\": %" PRIu64 ",\n", late_start);
	fprintf(f, "  \"schedule_hash\": \"%016" PRIx64 "\"\n}\n", hash);
	fclose(f);
}

static void write_batch(struct worker *ws, int n)
{
	FILE *f = open_out("batch.csv");
	uint64_t units = 0;

	fprintf(f, "thread,tid,units,cpu_ns,wall_ns\n");
	for (int i = 0; i < n; i++) {
		fprintf(f, "%d,%d,%" PRIu64 ",%" PRIu64 ",%" PRIu64 "\n", ws[i].idx, ws[i].tid, ws[i].units,
			ws[i].cpu_ns, ws[i].wall_ns);
		units += ws[i].units;
	}
	fclose(f);

	/* Per-window progress, measured windows only (temporal fairness, starvation windows). */
	f = open_out("batch_ts.csv");
	fprintf(f, "thread,win,t_win_start_ns,units\n");
	for (int i = 0; i < n; i++)
		for (uint64_t k = 0; k < ws[i].n_win; k++) {
			uint64_t ws_ns = t0 + k * WIN_NS;

			if (ws_ns >= t_meas_start && ws_ns + WIN_NS <= t_meas_end)
				fprintf(f, "%d,%" PRIu64 ",%" PRIu64 ",%u\n", ws[i].idx, k, ws_ns, ws[i].win[k]);
		}
	fclose(f);

	f = open_out("batch_summary.json");
	fprintf(f, "{\n  \"role\": \"batch\",\n  \"threads_n\": %d,\n  \"unit_us\": %.3f,\n", cfg.threads, cfg.unit_us);
	write_common_json(f, ws, n);
	fprintf(f, "  \"units_total\": %" PRIu64 "\n}\n", units);
	fclose(f);
}

/* ---------------------------------------------------------------- main */

static int join_cgroup(const char *path)
{
	char file[4096];
	int fd;

	snprintf(file, sizeof(file), "%s/cgroup.procs", path);
	fd = open(file, O_WRONLY);
	if (fd < 0)
		return -1;
	/* Writing "0" moves the writing process (all its threads) into the cgroup. */
	if (write(fd, "0", 1) != 1) {
		close(fd);
		return -1;
	}
	close(fd);
	return 0;
}

static void usage(void)
{
	fprintf(stderr,
		"usage: wlgen calibrate [--trials N]\n"
		"       wlgen interactive --tasks N --arrival poisson|onoff|periodic [--rate R]\n"
		"             [--on-ms X --off-ms Y] [--period-us P --jitter-us J]\n"
		"             --work-us W [--work-dist fixed|lognormal --work-sigma S] COMMON\n"
		"       wlgen batch --threads N --unit-us U COMMON\n"
		"COMMON: --seed S --start-at T0_NS --warmup-s W --duration-s D --out DIR\n"
		"        [--cgroup PATH] [--nice N] [--slice-us U] [--cpus LIST] [--iters-per-us F]\n");
	exit(2);
}

static void parse_args(int argc, char **argv)
{
	enum {
		O_TASKS = 256, O_ARRIVAL, O_RATE, O_ON, O_OFF, O_PERIOD, O_JITTER, O_WORK, O_WDIST, O_WSIGMA,
		O_THREADS, O_UNIT, O_SEED, O_START, O_WARMUP, O_DURATION, O_OUT, O_CGROUP, O_NICE, O_SLICE,
		O_CPUS, O_IPU, O_TRIALS,
	};
	static const struct option opts[] = {
		{ "tasks", required_argument, 0, O_TASKS },	  { "arrival", required_argument, 0, O_ARRIVAL },
		{ "rate", required_argument, 0, O_RATE },	  { "on-ms", required_argument, 0, O_ON },
		{ "off-ms", required_argument, 0, O_OFF },	  { "period-us", required_argument, 0, O_PERIOD },
		{ "jitter-us", required_argument, 0, O_JITTER },  { "work-us", required_argument, 0, O_WORK },
		{ "work-dist", required_argument, 0, O_WDIST },	  { "work-sigma", required_argument, 0, O_WSIGMA },
		{ "threads", required_argument, 0, O_THREADS },	  { "unit-us", required_argument, 0, O_UNIT },
		{ "seed", required_argument, 0, O_SEED },	  { "start-at", required_argument, 0, O_START },
		{ "warmup-s", required_argument, 0, O_WARMUP },	  { "duration-s", required_argument, 0, O_DURATION },
		{ "out", required_argument, 0, O_OUT },		  { "cgroup", required_argument, 0, O_CGROUP },
		{ "nice", required_argument, 0, O_NICE },	  { "slice-us", required_argument, 0, O_SLICE },
		{ "cpus", required_argument, 0, O_CPUS },	  { "iters-per-us", required_argument, 0, O_IPU },
		{ "trials", required_argument, 0, O_TRIALS },	  { 0, 0, 0, 0 },
	};
	int c;

	if (argc < 2)
		usage();
	if (!strcmp(argv[1], "calibrate"))
		cfg.role = ROLE_CALIBRATE;
	else if (!strcmp(argv[1], "interactive"))
		cfg.role = ROLE_INTERACTIVE;
	else if (!strcmp(argv[1], "batch"))
		cfg.role = ROLE_BATCH;
	else
		usage();

	optind = 2;
	while ((c = getopt_long(argc, argv, "", opts, NULL)) != -1) {
		switch (c) {
		case O_TASKS: cfg.tasks = atoi(optarg); break;
		case O_ARRIVAL:
			if (!strcmp(optarg, "poisson")) cfg.arrival = ARR_POISSON;
			else if (!strcmp(optarg, "onoff")) cfg.arrival = ARR_ONOFF;
			else if (!strcmp(optarg, "periodic")) cfg.arrival = ARR_PERIODIC;
			else usage();
			break;
		case O_RATE: cfg.rate = atof(optarg); break;
		case O_ON: cfg.on_ms = atof(optarg); break;
		case O_OFF: cfg.off_ms = atof(optarg); break;
		case O_PERIOD: cfg.period_us = atof(optarg); break;
		case O_JITTER: cfg.jitter_us = atof(optarg); break;
		case O_WORK: cfg.work_us = atof(optarg); break;
		case O_WDIST:
			if (!strcmp(optarg, "fixed")) cfg.work_dist = WD_FIXED;
			else if (!strcmp(optarg, "lognormal")) cfg.work_dist = WD_LOGNORMAL;
			else usage();
			break;
		case O_WSIGMA: cfg.work_sigma = atof(optarg); break;
		case O_THREADS: cfg.threads = atoi(optarg); break;
		case O_UNIT: cfg.unit_us = atof(optarg); break;
		case O_SEED: cfg.seed = strtoull(optarg, NULL, 10); break;
		case O_START: cfg.start_at = strtoull(optarg, NULL, 10); break;
		case O_WARMUP: cfg.warmup_s = atof(optarg); break;
		case O_DURATION: cfg.duration_s = atof(optarg); break;
		case O_OUT: cfg.out = optarg; break;
		case O_CGROUP: cfg.cgroup = optarg; break;
		case O_NICE: cfg.nice = atoi(optarg); cfg.nice_set = 1; break;
		case O_SLICE: cfg.slice_ns = (uint64_t)(atof(optarg) * NSEC_PER_USEC); break;
		case O_CPUS: cfg.cpus = optarg; break;
		case O_IPU: cfg.iters_per_us = atof(optarg); break;
		case O_TRIALS: cfg.trials = atoi(optarg); break;
		default: usage();
		}
	}
	if (cfg.role != ROLE_CALIBRATE) {
		if (!cfg.out) {
			fprintf(stderr, "wlgen: --out is required\n");
			usage();
		}
		if ((cfg.role == ROLE_INTERACTIVE && (cfg.tasks <= 0 || cfg.rate <= 0 || cfg.work_us <= 0)) ||
		    (cfg.role == ROLE_BATCH && (cfg.threads <= 0 || cfg.unit_us <= 0)) || cfg.duration_s <= 0 ||
		    cfg.warmup_s < 0) {
			fprintf(stderr, "wlgen: invalid parameters\n");
			usage();
		}
	}
}

int main(int argc, char **argv)
{
	struct worker *ws;
	pthread_t *th;
	int n;

	parse_args(argc, argv);

	if (cfg.role == ROLE_CALIBRATE) {
		double lo, hi, med = calibrate(cfg.trials, 20000000ULL, &lo, &hi);

		printf("{\"iters_per_us\": %.6f, \"min\": %.6f, \"max\": %.6f, \"trials\": %d}\n", med, lo, hi,
		       cfg.trials);
		return 0;
	}

	if (cfg.iters_per_us <= 0) {
		cfg.iters_per_us = calibrate(3, 5000000ULL, NULL, NULL);
		calibrated_inline = 1;
	}
	if (cfg.cgroup && join_cgroup(cfg.cgroup)) {
		fprintf(stderr, "wlgen: cannot join cgroup %s: %s\n", cfg.cgroup, strerror(errno));
		return 1;
	}

	n = cfg.role == ROLE_INTERACTIVE ? cfg.tasks : cfg.threads;
	ws = calloc((size_t)n, sizeof(*ws));
	th = calloc((size_t)n, sizeof(*th));
	if (!ws || !th) {
		perror("calloc");
		return 1;
	}
	for (int i = 0; i < n; i++) {
		ws[i].idx = i;
		if (cfg.role == ROLE_INTERACTIVE) {
			gen_schedule(&ws[i]);
		} else {
			ws[i].unit_iters = (uint64_t)llround(cfg.unit_us * cfg.iters_per_us);
			ws[i].n_win = (uint64_t)ceil((cfg.warmup_s + cfg.duration_s) * 1e9 / (double)WIN_NS);
			ws[i].win = calloc(ws[i].n_win, sizeof(uint32_t));
			if (!ws[i].win) {
				perror("calloc");
				return 1;
			}
		}
	}

	t0 = cfg.start_at ? cfg.start_at : now_ns() + 200 * 1000000ULL;
	t_meas_start = t0 + (uint64_t)(cfg.warmup_s * 1e9);
	t_meas_end = t_meas_start + (uint64_t)(cfg.duration_s * 1e9);

	pthread_barrier_init(&start_barrier, NULL, (unsigned)n);
	for (int i = 0; i < n; i++) {
		if (pthread_create(&th[i], NULL, cfg.role == ROLE_INTERACTIVE ? interactive_main : batch_main,
				   &ws[i])) {
			perror("pthread_create");
			return 1;
		}
	}
	for (int i = 0; i < n; i++)
		pthread_join(th[i], NULL);

	if (cfg.role == ROLE_INTERACTIVE)
		write_interactive(ws, n);
	else
		write_batch(ws, n);
	return 0;
}
