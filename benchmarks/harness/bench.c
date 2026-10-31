/* Linux benchmark driver for the third before/after comparison campaign.
 *
 * The same source is compiled against each library revision. The library has
 * no OpenSSL or pthread dependency; this optional tool uses POSIX threads,
 * Linux /proc interfaces and OpenSSL libcrypto for untimed SHA-256 digests.
 *
 * Every timed region excludes thread creation, warm-up, address generation,
 * hashing, /proc reads and formatting. See README.md for the protocol. */
#define _GNU_SOURCE
#include "maxminddb.h"

#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <limits.h>
#include <malloc.h>
#include <netinet/in.h>
#include <openssl/evp.h>
#include <pthread.h>
#include <sched.h>
#include <setjmp.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define MAX_WORKERS 64
#define MAX_PATH_COMPONENTS 6
#define RESERVOIR_SIZE (UINT32_C(1) << 20)
#define MAX_VISITED_NODES UINT64_C(100000000)
#define TIMER_CALIBRATION_PAIRS 100000
#define PRIVATE_PREPARED_FLAG (UINT32_C(1) << 31)

#if defined(__x86_64__) || defined(__i386__)
    #define cpu_relax() __builtin_ia32_pause()
#elif defined(__aarch64__)
    #define cpu_relax() __asm__ volatile("yield")
#else
    #define cpu_relax() ((void)0)
#endif

/* Allocation counting is compiled only into separate instrumented binaries
 * linked with -Wl,--wrap=malloc,--wrap=calloc,--wrap=realloc,--wrap=free.
 * Those binaries are never used for timing. Counters are thread-local and only
 * enabled around MMDB_open and inside each worker's measured loop. */
static __thread bool alloc_counting;
static __thread uint64_t alloc_calls, alloc_bytes, free_calls;
#ifdef BENCH_COUNT_ALLOCATIONS
void *__real_malloc(size_t size);
void *__real_calloc(size_t count, size_t size);
void *__real_realloc(void *pointer, size_t size);
void __real_free(void *pointer);
void *__wrap_malloc(size_t size) {
    if (alloc_counting) {
        ++alloc_calls;
        alloc_bytes += size;
    }
    return __real_malloc(size);
}
void *__wrap_calloc(size_t count, size_t size) {
    if (alloc_counting) {
        ++alloc_calls;
        alloc_bytes += count * size;
    }
    return __real_calloc(count, size);
}
void *__wrap_realloc(void *pointer, size_t size) {
    if (alloc_counting) {
        ++alloc_calls;
        alloc_bytes += size;
    }
    return __real_realloc(pointer, size);
}
void __wrap_free(void *pointer) {
    if (alloc_counting && pointer) {
        ++free_calls;
    }
    __real_free(pointer);
}
    #define ALLOCATION_COUNTING_BUILD 1
#else
    #define ALLOCATION_COUNTING_BUILD 0
#endif

typedef struct {
    uint8_t bytes[16];
    uint8_t bits;
} network_s;

typedef struct {
    const char *database;
    const char *mode;
    const char *open_variant;
    const char *path[MAX_PATH_COMPONENTS + 1];
    const char *path_text;
    int family;
    unsigned threads;
    size_t queries;
    bool hits;
    bool decode;
    bool latency;
    bool interactive;
    bool cold;
    bool no_prepare;
    unsigned path_length;
    unsigned repetitions;
    unsigned stride;
    size_t warmup;
    uint64_t seed;
    int cpus[MAX_WORKERS];
    unsigned cpu_count;
} options_s;

typedef struct {
    _Atomic int go;
    pthread_mutex_t mutex;
    pthread_cond_t cond;
    unsigned ready;
    unsigned done;
} gate_s;

typedef struct {
    uint64_t vm_rss_kib, vm_hwm_kib, rss_anon_kib, rss_file_kib, rss_shmem_kib;
} status_s;

/* Each worker's state starts on its own cache line; the size is a multiple of
 * the alignment, so array elements stay aligned. */
typedef struct {
    _Alignas(64) const MMDB_s *mmdb;
    const options_s *options;
    const uint8_t *queries;
    size_t query_size;
    size_t begin, end;
    int cpu;
    gate_s *gate;
    unsigned sample_phase;
    size_t warmup;
    uint32_t *samples;
    size_t sample_count;
    uint64_t start, finish;
    uint64_t completed, hits, decoded, errors, decode_errors, checksum;
    uint64_t cpu_ns, voluntary_switches, involuntary_switches;
    uint64_t aperf, mperf;
    uint64_t alloc_calls, alloc_bytes, free_calls;
    int affinity_error;
} worker_s;

static bool rdpru_available;

static void fail(const char *message) {
    fprintf(stderr, "bench: %s\n", message);
    exit(1);
}

static uint64_t now_ns(void) {
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC_RAW, &ts)) {
        fail("clock_gettime failed");
    }
    return (uint64_t)ts.tv_sec * UINT64_C(1000000000) + (uint64_t)ts.tv_nsec;
}

static uint64_t splitmix64(uint64_t *state) {
    uint64_t z = (*state += UINT64_C(0x9e3779b97f4a7c15));
    z = (z ^ (z >> 30)) * UINT64_C(0xbf58476d1ce4e5b9);
    z = (z ^ (z >> 27)) * UINT64_C(0x94d049bb133111eb);
    return z ^ (z >> 31);
}

/* AMD RDPRU exposes the current logical CPU's APERF/MPERF counters to user
 * space when the kernel permits it. Probe once; never require it. */
#if defined(__x86_64__)
static uint64_t rdpru(uint32_t which) {
    uint32_t low, high;
    __asm__ volatile(".byte 0x0f, 0x01, 0xfd"
                     : "=a"(low), "=d"(high)
                     : "c"(which));
    return (uint64_t)high << 32 | low;
}
static sigjmp_buf rdpru_probe;
static void rdpru_sigill(int signal_number) {
    (void)signal_number;
    siglongjmp(rdpru_probe, 1);
}
static void probe_rdpru(void) {
    struct sigaction action = {.sa_handler = rdpru_sigill}, previous;
    sigemptyset(&action.sa_mask);
    sigaction(SIGILL, &action, &previous);
    if (!sigsetjmp(rdpru_probe, 1)) {
        uint64_t a = rdpru(1);
        uint64_t b = rdpru(1);
        rdpru_available = b >= a;
    }
    sigaction(SIGILL, &previous, NULL);
}
#else
static uint64_t rdpru(uint32_t which) {
    (void)which;
    return 0;
}
static void probe_rdpru(void) {}
#endif

