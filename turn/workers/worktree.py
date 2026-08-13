"""Git worktree isolation + merge-up for Turn nodes.

Every node runs in its own git worktree. A node's worktree is branched from its
*parent's* worktree, so it starts with everything the parent (and earlier
siblings) have already produced. When a node completes, its worktree is merged
back up into the parent's worktree. The net effect: by the time a downstream
node runs, all of its prerequisites' real files are already present on disk in
its worktree -- so a composer/assembler reads and integrates existing files
instead of regenerating them from context.

This module is pure git + config; it has no Codex or store dependency so both
the worker and the runner can use it. All functions accept an explicit
``repo_path`` (defaulting to the global config) so they work with a worker's
per-instance repository configuration and in tests.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from turn.config import settings

logger = logging.getLogger("turn.worktree")

WT_ROOT = ".turn/worktrees"


def _repo(repo_path: str | None = None) -> Path:
    return Path(repo_path or settings.repo_path)


def worktree_path(node_id, repo_path: str | None = None) -> Path:
    return _repo(repo_path) / WT_ROOT / node_id.hex


def branch_name(node_id) -> str:
    return f"turn-{node_id.hex[:8]}"


def _git(args, cwd=None):
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, cwd=cwd
    )


def _is_git(repo: Path) -> bool:
    return (repo / ".git").exists()


def _ensure_gitignore(repo: Path) -> None:
    """Make sure the scratch worktree tree (.turn/) is never committed into a
    project's deliverable history."""
    gi = repo / ".gitignore"
    need = ".turn/"
    if gi.exists():
        lines = gi.read_text().splitlines()
        if need not in lines:
            with gi.open("a") as f:
                f.write("\n# Turn scratch worktrees\n" + need + "\n")
    else:
        gi.write_text("# Turn scratch worktrees\n" + need + "\n")


def init_project_repo(
    root_id,
    working_dir: str | None = None,
    open_existing: bool = False,
    projects_dir: str | None = None,
) -> str:
    """Create (or open) the per-project git repository that accumulates this
    project's work, and return its absolute path (the project repo root).

    The directory the user picks becomes a real, initialized git repo. By the
    time the project finishes, the repo holds the finished files plus a merge
    log, so the user can keep it, open it in an editor, or delete it.

    Create mode
    ----------
    * ``working_dir`` defaults to ``<projects_dir>/<slug>``.
    * The directory is created if needed.
    * If it is not yet a git repo: ``git init`` + an empty initial commit +
      a ``.gitignore`` that ignores ``.turn/`` (the scratch worktree tree).
    * An existing git repo at the path is reused (behaves like open).

    Open mode (refactoring an existing repo)
    ----------------------------------------
    * ``working_dir`` MUST be an existing git repo. We branch off its current
      HEAD so all of Turn's work lands on a side branch and is only merged back
      when the project is shipped.

    In both modes we create a working branch ``turn-<roothex8>`` off the repo's
    base branch and check the repo root out onto it. All work merges up onto
    that branch; :func:`ship_project` merges it into the base branch at the end.
    """
    root_hex = root_id.hex[:8]
    work_branch = f"turn-{root_hex}"
    if working_dir:
        repo = Path(working_dir).resolve()
    else:
        base = Path(projects_dir or "./projects").resolve()
        repo = base / f"proj-{root_hex}"
    repo.mkdir(parents=True, exist_ok=True)

    if open_existing and not _is_git(repo):
        raise ValueError(f"Open mode requires an existing git repo: {repo}")

    if not _is_git(repo):
        r = _git(["init"], cwd=str(repo))
        if r.returncode != 0:
            raise RuntimeError("git init failed: " + r.stderr)

    # Never let the scratch worktree tree pollute project commits. Write it
    # BEFORE the initial commit so it is tracked from the very first tree.
    _ensure_gitignore(repo)

    # Ensure at least one commit exists so we can branch off HEAD. The initial
    # commit carries the .gitignore so the project repo starts clean.
    if _git(["rev-parse", "HEAD"], cwd=str(repo)).returncode != 0:
        _git(["add", "-A"], cwd=str(repo))
        _git(["commit", "-qm", "turn: project initialized"], cwd=str(repo))

    # Capture the base branch so ship_project can merge back into it.
    base_branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=str(repo)).stdout.strip() or "main"
    _git(["config", "turn.baseBranch", base_branch], cwd=str(repo))

    # Create + check out the working branch for this project.
    if not _branch_exists(work_branch, repo_path=str(repo)):
        _git(["branch", work_branch], cwd=str(repo))
    _git(["checkout", work_branch], cwd=str(repo))

    return str(repo)


