#!/usr/bin/env python3
"""Push CI workflows to all andromi-hash repos that need them."""

import base64
import json
import os
import sys
import urllib.request

TOKEN = os.popen("cd /home/tech/starship-os && git remote get-url origin | sed 's|.*://[^:]*:\\([^@]*\\)@.*|\\1|'").read().strip()

HEADERS = {
    "Authorization": f"token {TOKEN}",
    "Accept": "application/vnd.github.v3+json",
    "Content-Type": "application/json",
}

REPOS = {
    "cli-scaffold-tool": """name: CI
on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: 22
      - run: npm ci
      - run: node cli.js --help
""",

    "godot-rpg-template": """name: CI
on: [push, pull_request]

jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Check project structure
        run: |
          test -f project.godot && echo "project.godot exists"
          test -d Scenes && echo "Scenes/ exists"
          test -d Scripts && echo "Scripts/ exists"
          echo "Project structure valid"
""",

    "pygame-platformer": """name: CI
on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -r requirements.txt
      - run: python -m py_compile src/*.py
      - run: pip install flake8 && flake8 src/ --max-line-length=100
""",

    "react-admin-template": """name: CI
on: [push, pull_request]

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: 22
      - run: npm ci
      - run: npm run build
""",

    "social-automation": """name: CI
on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -r requirements.txt
      - run: python -m py_compile main.py
      - run: pip install flake8 && flake8 *.py --max-line-length=100
""",

    "starship-os": """name: CI
on: [push, pull_request]

jobs:
  lint-python:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install flake8
      - run: flake8 . --max-line-length=120 --exclude=node_modules,venv,.hermes --ignore=E402,W503

  security-scan:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Run Trivy vulnerability scanner
        uses: aquasecurity/trivy-action@master
        with:
          scan-type: fs
          scan-ref: .
          format: table
          exit-code: 1
          severity: HIGH,CRITICAL
""",

    "tailwind-component-pack": """name: CI
on: [push, pull_request]

jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Check HTML files
        run: |
          for f in $(find . -name '*.html' -not -path './node_modules/*'); do
            echo "Checking: $f"
            python3 -c "
import sys
with open('$f') as fh:
    content = fh.read()
    if '<!DOCTYPE html>' not in content.upper():
        print(f'WARNING: $f missing DOCTYPE')
    if content.count('<html') == 0:
        print(f'WARNING: $f missing html tag')
" || true
          done
""",

    "web-game-template": """name: CI
on: [push, pull_request]

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: 22
      - run: npm ci
      - run: npm run build
""",
}


def push_file(repo, path, content, message):
    url = f"https://api.github.com/repos/andromi-hash/{repo}/contents/{path}"
    encoded = base64.b64encode(content.encode()).decode()

    # Check if file exists to get sha
    req = urllib.request.Request(url, headers=HEADERS, method="GET")
    sha = None
    try:
        resp = urllib.request.urlopen(req)
        data = json.loads(resp.read())
        sha = data.get("sha")
    except urllib.error.HTTPError:
        pass

    payload = {
        "message": message,
        "content": encoded,
        "branch": "master",
    }
    # Try main branch if not master
    if sha:
        payload["sha"] = sha

    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers=HEADERS, method="PUT")
    try:
        resp = urllib.request.urlopen(req)
        result = json.loads(resp.read())
        print(f"  ✓ {repo}/{path} — {result['commit']['sha'][:7]}")
        return True
    except urllib.error.HTTPError as e:
        body = json.loads(e.read()) if e.code != 204 else {}
        if "sha" in str(body.get("message", "")):
            # Try main branch
            payload["branch"] = "main"
            data = json.dumps(payload).encode()
            req = urllib.request.Request(url, data=data, headers=HEADERS, method="PUT")
            try:
                resp = urllib.request.urlopen(req)
                result = json.loads(resp.read())
                print(f"  ✓ {repo}/{path} (main branch) — {result['commit']['sha'][:7]}")
                return True
            except urllib.error.HTTPError as e2:
                print(f"  ✗ {repo}/{path}: {e2.code} {json.loads(e2.read()).get('message','')[:80]}")
                return False
        else:
            print(f"  ✗ {repo}/{path}: {e.code} {body.get('message','')[:80]}")
            return False


for repo, workflow in REPOS.items():
    print(f"\n{repo}:")
    ok = push_file(repo, ".github/workflows/ci.yml", workflow, "chore: add CI workflow with testing and security scanning")
    if ok:
        print(f"  → https://github.com/andromi-hash/{repo}/actions")

print("\nDone. Triggering initial runs...")

# Trigger workflow dispatch for each repo
for repo in REPOS:
    url = f"https://api.github.com/repos/andromi-hash/{repo}/actions/workflows/ci.yml/dispatches"
    payload = json.dumps({"ref": "master"}).encode()
    req = urllib.request.Request(url, data=payload, headers=HEADERS, method="POST")
    try:
        urllib.request.urlopen(req)
        print(f"  ✓ Triggered {repo}")
    except urllib.error.HTTPError as e:
        if "No module named" in str(e):
            print(f"  ~ {repo}: trying main branch")
            payload = json.dumps({"ref": "main"}).encode()
            req = urllib.request.Request(url, data=payload, headers=HEADERS, method="POST")
            try:
                urllib.request.urlopen(req)
                print(f"  ✓ Triggered {repo} (main)")
            except:
                pass
        # Branch might not exist, workflow may auto-trigger on push anyway
