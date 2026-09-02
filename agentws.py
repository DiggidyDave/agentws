#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "typer>=0.12",
#   "tomlkit>=0.12",
#   "requests>=2.31",
# ]
# ///
"""agentws — create and manage multi-repo agentic workspaces.

A workspace is a directory containing checkouts of several related repos,
all on the same named branch, plus a generated CLAUDE.md so agents start
oriented. Repo sets are defined as named profiles in config.toml under
AGENTWS_HOME (default ~/.agentws).

By default each repo is a git worktree backed by a shared bare clone in
AGENTWS_HOME/repos, so creating a workspace is nearly instant after the
first use of a repo. Pass --clone for fully isolated full clones instead.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import requests
import tomlkit
import typer

app = typer.Typer(
    help=__doc__,
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)
profile_app = typer.Typer(help="Manage workspace profiles.", no_args_is_help=True)
app.add_typer(profile_app, name="profile")
project_app = typer.Typer(
    help="Manage projects: long-lived efforts with a living doc, spanning many workspaces.",
    no_args_is_help=True,
)
app.add_typer(project_app, name="project")

WS_META = ".agentws-workspace.toml"

DEFAULT_CONFIG = """\
# agentws configuration.
#
# defaults.org expands bare repo names to git@github.com:<org>/<name>.git.
# Profile repo entries are either a plain name, or a table for overrides:
#   { name = "api-gateway", base = "develop", clone = true }
#   { name = "internal-tool", url = "git@github.com:other-org/internal-tool.git" }
#
#   base       — branch to fork the workspace branch from (default: repo's default branch)
#   clone      — always full-clone this repo into workspaces instead of using a worktree
#   url        — explicit clone URL (overrides defaults.org expansion)
#   submodules — set false to skip submodule init for this repo (default true)

[defaults]
workspace_root = "~/dev/workspaces"
org = ""  # e.g. "acme" -> git@github.com:acme/<repo>.git
branch_prefix = ""  # e.g. "jane/" -> workspace 'fix-x' works on branch 'jane/fix-x'

# Example profile:
#
# [profiles.payments]
# description = "Payments service and the repos it usually changes with"
# repos = [
#   "payments-service",
#   "api-gateway",
#   "deploy-config",
# ]

# Projects are long-lived efforts that span many workspaces. Each has a
# living document (local path or URL) holding shared context: open work
# items and notes from completed changes. Manage with 'agentws project'.
#
# [projects.checkout-v2]
# description = "Rework the checkout flow"
# doc = "~/.agentws/projects/checkout-v2.md"
# profile = "payments"  # default profile for this project's workspaces
"""

PROJECT_DOC_SKELETON = """\
# Project: {name}

{description}

## Goal

(What does "done" look like for this project?)

## Work items

- [ ] ...

## Change log

(One entry per completed change: date, branch, what changed in which repos,
follow-ups. Newest first.)

## Notes

