"""Workspace isolation and merge port for concurrent Turn workers."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import shutil
import subprocess
from pathlib import Path
import uuid


class WorkspaceError(RuntimeError):
    """A requested isolated workspace could not be allocated or merged."""


@dataclass
class WorkspaceManager:
    """Create durable Git worktrees outside the project's control root.

    ``data_dir`` is the server-owned location for production worktrees.  The
    no-argument form is retained for small direct unit tests and resolves to
    the historical project-local location; Runner always supplies its data
    directory.
    """

    data_dir: str | Path | None = None
    _merge_locks: dict[str, asyncio.Lock] = field(default_factory=dict, init=False, repr=False)

    def _worktree_root(
        self,
        project_root: Path,
        project_id: uuid.UUID | str | None,
    ) -> Path:
        if self.data_dir is None:
            return project_root / ".turn" / "worktrees"
        project_key = str(project_id or project_root.name).replace("/", "-")
        return Path(self.data_dir).expanduser().resolve() / "worktrees" / project_key

    def target(
        self,
        project_root: str | Path,
        node_id: uuid.UUID,
        project_id: uuid.UUID | str | None = None,
    ) -> Path:
        root = Path(project_root).expanduser().resolve()
        return self._worktree_root(root, project_id) / str(node_id)

    @staticmethod
    def branch_name(
        project_root: str | Path,
        node_id: uuid.UUID,
        project_id: uuid.UUID | str | None = None,
    ) -> str:
        project_key = str(project_id or Path(project_root).name).replace("/", "-")
        return f"turn/{project_key[:24]}/{str(node_id)[:12]}"

    async def isolation_available(self, project_root: str | Path) -> bool:
        """Return whether a clean source tree can safely spawn a worktree.

        Turn's own `.turn` control records are deliberately ignored here: they
        belong to the canonical control plane and should not make every Git
        project permanently fall back to serial execution. User source edits,
        untracked files, conflicts, and other Git dirt do serialize mutation.
        """
        root = Path(project_root).expanduser().resolve()
        if not (root / ".git").exists():
            return False
        result = await asyncio.to_thread(
            subprocess.run,
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=str(root),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return False
        for line in result.stdout.splitlines():
            path = line[3:].strip()
            if " -> " in path:
                path = path.split(" -> ", 1)[1].strip()
            path = path.strip('"').replace("\\", "/")
            if path == ".turn" or path.startswith(".turn/"):
                continue
            return False
        return True

    async def ensure(
        self,
        project_root: str | Path,
        node_id: uuid.UUID,
        project_id: uuid.UUID | str | None = None,
    ) -> str:
        root = Path(project_root).expanduser().resolve()
        if not (root / ".git").exists():
            raise WorkspaceError(f"project root is not a Git repository: {root}")
        target = self.target(root, node_id, project_id)
        if target.exists():
            return str(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        branch = self.branch_name(root, node_id, project_id)
        branch_exists = await asyncio.to_thread(
            subprocess.run,
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=str(root),
            capture_output=True,
        )
        if branch_exists.returncode == 0:
            await self._git(root, ["worktree", "add", str(target), branch])
        else:
            await self._git(root, ["worktree", "add", "-b", branch, str(target), "HEAD"])
        # Capabilities and interactive protocol files are runtime state, not
        # source files. Copy only the project-local capability deployment; the
        # state authority remains the server's canonical root store.
        source_capabilities = root / ".turn" / "capabilities"
        if source_capabilities.is_dir():
            shutil.copytree(
                source_capabilities,
                target / ".turn" / "capabilities",
                dirs_exist_ok=True,
            )
        return str(target)

    async def commit(self, workspace: str | Path, node_id: uuid.UUID) -> str | None:
        path = Path(workspace).expanduser().resolve()
        await self._git(path, ["add", "-A"])
        check = await asyncio.to_thread(
            subprocess.run,
            ["git", "diff", "--cached", "--quiet"],
            cwd=str(path),
            capture_output=True,
            text=True,
        )
        if check.returncode == 0:
            return None
        await self._git(path, ["commit", "-m", f"Turn worker {node_id}"])
        result = await asyncio.to_thread(
            subprocess.run,
            ["git", "rev-parse", "HEAD"],
            cwd=str(path),
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    async def merge(self, project_root: str | Path, commit: str, node_id: uuid.UUID) -> None:
        root = Path(project_root).expanduser().resolve()
        lock = self._merge_locks.setdefault(str(root), asyncio.Lock())
        # Canonical-branch merges are reserved for an explicitly accepted root
        # result. Serialize only this narrow Git index operation so independent
        # fan-out cannot race on .git/index.lock or interleave merge state.
        async with lock:
            await self._merge_with_abort(root, commit)

    async def merge_into_workspace(
        self,
        workspace: str | Path,
        commits: list[str] | tuple[str, ...],
        node_id: uuid.UUID,
    ) -> None:
        """Bring predecessor outputs into a worker's isolated worktree.

        Worker branches are deliberately not merged into the canonical branch
        as they finish. A downstream worker, especially a fan-in integrator,
        owns the merge in its own worktree and can resolve conflicts without
        mutating the user's checkout.
        """
        path = Path(workspace).expanduser().resolve()
        unique = list(dict.fromkeys(commit for commit in commits if commit))
        for commit in unique:
            if await self._is_ancestor(path, commit):
                continue
            await self._merge_with_abort(path, commit)

    async def _merge_with_abort(self, cwd: Path, commit: str) -> None:
        try:
            await self._git(cwd, ["merge", "--no-ff", "--no-edit", commit])
        except WorkspaceError:
            await self._abort_merge(cwd)
            raise

    async def _is_ancestor(self, cwd: Path, commit: str) -> bool:
        result = await asyncio.to_thread(
            subprocess.run,
            ["git", "merge-base", "--is-ancestor", commit, "HEAD"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        detail = (result.stderr or result.stdout).strip()
        raise WorkspaceError(f"git merge-base --is-ancestor failed: {detail}")

    async def _abort_merge(self, cwd: Path) -> None:
        result = await asyncio.to_thread(
            subprocess.run,
            ["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return
        await asyncio.to_thread(
            subprocess.run,
            ["git", "merge", "--abort"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
        )

    async def remove(
        self,
        project_root: str | Path,
        node_id: uuid.UUID,
        project_id: uuid.UUID | str | None = None,
    ) -> None:
        root = Path(project_root).expanduser().resolve()
        target = self.target(root, node_id, project_id)
        if not target.exists():
            return
        await self._git(root, ["worktree", "remove", "--force", str(target)])

    @staticmethod
    async def _git(cwd: Path, args: list[str]) -> None:
        result = await asyncio.to_thread(
            subprocess.run,
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise WorkspaceError(f"git {' '.join(args)} failed: {detail}")
