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
import secrets
import subprocess
import threading
import time
import types
import urllib.parse
import webbrowser

from . import ledger as L
from . import tried as T
from .config import Config
from .gitutil import DiasporaError, Repo
from .session import Session, record
from .ui import UI
from .userconf import UserConf

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
        found = self.cli.gather(self.repo, conf, self.ui, agent,
                                self.cli.pick_peers(conf, self.args.peers),
                                target, opts)
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
            run = {"id": data["id"], "dir": data["dir"],
                   "stage": data["stage"], "branch": data["branch"],
                   "target": data["target"], "workspace": session.ws.path,
                   "items": [{k: i.get(k) for k in (
                       "peer", "sha", "subject", "state", "note", "mode")}
                       for i in data["items"]]}
        count, lines = self.ui.since(since)
        return {
            "repo": self.repo.top, "target": target,
            "head": self.repo.rev(target)[:10],
            "peers": [{"name": p.name, "url": p.url, "branch": p.branch}
                      for p in conf.peers.values()],
            "tests": conf.chain(conf.levels_for(next(iter(conf.peers), ""),
                                                "bugfix")),
            "strategy": conf.strategy,
            "scanned": self.scanned, "commits": commits, "run": run,
            "job": dict(self.job) if self.job else None,
            "busy": self.busy(), "log": lines, "log_count": count,
            "agent_cost": round(self.agent_cost, 2),
            "repos": self.repos(),
        }

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
.run .items div { font-family: var(--mono); font-size: 12.5px; }
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
.badge.failed { color: var(--bad); border-color: var(--bad); }
.badge.suggested { color: var(--ok); border-color: var(--ok); }
.badge.deferred { color: var(--defer); border-color: var(--defer); }
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
  if (c.status === "failed") return '<span class="badge failed">failed before</span>';
  if (c.tried.length) return '<span class="badge failed">tried before</span>';
  if (c.status === "deferred") return '<span class="badge deferred">deferred</span>';
  if (c.disposition === "import") return '<span class="badge suggested">suggested</span>';
  if (c.disposition === "ignore") return '<span class="badge">ignored</span>';
  return '<span class="badge">review</span>';
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
function help(what) {
  const tgt = `<b>${esc(S.target || "the target branch")}</b>`;
  const later = `<p>Nothing happens until you press <b>Apply</b>; press again to unmark.</p>`;
  const tip = {
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
    apply: `<p>Carries out the marks: <i>reject</i> and <i>defer</i> are recorded
      in diaspora.ledger on ${tgt}; <i>accept</i> and <i>retry</i> start one
      import run (test branch, <b>${esc(gate())}</b>).</p>
      <p>${tgt} itself changes only when you press <b>Accept: promote</b>
      on the finished run.</p>`,
  }[what];
  const wait = what === "apply" && S.run ?
    `<p><b>Finish the run above first</b> (promote it or drop it).</p>` : "";
  return tip ? wait + tip + (what === "apply" ? "" : later) : "";
}
let tipFor = null, tipTimer = 0;
function showTip(el) {
  const t = $("tip"), html = help(el.dataset.tip);
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
  const busy = S.busy, blocked = S.busy || !!S.run;
  const list = S.commits.filter(c => (all || (c.disposition !== "ignore" &&
    c.status !== "deferred") || c.mark) && (!peer || c.peer === peer));
  if (!S.scanned) { setHTML($("rows"),
    '<tr><td colspan="6" class="dim">scanning ...</td></tr>'); return; }
  if (!list.length) { setHTML($("rows"),
    '<tr><td colspan="6" class="dim">Nothing to import' +
    (all ? "" : " (tick “show ignored and deferred” for the rest)") + '.</td></tr>'); return; }
  const multi = S.peers.length > 1;
  setHTML($("rows"), list.map(c => {
    const again = c.status === "failed" || c.tried.length;
    const opts = again ? ["retry", "reject", "defer"] : ["accept", "reject", "defer"];
    const btns = opts.map(m => `<button class="${m}${c.mark === m ? " on" : ""}"
      data-sha="${c.commit}" data-mark="${m}" data-tip="${m}" ${busy ? "disabled" : ""}>${
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
      <td class="subj">${esc(c.subject)} ${label(c)}
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
  let stage = R.stage === "ready" ? `tested: ${done} of ${R.items.length} passed` :
    paused ? "waiting: a conflict needs a person" :
    S.busy ? "running" : "stopped (blocked or interrupted)";
  let acts = "";
  if (R.stage === "ready") acts =
    `<button class="primary" data-run="promote" ${dis} ${done ? "" : "disabled"}>Accept: promote to ${esc(R.target)}</button>
     <button class="danger" data-run="abort" ${dis}>Drop the run</button>`;
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
  setHTML(el, `<h2>Run ${esc(R.id)} <span class="dim">· ${stage} · branch ${esc(R.branch)}</span></h2>
    <div class="items">${R.items.map(i => `<div><span class="st-${esc(i.state)}">${
      esc(i.state.padEnd(9))}</span> ${esc(i.sha.slice(0, 10))} ${esc(i.subject)}${
      i.note ? ` <span class="dim">— ${esc(i.note)}</span>` : ""}</div>`).join("")}</div>
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
    esc(S.head)} · peers ${S.peers.map(p => esc(p.name)).join(", ")} · gate: ${
    esc(S.tests.join(" → ") || "none")}${S.strategy === "batch" ? " (batch)" : ""}`;
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
  if (S.agent_cost) st += ` · agent $${S.agent_cost.toFixed(2)}`;
  $("status").textContent = st;
  $("status").className = "status" + (j && j.error ? " err" : "");
  $("fetch").disabled = $("rescan").disabled = S.busy;
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
  if (b.dataset.run) {
    const a = b.dataset.run;
    if (a === "abort" && !confirm("Drop the whole run? Imports that passed are thrown away.")) return;
    const keep = $("keeplog") ? $("keeplog").checked : true;
    return act("/api/run", {action: a, keep_log: keep});
  }
});
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
