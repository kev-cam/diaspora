# diaspora

**Bring fixes home from your peers: picked, ported and tested by an agent.**

A fork that has gone its own way cannot merge from where it came from any
more, and the project it came from is not going to merge the fork. diaspora
stops treating that as an upstream/downstream problem. Every repository
that shares your history is a **peer**: Nick's nvc is a peer of our nvc,
Sandia's Xyce is a peer of our Xyce, and our repositories are peers of
theirs. Fixes and features move between peers one at a time, by choice,
and only after they pass your tests.

You say who your peers are in a `diaspora.conf` at the top of your
repository. Then:

```
$ diaspora
Fetching nickg (https://github.com/nickg/nvc.git master) ...

nickg: 644 commits to look at: 219 suggested, 413 to review, 12 ignored by policy
  not listed: 19 excluded paths, 4 imported earlier, 2 same patch already here

  1 [x] 3cb89e1f0f 2026-01-06 bugfix   +47 -7     Parse Verilog pulse control specparams
  2 [x] d95d3e6808 2026-01-07 bugfix   +36 -33    Handle 'b' symbol in Verilog sequential UDP
  3 [ ] 5769d74252 2026-01-08 feature  +189 -38   Correctly implement x/z transitions in Verilog ...
  4 [x] df04425a0c 2026-01-08 bugfix   +23 -0     Handle empty named port connections
  ...
Import 3 of 50?  [Enter = go, numbers toggle, ? = help]
```

Pick, press Enter, and each chosen commit is applied on an import branch,
ported by the agent where it does not apply, tested as `diaspora.conf`
demands, and, if it passes, copied to your branch with a note of where it
came from. What you turn down is remembered and not offered again.

## Install

Python 3 (standard library only; tested with 3.13) and git. Run it from a
checkout (`bin/diaspora`), or:

```
make install                # under /usr/local; PREFIX=... for elsewhere
make link PREFIX=~/.local   # or a symlink to this checkout
make check                  # the tests
```

