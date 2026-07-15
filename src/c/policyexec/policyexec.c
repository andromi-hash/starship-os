/*
 * Starship OS — C11 policyexec spike (ADR 0001 / Phase 4)
 * Shared policy JSON gate for tools + commands; optional sandbox_run.
 *
 * Usage:
 *   policyexec [--policy PATH] [--role ROLE] check-tool NAME
 *   policyexec [--policy PATH] [--role ROLE] check-command CMD [ARGS...]
 *   policyexec [--policy PATH] [--role ROLE] run [--] CMD [ARGS...]
 *   policyexec [--policy PATH] list
 *
 * Exit: 0 allow/success · 1 deny · 2 usage/error
 */
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/wait.h>
#include <time.h>

#define MAX_ITEMS 128
#define MAX_ITEM_LEN 96
#define MAX_JSON (256 * 1024)

typedef struct {
    char deny_tools[MAX_ITEMS][MAX_ITEM_LEN];
    int n_deny_tools;
    char allow_tools[MAX_ITEMS][MAX_ITEM_LEN];
    int n_allow_tools;
    char block_cmds[MAX_ITEMS][MAX_ITEM_LEN];
    int n_block_cmds;
    char allow_cmds[MAX_ITEMS][MAX_ITEM_LEN];
    int n_allow_cmds;
    char role[64];
} Policy;

static void die(const char *msg) {
    fprintf(stderr, "policyexec: %s\n", msg);
    exit(2);
}

/* Skip whitespace */
static const char *skip_ws(const char *p) {
    while (*p && isspace((unsigned char)*p)) p++;
    return p;
}

/* Extract JSON string array values following "key": [ ... ] into out[] */
static int extract_string_array(const char *json, const char *key,
                                char out[][MAX_ITEM_LEN], int max_out) {
    char needle[128];
    snprintf(needle, sizeof(needle), "\"%s\"", key);
    const char *p = json;
    int count = 0;
    while ((p = strstr(p, needle)) != NULL) {
        p += strlen(needle);
        p = skip_ws(p);
        if (*p != ':') {
            continue;
        }
        p++;
        p = skip_ws(p);
        if (*p != '[') {
            continue;
        }
        p++;
        while (*p && *p != ']' && count < max_out) {
            p = skip_ws(p);
            if (*p == ']') break;
            if (*p == ',') { p++; continue; }
            if (*p != '"') { p++; continue; }
            p++;
            const char *start = p;
            while (*p && *p != '"') {
                if (*p == '\\' && p[1]) p += 2;
                else p++;
            }
            size_t len = (size_t)(p - start);
            if (len >= MAX_ITEM_LEN) len = MAX_ITEM_LEN - 1;
            memcpy(out[count], start, len);
            out[count][len] = '\0';
            count++;
            if (*p == '"') p++;
        }
        return count;
    }
    return 0;
}

/* Find role block and extract nested tools arrays (simple scan) */
static void load_role_tools(const char *json, const char *role, Policy *pol) {
    if (!role || !role[0]) return;
    char role_key[96];
    snprintf(role_key, sizeof(role_key), "\"%s\"", role);
    const char *rp = strstr(json, role_key);
    if (!rp) return;
    /* limit search to next 2KB after role key */
    char chunk[2048];
    size_t n = strlen(rp);
    if (n > sizeof(chunk) - 1) n = sizeof(chunk) - 1;
    memcpy(chunk, rp, n);
    chunk[n] = '\0';
    /* prefer role-specific arrays */
    int d = extract_string_array(chunk, "deny", pol->deny_tools, MAX_ITEMS);
    int a = extract_string_array(chunk, "allow", pol->allow_tools, MAX_ITEMS);
    if (d > 0) pol->n_deny_tools = d;
    if (a > 0) pol->n_allow_tools = a;
}

