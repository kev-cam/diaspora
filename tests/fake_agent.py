#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""A stand-in agent for the tests ("plain" protocol, see diaspora/agent.py).

  triage  every commit in the prompt gets the answer in $FAKE_TRIAGE (JSON,
          {subject substring: {field: value}}), else bugfix/import
  port    takes the incoming side of every conflict, or does what
          $FAKE_PORT says: "superseded", "not-applicable", "failed"
  fix     runs the shell command in $FAKE_FIX and reports "fixed"

The mode goes to $FAKE_AGENT_LOG and the prompt to $FAKE_PROMPT_LOG.
"""
import json
import os
import re
import subprocess
import sys

mode = sys.argv[1]
prompt = sys.stdin.read()
with open(os.environ.get("FAKE_AGENT_LOG", os.devnull), "a") as log:
    log.write(mode + "\n")
with open(os.environ.get("FAKE_PROMPT_LOG", os.devnull), "a") as log:
    log.write(prompt + "\n")


def git(*args):
    return subprocess.run(["git"] + list(args), capture_output=True,
                          text=True).stdout


if mode == "triage":
    rules = json.loads(os.environ.get("FAKE_TRIAGE", "{}"))
    commits = []
    for sha, subject in re.findall(r"^### ([0-9a-f]{40})\n(.*)$", prompt,
                                   re.M):
        answer = {"sha": sha, "kind": "bugfix", "relevant": "yes",
                  "recommend": "import", "risk": "low",
                  "summary": "fake: " + subject, "reason": "looks useful"}
        for needle, fields in rules.items():
            if needle in subject:
                answer.update(fields)
        commits.append(answer)
    print(json.dumps({"commits": commits}))
elif mode == "port":
    what = os.environ.get("FAKE_PORT", "ported")
    if what != "ported":
        git("reset", "--hard", "HEAD")
        print(json.dumps({"status": what, "summary": "fake says " + what}))
    else:
        for path in git("diff", "--name-only",
                        "--diff-filter=U").splitlines():
            git("checkout", "--theirs", "--", path)
            git("add", "--", path)
        print("some chatter before the answer")
        print(json.dumps({"status": "ported",
                          "summary": "took the peer's side"}))
elif mode == "fix":
    command = os.environ.get("FAKE_FIX")
    if command:
        subprocess.run(command, shell=True, check=True)
        print(json.dumps({"status": "fixed", "summary": "ran the fix"}))
    else:
        print(json.dumps({"status": "failed", "summary": "no idea"}))
else:
    sys.exit(2)
