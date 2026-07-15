/*
 * Starship OS — starshipd spike (Phase 5 / ADR 0001)
 * Minimal agent-loop placeholder: dual-prefix subject print + optional
 * NATS ping via nats CLI if present. Not a full agent runtime yet.
 *
 * Build: make -C src/c/starshipd
 * Usage: starshipd [--agent NAME] [--once]
 */
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <time.h>
#include <signal.h>

static volatile int running = 1;

static void on_sig(int s) {
    (void)s;
    running = 0;
}

static void dual_subjects(const char *agent) {
    printf("starship.agent.%s.command.>\n", agent);
    printf("agnetic.agent.%s.command.>\n", agent);
    printf("starship.agent.%s.status\n", agent);
    printf("agnetic.agent.%s.status\n", agent);
    printf("starship.agent.%s.event.>\n", agent);
    printf("agnetic.agent.%s.event.>\n", agent);
}

int main(int argc, char **argv) {
    const char *agent = "proxy";
    int once = 0;
    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--agent") == 0 && i + 1 < argc) {
            agent = argv[++i];
        } else if (strcmp(argv[i], "--once") == 0) {
            once = 1;
        } else if (strcmp(argv[i], "-h") == 0 || strcmp(argv[i], "--help") == 0) {
            fprintf(stderr, "Usage: %s [--agent NAME] [--once]\n", argv[0]);
            return 0;
        }
    }

    signal(SIGINT, on_sig);
    signal(SIGTERM, on_sig);

    printf("starshipd: spike agent=%s pid=%d\n", agent, (int)getpid());
    printf("starshipd: dual-publish subjects (subscribe map):\n");
    dual_subjects(agent);
    printf("starshipd: control plane remains Python agent_daemon (ADR 0001)\n");

    /* Best-effort nats ping */
    if (access("/usr/local/bin/nats", X_OK) == 0 || access("/usr/bin/nats", X_OK) == 0) {
        int rc = system("nats account info >/dev/null 2>&1 || nats server check connection >/dev/null 2>&1 || true");
        (void)rc;
    }

    if (once) {
        printf("starshipd: --once complete\n");
        return 0;
    }

    int tick = 0;
    while (running) {
        tick++;
        time_t now = time(NULL);
        printf("starshipd: heartbeat tick=%d agent=%s t=%ld\n", tick, agent, (long)now);
        fflush(stdout);
        sleep(10);
    }
    printf("starshipd: stopped\n");
    return 0;
}