(Shared context worth carrying between changes.)
"""


class Fail(Exception):
    """Fatal error with a user-facing message."""


# ---------------------------------------------------------------- output ----

def info(msg: str) -> None:
    typer.echo(msg)


def ok(msg: str) -> None:
    typer.secho(msg, fg=typer.colors.GREEN)


def warn(msg: str) -> None:
    typer.secho(msg, fg=typer.colors.YELLOW)


def err(msg: str) -> None:
    typer.secho(msg, fg=typer.colors.RED, err=True)


# ------------------------------------------------------------------- git ----

def git(*args: str, cwd: Path | str | None = None) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True
    )
    if proc.returncode != 0:
        where = f" (in {cwd})" if cwd else ""
        raise Fail(f"git {' '.join(args)} failed{where}:\n{proc.stderr.strip()}")
    return proc.stdout.strip()


def git_ok(*args: str, cwd: Path | str | None = None) -> bool:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True
    )
    return proc.returncode == 0


# ---------------------------------------------------------------- config ----

def agentws_home() -> Path:
    return Path(os.environ.get("AGENTWS_HOME", "~/.agentws")).expanduser()


def config_path() -> Path:
    return agentws_home() / "config.toml"


def bare_repos_dir() -> Path:
    return agentws_home() / "repos"


def project_docs_dir() -> Path:
    return agentws_home() / "projects"


def project_context(cfg: tomlkit.TOMLDocument, project_name: str | None) -> dict | None:
    """Resolve a project name to what CLAUDE.md rendering needs.

    Tolerant of projects since removed from config (workspace metadata may
    still reference them).
    """
    if not project_name:
        return None
    p = cfg.get("projects", {}).get(project_name, {})
    return {
        "name": project_name,
        "doc": p.get("doc", ""),
        "description": p.get("description", ""),
    }


def load_config() -> tomlkit.TOMLDocument:
    path = config_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(DEFAULT_CONFIG)
        warn(f"Created default config at {path} — add your org and profiles.")
    return tomlkit.parse(path.read_text())


def save_config(doc: tomlkit.TOMLDocument) -> None:
    config_path().write_text(tomlkit.dumps(doc))


def workspace_root(cfg: tomlkit.TOMLDocument) -> Path:
    root = cfg.get("defaults", {}).get("workspace_root", "~/dev/workspaces")
    return Path(root).expanduser()


@dataclass
class RepoSpec:
    name: str
    url: str
    base: str | None  # None -> repo's default branch
    clone: bool
    submodules: bool = True


def resolve_repos(cfg: tomlkit.TOMLDocument, profile: str) -> list[RepoSpec]:
    profiles = cfg.get("profiles", {})
    if profile not in profiles:
        known = ", ".join(sorted(profiles)) or "(none defined)"
        raise Fail(
            f"Unknown profile '{profile}'. Known profiles: {known}\n"
            f"Add one with: agentws profile add {profile} <repo>... "
            f"or edit {config_path()}"
        )
    org = cfg.get("defaults", {}).get("org", "")
    specs: list[RepoSpec] = []
    for entry in profiles[profile].get("repos", []):
        if isinstance(entry, str):
            table: dict = {"name": entry}
        else:
            table = dict(entry)
        name = table.get("name", "")
        if not name:
            raise Fail(f"Profile '{profile}' has a repo entry with no name: {entry}")
        url = table.get("url", "")
        if not url:
            if not org:
                raise Fail(
                    f"Repo '{name}' has no url and defaults.org is not set "
                    f"in {config_path()}"
                )
            url = f"git@github.com:{org}/{name}.git"
        specs.append(
            RepoSpec(
                name=name,
                url=url,
                base=table.get("base") or None,
                clone=bool(table.get("clone", False)),
                submodules=bool(table.get("submodules", True)),
            )
        )
    if not specs:
        raise Fail(f"Profile '{profile}' has no repos.")
    return specs


def repo_arg_to_spec(
    arg: str, org: str, clone: bool, base: str | None
) -> RepoSpec:
    """Turn a CLI repo argument (bare name or git URL) into a RepoSpec."""
    if arg.startswith(("git@", "https://", "ssh://", "file://")) or "/" in arg:
        name = arg.rstrip("/").split("/")[-1].removesuffix(".git")
        return RepoSpec(name=name, url=arg, base=base, clone=clone)
    if not org:
        raise Fail(
            f"Repo '{arg}' is a bare name but defaults.org is not set in "
            f"{config_path()}; pass a full git URL instead."
        )
    return RepoSpec(
        name=arg, url=f"git@github.com:{org}/{arg}.git", base=base, clone=clone
    )


# ------------------------------------------------------------- workspace ----

def ensure_bare(spec: RepoSpec) -> Path:
    """Clone (first use) or refresh the shared bare copy of a repo."""
    bare = bare_repos_dir() / f"{spec.name}.git"
    if not bare.exists():
        info(f"  first use of {spec.name}: cloning {spec.url} ...")
        bare_repos_dir().mkdir(parents=True, exist_ok=True)
        git("clone", "--bare", spec.url, str(bare))
        # Bare clones have no fetch refspec; without one, fetch updates nothing.
        git(
            "config",
            "remote.origin.fetch",
            "+refs/heads/*:refs/remotes/origin/*",
            cwd=bare,
        )
    git("fetch", "--prune", "origin", cwd=bare)
    return bare


def repo_default_branch(repo: Path) -> str:
    """Default branch of a bare clone (from its HEAD symref)."""
    ref = git("symbolic-ref", "--short", "HEAD", cwd=repo)
    return ref.removeprefix("origin/")


def init_submodules(spec: RepoSpec, dest: Path, want: bool) -> None:
    if not want or not spec.submodules or not (dest / ".gitmodules").exists():
        return
    info(f"  {spec.name}: initializing submodules ...")
    git("submodule", "update", "--init", "--recursive", cwd=dest)


def add_worktree(spec: RepoSpec, dest: Path, branch: str, base_override: str | None) -> str:
    bare = ensure_bare(spec)
    base = base_override or spec.base or repo_default_branch(bare)
    if git_ok("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", cwd=bare):
        # Branch already exists (e.g. resuming a workspace): attach to it.
        git("worktree", "add", str(dest), branch, cwd=bare)
    else:
        if not git_ok("rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{base}", cwd=bare):
            raise Fail(f"{spec.name}: base branch 'origin/{base}' does not exist")
        git(
            "worktree", "add", "--no-track",
            "-b", branch, str(dest), f"origin/{base}",
            cwd=bare,
        )
    return base


def clone_repo(spec: RepoSpec, dest: Path, branch: str, base_override: str | None) -> str:
    git("clone", spec.url, str(dest))
    head = git("symbolic-ref", "--short", "refs/remotes/origin/HEAD", cwd=dest)
    base = base_override or spec.base or head.removeprefix("origin/")
    if git_ok("rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}", cwd=dest):
        git("checkout", branch, cwd=dest)  # branch already pushed: track it
    else:
        git("checkout", "--no-track", "-b", branch, f"origin/{base}", cwd=dest)
    return base


def write_workspace_meta(
    ws_dir: Path, name: str, branch: str, profile: str, description: str,
    repos: list[dict], project: str | None = None,
) -> None:
    doc = tomlkit.document()
    doc["name"] = name
    doc["profile"] = profile
    doc["branch"] = branch
    doc["description"] = description
    if project:
        doc["project"] = project
    aot = tomlkit.aot()
    for repo in repos:
        item = tomlkit.table()
        item.update(repo)
        aot.append(item)
    doc["repos"] = aot
    (ws_dir / WS_META).write_text(tomlkit.dumps(doc))


def fetch_description_doc(url: str) -> str | None:
    """Fetch a description URL; return its text if it's an embeddable document.

    Returns None for pages that can't be usefully embedded (HTML apps like
    issue trackers, auth walls, fetch errors) — callers fall back to linking.
    """
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "agentws"})
        resp.raise_for_status()
    except requests.RequestException as exc:
        warn(f"  could not fetch description document ({exc}); linking it instead")
        return None
    if "html" in resp.headers.get("content-type", "").lower():
        return None
    body = resp.text.strip()
    if body[:15].lower().startswith(("<!doctype", "<html")):
        return None
    if len(body) > 16_000:
        body = body[:16_000] + "\n\n… (truncated; read the full document at the URL above)"
    return body


def render_problem(description: str) -> str:
    if not description:
        return "(no description provided — ask the user what this workspace is for)"
    if description.startswith(("http://", "https://")):
        doc = fetch_description_doc(description)
        if doc is not None:
            return f"Issue document: {description}\n\n{doc}"
        return (
            f"Issue document: {description}\n\n"
            "(Content could not be embedded automatically — likely an "
            "authenticated or dynamic page. Fetch this URL with your available "
            "tools — browser, issue-tracker MCP, etc. — and read it before "
            "starting work.)"
        )
    return description


def render_project_section(project: dict | None) -> str:
    """The Project block for CLAUDE.md; empty string for one-off workspaces."""
    if not project:
        return ""
    desc = f" — {project['description']}" if project.get("description") else ""
    doc = project.get("doc", "")
    if not doc:
        where = ("(Its document is no longer configured; ask the user where "
                 "the project doc lives.)")
    elif doc.startswith(("http://", "https://")):
        where = (f"Living project document: {doc}\n"
                 "(Fetch it with your available tools — browser, issue-tracker "
                 "MCP, etc.)")
    else:
        where = f"Living project document: `{Path(doc).expanduser()}`"
    return f"""\

