# SPDX-License-Identifier: GPL-3.0-or-later
"""Auto-import: a staged plan of what to import, carried out once agreed.

The plan takes the commits diaspora would suggest anyway and orders them by
how much can go wrong, so that the safe ones land first and a stage that
fails cannot hold them back:

  1. suggested commits that touch none of our own files
  2. suggested commits that touch files we changed too (or whose overlap is
     not known, or that the agent calls high-risk)
  3. with `review`, commits listed for review that the agent says to import

Each stage is split into runs of at most `size` commits.  Every run is an
ordinary import run: each commit is applied and must pass the test levels
diaspora.conf requires; what passes is promoted to the target before the
next run starts, what fails (its tests, or a conflict the agent could not
resolve) is left out and offered again.  The plan stops when a run cannot
go on by itself (the tests cannot run, or fail on the target without the
import): finish that run, then ask for a plan again; what was imported is
no longer in it.

Commits that failed before, or were tried and did not work, need someone
to look at them (Retry), so they are never in a plan; nor are deferred,
ignored or not-applicable ones.
"""

import fcntl
import json
import os
import textwrap
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from . import ledger as L
from . import tried as T
from .session import Session
from .workspace import cache_home

SAFE, OURS, REVIEW = "safe", "ours", "review"
TITLES = {
    SAFE: "suggested, touching none of our files",
    OURS: "suggested, touching files we changed too",
    REVIEW: "listed for review, the agent says to import",
}


@dataclass
class Stage:
    kind: str                  # SAFE, OURS or REVIEW
    part: int                  # 1.. within its kind
    parts: int
    commits: list = field(default_factory=list)

    @property
    def title(self):
        return TITLES[self.kind] + (
            " (%d of %d)" % (self.part, self.parts) if self.parts > 1 else "")


@dataclass
class Plan:
    target: str
    stages: list
    left: OrderedDict          # why not in the plan -> [commit]
    levels: dict               # peer -> the test levels its imports pass
    why: dict                  # sha -> why it is in the stage it is in

    @property
    def commits(self):
        return [c for s in self.stages for c in s.commits]

    def as_dict(self):
        return {
            "target": self.target,
            "stages": [{"kind": s.kind, "title": s.title,
                        "commits": [dict(c.as_dict(), short=c.short,
                                         plan_why=self.why.get(c.sha, ""))
                                    for c in s.commits]}
                       for s in self.stages],
            "left": [{"why": why, "count": len(cs),
                      "commits": [c.short for c in cs]}
                     for why, cs in self.left.items()],
            "levels": self.levels,
        }


def _matches(dep, commits):
    dep = dep.lower()
    return [c for c in commits if len(dep) >= 7 and c.sha.startswith(dep)]


def in_run(repo):
    """The commits of the run in progress, if there is one."""
    try:
        with open(os.path.join(repo.state_dir(), "session.json"),
                  encoding="utf-8") as handle:
            return {i["sha"] for i in json.load(handle)["items"]}
    except (OSError, ValueError, KeyError, TypeError):
        return set()


class Lock:
    """One `diaspora auto` at a time in a repository, held until it ends.

    A run only shows (session.json) once the scan is over, which can take
    minutes; without the lock two nightly runs could both start.
    """

    def __init__(self, repo):
        self.path = os.path.join(repo.state_dir(), "auto.lock")
        self.handle = None
        self.holder = ""

    def take(self):
        handle = open(self.path, "a+", encoding="utf-8")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.seek(0)
            self.holder = handle.read().strip()
            handle.close()
            return False
        handle.seek(0)
        handle.truncate()
        handle.write("%d\n" % os.getpid())
        handle.flush()
        self.handle = handle
        return True

    def release(self):
        if self.handle:
            self.handle.close()
            self.handle = None

    def __enter__(self):
        return self.take()

    def __exit__(self, *exc):
        self.release()


KEEP_LOGS = 30


def cron_log(repo):
    """A new log file for an unattended run; the oldest beyond KEEP_LOGS go."""
    where = os.path.join(cache_home(repo), "auto")
    os.makedirs(where, exist_ok=True)
    old = sorted(f for f in os.listdir(where) if f.endswith(".log"))
    for name in old[:max(0, len(old) - KEEP_LOGS + 1)]:
        try:
            os.unlink(os.path.join(where, name))
        except OSError:
            pass
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(where, stamp + ".log")
    num = 1
    while os.path.exists(path):
        num += 1
        path = os.path.join(where, "%s-%d.log" % (stamp, num))
    return path


def outcome(repo, conf, plan):
    """{sha: what became of it} for the plan's commits, from the ledger."""
    ledger = L.Ledger.at(repo, plan.target, conf.ledger).latest("import")
    out = {}
    for c in plan.commits:
        entry = ledger.get(c.sha) or {}
        out[c.sha] = entry.get("verdict") or "not run"
    return out


