/*
 * Starship OS — C11 sandbox spike (ADR 0001)
 * Minimal fork+exec with timeout and PATH allowlist.
 * Full seccomp lands after baseline timing is validated.
 *
 * Build:  make -C src/c/sandbox_spike
 * Usage:  ./sandbox_run --timeout 5 -- echo hello
 */
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static volatile sig_atomic_t timed_out = 0;

static void on_alarm(int sig) {
    (void)sig;
    timed_out = 1;
}

static int path_allowed(const char *cmd) {
    /* Spike allowlist: only absolute paths under /bin /usr/bin or bare names */
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
    return 1; /* bare name: rely on PATH (spike only) */
}

static void usage(const char *argv0) {
    fprintf(stderr,
            "Usage: %s [--timeout SECS] -- COMMAND [ARGS...]\n"
            "  Starship OS C11 sandbox spike (ADR 0001)\n",
            argv0);
}

int main(int argc, char **argv) {
    int timeout = 5;
    int i = 1;
    while (i < argc) {
        if (strcmp(argv[i], "--timeout") == 0 && i + 1 < argc) {
            timeout = atoi(argv[++i]);
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
        /* child */
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
    fprintf(stderr, "sandbox: wall_ms=%.3f exit=%d\n",
            ms, WIFEXITED(status) ? WEXITSTATUS(status) : -1);

    if (WIFEXITED(status)) {
        return WEXITSTATUS(status);
    }
    if (WIFSIGNALED(status)) {
        return 128 + WTERMSIG(status);
    }
    return 1;
}