static int load_policy(const char *path, Policy *pol, const char *role) {
    memset(pol, 0, sizeof(*pol));
    if (role) {
        strncpy(pol->role, role, sizeof(pol->role) - 1);
    }
    FILE *f = fopen(path, "r");
    if (!f) {
        fprintf(stderr, "policyexec: cannot open %s\n", path);
        return -1;
    }
    char *buf = malloc(MAX_JSON);
    if (!buf) { fclose(f); return -1; }
    size_t n = fread(buf, 1, MAX_JSON - 1, f);
    buf[n] = '\0';
    fclose(f);

    /* top-level tools + commands */
    pol->n_deny_tools = extract_string_array(buf, "deny", pol->deny_tools, MAX_ITEMS);
    /* first "deny" may be tools; also get blocklist */
    pol->n_block_cmds = extract_string_array(buf, "blocklist", pol->block_cmds, MAX_ITEMS);
    pol->n_allow_cmds = extract_string_array(buf, "allowlist", pol->allow_cmds, MAX_ITEMS);

    /* re-parse tools.allow more carefully: look under "tools" */
    const char *tools = strstr(buf, "\"tools\"");
    if (tools) {
        char tchunk[4096];
        size_t tn = strlen(tools);
        if (tn > sizeof(tchunk) - 1) tn = sizeof(tchunk) - 1;
        memcpy(tchunk, tools, tn);
        tchunk[tn] = '\0';
        /* stop at "commands" or "roles" */
        char *cut = strstr(tchunk, "\"commands\"");
        if (!cut) cut = strstr(tchunk, "\"roles\"");
        if (cut) *cut = '\0';
        pol->n_deny_tools = extract_string_array(tchunk, "deny", pol->deny_tools, MAX_ITEMS);
        pol->n_allow_tools = extract_string_array(tchunk, "allow", pol->allow_tools, MAX_ITEMS);
    }

    if (role && role[0]) {
        load_role_tools(buf, role, pol);
    }

    free(buf);
    return 0;
}

static int in_list(char list[][MAX_ITEM_LEN], int n, const char *name) {
    for (int i = 0; i < n; i++) {
        if (strcmp(list[i], name) == 0) return 1;
    }
    return 0;
}

static const char *basename_of(const char *path) {
    const char *b = strrchr(path, '/');
    return b ? b + 1 : path;
}

/* 0 = allow, 1 = deny */
static int check_tool(const Policy *pol, const char *name, char *reason, size_t rlen) {
    if (pol->n_allow_tools > 0) {
        if (!in_list((char(*)[MAX_ITEM_LEN])pol->allow_tools, pol->n_allow_tools, name)) {
            snprintf(reason, rlen, "tool '%s' not in allowlist", name);
            return 1;
        }
    }
    if (in_list((char(*)[MAX_ITEM_LEN])pol->deny_tools, pol->n_deny_tools, name)) {
        snprintf(reason, rlen, "tool '%s' denied by policy", name);
        return 1;
    }
    snprintf(reason, rlen, "tool '%s' allowed", name);
    return 0;
}

static int check_command(const Policy *pol, const char *cmd0, char *reason, size_t rlen) {
    const char *base = basename_of(cmd0);
    if (pol->n_allow_cmds > 0) {
        if (!in_list((char(*)[MAX_ITEM_LEN])pol->allow_cmds, pol->n_allow_cmds, base) &&
            !in_list((char(*)[MAX_ITEM_LEN])pol->allow_cmds, pol->n_allow_cmds, cmd0)) {
            snprintf(reason, rlen, "command '%s' not in allowlist", base);
            return 1;
        }
    }
    if (in_list((char(*)[MAX_ITEM_LEN])pol->block_cmds, pol->n_block_cmds, base) ||
        in_list((char(*)[MAX_ITEM_LEN])pol->block_cmds, pol->n_block_cmds, cmd0)) {
        snprintf(reason, rlen, "command '%s' blocked by policy", base);
        return 1;
    }
    snprintf(reason, rlen, "command '%s' allowed", base);
    return 0;
}

static void print_json_result(int allow, const char *reason) {
    printf("{\"allow\":%s,\"reason\":\"%s\"}\n", allow ? "true" : "false", reason);
}

static int run_sandbox(char **argv) {
    /* Prefer PATH sandbox_run */
    char *sr = getenv("STARSHIP_SANDBOX_RUN");
    char pathbuf[512];
    if (!sr || !sr[0]) {
        if (access("/opt/starship/bin/sandbox_run", X_OK) == 0) {
            sr = "/opt/starship/bin/sandbox_run";
        } else if (access("src/c/sandbox_spike/sandbox_run", X_OK) == 0) {
            sr = "src/c/sandbox_spike/sandbox_run";
        } else {
            /* direct exec if no sandbox binary */
            execvp(argv[0], argv);
            perror("execvp");
            return 127;
        }
    }
    /* build argv: sandbox_run --timeout 30 -- cmd... */
    char *nargv[256];
    int i = 0;
    nargv[i++] = sr;
    nargv[i++] = "--timeout";
    nargv[i++] = "30";
    nargv[i++] = "--";
    for (int j = 0; argv[j] && i < 254; j++) {
        nargv[i++] = argv[j];
    }
    nargv[i] = NULL;
    execvp(nargv[0], nargv);
    /* fallback path */
    snprintf(pathbuf, sizeof(pathbuf), "%s", sr);
    execv(pathbuf, nargv);
    perror("sandbox_run");
    return 127;
}

