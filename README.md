# agentws

Create and manage **multi-repo agentic workspaces**.

Many changes span several repos: a service, its API gateway, its deploy
config. `agentws` lets you define that set once as a named **profile**, then
materialize a **workspace** for a specific piece of work with one command —
every repo checked out on the same named branch, plus a generated `CLAUDE.md`
context file so a coding agent (Claude Code or any other harness) launched in
the workspace starts oriented.

```console
$ agentws create -n fix-refund-flow -p payments -d "Refunds double-charge on retry"
Workspace /Users/you/dev/workspaces/fix-refund-flow
  payments-service: branch 'fix-refund-flow' from origin/main (worktree)
  api-gateway: branch 'fix-refund-flow' from origin/main (worktree)
  deploy-config: branch 'fix-refund-flow' from origin/main (worktree)

Workspace ready: cd /Users/you/dev/workspaces/fix-refund-flow
```

## How it works

By default, repos are checked out as **git worktrees** backed by shared bare
clones the tool maintains under `~/.agentws/repos/`. The first workspace that
uses a repo pays the clone cost; every later workspace is just a `git fetch`
plus `git worktree add` — near-instant and a few MB of disk. Each workspace
directory is a fully independent checkout (own branch, own index, own dirty
state): `cd` into any workspace at any time and work, no branch switching.

Because worktrees share an object store, commits made in one workspace are
instantly visible from the others, and tearing a workspace down never loses
committed work — branches survive in the shared bare clone.

Prefer full isolation (e.g. an agent you don't trust with a shared `.git`)?
Pass `--clone` to get plain full clones instead, or set `clone = true` on
individual repos in a profile.

## Install

Requires `git`. Pick one (replace `OWNER` with this repo's GitHub owner):

**As a uv/pipx tool** (recommended):

```console
$ uv tool install git+https://github.com/DiggidyDave/agentws
```

**Single file** — the script is fully self-contained
([PEP 723](https://peps.python.org/pep-0723/) inline dependencies; requires
[`uv`](https://docs.astral.sh/uv/)):

```console
$ curl -fsSL https://raw.githubusercontent.com/OWNER/agentws/main/agentws.py \
    -o ~/.local/bin/agentws && chmod +x ~/.local/bin/agentws
```

**From a clone** (nice for hacking on it; requires `uv`):

```console
$ git clone https://github.com/DiggidyDave/agentws ~/dev/agentws
$ ln -s ~/dev/agentws/agentws.py ~/.local/bin/agentws
```

Optionally, install the Claude Code skill wrapper (see
[Working with agents](#working-with-agents)) by copying or symlinking
`skill/` to `~/.claude/skills/workspace`.

First run creates `~/.agentws/config.toml` with a commented template.
Set `AGENTWS_HOME` to relocate all tool state (config + shared clones).

## Configuration

`~/.agentws/config.toml`:

```toml
[defaults]
workspace_root = "~/dev/workspaces"
org = "acme"           # expands bare repo names to git@github.com:acme/<name>.git
branch_prefix = "jane/"  # workspace 'fix-x' works on branch 'jane/fix-x'

[profiles.payments]
description = "Payments service and the repos it usually changes with"
repos = [
  "payments-service",
  "api-gateway",
  "deploy-config",
]
```

Repo entries can be tables for per-repo overrides:

```toml
repos = [
  "payments-service",
  { name = "api-gateway", base = "develop" },          # fork from a non-default branch
  { name = "deploy-config", clone = true },            # always full-clone this one
  { name = "shared-lib", url = "git@github.com:other-org/shared-lib.git" },
  { name = "monorepo", submodules = false },           # skip submodule init
]
```

Submodules are initialized recursively after checkout by default (worktrees
and clones leave them empty otherwise). Skip with `--no-submodules` on
`create`/`add`, or per-repo as above. Note: submodules in worktrees live in
the worktree's private git dir, so unlike the superproject they are cloned
fresh per workspace.

## Commands

| Command | What it does |
|---------|--------------|
| `agentws create -n NAME -p PROFILE [-d DESC] [--clone] [--base BRANCH] [--branch BRANCH]` | Create (or resume) a workspace; branch `<branch_prefix>NAME` (or `--branch` exactly) in every repo. Idempotent — re-run to fill in repos that failed or were deleted. |
| `agentws add REPO... [-w WORKSPACE] [--clone] [--base BRANCH]` | Add repo(s) to an existing workspace on its branch, without touching the profile. Workspace defaults to the one containing the cwd. To add a repo to every future workspace, edit the profile and re-run `create`. |
| `agentws list` | All workspaces with per-repo dirty/unpushed rollup. |
| `agentws status [NAME]` | Per-repo branch, dirty count, pushed state. Name defaults to the workspace containing the cwd. |
| `agentws rm NAME [--force] [--delete-branches]` | Safe teardown: refuses if any repo has uncommitted or unpushed work unless `--force`. Branches are kept in the shared clones unless `--delete-branches`. |
| `agentws profile add NAME REPO... [-d DESC]` | Define a profile (full URLs allowed). |
| `agentws profile list` / `show NAME` / `rm NAME` | Inspect or remove profiles. |

## Working with agents

`create` writes two files at the workspace root:

- `CLAUDE.md` — human/agent-readable context: the problem statement (`-d`),
  the repo table, and conventions (work on branch `NAME`, push with
  `git push -u origin NAME`, don't rewrite history in worktrees).
- `.agentws-workspace.toml` — machine-readable metadata used by
  `list`/`status`/`rm`.

Launch your agent with the workspace as its working directory and it starts
with the full cross-repo picture. A sample Claude Code skill wrapper lives in
[`skill/SKILL.md`](skill/SKILL.md) — symlink it to `~/.claude/skills/workspace`
to create workspaces from natural language ("spin up a workspace for the
refund bug").

## Notes

- A branch can only be checked out in one worktree at a time; since the
  branch is named after the workspace, this doesn't come up in practice.
- Deleting a workspace directory by hand strands worktree registrations;
  prefer `agentws rm`. (If you did: `git -C ~/.agentws/repos/<repo>.git worktree prune`.)
- Default base branch is detected per-repo from its HEAD, so `main` vs
  `master` repos mix freely in one profile.