static status_s read_status(void) {
    status_s status = {0};
    FILE *file = fopen("/proc/self/status", "r");
    if (!file) {
        return status;
    }
    char line[256];
    while (fgets(line, sizeof(line), file)) {
        uint64_t value;
        if (sscanf(line, "VmRSS: %" SCNu64, &value) == 1) {
            status.vm_rss_kib = value;
        } else if (sscanf(line, "VmHWM: %" SCNu64, &value) == 1) {
            status.vm_hwm_kib = value;
        } else if (sscanf(line, "RssAnon: %" SCNu64, &value) == 1) {
            status.rss_anon_kib = value;
        } else if (sscanf(line, "RssFile: %" SCNu64, &value) == 1) {
            status.rss_file_kib = value;
        } else if (sscanf(line, "RssShmem: %" SCNu64, &value) == 1) {
            status.rss_shmem_kib = value;
        }
    }
    fclose(file);
    return status;
}

/* Resetting the high-water mark makes VmHWM describe the following phase. */
static bool reset_peak_rss(void) {
    int fd = open("/proc/self/clear_refs", O_WRONLY | O_CLOEXEC);
    if (fd < 0) {
        return false;
    }
    bool ok = write(fd, "5", 1) == 1;
    close(fd);
    return ok;
}

/* Resident KiB of every mapping of the database file. */
static uint64_t mapping_rss_kib(const char *database) {
    char resolved[PATH_MAX];
    if (!realpath(database, resolved)) {
        return 0;
    }
    FILE *file = fopen("/proc/self/smaps", "r");
    if (!file) {
        return 0;
    }
    char line[PATH_MAX + 256];
    bool inside = false;
    uint64_t total = 0;
    while (fgets(line, sizeof(line), file)) {
        unsigned long start, end;
        if (sscanf(line, "%lx-%lx ", &start, &end) == 2) {
            char *path = strchr(line, '/');
            if (path) {
                path[strcspn(path, "\n")] = '\0';
            }
            inside = path && strcmp(path, resolved) == 0;
            continue;
        }
        uint64_t value;
        if (inside && sscanf(line, "Rss: %" SCNu64, &value) == 1) {
            total += value;
        }
    }
    fclose(file);
    return total;
}

static uint64_t heap_in_use(void) {
    struct mallinfo2 info = mallinfo2();
    return (uint64_t)info.uordblks + (uint64_t)info.hblkhd;
}

static void print_status(const char *name, status_s status) {
    printf(",\"%s\":{\"vm_rss_kib\":%" PRIu64 ",\"vm_hwm_kib\":%" PRIu64
           ",\"rss_anon_kib\":%" PRIu64 ",\"rss_file_kib\":%" PRIu64
           ",\"rss_shmem_kib\":%" PRIu64 "}",
           name,
           status.vm_rss_kib,
           status.vm_hwm_kib,
           status.rss_anon_kib,
           status.rss_file_kib,
           status.rss_shmem_kib);
}

/* Populated networks are enumerated with an explicit stack, uniformly
 * reservoir-sampled, and never revisit the IPv4 subtree from IPv6 aliases
 * such as ::ffff:0:0/96 or 2002::/16. Selection is uniform over leaves, not
 * address space. Everything happens before measurement. */
typedef struct {
    uint32_t node;
    uint8_t depth;
    uint8_t prefix[16];
} frame_s;

typedef struct {
    network_s *reservoir;
    uint32_t stored;
    uint64_t leaves;
    uint64_t visited;
    uint64_t prefix_histogram[129];
} networks_s;

static void add_leaf(networks_s *networks,
                     const uint8_t prefix[16],
                     unsigned bits,
                     uint64_t *rng) {
    network_s network;
    memcpy(network.bytes, prefix, 16);
    network.bits = (uint8_t)bits;
    ++networks->prefix_histogram[bits];
    uint64_t index = networks->leaves++;
    if (index < RESERVOIR_SIZE) {
        networks->reservoir[networks->stored++] = network;
        return;
    }
    uint64_t pick = splitmix64(rng) % (index + 1);
    if (pick < RESERVOIR_SIZE) {
        networks->reservoir[pick] = network;
    }
}

static void enumerate_networks(const MMDB_s *mmdb,
                               int family,
                               uint64_t seed,
                               networks_s *networks) {
    uint32_t node_count = mmdb->metadata.node_count;
    uint32_t root = 0;
    uint32_t skip = UINT32_MAX;
    unsigned width = family == 4 ? 32 : 128;
    if (mmdb->metadata.ip_version == 6) {
        if (family == 4) {
            root = mmdb->ipv4_start_node.node_value;
        } else if (mmdb->ipv4_start_node.node_value != 0) {
            skip = mmdb->ipv4_start_node.node_value;
        }
    } else if (family == 6) {
        fail("IPv6 queries require an IPv6 database");
    }
    networks->reservoir = calloc(RESERVOIR_SIZE, sizeof(network_s));
    if (!networks->reservoir) {
        fail("Cannot allocate network reservoir");
    }
    uint64_t rng = seed ^ UINT64_C(0x5851f42d4c957f2d);
    if (root >= node_count) {
        if (root > node_count) {
            uint8_t empty[16] = {0};
            add_leaf(networks, empty, 0, &rng);
        }
        return;
    }
    frame_s stack[2 * 129 + 2];
    unsigned top = 0;
    stack[top++] = (frame_s){.node = root, .depth = 0};
    while (top) {
        frame_s frame = stack[--top];
        if (++networks->visited > MAX_VISITED_NODES) {
            fail("Network enumeration exceeded its node budget");
        }
        MMDB_search_node_s node;
        if (MMDB_read_node(mmdb, frame.node, &node) != MMDB_SUCCESS) {
            fail("Cannot read a search node during enumeration");
        }
        /* Push the right child first so the left subtree is visited first. */
        for (int side = 1; side >= 0; --side) {
            uint64_t record = side ? node.right_record : node.left_record;
            uint8_t type =
                side ? node.right_record_type : node.left_record_type;
            frame_s child = frame;
            if (side) {
                child.prefix[frame.depth / 8] |=
                    (uint8_t)(128U >> (frame.depth % 8));
            }
            child.depth = (uint8_t)(frame.depth + 1);
            if (type == MMDB_RECORD_TYPE_DATA) {
                add_leaf(networks, child.prefix, child.depth, &rng);
            } else if (type == MMDB_RECORD_TYPE_SEARCH_NODE && record != skip &&
                       child.depth < width) {
                child.node = (uint32_t)record;
                stack[top++] = child;
            }
        }
    }
}

