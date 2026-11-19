# SPDX-License-Identifier: GPL-3.0-or-later
"""End-to-end tests: two scratch repositories, a peer and ours.

Run with `make check` or `python3 -m unittest discover -s tests`.
"""

import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from diaspora import cli, gitutil, scan, triage             # noqa: E402
from diaspora import ledger as L                            # noqa: E402
from diaspora import tried as T                             # noqa: E402
from diaspora.agent import Agent, _json_object              # noqa: E402
from diaspora.config import Config, parse_duration          # noqa: E402
from diaspora.gitutil import DiasporaError, PathSet, Repo   # noqa: E402
from diaspora.session import Session                        # noqa: E402
from diaspora.ui import UI                                  # noqa: E402

FAKE = os.path.join(HERE, "fake_agent.py")

CONF = """\
[diaspora]
	target = main
[peer "up"]
	url = {peer}
	branch = main
	exclude = www/**
[test "compile"]
	run = test -f src/a.c
[test "regress"]
	needs = compile
	run = sh check.sh
[require]
	default = regress
	docs = none
[export]
	exclude = private/**
[agent]
	command = none
"""


def git(cwd, *args):
    return subprocess.run(["git"] + list(args), cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


class World(unittest.TestCase):
    """A peer repository and ours, forked from it and diverged."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="diaspora-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = {
            "GIT_AUTHOR_NAME": "Tester", "GIT_AUTHOR_EMAIL": "t@example.org",
            "GIT_COMMITTER_NAME": "Tester",
            "GIT_COMMITTER_EMAIL": "t@example.org",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "XDG_CACHE_HOME": os.path.join(self.tmp, "cache"),
            # never the real ~/.config/diaspora
            "DIASPORA_CONFIG": os.path.join(self.tmp, "config",
                                            "diaspora"),
            "FAKE_AGENT_LOG": os.path.join(self.tmp, "agent.log"),
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("FAKE_TRIAGE", "FAKE_PORT", "FAKE_FIX", "NO_COLOR",
                     "FAKE_PROMPT_LOG"):
            os.environ.pop(name, None)

        self.peer = os.path.join(self.tmp, "peer")
        self.ours = os.path.join(self.tmp, "ours")
        os.mkdir(self.peer)
        git(self.peer, "init", "-q", "-b", "main")
        self.commit(self.peer, "Initial import", {
            "src/a.c": "int a(void)\n{\n    return 1;\n}\n",
            "src/b.c": "line1\nline2\nline3\nline4\nline5\n",
            "src/d.c": "d1\nd2\nd3\n",
            "doc/guide.md": "guide\n",
            "check.sh": "# passes unless something says BREAK\n"
                        "! grep -rq BREAK src\n"})
        git(self.tmp, "clone", "-q", self.peer, self.ours)

        # ours diverges
        self.o1 = self.commit(self.ours, "Rework b for our needs", {
            "src/b.c": "line1\nline2 ours\nline3\nline4\nline5\n"})
        self.o2 = self.commit(self.ours, "Plug leak in d", {
            "src/d.c": "d1\nd2 fixed\nd3\n"})
        self.o3 = self.commit(self.ours, "Fix rounding in our scaler", {
            "src/ours.c": "scaler\n"})
        self.o4 = self.commit(self.ours, "Fix private notes", {
            "private/notes.txt": "secret\n"})
        self.commit(self.ours, "Add diaspora.conf", {
            "diaspora.conf": CONF.format(peer=self.peer)})

        # the peer moves on
        self.p1 = self.commit(self.peer, "Fix crash in a when called twice", {
            "src/a.c": "int a(void)\n{\n    return 2;\n}\n"})
        self.p2 = self.commit(self.peer, "docs: describe a", {
            "doc/guide.md": "guide\nmore\n"})
        self.p3 = self.commit(self.peer, "Add x support", {
            "src/x.c": "x\n"})
        self.p4 = self.commit(self.peer, "Fix overflow in b", {
            "src/b.c": "line1\nline2 peer fix\nline3\nline4\nline5\n"})
        self.p5 = self.commit(self.peer, "Fix typo handling in c", {
            "src/c.c": "BREAK\n"})
        self.p6 = self.commit(self.peer, "Plug leak in d", {
            "src/d.c": "d1\nd2 fixed\nd3\n"})
        self.p7 = self.commit(self.peer, "Fix the web page", {
            "www/index.html": "<p>hi</p>\n"})

    # -- helpers ------------------------------------------------------------------
    def commit(self, repo, message, files):
        for path, text in files.items():
            full = os.path.join(repo, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as handle:
                handle.write(text)
            git(repo, "add", "--", path)
        git(repo, "commit", "-q", "-m", message)
        return git(repo, "rev-parse", "HEAD")

    def use_fake_agent(self, extra=""):
        with open(os.path.join(self.ours, "diaspora.local.conf"), "w") as f:
            f.write("[agent]\n\tcommand = %s %s\n%s" % (sys.executable, FAKE,
                                                       extra))

    def local_conf(self, text):
        with open(os.path.join(self.ours, "diaspora.local.conf"), "w") as f:
            f.write(text)

    def run_cli(self, *args, code=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            got = cli.main(["-C", self.ours] + list(args))
        self.last_out, self.last_err = out.getvalue(), err.getvalue()
        if code is not None:
            self.assertEqual(got, code, self.last_out + self.last_err)
        return self.last_out

    def read(self, path, ref="main"):
        return git(self.ours, "show", "%s:%s" % (ref, path))

    def ledger(self, direction="import"):
        repo = Repo(self.ours)
        return {e["commit"]: e for e in L.Ledger.at(
            repo, "main", "diaspora.ledger").latest(direction).values()}

    def tried(self, ref="main"):
        """{commit: [diaspora.log blocks]} on `ref`."""
        out = {}
        for sha, text in T.Log.at(Repo(self.ours), ref,
                                  "diaspora.log").blocks:
            out.setdefault(sha, []).append(text)
        return out

    def subjects(self, rng="main"):
        return git(self.ours, "log", "--format=%s", rng).splitlines()

    def session(self, interactive=False, assume_yes=True):
        repo = Repo(self.ours)
        conf = Config.load(repo)
        agent = Agent(conf.agent, os.path.join(self.tmp, "agentlogs"))
        return Session(repo, conf, UI(color=False), agent,
                       interactive=interactive, assume_yes=assume_yes)

    def candidates(self):
        repo = Repo(self.ours)
        conf = Config.load(repo)
        peer = conf.peer("up")
        scan.fetch(repo, peer)
        ledger = L.Ledger.at(repo, "main", conf.ledger)
        commits, dropped = scan.import_candidates(repo, conf, peer, "main",
                                                  ledger)
        triage.triage(repo, conf, peer, "main", commits)
        return {c.sha: c for c in commits}, dropped


class Units(unittest.TestCase):
    def test_globs(self):
        self.assertTrue(PathSet(["src/**"]).match("src/a/b.c"))
        self.assertTrue(PathSet(["src/"]).match("src/a/b.c"))
        self.assertFalse(PathSet(["src/*"]).match("src/a/b.c"))
        self.assertTrue(PathSet(["src/*.c"]).match("src/a.c"))
        self.assertTrue(PathSet(["*.md"]).match("doc/deep/x.md"))
        self.assertTrue(PathSet(["**/test/**"]).match("lib/test/a.vhd"))
        self.assertFalse(PathSet(["src/**"]).match("lib/src/a.c"))
        self.assertTrue(PathSet(["NEWS"]).match("NEWS"))
        self.assertFalse(PathSet([]).match("anything"))
        self.assertTrue(PathSet(["www/**"]).all(["www/a", "www/b/c"]))
        self.assertFalse(PathSet(["www/**"]).all([]))

    def test_classify(self):
        def kind(subject, paths=("src/a.c",), body=""):
            commit = scan.Commit("0" * 40, "a", "e", "2026-01-01", subject,
                                 body, [(p, 1, 1) for p in paths])
            return triage.classify(commit)[0]

        self.assertEqual(kind("Fix crash on empty file"), "bugfix")
        self.assertEqual(kind("fix(parser): handle EOF"), "bugfix")
        self.assertEqual(kind("Whatever", ("doc/x.md", "NEWS.md")), "docs")
        self.assertEqual(kind("Whatever", (".github/workflows/x.yml",)), "ci")
        self.assertEqual(kind("Add VHDL-2019 interfaces"), "feature")
        self.assertEqual(kind("Speed up elaboration"), "perf")
        self.assertEqual(kind("Use after free in the heap walker"),
                         "security")
        self.assertEqual(kind("Tidy the lexer"), "refactor")
        self.assertEqual(kind("Rename things", (), ""), "refactor")
        self.assertEqual(kind("Thing", body="This fixes issue #12"),
                         "bugfix")
        self.assertEqual(kind("Zzz"), "other")

    def test_json_object(self):
        self.assertEqual(_json_object('{"a": 1}'), {"a": 1})
        self.assertEqual(_json_object('noise\n{"a": {"b": 2}}\n'),
                         {"a": {"b": 2}})
        self.assertIsNone(_json_object("no json here"))

    def test_duration(self):
        self.assertEqual(parse_duration("90s", "x"), 90)
        self.assertEqual(parse_duration("2h", "x"), 7200)
        self.assertEqual(parse_duration("45m", "x"), 2700)
        with self.assertRaises(DiasporaError):
            parse_duration("soon", "x")

    def test_conf_syntax(self):
        text = (
            "# a comment\n; another\n\n"
            "[diaspora]\n\ttarget = main\n"
            '[test "compile"]\n'
            '  run = mkdir -p "$B" && cd "$B"\n'
            "  run = { [ -f Makefile ] || \"$S/configure\" 'CFLAGS=-O2 -g'; }"
            "  # kept\n"
            "  run = make \\\n        -j4\n"
            '[ Peer   "x" ]\n  Since=2026-01-01\n  bare\n')
        self.assertEqual(Config.parse(text, "t"), [
            ("diaspora", "", "target", "main"),
            ("test", "compile", "run", 'mkdir -p "$B" && cd "$B"'),
            ("test", "compile", "run",
             "{ [ -f Makefile ] || \"$S/configure\" 'CFLAGS=-O2 -g'; }"
             "  # kept"),
            ("test", "compile", "run", "make -j4"),
            ("peer", "x", "since", "2026-01-01"),
            ("peer", "x", "bare", "true")])
        for bad in ("key = 1\n", "[peer x]\nurl = u\n",
                    "[peer]\nbad key = 1\n"):
            with self.assertRaises(DiasporaError):
                Config.parse(bad, "t")

    def test_numbers(self):
        self.assertEqual(UI._numbers("1 3-5 because", 9),
                         ([1, 3, 4, 5], "because"))
        self.assertEqual(UI._numbers("12", 9), ([], ""))


class ConfigTests(World):
    def test_levels(self):
        self.local_conf('[require "up"]\n\tfeature = compile\n'
                        '[peer "up"]\n\ttest = none\n')
        conf = Config.load(Repo(self.ours))
        self.assertEqual(conf.levels_for("up", "docs"), [])
        self.assertEqual(conf.levels_for("up", "feature"), ["compile"])
        self.assertEqual(conf.levels_for("up", "bugfix"), [])
        self.assertEqual(conf.levels_for("other", "bugfix"), ["regress"])
        self.assertEqual(conf.chain(["regress"]), ["compile", "regress"])
        peer = conf.peer("up")
        self.assertEqual(peer.ref, "refs/diaspora/up/main")
        self.assertIn("bugfix", peer.imports)
        self.assertIn("docs", peer.ignore)

    def test_errors(self):
        for text, needle in (
                ('[peer "up"]\n\timport = bogus\n', "unknown kind"),
                ('[require]\n\tdefault = nope\n', "unknown test level"),
                ('[test "t"]\n\tneeds = compile\n', "no run command"),
                ('[peer "x y"]\n\turl = /tmp\n', "peer name"),
                ('[diaspora]\n\tstrategy = fast\n', "strategy")):
            self.local_conf(text)
            with self.assertRaises(DiasporaError) as caught:
                Config.load(Repo(self.ours))
            self.assertIn(needle, str(caught.exception))

    def test_review_moves_a_kind(self):
        self.local_conf('[peer "up"]\n\treview = perf docs\n')
        peer = Config.load(Repo(self.ours)).peer("up")
        self.assertNotIn("perf", peer.imports)
        self.assertNotIn("docs", peer.ignore)

    def test_init(self):
        os.remove(os.path.join(self.ours, "diaspora.conf"))
        git(self.ours, "remote", "add", "upstream", self.peer)
        self.run_cli("init", code=0)
        conf = Config.load(Repo(self.ours))
        self.assertEqual(conf.peer("upstream").url, self.peer)
        self.assertEqual(conf.target, "main")
        self.assertEqual(conf.levels_for("upstream", "bugfix"),
                         ["full-regression"])
        self.run_cli("init", code=1)


class ScanTests(World):
    def test_candidates(self):
        found, dropped = self.candidates()
        self.assertEqual(set(found), {self.p1, self.p2, self.p3, self.p4,
                                      self.p5})
        self.assertEqual(dropped["same patch already here"], 1)
        self.assertEqual(dropped["excluded paths"], 1)
        self.assertEqual(found[self.p1].disposition, "import")
        self.assertEqual(found[self.p2].disposition, "ignore")
        self.assertEqual(found[self.p3].disposition, "review")
        self.assertEqual(found[self.p1].kind, "bugfix")
        self.assertEqual(found[self.p3].kind, "feature")

    def test_since(self):
        self.local_conf('[peer "up"]\n\tsince = %s\n' % self.p3)
        found, _ = self.candidates()
        self.assertEqual(set(found), {self.p4, self.p5})

    def test_paths(self):
        self.local_conf('[peer "up"]\n\tpaths = src/a.c doc/**\n')
        found, dropped = self.candidates()
        self.assertEqual(set(found), {self.p1, self.p2})
        self.assertEqual(dropped["outside the peer's paths"], 4)

    def test_removed_and_rewritten(self):
        self.local_conf('[divergence]\n\tremoved = src/x.c\n'
                        '\trewritten = src/b.c\n')
        found, dropped = self.candidates()
        self.assertNotIn(self.p3, found)
        self.assertEqual(dropped["touches only code removed here"], 1)
        self.assertEqual(found[self.p4].disposition, "review")
        self.assertIn("rewritten", found[self.p4].why)

    def test_peer_export_rules(self):
        self.commit(self.peer, "Fix the secret sauce", {
            "secret/sauce.c": "s\n"})
        self.commit(self.peer, "Publish export rules", {
            "diaspora.conf": "[export]\n\texclude = secret/**\n"})
        found, dropped = self.candidates()
        self.assertEqual(dropped["not exported by the peer"], 1)
        self.assertEqual(dropped["no files / diaspora metadata"], 1)
        self.assertEqual(len(found), 5)

    def test_changes_to_files_we_lack(self):
        add = self.commit(self.peer, "Add e support", {"src/e.c": "e\n"})
        fix = self.commit(self.peer, "Fix crash in e", {"src/e.c": "e2\n"})
        found, _ = self.candidates()
        self.assertEqual(found[add].disposition, "review")
        self.assertFalse(found[add].absent)
        self.assertEqual(found[fix].disposition, "ignore")
        self.assertIn("files we do not have", found[fix].why)

    def test_list_json(self):
        data = json.loads(self.run_cli("list", "--json", "--all", code=0))
        commits = data["peers"][0]["commits"]
        self.assertEqual([c["commit"] for c in commits],
                         [self.p1, self.p2, self.p3, self.p4, self.p5])
        self.assertEqual(data["target"], "main")
        text = self.run_cli("list", "--no-fetch", code=0)
        self.assertIn("Fix crash in a", text)
        self.assertNotIn("docs: describe a", text)
        self.assertIn("5 commits to look at: 3 suggested, 1 to review, "
                      "1 ignored", text)


class ImportTests(World):
    def test_yes_without_agent(self):
        before = git(self.ours, "rev-parse", "main")
        out = self.run_cli("import", "--yes", code=4)
        # p1 lands, p4 conflicts, p5 breaks the regression
        self.assertEqual(self.subjects(before + "..main"), [
            "diaspora: 1 applied, 2 failed",
            "Fix crash in a when called twice"])
        self.assertIn("return 2", self.read("src/a.c"))
        self.assertIn("line2 ours", self.read("src/b.c"))
        with open(os.path.join(self.ours, "src/a.c")) as handle:
            self.assertIn("return 2", handle.read())   # checkout followed
        body = git(self.ours, "log", "-1", "--format=%B", "main~1")
        self.assertIn("Diaspora-Origin: up@%s (applied)" % self.p1, body)
        self.assertEqual(git(self.ours, "log", "-1", "--format=%an",
                             "main~1"), "Tester")
        ledger = self.ledger()
        self.assertEqual(ledger[self.p1]["verdict"], "applied")
        self.assertEqual(ledger[self.p1]["tests"], ["compile", "regress"])
        self.assertEqual(ledger[self.p4]["verdict"], "failed")
        self.assertIn("conflicts in src/b.c", ledger[self.p4]["note"])
        self.assertEqual(ledger[self.p5]["verdict"], "failed")
        self.assertIn("test regress failed", ledger[self.p5]["note"])
        self.assertIn("1 imported, 2 failed", out)
        # how they failed is told in diaspora.log, in the same commit
        tried = self.tried()
        self.assertEqual(set(tried), {self.p4, self.p5})
        self.assertIn("conflicts in src/b.c", tried[self.p4][0])
        self.assertIn("test regress failed", tried[self.p5][0])
        self.assertTrue(tried[self.p5][0].rstrip().endswith("=> failed"))
        self.assertNotIn("(log:", tried[self.p5][0])   # a local path
        self.assertTrue(self.read("diaspora.log").startswith(
            "# diaspora.log - "))
        self.assertIn("diaspora.log", git(self.ours, "show", "--stat",
                                          "--format=", "main"))
        self.assertEqual(git(self.ours, "branch", "--list", "diaspora/*"),
                         "")
        self.assertEqual(git(self.ours, "status", "--porcelain",
                             "--untracked-files=no"), "")

        # a second look: nothing new is suggested, the failures are flagged
        data = json.loads(self.run_cli("list", "--json", code=0))
        commits = {c["commit"]: c for c in data["peers"][0]["commits"]}
        self.assertEqual(set(commits), {self.p3, self.p4, self.p5})
        self.assertEqual(commits[self.p4]["status"], "failed")
        out = self.run_cli("import", "--yes", code=0)
        self.assertIn("[!]", out)
        self.assertEqual(self.subjects(before + "..main")[0],
                         "diaspora: 1 applied, 2 failed")
        out = self.run_cli("show", self.p5, code=0)
        self.assertIn("tried before (diaspora.log)", out)
        self.assertIn("test regress failed", out)

    def test_agent_ports_and_fixes(self):
        self.use_fake_agent()
        os.environ["FAKE_TRIAGE"] = json.dumps({
            "Add x": {"kind": "feature", "recommend": "review"}})
        os.environ["FAKE_FIX"] = "sed -i s/BREAK/fine/ src/c.c"
        out = self.run_cli("import", "--yes", code=0)
        ledger = self.ledger()
        self.assertEqual(ledger[self.p1]["verdict"], "applied")
        self.assertEqual(ledger[self.p4]["verdict"], "adapted")
        self.assertEqual(ledger[self.p5]["verdict"], "adapted")
        self.assertNotIn(self.p3, ledger)
        self.assertIn("line2 peer fix", self.read("src/b.c"))
        self.assertEqual(self.read("src/c.c"), "fine")
        bodies = git(self.ours, "log", "--format=%B", "-4", "main")
        self.assertIn("Diaspora-Origin: up@%s (adapted)" % self.p4, bodies)
        self.assertIn("Diaspora-Origin: up@%s (adapted)" % self.p5, bodies)
        self.assertNotIn("(applied)\n\nDiaspora", bodies)
        self.assertIn("agent: ported - took the peer's side", out)
        self.assertIn("repaired after regress failed",
                      ledger[self.p5]["note"])
        # p4 was ported and passed: nothing went wrong; p5 needed a repair
        tried = self.tried()
        self.assertEqual(set(tried), {self.p5})
        self.assertIn("test regress failed", tried[self.p5][0])
        self.assertIn("agent repair: fixed - ran the fix", tried[self.p5][0])
        self.assertIn("=> passed after the repair (adapted)",
                      tried[self.p5][0])
        self.assertNotIn("also changed", out)
        with open(os.environ["FAKE_AGENT_LOG"]) as handle:
            self.assertEqual(handle.read().split(),
                             ["triage", "port", "fix"])
        # the agent's triage is cached: a second scan does not ask again
        self.run_cli("list", code=0)
        with open(os.environ["FAKE_AGENT_LOG"]) as handle:
            self.assertEqual(handle.read().split().count("triage"), 1)

    def test_repair_outside_the_import_is_reported(self):
        self.use_fake_agent()
        os.environ["FAKE_FIX"] = ("sed -i s/BREAK/fine/ src/c.c; "
                                  "echo tweak >> src/d.c")
        out = self.run_cli("import", "--pick", self.p5, "--yes", code=0)
        self.assertIn("also changed: src/d.c", out)
        self.assertIn("also changed: src/d.c", self.ledger()[self.p5]["note"])

    def test_what_the_build_changes_is_not_part_of_a_repair(self):
        # an in-tree build that rewrites a tracked file (a regenerated
        # configure, say) must not leak into the import
        self.use_fake_agent('[test "compile"]\n'
                            '\trun = echo built >> src/d.c\n')
        os.environ["FAKE_FIX"] = "sed -i s/BREAK/fine/ src/c.c"
        out = self.run_cli("import", "--pick", self.p5, "--pick", self.p3,
                           "--yes", code=0)
        self.assertEqual(self.read("src/c.c"), "fine")
        self.assertNotIn("built", self.read("src/d.c"))
        self.assertNotIn("also changed", out)
        self.assertEqual(self.ledger()[self.p3]["verdict"], "applied")
        self.assertEqual(git(self.ours, "status", "--porcelain",
                             "--untracked-files=no"), "")

    def test_agent_says_superseded(self):
        self.use_fake_agent()
        os.environ["FAKE_PORT"] = "superseded"
        self.run_cli("import", "--pick", self.p4[:12], code=0)
        # not a terminal and no --yes: tested, but left for `promote`
        self.assertEqual(self.ledger(), {})
        self.run_cli("promote", code=0)
        self.assertEqual(self.ledger()[self.p4]["verdict"], "superseded")
        self.assertIn("line2 ours", self.read("src/b.c"))

    def test_pick_a_reviewed_commit(self):
        self.run_cli("import", "--pick", self.p3, "--yes", code=0)
        self.assertEqual(self.read("src/x.c"), "x")
        self.assertEqual(self.ledger()[self.p3]["verdict"], "applied")
        self.run_cli("import", "--pick", self.p3, code=1)
        self.assertIn("not a candidate", self.last_err)

    def test_docs_need_no_test(self):
        self.commit(self.ours, "Break the regression on purpose", {
            "src/broken.c": "BREAK\n"})
        self.run_cli("import", "--pick", self.p2, "--yes", code=0)
        entry = self.ledger()[self.p2]
        self.assertEqual(entry["verdict"], "applied")
        self.assertNotIn("tests", entry)

    def test_blocked_when_the_target_fails(self):
        self.commit(self.ours, "Break the regression on purpose", {
            "src/broken.c": "BREAK\n"})
        tip = git(self.ours, "rev-parse", "main")
        self.run_cli("import", "--pick", self.p1, "--yes", code=3)
        self.assertIn("fails without the import", self.last_err)
        self.assertEqual(git(self.ours, "rev-parse", "main"), tip)
        self.assertIn("in progress", self.run_cli("status", code=0))
        self.run_cli("continue", "--yes", code=3)       # still broken
        self.run_cli("abort", code=0)
        self.assertEqual(git(self.ours, "branch", "--list", "diaspora/*"),
                         "")
        self.assertEqual(self.ledger(), {})

    def test_blocked_then_the_target_is_fixed(self):
        self.commit(self.ours, "Break the regression on purpose", {
            "src/broken.c": "BREAK\n"})
        self.run_cli("import", "--pick", self.p1, "--pick", self.p3, "--yes",
                     code=3)
        self.commit(self.ours, "Mend the regression", {
            "src/broken.c": "mended\n"})
        out = self.run_cli("continue", "--yes", code=0)
        self.assertIn("the run now sits on it", out)
        self.assertEqual(self.subjects("main")[:4], [
            "diaspora: 2 applied", "Add x support",
            "Fix crash in a when called twice", "Mend the regression"])
        ledger = self.ledger()
        self.assertEqual(ledger[self.p1]["tests"], ["compile", "regress"])
        self.assertEqual(ledger[self.p3]["verdict"], "applied")

    def test_pause_continue(self):
        found, _ = self.candidates()
        session = self.session(interactive=True)
        self.assertEqual(session.start("import", "main", [found[self.p4]]),
                         "paused")
        ws = session.ws.path
        self.assertNotEqual(os.path.realpath(ws),
                            os.path.realpath(self.ours))
        again = self.session(interactive=True).load()
        with self.assertRaises(DiasporaError) as caught:
            again.resume()                    # nothing resolved yet
        self.assertIn("unresolved conflicts in src/b.c",
                      str(caught.exception))
        with open(os.path.join(ws, "src/b.c"), "w") as handle:
            handle.write("line1\nline2 both\nline3\nline4\nline5\n")
        git(ws, "add", "src/b.c")
        self.assertEqual(self.session(interactive=True).load().resume(),
                         "promoted")
        self.assertIn("line2 both", self.read("src/b.c"))
        entry = self.ledger()[self.p4]
        self.assertEqual(entry["verdict"], "adapted")
        self.assertEqual(entry["note"], "conflicts resolved by hand")

    def test_leftover_markers_are_refused(self):
        found, _ = self.candidates()
        session = self.session(interactive=True)
        session.start("import", "main", [found[self.p4]])
        git(session.ws.path, "add", "src/b.c")     # markers still inside
        with self.assertRaises(DiasporaError):
            self.session(interactive=True).load().resume()

    def test_skip(self):
        found, _ = self.candidates()
        session = self.session(interactive=True)
        session.start("import", "main", [found[self.p4], found[self.p1]])
        self.assertEqual(self.session().load().skip(), "promoted")
        ledger = self.ledger()
        self.assertEqual(ledger[self.p4]["verdict"], "failed")
        self.assertEqual(ledger[self.p1]["verdict"], "applied")

    def test_abort_keeps_what_did_not_work(self):
        found, _ = self.candidates()
        before = git(self.ours, "rev-parse", "main")
        session = self.session(interactive=True)
        self.assertEqual(session.start("import", "main",
                                       [found[self.p5], found[self.p4]]),
                         "paused")
        self.run_cli("abort", code=0)
        self.assertIn("ledger is unchanged", self.last_out)
        self.assertEqual(self.subjects(before + "..main"),
                         ["diaspora: 1 attempt that did not work"])
        self.assertEqual(self.ledger(), {})
        self.assertEqual(set(self.tried()), {self.p5})
        self.assertEqual(git(self.ours, "branch", "--list", "diaspora/*"),
                         "")

    def test_abort_without_the_log(self):
        found, _ = self.candidates()
        before = git(self.ours, "rev-parse", "main")
        session = self.session(interactive=True)
        session.start("import", "main", [found[self.p5], found[self.p4]])
        self.run_cli("abort", "--no-log", code=0)
        self.assertIn("nothing was recorded", self.last_out)
        self.assertEqual(git(self.ours, "rev-parse", "main"), before)

    def test_the_agent_hears_of_earlier_attempts(self):
        self.run_cli("import", "--yes", "--pick", self.p5, code=4)
        self.run_cli("reopen", self.p5, code=0)
        self.use_fake_agent()
        os.environ["FAKE_FIX"] = "sed -i s/BREAK/fine/ src/c.c"
        os.environ["FAKE_PROMPT_LOG"] = os.path.join(self.tmp, "prompts")
        self.run_cli("import", "--yes", "--pick", self.p5, code=0)
        with open(os.environ["FAKE_PROMPT_LOG"]) as handle:
            prompts = handle.read()
        self.assertIn("This commit was tried before and did not work",
                      prompts)
        self.assertIn("up@%s" % self.p5, prompts)
        self.assertEqual(len(self.tried()[self.p5]), 2)
        self.assertIn("\n\n", self.read("diaspora.log"))   # blocks apart

    def test_a_compile_failure_is_told_by_its_error(self):
        self.local_conf('[test "compile"]\n\trun = ! grep -q BREAK src/*.c '
                        '|| { echo "$DIASPORA_SRC/src/c.c:1: error: BREAK"; '
                        'false; }\n')
        self.run_cli("import", "--yes", "--pick", self.p5, code=4)
        block = self.tried()[self.p5][0]
        self.assertIn("test compile failed: src/c.c:1: error: BREAK", block)
        self.assertNotIn(self.tmp, block)

    def test_batch_falls_back_to_each(self):
        out = self.run_cli("import", "--yes", "--strategy", "batch",
                           "--pick", self.p1, "--pick", self.p5, code=4)
        self.assertIn("testing the imports one by one", out)
        ledger = self.ledger()
        self.assertEqual(ledger[self.p1]["verdict"], "applied")
        self.assertEqual(ledger[self.p5]["verdict"], "failed")
        self.assertIn("return 2", self.read("src/a.c"))
        self.assertFalse(git(self.ours, "ls-tree", "main", "src/c.c"))

    def test_batch_passes(self):
        self.run_cli("import", "--yes", "--strategy", "batch",
                     "--pick", self.p1, "--pick", self.p3, code=0)
        ledger = self.ledger()
        self.assertEqual(ledger[self.p1]["tests"], ["compile", "regress"])
        self.assertEqual(ledger[self.p3]["verdict"], "applied")

    def test_target_moves_during_the_run(self):
        self.run_cli("import", "--pick", self.p1, "--no-promote", code=0)
        self.assertIn("diaspora promote", self.last_out)
        self.assertNotIn("return 2", self.read("src/a.c"))
        self.commit(self.ours, "Our own work meanwhile", {"src/new.c": "n\n"})
        self.run_cli("promote", code=0)
        self.assertIn("moved during the run", self.last_out)
        self.assertEqual(self.subjects("main")[:3], [
            "diaspora: 1 applied", "Fix crash in a when called twice",
            "Our own work meanwhile"])
        self.assertEqual(self.ledger()[self.p1]["verdict"], "applied")

    def test_inplace(self):
        self.local_conf("[diaspora]\n\tworkspace = inplace\n")
        self.run_cli("import", "--pick", self.p1, "--yes", code=0)
        self.assertEqual(git(self.ours, "symbolic-ref", "--short", "HEAD"),
                         "main")
        with open(os.path.join(self.ours, "src/a.c")) as handle:
            self.assertIn("return 2", handle.read())
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "cache")))
        # a dirty checkout is refused
        with open(os.path.join(self.ours, "src/a.c"), "a") as handle:
            handle.write("// local edit\n")
        self.run_cli("import", "--pick", self.p3, "--yes", code=1)
        self.assertIn("clean checkout", self.last_err)

    def test_here_imports_into_the_working_branch(self):
        git(self.ours, "checkout", "-q", "-b", "topic")
        tip = git(self.ours, "rev-parse", "main")
        self.run_cli("import", "--here", "--pick", self.p1, "--yes", code=0)
        self.assertEqual(git(self.ours, "rev-parse", "main"), tip)
        self.assertIn("return 2", self.read("src/a.c", "topic"))
        self.assertIn(self.p1, git(self.ours, "show",
                                   "topic:diaspora.ledger"))

    def test_excluded_files_stay_behind(self):
        sha = self.commit(self.peer, "Fix d and announce it", {
            "src/d.c": "d1\nd2 fixed\nd3 fixed\n",
            "www/news.html": "<p>fixed</p>\n"})
        self.run_cli("import", "--pick", sha, "--yes", code=0)
        self.assertIn("d3 fixed", self.read("src/d.c"))
        self.assertFalse(git(self.ours, "ls-tree", "main", "www/"))

    def test_already_present_is_superseded(self):
        self.commit(self.ours, "Fix a and more besides", {
            "src/a.c": "int a(void)\n{\n    return 2;\n}\n",
            "src/more.c": "more\n"})
        self.run_cli("import", "--pick", self.p1, "--yes", code=0)
        self.assertEqual(self.ledger()[self.p1]["verdict"], "superseded")

    def test_verdicts(self):
        self.run_cli("fetch", code=0)
        self.run_cli("reject", self.p3, "-m", "not for us", code=0)
        self.run_cli("defer", self.p5, code=0)
        ledger = self.ledger()
        self.assertEqual(ledger[self.p3]["verdict"], "rejected")
        self.assertEqual(ledger[self.p3]["note"], "not for us")
        found, dropped = self.candidates()
        self.assertNotIn(self.p3, found)
        self.assertEqual(found[self.p5].status, "deferred")
        self.assertNotIn("Fix typo handling", self.run_cli("list", code=0))
        self.assertIn("Fix typo handling",
                      self.run_cli("list", "--all", code=0))
        self.run_cli("reopen", self.p3, code=0)
        found, _ = self.candidates()
        self.assertIn(self.p3, found)
        self.assertIn("rejected", self.run_cli("log", code=0) + "rejected")
        self.assertIn("deferred", self.run_cli("log", code=0))

    def test_timeout_and_retry(self):
        flag = os.path.join(self.tmp, "second-time")
        self.local_conf(
            '[test "slow"]\n\trun = sleep 30\n\ttimeout = 1s\n'
            '[test "flaky"]\n\trun = test -f %s || { touch %s; false; }\n'
            '\tretry = 1\n[diaspora]\n\tledger = meta/ledger.jsonl\n'
            % (flag, flag))
        out = self.run_cli("test", "slow", code=1)
        self.assertIn("timed out after 1s", out)
        # a helper process left behind with the output open does not hold
        # the level up
        self.local_conf('[test "leaky"]\n'
                        '\trun = (setsid sleep 60 &) ; echo done\n')
        import time
        start = time.time()
        self.run_cli("test", "leaky", code=0)
        self.assertLess(time.time() - start, 30)
        self.local_conf(
            '[test "flaky"]\n\trun = test -f %s || { touch %s; false; }\n'
            '\tretry = 1\n[diaspora]\n\tledger = meta/ledger.jsonl\n'
            % (flag, flag))
        out = self.run_cli("test", "flaky", code=0)
        self.assertIn("retrying", out)
        self.run_cli("fetch", code=0)
        self.run_cli("reject", self.p3, code=0)      # ledger in a new dir
        self.assertIn(self.p3, self.read("meta/ledger.jsonl"))

    def test_interactive_list(self):
        found, _ = self.candidates()
        shown = [found[s] for s in (self.p1, self.p3, self.p4, self.p5)]
        answers = iter(["2", "r 3 conflicts with our rework", "l 3", ""])
        ui = UI(color=False)
        with mock.patch("builtins.input", lambda prompt="": next(answers)), \
                contextlib.redirect_stdout(io.StringIO()):
            selected, entries = ui.choose(Repo(self.ours), shown)
        self.assertEqual([c.sha for c in selected], [self.p1, self.p3])
        self.assertEqual([(e["commit"], e["verdict"]) for e in entries],
                         [(self.p4, "rejected"), (self.p5, "deferred")])
        self.assertEqual(entries[0]["note"], "conflicts with our rework")

    def test_status_and_show(self):
        self.run_cli("fetch", code=0)
        out = self.run_cli("status", code=0)
        self.assertIn("it has 7 commits we lack, we have 5 it lacks", out)
        self.assertIn("Required for a bugfix: compile regress", out)
        self.assertIn("No run in progress", out)
        self.run_cli("test", "compile", code=0)
        self.assertIn("All passed", self.last_out)


class NoNewFailuresTests(World):
    """A suite with standing failures: the gate is "no new failures"."""

    def setUp(self):
        super().setUp()
        self.count = os.path.join(self.tmp, "count")
        self.commit(self.ours, "A breakage we live with", {
            "src/old.c": "BROKEN\n"})
        self.pb = self.commit(self.peer, "Fix e handling", {
            "src/e.c": "BROKEN\n"})
        self.suite('grep -l BROKEN src/*.c > "$DIASPORA_FAILURES"; true')

    def suite(self, command, extra=""):
        self.local_conf(
            '[test "suite"]\n\tneeds = compile\n'
            '\trun = echo run >> %s; %s\n\tpass = no-new-failures\n%s'
            '[require]\n\tdefault = suite\n' % (self.count, command, extra))

    def runs(self):
        with open(self.count) as handle:
            return len(handle.readlines())

    def test_standing_failures_do_not_block(self):
        out = self.run_cli("import", "--yes", "--pick", self.p1,
                           "--pick", self.pb, code=4)
        ledger = self.ledger()
        self.assertEqual(ledger[self.p1]["verdict"], "applied")
        self.assertEqual(ledger[self.p1]["tests"], ["compile", "suite"])
        self.assertEqual(ledger[self.pb]["verdict"], "failed")
        self.assertIn("1 new failure: src/e.c", ledger[self.pb]["note"])
        self.assertIn("test suite failed: 1 new failure: src/e.c",
                      self.tried()[self.pb][0])
        self.assertIn("1 failing, none new", out)
        # the target before; the first import; the second import, and for
        # its new failure the code before it once more
        self.assertEqual(out.count("measuring suite on"), 2)
        self.assertIn("again: do they fail there too", out)
        self.assertEqual(self.runs(), 4)
        # next run: what the last good import measured is the reference
        self.run_cli("import", "--yes", "--pick", self.p3, code=0)
        self.assertNotIn("measuring suite on", self.last_out)
        self.assertEqual(self.runs(), 5)

    def test_diaspora_test_records_the_reference(self):
        out = self.run_cli("test", "suite", code=0)
        self.assertIn("1 failing, recorded", out)
        self.run_cli("import", "--yes", "--pick", self.p1, code=0)
        self.assertNotIn("measuring suite on", self.last_out)
        self.assertEqual(self.runs(), 2)

    def test_flaky_failure_is_retried(self):
        # the second run of the suite (the import's first) has a failure
        # that does not come back
        self.suite('{ [ "$(wc -l < %s)" = 2 ] && echo flaky; '
                   'grep -l BROKEN src/*.c; } > "$DIASPORA_FAILURES"; true'
                   % self.count, "\tretry = 1\n")
        out = self.run_cli("import", "--yes", "--pick", self.p1, code=0)
        self.assertIn("1 new failure) - retrying", out)
        self.assertEqual(self.ledger()[self.p1]["verdict"], "applied")
        # reference, import, reference again (not there), import again
        self.assertEqual(self.runs(), 4)

    def test_a_lucky_reference_is_measured_again(self):
        # a test that comes and goes passed in the one run that measured
        # the reference and fails afterwards, import or no import
        self.suite('{ [ "$(wc -l < %s)" != 1 ] && echo comes-and-goes; '
                   'grep -l BROKEN src/*.c; } > "$DIASPORA_FAILURES"; true'
                   % self.count)
        out = self.run_cli("import", "--yes", "--pick", self.p1,
                           "--pick", self.p3, code=0)
        self.assertIn("1 failure not in the reference", out)
        self.assertIn("1 also fails without the import", out)
        ledger = self.ledger()
        self.assertEqual(ledger[self.p1]["verdict"], "applied")
        self.assertEqual(ledger[self.p3]["verdict"], "applied")
        # reference, first import, reference again, second import: the
        # first import's own measurement is the reference of the second
        self.assertEqual(self.runs(), 4)
        self.assertEqual(out.count("measuring suite on"), 2)

    def test_the_suite_must_report(self):
        self.suite("true")
        self.run_cli("test", "suite", code=1)
        self.assertIn("did not write $DIASPORA_FAILURES", self.last_out)

    def test_a_suite_that_cannot_run_blocks(self):
        self.suite("false")
        self.run_cli("import", "--yes", "--pick", self.p1, code=3)
        self.assertIn("does not run on the code before the import",
                      self.last_err)
        self.run_cli("abort", code=0)

    def test_batch_compares_with_the_target(self):
        self.run_cli("import", "--yes", "--strategy", "batch",
                     "--pick", self.p1, "--pick", self.pb, code=4)
        ledger = self.ledger()
        self.assertEqual(ledger[self.p1]["verdict"], "applied")
        self.assertEqual(ledger[self.pb]["verdict"], "failed")

    def test_bad_mode(self):
        self.local_conf('[test "compile"]\n\tpass = maybe\n')
        with self.assertRaises(DiasporaError):
            Config.load(Repo(self.ours))


class TerminalTests(World):
    """The default mode: a person at a terminal."""

    def drive(self, args, script, timeout=60):
        """Run bin/diaspora on a pty, answering each prompt in `script`."""
        import pty
        import select
        master, slave = pty.openpty()
        proc = subprocess.Popen(
            [sys.executable, os.path.join(os.path.dirname(HERE), "bin",
                                          "diaspora"), "-C", self.ours]
            + args, stdin=slave, stdout=slave, stderr=slave, close_fds=True,
            env=dict(os.environ, TERM="dumb", NO_COLOR="1", COLUMNS="120"))
        os.close(slave)
        seen, script = "", list(script)
        try:
            while True:
                ready, _, _ = select.select([master], [], [], timeout)
                if not ready:
                    self.fail("timed out; so far:\n" + seen)
                try:
                    data = os.read(master, 65536)
                except OSError:
                    break
                if not data:
                    break
                seen += data.decode(errors="replace")
                while script and script[0][0] in seen:
                    needle, answer = script.pop(0)
                    seen_upto = seen.index(needle) + len(needle)
                    os.write(master, (answer + "\n").encode())
                    seen = seen[:seen_upto] + "<<%s>>" % answer \
                        + seen[seen_upto:]
        finally:
            os.close(master)
            proc.wait(timeout=timeout)
        self.assertEqual(script, [], "prompts never seen; output:\n" + seen)
        return proc.returncode, seen

    def test_pick_and_import(self):
        code, out = self.drive([], [
            ("Import 3 of 4?", "?"),
            ("quit without importing", "3 4"),       # unmark p4 and p5
            ("Import 1 of 4?", "2"),                 # mark the feature
            ("Import 2 of 4?", "r 3 clashes with our rework of b"),
            ("Import 2 of 3?", ""),
            ("Copy 2 imports to main?", "y")])
        self.assertEqual(code, 0, out)
        self.assertIn("main is now at", out)
        ledger = self.ledger()
        self.assertEqual(ledger[self.p1]["verdict"], "applied")
        self.assertEqual(ledger[self.p3]["verdict"], "applied")
        self.assertEqual(ledger[self.p4]["verdict"], "rejected")
        self.assertEqual(ledger[self.p4]["note"],
                         "clashes with our rework of b")
        self.assertNotIn(self.p5, ledger)

    def test_conflict_stops_for_the_person(self):
        code, out = self.drive(["--pick", self.p4], [])
        self.assertEqual(code, 3, out)
        self.assertIn("diaspora continue", out)
        ws = self.session().load().ws.path
        with open(os.path.join(ws, "src/b.c"), "w") as handle:
            handle.write("line1\nline2 merged\nline3\nline4\nline5\n")
        git(ws, "add", "src/b.c")
        code, out = self.drive(["continue"], [
            ("Copy 1 import to main?", "n")])
        self.assertEqual(code, 0, out)
        self.assertIn("diaspora promote", out)
        self.assertNotIn("merged", self.read("src/b.c"))
        code, out = self.drive(["promote"], [])
        self.assertEqual(code, 0, out)
        self.assertIn("line2 merged", self.read("src/b.c"))

    def test_quit_keeps_the_marks(self):
        code, out = self.drive([], [
            ("Import 3 of 4?", "l 4"),
            ("Import 2 of 3?", "q")])
        self.assertEqual(code, 0, out)
        ledger = self.ledger()
        self.assertEqual(ledger[self.p5]["verdict"], "deferred")
        self.assertEqual(len(ledger), 1)
        self.assertNotIn("return 2", self.read("src/a.c"))


class GuiTests(World):
    """The browser page's server, driven over HTTP as the page drives it."""

    def setUp(self):
        super().setUp()
        import argparse
        import http.server
        import threading
        from diaspora import gui
        args = argparse.Namespace(peers=[], target=None, here=False,
                                  no_agent=True, verbose=False, port=0)
        self.app = gui.App(Repo(self.ours), args)
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                                                      gui.Handler)
        port = self.server.server_address[1]
        gui.Handler.app, gui.Handler.token = self.app, "secret"
        gui.Handler.hosts = ("127.0.0.1:%d" % port,)
        self.base = "http://127.0.0.1:%d" % port
        threading.Thread(target=self.server.serve_forever,
                         daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.post("/api/scan", {"fetch": True})

    def call(self, path, body=None, token="secret"):
        import urllib.error
        import urllib.request
        req = urllib.request.Request(self.base + path, headers={
            "X-Diaspora-Token": token})
        if body is not None:
            req.data = json.dumps(body).encode()
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read().decode()
                code = resp.status
        except urllib.error.HTTPError as exc:
            data, code = exc.read().decode(), exc.code
        try:
            return code, json.loads(data)
        except ValueError:
            return code, data

    def post(self, path, body):
        code, out = self.call(path, body)
        self.assertEqual(code, 200, out)
        self.app.wait()
        self.assertIsNone(self.app.job["error"], self.app.job)
        return out

    def state(self):
        code, out = self.call("/api/state")
        self.assertEqual(code, 200, out)
        return out

    def test_the_page_needs_the_token(self):
        code, _ = self.call("/api/state", token="wrong")
        self.assertEqual(code, 403)
        code, page = self.call("/", token="secret")
        self.assertEqual(code, 200)
        self.assertIn('const TOKEN = "secret"', page)
        code, _ = self.call("/api/mark", {"commits": [], "mark": "accept"},
                            token="")
        self.assertEqual(code, 403)

    def test_accept_reject_defer_then_promote(self):
        state = self.state()
        shas = {c["commit"] for c in state["commits"]}
        self.assertIn(self.p1, shas)
        self.post("/api/mark", {"commits": [self.p1], "mark": "accept"})
        self.post("/api/mark", {"commits": [self.p3], "mark": "reject",
                                "why": "not ours"})
        self.post("/api/mark", {"commits": [self.p2], "mark": "defer"})
        marks = {c["commit"]: c["mark"] for c in self.state()["commits"]}
        self.assertEqual(marks[self.p1], "accept")
        self.post("/api/apply", {})
        state = self.state()
        self.assertEqual(state["run"]["stage"], "ready")
        self.assertEqual([i["state"] for i in state["run"]["items"]],
                         ["done"])
        # nothing lands before the run is accepted
        self.assertNotIn("return 2", self.read("src/a.c"))
        self.post("/api/run", {"action": "promote"})
        self.assertIsNone(self.state()["run"])
        self.assertIn("return 2", self.read("src/a.c"))
        ledger = self.ledger()
        self.assertEqual(ledger[self.p1]["verdict"], "applied")
        self.assertEqual(ledger[self.p3]["verdict"], "rejected")
        self.assertEqual(ledger[self.p3]["note"], "not ours")
        self.assertEqual(ledger[self.p2]["verdict"], "deferred")
        self.assertEqual(self.app.marks, {})

    def test_retry_after_a_failure(self):
        self.post("/api/mark", {"commits": [self.p5], "mark": "accept"})
        self.post("/api/apply", {})
        run = self.state()["run"]
        self.assertEqual(run["items"][0]["state"], "failed")
        self.post("/api/run", {"action": "promote"})   # records the failure
        c5 = {c["commit"]: c for c in self.state()["commits"]}[self.p5]
        self.assertEqual(c5["status"], "failed")
        self.assertIn("test regress failed", c5["tried"][0])
        # an agent that can repair it this time; it hears of the failure
        self.use_fake_agent()
        self.app.args.no_agent = False
        os.environ["FAKE_FIX"] = "sed -i s/BREAK/fine/ src/c.c"
        os.environ["FAKE_PROMPT_LOG"] = os.path.join(self.tmp, "prompts")
        self.post("/api/mark", {"commits": [self.p5], "mark": "retry"})
        self.post("/api/apply", {})
        self.assertEqual(self.state()["run"]["items"][0]["state"], "done")
        self.post("/api/run", {"action": "promote"})
        self.assertEqual(self.ledger()[self.p5]["verdict"], "adapted")
        with open(os.environ["FAKE_PROMPT_LOG"]) as handle:
            self.assertIn("tried before", handle.read())

    def test_drop_the_run(self):
        before = git(self.ours, "rev-parse", "main")
        self.post("/api/mark", {"commits": [self.p1, self.p5],
                                "mark": "accept"})
        self.post("/api/apply", {})
        code, out = self.call("/api/apply", {})        # a run is waiting
        self.assertEqual(code, 400)
        self.post("/api/run", {"action": "abort", "keep_log": False})
        self.assertIsNone(self.state()["run"])
        self.assertEqual(git(self.ours, "rev-parse", "main"), before)

    def test_show_and_bad_requests(self):
        code, out = self.call("/api/show?commit=" + self.p1)
        self.assertEqual(code, 200)
        self.assertIn("Fix crash in a", out["text"])
        code, out = self.call("/api/show?commit=" + self.p1 + "&diff=1")
        self.assertIn("return 2", out["text"])
        code, out = self.call("/api/mark", {"commits": ["f" * 40],
                                            "mark": "accept"})
        self.assertEqual(code, 400)
        code, out = self.call("/api/apply", {})
        self.assertEqual(code, 200)
        self.app.wait()
        self.assertIn("nothing is marked", self.app.job["error"])


class UserConfTests(World):
    """~/.config/diaspora: the repositories, chosen by name or from a list."""

    def setUp(self):
        super().setUp()
        self.conf_file = os.path.join(self.tmp, "userconf")
        with open(self.conf_file, "w") as handle:
            handle.write('[repo "mine"]\n\tpath = %s\n'
                         '[repo "gone"]\n\tpath = %s/nowhere\n'
                         % (self.ours, self.tmp))
        os.environ["DIASPORA_CONFIG"] = self.conf_file
        self.away = os.path.join(self.tmp, "away")
        os.mkdir(self.away)
        old = os.getcwd()
        self.addCleanup(os.chdir, old)

    def run_raw(self, *args, cwd=None, code=None, answers=None):
        """`answers`: a terminal, and what is typed at it."""
        os.chdir(cwd or self.away)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            if answers is not None:
                for stream in (sys.stdin, out):
                    stack.enter_context(mock.patch.object(
                        stream, "isatty", return_value=True))
                stack.enter_context(mock.patch("builtins.input",
                                               side_effect=answers))
            got = cli.main(list(args))
        self.last_out, self.last_err = out.getvalue(), err.getvalue()
        if code is not None:
            self.assertEqual(got, code, self.last_out + self.last_err)
        return self.last_out

    def test_by_name(self):
        out = self.run_raw("-C", "mine", "status", code=0)
        self.assertIn("Peer up:", out)
        self.run_raw("-C", "MINE", "status", code=0)      # any case
        self.run_raw("-C", "nosuch", "status", code=1)
        self.assertIn("nor a repository of that name", self.last_err)
        self.assertIn("mine, gone", self.last_err)

    def test_not_in_a_repository_and_no_terminal(self):
        self.run_raw("status", code=1)
        self.assertIn("say which repository with -C NAME: mine, gone",
                      self.last_err)
        # in a checkout without diaspora.conf, the same
        other = os.path.join(self.tmp, "other")
        git(self.tmp, "init", "-q", other)
        self.run_raw("status", cwd=other, code=1)
        self.assertIn("no diaspora.conf here", self.last_err)
        # in one of them, no question
        self.run_raw("status", cwd=os.path.join(self.ours, "src"), code=0)

    def test_pick_from_a_list(self):
        out = self.run_raw("status", code=0, answers=["9", "mine"])
        self.assertIn("Which one?", out)
        self.assertNotIn("gone", out.split("Which one?")[1].split(
            "Number")[0])                                  # missing: not offered
        self.assertIn("not one of them: '9'", out)
        self.assertIn("Peer up:", out)
        self.run_raw("status", code=1, answers=[""])
        self.assertIn("no repository chosen", self.last_err)

    def test_repos(self):
        out = self.run_raw("repos", code=0)
        self.assertIn("mine", out)
        self.assertIn("(missing)", out)
        out = self.run_raw("repos", "--json", cwd=self.ours, code=0)
        rows = {r["name"]: r for r in json.loads(out)["repos"]}
        self.assertTrue(rows["mine"]["current"])
        self.assertEqual(rows["gone"]["note"], "missing")

    def test_a_config_directory(self):
        del os.environ["DIASPORA_CONFIG"]
        base = os.path.join(self.tmp, "xdg")
        os.makedirs(os.path.join(base, "diaspora"))
        shutil.copy(self.conf_file, os.path.join(base, "diaspora", "config"))
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": base}):
            self.run_raw("-C", "mine", "status", code=0)

    def test_a_repository_used_is_added(self):
        os.environ["DIASPORA_CONFIG"] = path = os.path.join(self.tmp, "new")
        out = self.run_raw("status", cwd=self.ours, code=0)
        self.assertIn('(added %s to %s as "ours")' % (self.ours, path),
                      self.last_err)
        with open(path) as handle:
            text = handle.read()
        self.assertTrue(text.startswith("# ~/.config/diaspora"))
        self.assertIn('[repo "ours"]\n\tpath = %s\n' % self.ours, text)
        self.run_raw("status", cwd=os.path.join(self.ours, "src"), code=0)
        self.assertNotIn("added", self.last_err)
        # from anywhere now, by name
        self.run_raw("-C", "ours", "status", code=0)
        # a worktree counts as its main checkout; a checkout without
        # diaspora.conf is not added
        wt = os.path.join(self.tmp, "wt")
        git(self.ours, "worktree", "add", "-q", wt)
        self.run_raw("status", cwd=wt, code=0)
        self.assertNotIn("added", self.last_err)
        other = os.path.join(self.tmp, "other")
        git(self.tmp, "init", "-q", other)
        self.run_raw("-C", other, "repos", code=0)
        with open(path) as handle:
            self.assertEqual(handle.read(), text)

    def test_another_users_repository(self):
        """git refuses it: say so (even for gui, which must not start
        without it), and list it all the same."""
        os.environ["DIASPORA_CONFIG"] = path = os.path.join(self.tmp, "new")
        os.chmod(self.ours, 0o755)          # not shared with our group
        self.addCleanup(os.chmod, self.ours, 0o775)
        with mock.patch.dict(os.environ,
                             {"GIT_TEST_ASSUME_DIFFERENT_OWNER": "1"}):
            for args in (["gui", "--no-browser"], ["status"]):
                self.run_raw("-C", self.ours, *args, code=1)
                self.assertIn("%s belongs to" % self.ours, self.last_err)
                self.assertIn("git will not work", self.last_err)
                self.assertIn("not writable by the group", self.last_err)
            # in a group of ours we were added to after logging in
            with mock.patch.object(gitutil, "group_of",
                                   return_value=("dev", False, True)):
                self.run_raw("-C", self.ours, "status", code=1)
                self.assertIn("log out and in again", self.last_err)
            # in a group of no concern to us
            with mock.patch.object(gitutil, "group_of",
                                   return_value=("staff", False, False)):
                self.run_raw("-C", self.ours, "status", code=1)
                self.assertIn("share the repository with a group",
                              self.last_err)
        with open(path) as handle:
            self.assertIn('[repo "ours"]\n\tpath = %s\n' % self.ours,
                          handle.read())

    def test_another_users_repository_shared_with_us(self):
        """In a group of ours, and group-writable: git is told it is safe
        (for diaspora and what it runs, not in the user's git config)."""
        os.chmod(self.ours, 0o775)
        os.chmod(os.path.join(self.ours, ".git"), 0o775)
        with mock.patch.dict(os.environ,
                             {"GIT_TEST_ASSUME_DIFFERENT_OWNER": "1"}):
            out = self.run_raw("-C", self.ours, "status", code=0)
            self.assertIn("Peer up:", out)
            self.assertEqual(os.environ.get("GIT_CONFIG_VALUE_0"),
                             self.ours)
            gitutil.Repo(self.ours)          # trusted once, not twice
            self.assertEqual(os.environ["GIT_CONFIG_COUNT"], "1")
        self.assertNotIn("GIT_CONFIG_COUNT", os.environ)

    @unittest.skipIf(os.geteuid() == 0, "root can write anything")
    def test_a_repository_we_cannot_write(self):
        objects = os.path.join(self.ours, ".git", "objects")
        os.chmod(objects, 0o555)
        self.addCleanup(os.chmod, objects, 0o755)
        self.run_raw("-C", "mine", "status", code=0)    # reading is fine
        self.run_raw("-C", "mine", "fetch", code=1)
        self.assertIn("you cannot write to %s" % objects, self.last_err)
        self.run_raw("-C", "mine", "gui", "--no-browser", code=1)
        self.assertIn("you cannot write", self.last_err)

    def test_finding_claude(self):
        """`claude` not on PATH: found where its installer puts it; or
        named in ~/.config/diaspora; else a warning that says what to do."""
        from diaspora.config import AgentConf
        home = os.path.join(self.tmp, "home")
        local = os.path.join(home, ".local", "bin")
        os.makedirs(local)
        with mock.patch.dict(os.environ, {"HOME": home, "PATH": "/usr/bin:/bin"}):
            self.assertFalse(AgentConf().found)
            exe = os.path.join(local, "claude")
            with open(exe, "w") as handle:
                handle.write("#!/bin/sh\n")
            os.chmod(exe, 0o755)
            agent = AgentConf()
            self.assertEqual(agent.path, exe)
            self.assertEqual(AgentConf(command=["claude", "-x"]).argv,
                             [exe, "-x"])
            os.remove(exe)
            # the warning (diaspora.conf names no agent here)
            conf = os.path.join(self.ours, "diaspora.conf")
            with open(conf) as handle:
                text = handle.read()
            with open(conf, "w") as handle:
                handle.write(text.replace("\tcommand = none\n", ""))
            ui = mock.Mock()
            args = argparse.Namespace(no_agent=False, verbose=False)
            cli.make_agent(Repo(self.ours), Config.load(Repo(self.ours)),
                           ui, args)
            said = ui.warn.call_args[0][0]
            self.assertIn("Claude Code (`claude`), is not installed", said)
            self.assertIn("install.sh", said)
            self.assertIn("[agent] command", said)
            # named in the user's file
            with open(self.conf_file, "a") as handle:
                handle.write("[agent]\n\tcommand = %s %s\n"
                             % (sys.executable, FAKE))
            got = Config.load(Repo(self.ours)).agent
            self.assertTrue(got.found)
            self.assertEqual(got.protocol, "plain")
            self.assertEqual(got.argv, [sys.executable, FAKE])
            # but diaspora.conf, when it names one, wins
            with open(conf, "w") as handle:
                handle.write(text)
            self.assertFalse(Config.load(Repo(self.ours)).agent.enabled)

    def test_the_same_name_twice(self):
        os.environ["DIASPORA_CONFIG"] = path = os.path.join(self.tmp, "two")
        with open(path, "w") as handle:
            handle.write('[repo "ours"]\n\tpath = /elsewhere/ours')  # no \n
        self.run_raw("status", cwd=self.ours, code=0)
        name = "%s-ours" % os.path.basename(self.tmp)
        self.assertIn('as "%s"' % name, self.last_err)
        with open(path) as handle:
            self.assertIn('ours\n[repo "%s"]' % name, handle.read())

    def test_the_page_starts_without_a_repository(self):
        import argparse
        from diaspora import gui
        args = argparse.Namespace(peers=[], target=None, here=False,
                                  no_agent=True, verbose=False, port=0,
                                  no_fetch=False)
        app = gui.App(None, args)
        state = app.state()
        self.assertIsNone(state["repo"])
        self.assertEqual([r["name"] for r in state["repos"]],
                         ["mine", "gone"])
        with self.assertRaises(DiasporaError):
            app.switch("gone")
        app.switch("mine")
        app.wait()
        self.assertIsNone(app.job["error"], app.job)
        state = app.state()
        self.assertEqual(state["repo"], self.ours)
        self.assertIn(self.p1, {c["commit"] for c in state["commits"]})


class ExportTests(World):
    def test_export(self):
        self.run_cli("export", "up", "--yes", code=0)
        branches = git(self.ours, "branch", "--list",
                       "diaspora/export/up/*", "--format=%(refname:short)")
        self.assertEqual(len(branches.split()), 1)
        peer_tip = git(self.peer, "rev-parse", "main")
        self.assertEqual(self.subjects("%s..%s" % (peer_tip, branches)),
                         ["Fix rounding in our scaler"])
        self.assertNotIn("Diaspora", git(self.ours, "log", "-1",
                                         "--format=%B", branches))
        ledger = self.ledger("export")
        self.assertEqual(ledger[self.o3]["verdict"], "exported")
        self.assertEqual(set(ledger), {self.o3})
        self.assertIn("Nothing was pushed", self.last_out)
        # offered once
        self.run_cli("export", "up", "--yes", code=0)
        self.assertIn("Nothing to export", self.last_out)

    def test_fixes_to_code_the_peer_lacks_are_held_back(self):
        fix = self.commit(self.ours, "Fix the scaler again", {
            "src/ours.c": "scaler 2\n"})
        data = json.loads(self.run_cli("list", "--json", "--all", code=0))
        self.assertEqual(data["direction"], "import")
        out = self.run_cli("export", "up", code=0)
        self.assertNotIn("Fix the scaler again", out)
        self.assertIn("Fix the scaler again",
                      self.run_cli("export", "up", "--all", code=0))
        self.run_cli("export", "up", "--yes", code=0)
        self.assertNotIn(fix, self.ledger("export"))

    def test_export_list_and_withhold(self):
        out = self.run_cli("export", "up", "--all", code=0)
        self.assertIn("Fix rounding in our scaler", out)
        self.assertIn("Rework b for our needs", out)
        self.assertNotIn("private notes", out)
        self.assertNotIn("Plug leak in d", out)
        self.assertNotIn("Add diaspora.conf", out)
        self.run_cli("withhold", self.o3, code=0)
        self.assertEqual(self.ledger("export")[self.o3]["verdict"],
                         "withheld")
        self.assertNotIn("Fix rounding",
                         self.run_cli("export", "up", "--all", code=0))

    def test_imports_are_not_offered_back(self):
        other = os.path.join(self.tmp, "other")
        git(self.tmp, "clone", "-q", self.peer, other)
        self.commit(other, "Fix the thing only other has", {
            "src/other.c": "o\n"})
        self.local_conf('[peer "other"]\n\turl = %s\n\tbranch = main\n'
                        % other)
        self.run_cli("import", "other", "--yes", "--pick",
                     git(other, "rev-parse", "HEAD"), code=0)
        out = self.run_cli("export", "up", "--all", code=0)
        self.assertNotIn("only other has", out)
        self.assertIn("imported, not ours to offer", out)


if __name__ == "__main__":
    unittest.main()