## Project

Part of the long-running project `{project['name']}`{desc}. The project
document is shared, living context that outlives this workspace: the goal,
open work items, and notes from changes made in earlier workspaces.

{where}

- Read the project document at the start of every session.
- When work here completes a project work item, update the document — check
  the item off and add a change-log entry (what changed, in which repos, on
  which branch, plus follow-ups) — before this workspace is removed.
"""


def write_claude_md(
    ws_dir: Path, name: str, branch: str, profile: str, description: str,
    repos: list[dict], project: dict | None = None,
) -> None:
    rows = "\n".join(
        f"| `{r['name']}/` | {r['url']} | `{r['base']}` | {r['mode']} |"
        for r in repos
    )
    problem = render_problem(description)
    content = f"""\
# Workspace: {name}

Multi-repo workspace generated by `agentws` from profile `{profile}`.
Every repo below is checked out on branch `{branch}`.

## Problem

{problem}
{render_project_section(project)}
## Repos

| Directory | Upstream | Based on | Checkout |
|-----------|----------|----------|----------|
{rows}

## Conventions

- Work on branch `{branch}` in every repo; do not switch branches here.
- Commit per-repo as usual; publish with `git push -u origin {branch}`.
- Repos checked out as `worktree` share their git object store with other
  workspaces — normal git usage is fine, but avoid history-rewriting or
  maintenance commands (`gc`, `filter-branch`, pruning) inside them.
