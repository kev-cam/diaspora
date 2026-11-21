# SPDX-License-Identifier: GPL-3.0-or-later
"""Discussing an import: what you said about a commit, and the agent's answers.

You say how a commit should be brought in ("take only the parser part, keep
our error messages"); the agent, if asked, answers with how it would do it,
reading the commit and our code but changing nothing.  The discussion is
kept in .git/diaspora/discussion.json (this clone only):

    {sha: [{"who": "you" | "agent", "date": "...", "text": "..."}]}

Whenever the commit is imported, by any route (Accept, Retry, Merge, the
page's Discuss mark, `diaspora import --pick`, auto-import), the agent that
ports or repairs it is given the discussion, and looks the import over
against it even when the cherry-pick applied cleanly.
"""

import json
import os
import time

from . import scan
from . import tried as T

PROMPT = """\
A person is deciding how to bring one commit from the repository "{peer}"
({url}) into this one ({top}, branch {target}).  Nothing is being imported
yet: read, but do not change any file.

Commit {sha}:
{message}

See it with `git show {sha}`; our code is in {top}.
{triage}{history}
The discussion so far ("you" is the person, "agent" is you):
{thread}

Answer the person's last message.  Say how you would bring this commit in,
given what they said: what to take as it is, what to adapt and how, what to
leave out, and what could go wrong.  If they asked a question, answer it.
Be concrete (files, functions) and brief.

Report `reply`: plain text, at most 1500 characters.
"""

SCHEMA = {
    "type": "object",
    "properties": {"reply": {"type": "string"}},
    "required": ["reply"],
}

NAME = "discussion.json"


class Discussion:
    def __init__(self, repo):
        self.path = os.path.join(repo.state, NAME)
        self.repo = repo
        self.data = {}
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                self.data = data
        except (OSError, ValueError):
            pass

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(self.data, handle, indent=1)
        os.replace(tmp, self.path)

    def thread(self, sha):
        return list(self.data.get(sha) or [])

    def add(self, sha, who, text):
        text = (text or "").strip()
        if not text:
            return
        self.data.setdefault(sha, []).append({
            "who": who, "date": time.strftime("%Y-%m-%d %H:%M"),
            "text": text})
        self.save()

    def clear(self, sha):
        if self.data.pop(sha, None) is not None:
            self.save()

    def text(self, sha):
        """The discussion, for a prompt; '' if there is none."""
        return as_text(self.thread(sha))


def as_text(thread):
    return "\n".join("%s (%s):\n%s" % (
        m.get("who", "you"), m.get("date", ""),
        "\n".join("  " + line for line in str(m.get("text", "")).splitlines()))
        for m in thread)


def guidance(repo, sha):
    """The discussion as a block for the porting and repair prompts."""
    text = Discussion(repo).text(sha)
    if not text:
        return ""
    return ("\nThe person importing this commit discussed how it should be "
            "done (\"you\" is the person, \"agent\" an earlier answer of "
            "yours).  Do what the person asked; where they and an earlier "
            "answer disagree, the person wins:\n%s\n" % text)


def reply(repo, conf, agent, peer, sha, target, commit=None):
    """Ask the agent to answer the discussion; returns its reply."""
    disc = Discussion(repo)
    thread = disc.thread(sha)
    if not thread or thread[-1].get("who") != "you":
        raise ValueError("say something first: the agent answers you")
    one = scan.one(repo, sha)
    triage = ""
    if commit is not None and getattr(commit, "source", "") == "agent":
        triage = "\nYour earlier look at it: %s / relevant: %s / risk: %s - " \
                 "%s\n" % (commit.kind, commit.relevant, commit.risk,
                           commit.reason or commit.summary)
    blocks = T.Log.at(repo, target, conf.log).about(sha)[-3:]
    history = ("\nIt was tried before and did not work (from %s):\n%s\n"
               % (conf.log, "\n".join(blocks))) if blocks else ""
    prompt = PROMPT.format(
        peer=peer.name, url=peer.url, top=repo.top, target=target, sha=sha,
        message=(one.subject + "\n\n" + one.body).strip(), triage=triage,
        history=history, thread=as_text(thread))
    agent.log_dir = repo.state_dir("logs", "discuss")
    answer = agent.ask("discuss", prompt, SCHEMA, cwd=repo.top)
    if not answer or not str(answer.get("reply", "")).strip():
        raise RuntimeError("no answer from the agent (%s)" % (
            agent.last_error or "empty reply"))
    text = str(answer["reply"]).strip()
    disc.add(sha, "agent", text)
    return text
