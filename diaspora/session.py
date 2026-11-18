# SPDX-License-Identifier: GPL-3.0-or-later
"""An import (or export) run: apply, test, record, promote.

Imports are applied one commit at a time on a branch of their own
(`diaspora/import/<id>`) in the workspace.  Each one must pass the test
levels diaspora.conf requires for it; what passes is fast-forwarded onto
the target branch together with the ledger entries.  The state of a run
lives in .git/diaspora/session.json, so a run that stops for a conflict
can be picked up again with `diaspora continue`.
"""

import json
import os
import re
import time

from . import ledger as L
from . import scan
from .gitutil import DiasporaError, GitError, run
from .workspace import Tester, Workspace, tail

PORT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": [
            "ported", "not-applicable", "superseded", "failed"]},
        "summary": {"type": "string"},
    },
    "required": ["status", "summary"],
}
FIX_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["fixed", "unrelated",
                                               "failed"]},
        "summary": {"type": "string"},
    },
    "required": ["status", "summary"],
}

PORT_PROMPT = """\
You are bringing one commit from the repository "{source}" into this
working tree ({ws}), which holds {dest}.  Bring over the INTENT of the
change; the diff is only a starting point.

Commit {sha}:
{message}

See it with `git show {sha}`.

State of the working tree: {state}
{divergence}
Do this:
 1. Resolve every conflict and check that the applied hunks make sense
    against the code here.  Where the code here has diverged, re-implement
    the fix or feature in its terms.
 2. Bring the commit's tests along, adapted if needed.
 3. `git add` every file you changed or created.
 4. If the change does not belong here (the code it changes is gone:
    "not-applicable") or is already present in another form
    ("superseded"), undo everything with `git reset --hard HEAD` and
    report that status.

Do not commit, do not switch or create branches, do not push, and leave
{meta} and files unrelated to this change alone.  The tool commits and
runs the tests after you finish.

Report `status` (ported | not-applicable | superseded | failed) and a
`summary` of what you did or why it failed: one sentence, at most 150
characters; it goes into the ledger.
"""

FIX_PROMPT = """\
The commit at HEAD of this working tree ({ws}) is an import of commit
{sha} ("{subject}") from the repository "{source}".  After the import the
required test level "{level}" failed.  The same level passes without the
import.

Test command: {command}
{detail}End of the log ({log}):
---
{tail}
---

Find out why the import fails the test and repair the import: edit the
files and `git add` them.  Change only what this import needs.  If the
cause lies outside the import (the build system, a flaky or unrelated
test, the environment), do not repair that here: undo any edit you made
and report "unrelated".  Do not commit (the tool amends the commit and
runs the tests again), do not switch branches, do not push, do not weaken
or delete tests to make them pass.

Report `status`: "fixed" if you changed the import in a way that should
make the test pass, "unrelated" if the failure is not caused by this
import, "failed" if you could not repair it; and a `summary`: one
sentence, at most 150 characters.
"""


def _brief(text, limit=200):
    """An agent's summary, cut at a sentence or word end."""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    stop = max(cut.rfind(". "), cut.rfind("; "))
    if stop > limit // 2:
        return cut[:stop + 1]
    return cut[:cut.rfind(" ")] + " ..."


class Blocked(Exception):
    """The tests fail without the import: nothing can be validated."""