- `agentws status {name}` shows per-repo dirty/pushed state across the
  whole workspace; `agentws rm {name}` tears it down safely.
"""
    (ws_dir / "CLAUDE.md").write_text(content)


def find_enclosing_workspace() -> Path:
    """Walk up from cwd to the nearest directory containing a workspace meta file."""
    for d in [Path.cwd(), *Path.cwd().parents]:
        if (d / WS_META).exists():
            return d
    raise Fail(
        "Not inside an agentws workspace; pass a workspace name "
        "(agentws status <name>)."
    )


def load_workspace_meta(ws_dir: Path) -> dict:
    meta = ws_dir / WS_META
    if not meta.exists():
        raise Fail(
            f"{ws_dir} is not an agentws workspace (no {WS_META}). "
            "Refusing to touch it."
        )
    return tomlkit.parse(meta.read_text())


@dataclass
class RepoState:
    name: str
    branch: str
    dirty: int          # changed/untracked paths
    unpushed: int       # commits not reachable from any origin ref
    has_upstream: bool  # origin/<branch> exists


def repo_state(repo_dir: Path) -> RepoState:
    branch = git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo_dir)
    dirty = len(
        [l for l in git("status", "--porcelain", cwd=repo_dir).splitlines() if l]
    )
    unpushed_out = git(
        "rev-list", "HEAD", "--not", "--remotes=origin", "--count", cwd=repo_dir
    )
    has_upstream = git_ok(
        "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}",
        cwd=repo_dir,
    )
    return RepoState(
        name=repo_dir.name,
        branch=branch,
        dirty=dirty,
        unpushed=int(unpushed_out),
        has_upstream=has_upstream,
    )


# -------------------------------------------------------------- commands ----

@app.command()
def create(
    name: str = typer.Option(
        ..., "-n", "--name",
        help="Workspace name; branch name is defaults.branch_prefix + this.",
    ),
    branch_override: str = typer.Option(
        None, "--branch",
        help="Exact branch name to use, ignoring name and branch_prefix.",
    ),
    profile: str = typer.Option(
        None, "-p", "--profile",
        help="Profile to use (optional when --project defines one).",
    ),
    project: str = typer.Option(
        None, "--project",
        help="Long-running project this workspace is part of (see 'agentws "
             "project'). Its living doc is referenced in CLAUDE.md and its "
             "profile is used when -p is omitted.",
    ),
    description: str = typer.Option(
        "", "-d", "--description",
        help="What this workspace is for: freeform text, or an http(s) URL to "
             "an issue/design doc (markdown docs are embedded in the generated "
             "CLAUDE.md; tracker pages like Jira/Linear are linked for the "
             "agent to fetch).",
    ),
    clone: bool = typer.Option(
        False, "--clone",
        help="Full-clone every repo into the workspace instead of shared worktrees.",
    ),
    base: str = typer.Option(
        None, "--base",
        help="Base branch for the new branch (default: each repo's default branch).",
    ),
    submodules: bool = typer.Option(
        True, "--submodules/--no-submodules",
        help="Initialize submodules recursively after checkout.",
    ),
) -> None:
    """Create (or resume) a workspace: every profile repo on the same new branch."""
    cfg = load_config()
    prefix = cfg.get("defaults", {}).get("branch_prefix", "")
    branch = branch_override or f"{prefix}{name}"
    if not git_ok("check-ref-format", "--branch", branch):
        raise Fail(f"'{branch}' is not a valid git branch name.")
    ws_dir = workspace_root(cfg) / name

    if ws_dir.exists() and not (ws_dir / WS_META).exists() and any(ws_dir.iterdir()):
        raise Fail(f"{ws_dir} exists and is not an agentws workspace.")

    # Resuming an existing workspace: its recorded branch, description,
    # project, and per-repo details win over what this invocation derives.
    existing = None
    if (ws_dir / WS_META).exists():
        existing = load_workspace_meta(ws_dir)
        branch = branch_override or existing.get("branch", branch)
        description = description or existing.get("description", "")
        project = project or existing.get("project")
    prior = {r["name"]: dict(r) for r in (existing or {}).get("repos", [])}

    projects_cfg = cfg.get("projects", {})
    if project and project not in projects_cfg and not (existing or {}).get("project"):
        known = ", ".join(sorted(projects_cfg)) or "(none defined)"
        raise Fail(
            f"Unknown project '{project}'. Known projects: {known}\n"
            f"Add one with: agentws project add {project}"
        )
    if not profile and project:
        profile = projects_cfg.get(project, {}).get("profile") or None
    if not profile:
        profile = (existing or {}).get("profile") or None
    if not profile:
        raise Fail(
            "No profile: pass -p/--profile, or use --project with a project "
            "that defines a default profile."
        )
    specs = resolve_repos(cfg, profile)

    ws_dir.mkdir(parents=True, exist_ok=True)
    info(f"Workspace {ws_dir}")

    repos_meta: list[dict] = []
    failures: list[str] = []
    for spec in specs:
        dest = ws_dir / spec.name
        use_clone = clone or spec.clone
        mode = "clone" if use_clone else "worktree"
        if dest.exists():
            info(f"  {spec.name}: already present, skipping")
            repos_meta.append(
                prior.get(spec.name)
                or {"name": spec.name, "url": spec.url,
                    "base": base or spec.base or "?", "mode": mode}
            )
            continue
        try:
            if use_clone:
                used_base = clone_repo(spec, dest, branch, base)
            else:
                used_base = add_worktree(spec, dest, branch, base)
            ok(f"  {spec.name}: branch '{branch}' from origin/{used_base} ({mode})")
            init_submodules(spec, dest, submodules)
            repos_meta.append(
                {"name": spec.name, "url": spec.url, "base": used_base, "mode": mode}
            )
        except Fail as exc:
            failures.append(spec.name)
            err(f"  {spec.name}: FAILED\n{exc}")

    # Keep repos that were added ad-hoc (agentws add) but aren't in the profile.
    covered = {r["name"] for r in repos_meta}
    repos_meta.extend(r for n, r in prior.items() if n not in covered)

    if repos_meta:
        write_workspace_meta(
            ws_dir, name, branch, profile, description, repos_meta, project
        )
        write_claude_md(
            ws_dir, name, branch, profile, description, repos_meta,
            project_context(cfg, project),
        )

    if failures:
        err(
            f"\n{len(failures)} repo(s) failed: {', '.join(failures)}. "
            "Fix the cause and re-run the same create command; existing repos are skipped."
        )
        raise typer.Exit(1)
    ok(f"\nWorkspace ready: cd {ws_dir}")


@app.command()
def add(
    repos: list[str] = typer.Argument(
        ..., help="Repo names (org-expanded) or full git URLs."
    ),
    workspace: str = typer.Option(
        None, "-w", "--workspace",
        help="Workspace to add to (default: the workspace containing the cwd).",
    ),
    clone: bool = typer.Option(
        False, "--clone", help="Full-clone instead of a shared worktree."
    ),
    base: str = typer.Option(
        None, "--base",
        help="Base branch for the new branch (default: the repo's default branch).",
    ),
    submodules: bool = typer.Option(
        True, "--submodules/--no-submodules",
        help="Initialize submodules recursively after checkout.",
    ),
) -> None:
    """Add repo(s) to an existing workspace, on its branch (profile unchanged).

    To add a repo to every future workspace instead, put it in the profile
    and re-run the original create command.
    """
    cfg = load_config()
    if workspace:
        ws_dir = workspace_root(cfg) / workspace
    else:
        ws_dir = find_enclosing_workspace()
    meta = load_workspace_meta(ws_dir)
    branch = meta["branch"]
    org = cfg.get("defaults", {}).get("org", "")

    repos_meta = [dict(r) for r in meta.get("repos", [])]
    known = {r["name"] for r in repos_meta}
    failures: list[str] = []
    for arg in repos:
        spec = repo_arg_to_spec(arg, org, clone, base)
        dest = ws_dir / spec.name
        mode = "clone" if spec.clone else "worktree"
        if dest.exists():
            info(f"  {spec.name}: already present, skipping")
            if spec.name not in known:
                repos_meta.append(
                    {"name": spec.name, "url": spec.url,
                     "base": base or "?", "mode": mode}
                )
            continue
        try:
            if spec.clone:
                used_base = clone_repo(spec, dest, branch, base)
            else:
                used_base = add_worktree(spec, dest, branch, base)
            ok(f"  {spec.name}: branch '{branch}' from origin/{used_base} ({mode})")
            init_submodules(spec, dest, submodules)
            repos_meta.append(
                {"name": spec.name, "url": spec.url, "base": used_base, "mode": mode}
            )
        except Fail as exc:
            failures.append(spec.name)
            err(f"  {spec.name}: FAILED\n{exc}")

    project = meta.get("project")
    write_workspace_meta(
        ws_dir, meta["name"], branch, meta["profile"],
        meta.get("description", ""), repos_meta, project,
    )
    write_claude_md(
        ws_dir, meta["name"], branch, meta["profile"],
        meta.get("description", ""), repos_meta,
        project_context(cfg, project),
    )
    if failures:
        err(f"\n{len(failures)} repo(s) failed: {', '.join(failures)}.")
        raise typer.Exit(1)


@app.command("list")
def list_workspaces() -> None:
    """List workspaces (only those created by agentws)."""
    cfg = load_config()
    root = workspace_root(cfg)
    if not root.exists():
        info(f"No workspaces (workspace root {root} does not exist).")
        return
    found = False
    for ws_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        if not (ws_dir / WS_META).exists():
            continue
        found = True
        meta = load_workspace_meta(ws_dir)
        states = [
            repo_state(ws_dir / r["name"])
            for r in meta.get("repos", [])
            if (ws_dir / r["name"]).exists()
        ]
        dirty = sum(1 for s in states if s.dirty)
        unpushed = sum(1 for s in states if s.unpushed)
        flags = []
        if dirty:
            flags.append(f"{dirty} dirty")
        if unpushed:
            flags.append(f"{unpushed} unpushed")
        flag_str = f"  [{', '.join(flags)}]" if flags else "  [clean]"
        proj = f" project={meta['project']}" if meta.get("project") else ""
        info(
            f"{meta['name']:<24} profile={meta['profile']:<16} "
            f"{len(states)} repos{flag_str}{proj}"
        )
    if not found:
        info(f"No agentws workspaces under {root}.")


@app.command()
def status(
    name: str = typer.Argument(
        None, help="Workspace name (default: the workspace containing the cwd)."
    ),
) -> None:
    """Per-repo detail for one workspace."""
    if name:
        cfg = load_config()
        ws_dir = workspace_root(cfg) / name
    else:
        ws_dir = find_enclosing_workspace()
        name = ws_dir.name
    meta = load_workspace_meta(ws_dir)
    desc = meta.get("description", "")
    proj = f", project={meta['project']}" if meta.get("project") else ""
    info(f"Workspace {name} (profile={meta['profile']}{proj})"
         + (f": {desc}" if desc else ""))
    for r in meta.get("repos", []):
        repo_dir = ws_dir / r["name"]
        if not repo_dir.exists():
            warn(f"  {r['name']:<28} MISSING (re-run create to restore)")
            continue
        s = repo_state(repo_dir)
        parts = [f"branch={s.branch}"]
        parts.append(f"{s.dirty} dirty" if s.dirty else "clean")
        if s.unpushed:
            parts.append(f"{s.unpushed} unpushed commit(s)")
        elif not s.has_upstream:
            parts.append("not pushed yet (no commits)")
        else:
            parts.append("pushed")
        line = f"  {s.name:<28} {', '.join(parts)}"
        (warn if s.dirty or s.unpushed else info)(line)


@app.command()
def rm(
    name: str = typer.Argument(..., help="Workspace name."),
    force: bool = typer.Option(
        False, "--force", help="Remove even with uncommitted/unpushed work."
    ),
    delete_branches: bool = typer.Option(
        False, "--delete-branches",
        help="Also delete the workspace branch from the shared bare repos.",
    ),
) -> None:
    """Tear down a workspace (worktrees removed; branches kept unless asked)."""
    cfg = load_config()
    ws_dir = workspace_root(cfg) / name
    meta = load_workspace_meta(ws_dir)

    problems = []
    for r in meta.get("repos", []):
        repo_dir = ws_dir / r["name"]
        if not repo_dir.exists():
            continue
        s = repo_state(repo_dir)
        if s.dirty:
            problems.append(f"{s.name}: {s.dirty} uncommitted change(s)")
        if s.unpushed:
            problems.append(f"{s.name}: {s.unpushed} unpushed commit(s)")
    if problems and not force:
        err("Refusing to remove workspace with unsaved work:")
        for p in problems:
            err(f"  {p}")
        err("Push your work, or re-run with --force to discard it.")
        raise typer.Exit(1)

    branch = meta.get("branch", name)
    for r in meta.get("repos", []):
        repo_dir = ws_dir / r["name"]
        if r.get("mode") == "worktree" and repo_dir.exists():
            bare = Path(git("rev-parse", "--git-common-dir", cwd=repo_dir))
            git("worktree", "remove", "--force", str(repo_dir), cwd=bare)
            git("worktree", "prune", cwd=bare)
            if delete_branches:
                git("branch", "-D", branch, cwd=bare)
            info(f"  {r['name']}: worktree removed"
                 + (", branch deleted" if delete_branches else f" (branch '{branch}' kept)"))
        elif repo_dir.exists():
            info(f"  {r['name']}: clone deleted")
    shutil.rmtree(ws_dir)
    ok(f"Removed workspace {ws_dir}")


# ------------------------------------------------------- profile commands ---

@profile_app.command("list")
def profile_list() -> None:
    """List configured profiles."""
    cfg = load_config()
    profiles = cfg.get("profiles", {})
    if not profiles:
        info(f"No profiles defined. Add one with 'agentws profile add' "
             f"or edit {config_path()}")
        return
    for pname in sorted(profiles):
        p = profiles[pname]
        desc = p.get("description", "")
        info(f"{pname:<20} {len(p.get('repos', []))} repos"
             + (f"  — {desc}" if desc else ""))


@profile_app.command("show")
def profile_show(name: str = typer.Argument(...)) -> None:
    """Show a profile's repos."""
    cfg = load_config()
    specs = resolve_repos(cfg, name)
    p = cfg["profiles"][name]
    desc = p.get("description", "")
    info(f"[{name}]" + (f" {desc}" if desc else ""))
    for s in specs:
        extras = []
        if s.base:
            extras.append(f"base={s.base}")
        if s.clone:
            extras.append("clone")
        info(f"  {s.name:<28} {s.url}" + (f"  ({', '.join(extras)})" if extras else ""))


