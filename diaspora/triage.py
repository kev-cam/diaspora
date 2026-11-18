# SPDX-License-Identifier: GPL-3.0-or-later
"""Decide what each candidate commit is and whether to suggest it.

A cheap pass over paths and commit messages classifies everything; an
agent then looks at the commits the cheap pass is unsure about, with our
tree at hand, and says whether the change is relevant here.  Agent
answers are cached in .git/diaspora/triage.json.
"""

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor

from .config import KINDS
from .gitutil import PathSet

DOCS = PathSet(["*.md", "*.rst", "*.adoc", "*.texi", "*.1", "doc/", "docs/",
                "www/", "NEWS", "NEWS.*", "README", "README.*", "AUTHORS",
                "THANKS", "ChangeLog", "CHANGELOG*", "CHANGES*",
                "CONTRIBUTING*", "LICENSE*", "COPYING*"])
CI = PathSet([".github/", ".gitlab-ci.yml", ".travis.yml", ".circleci/",
              "appveyor.yml", ".cirrus.yml", "azure-pipelines.yml", "ci/",
              ".gitlab/", "gitlab-ci/", "Jenkinsfile"])
TESTS = PathSet(["test/", "tests/", "t/", "testsuite/", "regress/",
                 "**/test/**", "**/tests/**", "*_test.*", "test_*.*"])
BUILD = PathSet(["Makefile", "Makefile.*", "GNUmakefile", "*.mk", "*.am",
                 "configure.ac", "configure", "CMakeLists.txt", "*.cmake",
                 "cmake/", "meson.build", "meson_options.txt", "*.m4", "m4/",
                 "autogen.sh", "*.spec", "debian/", "Dockerfile", "*.nix",
                 "flake.lock", ".gitignore", ".gitattributes",
                 ".gitmodules"])

# conventional-commit style prefixes: "fix:", "feat(parser):" ...
PREFIX = {
    "fix": "bugfix", "bugfix": "bugfix", "bug": "bugfix", "hotfix": "bugfix",
    "feat": "feature", "feature": "feature", "perf": "perf",
    "refactor": "refactor", "cleanup": "refactor", "docs": "docs",
    "doc": "docs", "test": "test", "tests": "test", "ci": "ci",
    "build": "build", "style": "style", "chore": "other", "security": "security",
}
WORDS = (
    ("security", r"\b(cve-\d+|security|vulnerab\w*|exploit\w*|"
                 r"(buffer|stack|heap|integer) overflow|use.after.free|"
                 r"out.of.bounds)\b"),
    ("bugfix", r"\b(fix(e[sd])?|fixing|bug|crash(es|ed)?|segfault\w*|"
               r"sigsegv|assert(ion)? fail\w*|leak(s|ed)?|regression|"
               r"incorrect(ly)?|wrong(ly)?|broken|deadlock|race|hang(s)?|"
               r"infinite loop|uninitiali[sz]ed|null pointer|workaround|"
               r"miscompil\w*|issue #?\d+|closes #\d+|resolves #\d+)\b"),
    ("perf", r"\b(perf(ormance)?|speed ?up|faster|slow(er)?|optimi[sz]\w*|"
             r"cache|avoid (re)?comput\w*|reduce (memory|allocations?))\b"),
    ("style", r"\b(typos?|whitespace|indent\w*|reformat\w*|formatting|lint|"
              r"spelling|clang-format|cosmetic)\b"),
    ("refactor", r"\b(refactor\w*|clean ?up|tidy|renam\w+|simplif\w+|"
                 r"move[sd]?|reorgani[sz]\w*|dead code|unused|"
                 r"remove[sd]? (old|obsolete|redundant))\b"),
    ("docs", r"\b(docs?|documentation|readme|comments?|changelog|news|"
             r"man ?page)\b"),
    ("test", r"\b(tests?|regression test|test ?case|testbench)\b"),
    ("feature", r"\b(add(s|ed)?|implement\w*|support(s|ed)?|new|introduc\w+|"
                r"allow(s|ed)?|enable[sd]?)\b"),
    ("other", r"\b(bump|release|version|merge|revert)\b"),
)
WORDS = tuple((kind, re.compile(rx, re.I)) for kind, rx in WORDS)
PREFIX_RE = re.compile(r"^\s*\[?([A-Za-z]+)\]?(?:\([^)]*\))?!?:\s")