def ship_project(root_id, repo_path: str | None = None) -> None:
    """Merge the project's accumulated working branch into its base branch and
    check the repo root out onto the base branch, leaving the user with a real,
    initialized git repo of their finished work. Idempotent."""
    if not repo_path:
        return
    repo = _repo(repo_path)
    if not _is_git(repo):
        return
    work_branch = branch_name(root_id)
    base_branch = _git(["config", "turn.baseBranch"], cwd=str(repo)).stdout.strip()
    if not base_branch:
        base_branch = _default_branch(repo_path)
    if not _branch_exists(base_branch, repo_path):
        if _branch_exists(work_branch, repo_path):
            _git(["branch", base_branch, work_branch], cwd=str(repo))
        else:
            return
    if _branch_exists(work_branch, repo_path) and _branch_exists(base_branch, repo_path):
        # Already shipped: nothing to do but land back on the base branch.
        if _git(["merge-base", "--is-ancestor", work_branch, base_branch], cwd=str(repo)).returncode == 0:
            _git(["checkout", base_branch], cwd=str(repo))
            return
        _git(["checkout", base_branch], cwd=str(repo))
        r = _git(
            ["merge", work_branch, "--no-ff", "-m", f"turn: ship project {root_id.hex[:8]}"],
            cwd=str(repo),
        )
        if r.returncode != 0:
            logger.warning("ship merge failed for %s: %s", root_id.hex, r.stderr)
            _git(["merge", "--abort"], cwd=str(repo))
            shutil.rmtree(str(worktree_path(root_id, repo_path)), ignore_errors=True)
    else:
        _git(["checkout", base_branch], cwd=str(repo))


def _default_branch(repo_path: str | None = None) -> str:
    r = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=str(_repo(repo_path)))
    return r.stdout.strip() or "main"


def _branch_exists(branch: str, repo_path: str | None = None) -> bool:
    r = _git(
        ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=str(_repo(repo_path)),
    )
    return r.returncode == 0


def get_or_create_worktree(
    node_id,
    parent_id,
    force: bool = False,
    resolve_parent=None,
    repo_path: str | None = None,
) -> str | None:
    """Create (or return) a node's isolated worktree.

    The worktree is branched from ``parent_id``'s branch so it inherits all
    files already merged up into the parent. The root node (parent_id is None)
    is branched from the default branch. ``force`` removes any prior worktree
    first so re-runs start clean from the parent's current state.

    ``resolve_parent`` (optional parent_id -> parent_parent_id) lets us lazily
    materialise an ancestor chain when the immediate parent's worktree does not
    yet exist.
    """
    if not (repo_path or settings.repo_path):
        return None
    repo = _repo(repo_path)

    # --- Root node ---------------------------------------------------
    # The root node's worktree IS the project repo root itself (not a sub-
    # worktree under .turn/worktrees). So the finished files accumulate in the
    # directory the user picked/created, and they are left with a real repo.
    # We just ensure the project's working branch exists and is checked out.
    if parent_id is None:
        if not _is_git(repo):
            return None
        work_branch = branch_name(node_id)
        if not _branch_exists(work_branch, repo_path):
            base = (
                _git(["config", "turn.baseBranch"], cwd=str(repo)).stdout.strip()
                or _default_branch(repo_path)
            )
            _git(["checkout", base], cwd=str(repo))
            _git(["branch", work_branch], cwd=str(repo))
        # Only switch the repo root onto the working branch when the tree is
        # clean, so we never clobber manual edits the user has made in the repo.
        if _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=str(repo)).stdout.strip() != work_branch:
            if _git(["status", "--porcelain"], cwd=str(repo)).stdout.strip() == "":
                _git(["checkout", work_branch], cwd=str(repo))
        return str(repo)

    # --- Non-root node: a normal isolated worktree -------------------
    wt = worktree_path(node_id, repo_path)
    branch = branch_name(node_id)

    if wt.exists() and not force:
        return str(wt)

    # Clean up any prior worktree/branch for this node so re-runs isolate
    # cleanly instead of failing and falling back to the main repo.
    _git(["worktree", "remove", "--force", str(wt)], cwd=str(repo))
    _git(["branch", "-D", branch], cwd=str(repo))

    if parent_id is not None:
        base = branch_name(parent_id)
        # Ensure the parent's worktree/branch exists. In normal flow it already
        # does (created when the parent was set up); this is a defensive backstop.
        if not _branch_exists(base, repo_path):
            grandparent = resolve_parent(parent_id) if resolve_parent else None
            if grandparent is not None:
                get_or_create_worktree(
                    parent_id, grandparent, repo_path=repo_path, resolve_parent=resolve_parent
                )
            else:
                # Parent has no parent -> it is the project root: branch from default.
                get_or_create_worktree(parent_id, None, repo_path=repo_path)
    else:
        base = _default_branch(repo_path)

    try:
        r = _git(["worktree", "add", str(wt), "-b", branch, base], cwd=str(repo))
        if r.returncode != 0:
            logger.warning("worktree add failed for %s: %s", node_id.hex, r.stderr)
            return None
        return str(wt)
    except (subprocess.CalledProcessError, OSError) as e:  # pragma: no cover
        logger.warning("worktree add error for %s: %s", node_id.hex, e)
        # Never return the main repo: callers must refuse to run Codex here.
        return None


