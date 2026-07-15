/*
 * Starship OS — heald spike (Phase 5 / ADR 0001)
 * Self-healing watchdog placeholder: check process liveness by name/pidfile,
 * log recoveries. Full auto-restart policy stays in Python healer for now.
 *
 * Build: make -C src/c/heald
 * Usage: heald [--check NAME]... [--once] [--interval SEC]
 */
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <signal.h>
#include <time.h>
#include <dirent.h>
#include <ctype.h>

static volatile int running = 1;

static void on_sig(int s) {
    (void)s;
    running = 0;
}

/* Return 1 if a process cmdline contains name */
static int proc_alive(const char *name) {
    DIR *d = opendir("/proc");
    if (!d) return 0;
    struct dirent *ent;
    int found = 0;
    char path[256];
    char buf[512];
    while ((ent = readdir(d)) != NULL) {
        if (!isdigit((unsigned char)ent->d_name[0])) continue;
        snprintf(path, sizeof(path), "/proc/%s/cmdline", ent->d_name);
        FILE *f = fopen(path, "r");
        if (!f) continue;
        size_t n = fread(buf, 1, sizeof(buf) - 1, f);
        fclose(f);
        if (n == 0) continue;
        buf[n] = '\0';
        /* cmdline is NUL-separated */
        for (size_t i = 0; i < n; i++) {
            if (buf[i] == '\0') buf[i] = ' ';
        }
        if (strstr(buf, name) != NULL) {
            found = 1;
            break;
        }
    }
    closedir(d);
    return found;
}

int main(int argc, char **argv) {
    const char *checks[32];
    int ncheck = 0;
    int once = 0;
    int interval = 15;

    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--check") == 0 && i + 1 < argc) {
            if (ncheck < 32) checks[ncheck++] = argv[++i];
        } else if (strcmp(argv[i], "--once") == 0) {
            once = 1;
        } else if (strcmp(argv[i], "--interval") == 0 && i + 1 < argc) {
            interval = atoi(argv[++i]);
            if (interval < 1) interval = 1;
        } else if (strcmp(argv[i], "-h") == 0 || strcmp(argv[i], "--help") == 0) {
            fprintf(stderr,
                    "Usage: %s [--check NAME]... [--once] [--interval SEC]\n"
                    "  Default checks: nats-server, agent_daemon, staragent\n",
                    argv[0]);
            return 0;
        }
    }
    if (ncheck == 0) {
        checks[ncheck++] = "nats-server";
        checks[ncheck++] = "agent_daemon";
        checks[ncheck++] = "staragent";
    }

    signal(SIGINT, on_sig);
    signal(SIGTERM, on_sig);

    printf("heald: spike pid=%d checks=%d interval=%ds\n", (int)getpid(), ncheck, interval);
    printf("heald: Python healer remains control plane; this is a liveness probe spike\n");

    int recoveries = 0;
    do {
        for (int i = 0; i < ncheck; i++) {
            int ok = proc_alive(checks[i]);
            if (ok) {
                printf("heald: OK   %s\n", checks[i]);
            } else {
                printf("heald: DOWN %s  (would recover — spike logs only)\n", checks[i]);
                recoveries++;
            }
        }
        fflush(stdout);
        if (once) break;
        sleep((unsigned)interval);
    } while (running);

    printf("heald: stopped recoveries_logged=%d\n", recoveries);
    /* exit 0 even if down in spike mode (observability only) */
    return 0;
}