static uint8_t *make_queries(const MMDB_s *mmdb,
                             const options_s *options,
                             size_t *query_size,
                             networks_s *networks) {
    size_t size = options->family == 4 ? sizeof(struct sockaddr_in)
                                       : sizeof(struct sockaddr_in6);
    if (options->queries > SIZE_MAX / size) {
        fail("Query corpus is too large");
    }
    uint8_t *queries = calloc(options->queries, size);
    if (!queries) {
        fail("Cannot allocate query corpus");
    }
    *query_size = size;
    if (options->hits) {
        enumerate_networks(mmdb, options->family, options->seed, networks);
        if (!networks->stored) {
            fail("No populated networks for this family");
        }
    }
    uint64_t seed = options->seed;
    unsigned width = options->family == 4 ? 4 : 16;
    for (size_t i = 0; i < options->queries; ++i) {
        uint8_t *bytes;
        if (options->family == 4) {
            struct sockaddr_in *address =
                (struct sockaddr_in *)(queries + i * size);
            address->sin_family = AF_INET;
            bytes = (uint8_t *)&address->sin_addr;
        } else {
            struct sockaddr_in6 *address =
                (struct sockaddr_in6 *)(queries + i * size);
            address->sin6_family = AF_INET6;
            bytes = (uint8_t *)&address->sin6_addr;
        }
        network_s prefix = {{0}, 0};
        if (options->hits) {
            prefix = networks->reservoir[splitmix64(&seed) % networks->stored];
        }
        uint64_t random = 0;
        for (unsigned j = 0; j < width; ++j) {
            if (j % 8 == 0) {
                random = splitmix64(&seed);
            }
            uint8_t value = (uint8_t)(random >> (8 * (j % 8)));
            unsigned kept = prefix.bits > j * 8 ? prefix.bits - j * 8 : 0;
            if (kept > 8) {
                kept = 8;
            }
            uint8_t mask = kept ? (uint8_t)(255U << (8 - kept)) : 0;
            bytes[j] = (uint8_t)((value & ~mask) | (prefix.bytes[j] & mask));
        }
    }
    return queries;
}

static const uint8_t *query_address(const uint8_t *query, int family) {
    return family == 4
               ? (const uint8_t *)&((const struct sockaddr_in *)query)->sin_addr
               : (const uint8_t *)&((const struct sockaddr_in6 *)query)
                     ->sin6_addr;
}

static void hex_digest(EVP_MD_CTX *context, char output[65]) {
    unsigned char digest[32];
    unsigned length;
    if (EVP_DigestFinal_ex(context, digest, &length) != 1 || length != 32) {
        fail("SHA-256 finalization failed");
    }
    for (unsigned i = 0; i < 32; ++i) {
        sprintf(output + i * 2, "%02x", digest[i]);
    }
}

/* Hash canonical network-order address bytes, excluding sockaddr layout. */
static void corpus_sha256(const uint8_t *queries,
                          size_t count,
                          size_t size,
                          int family,
                          char output[65]) {
    EVP_MD_CTX *context = EVP_MD_CTX_new();
    if (!context || EVP_DigestInit_ex(context, EVP_sha256(), NULL) != 1) {
        fail("SHA-256 initialization failed");
    }
    unsigned width = family == 4 ? 4 : 16;
    for (size_t i = 0; i < count; ++i) {
        if (EVP_DigestUpdate(context,
                             query_address(queries + i * size, family),
                             width) != 1) {
            fail("SHA-256 update failed");
        }
    }
    hex_digest(context, output);
    EVP_MD_CTX_free(context);
}

static int get_selected_value(MMDB_entry_s *entry,
                              MMDB_entry_data_s *data,
                              const options_s *options) {
    const char *const *p = options->path;
    switch (options->path_length) {
        case 1:
            return MMDB_get_value(entry, data, p[0], NULL);
        case 2:
            return MMDB_get_value(entry, data, p[0], p[1], NULL);
        case 3:
            return MMDB_get_value(entry, data, p[0], p[1], p[2], NULL);
        case 4:
            return MMDB_get_value(entry, data, p[0], p[1], p[2], p[3], NULL);
        case 5:
            return MMDB_get_value(
                entry, data, p[0], p[1], p[2], p[3], p[4], NULL);
        default:
            return MMDB_get_value(
                entry, data, p[0], p[1], p[2], p[3], p[4], p[5], NULL);
    }
}

typedef struct {
    MMDB_lookup_result_s result;
    int error;
    int decode_error;
    bool has_data;
    uint64_t value;
} outcome_s;

static inline outcome_s
execute(const MMDB_s *mmdb, const options_s *options, const uint8_t *query) {
    outcome_s outcome = {.has_data = false, .value = 0, .decode_error = 0};
    outcome.result = MMDB_lookup_sockaddr(
        mmdb, (const struct sockaddr *)query, &outcome.error);
    if (!outcome.error && outcome.result.found_entry && options->decode) {
        MMDB_entry_data_s data;
        outcome.decode_error =
            get_selected_value(&outcome.result.entry, &data, options);
        /* A record without the selected key is a valid, absent field. */
        if (outcome.decode_error ==
            MMDB_LOOKUP_PATH_DOES_NOT_MATCH_DATA_ERROR) {
            outcome.decode_error = MMDB_SUCCESS;
            data.has_data = false;
        }
        if (!outcome.decode_error && data.has_data) {
            outcome.has_data = true;
            uint64_t value = data.type;
            if (data.type == MMDB_DATA_TYPE_UINT32) {
                value += data.uint32;
            } else if (data.type == MMDB_DATA_TYPE_UINT16) {
                value += data.uint16;
            } else if (data.type == MMDB_DATA_TYPE_UTF8_STRING ||
                       data.type == MMDB_DATA_TYPE_BYTES) {
                const uint8_t *bytes = data.type == MMDB_DATA_TYPE_BYTES
                                           ? data.bytes
                                           : (const uint8_t *)data.utf8_string;
                for (uint32_t j = 0; j < data.data_size; ++j) {
                    value = value * 33 + bytes[j];
                }
            } else {
                /* A wrong field must not silently produce a decode series. */
                outcome.decode_error = MMDB_INVALID_DATA_ERROR;
            }
            outcome.value = value;
        }
    }
    return outcome;
}