def commit_worktree(node_id, repo_path: str | None = None) -> None:
    """Commit any work the node produced so it can be merged up."""
    wt = worktree_path(node_id, repo_path)
    if not wt.exists():
        return
    _git(["add", "-A"], cwd=str(wt))
    r = _git(["diff", "--cached", "--quiet"], cwd=str(wt))
    if r.returncode != 0:  # there are staged changes -> commit them
        _git(["commit", "-m", f"turn: work for {node_id.hex}"], cwd=str(wt))


def merge_into_parent(node_id, parent_id, repo_path: str | None = None) -> None:
    """Merge a completed node's worktree up into its parent's worktree.

    On conflict we prefer the child's content (-X theirs); Turn plans are
    supposed to keep siblings from touching the same file, but this keeps a
    stray overlap from blocking the whole graph. The root node (parent_id is
    None) is the accumulation target and has nothing to merge into.
    """
    if parent_id is None:
        # Root is the final accumulation point; nothing to merge upward.
        return
    # A nested parent already owns an isolated worktree created when it was
    # planned. Use that exact workspace. Only a top-level child has the project
    # root as its parent (for which no .turn/worktrees/<root> directory exists)
    # and should fall back to the repository root.
    nested_parent = worktree_path(parent_id, repo_path)
    parent_wt = (
        str(nested_parent)
        if nested_parent.exists()
        else get_or_create_worktree(parent_id, None, repo_path=repo_path)
    )
    if parent_wt is None:
        logger.warning("cannot merge %s: parent worktree unavailable", node_id.hex)
        return
    commit_worktree(node_id, repo_path)
    child_branch = branch_name(node_id)
    r = _git(
        ["merge", child_branch, "-X", "theirs", "--no-ff", "-m", f"turn: merge {node_id.hex}"],
        cwd=parent_wt,
    )
    if r.returncode != 0:
        # Abort a conflicted merge and fall back to copying the files in.
        logger.warning("merge conflict for %s into parent; copying files", node_id.hex)
        _git(["merge", "--abort"], cwd=parent_wt)
        _copy_files(worktree_path(node_id, repo_path), Path(parent_wt))
        _git(["add", "-A"], cwd=parent_wt)
        _git(["commit", "-m", f"turn: merge (copy) {node_id.hex}"], cwd=parent_wt)


def _copy_files(src: Path, dst: Path) -> None:
    """Copy tracked/untracked project files from src into dst, skipping .git."""
    for item in src.iterdir():
        if item.name == ".git":
            continue
        target = dst / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)


def remove_worktree(node_id, repo_path: str | None = None) -> None:
    """Delete a single node's git worktree directory (its accumulated files
    have already been merged up into the parent, so this is safe). Idempotent:
    no-ops if the worktree is already gone. Never deletes the project repo root
    itself (a node whose worktree IS the repo root has no .turn/worktrees dir)."""
    wt = worktree_path(node_id, repo_path)
    repo = _repo(repo_path)
    # Safety: the root node's worktree is the project repo root -- never remove
    # the user's finished repository.
    if wt.resolve() == repo.resolve():
        return
    if wt.exists():
        _git(["worktree", "remove", "--force", str(wt)], cwd=str(repo))
        shutil.rmtree(str(wt), ignore_errors=True)


def remove_branches(ids, repo_path: str | None = None) -> None:
    """Delete the turn-* branches for the given node ids and prune dangling
    worktree metadata. Called after every worktree dir in a subtree is gone."""
    repo = str(_repo(repo_path))
    for nid in ids:
        b = branch_name(nid)
        if _branch_exists(b, repo_path):
            _git(["branch", "-D", b], cwd=repo)
    _git(["worktree", "prune"], cwd=repo)