@profile_app.command("add")
def profile_add(
    name: str = typer.Argument(..., help="Profile name."),
    repos: list[str] = typer.Argument(..., help="Repo names (or full git URLs)."),
    description: str = typer.Option("", "-d", "--description"),
) -> None:
    """Add a new profile."""
    cfg = load_config()
    profiles = cfg.setdefault("profiles", tomlkit.table())
    if name in profiles:
        raise Fail(f"Profile '{name}' already exists. Edit {config_path()} to change it.")
    table = tomlkit.table()
    if description:
        table["description"] = description
    entries = tomlkit.array()
    entries.multiline(True)
    for r in repos:
        if r.startswith(("git@", "https://", "ssh://", "file://")) or "/" in r:
            item = tomlkit.inline_table()
            item["name"] = r.rstrip("/").split("/")[-1].removesuffix(".git")
            item["url"] = r
            entries.append(item)
        else:
            entries.append(r)
    table["repos"] = entries
    profiles[name] = table
    save_config(cfg)
    ok(f"Added profile '{name}' with {len(repos)} repos to {config_path()}")


@profile_app.command("rm")
def profile_rm(name: str = typer.Argument(...)) -> None:
    """Delete a profile (does not touch existing workspaces)."""
    cfg = load_config()
    profiles = cfg.get("profiles", {})
    if name not in profiles:
        raise Fail(f"No profile named '{name}'.")
    del profiles[name]
    save_config(cfg)
    ok(f"Removed profile '{name}'.")