def classify(commit):
    """(kind, confidence) from paths and the commit message alone."""
    paths = commit.paths
    for kind, pathset in (("docs", DOCS), ("ci", CI), ("test", TESTS),
                          ("build", BUILD)):
        if pathset.all(paths):
            return kind, 0.9
    m = PREFIX_RE.match(commit.subject)
    if m and m.group(1).lower() in PREFIX:
        return PREFIX[m.group(1).lower()], 0.8
    for kind, rx in WORDS:
        if rx.search(commit.subject):
            return kind, 0.6
    for kind, rx in WORDS[:3]:          # the body counts for fixes only
        if rx.search(commit.body):
            return kind, 0.4
    return "other", 0.2


# -- agent triage -------------------------------------------------------------

SCHEMA = {
    "type": "object",
    "properties": {"commits": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "sha": {"type": "string"},
            "kind": {"type": "string", "enum": list(KINDS)},
            "relevant": {"type": "string",
                         "enum": ["yes", "partial", "no", "already"]},
            "recommend": {"type": "string",
                          "enum": ["import", "review", "skip"]},
            "risk": {"type": "string", "enum": ["low", "medium", "high"]},
            "summary": {"type": "string"},
            "reason": {"type": "string"},
            "depends": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["sha", "kind", "relevant", "recommend", "summary",
                     "reason"],
    }}},
    "required": ["commits"],
}

PROMPT = """\
You are triaging commits of a peer repository for possible import into
this repository.  This is triage, not porting: be brief and do not change
any file.

This repository: {top}, branch `{target}`.
Peer: "{peer}" ({url}, branch {branch}).  Its commits are already fetched
here, so `git show <sha>` works.  The checkout may be on another branch;
look at our code with `git show {target}:<path>` and
`git grep <pattern> {target} -- <path>`.
{divergence}
For each commit below decide:
  kind      one of: {kinds}
  relevant  "yes" if the change applies to our tree; "partial"; "no" if
            the code it changes does not exist here (removed or replaced);
            "already" if our tree has an equivalent change
  recommend "import" (worth bringing in), "review" (a person should
            decide) or "skip"
  risk      low | medium | high: how likely an import breaks something here
  summary   what the commit does, at most 100 characters
  reason    one sentence for the recommendation
  depends   ids from this list that must be imported first (usually empty)

Read the diff (`git show --stat <sha>`, `git show <sha>`) when the message
is not enough, and check our tree before calling something relevant.
Answer for every commit, with the full id as `sha`.

Commits:
{commits}
"""


def _describe(commit):
    files = ", ".join("%s (+%d -%d)" % f for f in commit.files[:12])
    if len(commit.files) > 12:
        files += ", ... %d more" % (len(commit.files) - 12)
    body = commit.body
    if len(body) > 700:
        body = body[:700] + " [...]"
    return "### %s\n%s\n%s, %s\nfiles: %s\n%s\n" % (
        commit.sha, commit.subject, commit.author, commit.date, files, body)


class Cache:
    def __init__(self, repo):
        self.path = os.path.join(repo.state_dir(), "triage.json")
        try:
            with open(self.path, encoding="utf-8") as handle:
                self.data = json.load(handle)
        except (OSError, ValueError):
            self.data = {}

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(self.data, handle, indent=0, sort_keys=True)
        os.replace(tmp, self.path)


def _apply(commit, answer):
    commit.source = "agent"
    commit.kind = answer.get("kind") if answer.get("kind") in KINDS \
        else commit.kind
    commit.confidence = 1.0
    commit.summary = (answer.get("summary") or "")[:160]
    commit.reason = (answer.get("reason") or "")[:300]
    commit.recommend = answer.get("recommend")
    commit.risk = answer.get("risk")
    commit.relevant = answer.get("relevant")
    commit.depends = list(answer.get("depends") or [])