class Session:
    def __init__(self, repo, conf, ui, agent, interactive=True,
                 assume_yes=False, verbose=False):
        self.repo, self.conf, self.ui, self.agent = repo, conf, ui, agent
        self.interactive = interactive
        self.assume_yes = assume_yes
        self.verbose = verbose
        self.file = os.path.join(repo.state_dir(), "session.json")
        self.ws = Workspace(repo, conf)
        self.data = None
        self.tester = None
        self._remeasured = set()     # (commit, level) references run again

    # -- persistence ----------------------------------------------------------
    def exists(self):
        return os.path.exists(self.file)

    def load(self):
        if not self.exists():
            raise DiasporaError("no diaspora run in progress")
        with open(self.file, encoding="utf-8") as handle:
            self.data = json.load(handle)
        self._tester()
        return self

    def save(self):
        tmp = self.file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(self.data, handle, indent=1)
        os.replace(tmp, self.file)

    def _tester(self):
        self.log_dir = os.path.join(self.repo.state_dir("logs"),
                                    self.data["id"])
        self.tester = Tester(self.conf, self.ws, self.log_dir, self.ui.step,
                             self.verbose)
        self.agent.log_dir = self.log_dir

    def _archive(self):
        done = os.path.join(self.repo.state_dir("sessions"),
                            self.data["id"] + ".json")
        os.replace(self.file, done)

    @property
    def export(self):
        return self.data["dir"] == "export"

    # -- starting -------------------------------------------------------------
    def start(self, direction, target, commits, verdicts=(), peer=None):
        if self.exists():
            raise DiasporaError(
                "a diaspora run is already in progress; see `diaspora "
                "status`, then `diaspora continue` or `diaspora abort`")
        if not self.repo.identity_ok():
            raise DiasporaError(
                "git does not know who you are; set user.name and "
                "user.email (git config) before importing")
        sid = time.strftime("%Y%m%d-%H%M%S")
        if direction == "export":
            branch = "diaspora/export/%s/%s" % (peer.name, sid)
            base = self.repo.rev(peer.ref)
        else:
            branch = "diaspora/import/%s" % sid
            base = self.repo.rev(target)
        if not base:
            raise DiasporaError("branch %s does not exist" % target)
        self.data = {
            "id": sid, "dir": direction, "target": target, "base": base,
            "branch": branch, "stage": "running", "restore": None,
            "peer": peer.name if peer else None,
            "verdicts": list(verdicts), "entries": [],
            "ledger_commit": False,
            "items": [{"peer": c.peer, "sha": c.sha, "subject": c.subject,
                       "kind": c.kind, "state": "queued", "note": ""}
                      for c in commits],
        }
        self._tester()
        self.data["restore"] = self.ws.begin(branch, base)
        self.save()
        return self.run()

    # -- the loop ---------------------------------------------------------------
    def run(self):
        items = self.data["items"]
        each = self.conf.strategy == "each"
        try:
            if self.data.get("retest"):
                # the run was moved onto a new target: what it holds so far
                # has to pass there too
                self._test_batch()
                self.data["retest"] = False
                self.save()
            for num, item in enumerate(items, 1):
                if item["state"] not in ("queued", "paused", "picked"):
                    continue
                if item["state"] == "paused":
                    self._explain_pause(item)
                    return "paused"
                if item["state"] == "queued":
                    self.ui.item(num, len(items), item)
                    self._pick(item)
                    self.save()
                    if item["state"] == "paused":
                        self._explain_pause(item)
                        return "paused"
                if item["state"] == "picked" and each:
                    self._test_item(item)
                    self.save()
                self._report(item)
            if not each:
                self._test_batch()
        except Blocked as exc:
            self.save()
            self.ui.warn(str(exc))
            self.ui.say("Fix the test setup, or fix %s itself (the run "
                        "follows it), then `diaspora continue`; or "
                        "`diaspora abort`." % self.data["target"])
            return "blocked"
        self._finalize()
        return self.finish()

    def _report(self, item):
        state = item["state"]
        if state == "done":
            self.ui.step("-> %s as %s" % (item["mode"], item["our"][:10]))
        elif state == "failed":
            self.ui.step("-> failed: %s" % item["note"])
        elif state in (L.SUPERSEDED, L.NOT_APPLICABLE):
            self.ui.step("-> %s: %s" % (state, item["note"]))

    # -- applying one commit ------------------------------------------------------
    def _reset(self, sha):
        self.ws.proc("cherry-pick", "--quit", check=False)
        self.ws.git("reset", "--quiet", "--hard", sha)

    def _unmerged(self):
        return self.ws.git("diff", "--name-only",
                           "--diff-filter=U").splitlines()

    def _drop(self, paths):
        """Undo a commit's changes to files that must not travel."""
        for path in paths:
            if self.repo.ok("cat-file", "-e", "HEAD:" + path,
                            cwd=self.ws.path):
                self.ws.proc("checkout", "--quiet", "HEAD", "--", path,
                             check=False)
            else:
                self.ws.proc("rm", "--quiet", "--force", "--cached",
                             "--ignore-unmatch", "--", path, check=False)
                full = os.path.join(self.ws.path, path)
                if os.path.isfile(full):
                    os.remove(full)

    def _fail(self, item, note):
        self._reset(item["head0"])
        item["state"], item["note"] = "failed", note

    def _pick(self, item):
        peer = self.conf.peer(item["peer"])
        commit = scan.one(self.repo, item["sha"])
        self._reset("HEAD")          # leftovers of an interrupted attempt
        item["head0"] = self.ws.head()
        try:
            proc = self.ws.proc("cherry-pick", "--no-commit", item["sha"],
                                check=False)
            self._drop(scan.dropped_paths(self.conf, peer, commit,
                                          self.data["dir"]))
            unmerged = self._unmerged()
            item["conflicts"] = unmerged
            staged = not self.ws.ok("diff", "--cached", "--quiet")
            if proc.returncode and not unmerged and not staged:
                first = (proc.stderr.strip().splitlines() or ["no output"])[0]
                return self._fail(item, "cherry-pick: " + first)
            rewritten = (not self.export
                         and self.conf.rewritten.any(commit.paths))
            adapted = False
            if unmerged or rewritten:
                self.ui.step("cherry-pick: %s" % (
                    "conflicts in " + ", ".join(unmerged[:4])
                    + (" ..." if len(unmerged) > 4 else "") if unmerged
                    else "applies, but touches code rewritten here"))
                if self.agent.port_enabled:
                    status, summary = self._port(item, commit, unmerged)
                    if status in (L.SUPERSEDED, L.NOT_APPLICABLE):
                        self._reset(item["head0"])
                        item["state"], item["note"] = status, summary
                        return None
                    if status != "ported":
                        return self._fail(item, "agent could not port it: "
                                          + summary)
                    adapted, item["note"] = True, summary
                elif unmerged and self.interactive:
                    item["state"], item["note"] = "paused", "conflict"
                    return None
                elif unmerged:
                    return self._fail(item, "conflicts in " + ", ".join(
                        unmerged[:6]))
            else:
                self.ui.step("cherry-pick: clean")
            self._commit(item, commit, adapted)
        except GitError as exc:
            self._fail(item, str(exc))
        return None

    def _commit(self, item, commit, adapted):
        """Commit what is staged for `item`; False if it is not ready."""
        ws = self.ws
        ws.git("add", "--update")
        dropped = set(scan.dropped_paths(
            self.conf, self.conf.peer(item["peer"]), commit,
            self.data["dir"]))
        for path in commit.paths:
            if path not in dropped and os.path.exists(
                    os.path.join(ws.path, path)):
                ws.proc("add", "--", path, check=False)
        unmerged = self._unmerged()
        check = ws.proc("diff", "--cached", "--check", "--",
                        *item["conflicts"], check=False).stdout \
            if item.get("conflicts") else ""
        if unmerged or "conflict marker" in check:
            item["note"] = "unresolved conflicts in " + ", ".join(
                unmerged or sorted({line.split(":")[0] for line in
                                    check.splitlines()
                                    if "conflict marker" in line}))
            return False
        if ws.ok("diff", "--cached", "--quiet"):
            self._reset(item["head0"])
            item["state"] = L.SUPERSEDED
            item["note"] = item["note"] if adapted and item["note"] else \
                "nothing left to apply: the change is already here"
            return True
        mode = L.ADAPTED if adapted else L.APPLIED
        message = self.repo.git("show", "-s", "--format=%B", item["sha"])
        if self.export:
            message = "\n".join(
                line for line in message.splitlines()
                if not line.startswith("Diaspora-")).strip() + "\n"
        else:
            message = run(
                ["git", "interpret-trailers", "--trailer",
                 "Diaspora-Origin: %s@%s (%s)" % (item["peer"], item["sha"],
                                                 mode)],
                input=message.strip() + "\n").stdout
        name, email, date = self.repo.git(
            "show", "-s", "--format=%an%x1f%ae%x1f%aI",
            item["sha"]).split("\x1f")
        ws.git("commit", "--quiet", "--author", "%s <%s>" % (name, email),
               "--date", date, "-F", "-", input=message)
        item["our"], item["mode"], item["state"] = ws.head(), mode, "picked"
        self._extra(item, commit.paths)
        return True

    def _extra(self, item, expected):
        """Note files the import changed that the peer's commit did not."""
        changed = self.ws.git("diff", "--name-only", item["head0"],
                              "HEAD").splitlines()
        extra = sorted(set(changed) - set(expected))
        item["extra"] = extra
        if extra:
            self.ui.step("also changed: " + ", ".join(extra))

    def _port(self, item, commit, unmerged):
        peer = self.conf.peer(item["peer"])
        if unmerged:
            state = ("`git cherry-pick --no-commit %s` stopped with "
                     "conflicts in: %s." % (item["sha"][:12],
                                            ", ".join(unmerged)))
        else:
            state = ("the cherry-pick applied without conflicts, but it "
                     "touches code this repository has rewritten, so the "
                     "literal diff may be wrong here.")
        divergence = ""
        if not self.export and (self.conf.rewritten or self.conf.removed):
            divergence = ("\nHow this repository differs from its peers: "
                          "rewritten here: %s; removed here: %s.\n" % (
                              " ".join(self.conf.rewritten.patterns) or "-",
                              " ".join(self.conf.removed.patterns) or "-"))
        if self.export:
            source = "ours (%s)" % os.path.basename(self.repo.top)
            dest = "the code of the peer \"%s\" (%s), for which we are " \
                   "preparing a contribution" % (peer.name, peer.url)
        else:
            source = "%s (%s)" % (peer.name, peer.url)
            dest = "our code, on a branch made for this import"
        prompt = PORT_PROMPT.format(
            source=source, dest=dest, ws=self.ws.path, sha=item["sha"],
            message=(commit.subject + "\n\n" + commit.body).strip(),
            state=state, divergence=divergence,
            meta="diaspora.conf, %s" % self.conf.ledger)
        self.ui.step("agent: porting ...")
        answer = self.agent.ask("port", prompt, PORT_SCHEMA, cwd=self.ws.path)
        if not answer:
            return "failed", self.agent.last_error or "no answer"
        summary = _brief(answer.get("summary"))
        self.ui.step("agent: %s - %s" % (answer.get("status"), summary))
        return answer.get("status"), summary

    # -- testing ------------------------------------------------------------------
    def _levels(self, item):
        if self.export:
            return list(self.conf.peer(item["peer"]).export_test)
        return self.conf.levels_for(item["peer"], item["kind"])

    def _env(self, item):
        return {"DIASPORA_PEER": item["peer"], "DIASPORA_COMMIT": item["sha"]}

    def _baseline(self, item, level):
        """Does `level` pass right before this import?  Blocked if not."""
        ws = self.ws
        before = self.repo.git("rev-parse", item["head0"] + "^{tree}")
        if (before, level) in self.tester.verified:
            return
        self.ui.step("checking that %s passes without this import ..."
                     % level)
        ws.git("checkout", "--quiet", "--force", "--detach", item["head0"])
        try:
            ok, failed, log = self.tester.run([level], "baseline")
        finally:
            ws.git("checkout", "--quiet", "--force", self.data["branch"])
        if not ok:
            # the import stays on the branch: `continue` tests it again
            raise Blocked(
                "test level %s fails without the import (at %s), so no "
                "import can be validated.  Log: %s" % (
                    failed, item["head0"][:10], log))

    def _reference(self, sha, level, again=False):
        """What fails in `level` on commit `sha`, the code before an import.

        `again` measures it once more (once per run): an import is about to
        be blamed for failures that the stored measurement does not have.
        """
        found = self.tester.load(sha, level)
        if again and (sha, level) in self._remeasured:
            again = False
        if found is not None and not again:
            return found
        if again:
            self._remeasured.add((sha, level))
            self.ui.step("measuring %s on %s again: do they fail there "
                         "too? ..." % (level, sha[:10]))
        else:
            self.ui.step("measuring %s on %s, before the import ..." % (
                level, sha[:10]))
        # (--force: the build may have changed tracked files; they are build
        # products here, the import itself is committed)
        self.ws.git("checkout", "--quiet", "--force", "--detach", sha)
        try:
            ok, failed, log = self.tester.run([level], "reference",
                                              force=again)
        finally:
            self.ws.git("checkout", "--quiet", "--force", self.data["branch"])
        if not ok:
            raise Blocked(
                "test level %s does not run on the code before the import "
                "(at %s), so no import can be validated.  Log: %s" % (
                    failed, sha[:10], log))
        return self.tester.load(sha, level) or set()

    def _test_item(self, item):
        levels = self._levels(item)
        if not levels:
            item["state"], item["tests"] = "done", []
            if not self.conf.has_requirements() and not self.export:
                self.ui.step("not tested: diaspora.conf requires no test")
            return
        attempts = self.conf.agent.fix_attempts if self.agent.port_enabled \
            else 0
        tag = "%s-%s" % (item["peer"], item["sha"][:10])
        while True:
            ok, level, log = self.tester.run(
                levels, tag, self._env(item),
                lambda name: self._reference(item["head0"], name),
                lambda name: self._reference(item["head0"], name, True))
            if ok:
                item["state"] = "done"
                item["tests"] = self.conf.chain(levels)
                return
            detail = self.tester.detail
            if not self.tester.compared:
                self._baseline(item, level)
            if attempts > 0:
                attempts -= 1
                if self._fix(item, level, log, detail):
                    continue
            self._fail(item, "test %s failed%s (log: %s)" % (
                level, ", " + detail if detail else "", log))
            return

    def _fix(self, item, level, log, detail=""):
        prompt = FIX_PROMPT.format(
            ws=self.ws.path, sha=item["sha"], subject=item["subject"],
            source=item["peer"], level=level,
            command=self.conf.tests[level].run, log=log, tail=tail(log),
            detail="Tests that pass without the import and fail with it: "
                   "%s (full list: %s).  Tests that failed before are not "
                   "your concern.\n" % (detail, log[:-4] + ".new-failures")
            if detail else "")
        # tracked files the build itself changed (a regenerated configure,
        # say) are not part of the repair
        built = set(self.ws.git("diff", "--name-only").splitlines())
        self.ui.step("agent: looking at the %s failure ..." % level)
        answer = self.agent.ask("fix", prompt, FIX_SCHEMA, cwd=self.ws.path)
        if not answer:
            self.ui.step("agent: %s" % (self.agent.last_error or "no answer"))
            return False
        summary = _brief(answer.get("summary"))
        self.ui.step("agent: %s - %s" % (answer.get("status"), summary))
        if answer.get("status") != "fixed":
            return False
        ws = self.ws
        edited = [path for path in ws.git("diff", "--name-only").splitlines()
                  if path not in built]
        if edited:
            ws.git("add", "--", *edited)
        if ws.ok("diff", "--cached", "--quiet"):
            return False
        message = ws.git("show", "-s", "--format=%B", "HEAD")
        message = re.sub(r"^(Diaspora-Origin: .*)\(applied\)\s*$",
                         r"\1(adapted)", message, flags=re.M)
        ws.git("commit", "--quiet", "--amend", "-F", "-",
               input=message + "\n")
        item["our"], item["mode"] = ws.head(), L.ADAPTED
        item["note"] = (item["note"] + "; " if item["note"] else "") + \
            "repaired after %s failed: %s" % (level, summary)
        self._extra(item, scan.one(self.repo, item["sha"]).paths)
        return True

    def _test_batch(self):
        picked = [i for i in self.data["items"] if i["state"] == "picked"]
        levels = []
        for item in picked:
            levels += [lv for lv in self._levels(item) if lv not in levels]
        if not picked:
            return
        if levels:
            self.ui.say("Testing %d import%s together ..." % (
                len(picked), "" if len(picked) == 1 else "s"))
            ok, level, log = self.tester.run(
                levels, "batch", None,
                lambda name: self._reference(picked[0]["head0"], name),
                lambda name: self._reference(picked[0]["head0"], name, True))
        else:
            ok = True
        if ok:
            for item in picked:
                item["state"], item["tests"] = "done", \
                    self.conf.chain(levels)
            self.save()
            return
        self.ui.say("The batch fails %s; testing the imports one by one."
                    % level)
        self._reset(picked[0]["head0"])
        for num, item in enumerate(picked, 1):
            self.ui.item(num, len(picked), item)
            item["head0"] = self.ws.head()
            proc = self.ws.proc("cherry-pick", "--allow-empty", item["our"],
                                check=False)
            if proc.returncode:
                self.ws.proc("cherry-pick", "--abort", check=False)
                self._fail(item, "does not apply without an earlier import "
                                 "that failed its tests")
            else:
                item["our"] = self.ws.head()
                self._test_item(item)
            self.save()
            self._report(item)

    # -- recording ------------------------------------------------------------------
    def _entries(self):
        out = list(self.data["verdicts"])
        for item in self.data["items"]:
            state = item["state"]
            if self.export:
                if state == "done":
                    out.append(L.entry(
                        item["peer"], item["sha"], L.EXPORTED,
                        item["subject"], item["kind"],
                        note="branch " + self.data["branch"],
                        direction="export"))
                continue
            if state == "done":
                note = item["note"]
                if item.get("extra"):
                    note = (note + "; " if note else "") + \
                        "also changed: " + ", ".join(item["extra"])
                out.append(L.entry(item["peer"], item["sha"], item["mode"],
                                   item["subject"], item["kind"],
                                   note=note or None,
                                   tests=item.get("tests")))
            elif state in ("failed", L.SUPERSEDED, L.NOT_APPLICABLE):
                out.append(L.entry(item["peer"], item["sha"], state,
                                   item["subject"], item["kind"],
                                   note=item["note"] or None))
        return out

    def _commit_ledger(self, entries):
        if not entries:
            return False
        path = os.path.join(self.ws.path, self.conf.ledger)
        L.append(path, entries)
        self.ws.git("add", "--", self.conf.ledger)
        counts = {}
        for item in entries:
            counts[item["verdict"]] = counts.get(item["verdict"], 0) + 1
        subject = "diaspora: " + ", ".join(
            "%d %s" % (n, v) for v, n in sorted(counts.items()))
        body = "\n".join("%-14s %s@%s %s" % (
            e["verdict"], e["peer"], e["commit"][:10], e.get("subject", ""))
            for e in entries)
        self.ws.git("commit", "--quiet", "-F", "-",
                    input=subject + "\n\n" + body + "\n")
        return True

    def _finalize(self):
        entries = self._entries()
        self.data["entries"] = entries
        if self.export:
            self.data["stage"] = "exported"
        else:
            self.data["ledger_commit"] = self._commit_ledger(entries)
            self.data["stage"] = "ready"
        self.save()

    # -- finishing --------------------------------------------------------------------
    def counts(self):
        out = {}
        for item in self.data["items"]:
            out[item["state"]] = out.get(item["state"], 0) + 1
        return out

    def finish(self):
        data = self.data
        done = [i for i in data["items"] if i["state"] == "done"]
        failed = [i for i in data["items"] if i["state"] == "failed"]
        self.ui.say("")
        if self.export:
            return self._finish_export(done, failed)
        self.ui.say("%d imported, %d failed, %d not needed." % (
            len(done), len(failed),
            len(data["items"]) - len(done) - len(failed)))
        for item in failed:
            self.ui.say("  failed  %s %s  %s" % (
                item["sha"][:10], item["subject"][:50], item["note"]))
        tip = self.repo.rev(data["branch"])
        if tip == data["base"]:
            self._cleanup()
            return "nothing"
        what = "%d import%s" % (len(done), "" if len(done) == 1 else "s") \
            if done else "the ledger update"
        policy = self.conf.promote
        if policy == "ask" and not self.assume_yes and self.interactive:
            go = self.ui.confirm("Copy %s to %s?" % (what, data["target"]))
        else:
            go = policy == "yes" or (policy == "ask" and self.assume_yes)
        if not go:
            self.ws.end(data["restore"])
            data["restore"] = None
            self.save()
            self.ui.say("Left on branch %s; `diaspora promote` copies it to "
                        "%s, `diaspora abort` drops it." % (data["branch"],
                                                            data["target"]))
            return "ready"
        return self.promote()

    def _finish_export(self, done, failed):
        data = self.data
        peer = self.conf.peer(data["peer"])
        self.ws.end(data["restore"])
        data["restore"] = None
        for item in failed:
            self.ui.say("  failed  %s %s  %s" % (
                item["sha"][:10], item["subject"][:50], item["note"]))
        entries = data["entries"]
        branch = data["branch"]
        self._archive()
        if not done:
            self.repo.proc("branch", "-D", branch, check=False)
            self.ui.say("Nothing to export.")
            return "nothing"
        self.ui.say("%d commit%s for %s on branch %s (based on %s %s)." % (
            len(done), "" if len(done) == 1 else "s", peer.name, branch,
            peer.name, peer.branch))
        self.ui.say("Nothing was pushed.  To offer it: push the branch to "
                    "your fork and open a pull request,")
        self.ui.say("or mail it: git format-patch %s..%s" % (peer.ref,
                                                              branch))
        if entries:
            record(self.repo, self.conf, self.ui, self.agent, data["target"],
                   entries)
        return "exported"

    def promote(self):
        data, repo, ws = self.data, self.repo, self.ws
        if data["stage"] != "ready":
            raise DiasporaError("the run is not finished (stage: %s); "
                                "`diaspora continue` first" % data["stage"])
        branch, target = data["branch"], data["target"]
        if not repo.is_ancestor(target, branch):
            self._rebase()
        ws.end(data["restore"])
        data["restore"] = None
        self.save()
        where = repo.worktrees().get(target)
        if where:
            proc = repo.proc("merge", "--ff-only", "--quiet", branch,
                             cwd=where, check=False)
        else:
            proc = repo.proc("fetch", "--quiet", ".",
                             "refs/heads/%s:refs/heads/%s" % (branch, target),
                             check=False)
        if proc.returncode:
            raise DiasporaError(
                "cannot fast-forward %s to the tested imports: %s\nThey are "
                "kept on branch %s; `diaspora promote` tries again." % (
                    target, proc.stderr.strip(), branch))
        done = sum(1 for i in data["items"] if i["state"] == "done")
        self.ui.say("%s is now at %s (%d import%s)." % (
            target, repo.rev(target)[:10], done, "" if done == 1 else "s"))
        self._cleanup()
        return "promoted"

    def _rebase(self):
        """The target moved during the run: replay the imports on it."""
        data, ws = self.data, self.ws
        self.ui.say("%s moved during the run; replaying the imports on it."
                    % data["target"])
        if ws.inplace and data["restore"] is None:
            if ws.dirty():
                raise DiasporaError(
                    "%s moved and the imports must be replayed on it, which "
                    "needs a clean checkout (workspace = inplace)"
                    % data["target"])
            data["restore"] = self.repo.current_branch() or ws.head()
            self.save()
        ws.git("checkout", "--quiet", "--force", data["branch"])
        if data["ledger_commit"]:
            ws.git("reset", "--quiet", "--hard", "HEAD~1")
        proc = ws.proc("rebase", "--quiet", data["target"], check=False)
        if proc.returncode:
            ws.proc("rebase", "--abort", check=False)
            if data["ledger_commit"]:
                self._commit_ledger(data["entries"])
            raise DiasporaError(
                "the imports do not replay cleanly on the new %s; they are "
                "kept on branch %s" % (data["target"], data["branch"]))
        levels = []
        for item in data["items"]:
            if item["state"] == "done":
                levels += [lv for lv in self._levels(item)
                           if lv not in levels]
        tip = self.repo.rev(data["target"])
        ok, level, log = self.tester.run(
            levels, "rebased", None,
            lambda name: self._reference(tip, name),
            lambda name: self._reference(tip, name, True)) if levels \
            else (True, None, None)
        if data["ledger_commit"]:
            self._commit_ledger(data["entries"])
        self.save()
        if not ok:
            raise DiasporaError(
                "after replaying on the new %s, test %s fails (log: %s); "
                "the imports are kept on branch %s" % (
                    data["target"], level, log, data["branch"]))

    def _cleanup(self):
        if self.data.get("restore") is not None or not self.ws.inplace:
            self.ws.end(self.data.get("restore"))
        self.repo.proc("branch", "-D", self.data["branch"], check=False)
        self._archive()

    # -- continue / skip / abort ------------------------------------------------------
    def _explain_pause(self, item):
        self.ui.say("")
        self.ui.warn("%s %s does not apply cleanly and no agent is "
                     "available to port it." % (item["sha"][:10],
                                                item["subject"][:60]))
        self.ui.say("Resolve it in %s (edit, then `git add`), then run "
                    "`diaspora continue`." % self.ws.path)
        self.ui.say("`diaspora skip` leaves this commit out; `diaspora "
                    "abort` drops the whole run.")

    def _follow_target(self):
        """The target moved while the run was stopped: move the run onto it."""
        data, ws = self.data, self.ws
        tip = self.repo.rev(data["target"])
        if tip == data["base"] or not self.repo.is_ancestor(data["base"],
                                                            tip):
            return
        kept = [i for i in data["items"] if i["state"] in ("picked", "done")]
        old = ws.head()
        ws.git("reset", "--quiet", "--hard")      # drop what a build changed
        proc = ws.proc("rebase", "--quiet", tip, check=False)
        new = [] if proc.returncode else ws.git(
            "rev-list", "--reverse", "%s..HEAD" % tip).split()
        if proc.returncode or len(new) != len(kept):
            ws.proc("rebase", "--abort", check=False)
            ws.git("reset", "--quiet", "--hard", old)
            raise DiasporaError(
                "%s moved and the imports made so far do not replay on it; "
                "`diaspora abort` and start again" % data["target"])
        before = tip
        for item, sha in zip(kept, new):
            item["head0"], item["our"], item["state"] = before, sha, "picked"
            before = sha
        data["base"], data["retest"] = tip, bool(kept)
        self.save()
        self.ui.say("%s moved to %s; the run now sits on it." % (
            data["target"], tip[:10]))

    def resume(self):
        data = self.data
        if data["stage"] == "ready":
            return self.finish()
        paused = next((i for i in data["items"] if i["state"] == "paused"),
                      None)
        if not paused and not self.export:
            self._follow_target()
        if paused:
            commit = scan.one(self.repo, paused["sha"])
            if not self._commit(paused, commit, adapted=True):
                self.save()
                raise DiasporaError(
                    "%s; resolve and `git add` them in %s, then `diaspora "
                    "continue`" % (paused["note"], self.ws.path))
            if paused["state"] == "picked":
                paused["note"] = "conflicts resolved by hand"
            self.save()
        return self.run()

    def skip(self):
        paused = next((i for i in self.data["items"]
                       if i["state"] == "paused"), None)
        if not paused:
            raise DiasporaError("no commit is waiting for you; nothing to "
                                "skip")
        self._fail(paused, "conflicts, not resolved")
        self.save()
        return self.run()

    def abort(self):
        data = self.data
        if os.path.isdir(self.ws.path) and self.repo.current_branch(
                cwd=self.ws.path) == data["branch"]:
            self.ws.proc("cherry-pick", "--quit", check=False)
            self.ws.proc("reset", "--quiet", "--hard", check=False)
            self.ws.end(data.get("restore"))
        self.repo.proc("branch", "-D", data["branch"], check=False)
        self._archive()
        self.ui.say("Run %s dropped; nothing was recorded." % data["id"])


