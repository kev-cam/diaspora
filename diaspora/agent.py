# SPDX-License-Identifier: GPL-3.0-or-later
"""Run the AI agent that triages and ports commits.

Two protocols:

  claude  `claude -p` (Claude Code, headless).  The prompt goes in on
          stdin; the answer comes back as JSON checked against a schema.
  plain   any other command.  It is run as `<command> <mode>` (mode is
          triage, discuss, port or fix) in the directory to work in, gets the
          prompt on stdin and the JSON schema in $DIASPORA_SCHEMA, and
          must print one JSON object on stdout.
"""

import json
import os
import subprocess
import threading
import time

TRIAGE_TOOLS = ["Read", "Grep", "Glob", "Bash(git show *)", "Bash(git log *)",
                "Bash(git diff *)", "Bash(git grep *)", "Bash(git ls-tree *)",
                "Bash(git cat-file *)", "Bash(git blame *)"]
PORT_TOOLS = ["Read", "Edit", "Write", "Grep", "Glob", "Bash(git *)"]
PORT_DENY = ["Bash(git push *)", "Bash(git commit *)", "Bash(git checkout -b *)",
             "Bash(git switch *)", "Bash(git branch *)", "Bash(git rebase *)",
             "Bash(git merge *)", "Bash(git remote *)", "Bash(git config *)",
             "Bash(git worktree *)"]


class Agent:
    def __init__(self, conf, log_dir, verbose=False, enabled=True):
        self.conf = conf                   # config.AgentConf
        self.log_dir = log_dir
        self.verbose = verbose
        self.enabled = enabled and conf.found
        self.cost = 0.0
        self.calls = 0
        self.last_error = None
        self._lock = threading.Lock()     # triage batches run in parallel

    @property
    def triage_enabled(self):
        return self.enabled and self.conf.triage != "off"

    @property
    def port_enabled(self):
        return self.enabled and self.conf.port

    def _command(self, mode, schema):
        conf = self.conf
        if conf.protocol == "plain":
            return conf.argv + [mode]
        cmd = conf.argv + ["-p", "--output-format", "json",
                              "--json-schema", json.dumps(schema)]
        model = conf.triage_model if mode == "triage" else conf.model
        if model:
            cmd += ["--model", model]
        if mode in ("triage", "discuss"):         # reading only
            cmd += conf.triage_args if conf.triage_args is not None else \
                ["--allowedTools"] + TRIAGE_TOOLS
        else:
            cmd += conf.port_args if conf.port_args is not None else \
                ["--permission-mode", "acceptEdits",
                 "--allowedTools"] + PORT_TOOLS + \
                ["--disallowedTools"] + PORT_DENY
        return cmd

    def ask(self, mode, prompt, schema, cwd):
        """Run the agent; return its JSON answer as a dict, or None."""
        self.last_error = None
        if not self.enabled:
            self.last_error = "no agent"
            return None
        cmd = self._command(mode, schema)
        env = dict(os.environ, DIASPORA_AGENT_MODE=mode,
                   DIASPORA_SCHEMA=json.dumps(schema))
        with self._lock:
            self.calls += 1
            stamp = "%s-%03d-%s" % (time.strftime("%H%M%S"), self.calls,
                                    mode)
        os.makedirs(self.log_dir, exist_ok=True)
        with open(os.path.join(self.log_dir, stamp + ".prompt"), "w",
                  encoding="utf-8") as handle:
            handle.write(prompt)
        try:
            proc = subprocess.run(cmd, cwd=cwd, input=prompt, env=env,
                                  capture_output=True, text=True,
                                  errors="replace", timeout=self.conf.timeout)
        except subprocess.TimeoutExpired:
            self.last_error = "timed out after %ds" % self.conf.timeout
            return None
        except OSError as exc:
            self.last_error = str(exc)
            return None
        with open(os.path.join(self.log_dir, stamp + ".answer"), "w",
                  encoding="utf-8") as handle:
            handle.write(proc.stdout)
            if proc.stderr:
                handle.write("\n--- stderr ---\n" + proc.stderr)
        if self.verbose and proc.stderr:
            print(proc.stderr, end="")
        if proc.returncode:
            self.last_error = "exit %d: %s" % (
                proc.returncode,
                (proc.stderr or proc.stdout).strip()[-300:])
            return None
        answer = self._parse(proc.stdout)
        if answer is None and not self.last_error:
            self.last_error = "answer is not a JSON object"
        return answer

    def _parse(self, text):
        data = _json_object(text)
        if data is None:
            return None
        if self.conf.protocol == "plain":
            return data
        # the `claude -p --output-format json` envelope
        with self._lock:
            self.cost += float(data.get("total_cost_usd") or 0)
        if data.get("is_error"):
            self.last_error = str(data.get("result") or "agent error")[:300]
            return None
        out = data.get("structured_output")
        if isinstance(out, dict):
            return out
        return _json_object(str(data.get("result") or ""))


def _json_object(text):
    text = text.strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except ValueError:
        pass
    # tolerate chatter around the object: take the last {...} that parses
    end = text.rfind("}")
    while end != -1:
        start = text.rfind("{", 0, end)
        while start != -1:
            try:
                data = json.loads(text[start:end + 1])
                if isinstance(data, dict):
                    return data
            except ValueError:
                pass
            start = text.rfind("{", 0, start)
        end = text.rfind("}", 0, end)
    return None