/* Thread-local tallies live in a non-escaping local so the compiler can keep
 * them in registers; nothing is shared between workers during measurement. */
typedef struct {
    uint64_t completed, hits, decoded, errors, decode_errors, checksum;
} tally_s;

static inline void consume(const MMDB_s *mmdb,
                           const options_s *options,
                           const uint8_t *query,
                           tally_s *tally) {
    outcome_s outcome = execute(mmdb, options, query);
    ++tally->completed;
    tally->hits += outcome.result.found_entry && !outcome.error;
    tally->decoded += outcome.has_data;
    tally->errors += outcome.error != 0;
    tally->decode_errors += outcome.decode_error != 0;
    tally->checksum += outcome.result.netmask + outcome.result.entry.offset +
                       (uint64_t)outcome.result.found_entry + outcome.value;
}

static uint64_t thread_cpu(struct rusage *usage) {
    if (getrusage(RUSAGE_THREAD, usage)) {
        fail("getrusage failed");
    }
    return ((uint64_t)usage->ru_utime.tv_sec +
            (uint64_t)usage->ru_stime.tv_sec) *
               UINT64_C(1000000000) +
           ((uint64_t)usage->ru_utime.tv_usec +
            (uint64_t)usage->ru_stime.tv_usec) *
               UINT64_C(1000);
}

static void *run_worker(void *argument) {
    worker_s *w = argument;
    cpu_set_t set;
    CPU_ZERO(&set);
    CPU_SET(w->cpu, &set);
    w->affinity_error =
        pthread_setaffinity_np(pthread_self(), sizeof(set), &set);
    const MMDB_s *mmdb = w->mmdb;
    const options_s *options = w->options;
    const uint8_t *queries = w->queries;
    const size_t size = w->query_size, begin = w->begin, end = w->end;
    /* Untimed warm-up of the first queries of this partition, capped so that
     * every repetition warms at most --warmup queries in total. */
    size_t warm_end = begin + w->warmup;
    tally_s warm = {0};
    for (size_t i = begin; i < warm_end; ++i) {
        consume(mmdb, options, queries + i * size, &warm);
    }
    if (warm.checksum == UINT64_MAX || warm.errors || warm.decode_errors) {
        fputs("bench: warm-up sentinel or error\n", stderr);
    }
    pthread_mutex_lock(&w->gate->mutex);
    ++w->gate->ready;
    pthread_cond_broadcast(&w->gate->cond);
    pthread_mutex_unlock(&w->gate->mutex);
    while (!atomic_load_explicit(&w->gate->go, memory_order_acquire)) {
        cpu_relax();
    }
    struct rusage before, after;
    uint64_t cpu_before = thread_cpu(&before);
    uint64_t aperf = rdpru_available ? rdpru(1) : 0;
    uint64_t mperf = rdpru_available ? rdpru(0) : 0;
    alloc_calls = alloc_bytes = free_calls = 0;
    alloc_counting = true;
    tally_s tally = {0};
    if (w->samples) {
        /* Time one actual operation every stride operations, starting at a
         * phase that varies by worker and repetition. */
        uint32_t *samples = w->samples;
        size_t count = 0;
        unsigned stride = options->stride;
        unsigned countdown = w->sample_phase + 1;
        for (size_t i = begin; i < end; ++i) {
            const uint8_t *query = queries + i * size;
            if (--countdown == 0) {
                countdown = stride;
                uint64_t start = now_ns();
                consume(mmdb, options, query, &tally);
                uint64_t elapsed = now_ns() - start;
                samples[count++] =
                    elapsed > UINT32_MAX ? UINT32_MAX : (uint32_t)elapsed;
            } else {
                consume(mmdb, options, query, &tally);
            }
        }
        w->sample_count = count;
    } else {
        for (size_t i = begin; i < end; ++i) {
            consume(mmdb, options, queries + i * size, &tally);
        }
    }
    w->finish = now_ns();
    alloc_counting = false;
    w->alloc_calls = alloc_calls;
    w->alloc_bytes = alloc_bytes;
    w->free_calls = free_calls;
    if (rdpru_available) {
        w->aperf = rdpru(1) - aperf;
        w->mperf = rdpru(0) - mperf;
    }
    w->cpu_ns = thread_cpu(&after) - cpu_before;
    w->voluntary_switches = (uint64_t)(after.ru_nvcsw - before.ru_nvcsw);
    w->involuntary_switches = (uint64_t)(after.ru_nivcsw - before.ru_nivcsw);
    w->completed = tally.completed;
    w->hits = tally.hits;
    w->decoded = tally.decoded;
    w->errors = tally.errors;
    w->decode_errors = tally.decode_errors;
    w->checksum = tally.checksum;
    pthread_mutex_lock(&w->gate->mutex);
    ++w->gate->done;
    pthread_cond_broadcast(&w->gate->cond);
    pthread_mutex_unlock(&w->gate->mutex);
    return NULL;
}

static int compare_u32(const void *a, const void *b) {
    uint32_t x = *(const uint32_t *)a, y = *(const uint32_t *)b;
    return x < y ? -1 : x > y;
}

static int compare_u64(const void *a, const void *b) {
    uint64_t x = *(const uint64_t *)a, y = *(const uint64_t *)b;
    return x < y ? -1 : x > y;
}

/* Nearest lower rank: floor((n - 1) * p), with p expressed per million. */
static uint32_t rank_u32(const uint32_t *sorted, size_t n, uint64_t ppm) {
    return sorted[(size_t)(((uint64_t)(n - 1) * ppm) / UINT64_C(1000000))];
}

static void timer_calibration(uint64_t result[4]) {
    static uint64_t samples[TIMER_CALIBRATION_PAIRS];
    for (unsigned i = 0; i < TIMER_CALIBRATION_PAIRS; ++i) {
        uint64_t a = now_ns();
        uint64_t b = now_ns();
        samples[i] = b - a;
    }
    qsort(samples, TIMER_CALIBRATION_PAIRS, sizeof(uint64_t), compare_u64);
    result[0] = samples[0];
    result[1] = samples[TIMER_CALIBRATION_PAIRS / 2];
    result[2] = samples[(TIMER_CALIBRATION_PAIRS - 1) * 99 / 100];
    result[3] = samples[TIMER_CALIBRATION_PAIRS - 1];
}