def triage(repo, conf, peer, target, commits, agent=None, say=None,
           direction="import", ask=True):
    """Classify `commits` in place and set each one's disposition.

    Earlier agent answers are always used; the agent is asked about the
    rest only if `ask` is set.
    """
    for commit in commits:
        commit.kind, commit.confidence = classify(commit)

    if direction == "import" and agent and agent.triage_enabled:
        cache = Cache(repo)
        for commit in commits:
            if commit.sha in cache.data:
                _apply(commit, cache.data[commit.sha])
    if direction == "import" and agent and agent.triage_enabled and ask:
        mode = conf.agent.triage
        todo = [c for c in commits if c.source != "agent"
                and c.status == "pending" and not c.absent
                and (mode == "all" or (c.confidence < 0.9
                                       and c.kind not in peer.ignore))]
        left = max(0, len(todo) - conf.agent.triage_max)
        todo = todo[:conf.agent.triage_max]
        by_sha = {c.sha: c for c in commits}
        size = max(1, conf.agent.triage_batch)
        batches = [todo[i:i + size] for i in range(0, len(todo), size)]
        divergence = ""
        if conf.rewritten or conf.removed:
            divergence = (
                "\nHow our tree differs: rewritten here (port the intent, "
                "the diff will not fit): %s; removed here: %s.\n"
                % (" ".join(conf.rewritten.patterns) or "-",
                   " ".join(conf.removed.patterns) or "-"))

        def question(batch):
            prompt = PROMPT.format(
                top=repo.top, target=target, peer=peer.name, url=peer.url,
                branch=peer.branch, divergence=divergence,
                kinds=" ".join(KINDS),
                commits="\n".join(_describe(c) for c in batch))
            return agent.ask("triage", prompt, SCHEMA, cwd=repo.top)

        if batches and say:
            say("triage: asking the agent about %d commit%s from %s ..." % (
                len(todo), "" if len(todo) == 1 else "s", peer.name))
        failed = 0
        with ThreadPoolExecutor(max_workers=max(
                1, conf.agent.triage_jobs)) as pool:
            for batch, answer in zip(batches, pool.map(question, batches)):
                if not answer:
                    failed += len(batch)
                    continue
                for item in answer.get("commits", []):
                    sha = str(item.get("sha", ""))
                    match = by_sha.get(sha) or next(
                        (c for c in batch if sha and c.sha.startswith(sha)),
                        None)
                    if match:
                        _apply(match, item)
                        cache.data[match.sha] = item
                cache.save()
        if failed and say:
            say("triage: no usable answer from the agent for %d commit%s "
                "(%s); the message heuristics stand" % (
                    failed, "" if failed == 1 else "s",
                    agent.last_error or "empty answer"))
        if left and say:
            say("triage: %d more commit%s left to the message heuristics "
                "(agent.triage-max = %d)" % (
                    left, "" if left == 1 else "s", conf.agent.triage_max))

    for commit in commits:
        decide(conf, peer, commit, direction)
    return commits


def decide(conf, peer, commit, direction="import"):
    """Set commit.disposition (import | review | ignore) and the reason."""
    size = commit.added + commit.deleted
    if direction == "export":
        if commit.absent:
            commit.disposition = "ignore"
            commit.why = "changes only files %s does not have" % peer.name
        elif commit.kind in peer.exports:
            commit.disposition, commit.why = "import", ""
        else:
            commit.disposition = "ignore"
            commit.why = "%s is not exported to %s" % (commit.kind, peer.name)
        return
    if commit.status != "pending":
        commit.disposition, commit.why = "review", commit.status
    elif commit.recommend == "skip" or commit.relevant in ("no", "already"):
        commit.disposition = "ignore"
        commit.why = "agent: " + (commit.reason or commit.relevant or "skip")
    elif commit.absent and commit.source != "agent":
        commit.disposition = "ignore"
        commit.why = "changes only files we do not have"
    elif commit.kind in peer.ignore:
        commit.disposition = "ignore"
        commit.why = "%s is ignored for %s" % (commit.kind, peer.name)
    elif commit.kind not in peer.imports:
        commit.disposition = "review"
        commit.why = "%s is not imported by default" % commit.kind
    elif commit.recommend == "review":
        commit.disposition, commit.why = "review", "agent asks for review"
    elif peer.max_lines and size > peer.max_lines:
        commit.disposition = "review"
        commit.why = "%d changed lines (max-lines %d)" % (size,
                                                          peer.max_lines)
    elif conf.rewritten.any(commit.paths) and commit.source != "agent":
        commit.disposition = "review"
        commit.why = "touches code rewritten here"
    else:
        commit.disposition, commit.why = "import", ""
