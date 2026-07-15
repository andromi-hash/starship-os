/*
 * Starship OS — C11 sandbox (ADR 0001)
 * fork/clone+exec with timeout, path deny, seccomp-bpf, optional PID/mount NS.
 *
 * Build:  make -C src/c/sandbox_spike
 * Usage:  ./sandbox_run [--timeout SECS] [--no-seccomp] [--no-ns] -- CMD...
 */
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include <errno.h>
#include <sched.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#if defined(HAVE_SECCOMP) && HAVE_SECCOMP
#include <seccomp.h>
#define SANDBOX_HAS_SECCOMP 1
#else
#define SANDBOX_HAS_SECCOMP 0
#endif

static volatile sig_atomic_t timed_out = 0;

static void on_alarm(int sig) {
    (void)sig;
    timed_out = 1;
}

static int path_allowed(const char *cmd) {
    static const char *blocked[] = {
        "mount", "umount", "reboot", "shutdown", "mkfs", "dd", NULL
    };
    const char *base = strrchr(cmd, '/');
    base = base ? base + 1 : cmd;
    for (int i = 0; blocked[i]; i++) {
        if (strcmp(base, blocked[i]) == 0) {
            return 0;
        }
    }
    if (cmd[0] == '/') {
        if (strncmp(cmd, "/bin/", 5) == 0 ||
            strncmp(cmd, "/usr/bin/", 9) == 0 ||
            strncmp(cmd, "/usr/local/bin/", 15) == 0) {
            return 1;
        }
        return 0;
    }
    return 1;
}

#if SANDBOX_HAS_SECCOMP
static int apply_seccomp(void) {
    scmp_filter_ctx ctx = seccomp_init(SCMP_ACT_ERRNO(EPERM));
    if (!ctx) {
        return -1;
    }

    static const int allow[] = {
        SCMP_SYS(read), SCMP_SYS(write), SCMP_SYS(close),
        SCMP_SYS(fstat), SCMP_SYS(newfstatat), SCMP_SYS(statx),
        SCMP_SYS(lseek), SCMP_SYS(mmap), SCMP_SYS(mprotect), SCMP_SYS(munmap),
        SCMP_SYS(brk), SCMP_SYS(rt_sigaction), SCMP_SYS(rt_sigprocmask),
        SCMP_SYS(rt_sigreturn), SCMP_SYS(ioctl), SCMP_SYS(access),
        SCMP_SYS(faccessat), SCMP_SYS(faccessat2), SCMP_SYS(pipe), SCMP_SYS(pipe2),
        SCMP_SYS(dup), SCMP_SYS(dup2), SCMP_SYS(dup3),
        SCMP_SYS(getpid), SCMP_SYS(gettid), SCMP_SYS(getuid), SCMP_SYS(geteuid),
        SCMP_SYS(getgid), SCMP_SYS(getegid), SCMP_SYS(getppid), SCMP_SYS(getcwd),
        SCMP_SYS(fcntl), SCMP_SYS(arch_prctl), SCMP_SYS(set_tid_address),
        SCMP_SYS(set_robust_list), SCMP_SYS(rseq), SCMP_SYS(prlimit64),
        SCMP_SYS(getrandom), SCMP_SYS(clock_gettime), SCMP_SYS(clock_nanosleep),
        SCMP_SYS(nanosleep), SCMP_SYS(exit), SCMP_SYS(exit_group),
        SCMP_SYS(execve), SCMP_SYS(execveat), SCMP_SYS(openat), SCMP_SYS(open),
        SCMP_SYS(readlink), SCMP_SYS(readlinkat), SCMP_SYS(sysinfo), SCMP_SYS(uname),
        SCMP_SYS(futex), SCMP_SYS(getdents64), SCMP_SYS(pread64), SCMP_SYS(pwrite64),
        /* clone/fork needed if dynamic linker or runtime forks — keep minimal */
        SCMP_SYS(clone), SCMP_SYS(clone3), SCMP_SYS(fork), SCMP_SYS(vfork),
        SCMP_SYS(wait4), SCMP_SYS(waitid),
    };

    for (size_t i = 0; i < sizeof(allow) / sizeof(allow[0]); i++) {
        seccomp_rule_add(ctx, SCMP_ACT_ALLOW, allow[i], 0);
    }

    if (seccomp_load(ctx) < 0) {
        seccomp_release(ctx);
        return -1;
    }
    seccomp_release(ctx);
    return 0;
}
#endif