The agent is [Claude Code](https://claude.com/claude-code) run headless
(`claude -p`); any other command can stand in for it (see [The
agent](#the-agent)). Without an agent diaspora still works: it classifies
commits from their messages and stops at conflicts for you to resolve.

## Getting started

```
cd ~/src/nvc
diaspora init          # writes a starting diaspora.conf from your remotes
$EDITOR diaspora.conf  # peers, test commands, what testing is required
diaspora test          # do the test levels pass on your branch as it is?
diaspora               # list what the peers have, pick, import
```

Commit `diaspora.conf`: it is part of the repository, and peers that run
diaspora read it too (your `[export]` rules tell them what you do not
offer).

## What a run does

1. **Fetch** each peer's branch into `refs/diaspora/<peer>/<branch>`. Your
   remotes and tags are not touched.
2. **Scan.** Every peer commit you do not have, minus what is already
   settled: the same patch is already in your tree, you imported it before,
   the ledger has a verdict, it only touches paths you excluded or code you
   removed. A commit that only changes files you do not have is set aside
   (`--all` shows it).
3. **Triage.** Each remaining commit gets a kind (`bugfix`, `feature`,
   `docs`, ...). A fast pass reads paths and messages; the agent then looks
   at the commits that pass leaves open, with your tree at hand, and says
   whether the change is relevant here, how risky it is, and why. Your
   policy turns that into *suggested* (`[x]`), *to review* (`[ ]`) or
   *ignored* (not listed).
4. **Pick.** The list is interactive: toggle numbers, `s 3` shows a commit,
   `r 3 why` rejects it for good, `l 3` puts it off until later.
5. **Import** on a branch of its own, `diaspora/import/<time>`, in a
   separate worktree (your checkout and your uncommitted work are left
   alone). Per commit:
   - `git cherry-pick`. If it applies, good.
   - If it conflicts, or it touches code you have declared rewritten, the
     agent ports the *intent* of the change into your code, tests included.
     With no agent, the run stops and you resolve it by hand
     (`diaspora continue`).
   - The **test levels required** for that kind of change are run. On a
     failure the agent gets one attempt to repair the import; if it still
     fails the import is dropped from the branch and recorded as failed.
6. **Promote.** What passed is fast-forwarded onto your target branch
   (`main`, or whichever branch you were on), together with the ledger
   entries. If your branch moved in the meantime, the imports are replayed
   on it and tested again first.

Nothing reaches your branch untested, and nothing is ever pushed.

If a required test fails *without* the import, diaspora says so and stops:
no import can be validated against a broken baseline. A regression suite
that has failures of its own is handled differently: the gate is then that
the import adds no failure (see [below](#a-suite-that-already-has-failures)).

## diaspora.conf

It looks like a git config file. Values run to the end of the line and are
taken as written, so shell commands need no quoting; comments are whole
lines starting with `#` or `;`; a trailing `\` continues a line. A repeated
key adds to a list (`run` lines are joined with `&&`).

```ini
[diaspora]
	target = master
	import = bugfix security perf
	ignore = docs ci style
	export = bugfix

[peer "nickg"]
	url = https://github.com/nickg/nvc.git
	branch = master
	exclude = www/** NEWS.md .github/**

[test "compile"]
	run = mkdir -p "$DIASPORA_BUILD" && cd "$DIASPORA_BUILD"
	run = { [ -f Makefile ] || "$DIASPORA_SRC/configure"; } && make -j"$(nproc)"

[test "full-regression"]
	needs = compile
	run = make -C "$DIASPORA_BUILD" check
	timeout = 3h

[require]
	default = full-regression
	docs = none
```

[`examples/`](examples) has the `diaspora.conf` of two real forks, nvc and
Xyce. In both the full regression is the gate for every kind of change, and
since their suites have failures of their own, it is a
[no-new-failures](#a-suite-that-already-has-failures) gate fed by a
regression harness.

### `[diaspora]`

| key | meaning | default |
|-----|---------|---------|
| `target` | branch that imports land on | the branch you are on |
| `import` | kinds suggested for import | `bugfix security perf` |
| `ignore` | kinds not listed at all | `docs ci style` |
| `export` | kinds offered to peers | `bugfix security perf` |
| `max-lines` | a bigger change is listed for review, not suggested | no limit |
| `strategy` | `each`: test every import; `batch`: test them together, and one by one only if that fails | `each` |
| `promote` | `ask`, `yes` or `no`: copy what passed to the target | `ask` |
| `workspace` | `worktree` (separate, kept between runs) or `inplace` (your checkout, which must be clean) | `worktree` |
| `worktree`, `build` | where the worktree and its build directory live | `~/.cache/diaspora/<repo>/` |
| `ledger` | the ledger file | `diaspora.ledger` |

Kinds: `bugfix security perf feature test refactor docs build ci style
other`; `all` and `none` also work. Kinds in neither `import` nor `ignore`
are listed for review.

### `[peer "name"]`

| key | meaning |
|-----|---------|
| `url` | where the peer lives (or `remote = upstream` to use a git remote) |
| `branch` | its branch (default: its `HEAD`) |
| `since` | a commit or a date: ignore the peer's history before it |
| `import`, `ignore`, `export`, `max-lines` | this peer's policy, instead of the `[diaspora]` one |
| `review` | kinds to list for review even if policy would suggest or ignore them |
| `paths` | only commits touching these paths are considered |
| `exclude` | paths that are never taken; a commit touching nothing else is dropped, and these files are left out of a mixed commit |
| `test` | test levels required for imports from this peer |
| `export-paths`, `export-exclude` | which of our paths this peer is offered |
| `export-test` | test levels an export branch must pass (default: none) |

Path lists are globs: `src/**` is everything under `src`, `*.md` matches
at any depth, `*` stays within one directory.

### `[test "level"]` and `[require]`: what testing is required

A test level is a named command. `needs` puts levels in order;
`timeout` (`90s`, `45m`, `2h`) and `retry` (for a flaky suite) are
optional. Commands run in the import workspace with:

| variable | |
|----------|---|
| `DIASPORA_SRC` | the source tree under test (the workspace) |
| `DIASPORA_BUILD` | a build directory kept between runs |
| `DIASPORA_REPO` | your checkout |
| `DIASPORA_PEER`, `DIASPORA_COMMIT` | what is being imported |
| `DIASPORA_FAILURES` | where a `pass = no-new-failures` level lists the failing tests |

A level tests whatever its command builds, so the build has to pick up
everything an import can change: generated sources, a new file in the
build system, a changed configure script. A stale build passes anything.
(nvc's `configure`, for one, turns maintainer mode off in a git worktree,
after which make no longer regenerates its lexers;
[`examples/nvc.diaspora.conf`](examples/nvc.diaspora.conf) shows the way
round it.)

`[require]` says which level an import must pass, by kind of change:

```ini
[require]
	default = full-regression
	docs = none
	test = unit

[require "nickg"]
	feature = full-regression
```

Everything passes `full-regression` except documentation (no testing) and
test-only changes (`unit`); the second section is stricter or looser for
one peer. The most specific line wins: the peer's kind, the kind, the
peer's default (`test =` in its `[peer]` section), the default. With just
`default = full-regression`, the full regression is the gate for
everything.

#### A suite that already has failures

A regression in which some tests fail on your own branch cannot be a gate
by exit status. Such a level compares instead:

```ini
[test "full-regression"]
	needs = compile
	run = ./run-suite --failures-to "$DIASPORA_FAILURES"
	pass = no-new-failures
	retry = 1
```

The command runs the suite and writes the names of the failing tests, one
per line, to the file `$DIASPORA_FAILURES`; it exits 0 if the suite ran at
all. The level passes when no test fails that did not already fail on the
code before the import.

That reference is measured by running the same level on the code before
the import. It is kept in `.git/diaspora/results/`, per source tree, and
what a passing import measured is the reference for the next one, so in
the steady state an import costs one run of the suite. A reference older
than `reference-age` (default `1d`; `0` never expires) is measured again,
because the tools around the suite move too. With `retry`, a new failure
counts only if it fails again. `diaspora test full-regression` measures
the reference for your branch ahead of time.

### `[divergence]`

```ini
[divergence]
	rewritten = src/jit/**
	removed = contrib/**
```

Commits touching only `removed` paths are not applicable and never listed.
A commit touching `rewritten` paths is handed to the agent even when the
diff applies: a diff that applies to rewritten code is often wrong.

### `[export]`

`exclude` and `paths` say what never leaves this repository. They hold for
`diaspora export`, and a peer running diaspora honours them when it scans
you.

### `diaspora.local.conf`

Same syntax, next to `diaspora.conf`, not committed (`diaspora init` adds
it to `.git/info/exclude`). A key set there replaces that key from
`diaspora.conf`. Use it for what is true of one machine only: build
directory, job counts, which agent.

## The agent

```ini
[agent]
	command = claude
	triage = auto
	triage-model = haiku
	fix-attempts = 1
```

| key | meaning | default |
|-----|---------|---------|
| `command` | the agent; `none` for no agent | `claude` |
| `triage` | `auto`: ask about commits the message pass is unsure of; `all`; `off` | `auto` |
| `triage-batch`, `triage-max`, `triage-jobs` | commits per question, commits per run, questions asked at once | 20, 100, 4 |
| `port` | let the agent port conflicts and repair test failures | `true` |
| `fix-attempts` | repair attempts after a failed test | 1 |
| `model`, `triage-model` | models for porting and for triage | the agent's own default |
| `timeout` | per agent call | `30m` |
| `triage-args`, `port-args` | replace the tool permissions passed to `claude` | see below |

Triage answers are cached in `.git/diaspora/triage.json`, so a commit is
asked about once. Triage is read-only (`Read`, `Grep`, `git show/log/grep`).
For porting the agent may edit files and run git, but not commit, push or
switch branches; diaspora commits and runs the tests itself. If you want
the agent to build and run tests while porting, widen `port-args`.

Prompts and answers are kept under `.git/diaspora/logs/`, and the cost of a
run is printed at its end.

Any other program can be the agent. It is run as `<command> triage|port|fix`
in the directory to work in, gets the prompt on standard input and the
JSON schema of the answer in `$DIASPORA_SCHEMA`, and prints one JSON
object. [`tests/fake_agent.py`](tests/fake_agent.py) is a small one.

### Running diaspora from inside an agent session

An agent session can drive diaspora instead of being called by it:

```
diaspora list --json                    # candidates, with triage
diaspora show <commit>
diaspora import --pick <commit> --no-agent
                                        # exit 3: stopped at a conflict
(resolve in the workspace, git add)
diaspora continue                       # tests, records
diaspora promote                        # copy to the target branch
diaspora reject <commit> -m "why"
```

Without a terminal and without `--yes`, `import` only lists; with `--pick`
it applies and tests but leaves the result on the import branch for
`diaspora promote`. Exit status: 0 done, 3 waiting (conflict, or the
baseline fails), 4 some imports failed, 1 error.

## Provenance and the ledger

Every imported commit keeps its author and date and says where it came
from:

```
Diaspora-Origin: nickg@df04425a0c8b... (adapted)
```

`applied` means the cherry-pick went in as it was; `adapted` means the
agent or you ported it.

`diaspora.ledger`, committed next to `diaspora.conf`, has one JSON line per
decision:

```json
{"commit": "df04425a0c...", "date": "2026-11-17", "dir": "import", "kind": "bugfix", "peer": "nickg", "subject": "Handle empty named port connections", "tests": ["compile", "full-regression"], "verdict": "adapted"}
```

| verdict | |
|---------|---|
| `applied`, `adapted` | imported, with the test levels it passed |
| `rejected` | not wanted; not offered again |
| `not-applicable` | the code it changes is not here |
| `superseded` | already here in another form |
| `failed` | conflict nobody resolved, or a test failure; listed again, flagged `[!]`, never preselected |
| `deferred` | later; hidden until `--all` |
| `exported`, `withheld` | our commits, offered to a peer or held back |

Because the ledger travels with the branch, another clone, another machine
or another agent sees the same decisions, and a release branch can have a
different history of imports than `main`. `diaspora log` prints it;
`diaspora reopen <commit>` takes a verdict back. If two branches both add
lines, `diaspora.ledger merge=union` in `.gitattributes` merges them.

To set a whole stretch of a peer's history aside, give the peer a `since`.

## Trust

Importing a commit means running a peer's code in your tests, and letting
an agent read a peer's commit messages and diffs. Name as peers only
repositories you would take a patch from. The agent works in the import
worktree with file edits and git, without commit or push; whatever it
produces lands on your branch only after your tests pass, as an ordinary
commit you can read.

## Export

```
diaspora export nickg
```

lists the commits on your branch that the peer lacks and that you are
willing to offer (your own work only: imports from other peers are not
passed on; `[export]` paths and the peer's `export` kinds apply; fixes to
files the peer does not have are held back). The ones
you pick are put on `diaspora/export/<peer>/<time>`, based on the peer's
branch, ported by the agent where they do not apply, without diaspora's
own files and trailers. Nothing is pushed: push the branch to your fork
and open a pull request, or `git format-patch` it. The ledger notes what
has been offered so it is not offered twice.

## Commands

| | |
|---|---|
| `diaspora` / `diaspora import [peer...]` | list, pick, import, test, promote |
| `diaspora list [peer...]` | list only; `--json`, `--all`, `--max N` |
| `diaspora export <peer>` | prepare a branch of our commits for a peer |
| `diaspora status` | peers, ledger counts, the run in progress |
| `diaspora show <commit>` | a candidate, with its triage |
| `diaspora continue` / `skip` / `abort` | after a conflict: go on, leave the commit out, drop the run |
| `diaspora promote` | copy a finished run to the target branch |
| `diaspora reject` / `defer` / `reopen` / `withhold <commit...>` | record a verdict without importing |
| `diaspora test [level...]` | run test levels on the target branch in the workspace |
| `diaspora fetch`, `diaspora log`, `diaspora init` | |

Useful options to `import`: `--yes` (take the suggested commits and promote
without asking), `--pick <commit>`, `--no-agent`, `--no-fetch`,
`--strategy batch`, `--no-promote`, `--target <branch>`, `--here` (import
into the branch you are on, whatever `target` says), `-v` (show test
output as it runs).

## Running unattended

`diaspora import --yes --no-promote` takes the suggested commits and leaves
the tested result on its `diaspora/import/*` branch for a person to look
at; `--yes` alone also promotes. Things to know for scheduled or cloud
runs:

- A fresh clone sees only what is committed: `diaspora.conf` and the
  ledger are there, `diaspora.local.conf` and the triage cache are not.
- diaspora never pushes. If the job pushes, push `diaspora/*` branches,
  not the default branch, and protect the default branch as a backstop.
- The first build in a new workspace is a full build. Choose the required
  test levels with the machine in mind; a level that cannot run there will
  stop every import.

## Status

Version 0.1.

- `make check` (tests on scratch repositories, with a stand-in agent)
  covers scanning and policy; clean, conflicting and failing imports;
  agent porting and repair; stopping for a person and continuing, through
  a real terminal; batch testing; a broken baseline; suites with standing
  failures; a target branch that moves; both workspace kinds; verdicts;
  export.
- Tried on a clone of kev-cam/nvc with nickg/nvc as the peer and Claude as
  the agent: the scan of 644 candidate commits takes about 3 seconds;
  agent triage of 40 commits took 34 seconds; two of Nick's fixes that
  conflicted with the fork were ported by the agent, passed a `compile`
  level and landed with their trailers and ledger entries.
- kev-cam/nvc and kev-cam/xyce carry the `diaspora.conf` files in
  [`examples/`](examples), with the full regression as the gate; what has
  been run with them is in the commit messages there.

Not there yet: several branches per peer, agent triage for exports (the
export list is by message and by whether the peer has the files),
opening pull requests, importing a series of commits as one unit.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
