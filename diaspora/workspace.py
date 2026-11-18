# SPDX-License-Identifier: GPL-3.0-or-later
"""Where imports are applied and tested, and how test levels are run.

The default workspace is a git worktree of its own, kept between runs so
builds stay incremental and your checkout is never touched.  With
`workspace = inplace` the import branch is checked out in your checkout
instead (it must be clean), which reuses the build tree you already have.
"""

import hashlib
import json
import os
import signal
import subprocess
import threading
import time

from .config import CONF_NAME, LOCAL_NAME
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
    """Runs test levels in the workspace; remembers what passed where.

    A level either passes by exit status, or (pass = no-new-failures) it
    reports the tests that fail and passes when none of them is new: the
    failures are compared with those of the same level on the code before
    the import (the reference), which is measured once and kept in
    .git/diaspora/results.
    """

    def __init__(self, conf, workspace, log_dir, say, verbose=False):
        self.conf = conf
        self.ws = workspace
        self.log_dir = log_dir
        self.say = say
        self.verbose = verbose
        self.verified = set()     # (tree, level): known to pass on that tree
        self.current = None       # the tree the build directory holds
        self.compared = False     # the last failure was new failing tests
        self.detail = ""          # ... and which
        self.new_failures = []    # ... all of them
        self.meta = {CONF_NAME, LOCAL_NAME, conf.ledger, conf.log}

    # -- measured failures ----------------------------------------------------
    def key(self, rev="HEAD"):
        """Identifies a source tree, ignoring diaspora's own files."""
        lines = [line for line in self.ws.repo.git(
            "ls-tree", "-r", rev, cwd=self.ws.path).splitlines()
            if line.partition("\t")[2] not in self.meta]
        return hashlib.sha1("\n".join(lines).encode()).hexdigest()

    def _result_file(self, rev, name):
        return os.path.join(self.ws.repo.state_dir("results", name),
                            self.key(rev) + ".json")

    def store(self, rev, name, failures):
        """Record what failed on this tree.

        Measurements of one tree add up: a test that fails in one run and
        passes in the next is a test that fails there, as far as blaming
        an import for it goes.
        """
        failures = set(failures) | (self.load(rev, name) or set())
        with open(self._result_file(rev, name), "w",
                  encoding="utf-8") as handle:
            json.dump({"command": self.conf.tests[name].run,
                       "time": time.time(), "failures": sorted(failures)},
                      handle, indent=0)

    def load(self, rev, name):
        """Failures measured earlier for this tree, or None."""
        level = self.conf.tests[name]
        try:
            with open(self._result_file(rev, name),
                      encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return None
        if data.get("command") != level.run:
            return None
        if level.reference_age and \
                time.time() - data.get("time", 0) > level.reference_age:
            return None
        return set(data.get("failures", []))

    # -- running --------------------------------------------------------------
    def _attempt(self, name, tag, env, again):
        """Run the level's command once: (ok, why not, failures, log)."""
        level = self.conf.tests[name]
        os.makedirs(self.log_dir, exist_ok=True)
        log = os.path.join(self.log_dir, "%s-%s.log" % (tag, name))
        listing = log[:-4] + ".failures"
        if os.path.exists(listing):
            os.remove(listing)
        full_env = dict(os.environ)
        full_env.update({
            "DIASPORA_SRC": self.ws.path, "DIASPORA_REPO": self.ws.repo.top,
            "DIASPORA_BUILD": self.ws.build or "", "DIASPORA_LEVEL": name,
            "DIASPORA_FAILURES": listing})
        full_env.update(env or {})
        ok, why = self._exec(level, log, full_env, again)
        failures = None
        if ok and level.compares:
            try:
                with open(listing, encoding="utf-8",
                          errors="replace") as handle:
                    failures = {line.strip() for line in handle
                                if line.strip()}
            except OSError:
                ok, why = False, "the command did not write $DIASPORA_FAILURES"
        return ok, why, failures, log

    def _exec(self, level, log, env, again):
        timed_out = False
        with open(log, "a" if again else "w", encoding="utf-8",
                  errors="replace") as out:
            out.write("### %s: %s\n### in %s\n" % (level.name, level.run,
                                                   self.ws.path))
            out.flush()
            proc = subprocess.Popen(
                ["bash", "-c", level.run], cwd=self.ws.path, env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, start_new_session=True, text=True,
                errors="replace")

            def pump():
                try:
                    for line in proc.stdout:
                        out.write(line)
                        if self.verbose:
                            print("      | " + line, end="")
                except ValueError:      # the log was closed under us
                    pass

            # The output is read on the side: a build tool may leave helper
            # processes behind that keep the pipe open after the command
            # itself has ended, and the level must not wait for those.
            reader = threading.Thread(target=pump, daemon=True)
            reader.start()
            try:
                proc.wait(timeout=level.timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                proc.wait()
            reader.join(10)
            if not reader.is_alive():
                proc.stdout.close()
            out.write("### exit %d\n" % proc.returncode)
        if timed_out:
            return False, "timed out after %s" % fmt_secs(level.timeout)
        return proc.returncode == 0, "exit %d" % proc.returncode

    def _ready(self, chain, name, tag, env, tree):
        """Have the build directory hold `tree` before level `name` runs.

        Measuring a reference takes the workspace and the build to another
        tree; the levels before `name` that build are then run again.
        """
        if self.current != tree:
            for earlier in chain[:chain.index(name)]:
                if not self.conf.tests[earlier].compares:
                    ok, log = self._level(earlier, tag, env, tree, None)
                    if not ok:
                        return False, log
            self.current = tree
        return True, None

    def _level(self, name, tag, env, tree, reference, recheck=None,
               ready=None):
        """Run one level with its retries; return (ok, log)."""
        level = self.conf.tests[name]
        new = None                 # failures that are new in every attempt
        attempt = 0
        while True:
            if ready:
                ok, log = ready()
                if not ok:
                    return False, log
            start = time.time()
            ok, why, failures, log = self._attempt(name, tag, env, attempt)
            took = fmt_secs(time.time() - start)
            note = ""
            if ok and level.compares:
                self.store("HEAD", name, failures)
                note = ", %d failing" % len(failures)
                if reference is not None:
                    fresh = failures - reference
                    new = fresh if new is None else new & fresh
                    if new and recheck is not None:
                        # Before the import is blamed: do they fail without
                        # it, now?  The reference is one earlier run, and a
                        # test that comes and goes may have passed in it.
                        self.say("test %s: %d failure%s not in the "
                                 "reference (%s)" % (
                                     name, len(new),
                                     "" if len(new) == 1 else "s", took))
                        reference = reference | recheck(name)
                        recheck = None
                        cleared = len(new) - len(new - reference)
                        new -= reference
                        if cleared:
                            note += ", %d also fail%s without the import" % (
                                cleared, "s" if cleared == 1 else "")
                    ok = not new
                    why = "%d new failure%s" % (len(new),
                                                "" if len(new) == 1 else "s")
                    note += ", none new" if ok else ""
                else:
                    note += ", recorded"
            if ok:
                self.say("test %s: ok (%s%s)" % (name, took, note))
                return True, log
            again = attempt < level.retry
            self.say("test %s: FAILED (%s, %s)%s" % (
                name, took, why, " - retrying" if again else ""))
            if not again:
                break
            attempt += 1
        if new:
            names = sorted(new)
            self.compared = True
            self.new_failures = names
            self.detail = "%d new failure%s: %s%s" % (
                len(names), "" if len(names) == 1 else "s",
                ", ".join(names[:8]), " ..." if len(names) > 8 else "")
            with open(log[:-4] + ".new-failures", "w",
                      encoding="utf-8") as handle:
                handle.write("\n".join(names) + "\n")
        return False, log

    def run(self, levels, tag, env=None, reference=None, recheck=None,
            force=False):
        """Run levels and what they need; return (ok, failed level, log).

        `reference(level)` gives the failures of a comparing level on the
        code before the import, and `recheck(level)` measures them again.
        Without a reference such a level only has to run, and what it
        reports is recorded.  `force` runs levels already seen to pass.
        """
        tree = self.ws.tree()
        chain = self.conf.chain(levels)
        todo = [n for n in chain
                if force or (tree, n) not in self.verified]
        if not todo:
            return True, None, None
        # references first: measuring one takes the workspace and the build
        # directory to another tree
        refs = {}
        if reference:
            for name in todo:
                if self.conf.tests[name].compares:
                    refs[name] = reference(name)
        self.compared, self.detail, self.new_failures = False, "", []
        for name in todo:
            ok, log = self._level(
                name, tag, env, tree, refs.get(name),
                recheck if name in refs else None,
                lambda name=name: self._ready(chain, name, tag, env, tree))
            if not ok:
                return False, name, log
            self.verified.add((tree, name))
        return True, None, None


def tail(path, lines=60, width=4000):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])[-width:]