# ------------------------------------------------------- project commands ---

@project_app.command("list")
def project_list() -> None:
    """List configured projects."""
    cfg = load_config()
    projects = cfg.get("projects", {})
    if not projects:
        info("No projects defined. Add one with 'agentws project add'.")
        return
    for pname in sorted(projects):
        p = projects[pname]
        parts = []
        if p.get("profile"):
            parts.append(f"profile={p['profile']}")
        if p.get("doc"):
            parts.append(f"doc={p['doc']}")
        desc = f"  — {p['description']}" if p.get("description") else ""
        info(f"{pname:<20} {', '.join(parts)}{desc}")


@project_app.command("show")
def project_show(name: str = typer.Argument(...)) -> None:
    """Show a project's configuration and document status."""
    cfg = load_config()
    projects = cfg.get("projects", {})
    if name not in projects:
        raise Fail(f"No project named '{name}'.")
    p = projects[name]
    info(f"[{name}]" + (f" {p['description']}" if p.get("description") else ""))
    if p.get("profile"):
        info(f"  default profile: {p['profile']}")
    doc = p.get("doc", "")
    if not doc:
        warn("  no document configured")
    elif doc.startswith(("http://", "https://")):
        info(f"  document: {doc}")
    else:
        path = Path(doc).expanduser()
        state = "" if path.exists() else "  (MISSING)"
        info(f"  document: {path}{state}")