static void usage(const char *a0) {
    fprintf(stderr,
        "Usage: %s [--policy PATH] [--role ROLE] <command>\n"
        "  check-tool NAME\n"
        "  check-command CMD [ARGS...]\n"
        "  run [--] CMD [ARGS...]\n"
        "  list\n", a0);
}

static const char *default_policy_path(void) {
    const char *e = getenv("STARSHIP_POLICY");
    if (e && e[0]) return e;
    if (access("/etc/starship/policy.json", R_OK) == 0) return "/etc/starship/policy.json";
    if (access("config/policy.default.json", R_OK) == 0) return "config/policy.default.json";
    return "config/policy.default.json";
}

int main(int argc, char **argv) {
    const char *policy_path = NULL;
    const char *role = getenv("STARSHIP_FLEET_ROLES");
    /* take first role if comma-separated */
    char rolebuf[64] = {0};
    if (role && role[0]) {
        strncpy(rolebuf, role, sizeof(rolebuf) - 1);
        char *c = strchr(rolebuf, ',');
        if (c) *c = '\0';
        role = rolebuf;
    } else {
        role = getenv("STARSHIP_FLEET_TEAM");
    }

    int i = 1;
    while (i < argc) {
        if (strcmp(argv[i], "--policy") == 0 && i + 1 < argc) {
            policy_path = argv[++i];
            i++;
            continue;
        }
        if (strcmp(argv[i], "--role") == 0 && i + 1 < argc) {
            role = argv[++i];
            i++;
            continue;
        }
        if (strcmp(argv[i], "-h") == 0 || strcmp(argv[i], "--help") == 0) {
            usage(argv[0]);
            return 0;
        }
        break;
    }
    if (!policy_path) policy_path = default_policy_path();
    if (i >= argc) {
        usage(argv[0]);
        return 2;
    }

    Policy pol;
    if (load_policy(policy_path, &pol, role) != 0) {
        return 2;
    }

    const char *cmd = argv[i++];
    char reason[256];

    if (strcmp(cmd, "list") == 0) {
        printf("policy=%s role=%s\n", policy_path, role ? role : "(none)");
        printf("deny_tools=%d allow_tools=%d block_cmds=%d allow_cmds=%d\n",
               pol.n_deny_tools, pol.n_allow_tools, pol.n_block_cmds, pol.n_allow_cmds);
        for (int k = 0; k < pol.n_deny_tools; k++) printf("  deny_tool: %s\n", pol.deny_tools[k]);
        for (int k = 0; k < pol.n_block_cmds; k++) printf("  block_cmd: %s\n", pol.block_cmds[k]);
        return 0;
    }

    if (strcmp(cmd, "check-tool") == 0) {
        if (i >= argc) die("check-tool requires NAME");
        int deny = check_tool(&pol, argv[i], reason, sizeof(reason));
        print_json_result(!deny, reason);
        return deny ? 1 : 0;
    }

    if (strcmp(cmd, "check-command") == 0) {
        if (i >= argc) die("check-command requires CMD");
        int deny = check_command(&pol, argv[i], reason, sizeof(reason));
        print_json_result(!deny, reason);
        return deny ? 1 : 0;
    }

    if (strcmp(cmd, "run") == 0) {
        if (i < argc && strcmp(argv[i], "--") == 0) i++;
        if (i >= argc) die("run requires CMD");
        int deny = check_command(&pol, argv[i], reason, sizeof(reason));
        if (deny) {
            print_json_result(0, reason);
            return 1;
        }
        /* also deny if shell tool blocked for role when invoking shell-like */
        struct timespec t0, t1;
        clock_gettime(CLOCK_MONOTONIC, &t0);
        pid_t pid = fork();
        if (pid < 0) { perror("fork"); return 2; }
        if (pid == 0) {
            run_sandbox(&argv[i]);
            _exit(127);
        }
        int st = 0;
        waitpid(pid, &st, 0);
        clock_gettime(CLOCK_MONOTONIC, &t1);
        double ms = (t1.tv_sec - t0.tv_sec) * 1000.0 +
                    (t1.tv_nsec - t0.tv_nsec) / 1e6;
        int code = WIFEXITED(st) ? WEXITSTATUS(st) : 1;
        fprintf(stderr, "policyexec: wall_ms=%.3f exit=%d\n", ms, code);
        return code;
    }

    usage(argv[0]);
    return 2;
}