static void print_json_string(const char *text) {
    putchar('"');
    for (const char *c = text; *c; ++c) {
        if (*c == '"' || *c == '\\') {
            putchar('\\');
        }
        putchar(*c);
    }
    putchar('"');
}

static void print_identity(const MMDB_s *mmdb, const options_s *options) {
    printf("\"library_version\":");
    print_json_string(MMDB_lib_version());
    printf(",\"allocation_counting_build\":%s",
           ALLOCATION_COUNTING_BUILD ? "true" : "false");
    printf(",\"rdpru_available\":%s", rdpru_available ? "true" : "false");
    printf(",\"database\":");
    print_json_string(options->database);
    printf(",\"database_type\":");
    print_json_string(mmdb->metadata.database_type);
    printf(",\"node_count\":%" PRIu32 ",\"record_size\":%" PRIu16
           ",\"ip_version\":%" PRIu16 ",\"build_epoch\":%" PRIu64
           ",\"mmdb_flags\":%" PRIu32 ",\"private_prepared_flag\":%s",
           mmdb->metadata.node_count,
           mmdb->metadata.record_size,
           mmdb->metadata.ip_version,
           mmdb->metadata.build_epoch,
           mmdb->flags,
           mmdb->flags & PRIVATE_PREPARED_FLAG ? "true" : "false");
}

static void parse_cpus(options_s *options, char *text) {
    options->cpu_count = 0;
    for (char *token = strtok(text, ","); token; token = strtok(NULL, ",")) {
        if (options->cpu_count == MAX_WORKERS) {
            fail("Too many CPUs");
        }
        options->cpus[options->cpu_count++] = atoi(token);
    }
}

static void parse_path(options_s *options, char *text) {
    options->path_text = strdup(text);
    options->path_length = 0;
    for (char *token = strtok(text, "/"); token; token = strtok(NULL, "/")) {
        if (options->path_length == MAX_PATH_COMPONENTS) {
            fail("Decode path is too long");
        }
        options->path[options->path_length++] = token;
    }
    options->path[options->path_length] = NULL;
    if (!options->path_length) {
        fail("Empty decode path");
    }
}

static options_s parse_options(int argc, char **argv) {
    options_s options = {.mode = "run",
                         .open_variant = "default",
                         .family = 4,
                         .threads = 1,
                         .queries = 1000000,
                         .repetitions = 1,
                         .stride = 16,
                         .seed = UINT64_C(0x6a09e667f3bcc909)};
    for (int i = 1; i < argc; ++i) {
        const char *key = argv[i];
        bool flag = !strcmp(key, "--interactive") || !strcmp(key, "--cold") ||
                    !strcmp(key, "--no-prepare");
        if (!flag && i + 1 >= argc) {
            fail("Missing option value");
        }
        char *value = flag ? NULL : argv[++i];
        if (!strcmp(key, "--db")) {
            options.database = value;
        } else if (!strcmp(key, "--mode")) {
            options.mode = value;
        } else if (!strcmp(key, "--open-variant")) {
            options.open_variant = value;
        } else if (!strcmp(key, "--family")) {
            options.family = atoi(value);
        } else if (!strcmp(key, "--threads")) {
            options.threads = (unsigned)strtoul(value, NULL, 10);
        } else if (!strcmp(key, "--queries")) {
            options.queries = (size_t)strtoull(value, NULL, 10);
        } else if (!strcmp(key, "--profile")) {
            options.hits = !strcmp(value, "hits");
            if (!options.hits && strcmp(value, "random")) {
                fail("Profile must be random or hits");
            }
        } else if (!strcmp(key, "--operation")) {
            options.decode = !strcmp(value, "decode");
            if (!options.decode && strcmp(value, "lookup")) {
                fail("Operation must be lookup or decode");
            }
        } else if (!strcmp(key, "--path")) {
            parse_path(&options, value);
        } else if (!strcmp(key, "--campaign")) {
            options.latency = !strcmp(value, "latency");
            if (!options.latency && strcmp(value, "throughput")) {
                fail("Campaign must be throughput or latency");
            }
        } else if (!strcmp(key, "--reps")) {
            options.repetitions = (unsigned)strtoul(value, NULL, 10);
        } else if (!strcmp(key, "--warmup")) {
            options.warmup = (size_t)strtoull(value, NULL, 10);
        } else if (!strcmp(key, "--stride")) {
            options.stride = (unsigned)strtoul(value, NULL, 10);
        } else if (!strcmp(key, "--seed")) {
            options.seed = strtoull(value, NULL, 0);
        } else if (!strcmp(key, "--cpus")) {
            parse_cpus(&options, value);
        } else if (!strcmp(key, "--interactive")) {
            options.interactive = true;
        } else if (!strcmp(key, "--cold")) {
            options.cold = true;
        } else if (!strcmp(key, "--no-prepare")) {
            options.no_prepare = true;
        } else {
            fail("Unknown option");
        }
    }
    if (!options.database) {
        fail("--db is required");
    }
    if ((options.family != 4 && options.family != 6) || !options.queries ||
        !options.threads || options.threads > MAX_WORKERS ||
        !options.repetitions || !options.stride) {
        fail("Invalid benchmark options");
    }
    if (!options.warmup) {
        options.warmup = options.queries;
    }
    if (options.decode && !options.path_length) {
        fail("--path is required for decode");
    }
    return options;
}

/* Evict clean cached pages of the database before a cold open. This only
 * succeeds for pages no process maps; residency is reported, not assumed. */
static uint64_t evict_database(const char *database, uint64_t *pages) {
    int fd = open(database, O_RDONLY | O_CLOEXEC);
    if (fd < 0) {
        fail("Cannot open database for eviction");
    }
    struct stat st;
    if (fstat(fd, &st)) {
        fail("Cannot stat database");
    }
    posix_fadvise(fd, 0, 0, POSIX_FADV_DONTNEED);
    size_t page = (size_t)sysconf(_SC_PAGESIZE);
    size_t length = (size_t)st.st_size;
    *pages = (length + page - 1) / page;
    void *mapping = mmap(NULL, length, PROT_READ, MAP_SHARED, fd, 0);
    uint64_t resident = 0;
    if (mapping != MAP_FAILED) {
        unsigned char *vector = malloc(*pages);
        if (vector && !mincore(mapping, length, vector)) {
            for (size_t i = 0; i < *pages; ++i) {
                resident += vector[i] & 1;
            }
        }
        free(vector);
        munmap(mapping, length);
    }
    close(fd);
    return resident;
}

