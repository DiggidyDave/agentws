---
name: workspace
description: Create or manage multi-repo agentic workspaces with the agentws CLI. Use when the user asks to spin up / create / set up a workspace for a problem or feature, list their workspaces, check workspace status, or tear a workspace down.
---

# Multi-repo workspaces (agentws)

`agentws` materializes a workspace directory containing several related repos,
all checked out on the same named branch. Profiles (named repo sets) live in
`~/.agentws/config.toml`.

## Creating a workspace

1. Pick the profile. Run `agentws profile list` and match the user's problem
   domain to a profile. If nothing fits, show the list and ask; offer to
   create a profile with `agentws profile add <name> <repo>...`.
2. Derive a branch/workspace name from the problem: short kebab-case,
   e.g. "the refund double-charge bug" → `fix-refund-double-charge`.
   Confirm the name with the user if it isn't obvious.
3. Create it, passing the user's problem statement as the description:

   ```
   agentws create -n <name> -p <profile> -d "<one-line problem statement>"
   ```

   Add `--clone` only if the user asks for fully isolated clones.
4. `cd` into the workspace path printed by the command and **read its
   `CLAUDE.md`** — it lists every repo, its upstream, and the working
   conventions. Then read each relevant repo's own README/CLAUDE.md before
   making changes.

## Working inside a workspace

- Every repo is already on the workspace branch; never switch branches.
- Commit per-repo; publish with `git push -u origin <branch>`.
- Repos checked out as worktrees share a git object store — do not run
  history-rewriting or maintenance commands (`gc`, `filter-branch`) in them.

## Adding a repo mid-work

- Just this workspace: `agentws add <repo>` from inside it (accepts bare
  names, expanded via the configured org, or full git URLs).
- Every future workspace of this profile: add the repo to the profile in
  `~/.agentws/config.toml`, then re-run the original `agentws create`
  command — it is idempotent and fills in only the missing repo.

## Other operations

- `agentws list` — all workspaces with dirty/unpushed rollup.
- `agentws status [<name>]` — per-repo detail; run before reporting progress.
  The name is optional when the cwd is inside the workspace.
- `agentws rm <name>` — teardown. It refuses if work is uncommitted or
  unpushed; relay that to the user rather than reaching for `--force`.
  Never use `--force` or `--delete-branches` without explicit user approval.
