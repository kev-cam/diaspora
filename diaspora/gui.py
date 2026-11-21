# SPDX-License-Identifier: GPL-3.0-or-later
"""diaspora gui: the pick list and the run, in a browser.

A small web server on 127.0.0.1 (standard library only) serves one page.
Every candidate commit can be marked accept (import it, through the test
gate), reject (never suggest it again), defer (hide it until asked), or
retry (import again a commit that failed before; diaspora.log tells the
agent how it went).  Apply records the rejections and deferrals and starts
one import run with the accepted commits.  The run is left on its branch;
the page promotes it to the target ("accept the run") or drops it.

Requests must carry the token printed in the URL, and a Host of this
machine, so other users of the machine and web pages cannot drive it.
"""

import argparse
import collections
import http.server
import json
import os
import re
import secrets
import subprocess
import threading
import time
import types
import urllib.parse
import webbrowser

from . import ledger as L
from . import plan as P
from . import tried as T
from .config import KINDS, LOCAL_NAME, Config
from .gitutil import DiasporaError, Repo
from .session import Session, record
from .ui import UI
from .userconf import UserConf
from .workspace import Tester, Workspace, fmt_secs

MARKS = ("accept", "reject", "defer", "retry")


class PageUI(UI):
    """What a job says, kept for the page instead of printed."""

    def __init__(self):
        super().__init__(color=False)
        self.width = 160
        self.lines = collections.deque(maxlen=5000)
        self.count = 0              # lines ever said: the page asks for more
        self.lock = threading.Lock()

    def _add(self, text):
        with self.lock:
            for line in str(text).splitlines() or [""]:
                self.lines.append(line)
                self.count += 1

    def say(self, msg=""):
        self._add(msg)

    def step(self, msg):
        self._add("      " + msg)

    def warn(self, msg):
        self._add("diaspora: " + msg)

    def item(self, num, total, item):
        self._add("[%d/%d] %s %s  %s" % (num, total, item["peer"],
                                         item["sha"][:10], item["subject"]))

    def confirm(self, question, default=True):
        return False                # the page asks, with buttons

    def since(self, num):
        with self.lock:
            first = self.count - len(self.lines)
            return self.count, list(self.lines)[max(0, num - first):]