static int run_open(const options_s *options) {
    uint64_t resident = 0, pages = 0;
    if (options->cold) {
        resident = evict_database(options->database, &pages);
    }
    status_s start = read_status();
    uint64_t heap_start = heap_in_use();
    uint32_t flags = MMDB_MODE_MMAP;
    bool context = !strcmp(options->open_variant, "context");
    if (!strcmp(options->open_variant, "no-prepare") || context) {
#ifdef MMDB_NO_PREPARE
        flags |= MMDB_NO_PREPARE;
#else
        fail("This library revision has no MMDB_NO_PREPARE");
#endif
    } else if (strcmp(options->open_variant, "default")) {
        fail("Unknown open variant");
    }
    MMDB_s mmdb;
    alloc_calls = alloc_bytes = free_calls = 0;
    alloc_counting = true;
    uint64_t begin = now_ns();
    int error = MMDB_open(options->database, flags, &mmdb);
    uint64_t opened = now_ns();
    alloc_counting = false;
    uint64_t open_calls = alloc_calls, open_bytes = alloc_bytes;
    if (error) {
        fail(MMDB_strerror(error));
    }
    uint64_t context_ns = 0, context_calls = 0, context_bytes = 0;
#ifdef MMDB_NO_PREPARE
    MMDB_lookup_context_s *prepared = NULL;
    if (context) {
        alloc_calls = alloc_bytes = 0;
        alloc_counting = true;
        uint64_t context_begin = now_ns();
        error = MMDB_create_lookup_context(&mmdb, &prepared);
        context_ns = now_ns() - context_begin;
        alloc_counting = false;
        context_calls = alloc_calls;
        context_bytes = alloc_bytes;
        if (error) {
            fail(MMDB_strerror(error));
        }
    }
#endif
    status_s after = read_status();
    uint64_t heap_after = heap_in_use();
    uint64_t mapped = mapping_rss_kib(options->database);
    printf("{\"type\":\"open\",");
    print_identity(&mmdb, options);
    printf(",\"open_variant\":");
    print_json_string(options->open_variant);
    printf(",\"cold\":%s,\"resident_pages_before_open\":%" PRIu64
           ",\"database_pages\":%" PRIu64 ",\"open_ns\":%" PRIu64
           ",\"context_ns\":%" PRIu64 ",\"open_alloc_calls\":%" PRIu64
           ",\"open_alloc_bytes\":%" PRIu64 ",\"context_alloc_calls\":%" PRIu64
           ",\"context_alloc_bytes\":%" PRIu64 ",\"heap_before_open\":%" PRIu64
           ",\"heap_after_open\":%" PRIu64 ",\"mapping_rss_kib\":%" PRIu64,
           options->cold ? "true" : "false",
           resident,
           pages,
           opened - begin,
           context_ns,
           open_calls,
           open_bytes,
           context_calls,
           context_bytes,
           heap_start,
           heap_after,
           mapped);
    print_status("status_before_open", start);
    print_status("status_after_open", after);
    printf("}\n");
#ifdef MMDB_NO_PREPARE
    MMDB_free_lookup_context(prepared);
#endif
    MMDB_close(&mmdb);
    return 0;
}

/* Ordered digest of every per-query result, for differential comparison. */
static void verify_corpus(const MMDB_s *mmdb,
                          const options_s *options,
                          const uint8_t *queries,
                          size_t size) {
    EVP_MD_CTX *context = EVP_MD_CTX_new();
    if (!context || EVP_DigestInit_ex(context, EVP_sha256(), NULL) != 1) {
        fail("SHA-256 initialization failed");
    }
    uint64_t hits = 0, errors = 0, decode_errors = 0, with_data = 0;
    for (size_t i = 0; i < options->queries; ++i) {
        outcome_s outcome = execute(mmdb, options, queries + i * size);
        uint8_t record[32] = {0};
        record[0] = outcome.result.found_entry;
        record[1] = outcome.has_data;
        record[2] = (uint8_t)outcome.error;
        record[3] = (uint8_t)outcome.decode_error;
        uint16_t netmask = outcome.result.netmask;
        uint64_t offset = outcome.result.entry.offset;
        for (unsigned j = 0; j < 2; ++j) {
            record[4 + j] = (uint8_t)(netmask >> (8 * j));
        }
        for (unsigned j = 0; j < 8; ++j) {
            record[8 + j] = (uint8_t)(offset >> (8 * j));
            record[16 + j] = (uint8_t)(outcome.value >> (8 * j));
        }
        if (EVP_DigestUpdate(context, record, sizeof(record)) != 1) {
            fail("SHA-256 update failed");
        }
        hits += outcome.result.found_entry && !outcome.error;
        errors += outcome.error != 0;
        decode_errors += outcome.decode_error != 0;
        with_data += outcome.has_data;
    }
    char digest[65];
    hex_digest(context, digest);
    EVP_MD_CTX_free(context);
    printf(",\"result_sha256\":\"%s\",\"verify_hits\":%" PRIu64
           ",\"verify_errors\":%" PRIu64 ",\"verify_decode_errors\":%" PRIu64
           ",\"verify_with_data\":%" PRIu64,
           digest,
           hits,
           errors,
           decode_errors,
           with_data);
}

