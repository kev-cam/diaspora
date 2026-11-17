# SPDX-License-Identifier: GPL-3.0-or-later
"""Find the commits a peer has that we do not (and the reverse)."""

import re
from collections import Counter
from dataclasses import dataclass, field

from . import ledger as L
from .config import CONF_NAME, LOCAL_NAME, Config
from .gitutil import DiasporaError

LOG_FORMAT = "%x00%H%x1f%an%x1f%ae%x1f%aI%x1f%s%x1f%b%x1e"
ORIGIN_RE = re.compile(r"^Diaspora-Origin:\s*(\S+?)@([0-9a-f]{7,40})\b",
                       re.M | re.I)
PICKED_RE = re.compile(r"\(cherry picked from commit ([0-9a-f]{40})\)")


@dataclass
class Commit:
    sha: str
    author: str
    email: str
    date: str
    subject: str
    body: str
    files: list                      # (path, added, deleted)
    new_paths: set = field(default_factory=set)   # files the commit creates
    peer: str = None
    absent: bool = False             # changes only files not in the destination
    # triage
    kind: str = "other"
    confidence: float = 0.0
    source: str = "heuristic"        # heuristic | agent
    summary: str = ""
    reason: str = ""
    recommend: str = None            # import | review | skip
    risk: str = None
    relevant: str = None
    depends: list = field(default_factory=list)
    # state
    status: str = "pending"          # pending | deferred | failed
    note: str = ""
    disposition: str = "review"      # import | review | ignore
    why: str = ""

    @property
    def paths(self):
        return [f[0] for f in self.files]

    @property
    def added(self):
        return sum(f[1] for f in self.files)

    @property
    def deleted(self):
        return sum(f[2] for f in self.files)

    @property
    def short(self):
        return self.sha[:10]

    def as_dict(self):
        return {
            "peer": self.peer, "commit": self.sha, "author": self.author,
            "date": self.date, "subject": self.subject, "kind": self.kind,
            "confidence": round(self.confidence, 2), "triage": self.source,
            "summary": self.summary, "reason": self.reason,
            "recommend": self.recommend, "risk": self.risk,
            "relevant": self.relevant, "depends": self.depends,
            "status": self.status, "note": self.note,
            "disposition": self.disposition, "why": self.why,
            "absent": self.absent,
            "added": self.added, "deleted": self.deleted,
            "files": self.paths,
        }


def fetch(repo, peer):
    """Fetch the peer's branch into refs/diaspora/<peer>/<branch>."""
    proc = repo.proc("fetch", "--no-tags", "--quiet", peer.url,
                     "+%s:%s" % (peer.branch, peer.ref), check=False)
    if proc.returncode:
        raise DiasporaError("cannot fetch peer %s (%s %s): %s" % (
            peer.name, peer.url, peer.branch, proc.stderr.strip()))


def require_ref(repo, peer):
    if not repo.rev(peer.ref):
        raise DiasporaError(
            "peer %s has not been fetched yet (run `diaspora fetch %s`)"
            % (peer.name, peer.name))


def log(repo, *range_args):
    """Non-merge commits of a range, oldest first, with their file stats."""
    out = repo.git("-c", "core.quotepath=off", "log", "--no-merges",
                   "--topo-order", "--reverse", "--no-renames", "--numstat",
                   "--raw", "--format=" + LOG_FORMAT, *range_args)
    commits = []
    for record in out.split("\0"):
        if not record.strip():
            continue
        head, _, stats = record.partition("\x1e")
        sha, author, email, date, subject, body = head.split("\x1f", 5)
        files, new_paths = [], set()
        for line in stats.splitlines():
            parts = line.split("\t", 2)
            if line.startswith(":"):          # --raw: ":mode mode sha sha A"
                if len(parts) == 2 and parts[0].endswith("A"):
                    new_paths.add(parts[1])
            elif len(parts) == 3:             # --numstat
                files.append((parts[2],
                              int(parts[0]) if parts[0].isdigit() else 0,
                              int(parts[1]) if parts[1].isdigit() else 0))
        commits.append(Commit(sha, author, email, date[:10], subject,
                              body.strip(), files, new_paths))
    return commits


def tree_paths(repo, ref):
    return set(repo.git("-c", "core.quotepath=off", "ls-tree", "-r",
                        "--name-only", ref).splitlines())


def mark_absent(commits, tree, meta):
    """Flag commits that only change files the destination does not have."""
    for commit in commits:
        old = [p for p in commit.paths
               if p not in commit.new_paths and p not in meta]
        commit.absent = bool(old) and not any(p in tree for p in old)


def one(repo, sha):
    commits = log(repo, "--no-walk", sha)
    if not commits:
        raise DiasporaError("%s is a merge commit or does not exist" % sha)
    return commits[0]