class App:
    def __init__(self, repo, args):
        from . import cli           # (cli imports this module)
        self.cli = cli
        self.repo, self.args = repo, args
        self.ui = PageUI()
        self.lock = threading.Lock()
        self.job = None             # {"name", "started", "done", "error"}
        self.thread = None
        self.commits = []           # the last scan
        self.scanned = None
        self.marks = {}             # sha -> mark
        self.reading = 0            # commits the agent is reading
        self.notes = {}             # sha -> why (for reject and defer)
        self.agent_cost = 0.0
        self.fetch = not getattr(args, "no_fetch", False)

    # -- which repository ----------------------------------------------------------
    def repos(self):
        """The repositories of ~/.config/diaspora (and the one open now)."""
        user = UserConf.load()
        here = self.repo.top if self.repo is not None else None
        out = [{"name": name, "path": where, "note": note,
                "run": "" if note == "missing" else self.cli.run_state(where),
                "current": where == here}
               for name, where, note in user.listing()]
        if here and not any(r["current"] for r in out):
            out.insert(0, {"name": os.path.basename(here), "path": here,
                           "note": "", "run": "", "current": True})
        return out

    def switch(self, name):
        """Work on another repository (one of repos())."""
        if self.busy():
            raise DiasporaError("busy: %s is still running" % self.job["name"])
        hit = [r for r in self.repos() if r["name"] == name]
        if not hit:
            raise DiasporaError("no repository called %r" % name)
        if hit[0]["note"]:
            raise DiasporaError("%s: %s" % (name, hit[0]["note"]))
        repo = Repo(hit[0]["path"])
        repo.check_writable()
        Config.load(repo)                  # it must have a diaspora.conf
        self.repo = repo
        self.args.target = None             # (named for the first one only)
        self.args.here = False
        self.commits, self.scanned = [], None
        self.marks.clear()
        self.notes.clear()
        self.ui.say("")
        self.ui.say("== now working on %s" % repo.top)
        self.start("fetch and scan" if self.fetch else "scan", self.do_scan,
                   self.fetch)

    # -- helpers ----------------------------------------------------------------
    def conf(self):
        conf = Config.load(self.repo)
        conf.promote = "no"         # the page promotes, when asked
        return conf

    def target(self, conf):
        return self.cli.pick_target(self.repo, conf, self.args)

    def agent(self, conf):
        agent = self.cli.make_agent(self.repo, conf, self.ui, self.args)
        return agent

    def session(self, conf=None):
        conf = conf or self.conf()
        return Session(self.repo, conf, self.ui, self.agent(conf),
                       interactive=False, assume_yes=False)

    def session_file(self):
        if self.repo is None:
            raise DiasporaError("pick a repository first")
        return os.path.join(self.repo.state, "session.json")

    # -- jobs (one at a time, in a thread) --------------------------------------
    def busy(self):
        return self.job is not None and not self.job["done"]

    def start(self, name, func, *fargs):
        with self.lock:
            if self.busy():
                raise DiasporaError("busy: %s is still running"
                                    % self.job["name"])
            self.job = {"name": name, "started": time.time(), "done": False,
                        "error": None, "result": None}
        self.ui.say("")
        self.ui.say("== %s (%s)" % (name, time.strftime("%H:%M:%S")))

        def body():
            try:
                self.job["result"] = func(*fargs)
            except DiasporaError as exc:
                self.job["error"] = str(exc)
                self.ui.warn(str(exc))
            except Exception as exc:          # keep serving the page
                self.job["error"] = "%s: %s" % (type(exc).__name__, exc)
                self.ui.warn("internal error: %s" % self.job["error"])
            finally:
                self.job["done"] = True
                self.ui.say("== %s: %s" % (name, "failed" if
                                           self.job["error"] else "done"))

        self.thread = threading.Thread(target=body, daemon=True)
        self.thread.start()

    def wait(self, timeout=600):
        if self.thread:
            self.thread.join(timeout)

    # -- what the jobs do -------------------------------------------------------
    def do_scan(self, fetch):
        conf = self.conf()
        agent = self.agent(conf)
        target = self.target(conf)
        opts = argparse.Namespace(no_fetch=not fetch, json=False, pick=None)
        old, seen = list(self.commits), {}

        def progress(peer, commits):
            # the page lists what is read so far (the other peers as they
            # were until their turn comes)
            seen[peer.name] = list(commits)
            self.commits = [c for got in seen.values() for c in got] + [
                c for c in old if c.peer not in seen]
            self.reading = sum(c.asking for c in self.commits)

        self.reading = 0
        try:
            found = self.cli.gather(
                self.repo, conf, self.ui, agent,
                self.cli.pick_peers(conf, self.args.peers), target, opts,
                progress=progress)
        finally:
            self.reading = 0
            for commit in self.commits:
                commit.asking = False
        for peer, commits, dropped in found:
            self.cli.summarize(self.ui, peer, commits, dropped, "import")
        self.commits = [c for _p, commits, _d in found for c in commits]
        self.scanned = time.time()
        shas = {c.sha for c in self.commits}
        self.marks = {s: m for s, m in self.marks.items() if s in shas}
        self.agent_cost += agent.cost
        return "scanned"

    def do_apply(self):
        conf = self.conf()
        target = self.target(conf)
        by_sha = {c.sha: c for c in self.commits}
        selected, entries = [], []
        for commit in self.commits:            # in the scan's order
            mark = self.marks.get(commit.sha)
            if mark in ("accept", "retry"):
                selected.append(commit)
            elif mark in ("reject", "defer"):
                entries.append(L.entry(
                    commit.peer, commit.sha,
                    L.REJECTED if mark == "reject" else L.DEFERRED,
                    commit.subject, commit.kind,
                    note=self.notes.get(commit.sha) or None))
        if not selected and not entries:
            raise DiasporaError("nothing is marked")
        agent = self.agent(conf)
        if selected:
            session = Session(self.repo, conf, self.ui, agent,
                              interactive=False, assume_yes=False)
            status = session.start("import", target, selected, entries)
        else:
            record(self.repo, conf, self.ui, agent, target, entries)
            status = "recorded"
        self.agent_cost += agent.cost
        for sha in list(self.marks):
            if sha in by_sha:
                del self.marks[sha]
        self.notes.clear()
        self.do_scan(fetch=False)
        return status

    def plan(self, review=False, size=8):
        """The auto-import plan for the commits on the page."""
        conf = self.conf()
        target = self.target(conf)
        plan = P.propose(self.repo, conf, target, self.commits, size=size,
                         review=review)
        out = plan.as_dict()
        out["running"] = Session(self.repo, conf, self.ui,
                                 types.SimpleNamespace(log_dir=None),
                                 interactive=False).exists()
        out["untested"] = not conf.has_requirements()
        out["scanned"] = bool(self.scanned)
        return out

    def do_auto(self, review, size, shown):
        """Carry out the plan the page showed (`shown`: its commits)."""
        conf = self.conf()
        target = self.target(conf)
        if not conf.has_requirements():
            raise DiasporaError("diaspora.conf requires no testing: an "
                                "automatic import would land untested")
        plan = P.propose(self.repo, conf, target, self.commits, size=size,
                         review=review)
        if [c.sha for c in plan.commits] != list(shown):
            raise DiasporaError("the candidates changed since the plan was "
                                "shown: look at the plan again")
        if not plan.stages:
            raise DiasporaError("nothing to import")
        agent = self.agent(conf)

        def on_stage(num, total, stage):
            self.job["name"] = "auto-import: stage %d of %d" % (num, total)

        try:
            status, done = P.carry_out(self.repo, conf, self.ui, agent, plan,
                                       verbose=self.args.verbose,
                                       on_stage=on_stage)
        finally:
            self.agent_cost += agent.cost
            self.do_scan(fetch=False)
        if status != "done":
            raise DiasporaError(
                "the plan stopped at stage %d of %d: the run is %s; finish "
                "it above, then look at the plan again" % (
                    done + 1, len(plan.stages), status))
        return status

    def do_run(self, action, keep_log=True):
        session = self.session().load()
        if action == "promote":
            out = session.promote()
        elif action == "abort":
            out = session.abort(keep_log=keep_log)
        elif action == "continue":
            out = session.resume()
        elif action == "skip":
            out = session.skip()
        else:
            raise DiasporaError("unknown action %r" % action)
        self.do_scan(fetch=False)
        return out

    # -- what the page sees ------------------------------------------------------
    def state(self, since=0):
        if self.repo is None:
            count, lines = self.ui.since(since)
            return {"repo": None, "repos": self.repos(), "commits": [],
                    "run": None, "busy": self.busy(), "log": lines,
                    "log_count": count, "scanned": None,
                    "job": dict(self.job) if self.job else None,
                    "peers": [], "tests": [], "agent_cost": 0}
        conf = self.conf()
        target = self.target(conf)
        log = T.Log.at(self.repo, target, conf.log)
        commits = []
        for commit in self.commits:
            item = commit.as_dict()
            item["short"] = commit.short
            item["tried"] = log.about(commit.sha)
            item["mark"] = self.marks.get(commit.sha)
            item["why_mark"] = self.notes.get(commit.sha, "")
            commits.append(item)
        run = None
        # (no agent: looking must not warn about a missing one every time)
        session = Session(self.repo, conf, self.ui,
                          types.SimpleNamespace(log_dir=None),
                          interactive=False)
        if session.exists():
            data = session.load().data
            choices = data.get("choices") or {}
            items = []
            for i in data["items"]:
                item = {k: i.get(k) for k in (
                    "peer", "sha", "subject", "state", "note", "mode")}
                if i["state"] == "failed":
                    item["choice"] = choices.get(i["sha"], "failed")
                    item["can_include"] = bool(
                        data["stage"] == "ready" and session.attempt(i))
                    item["log"] = bool(self._test_log(i))
                items.append(item)
            run = {"id": data["id"], "dir": data["dir"],
                   "stage": data["stage"], "branch": data["branch"],
                   "target": data["target"], "workspace": session.ws.path,
                   "items": items}
        count, lines = self.ui.since(since)
        return {
            "repo": self.repo.top, "target": target,
            "head": self.repo.rev(target)[:10],
            "peers": [{"name": p.name, "url": p.url, "branch": p.branch,
                       "imports": list(p.imports), "ignore": list(p.ignore),
                       "max_lines": p.max_lines}
                      for p in conf.peers.values()],
            "tests": conf.chain(conf.levels_for(next(iter(conf.peers), ""),
                                                "bugfix")),
            "strategy": conf.strategy,
            "scanned": self.scanned, "commits": commits, "run": run,
            "reading": self.reading,
            "job": dict(self.job) if self.job else None,
            "busy": self.busy(), "log": lines, "log_count": count,
            "agent_cost": round(self.agent_cost, 2),
            "repos": self.repos(),
        }

    def _test_log(self, item):
        """The test log a failed item's note names, if it is one of ours."""
        match = re.search(r"\(log: ([^)\s]+)", item.get("note") or "")
        if not match:
            return None
        where = os.path.realpath(match.group(1))
        logs = os.path.realpath(self.repo.state_dir("logs"))
        if not where.startswith(logs + os.sep) or not os.path.isfile(where):
            return None
        return where

    def tests_text(self):
        """What an import must pass here, as the testing will run it."""
        conf = self.conf()
        target = self.target(conf)
        notes = conf.comments()
        out = ["What an import must pass before it can go to %s" % target,
               "(from %s%s)" % (conf.path, " and %s" % LOCAL_NAME
                                if os.path.exists(os.path.join(
                                    self.repo.top, LOCAL_NAME)) else ""),
               ""]
        if not conf.tests:
            out.append("No [test] levels: imports are not tested.")
            return {"text": "\n".join(out)}

        # which levels, for which peer and kind
        out.append("Required")
        chains = []
        for peer in conf.peers.values():
            groups = collections.OrderedDict()
            for kind in KINDS:
                if kind in peer.ignore:
                    continue
                levels = tuple(conf.chain(conf.levels_for(peer.name, kind)))
                groups.setdefault(levels, []).append(kind)
            for levels, kinds in groups.items():
                which = ("every import" if len(groups) == 1 else
                         "imports of kind %s" % ", ".join(kinds))
                out.append("  %s from %s: %s" % (
                    which, peer.name, " -> ".join(levels) or "no testing"))
                if levels not in chains:
                    chains.append(levels)
        if not conf.peers:
            chains.append(tuple(conf.chain(conf.levels_for("", "bugfix"))))
        out.append("  (a level's `needs` run first; each level runs only "
                   "once the one before it passed)")
        out.append("")

        ws = Workspace(self.repo, conf)
        out.append("Where")
        if ws.inplace:
            out.append("  in this checkout ($DIASPORA_SRC = %s), which must "
                       "be clean" % ws.path)
        else:
            out.append("  in the import workspace, a worktree of this "
                       "repository:\n    $DIASPORA_SRC   = %s" % ws.path)
        if ws.build:
            out.append("    $DIASPORA_BUILD = %s (kept between runs)"
                       % ws.build)
        out.append("  %s" % (
            "each imported commit is tested on its own"
            if conf.strategy == "each" else
            "the imports are tested together, and one by one only if "
            "that fails"))
        out.append("")

        def secs(value):
            return re.sub(r"(\d)h00m$", r"\1h", fmt_secs(value))

        tester = Tester(conf, ws, None, lambda *a, **k: None)
        used = [name for name in conf.tests
                if any(name in levels for levels in chains)]
        unused = [name for name in conf.tests if name not in used]
        for n, name in enumerate(used, 1):
            level = conf.tests[name]
            out.append("%d. %s%s" % (n, name, "  (needs %s)" % ", ".join(
                level.needs) if level.needs else ""))
            note = notes.get(("test", name))
            if note:
                out.extend("   # " + line if line else "   #"
                           for line in note.splitlines())
            out.append("   runs:")
            out.extend("     $ " + run for run in level.runs or [level.run])
            if level.compares:
                out.append(
                    "   passes when no test fails that did not already fail "
                    "on %s before the import (the command lists the "
                    "failing tests in $DIASPORA_FAILURES)" % target)
                ref = None
                try:
                    if os.path.isdir(ws.path):
                        ref = tester.load(target, name)
                except DiasporaError:
                    pass
                out.append(
                    "   reference for %s: %s" % (target, (
                        "measured, %d test%s listed as failing there" % (
                            len(ref), "" if len(ref) == 1 else "s"))
                        if ref is not None else
                        "none current; the next run measures it first") +
                    (" (good for %s)" % secs(level.reference_age)
                     if level.reference_age else ""))
            else:
                out.append("   passes when every command exits 0")
            extra = ["time limit %s" % secs(level.timeout)
                     if level.timeout else "no time limit"]
            if level.retry:
                extra.append("a failure is run %d more time%s before it "
                             "counts" % (level.retry,
                                         "" if level.retry == 1 else "s"))
            out.append("   " + "; ".join(extra))
            out.append("")
        if unused:
            out.append("Defined but not required: %s" % ", ".join(unused))
        note = notes.get(("require", ""))
        if note:
            if unused:
                out.append("")
            out.extend("# " + line if line else "#"
                       for line in note.splitlines())
        return {"text": "\n".join(out).rstrip() + "\n"}

    def run_log(self, sha):
        session = self.session()
        if not session.exists():
            raise DiasporaError("no run")
        item = next((i for i in session.load().data["items"]
                     if i["sha"] == sha), None)
        where = item and self._test_log(item)
        if not where:
            raise DiasporaError("no test log for %s" % sha[:10])
        with open(where, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
        if len(text) > 400000:
            text = "... (the start cut)\n" + text[-400000:]
        return {"text": text, "path": where}

    def choose(self, sha, choice):
        if self.busy():
            raise DiasporaError("busy: %s is still running" % self.job["name"])
        session = self.session()
        if not session.exists():
            raise DiasporaError("no run")
        session.load().choose(sha, choice)

    def show(self, sha, diff=False):
        commit = next((c for c in self.commits if c.sha == sha), None)
        if commit is None:
            raise DiasporaError("%s is not in the list" % sha)
        args = ["git", "--no-pager", "show", "--format=fuller", "--no-color"]
        if not diff:
            args.append("--stat")
        proc = subprocess.run(args + [sha], cwd=self.repo.top,
                              capture_output=True, text=True)
        text = proc.stdout
        if len(text) > 400000:
            text = text[:400000] + "\n... (cut)"
        return {"text": text}

    def mark(self, shas, mark, why=""):
        known = {c.sha: c for c in self.commits}
        for sha in shas:
            if sha not in known:
                raise DiasporaError("%s is not in the list" % sha)
            if mark in (None, "", "clear"):
                self.marks.pop(sha, None)
                self.notes.pop(sha, None)
            elif mark in MARKS:
                self.marks[sha] = mark
                if why:
                    self.notes[sha] = why
                else:
                    self.notes.pop(sha, None)
            else:
                raise DiasporaError("unknown mark %r" % mark)


class Handler(http.server.BaseHTTPRequestHandler):
    app = None
    token = None
    hosts = ()

    def log_message(self, fmt, *args):        # quiet
        pass

    def _send(self, code, body, kind="application/json"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", kind + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj))

    def _allowed(self, query):
        if self.headers.get("Host", "") not in self.hosts:
            return False
        got = self.headers.get("X-Diaspora-Token") or \
            (query.get("token") or [""])[0]
        return secrets.compare_digest(got, self.token)

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query)
        if not self._allowed(query):
            return self._send(403, "forbidden: open the URL diaspora "
                              "printed\n", "text/plain")
        try:
            if url.path == "/":
                return self._send(200, PAGE.replace("@TOKEN@", self.token),
                                  "text/html")
            if url.path != "/api/state" and self.app.repo is None:
                raise DiasporaError("pick a repository first")
            if url.path == "/api/state":
                since = int((query.get("since") or ["0"])[0])
                return self._json(200, self.app.state(since))
            if url.path == "/api/plan":
                return self._json(200, self.app.plan(
                    (query.get("review") or ["0"])[0] == "1",
                    int((query.get("size") or ["8"])[0] or 0)))
            if url.path == "/api/tests":
                return self._json(200, self.app.tests_text())
            if url.path == "/api/runlog":
                return self._json(200, self.app.run_log(
                    (query.get("commit") or [""])[0]))
            if url.path == "/api/show":
                return self._json(200, self.app.show(
                    (query.get("commit") or [""])[0],
                    (query.get("diff") or ["0"])[0] == "1"))
            return self._send(404, "not found\n", "text/plain")
        except DiasporaError as exc:
            return self._json(400, {"error": str(exc)})

    def do_POST(self):
        url = urllib.parse.urlsplit(self.path)
        if not self._allowed({}):
            return self._json(403, {"error": "forbidden"})
        if self.headers.get("Content-Type", "").split(";")[0] != \
                "application/json":
            return self._json(415, {"error": "send JSON"})
        try:
            size = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(size) or b"{}")
        except ValueError:
            return self._json(400, {"error": "bad JSON"})
        app = self.app
        try:
            if url.path == "/api/repo":
                app.switch(body.get("name", ""))
            elif self.app.repo is None:
                raise DiasporaError("pick a repository first")
            elif url.path == "/api/mark":
                app.mark(body.get("commits") or [], body.get("mark"),
                         body.get("why", ""))
            elif url.path == "/api/scan":
                app.start("fetch and scan" if body.get("fetch") else "scan",
                          app.do_scan, bool(body.get("fetch")))
            elif url.path == "/api/apply":
                if os.path.exists(app.session_file()):
                    raise DiasporaError(
                        "a run is waiting: accept (promote) or drop it first")
                app.start("import", app.do_apply)
            elif url.path == "/api/auto":
                if os.path.exists(app.session_file()):
                    raise DiasporaError(
                        "a run is waiting: accept (promote) or drop it first")
                shown = body.get("commits")
                if not isinstance(shown, list) or not shown:
                    raise DiasporaError("no plan to carry out")
                app.start("auto-import", app.do_auto,
                          bool(body.get("review")),
                          int(body.get("size") or 0), shown)
            elif url.path == "/api/choose":
                app.choose(body.get("commit", ""), body.get("choice", ""))
            elif url.path == "/api/run":
                action = body.get("action")
                app.start(action, app.do_run, action,
                          body.get("keep_log", True))
            else:
                return self._json(404, {"error": "not found"})
        except DiasporaError as exc:
            return self._json(400, {"error": str(exc)})
        return self._json(200, {"ok": True})