@project_app.command("add")
def project_add(
    name: str = typer.Argument(..., help="Project name."),
    doc: str = typer.Option(
        None, "--doc",
        help="Path or URL of the living project document. Default: a skeleton "
             "created under the agentws home projects/ directory.",
    ),
    profile: str = typer.Option(
        None, "--profile", help="Default profile for this project's workspaces."
    ),
    description: str = typer.Option("", "-d", "--description"),
) -> None:
    """Add a project (creates a skeleton doc if none is given)."""
    cfg = load_config()
    projects = cfg.setdefault("projects", tomlkit.table())
    if name in projects:
        raise Fail(f"Project '{name}' already exists. Edit {config_path()} to change it.")
    if profile and profile not in cfg.get("profiles", {}):
        raise Fail(f"Unknown profile '{profile}'. Define it first (agentws profile add).")

    doc = doc or str(project_docs_dir() / f"{name}.md")
    if not doc.startswith(("http://", "https://")):
        path = Path(doc).expanduser()
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                PROJECT_DOC_SKELETON.format(name=name, description=description)
            )
            info(f"Created project document skeleton at {path}")

    table = tomlkit.table()
    if description:
        table["description"] = description
    table["doc"] = doc
    if profile:
        table["profile"] = profile
    projects[name] = table
    save_config(cfg)
    ok(f"Added project '{name}' to {config_path()}")


@project_app.command("rm")
def project_rm(name: str = typer.Argument(...)) -> None:
    """Remove a project from config (its document is left in place)."""
    cfg = load_config()
    projects = cfg.get("projects", {})
    if name not in projects:
        raise Fail(f"No project named '{name}'.")
    doc = projects[name].get("doc", "")
    del projects[name]
    save_config(cfg)
    ok(f"Removed project '{name}'." + (f" Document kept: {doc}" if doc else ""))


def main() -> None:
    try:
        app()
    except Fail as exc:
        err(str(exc))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
