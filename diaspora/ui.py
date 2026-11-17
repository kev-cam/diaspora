# SPDX-License-Identifier: GPL-3.0-or-later
"""Terminal output and the interactive pick list."""

import os
import re
import shutil
import subprocess
import sys

from . import ledger as L

HELP = """\
  <Enter>        go: {verb} the commits marked [x]
  1 3-5          toggle those commits
  a / n          mark all / none
  s N            show commit N (message and file list)
  d N            show the full diff of commit N
  r N.. [why]    {reject}
  l N..          later: hide until asked for with --all
  p              print the list again
  q              quit without {verb}ing (marks made with r and l are kept)
"""


class UI:
    def __init__(self, color=None):
        if color is None:
            color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
        self.color = color
        self.width = shutil.get_terminal_size((100, 24)).columns

    # -- plain output -----------------------------------------------------------
    def paint(self, text, code):
        return "\033[%sm%s\033[0m" % (code, text) if self.color else text

    def say(self, msg=""):
        print(msg, flush=True)

    def step(self, msg):
        print("      " + msg, flush=True)

    def warn(self, msg):
        print(self.paint("diaspora: " + msg, "33"), file=sys.stderr,
              flush=True)

    def item(self, num, total, item):
        print("[%d/%d] %s %s  %s" % (
            num, total, item["peer"], self.paint(item["sha"][:10], "36"),
            item["subject"][:self.width - 30]), flush=True)

    def confirm(self, question, default=True):
        try:
            answer = input("%s [%s] " % (question,
                                         "Y/n" if default else "y/N"))
        except EOFError:
            print()
            return False
        answer = answer.strip().lower()
        return default if not answer else answer in ("y", "yes")

    # -- the list -----------------------------------------------------------------
    def table(self, commits, marked):
        if not commits:
            return
        kind_w = max(len(c.kind) for c in commits)
        peer_w = max(len(c.peer) for c in commits)
        many_peers = len({c.peer for c in commits}) > 1
        for num, commit in enumerate(commits, 1):
            if commit.status == L.FAILED:
                mark = self.paint("[!]", "31")
            elif commit.status == L.DEFERRED:
                mark = "[l]" if commit.sha not in marked else "[x]"
            else:
                mark = self.paint("[x]", "32") if commit.sha in marked \
                    else "[ ]"
            size = "+%d -%d" % (commit.added, commit.deleted)
            head = "%3d %s %s%s %s %-*s %-10s " % (
                num, mark,
                (commit.peer.ljust(peer_w) + " ") if many_peers else "",
                self.paint(commit.short, "36"), commit.date, kind_w,
                commit.kind, size)
            plain = len(re.sub(r"\033\[[0-9;]*m", "", head))
            print(head + commit.subject[:max(20, self.width - plain)])
            notes = []
            if commit.status != "pending":
                notes.append("%s earlier%s" % (
                    commit.status, ": " + commit.note if commit.note else ""))
            if commit.source == "agent":
                notes.append("agent%s: %s" % (
                    ", risk " + commit.risk if commit.risk else "",
                    commit.reason or commit.summary))
            elif commit.why and commit.sha not in marked \
                    and commit.status == "pending" \
                    and not commit.why.endswith("by default"):
                notes.append(commit.why)
            for note in notes:
                print(self.paint(" " * 8 + note[:self.width - 9], "2"))

    def show(self, repo, commit, full=False):
        args = ["git", "show", "--format=fuller", commit.sha]
        if not full:
            args.insert(2, "--stat")
        if not sys.stdout.isatty():
            args.insert(1, "--no-pager")
        subprocess.run(args, cwd=repo.top)
        if commit.source == "agent":
            print("\nagent triage: %s / relevant: %s / recommend: %s / "
                  "risk: %s\n  %s\n  %s" % (
                      commit.kind, commit.relevant, commit.recommend,
                      commit.risk, commit.summary, commit.reason))

    def choose(self, repo, commits, direction="import"):
        """The interactive pick list.

        Returns (commits to take, ledger entries for r/l marks), or
        (None, entries) when the user quits.
        """
        verb = direction
        reject = L.REJECTED if direction == "import" else L.WITHHELD
        commits = list(commits)
        marked = {c.sha for c in commits if c.disposition == "import"
                  and c.status == "pending"}
        entries = []
        self.table(commits, marked)
        while True:
            count = sum(1 for c in commits if c.sha in marked)
            try:
                line = input("\n%s %d of %d?  [Enter = go, numbers toggle, "
                             "? = help] " % (verb.capitalize(), count,
                                             len(commits))).strip()
            except EOFError:
                print()
                return None, entries
            word, _, rest = line.partition(" ")
            word = word.lower()
            if word in ("", "g", "go", "y", "yes"):
                return [c for c in commits if c.sha in marked], entries
            if word in ("q", "quit", "exit"):
                return None, entries
            if word in ("?", "h", "help"):
                print(HELP.format(verb=verb, reject=(
                    "reject for good: never suggest again"
                    if direction == "import"
                    else "withhold: never offer to this peer")))
            elif word == "a":
                marked = {c.sha for c in commits}
                self.table(commits, marked)
            elif word == "n":
                marked = set()
                self.table(commits, marked)
            elif word == "p":
                self.table(commits, marked)
            elif word in ("s", "d", "r", "l"):
                nums, why = self._numbers(rest, len(commits))
                if not nums:
                    print("which commit? e.g. `%s 3`" % word)
                elif word in ("s", "d"):
                    for num in nums[:1]:
                        self.show(repo, commits[num - 1], full=word == "d")
                else:
                    verdict = reject if word == "r" else L.DEFERRED
                    gone = [commits[n - 1] for n in nums]
                    for commit in gone:
                        entries.append(L.entry(
                            commit.peer, commit.sha, verdict, commit.subject,
                            commit.kind, note=why or None,
                            direction=direction))
                        marked.discard(commit.sha)
                    commits = [c for c in commits if c not in gone]
                    print("%s: %s" % (verdict, ", ".join(
                        c.short for c in gone)))
                    self.table(commits, marked)
            else:
                nums, junk = self._numbers(line, len(commits))
                if not nums or junk:
                    print("not understood: %r (? for help)" % line)
                    continue
                for num in nums:
                    marked ^= {commits[num - 1].sha}
                self.table(commits, marked)

    @staticmethod
    def _numbers(text, limit):
        """Leading `1 3-5` style numbers of text; returns (numbers, rest)."""
        nums, words = [], text.split()
        while words:
            m = re.fullmatch(r"(\d+)(?:-(\d+))?,?", words[0])
            if not m:
                break
            lo = int(m.group(1))
            hi = int(m.group(2) or lo)
            nums += [n for n in range(lo, hi + 1)
                     if 1 <= n <= limit and n not in nums]
            words.pop(0)
        return nums, " ".join(words)
