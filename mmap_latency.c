// mmap_latency.c — measures per-page latency for three cases:
//   1. warm read:    page in cache, PTE installed  (nanoseconds)
//   2. minor fault:  page in cache, PTE not installed (microseconds)
//   3. major fault:  page not in cache, RAM free    (microseconds, disk-bound)
//   4. major fault + eviction: page cache full       (microseconds, disk-bound + eviction)
//
// compile: gcc -O2 -o mmap_latency mmap_latency.c
// run:     sudo ./mmap_latency /dev/sda

#define _GNU_SOURCE
#include <fcntl.h>
#include <linux/fs.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#include <stdint.h>

#define PAGE_SIZE  4096
#define N_SAMPLES  300

static double now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1e9 + ts.tv_nsec;
}

static uint64_t dev_size(int fd) {
    struct stat sb;
    fstat(fd, &sb);
    if (sb.st_size > 0) return (uint64_t)sb.st_size;
    uint64_t sz = 0;
    ioctl(fd, BLKGETSIZE64, &sz);
    return sz;
}

static uint64_t mem_available(void) {
    FILE *f = fopen("/proc/meminfo", "r");
    char line[256]; uint64_t kb = 0;
    while (fgets(line, sizeof(line), f))
        if (sscanf(line, "MemAvailable: %lu kB", &kb) == 1) break;
    fclose(f);
    return kb * 1024;
}

static void drop_caches(void) {
    FILE *f = fopen("/proc/sys/vm/drop_caches", "w");
    if (f) { fprintf(f, "1\n"); fclose(f); }
}

// in-place insertion sort for median
static double median(double *a, int n) {
    for (int i = 1; i < n; i++) {
        double key = a[i]; int j = i - 1;
        while (j >= 0 && a[j] > key) { a[j+1] = a[j]; j--; }
        a[j+1] = key;
    }
    return a[n / 2];
}

static void print_ns(const char *label, double *times, int n) {
    double sum = 0;
    for (int i = 0; i < n; i++) sum += times[i];
    double med = median(times, n);
    printf("  %-44s median=%7.1f ns   mean=%7.1f ns\n", label, med, sum/n);
}

static void print_us(const char *label, double *times, int n) {
    double sum = 0;
    for (int i = 0; i < n; i++) sum += times[i];
    double med = median(times, n);
    printf("  %-44s median=%7.2f us   mean=%7.2f us\n", label, med/1e3, sum/n/1e3);
}