def serve(repo, args, ui):
    app = App(repo, args)
    token = secrets.token_urlsafe(24)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port),
                                             Handler)
    port = server.server_address[1]
    Handler.app, Handler.token = app, token
    Handler.hosts = ("127.0.0.1:%d" % port, "localhost:%d" % port)
    url = "http://127.0.0.1:%d/?token=%s" % (port, token)
    if repo is not None:
        app.start("fetch and scan" if app.fetch else "scan", app.do_scan,
                  app.fetch)
        ui.say("diaspora gui for %s at" % repo.top)
    else:
        ui.say("diaspora gui (pick the repository on the page) at")
    ui.say("  " + url)
    ui.say("(only this machine can reach it; over ssh: ssh -L %d:127.0.0.1:"
           "%d ...).  Ctrl-C stops it." % (port, port))
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except webbrowser.Error:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        if app.busy():
            ui.warn("stopped while %s was running; `diaspora status` shows "
                    "where the run is" % app.job["name"])
    finally:
        server.server_close()
    return 0


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>diaspora</title>
<style>
:root {
  --bg: #f7f7f5; --panel: #ffffff; --ink: #1d1d1b; --dim: #6b6b66;
  --line: #e2e1dc; --accent: #2f5fd0; --ok: #1f7a3d; --bad: #b3261e;
  --warn: #8a5a00; --defer: #6b5ca5; --mark-bg: #eef2fd; --on-ink: #ffffff;
  --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #161615; --panel: #1f1f1d; --ink: #e8e7e3; --dim: #9a9993;
    --line: #34332f; --accent: #7fa2ff; --ok: #5cc27e; --bad: #ff7b72;
    --warn: #e0b34f; --defer: #b0a2f0; --mark-bg: #232a3d; --on-ink: #10110f;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
header { padding: 14px 20px; border-bottom: 1px solid var(--line);
  background: var(--panel); display: flex; flex-wrap: wrap; gap: 10px 24px;
  align-items: baseline; }
header h1 { font-size: 17px; margin: 0; }
header .meta { color: var(--dim); font-size: 13px; }
header .meta code { font-family: var(--mono); color: var(--ink); }
header select { font: inherit; font-weight: 600; background: var(--panel);
  color: var(--ink); border: 1px solid var(--line); border-radius: 6px; padding: 2px 6px; }
#pick .repo { display: flex; gap: 12px; align-items: baseline; padding: 6px 0;
  border-bottom: 1px solid var(--line); }
