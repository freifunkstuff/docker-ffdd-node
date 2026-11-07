#!/usr/bin/env python3
"""Opt-in offline Docker proof: no mounts, network, host outputs or real keys.
Run directly, not unittest discovery. Requires cached compiler image.
"""
import argparse
import pathlib
import subprocess

PREFIX = r'''
#define _GNU_SOURCE
#include <stdbool.h>
#include <stdlib.h>
#include <stdio.h>
#include <errno.h>
#include <limits.h>
#include <stddef.h>
#include <stdint.h>
#include <sys/syscall.h>
#include <sys/resource.h>
#include <sys/wait.h>
#include <unistd.h>
#include <fcntl.h>
#include <stdarg.h>
#include <string.h>
#include <time.h>
#include <assert.h>
#include <pthread.h>
static int mode, low, high, guarded;
static unsigned int close_calls, read_calls;
static int expected_failure(void) {
    return mode == 3 || mode == 4 || mode == 7 || (mode >= 10 && mode <= 15);
}
static pthread_mutex_t allocator_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_barrier_t ready;
void *__real_malloc(size_t);
void *__wrap_malloc(size_t n) {
    if (guarded) pthread_mutex_lock(&allocator_lock);
    return __real_malloc(n);
}
static void *holder(void *unused) {
    (void)unused;
    pthread_mutex_lock(&allocator_lock);
    pthread_barrier_wait(&ready);
    for (;;) pause();
    return NULL;
}
static long proof_syscall(long nr, ...) {
    va_list ap; va_start(ap, nr); long result;
    if (nr == SYS_close_range) {
        unsigned int first = va_arg(ap, unsigned int);
        unsigned int last = va_arg(ap, unsigned int);
        unsigned int flags = va_arg(ap, unsigned int);
        if (mode != 0 && mode != 5) { errno = mode == 2 ? EPERM : ENOSYS; result = -1; }
        else result = syscall(nr, first, last, flags);
    } else if (nr == SYS_openat) {
        int dir = va_arg(ap, int); const char *path = va_arg(ap, const char *);
        int flags = va_arg(ap, int); int perms = va_arg(ap, int);
        if (mode == 3 || mode == 4) { errno = mode == 3 ? EMFILE : ENOENT; result = -1; }
        else result = syscall(nr, dir, path, flags, perms);
    } else if (nr == SYS_getdents64) {
        int fd = va_arg(ap, int); void *buffer = va_arg(ap, void *); size_t len = va_arg(ap, size_t);
        read_calls++;
        if (mode == 9 && read_calls == 1) { errno = EINTR; result = -1; }
        else if (mode == 7 || (mode >= 10 && mode <= 14)) {
            unsigned char *p = buffer;
            memset(buffer, 0, 64);
            unsigned short record = mode == 10 ? 80 : mode == 14 ? 64 : 24;
            memcpy(p + 16, &record, 2);
            p[19] = '3';
            if (mode == 7) { p[16] = p[17] = 0; }
            if (mode == 12) memset(p + 19, '3', 5);
            if (mode == 13) p[19] = 'x';
            if (mode == 14) memset(p + 19, '9', 30);
            result = mode == 11 ? 8 : mode == 14 ? 64 : 24;
        } else result = syscall(nr, fd, buffer, len);
    } else if (nr == SYS_close) {
        int fd = va_arg(ap, int); close_calls++;
        if (mode == 15) { errno = EPERM; result = -1; }
        else result = syscall(nr, fd);
        if (mode == 8 && result == 0) { errno = EINTR; result = -1; }
    } else if (nr == SYS_write) {
        int fd = va_arg(ap, int); const char *s = va_arg(ap, const char *); size_t n = va_arg(ap, size_t);
        result = syscall(nr, fd, s, n);
    } else { _exit(95); }
    va_end(ap); return result;
}
/* These APIs must not appear in the exact extracted closure body. */
#define malloc forbidden_malloc
#define calloc forbidden_calloc
#define realloc forbidden_realloc
#define free forbidden_free
#define opendir forbidden_opendir
#define readdir forbidden_readdir
#define strtol forbidden_strtol
#define syscall proof_syscall
'''
SUFFIX = r'''
#undef syscall
#undef malloc
#undef calloc
#undef realloc
#undef free
#undef opendir
#undef readdir
#undef strtol
int main(int argc, char **argv) {
    assert(argc == 2); mode = atoi(argv[1]);
    struct rlimit rl; assert(getrlimit(RLIMIT_NOFILE, &rl) == 0);
    assert(rl.rlim_cur >= 1048576);
    for (int fd = 0; fd < 3; fd++) assert(fcntl(fd, F_GETFD) >= 0);
    low = open("/dev/null", O_RDONLY); assert(low >= 3);
    high = fcntl(low, F_DUPFD, 1000000); assert(high >= 1000000);
    if (mode != 5) { rl.rlim_cur = rl.rlim_max = 64; assert(setrlimit(RLIMIT_NOFILE, &rl) == 0); }
    if (mode == 6) assert(close(0) == 0);
    pthread_t thread;
    assert(pthread_barrier_init(&ready, NULL, 2) == 0);
    assert(pthread_create(&thread, NULL, holder, NULL) == 0);
    pthread_barrier_wait(&ready);
    struct timespec a, b; clock_gettime(CLOCK_MONOTONIC, &a);
    pid_t child = fork(); assert(child >= 0);
    if (child == 0) {
        guarded = 1;
        assert(pthread_mutex_trylock(&allocator_lock) == EBUSY);
        fastd_close_all_fds();
        if (expected_failure()) _exit(91);
        for (int fd = 0; fd < 3; fd++) {
            if (mode == 6 && fd == 0) assert(fcntl(fd, F_GETFD) == -1 && errno == EBADF);
            else assert(fcntl(fd, F_GETFD) >= 0);
        }
        assert(fcntl(low, F_GETFD) == -1 && errno == EBADF);
        assert(fcntl(high, F_GETFD) == -1 && errno == EBADF);
        if (mode == 8) assert(close_calls == 3);
        _exit(0);
    }
    int status; assert(waitpid(child, &status, 0) == child);
    assert(WIFEXITED(status));
    assert(WEXITSTATUS(status) == (expected_failure() ? EXIT_FAILURE : 0));
    clock_gettime(CLOCK_MONOTONIC, &b);
    double elapsed = b.tv_sec-a.tv_sec + (b.tv_nsec-a.tv_nsec)/1e9;
    assert(elapsed < 2.0);
    if (mode == 6) assert(dup2(1, 0) == 0);
    printf("PASS mode=%d high_fd=%d child_status=%d seconds=%.6f\n", mode, high, WEXITSTATUS(status), elapsed);
    return 0;
}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', default='ffdd-node-test-builder')
    args = parser.parse_args()
    patch = pathlib.Path(__file__).resolve().parents[1] / 'patches/fastd-close-fds.patch'
    lines = patch.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line == ' void fastd_close_all_fds(void) {')
    body = '\n'.join(line[1:] for line in lines[start:] if line.startswith((' ', '+')))
    body = body[:body.index('\n}') + 2]
    source = PREFIX + '\n' + body + '\n' + SUFFIX
    command = ['docker', 'run', '--rm', '-i', '--network', 'none',
               '--ulimit', 'nofile=1048576:1048576', '--entrypoint', 'sh', args.image,
               '-ec', 'umask 077; mkdir /tmp/fd-proof; '
               'cc -x c -O2 -Wall -Wextra -Werror -pthread -Wl,--wrap=malloc -o /tmp/fd-proof/test -; '
               'for mode in 0 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do timeout 5 /tmp/fd-proof/test "$mode"; done']
    subprocess.run(command, input=source, text=True, check=True, timeout=60)


if __name__ == '__main__':
    main()
