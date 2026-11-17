# SPDX-License-Identifier: GPL-3.0-or-later
"""Where imports are applied and tested, and how test levels are run.

The default workspace is a git worktree of its own, kept between runs so
builds stay incremental and your checkout is never touched.  With
`workspace = inplace` the import branch is checked out in your checkout
instead (it must be clean), which reuses the build tree you already have.
"""

import hashlib
import os
import signal
import subprocess
import threading
import time

from .gitutil import DiasporaError


class Workspace:
    def __init__(self, repo, conf):
        self.repo = repo
        self.inplace = conf.workspace == "inplace"
        cache = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser(
            "~/.cache")
        tag = "%s-%s" % (os.path.basename(repo.top), hashlib.sha1(
            repo.common.encode()).hexdigest()[:8])
        home = os.path.join(cache, "diaspora", tag)
        if self.inplace:
            self.path = repo.top
        else:
            self.path = os.path.abspath(os.path.expanduser(
                conf.worktree)) if conf.worktree else os.path.join(home,
                                                                    "src")
        self.build = os.path.abspath(os.path.expanduser(conf.build)) \
            if conf.build else (None if self.inplace
                                else os.path.join(home, "build"))

    def git(self, *args, **kw):
        return self.repo.git(*args, cwd=self.path, **kw)

    def proc(self, *args, **kw):
        return self.repo.proc(*args, cwd=self.path, **kw)

    def ok(self, *args, **kw):
        return self.repo.ok(*args, cwd=self.path, **kw)

    def head(self):
        return self.git("rev-parse", "HEAD")

    def tree(self):
        return self.git("rev-parse", "HEAD^{tree}")

    def dirty(self):
        return bool(self.git("status", "--porcelain",
                             "--untracked-files=no"))

    def _ensure_worktree(self, base):
        repo = self.repo
        registered = os.path.realpath(self.path) in [
            os.path.realpath(line[9:]) for line in repo.git(
                "worktree", "list", "--porcelain").splitlines()
            if line.startswith("worktree ")]
        if registered and os.path.isdir(self.path):
            return
        repo.git("worktree", "prune")
        if os.path.exists(self.path) and os.listdir(self.path):
            raise DiasporaError(
                "%s exists but is not a worktree of this repository; remove "
                "it or set diaspora.worktree" % self.path)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        repo.git("worktree", "add", "--quiet", "--detach", self.path, base)

    def begin(self, branch, base):
        """Check out a new branch `branch` at `base`; return what to restore."""
        if self.inplace:
            if self.dirty():
                raise DiasporaError(
                    "workspace = inplace needs a clean checkout: commit or "
                    "stash your changes in %s first" % self.path)
            restore = self.repo.current_branch() or self.head()
            self.git("checkout", "--quiet", "-b", branch, base)
            return restore
        self._ensure_worktree(base)
        self.proc("cherry-pick", "--quit", check=False)
        self.git("reset", "--quiet", "--hard")
        self.git("checkout", "--quiet", "--force", "-B", branch, base)
        return None

    def end(self, restore):
        """Let go of the branch so it can be merged or deleted."""
        if self.inplace:
            if restore:
                self.git("checkout", "--quiet", restore)
        elif os.path.isdir(self.path):
            self.proc("checkout", "--quiet", "--detach", check=False)


def fmt_secs(secs):
    secs = int(secs)
    if secs < 60:
        return "%ds" % secs
    if secs < 3600:
        return "%dm%02ds" % (secs // 60, secs % 60)
    return "%dh%02dm" % (secs // 3600, secs % 3600 // 60)


class Tester:
    """Runs test levels in the workspace; remembers what passed where."""

    def __init__(self, conf, workspace, log_dir, say, verbose=False):
        self.conf = conf
        self.ws = workspace
        self.log_dir = log_dir
        self.say = say
        self.verbose = verbose
        self.passed = set()             # (tree, level)

    def run_level(self, name, tag, env=None):
        """Run one level; return (ok, log path)."""
        level = self.conf.tests[name]
        os.makedirs(self.log_dir, exist_ok=True)
        log = os.path.join(self.log_dir, "%s-%s.log" % (tag, name))
        full_env = dict(os.environ)
        full_env.update({
            "DIASPORA_SRC": self.ws.path, "DIASPORA_REPO": self.ws.repo.top,
            "DIASPORA_BUILD": self.ws.build or "", "DIASPORA_LEVEL": name})
        full_env.update(env or {})
        for attempt in range(level.retry + 1):
            start = time.time()
            ok, why = self._run(level, log, full_env, attempt)
            took = fmt_secs(time.time() - start)
            if ok:
                self.say("test %s: ok (%s)" % (name, took))
                return True, log
            again = attempt < level.retry
            self.say("test %s: FAILED (%s%s)%s" % (
                name, took, ", " + why if why else "",
                " - retrying" if again else ""))
        return False, log

    def _run(self, level, log, env, attempt):
        timed_out = []
        with open(log, "a" if attempt else "w", encoding="utf-8",
                  errors="replace") as out:
            out.write("### %s: %s\n### in %s\n" % (level.name, level.run,
                                                   self.ws.path))
            out.flush()
            proc = subprocess.Popen(
                ["bash", "-c", level.run], cwd=self.ws.path, env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, start_new_session=True, text=True,
                errors="replace")

            def kill():
                timed_out.append(True)
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass

            timer = None
            if level.timeout:
                timer = threading.Timer(level.timeout, kill)
                timer.start()
            try:
                with proc.stdout:
                    for line in proc.stdout:
                        out.write(line)
                        if self.verbose:
                            print("      | " + line, end="")
                proc.wait()
            finally:
                if timer:
                    timer.cancel()
            out.write("### exit %d\n" % proc.returncode)
        if timed_out:
            return False, "timed out after %s" % fmt_secs(level.timeout)
        return proc.returncode == 0, "exit %d" % proc.returncode

    def run(self, levels, tag, env=None):
        """Run levels and what they need; return (ok, failed level, log)."""
        tree = self.ws.tree()
        for name in self.conf.chain(levels):
            if (tree, name) in self.passed:
                continue
            ok, log = self.run_level(name, tag, env)
            if not ok:
                return False, name, log
            self.passed.add((tree, name))
        return True, None, None


def tail(path, lines=60, width=4000):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])[-width:]