#pick .repo:last-child { border-bottom: 0; }
#pick .repo button { min-width: 110px; text-align: left; }
#pick .repo code { font-family: var(--mono); font-size: 12.5px; color: var(--dim); flex: 1; }
main { padding: 16px 20px 120px; max-width: 1400px; }
button { font: inherit; border: 1px solid var(--line); background: var(--panel);
  color: var(--ink); border-radius: 6px; padding: 3px 10px; cursor: pointer; }
button:hover:not(:disabled) { border-color: var(--accent); }
button:disabled { opacity: .45; cursor: default; }
button.primary { background: var(--accent); color: var(--on-ink); border-color: var(--accent); }
button.danger { color: var(--bad); }
.toolbar { display: flex; flex-wrap: wrap; gap: 8px 14px; align-items: center;
  margin: 0 0 12px; }
.toolbar label { color: var(--dim); font-size: 13px; }
section.run { background: var(--panel); border: 1px solid var(--line);
  border-radius: 8px; padding: 12px 14px; margin-bottom: 16px; }
section.run h2 { font-size: 15px; margin: 0 0 8px; }
.run .items div.item { font-family: var(--mono); font-size: 12.5px; }
.run .choice { font-family: system-ui, sans-serif; margin: 3px 0 6px 11ch; color: var(--dim); }
.run .choice select { font: inherit; color: var(--ink); background: var(--panel);
  border: 1px solid var(--line); border-radius: 6px; padding: 1px 4px; }
.run .actions { margin-top: 10px; display: flex; gap: 8px; flex-wrap: wrap;
  align-items: center; }
table { width: 100%; border-collapse: collapse; background: var(--panel);
  border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--line);
  vertical-align: top; }
th { font-size: 12px; color: var(--dim); font-weight: 600; }
tr.marked td { background: var(--mark-bg); }
tr:last-child td { border-bottom: 0; }
td.sha { font-family: var(--mono); font-size: 12.5px; white-space: nowrap; }
td.sha a { color: var(--accent); text-decoration: none; cursor: pointer; }
td.subj .note { color: var(--dim); font-size: 12.5px; }
td.subj .tried { color: var(--bad); font-size: 12.5px; }
td.acts { white-space: nowrap; }
td.acts button { padding: 2px 8px; font-size: 12.5px; }
td.acts button.on.accept, td.acts button.on.retry { background: var(--ok); color: var(--on-ink); border-color: var(--ok); }
td.acts button.on.reject { background: var(--bad); color: var(--on-ink); border-color: var(--bad); }
td.acts button.on.defer { background: var(--defer); color: var(--on-ink); border-color: var(--defer); }
.badge { display: inline-block; font-size: 11.5px; padding: 0 6px;
  border-radius: 9px; border: 1px solid var(--line); color: var(--dim);
  margin-left: 4px; white-space: nowrap; }
.badge[data-tip] { cursor: help; }
.badge.failed { color: var(--bad); border-color: var(--bad); }
.badge.suggested { color: var(--ok); border-color: var(--ok); }
.badge.asking { color: var(--accent); border-color: var(--accent); border-style: dashed; }
.badge.deferred { color: var(--defer); border-color: var(--defer); }
.badge.overlap { color: var(--warn); border-color: var(--warn); }
#tip ul { margin: 0 0 5px; padding-left: 18px; } #tip code { font-size: 11.5px; }
#tip .by { opacity: .8; font-size: 12px; }
.dim { color: var(--dim); }
.st-done { color: var(--ok); } .st-failed { color: var(--bad); }
.st-paused { color: var(--warn); }
footer { position: fixed; left: 0; right: 0; bottom: 0; background: var(--panel);
  border-top: 1px solid var(--line); }
footer .bar { display: flex; gap: 14px; align-items: center; padding: 8px 20px;
  flex-wrap: wrap; }
footer .bar .status { color: var(--dim); font-size: 13px; flex: 1; min-width: 200px; }
#log { margin: 0; padding: 0 20px; max-height: 0; overflow: auto;
  font-family: var(--mono); font-size: 12px; white-space: pre-wrap;
  border-top: 1px solid var(--line); transition: max-height .15s; }
#log.open { max-height: 38vh; padding: 8px 20px; }
dialog { border: 1px solid var(--line); border-radius: 10px; background: var(--panel);
  color: var(--ink); width: min(1000px, 94vw); max-height: 88vh; padding: 0; }
dialog .head { display: flex; gap: 8px; padding: 10px 14px; border-bottom: 1px solid var(--line);
  align-items: center; }
dialog .head .t { flex: 1; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
#plandlg .opts { display: flex; gap: 14px; align-items: center; flex-wrap: wrap;
  padding: 8px 14px; border-bottom: 1px solid var(--line); font-size: 13px; }
#plandlg .opts input[type=number] { width: 4em; }
#plantext { padding: 4px 14px 12px; overflow: auto; max-height: calc(88vh - 100px);
  font-size: 13px; }
#plantext h3 { font-size: 13.5px; margin: 14px 0 4px; }
#plantext table { border-radius: 6px; table-layout: fixed; }
#plantext td:nth-child(1) { width: 7.5em; } #plantext td:nth-child(2) { width: 5.5em; }
#plantext td { padding: 3px 8px; font-size: 12.5px; }
#plantext .why { color: var(--dim); }
#plantext .err { color: var(--bad); }
dialog pre { margin: 0; padding: 12px 14px; font-family: var(--mono); font-size: 12px;
  white-space: pre-wrap; overflow: auto; max-height: calc(88vh - 56px); }
.err { color: var(--bad); }
#tip { position: fixed; z-index: 10; max-width: 360px; padding: 8px 10px;
  background: var(--ink); color: var(--bg); border-radius: 6px;
  font-size: 12.5px; line-height: 1.45; pointer-events: none;
  box-shadow: 0 4px 14px rgba(0,0,0,.25); opacity: 0; transition: opacity .1s; }
#tip.on { opacity: 1; }
#tip b { font-weight: 600; }
#tip p { margin: 0 0 5px; } #tip p:last-child { margin: 0; }
@media (max-width: 760px) {
  .hide-sm { display: none; }
  main, header { padding-left: 16px; padding-right: 16px; }
}
</style>
</head>
<body>
<header>
  <h1>diaspora</h1>
  <select id="repo" title="repository" hidden></select>
  <span class="meta" id="where"></span>
</header>
<main>
  <section class="run" id="pick" hidden></section>
  <div id="work">
  <section class="run" id="run" hidden></section>
  <div class="toolbar">
    <button id="fetch">Fetch and rescan</button>
    <button id="rescan">Rescan</button>
    <button id="auto" data-tip="auto">Auto-import…</button>
    <label><input type="checkbox" id="showall"> show ignored and deferred</label>
    <label>peer <select id="peer"><option value="">all</option></select></label>
    <span class="dim" id="counts"></span>
  </div>
  <table>
    <thead><tr>
      <th>commit</th><th class="hide-sm">date</th><th>kind</th>
      <th class="hide-sm">size</th><th>subject</th><th>decision</th>
    </tr></thead>
    <tbody id="rows"><tr><td colspan="6" class="dim">scanning ...</td></tr></tbody>
  </table>
  </div>