def record(repo, conf, ui, agent, target, entries):
    """Commit ledger entries on `target` without importing anything."""
    session = Session(repo, conf, ui, agent, interactive=False,
                      assume_yes=True)
    if session.exists():
        raise DiasporaError(
            "a diaspora run is in progress; finish it (`diaspora continue` "
            "or `diaspora abort`) before recording verdicts")
    if not repo.identity_ok():
        raise DiasporaError("git does not know who you are; set user.name "
                            "and user.email (git config)")
    sid = time.strftime("%Y%m%d-%H%M%S")
    session.data = {
        "id": sid, "dir": "import", "target": target,
        "base": repo.rev(target), "branch": "diaspora/ledger/%s" % sid,
        "stage": "ready", "restore": None, "peer": None, "verdicts": [],
        "entries": entries, "ledger_commit": True, "items": []}
    session._tester()
    session.data["restore"] = session.ws.begin(session.data["branch"],
                                               session.data["base"])
    session.save()
    session._commit_ledger(entries)
    session.ws.end(session.data["restore"])
    session.data["restore"] = None
    where = repo.worktrees().get(target)
    if where:
        proc = repo.proc("merge", "--ff-only", "--quiet",
                         session.data["branch"], cwd=where, check=False)
    else:
        proc = repo.proc("fetch", "--quiet", ".", "refs/heads/%s:refs/heads/%s"
                         % (session.data["branch"], target), check=False)
    if proc.returncode:
        raise DiasporaError(
            "cannot record the verdicts on %s: %s\nThey are on branch %s; "
            "`diaspora promote` tries again." % (
                target, proc.stderr.strip(), session.data["branch"]))
    repo.proc("branch", "-D", session.data["branch"], check=False)
    session._archive()
    ui.say("Recorded %d verdict%s in %s on %s." % (
        len(entries), "" if len(entries) == 1 else "s", conf.ledger, target))