static void run_repetition(const MMDB_s *mmdb,
                           const options_s *options,
                           const uint8_t *queries,
                           size_t query_size,
                           unsigned repetition) {
    gate_s gate = {.mutex = PTHREAD_MUTEX_INITIALIZER,
                   .cond = PTHREAD_COND_INITIALIZER};
    atomic_init(&gate.go, 0);
    unsigned workers = options->threads;
    worker_s *state = NULL;
    if (posix_memalign((void **)&state, 64, workers * sizeof(*state))) {
        fail("Cannot allocate workers");
    }
    memset(state, 0, workers * sizeof(*state));
    pthread_t threads[MAX_WORKERS];
    uint64_t sample_bytes = 0;
    for (unsigned j = 0; j < workers; ++j) {
        worker_s *w = &state[j];
        w->mmdb = mmdb;
        w->options = options;
        w->queries = queries;
        w->query_size = query_size;
        w->begin = options->queries * j / workers;
        w->end = options->queries * (j + 1) / workers;
        size_t share = (options->warmup + workers - 1) / workers;
        w->warmup = share < w->end - w->begin ? share : w->end - w->begin;
        w->cpu = options->cpus[j % options->cpu_count];
        w->gate = &gate;
        w->sample_phase =
            (unsigned)((options->seed + repetition * 37U + j * 101U) %
                       options->stride);
        if (options->latency) {
            size_t capacity = (w->end - w->begin) / options->stride + 1;
            w->samples = calloc(capacity, sizeof(uint32_t));
            if (!w->samples) {
                fail("Cannot allocate latency samples");
            }
            sample_bytes += capacity * sizeof(uint32_t);
        }
        if (pthread_create(&threads[j], NULL, run_worker, w)) {
            fail("Cannot create worker");
        }
    }
    pthread_mutex_lock(&gate.mutex);
    while (gate.ready != workers) {
        pthread_cond_wait(&gate.cond, &gate.mutex);
    }
    pthread_mutex_unlock(&gate.mutex);
    status_s warmed = read_status();
    uint64_t mapped_warm = mapping_rss_kib(options->database);
    bool reset = reset_peak_rss();
    uint64_t start = now_ns();
    atomic_store_explicit(&gate.go, 1, memory_order_release);
    pthread_mutex_lock(&gate.mutex);
    while (gate.done != workers) {
        pthread_cond_wait(&gate.cond, &gate.mutex);
    }
    pthread_mutex_unlock(&gate.mutex);
    uint64_t finish = start, completed = 0, hits = 0, decoded = 0, errors = 0,
             decode_errors = 0, checksum = 0, cpu_ns = 0, voluntary = 0,
             involuntary = 0, aperf = 0, mperf = 0, allocations = 0,
             allocated = 0, frees = 0;
    size_t sample_count = 0;
    for (unsigned j = 0; j < workers; ++j) {
        pthread_join(threads[j], NULL);
        worker_s *w = &state[j];
        if (w->affinity_error) {
            fail("Cannot pin worker");
        }
        if (w->finish > finish) {
            finish = w->finish;
        }
        completed += w->completed;
        hits += w->hits;
        decoded += w->decoded;
        errors += w->errors;
        decode_errors += w->decode_errors;
        checksum += w->checksum;
        cpu_ns += w->cpu_ns;
        voluntary += w->voluntary_switches;
        involuntary += w->involuntary_switches;
        aperf += w->aperf;
        mperf += w->mperf;
        allocations += w->alloc_calls;
        allocated += w->alloc_bytes;
        frees += w->free_calls;
        sample_count += w->sample_count;
    }
    status_s peak = read_status();
    if (completed != options->queries || errors || decode_errors) {
        fail("Invalid completed count or lookup/decode errors");
    }
    uint64_t elapsed = finish - start;
    printf("{\"type\":\"repetition\",\"repetition\":%u,\"completed\":%" PRIu64
           ",\"hits\":%" PRIu64 ",\"misses\":%" PRIu64 ",\"decoded\":%" PRIu64
           ",\"errors\":%" PRIu64 ",\"decode_errors\":%" PRIu64
           ",\"checksum\":%" PRIu64 ",\"elapsed_ns\":%" PRIu64
           ",\"lookups_per_second\":%.6f"
           ",\"cpu_ns\":%" PRIu64 ",\"voluntary_switches\":%" PRIu64
           ",\"involuntary_switches\":%" PRIu64 ",\"aperf\":%" PRIu64
           ",\"mperf\":%" PRIu64 ",\"measured_alloc_calls\":%" PRIu64
           ",\"measured_alloc_bytes\":%" PRIu64
           ",\"measured_free_calls\":%" PRIu64
           ",\"peak_reset\":%s,\"mapping_rss_after_warmup_kib\":%" PRIu64
           ",\"sample_buffer_bytes\":%" PRIu64,
           repetition,
           completed,
           hits,
           completed - hits,
           decoded,
           errors,
           decode_errors,
           checksum,
           elapsed,
           (double)completed * 1e9 / (double)elapsed,
           cpu_ns,
           voluntary,
           involuntary,
           aperf,
           mperf,
           allocations,
           allocated,
           frees,
           reset ? "true" : "false",
           mapped_warm,
           sample_bytes);
    print_status("status_after_warmup", warmed);
    print_status("status_after_lookups", peak);
    printf(",\"workers\":[");
    for (unsigned j = 0; j < workers; ++j) {
        worker_s *w = &state[j];
        printf("%s{\"cpu\":%d,\"begin\":%zu,\"end\":%zu,\"elapsed_ns\":%" PRIu64
               ",\"cpu_ns\":%" PRIu64 ",\"voluntary_switches\":%" PRIu64
               ",\"involuntary_switches\":%" PRIu64 ",\"aperf\":%" PRIu64
               ",\"mperf\":%" PRIu64 ",\"checksum\":%" PRIu64 "}",
               j ? "," : "",
               w->cpu,
               w->begin,
               w->end,
               w->finish - start,
               w->cpu_ns,
               w->voluntary_switches,
               w->involuntary_switches,
               w->aperf,
               w->mperf,
               w->checksum);
    }
    printf("],\"latency_observations\":%zu", sample_count);
    if (sample_count) {
        uint32_t *samples = malloc(sample_count * sizeof(uint32_t));
        if (!samples) {
            fail("Cannot merge latency samples");
        }
        size_t next = 0;
        for (unsigned j = 0; j < workers; ++j) {
            memcpy(samples + next,
                   state[j].samples,
                   state[j].sample_count * sizeof(uint32_t));
            next += state[j].sample_count;
        }
        /* Merge every worker's observations before computing percentiles. */
        qsort(samples, sample_count, sizeof(uint32_t), compare_u32);
        printf(",\"latency_ns\":{\"min\":%" PRIu32 ",\"p50\":%" PRIu32
               ",\"p90\":%" PRIu32 ",\"p95\":%" PRIu32 ",\"p99\":%" PRIu32
               ",\"p999\":%" PRIu32 ",\"max\":%" PRIu32 "}",
               samples[0],
               rank_u32(samples, sample_count, 500000),
               rank_u32(samples, sample_count, 900000),
               rank_u32(samples, sample_count, 950000),
               rank_u32(samples, sample_count, 990000),
               rank_u32(samples, sample_count, 999000),
               samples[sample_count - 1]);
        printf(",\"latency_histogram_ns\":[");
        for (size_t k = 0; k < sample_count;) {
            size_t end = k + 1;
            while (end < sample_count && samples[end] == samples[k]) {
                ++end;
            }
            printf("%s[%" PRIu32 ",%zu]", k ? "," : "", samples[k], end - k);
            k = end;
        }
        printf("]");
        free(samples);
    }
    printf("}\n");
    fflush(stdout);
    for (unsigned j = 0; j < workers; ++j) {
        free(state[j].samples);
    }
    free(state);
    pthread_mutex_destroy(&gate.mutex);
    pthread_cond_destroy(&gate.cond);
}