</main>
<footer>
  <div class="bar">
    <span class="status" id="status"></span>
    <button id="logbtn">Output</button>
    <button id="clear">Clear marks</button>
    <button id="apply" class="primary" data-tip="apply">Apply</button>
  </div>
  <pre id="log"></pre>
</footer>
<dialog id="dlg">
  <div class="head"><span class="t" id="dlgt"></span>
    <button id="dlgdiff">Diff</button><button id="dlgclose">Close</button></div>
  <pre id="dlgtext"></pre>
</dialog>
<dialog id="plandlg">
  <div class="head"><span class="t">Auto-import: the plan</span>
    <button id="plancancel">Cancel</button>
    <button id="plango" class="primary" disabled>Carry out</button></div>
  <div class="opts">
    <label>at most <input type="number" id="plansize" value="8" min="0"> commits a stage</label>
    <label><input type="checkbox" id="planreview"> also commits listed for review that the agent says to import</label>
  </div>
  <div id="plantext"></div>
</dialog>
<div id="tip" role="tooltip"></div>
<script>
"use strict";
const TOKEN = "@TOKEN@";
let S = null, logCount = 0, logOpen = false, wasBusy = false, shown = null;
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

async function api(path, body) {
  const opt = {headers: {"X-Diaspora-Token": TOKEN}};
  if (body !== undefined) {
    opt.method = "POST"; opt.body = JSON.stringify(body);
    opt.headers["Content-Type"] = "application/json";
  }
  const r = await fetch(path, opt);
  const j = await r.json().catch(() => ({error: r.statusText}));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}
async function act(path, body) {
  try { await api(path, body); } catch (e) { alert(e.message); }
  refresh();
}

function label(c) {
  const b = (cls, text) => `<span class="badge ${cls}" tabindex="0" data-tip="badge" data-sha="${c.commit}">${text}</span>`;
  if (S.run && S.run.items.some(i => i.sha === c.commit)) return b("asking", "in the run above");
  if (c.asking) return b("asking", "agent reading …");
  if (c.status === "failed") return b("failed", "failed before");
  if (c.tried.length) return b("failed", "tried before");
  if (c.status === "deferred") return b("deferred", "deferred");
  if (c.disposition === "import") return b("suggested", "suggested");
  if (c.disposition === "ignore") return b("", "ignored");
  return b("", "review");
}
// does it touch files our own commits changed since we parted from the peer?
function overlap(c) {
  if (c.ours == null) return "";
  const n = c.ours.length;
  return `<span class="badge${n ? " overlap" : ""}" tabindex="0" data-tip="overlap"
    data-sha="${c.commit}">${n ? `touches ours: ${n}` : "no overlap"}</span>`;
}
function overlapHelp(c) {
  const files = c.files.length, n = c.ours.length;
  const since = `since this repository parted from <b>${esc(c.peer)}</b>`;
  const lists = (c.ours_lists || []).length ? `<p>It also adds to ${
    c.ours_lists.map(p => `<code>${esc(p)}</code>`).join(", ")}, which we add
    to too; a list like that (<code>lists =</code> in diaspora.conf) is not
    counted: entries from both sides usually just go together.</p>` : "";
  if (!n) return `<p><b>No overlap.</b> None of the ${files} file${
    files === 1 ? "" : "s"} it changes has been changed by our own commits ${
    since}${lists ? " (but for a list)" : ""}: it should go in as ${
    esc(c.peer)} wrote it.</p>${lists}`;
  const shown = c.ours.slice(0, 8).map(p => `<li><code>${esc(p)}</code>${
    (c.ours_by[p] || []).map(([sha, subj]) =>
      `<br><span class="by">${esc(sha.slice(0, 8))} ${esc(subj)}</span>`).join("")}</li>`);
  if (n > 8) shown.push(`<li>and ${n - 8} more</li>`);
  return `<p><b>Touches files we changed.</b> ${n} of the ${files} file${
    files === 1 ? "" : "s"} it changes ${n === 1 ? "has" : "have"} also been
    changed by our own commits ${since} (imports from it not counted):</p>
    <ul>${shown.join("")}</ul>${lists}
    <p>It may not apply as written (the agent then ports it), and it is worth
    checking it fits with what we did there.</p>`;
}
// why a commit got its label (triage.decide), for the tip over it
function badgeHelp(c) {
  const P = S.peers.find(p => p.name === c.peer) || {imports: [], ignore: []};
  const kinds = P.imports.length ? P.imports.join(", ") : "none";
  const agent = c.triage === "agent";
  const said = agent && (c.reason || c.summary) ?
    `<p>The agent: “${esc(c.reason || c.summary)}”</p>` : "";
  const how = `<p>diaspora sorts each commit by its kind (read from the message${
    agent ? ", and here also by asking the agent" : ""}). This one is
    <b>${esc(c.kind)}</b>; from ${esc(c.peer)} it imports <b>${esc(kinds)}</b>
    by default (<code>import =</code> in diaspora.conf).</p>`;
  const pick = `<p>Labels only advise: nothing is marked until you press a button.</p>`;
  if (S.run && S.run.items.some(i => i.sha === c.commit)) return `<p><b>In the
    run above.</b> It is being imported (or was tried) in the run that is
    waiting; what becomes of it is decided there, when the run is promoted
    or dropped.</p>`;
  if (c.asking) return `<p><b>The agent is reading it.</b> Its message
    did not make clear enough what it is, so diaspora asked the agent. Until the
    answer is in, it is sorted by its message alone: <b>${esc(c.kind)}</b>.</p>
    <p>The answer is kept, so it is never asked about again. You can mark it
    already; the label changes when the agent answers.</p>`;
  if (c.status === "failed") return `<p><b>Failed before.</b> An earlier import
    of this commit did not pass (conflict or new test failures); the ledger
    holds it as <i>failed</i>${c.note ? ": " + esc(c.note) : ""}.</p>
    <p>It stays in the list so you can <b>Retry</b> (the agent is told what went
    wrong), Reject or Defer it.</p>`;
  if (c.tried.length) return `<p><b>Tried before.</b> diaspora.log has
    ${c.tried.length} earlier attempt${c.tried.length > 1 ? "s" : ""} at this
    commit that did not work out; the last one is summed up in red below the
    subject.</p><p><b>Retry</b> tells the agent about them.</p>`;
  if (c.status === "deferred") return `<p><b>Deferred.</b> Someone chose
    <i>Defer</i> for it earlier, so the ledger says “ask again later”. It is
    hidden unless “show ignored and deferred” is ticked.</p>
    <p>Accept, Reject or Defer it again as you like.</p>`;
  if (c.disposition === "import") return `<p><b>Suggested.</b> diaspora
    would import this one: its kind is one imported by default, it is not too
    big${P.max_lines ? " (" + P.max_lines + " lines)" : ""}, and it does not touch
    code marked as rewritten here${agent ? ", and the agent sees no reason not to" : ""}.</p>
    ${how}${said}${pick}`;
  if (c.disposition === "ignore") return `<p><b>Ignored.</b> Not worth
    importing, it seems: ${esc(c.why || "")}.</p>
    <p>Hidden unless “show ignored and deferred” is ticked; you can still
    Accept it.</p>${said}`;
  return `<p><b>Review.</b> Neither suggested nor ignored: a person should
    decide. Here because ${esc(c.why || "the rules do not decide it")}.</p>
    ${how}${said}${pick}`;
}
function lastTry(c) {
  if (!c.tried.length) return c.status === "failed" ? c.note : "";
  const lines = c.tried[c.tried.length - 1].trim().split("\n").slice(1)
    .map(s => s.trim()).filter(s => !s.startsWith("=>"));
  return lines.join("; ");
}