/* Best-effort namespaces (soft-fail without CAP_SYS_ADMIN). */
static int enter_namespaces(int use_ns) {
    int flags = 0;
    if (!use_ns) {
        return 0;
    }
#ifdef CLONE_NEWPID
    /* NEWPID after fork only affects children of this process; re-fork below. */
#endif
#ifdef CLONE_NEWNS
    if (unshare(CLONE_NEWNS) == 0) {
        flags |= 1;
    }
#endif
#ifdef CLONE_NEWPID
    if (unshare(CLONE_NEWPID) == 0) {
        flags |= 2;
        /* Must fork so exec'd process is PID 1 in new PID NS */
        pid_t g = fork();
        if (g < 0) {
            return flags;
        }
        if (g > 0) {
            int st = 0;
            waitpid(g, &st, 0);
            _exit(WIFEXITED(st) ? WEXITSTATUS(st) : 1);
        }
        /* grandchild continues to seccomp + exec */
    }
#endif
    return flags;
}

static void usage(const char *argv0) {
    fprintf(stderr,
            "Usage: %s [--timeout SECS] [--no-seccomp] [--no-ns] -- COMMAND [ARGS...]\n"
            "  Starship OS C11 sandbox (ADR 0001)\n"
            "  seccomp: %s\n",
            argv0,
            SANDBOX_HAS_SECCOMP ? "built-in (default on)" : "not built");
}

int main(int argc, char **argv) {
    int timeout = 5;
    int use_seccomp = SANDBOX_HAS_SECCOMP;
    int use_ns = 1;
    int i = 1;
    while (i < argc) {
        if (strcmp(argv[i], "--timeout") == 0 && i + 1 < argc) {
            timeout = atoi(argv[++i]);
            i++;
            continue;
        }
        if (strcmp(argv[i], "--no-seccomp") == 0) {
            use_seccomp = 0;
            i++;
            continue;
        }
        if (strcmp(argv[i], "--no-ns") == 0) {
            use_ns = 0;
            i++;
            continue;
        }
        if (strcmp(argv[i], "--") == 0) {
            i++;
            break;
        }
        if (strcmp(argv[i], "-h") == 0 || strcmp(argv[i], "--help") == 0) {
            usage(argv[0]);
            return 0;
        }
        break;
    }
    if (i >= argc) {
        usage(argv[0]);
        return 2;
    }

    char **cmd = &argv[i];
    if (!path_allowed(cmd[0])) {
        fprintf(stderr, "sandbox: denied command: %s\n", cmd[0]);
        return 126;
    }

    struct timespec t0, t1;
    clock_gettime(CLOCK_MONOTONIC, &t0);

    pid_t pid = fork();
    if (pid < 0) {
        perror("fork");
        return 1;
    }
    if (pid == 0) {
        int ns_flags = enter_namespaces(use_ns);
        (void)ns_flags;
#if SANDBOX_HAS_SECCOMP
        if (use_seccomp) {
            if (apply_seccomp() != 0) {
                fprintf(stderr, "sandbox: seccomp load failed (fail closed)\n");
                _exit(125);
            }
        }
#else
        (void)use_seccomp;
#endif
        execvp(cmd[0], cmd);
        perror("execvp");
        _exit(127);
    }

    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = on_alarm;
    sigaction(SIGALRM, &sa, NULL);
    alarm((unsigned)timeout);

    int status = 0;
    if (waitpid(pid, &status, 0) < 0) {
        if (errno == EINTR && timed_out) {
            kill(pid, SIGKILL);
            waitpid(pid, &status, 0);
            fprintf(stderr, "sandbox: timeout after %ds\n", timeout);
            return 124;
        }
        perror("waitpid");
        return 1;
    }
    alarm(0);

    clock_gettime(CLOCK_MONOTONIC, &t1);
    double ms = (t1.tv_sec - t0.tv_sec) * 1000.0 +
                (t1.tv_nsec - t0.tv_nsec) / 1e6;
    fprintf(stderr, "sandbox: wall_ms=%.3f exit=%d seccomp=%d ns=%d\n",
            ms, WIFEXITED(status) ? WEXITSTATUS(status) : -1, use_seccomp, use_ns);

    if (WIFEXITED(status)) {
        return WEXITSTATUS(status);
    }
    if (WIFSIGNALED(status)) {
        return 128 + WTERMSIG(status);
    }
    return 1;
}