int main(int argc, char **argv) {
    options_s options = parse_options(argc, argv);
    probe_rdpru();
    if (!strcmp(options.mode, "open")) {
        return run_open(&options);
    }
    bool verify = !strcmp(options.mode, "verify");
    if (!verify && strcmp(options.mode, "run")) {
        fail("Mode must be run, verify or open");
    }
    cpu_set_t allowed;
    if (sched_getaffinity(0, sizeof(allowed), &allowed)) {
        fail("Cannot read affinity");
    }
    if (!options.cpu_count) {
        fail("--cpus is required");
    }
    for (unsigned i = 0; i < options.cpu_count; ++i) {
        if (options.cpus[i] < 0 || options.cpus[i] >= CPU_SETSIZE ||
            !CPU_ISSET(options.cpus[i], &allowed)) {
            fail("Requested CPU is not available to this process");
        }
    }
    status_s initial = read_status();
    uint64_t heap_start = heap_in_use();
    MMDB_s mmdb;
    alloc_calls = alloc_bytes = 0;
    alloc_counting = true;
    uint64_t opening_start = now_ns();
    /* --no-prepare isolates the specialized packed traversal of After. */
    uint32_t open_flags = MMDB_MODE_MMAP;
    if (options.no_prepare) {
#ifdef MMDB_NO_PREPARE
        open_flags |= MMDB_NO_PREPARE;
#else
        fail("This library revision has no MMDB_NO_PREPARE");
#endif
    }
    int error = MMDB_open(options.database, open_flags, &mmdb);
    uint64_t opening_ns = now_ns() - opening_start;
    alloc_counting = false;
    uint64_t open_calls = alloc_calls, open_bytes = alloc_bytes;
    if (error) {
        fail(MMDB_strerror(error));
    }
    status_s opened = read_status();
    uint64_t heap_open = heap_in_use();
    uint64_t mapped_open = mapping_rss_kib(options.database);
    size_t query_size = 0;
    networks_s networks;
    memset(&networks, 0, sizeof(networks));
    uint64_t generation_start = now_ns();
    uint8_t *queries = make_queries(&mmdb, &options, &query_size, &networks);
    uint64_t generation_ns = now_ns() - generation_start;
    free(networks.reservoir);
    networks.reservoir = NULL;
    char corpus_hash[65];
    corpus_sha256(
        queries, options.queries, query_size, options.family, corpus_hash);
    status_s corpus = read_status();
    uint64_t timer[4];
    timer_calibration(timer);
    printf("{\"type\":\"header\",");
    print_identity(&mmdb, &options);
    printf(",\"no_prepare\":%s", options.no_prepare ? "true" : "false");
    printf(",\"mode\":\"%s\",\"family\":%d,\"threads\":%u,\"queries\":%zu"
           ",\"profile\":\"%s\",\"operation\":\"%s\",\"decode_path\":",
           options.mode,
           options.family,
           options.threads,
           options.queries,
           options.hits ? "hits" : "random",
           options.decode ? "decode" : "lookup");
    print_json_string(options.decode ? options.path_text : "");
    printf(",\"campaign\":\"%s\",\"stride\":%u,\"warmup\":%zu,\"seed\":%" PRIu64
           ",\"repetitions\":%u,\"opening_ns\":%" PRIu64
           ",\"open_alloc_calls\":%" PRIu64 ",\"open_alloc_bytes\":%" PRIu64
           ",\"heap_before_open\":%" PRIu64 ",\"heap_after_open\":%" PRIu64
           ",\"mapping_rss_after_open_kib\":%" PRIu64
           ",\"corpus_sha256\":\"%s\",\"corpus_bytes\":%zu,\"query_size\":%zu"
           ",\"corpus_generation_ns\":%" PRIu64 ",\"populated_leaves\":%" PRIu64
           ",\"sampled_networks\":%" PRIu32 ",\"enumerated_nodes\":%" PRIu64
           ",\"timer_ns\":{\"min\":%" PRIu64 ",\"p50\":%" PRIu64
           ",\"p99\":%" PRIu64 ",\"max\":%" PRIu64 "},\"cpus\":[",
           options.latency ? "latency" : "throughput",
           options.stride,
           options.warmup,
           options.seed,
           options.repetitions,
           opening_ns,
           open_calls,
           open_bytes,
           heap_start,
           heap_open,
           mapped_open,
           corpus_hash,
           options.queries * query_size,
           query_size,
           generation_ns,
           networks.leaves,
           networks.stored,
           networks.visited,
           timer[0],
           timer[1],
           timer[2],
           timer[3]);
    for (unsigned i = 0; i < options.threads; ++i) {
        printf("%s%d", i ? "," : "", options.cpus[i % options.cpu_count]);
    }
    printf("],\"oversubscribed\":%s",
           options.threads > options.cpu_count ? "true" : "false");
    if (options.hits) {
        printf(",\"prefix_histogram\":[");
        bool comma = false;
        for (unsigned bits = 0; bits <= 128; ++bits) {
            if (networks.prefix_histogram[bits]) {
                printf("%s[%u,%" PRIu64 "]",
                       comma ? "," : "",
                       bits,
                       networks.prefix_histogram[bits]);
                comma = true;
            }
        }
        printf("]");
    }
    print_status("status_initial", initial);
    print_status("status_after_open", opened);
    print_status("status_after_corpus", corpus);
    if (verify) {
        verify_corpus(&mmdb, &options, queries, query_size);
    }
    printf("}\n");
    fflush(stdout);
    if (!verify) {
        for (unsigned repetition = 0; repetition < options.repetitions;
             ++repetition) {
            if (options.interactive) {
                int c;
                while ((c = getchar()) != '\n') {
                    if (c == EOF) {
                        fail("Missing repetition trigger");
                    }
                }
            }
            run_repetition(&mmdb, &options, queries, query_size, repetition);
        }
    }
    free(queries);
    free((void *)options.path_text);
    MMDB_close(&mmdb);
    return 0;
}