// what the buttons do, shown floating over them
function gate() {
  return S.tests && S.tests.length ? S.tests.join(" then ") : "the test level";
}
function help(what, el) {
  if (what === "badge" || what === "overlap") {
    const c = S.commits.find(x => x.commit === (el && el.dataset.sha));
    return !c ? "" : what === "badge" ? badgeHelp(c) : overlapHelp(c);
  }
  const tgt = `<b>${esc(S.target || "the target branch")}</b>`;
  const later = `<p>Nothing happens until you press <b>Apply</b>; press again to unmark.</p>`;
  const tip = {
    auto: `<p><b>Propose an import plan.</b> diaspora sorts the suggested
      commits into stages, safest first: those touching none of our files,
      then those touching files we changed too.</p><p>You see the plan and
      nothing happens until you press <b>Carry out</b>; then each stage is an
      import run that must pass <b>${esc(gate())}</b>, and what passes goes to
      ${tgt} before the next stage starts.</p>`,
    gate: `<p>The test levels an import must pass before it can go to ${tgt},
      in the order they run.</p><p>Click to see each level's commands, what
      counts as passing, and where they run.</p>`,
    accept: `<p><b>Import it.</b> Apply cherry-picks it onto a test branch in
      diaspora's own workspace (~/.cache/diaspora), not onto ${tgt}. If it does
      not apply cleanly the agent ports it.</p>
      <p>Then <b>${esc(gate())}</b> runs: it passes if no test fails that did not
      fail before the import. ${tgt} changes only when you press
      <b>Accept: promote</b> afterwards. Nothing is ever pushed.</p>`,
    retry: `<p><b>Try it again.</b> As Accept: test branch, agent port if it
      conflicts, <b>${esc(gate())}</b>, then your <b>Accept: promote</b>.</p>
      <p>The agent is told how the earlier attempts went (from diaspora.log),
      so it need not repeat them.</p>`,
    reject: `<p><b>Not wanted here.</b> Apply records it as <i>rejected</i>,
      with your reason, in diaspora.ledger, committed on ${tgt}.
      diaspora will not suggest it again.</p>
      <p>No code changes. To change your mind: <code>diaspora reopen &lt;commit&gt;</code>.</p>`,
    defer: `<p><b>Not now.</b> Apply records it as <i>deferred</i> in
      diaspora.ledger, committed on ${tgt}.</p>
      <p>No code changes. It drops out of the list but is still a candidate:
      tick “show ignored and deferred” to see it, and Accept it whenever you like.</p>`,
    promote: (() => {
      const R = S.run; if (!R) return "";
      const done = R.items.filter(i => i.state === "done");
      const failed = R.items.filter(i => i.state === "failed");
      const what = {failed: "left out, recorded as <i>failed</i>: it shows again as “failed before”, to Retry",
        reject: "left out, recorded as <i>rejected</i>: not offered again",
        defer: "left out, recorded as <i>deferred</i>",
        include: "<b>included</b> although its tests failed (the ledger says so)"};
      return `<p>Fast-forwards ${tgt} to the run's branch: the ${done.length} that passed
        ${done.length === 1 ? "goes" : "go"} in, each as tested.</p>` +
        (failed.length ? `<p>${failed.map(i => `<code>${esc(i.sha.slice(0, 10))}</code> is ${what[i.choice] || what.failed}`).join("<br>")}.</p>` : "") +
        `<p>What was tried and failed goes into diaspora.log, the verdicts into
        diaspora.ledger, in one more commit. Nothing is pushed.</p>`;
    })(),
    abort: `<p>Throws the run away: ${tgt} is not touched and the ledger is not
      changed, so every commit in it is offered again as before.</p>
      <p>With the box ticked, what failed is still noted in diaspora.log (a
      commit on ${tgt}), so the agent knows next time.</p>`,
    choice: `<p>What becomes of this commit when you promote the run.
      <b>Include it anyway</b> takes the attempt that failed its tests (the
      agent's port and repair, if any) onto the branch after the ones that
      passed, untested together with them; use it when the failure is not
      the commit's fault, e.g. a test that needs something this machine lacks.</p>`,
    apply: `<p>Carries out the marks: <i>reject</i> and <i>defer</i> are recorded
      in diaspora.ledger on ${tgt}; <i>accept</i> and <i>retry</i> start one
      import run (test branch, <b>${esc(gate())}</b>).</p>
      <p>${tgt} itself changes only when you press <b>Accept: promote</b>
      on the finished run.</p>`,
  }[what];
  const wait = what === "apply" && S.run ?
    `<p><b>Finish the run above first</b> (promote it or drop it).</p>` : "";
  const own = what === "apply" || what === "promote" || what === "abort" || what === "choice";
  return tip ? wait + tip + (own ? "" : later) : "";
}
let tipFor = null, tipTimer = 0;
function showTip(el) {
  const t = $("tip"), html = help(el.dataset.tip, el);
  if (!html || !el.isConnected) return;
  t.innerHTML = html;
  const r = el.getBoundingClientRect(), w = t.offsetWidth, h = t.offsetHeight;
  let x = Math.min(Math.max(8, r.left + r.width / 2 - w / 2), innerWidth - w - 8);
  let y = r.top - h - 8;
  if (y < 8) y = r.bottom + 8;                 // no room above: below
  t.style.left = x + "px"; t.style.top = y + "px";
  t.classList.add("on");
}
function hideTip() { clearTimeout(tipTimer); tipFor = null; $("tip").classList.remove("on"); }
function tipOn(ev) {
  const el = ev.target.closest && ev.target.closest("[data-tip]");
  if (el === tipFor) return;
  hideTip();
  if (!el) return;
  tipFor = el;
  tipTimer = setTimeout(() => tipFor && showTip(tipFor), ev.type === "focusin" ? 0 : 250);
}
document.addEventListener("mouseover", tipOn);
document.addEventListener("focusin", tipOn);
document.addEventListener("focusout", hideTip);
document.addEventListener("scroll", hideTip, true);
document.addEventListener("keydown", ev => { if (ev.key === "Escape") hideTip(); });

// redraw only what changed: a redraw every poll would lose the focus, the
// tip over a button and what was ticked
const drawn = new WeakMap();
function setHTML(el, html) {
  if (drawn.get(el) === html) return;
  drawn.set(el, html);
  el.innerHTML = html;
}

function renderRows() {
  const all = $("showall").checked, peer = $("peer").value;
  // marking goes on while a scan fills the list; not while a run works
  const scanning = S.busy && S.job && /scan/.test(S.job.name);
  const busy = S.busy && !scanning, blocked = S.busy || !!S.run;
  const list = S.commits.filter(c => (all || (c.disposition !== "ignore" &&
    c.status !== "deferred") || c.mark) && (!peer || c.peer === peer));
  if (!S.scanned && !S.commits.length) { setHTML($("rows"),
    '<tr><td colspan="6" class="dim">scanning ...</td></tr>'); return; }
  if (!list.length && scanning) { setHTML($("rows"),
    '<tr><td colspan="6" class="dim">scanning ...</td></tr>'); return; }
  if (!list.length) { setHTML($("rows"),
    '<tr><td colspan="6" class="dim">Nothing to import' +
    (all ? "" : " (tick “show ignored and deferred” for the rest)") + '.</td></tr>'); return; }
  const multi = S.peers.length > 1;
  setHTML($("rows"), list.map(c => {
    const again = c.status === "failed" || c.tried.length;
    const opts = again ? ["retry", "reject", "defer"] : ["accept", "reject", "defer"];
    const held = S.run && S.run.items.some(i => i.sha === c.commit);
    const btns = opts.map(m => `<button class="${m}${c.mark === m ? " on" : ""}"
      data-sha="${c.commit}" data-mark="${m}" data-tip="${m}" ${busy || held ? "disabled" : ""}>${
      m[0].toUpperCase() + m.slice(1)}</button>`).join(" ");
    const agent = c.triage === "agent" ? `agent${c.risk ? ", risk " + esc(c.risk) : ""}: ${
      esc(c.reason || c.summary)}` : (c.why && c.status === "pending" &&
      c.disposition !== "import" ? esc(c.why) : "");
    const t = lastTry(c);
    return `<tr class="${c.mark ? "marked" : ""}">
      <td class="sha">${multi ? esc(c.peer) + " " : ""}<a data-show="${c.commit}">${esc(c.short)}</a></td>
      <td class="hide-sm dim">${esc(c.date)}</td>
      <td>${esc(c.kind)}</td>
      <td class="hide-sm dim">+${c.added} −${c.deleted}</td>
      <td class="subj">${esc(c.subject)} ${label(c)}${overlap(c)}
        ${agent ? `<div class="note">${agent}</div>` : ""}
        ${t ? `<div class="tried">${esc(t)}</div>` : ""}
        ${c.why_mark ? `<div class="note">why: ${esc(c.why_mark)}</div>` : ""}</td>
      <td class="acts">${btns}</td></tr>`;
  }).join(""));
  const n = m => S.commits.filter(c => c.mark === m).length;
  $("counts").textContent = `${list.length} shown of ${S.commits.length}`;
  const a = n("accept") + n("retry"), r = n("reject"), d = n("defer");
  $("apply").disabled = blocked || !(a + r + d);
  $("apply").textContent = a ? `Import ${a}` + (r + d ? `, record ${r + d}` : "") :
    (r + d ? `Record ${r + d}` : "Apply");
  $("clear").disabled = busy || !(a + r + d);
}

