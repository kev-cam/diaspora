# SPDX-License-Identifier: GPL-3.0-or-later
"""The ledger: one JSON line per decision, kept in the repository.

A commit that has a verdict is not suggested again.  Later lines override
earlier ones for the same (direction, peer, commit), so a deferred commit
can become applied and `pending` reopens a decision.
"""

import datetime
import json
import os

from .gitutil import DiasporaError

# import verdicts
APPLIED = "applied"                # cherry-picked as is
ADAPTED = "adapted"                # ported by hand or by an agent
REJECTED = "rejected"              # not wanted here
NOT_APPLICABLE = "not-applicable"  # the code it changes is not here
SUPERSEDED = "superseded"          # already present in another form
DEFERRED = "deferred"              # ask again later
FAILED = "failed"                  # tried; conflict or test failure
PENDING = "pending"                # reopen: forget the earlier verdict
# export verdicts
EXPORTED = "exported"              # put on an export branch for the peer
WITHHELD = "withheld"              # not to be offered

IMPORT_VERDICTS = (APPLIED, ADAPTED, REJECTED, NOT_APPLICABLE, SUPERSEDED,
                   DEFERRED, FAILED, PENDING)
EXPORT_VERDICTS = (EXPORTED, WITHHELD, PENDING)
# a commit with one of these is still shown (flagged), not preselected
OPEN = (DEFERRED, FAILED)


def entry(peer, commit, verdict, subject="", kind=None, note=None,
          tests=None, direction="import"):
    out = {"date": datetime.date.today().isoformat(), "dir": direction,
           "peer": peer, "commit": commit, "verdict": verdict}
    if kind:
        out["kind"] = kind
    if subject:
        out["subject"] = subject
    if tests:
        out["tests"] = list(tests)
    if note:
        out["note"] = note
    return out


def dumps(item):
    return json.dumps(item, sort_keys=True, ensure_ascii=False)


class Ledger:
    def __init__(self, text=""):
        self.entries = []
        for num, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                self.entries.append(json.loads(line))
            except ValueError as exc:
                raise DiasporaError("ledger line %d: %s" % (num, exc))

    @classmethod
    def at(cls, repo, ref, path):
        """The ledger as committed on `ref` (empty if there is none)."""
        proc = repo.proc("show", "%s:%s" % (ref, path), check=False)
        return cls(proc.stdout if proc.returncode == 0 else "")

    def latest(self, direction="import", peer=None):
        """{commit: entry} with `pending` entries removed."""
        out = {}
        for item in self.entries:
            if item.get("dir", "import") != direction:
                continue
            if peer and item.get("peer") != peer:
                continue
            if item.get("verdict") == PENDING:
                out.pop(item.get("commit"), None)
            else:
                out[item.get("commit")] = item
        return out


def append(path, items):
    """Append entries to the ledger file at `path` (created if missing)."""
    try:
        with open(path, encoding="utf-8") as handle:
            old = handle.read()
    except FileNotFoundError:
        old = ""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        if old and not old.endswith("\n"):
            handle.write("\n")
        for item in items:
            handle.write(dumps(item) + "\n")
