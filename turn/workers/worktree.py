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
    parent_wt = get_or_create_worktree(parent_id, None, repo_path=repo_path)
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