function renderRun() {
  const R = S.run, el = $("run");
  if (!R) { el.hidden = true; return; }
  el.hidden = false;
  const paused = R.items.some(i => i.state === "paused");
  const done = R.items.filter(i => i.state === "done").length;
  const dis = S.busy ? "disabled" : "";
  const failed = R.items.filter(i => i.state === "failed");
  const inc = failed.filter(i => i.choice === "include").length;
  let stage = R.stage === "ready" ? `tested: ${done} of ${R.items.length} passed` :
    paused ? "waiting: a conflict needs a person" :
    S.busy ? "running" : "stopped (blocked or interrupted)";
  let acts = "";
  if (R.stage === "ready") {
    const n = done + inc;
    acts = `<button class="primary" data-run="promote" data-tip="promote" ${dis} ${n || failed.length ? "" : "disabled"}>${
      n ? `Promote ${n} to ${esc(R.target)}` + (inc ? ` (${inc} despite failed tests)` : "")
        : `Record the outcome on ${esc(R.target)}`}</button>
     <button class="danger" data-run="abort" data-tip="abort" ${dis}>Drop the run: nothing goes to ${esc(R.target)}</button>`;
  }
  else if (paused) acts =
    `<span class="dim">Resolve it in <code>${esc(R.workspace)}</code> and <code>git add</code>, then</span>
     <button data-run="continue" ${dis}>Continue</button>
     <button data-run="skip" ${dis}>Skip that commit</button>
     <button class="danger" data-run="abort" ${dis}>Drop the run</button>`;
  else if (!S.busy) acts =
    `<button data-run="continue">Continue</button>
     <button class="danger" data-run="abort">Drop the run</button>`;
  if (acts.includes('data-run="abort"')) acts +=
    ` <label class="dim"><input type="checkbox" id="keeplog" checked> keep what did not work in diaspora.log</label>`;
  const word = {done: "passed", failed: "failed", picked: "testing", queued: "queued",
    paused: "waiting", superseded: "not needed", "not-applicable": "not needed"};
  const choice = i => {
    if (R.stage !== "ready" || i.state !== "failed") return "";
    const opt = (v, t, off) => `<option value="${v}" ${i.choice === v ? "selected" : ""} ${off ? "disabled" : ""}>${t}</option>`;
    return `<div class="choice">on promote:
      <select data-choose="${i.sha}" data-tip="choice" ${dis}>
        ${opt("failed", "leave it out, offer it again later (Retry)")}
        ${opt("reject", "leave it out and reject it")}
        ${opt("defer", "leave it out and defer it")}
        ${opt("include", "include it anyway" + (i.can_include ? "" : " (nothing to include: it never got as far as a commit)"), !i.can_include)}
      </select>
      ${i.log ? `<button data-runlog="${i.sha}">Test log</button>` : ""}</div>`;
  };
  setHTML(el, `<h2>Run ${esc(R.id)} <span class="dim">· ${stage} · branch ${esc(R.branch)}</span></h2>
    <div class="items">${R.items.map(i => `<div class="item"><span class="st-${esc(i.state)}">${
      esc((word[i.state] || i.state).padEnd(10))}</span> ${esc(i.sha.slice(0, 10))} ${esc(i.subject)}${
      i.note ? ` <span class="dim">— ${esc(i.note.replace(/ \(log: [^)]*\)/, ""))}</span>` : ""}${choice(i)}</div>`).join("")}</div>
    <div class="actions">${acts}</div>`);
}

function renderRepos() {
  const sel = $("repo"), repos = S.repos || [];
  const key = repos.map(r => r.name + (r.current ? "*" : "")).join(",");
  if (sel.dataset.key !== key) {
    sel.dataset.key = key;
    sel.innerHTML = (S.repo ? "" : '<option value="">choose …</option>') +
      repos.map(r => `<option value="${esc(r.name)}" ${r.current ? "selected" : ""} ${
        r.note ? "disabled" : ""}>${esc(r.name)}${r.note ? " (" + esc(r.note) + ")" : ""}</option>`).join("");
  }
  sel.hidden = repos.length < 2 && !!S.repo;
  sel.disabled = S.busy;
  const pick = $("pick");
  pick.hidden = !!S.repo;
  $("work").hidden = !S.repo;
  if (!S.repo) {
    pick.innerHTML = `<h2>Which repository?</h2>` + (repos.length ? repos.map(r =>
      `<div class="repo"><button data-repo="${esc(r.name)}" ${r.note || S.busy ? "disabled" : ""}>${
        esc(r.name)}</button><code>${esc(r.path)}</code><span class="dim">${
        esc([r.note, r.run].filter(Boolean).join(", "))}</span></div>`).join("") :
      `<p class="dim">No repositories listed in ~/.config/diaspora. Add some:</p>
       <pre>[repo "name"]\n    path = /path/to/checkout</pre>`);
  }
}

function render() {
  renderRepos();
  try { renderAll(); } finally { retip(); }
}
// the rows are drawn afresh: keep the tip on the button now in their place
function retip() {
  if (!tipFor || tipFor.isConnected) return;
  const sha = tipFor.dataset.sha, what = tipFor.dataset.tip;
  const now = [...document.querySelectorAll("[data-tip]")].find(b =>
    b.dataset.tip === what && b.dataset.sha === sha);
  if (now && now.matches(":hover")) tipFor = now; else hideTip();
}
function renderAll() {
  if (!S.repo) {
    $("where").textContent = "";
    $("status").textContent = S.job && S.job.error ? S.job.error : "";
    $("apply").disabled = $("clear").disabled = true;
    return;
  }
  $("where").innerHTML = `<code>${esc(S.repo)}</code> → <code>${esc(S.target)}</code> at ${
    esc(S.head)} · peers ${S.peers.map(p => esc(p.name)).join(", ")} · gate: <a href="#" id="gatelink" data-tip="gate">${
    esc(S.tests.join(" → ") || "none")}${S.strategy === "batch" ? " (batch)" : ""}</a>`;
  const sel = $("peer");
  if (sel.options.length !== S.peers.length + 1)
    sel.innerHTML = '<option value="">all</option>' +
      S.peers.map(p => `<option>${esc(p.name)}</option>`).join("");
  renderRun(); renderRows();
  const j = S.job;
  let st = j ? (j.done ? `${j.name}: ${j.error ? "failed — " + j.error : "done"}` :
    `${j.name} … ${Math.round(Date.now() / 1000 - j.started)}s`) : "";
  if (S.run && !S.busy && S.run.stage !== "ready")
    st = (st ? st + " · " : "") + "a run is waiting";
  if (S.reading) st += ` · the agent is reading ${S.reading} commit${S.reading > 1 ? "s" : ""}`;
  if (S.agent_cost) st += ` · agent $${S.agent_cost.toFixed(2)}`;
  $("status").textContent = st;
  $("status").className = "status" + (j && j.error ? " err" : "");
  $("fetch").disabled = $("rescan").disabled = $("auto").disabled = S.busy;
}

async function refresh() {
  try {
    S = await api("/api/state?since=" + logCount);
  } catch (e) { $("status").textContent = "lost the server: " + e.message; return; }
  if (S.log.length) {
    const log = $("log"), stick = log.scrollTop + log.clientHeight >= log.scrollHeight - 4;
    log.textContent += S.log.join("\n") + "\n";
    if (log.textContent.length > 400000) log.textContent = log.textContent.slice(-300000);
    if (stick) log.scrollTop = log.scrollHeight;
  }
  logCount = S.log_count;
  if (S.busy && !wasBusy && !logOpen) toggleLog(true);
  wasBusy = S.busy;
  render();
}

