# SPDX-License-Identifier: GPL-3.0-or-later
"""diaspora.log: what was tried and did not work, for people and agents.

The ledger says what became of a commit; this file says how it went: the
conflicts, what the agent did about them, which tests failed and with what
new failures, what a repair tried.  It is plain text, committed next to
diaspora.conf, one block per attempt, appended to and never rewritten:

    2026-11-18 run 20261118-052816 import sandia@<40 hex> Fix the thing
      cherry-pick: conflicts in src/a.C; agent ported it: re-did the fix
      test full-regression failed: 2 new failures: xyce/regr:a, xyce/regr:b
      agent repair: fixed - initialised the member
      test full-regression failed: 1 new failure: xyce/regr:a
      => failed

A commit that is tried again later shows what went wrong before, and the
agent is told, so it need not walk into the same wall twice.
"""

import os
import re

HEAD = re.compile(r"^(\d{4}-\d\d-\d\d) run (\S+) (import|export) "
                  r"([^@\s]+)@([0-9a-f]{40})(?: (.*))?$")
MAX_TESTS = 30           # new failures named in one line
HEADER = """\
# diaspora.log - imports that were tried and did not work, and how they
# went wrong.  Written by diaspora (github.com/kev-cam/diaspora): one block
# per attempt, appended, never rewritten.  The verdicts are in
# diaspora.ledger; `diaspora show <commit>` prints a commit's blocks.
"""


def block(date, run, direction, peer, sha, subject, lines, outcome):
    out = ["%s run %s %s %s@%s %s" % (date, run, direction, peer, sha,
                                      " ".join(subject.split()))]
    out += ["  " + " ".join(str(line).split()) for line in lines]
    out.append("  => " + outcome)
    return "\n".join(out) + "\n"


ERROR = re.compile(r"\b(error|Error|ERROR|fatal|Fatal)\b")
FAILED = re.compile(r"\b(FAILED|failed|Failed)\b")


def first_error(log, roots=()):
    """The first line of a test log that looks like an error (a compiler's
    error before a build tool's FAILED; not one of the commands, which the
    log heads with ###), with the workspace paths taken out (they mean
    nothing in another clone)."""
    try:
        with open(log, encoding="utf-8", errors="replace") as handle:
            lines = [line.strip() for line in handle
                     if not line.startswith("###")]
    except OSError:
        return ""
    for pattern in (ERROR, FAILED):
        for line in lines:
            if pattern.search(line):
                for root in roots:
                    if root:
                        line = line.replace(root.rstrip("/") + "/", "")
                return line[:300]
    return ""


def tests_line(level, names, detail=""):
    """One line for a failed test level: its new failures, by name."""
    if names:
        names = sorted(names)
        more = len(names) - MAX_TESTS
        return "test %s failed: %d new failure%s: %s%s" % (
            level, len(names), "" if len(names) == 1 else "s",
            ", ".join(names[:MAX_TESTS]),
            " (and %d more)" % more if more > 0 else "")
    return "test %s failed%s" % (level, ": " + detail if detail else "")


class Log:
    def __init__(self, text=""):
        self.blocks = []            # (sha, text)
        cur = None
        for line in text.splitlines(True):
            match = HEAD.match(line.rstrip("\n"))
            if match:
                cur = [match.group(5), line]
                self.blocks.append(cur)
            elif cur is not None and line.startswith((" ", "\t")):
                cur[1] += line
        self.blocks = [(sha, body) for sha, body in self.blocks]

    @classmethod
    def at(cls, repo, ref, path):
        """The log as committed on `ref` (empty if there is none)."""
        proc = repo.proc("show", "%s:%s" % (ref, path), check=False)
        return cls(proc.stdout if proc.returncode == 0 else "")

    def about(self, sha):
        """The blocks for one commit, oldest first."""
        return [body for got, body in self.blocks if got == sha]


def append(path, text):
    """Append blocks to the log file at `path` (created if missing)."""
    if not text:
        return
    try:
        with open(path, encoding="utf-8") as handle:
            old = handle.read()
    except FileNotFoundError:
        old = ""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        if not old:
            handle.write(HEADER)
        elif not old.endswith("\n"):
            handle.write("\n")
        handle.write("\n" + text)
