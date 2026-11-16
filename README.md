# diaspora

**Bring fixes home without git surgery.**

diaspora is an agentic tool for keeping diverged forks in step with their
upstreams (and with each other) *selectively*. It doesn't merge, rebase, or
cherry-pick. An agent reads each candidate upstream change (the commit, its
issue, its tests), decides whether it matters to your fork, and if so
re-implements the **intent** in your tree, test first. Every decision goes
in a ledger so nothing is evaluated twice.

Initial targets:

| Project  | Fork                                                    | Upstream (confirm in config) |
|----------|---------------------------------------------------------|------------------------------|
| Xyce     | [kev-cam/xyce](https://github.com/kev-cam/xyce)         | Xyce/Xyce                    |
| nvc      | [kev-cam/nvc](https://github.com/kev-cam/nvc)           | nickg/nvc                    |
| iverilog | [kev-cam/iverilog](https://github.com/kev-cam/iverilog) | steveicarus/iverilog         |
| VACASK   | [kev-cam/VACASK](https://github.com/kev-cam/VACASK)     | arpadbuermen/VACASK          |

## Why not just cherry-pick?

Once a fork has rewritten a subsystem, upstream diffs stop applying cleanly.
Worse, sometimes they *do* apply and are semantically wrong. A diff is the
wrong thing to transfer. The thing to transfer is "this fixes X, here's the
test that proves it."

## How it works

For each project, per run:

1. **Scan.** List upstream commits since the last ledger watermark.
2. **Triage (cheap).** Use paths, messages, and the divergence map to drop
   anything irrelevant, already present, or touching code the fork has
   deleted. Personal preferences filter further (see below).
3. **Port (expensive, candidates only).**
   - Port or write the test. Show it **fails** on the fork.
   - Port the fix: a near-literal apply in areas still close to upstream,
     an intent-level re-implementation in rewritten areas.
   - Show the test **passes** and the fork's regression suite is still green.
4. **Record.** Commit on a `diaspora/<project>/<topic>` branch with a
   provenance trailer, and add a ledger entry.

Each commit carries a provenance trailer:

```
Diaspora-Origin: nvc@3f2a9c1 (adapted)
Diaspora-Test: test/regress/issue1234.vhd
```

Ledger verdicts are `applied`, `adapted`, `rejected`, `not-applicable`, and
`superseded`. A ledger entry with its verifying test is also a traceability
record, which is useful evidence for DO-254 / ISO 26262 style flows.

The same machinery runs in reverse (**gather**): it packages fork-only fixes
as upstream-ready patches or PRs.

## Repository layout (planned)

Most of this repo is configuration. The agent's behaviour comes from these
files, not from code.

```
projects/
  nvc.toml          # shared, per-project facts
  xyce.toml
  iverilog.toml
  vacask.toml
prefs/
  default.toml      # baseline import policy
  kev.toml          # personal overrides (see "Local preferences")
ledger/
  nvc.jsonl         # one line per upstream commit evaluated
env/
  setup.sh          # cloud environment setup script (paste into the env)
CLAUDE.md           # instructions the agent follows on every run
```

### Project config: `projects/<name>.toml`

Facts about the project that are true for everyone:

```toml
name     = "nvc"
fork     = { repo = "kev-cam/nvc", branch = "master" }
upstream = { repo = "nickg/nvc",   branch = "master" }

[build]
configure = "./autogen.sh && mkdir -p build && cd build && ../configure"
build     = "make -C build -j4"
test      = "make -C build check"
timeout_min = 25

# Where the fork has diverged. Drives "literal apply" vs "re-implement".
[divergence]
rewritten = ["src/jit/**"]        # port intent only
close     = ["src/lexer.l", "lib/**"]  # near-literal apply is fine
removed   = ["contrib/**"]        # anything here is not-applicable
```

### Local preferences: `prefs/*.toml`

Your policy on **what may be brought in**, kept separate from project facts:

```toml
[nvc]
accept   = ["bugfix", "security", "perf"]
review   = ["feature"]            # port, but leave the branch for a human
reject   = ["style", "ci", "docs"]
paths_deny = ["src/vhpi/**"]      # never touch, whatever the category
max_diff_lines = 400              # bigger than this -> review, not auto
```

Precedence: `prefs/default.toml` < `prefs/<user>.toml` <
`~/.config/diaspora/prefs.toml` (if present).

> **Note for cloud runs:** a cloud session starts from a fresh clone, so it
> sees only what is committed. A gitignored local file won't exist there.
> Either commit `prefs/<user>.toml` (simplest), or have the cloud
> environment's setup script write `~/.config/diaspora/prefs.toml`.

## Running on Anthropic's cloud (Claude Code on the web)

Runs happen as Claude Code cloud sessions: a fresh Ubuntu 24.04 VM with your
repos cloned in. Start them from [claude.ai/code](https://claude.ai/code),
the desktop or mobile app, or `claude --cloud` in a terminal.

### 1. Create a cloud environment

At claude.ai/code, open the environment selector and create a `diaspora`
environment:

- **Network access: Trusted.** This covers GitHub and the standard package
  registries. Use **Custom** only if a dependency lives on another host
  (e.g. an OpenVAF binary release).
- **Setup script.** Installs build dependencies; paste in `env/setup.sh`.
  A starting point:

  ```bash
  #!/bin/bash
  set -e
  apt-get update
  apt-get install -y \
    build-essential autoconf automake pkg-config flex bison gperf check \
    llvm-dev libdw-dev libffi-dev zlib1g-dev libzstd-dev \
    cmake libsuitesparse-dev libblas-dev liblapack-dev libfftw3-dev
  # Personal prefs without committing them (optional):
  # mkdir -p ~/.config/diaspora && cat > ~/.config/diaspora/prefs.toml <<'EOF'
  # ...
  # EOF
  ```

- **No secrets in env vars.** Anyone using the environment can read them.

The setup script's result is cached as a filesystem snapshot (rebuilt about
weekly, or when you edit the script), but **only if it finishes in about 5
minutes**. Keep it to package installs. Don't build anything big in it.

### 2. Start a session with the right repos attached

Attach **diaspora plus the fork(s)** for the run, and the upstream repos too.
The session's GitHub proxy only serves repositories attached to the session.

Then prompt, e.g.:

> Run diaspora triage for nvc and port the accepted candidates.

Sessions with several repos don't load hooks from any repo's
`.claude/settings.json`, so put environment setup in the setup script, not
in a SessionStart hook.

### 3. Schedule it

Create a **scheduled task** (formerly "routines") at
[claude.ai/code/routines](https://claude.ai/code/routines), or with
`/schedule` in the CLI. Give it the same repos, the `diaspora` environment,
and a prompt like "Run diaspora for all projects; report new verdicts."
Weekly is plenty; the minimum interval is 1 hour.

Scheduled runs are **fully autonomous, with no approval prompts**. So:

- diaspora only ever pushes `diaspora/*` branches. It never pushes to the
  default branch.
- Protect `main`/`master` on each fork with a branch rule as a backstop.
- `gh pr …` is blocked in cloud sessions (it uses GraphQL). Open PRs through
  the REST API (`gh api repos/{owner}/{repo}/pulls …`) or the built-in
  GitHub tools.

### Cloud limits that matter here

Each VM has roughly **4 vCPU / 16 GB RAM / 30 GB disk**. A single command can
run for up to ~10 min in the foreground plus ~30 min in the background.

- **iverilog, nvc:** fine.
- **VACASK:** fine once OpenVAF is available (allow its download host, or
  vendor a binary).
- **Xyce:** building Trilinos from source won't fit in the setup-script
  window and strains the VM. Options: publish a prebuilt Trilinos/Xyce
  dependency tarball as a GitHub release on your fork and unpack it in the
  setup script, or run Xyce verification on your own hardware via Claude
  Code Remote Control / a self-hosted environment.

## Status

Early design. Next steps:

1. `projects/nvc.toml` and `prefs/default.toml`
2. `CLAUDE.md` with the scan → triage → port → record loop
3. Ledger schema plus a first end-to-end run on nvc
4. Then iverilog, VACASK, and Xyce (hardest) last

## License

See [LICENSE](LICENSE).