int main(int argc, char *argv[]) {
    if (argc < 2) {
        fprintf(stderr, "usage: sudo %s <device>\n", argv[0]);
        return 1;
    }

    int fd = open(argv[1], O_RDONLY);
    if (fd < 0) { perror("open"); return 1; }

    uint64_t size      = dev_size(fd);
    uint64_t test_size = (uint64_t)N_SAMPLES * PAGE_SIZE;  // 1.2 MB
    if (size < test_size * 4) { fprintf(stderr, "device too small\n"); return 1; }

    volatile char sink = 0;
    double times[N_SAMPLES];
    char *p;

    printf("device: %s  (%.1f GB)\n\n", argv[1], size / 1e9);

    // ----------------------------------------------------------------
    // 1. Warm read: page in cache, PTE already installed
    //    Measure many iterations in one shot to avoid clock() overhead
    // ----------------------------------------------------------------
    p = mmap(NULL, test_size, PROT_READ, MAP_SHARED, fd, 0);
    if (p == MAP_FAILED) { perror("mmap"); return 1; }

    for (int i = 0; i < N_SAMPLES; i++) sink += p[(uint64_t)i * PAGE_SIZE]; // warm

    double t0 = now_ns();
    for (int i = 0; i < N_SAMPLES; i++) sink += p[(uint64_t)i * PAGE_SIZE];
    double elapsed = now_ns() - t0;

    for (int i = 0; i < N_SAMPLES; i++) times[i] = elapsed / N_SAMPLES;
    print_ns("1. warm read (cached + PTE)", times, N_SAMPLES);
    munmap(p, test_size);

    // ----------------------------------------------------------------
    // 2. Minor fault: page in cache, but PTE not installed
    //    Populate cache via read(), then fresh mmap (no PTEs yet)
    // ----------------------------------------------------------------
    {
        char *buf = malloc(test_size);
        lseek(fd, 0, SEEK_SET);
        ssize_t r = read(fd, buf, test_size);
        (void)r;
        free(buf);
    }
    p = mmap(NULL, test_size, PROT_READ, MAP_SHARED, fd, 0);
    if (p == MAP_FAILED) { perror("mmap"); return 1; }

    for (int i = 0; i < N_SAMPLES; i++) {
        double a = now_ns();
        sink += p[(uint64_t)i * PAGE_SIZE];
        times[i] = now_ns() - a;
    }
    print_us("2. minor fault (cached, no PTE)", times, N_SAMPLES);
    munmap(p, test_size);

    // ----------------------------------------------------------------
    // 3. Major fault: page not in cache, RAM available
    //    Drop caches, then evict each page individually before timing
    // ----------------------------------------------------------------
    drop_caches();
    p = mmap(NULL, test_size, PROT_READ, MAP_SHARED, fd, 0);
    if (p == MAP_FAILED) { perror("mmap"); return 1; }

    for (int i = 0; i < N_SAMPLES; i++) {
        madvise(p + (uint64_t)i * PAGE_SIZE, PAGE_SIZE, MADV_DONTNEED);
        double a = now_ns();
        sink += p[(uint64_t)i * PAGE_SIZE];
        times[i] = now_ns() - a;
    }
    print_us("3. major fault (not cached, RAM free)", times, N_SAMPLES);
    munmap(p, test_size);

    // ----------------------------------------------------------------
    // 4. Major fault with eviction: fill page cache, then fault in
    //    Fill cache by reading a different region of the device,
    //    then access our test pages (requires evicting cached pages)
    // ----------------------------------------------------------------
    drop_caches();
    uint64_t fill_size = mem_available();
    uint64_t fill_offset = test_size;  // fill starting after our test region
    if (fill_offset + fill_size > size)
        fill_size = size - fill_offset;

    printf("  filling %.1f GB of page cache from device...\n", fill_size / 1e9);
    fflush(stdout);

    {
        char *buf = malloc(1 << 20);  // 1 MB read buffer
        int fill_fd = open(argv[1], O_RDONLY);
        lseek(fill_fd, (off_t)fill_offset, SEEK_SET);
        uint64_t remaining = fill_size;
        while (remaining > 0) {
            size_t chunk = remaining < (1u << 20) ? (size_t)remaining : (1u << 20);
            ssize_t r = read(fill_fd, buf, chunk);
            if (r <= 0) break;
            remaining -= (uint64_t)r;
        }
        close(fill_fd);
        free(buf);
    }

    // test pages were never accessed in the fill, so they're not in cache
    p = mmap(NULL, test_size, PROT_READ, MAP_SHARED, fd, 0);
    if (p == MAP_FAILED) { perror("mmap"); return 1; }

    for (int i = 0; i < N_SAMPLES; i++) {
        madvise(p + (uint64_t)i * PAGE_SIZE, PAGE_SIZE, MADV_DONTNEED);
        double a = now_ns();
        sink += p[(uint64_t)i * PAGE_SIZE];
        times[i] = now_ns() - a;
    }
    print_us("4. major fault + eviction (cache full)", times, N_SAMPLES);
    munmap(p, test_size);

    // ----------------------------------------------------------------
    // 5. Direct pread (O_DIRECT): bypasses page cache entirely
    //    Each read goes straight to disk — no fault overhead, no cache
    // ----------------------------------------------------------------
    {
        int dfd = open(argv[1], O_RDONLY | O_DIRECT);
        if (dfd < 0) { perror("open O_DIRECT"); goto done; }

        char *buf;
        if (posix_memalign((void **)&buf, PAGE_SIZE, PAGE_SIZE) != 0) {
            perror("posix_memalign"); close(dfd); goto done;
        }

        for (int i = 0; i < N_SAMPLES; i++) {
            double a = now_ns();
            ssize_t r = pread(dfd, buf, PAGE_SIZE, (off_t)i * PAGE_SIZE);
            times[i] = now_ns() - a;
            if (r > 0) sink += buf[0];
        }
        print_us("5. pread O_DIRECT (no cache, no fault overhead)", times, N_SAMPLES);

        free(buf);
        close(dfd);
    }

done:
    printf("\n(sink=%d)\n", (int)(unsigned char)sink);
    close(fd);
    return 0;
}