function toggleLog(open) {
  logOpen = open ?? !logOpen;
  $("log").classList.toggle("open", logOpen);
  if (logOpen) $("log").scrollTop = $("log").scrollHeight;
}

async function showCommit(sha, diff) {
  shown = sha;
  const c = S.commits.find(x => x.commit === sha);
  $("dlgt").textContent = `${c.short} ${c.subject}`;
  $("dlgdiff").hidden = false;
  $("dlgdiff").textContent = diff ? "Files" : "Diff";
  $("dlgdiff").dataset.diff = diff ? "0" : "1";
  $("dlgtext").textContent = "…";
  if (!$("dlg").open) $("dlg").showModal();
  try {
    const r = await api(`/api/show?commit=${sha}&diff=${diff ? 1 : 0}`);
    let extra = "";
    if (c.triage === "agent") extra += `\nagent triage: ${c.kind} / relevant: ${c.relevant} / recommend: ${
      c.recommend} / risk: ${c.risk}\n  ${c.summary}\n  ${c.reason}\n`;
    if (c.tried.length) extra += "\ntried before (diaspora.log):\n" + c.tried.join("\n");
    $("dlgtext").textContent = r.text + extra;
  } catch (e) { $("dlgtext").textContent = e.message; }
}

document.addEventListener("click", ev => {
  const b = ev.target.closest("button, a");
  if (!b) return;
  if (b.dataset.show) return showCommit(b.dataset.show, false);
  if (b.dataset.repo) return act("/api/repo", {name: b.dataset.repo});
  if (b.dataset.mark) {
    const c = S.commits.find(x => x.commit === b.dataset.sha);
    let mark = b.dataset.mark, why = "";
    if (c.mark === mark) mark = "clear";
    else if (mark === "reject" || mark === "defer") {
      why = prompt(mark === "reject" ? "Why reject it? (optional, goes into the ledger)"
                                     : "Why later? (optional)", "");
      if (why === null) return;
    }
    return act("/api/mark", {commits: [c.commit], mark, why});
  }
  if (b.dataset.runlog) return showRunLog(b.dataset.runlog);
  if (b.dataset.run) {
    const a = b.dataset.run;
    if (a === "abort" && !confirm("Drop the whole run? Imports that passed are thrown away.")) return;
    const keep = $("keeplog") ? $("keeplog").checked : true;
    return act("/api/run", {action: a, keep_log: keep});
  }
});
document.addEventListener("change", ev => {
  const s = ev.target;
  if (s.dataset && s.dataset.choose)
    act("/api/choose", {commit: s.dataset.choose, choice: s.value});
});
async function showRunLog(sha) {
  const i = S.run.items.find(x => x.sha === sha);
  shown = null;
  $("dlgt").textContent = `test log: ${sha.slice(0, 10)} ${i ? i.subject : ""}`;
  $("dlgdiff").hidden = true;
  $("dlgtext").textContent = "loading …";
  $("dlg").showModal();
  try { $("dlgtext").textContent = (await api("/api/runlog?commit=" + sha)).text; }
  catch (e) { $("dlgtext").textContent = e.message; }
}
async function showTests() {
  shown = null;
  $("dlgt").textContent = "testing an import must pass";
  $("dlgdiff").hidden = true;
  $("dlgtext").textContent = "loading …";
  if (!$("dlg").open) $("dlg").showModal();
  try { $("dlgtext").textContent = (await api("/api/tests")).text; }
  catch (e) { $("dlgtext").textContent = e.message; }
}
$("where").onclick = e => {
  if (e.target.closest("#gatelink")) { e.preventDefault(); showTests(); }
};
// auto-import: show the plan, carry it out only when told
let plan = null;
const planOpts = () => ({review: $("planreview").checked,
                         size: parseInt($("plansize").value || "0", 10) || 0});
async function showPlan() {
  plan = null;
  $("plango").disabled = true;
  $("plantext").innerHTML = '<p class="dim">working out a plan …</p>';
  if (!$("plandlg").open) $("plandlg").showModal();
  const o = planOpts();
  try {
    plan = await api(`/api/plan?review=${o.review ? 1 : 0}&size=${o.size}`);
    renderPlan();
  } catch (e) { $("plantext").innerHTML = `<p class="err">${esc(e.message)}</p>`; }
}
function renderPlan() {
  const p = plan, n = p.stages.reduce((a, s) => a + s.commits.length, 0);
  const tgt = `<b>${esc(p.target)}</b>`;
  let h = "";
  if (!p.scanned) h += `<p class="dim">The scan has not finished; the plan is of what is read so far.</p>`;
  h += n ? `<p>${n} commit${n === 1 ? "" : "s"} in ${p.stages.length} stage${
    p.stages.length === 1 ? "" : "s"}, into ${tgt}.</p>` :
    `<p>Nothing to import automatically into ${tgt}.</p>`;
  p.stages.forEach((s, i) => {
    h += `<h3>Stage ${i + 1}: ${esc(s.title)}</h3><table>` + s.commits.map(c =>
      `<tr><td class="sha">${esc(c.short)}</td><td>${esc(c.kind)}</td>
       <td>${esc(c.subject)}${s.kind !== "safe" && c.plan_why ?
         `<div class="why">${esc(c.plan_why)}</div>` : ""}</td></tr>`).join("") + `</table>`;
  });
  if (n) {
    h += `<h3>How it runs</h3>` + Object.entries(p.levels).map(([peer, lv]) =>
      `<p>Each commit from ${esc(peer)} must pass <b>${esc(lv.join(" → ") || "no testing")}</b>.</p>`).join("");
    h += `<p>Each stage is one import run. What passes in it is promoted to ${tgt}
      before the next stage starts. A commit that fails (its tests, or a
      conflict the agent cannot resolve) is left out and offered again.</p>
      <p>The plan stops if the tests cannot run, or fail on ${tgt} without the
      import: that run then waits above, as any other.</p>`;
  }
  if (p.left.length) h += `<h3>Not in the plan</h3><ul>` + p.left.map(l =>
    `<li>${l.count} ${esc(l.why)}</li>`).join("") + `</ul>`;
  let stop = "";
  if (p.running) stop = "A run is waiting above: promote or drop it first.";
  else if (p.untested) stop = "diaspora.conf requires no testing: no automatic import.";
  else if (S.busy) stop = `Busy: ${S.job ? S.job.name : "a job"} is running.`;
  if (stop) h += `<p class="err">${esc(stop)}</p>`;
  $("plantext").innerHTML = h;
  $("plango").disabled = !n || !!stop;
  $("plango").textContent = n ? `Carry out (${n} commit${n === 1 ? "" : "s"})` : "Carry out";
}
$("auto").onclick = showPlan;
$("planreview").onchange = showPlan;
$("plansize").onchange = showPlan;
$("plancancel").onclick = () => $("plandlg").close();
$("plango").onclick = () => {
  if (!plan) return;
  const commits = plan.stages.flatMap(s => s.commits.map(c => c.commit));
  $("plandlg").close();
  act("/api/auto", Object.assign(planOpts(), {commits}));
};
$("fetch").onclick = () => act("/api/scan", {fetch: true});
$("rescan").onclick = () => act("/api/scan", {fetch: false});
$("apply").onclick = () => act("/api/apply", {});
$("clear").onclick = () => act("/api/mark", {
  commits: S.commits.filter(c => c.mark).map(c => c.commit), mark: "clear"});
$("logbtn").onclick = () => toggleLog();
$("repo").onchange = () => { if ($("repo").value) act("/api/repo", {name: $("repo").value}); };
$("showall").onchange = $("peer").onchange = () => renderRows();
$("dlgclose").onclick = () => $("dlg").close();
$("dlgdiff").onclick = () => showCommit(shown, $("dlgdiff").dataset.diff === "1");

refresh();
setInterval(() => { if (!document.hidden || (S && S.busy)) refresh(); }, 2000);
</script>
</body>
</html>
"""