def propose(repo, conf, target, commits, size=8, review=False):
    """The plan for these scanned candidates (in the scan's order)."""
    log = T.Log.at(repo, target, conf.log)
    left = OrderedDict()
    why = {}
    kind_of = {}

    def leave(reason, commit):
        left.setdefault(reason, []).append(commit)

    running = in_run(repo)
    for c in commits:
        if c.sha in running:
            leave("in the run in progress", c)
        elif c.status == L.FAILED:
            leave("failed before (needs Retry)", c)
        elif log.about(c.sha):
            leave("tried before and did not work (needs Retry)", c)
        elif c.status == L.DEFERRED:
            leave("deferred", c)
        elif c.disposition == "ignore":
            leave("ignored by policy", c)
        elif c.disposition == "review":
            if review and c.source == "agent" and c.recommend == "import":
                kind_of[c.sha] = REVIEW
                why[c.sha] = "the agent: " + (c.reason or c.summary or
                                              "import")
            else:
                leave("to review (not a kind imported by default)", c)
        elif c.ours is None:
            kind_of[c.sha] = OURS
            why[c.sha] = "overlap with our files not known"
        elif c.ours:
            kind_of[c.sha] = OURS
            why[c.sha] = "touches " + ", ".join(c.ours[:3]) + (
                " and %d more" % (len(c.ours) - 3) if len(c.ours) > 3 else "")
        elif (c.risk or "").lower() == "high":
            kind_of[c.sha] = OURS
            why[c.sha] = "the agent calls it high-risk"
        else:
            kind_of[c.sha] = SAFE
            why[c.sha] = "no overlap" + (
                " (but for %s)" % ", ".join(c.ours_lists)
                if c.ours_lists else "")

    # a commit goes no earlier than what it needs; one that needs a commit
    # the plan leaves out is left out too
    order = [SAFE, OURS, REVIEW]
    planned = [c for c in commits if c.sha in kind_of]
    changed = True
    while changed:
        changed = False
        for c in planned:
            if c.sha not in kind_of:
                continue
            for dep in c.depends or []:
                hits = _matches(dep, commits)
                if not hits or hits[0].sha == c.sha:
                    continue
                need = hits[0]
                if need.sha not in kind_of:
                    del kind_of[c.sha]
                    leave("needs %s, which is not in the plan" % need.short,
                          c)
                    changed = True
                    break
                if order.index(kind_of[need.sha]) > \
                        order.index(kind_of[c.sha]):
                    kind_of[c.sha] = kind_of[need.sha]
                    why[c.sha] = "needs %s" % need.short
                    changed = True

    stages = []
    size = max(1, int(size or 0)) if size else 0
    for kind in order:
        mine = [c for c in commits if kind_of.get(c.sha) == kind]
        if not mine:
            continue
        # as even as can be: 9 at 8 a stage is 5 and 4, not 8 and 1
        parts = -(-len(mine) // size) if size else 1
        chunks = [mine[len(mine) * i // parts:len(mine) * (i + 1) // parts]
                  for i in range(parts)]
        for n, chunk in enumerate(chunks, 1):
            stages.append(Stage(kind, n, len(chunks), chunk))

    levels = {}
    for c in commits:
        if c.sha in kind_of and c.peer not in levels:
            levels[c.peer] = conf.chain(conf.levels_for(c.peer, c.kind))
    return Plan(target, stages, left, levels, why)


def describe(plan):
    """The plan as lines of text, for the terminal."""
    n = len(plan.commits)
    if not n:
        out = ["Nothing to import automatically into %s." % plan.target]
    else:
        out = ["Import plan for %s: %d commit%s in %d stage%s." % (
            plan.target, n, "" if n == 1 else "s", len(plan.stages),
            "" if len(plan.stages) == 1 else "s")]
    for num, stage in enumerate(plan.stages, 1):
        out.append("")
        out.append("Stage %d: %s" % (num, stage.title))
        for c in stage.commits:
            out.append("  %s %-8s %s" % (c.short, c.kind, c.subject[:60]))
            if plan.why.get(c.sha) and stage.kind != SAFE:
                out.append("             %s" % plan.why[c.sha][:70])
    if n:
        out.append("")
        for peer, levels in plan.levels.items():
            out.append("Each commit from %s must pass: %s." % (
                peer, " -> ".join(levels) or "no testing"))
        out.extend(textwrap.wrap(
            "Each stage is one import run; what passes in it is promoted to "
            "%s before the next stage starts.  A commit that fails (its "
            "tests, or a conflict the agent cannot resolve) is left out and "
            "offered again.  The plan stops if the tests cannot run, or fail "
            "on %s without the import." % (plan.target, plan.target),
            78))
    if plan.left:
        out.append("")
        out.extend(textwrap.wrap("Not in the plan: " + "; ".join(
            "%d %s" % (len(cs), why) for why, cs in plan.left.items()) + ".",
            78))
    return out


def carry_out(repo, conf, ui, agent, plan, verbose=False, on_stage=None):
    """Run the stages in turn; returns (status, stages done).

    status: "done", or the status of the run that stopped the plan
    ("paused", "blocked", "ready").  `on_stage(num, total, stage)` hears of
    each stage as it starts.
    """
    conf.promote = "yes"                 # agreeing to the plan agreed to it
    total = len(plan.stages)
    for num, stage in enumerate(plan.stages, 1):
        if on_stage:
            on_stage(num, total, stage)
        ui.say("")
        ui.say("== Stage %d of %d: %s (%d commit%s)" % (
            num, total, stage.title, len(stage.commits),
            "" if len(stage.commits) == 1 else "s"))
        session = Session(repo, conf, ui, agent, interactive=False,
                          assume_yes=True, verbose=verbose)
        status = session.start("import", plan.target, stage.commits)
        if status not in ("promoted", "nothing"):
            rest = total - num
            ui.warn("The plan stops at stage %d of %d: the run is %s.%s" % (
                num, total, status,
                " %d stage%s not started; when the run is finished, ask "
                "for a plan again." % (rest, "" if rest == 1 else "s")
                if rest else ""))
            return status, num - 1
    ui.say("")
    ui.say("The plan is carried out: %d stage%s." % (
        total, "" if total == 1 else "s"))
    return "done", total