def equivalents(repo, upstream, head):
    """Commits of `head` whose patch is already in `upstream`."""
    proc = repo.proc("cherry", upstream, head, check=False)
    return {line[2:].strip() for line in proc.stdout.splitlines()
            if line.startswith("- ")}


def origins(repo, *range_args):
    """Peer commits our history says it already took.

    Reads the Diaspora-Origin trailers and git's own `cherry picked from`
    lines.  Returns full and abbreviated ids.
    """
    text = repo.git("log", "--format=%B%x00", *range_args)
    found = {m.group(2) for m in ORIGIN_RE.finditer(text)}
    found.update(PICKED_RE.findall(text))
    return found


def _taken(sha, found):
    return sha in found or any(sha.startswith(f) for f in found
                               if len(f) < 40)


def since_args(repo, peer):
    if not peer.since:
        return []
    sha = repo.rev(peer.since)
    return ["^" + sha] if sha else ["--since=" + peer.since]


def import_candidates(repo, conf, peer, target, ledger):
    """Peer commits that could be imported into `target`.

    Returns (commits, Counter of why the others were dropped).  Commits the
    ledger holds as deferred or failed are returned with that status.
    """
    require_ref(repo, peer)
    commits = log(repo, peer.ref, "^" + target, *since_args(repo, peer))
    dropped = Counter()
    if not commits:
        return [], dropped
    same = equivalents(repo, target, peer.ref)
    taken = origins(repo, target, "^" + peer.ref)
    verdicts = ledger.latest("import", peer.name)
    peer_paths, peer_exclude = Config.peer_export_rules(repo, peer.ref)
    meta = {CONF_NAME, LOCAL_NAME, conf.ledger}
    keep = []
    for commit in commits:
        commit.peer = peer.name
        paths = [p for p in commit.paths if p not in meta]
        verdict = verdicts.get(commit.sha)
        if commit.sha in same:
            dropped["same patch already here"] += 1
        elif _taken(commit.sha, taken):
            dropped["imported earlier"] += 1
        elif verdict and verdict["verdict"] not in L.OPEN:
            dropped["decided (%s)" % verdict["verdict"]] += 1
        elif not paths:
            dropped["no files / diaspora metadata"] += 1
        elif conf.removed.all(paths):
            dropped["touches only code removed here"] += 1
        elif peer.paths and not peer.paths.any(paths):
            dropped["outside the peer's paths"] += 1
        elif peer.exclude.all(paths):
            dropped["excluded paths"] += 1
        elif peer_exclude.all(paths) or (
                peer_paths and not peer_paths.any(paths)):
            dropped["not exported by the peer"] += 1
        else:
            if verdict:
                commit.status = verdict["verdict"]
                commit.note = verdict.get("note", "")
            keep.append(commit)
    mark_absent(keep, tree_paths(repo, target), meta)
    return keep, dropped


def export_candidates(repo, conf, peer, target, ledger):
    """Our commits on `target` that the peer does not have."""
    require_ref(repo, peer)
    commits = log(repo, target, "^" + peer.ref)
    dropped = Counter()
    if not commits:
        return [], dropped
    same = equivalents(repo, peer.ref, target)
    verdicts = ledger.latest("export", peer.name)
    meta = {CONF_NAME, LOCAL_NAME, conf.ledger}
    keep = []
    for commit in commits:
        commit.peer = peer.name
        paths = [p for p in commit.paths if p not in meta]
        if commit.sha in same:
            dropped["same patch already there"] += 1
        elif ORIGIN_RE.search(commit.body) or PICKED_RE.search(commit.body):
            dropped["imported, not ours to offer"] += 1
        elif commit.sha in verdicts:
            dropped["decided (%s)" % verdicts[commit.sha]["verdict"]] += 1
        elif not paths:
            dropped["no files / diaspora metadata"] += 1
        elif conf.export_exclude.all(paths) or peer.export_exclude.all(paths):
            dropped["private paths"] += 1
        elif (conf.export_paths and not conf.export_paths.any(paths)) or (
                peer.export_paths and not peer.export_paths.any(paths)):
            dropped["outside the export paths"] += 1
        else:
            keep.append(commit)
    mark_absent(keep, tree_paths(repo, peer.ref), meta)
    return keep, dropped


def dropped_paths(conf, peer, commit, direction="import"):
    """Files of a commit that must not travel with it."""
    meta = {CONF_NAME, LOCAL_NAME, conf.ledger}
    out = []
    for path in commit.paths:
        if path in meta:
            out.append(path)
        elif direction == "import" and (conf.removed.match(path)
                                        or peer.exclude.match(path)):
            out.append(path)
        elif direction == "export" and (conf.export_exclude.match(path)
                                        or peer.export_exclude.match(path)):
            out.append(path)
    return out
