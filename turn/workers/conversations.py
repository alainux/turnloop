"""Provider-owned conversation cleanup through public harness commands.

Turn persists conversation identifiers so it can reconnect to a node. It does
not own the transcripts behind those identifiers. This module therefore only
constructs and executes documented harness commands, one conversation at a
time, and reports every result to the caller.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import inspect
from pathlib import Path
from typing import Awaitable, Callable, Sequence
import uuid

from turn.domain.schemas import HarnessKind, Node, Run
from turn.workers.harness_catalog import HarnessCommandFactory


@dataclass(frozen=True)
class ConversationRef:
    harness: HarnessKind
    session_id: str
    node_id: uuid.UUID


@dataclass(frozen=True)
class ConversationProgress:
    completed: int
    total: int
    harness: str
    session_id: str
    status: str
    message: str


@dataclass(frozen=True)
class ConversationCleanup:
    total: int
    deleted: int
    archived: int
    failed: int
    unsupported: int
    errors: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.failed == 0 and self.unsupported == 0

    def as_dict(self) -> dict[str, object]:
        return {
            "total": self.total,
            "deleted": self.deleted,
            "archived": self.archived,
            "failed": self.failed,
            "unsupported": self.unsupported,
            "errors": list(self.errors),
        }


ProgressCallback = Callable[[ConversationProgress], Awaitable[None] | None]
CommandRunner = Callable[[Sequence[str], Path | None], Awaitable[tuple[int, str]]]


def conversation_refs(nodes: Sequence[Node], runs: Sequence[Run]) -> list[ConversationRef]:
    """Collect persisted provider sessions once, preserving discovery order.

    A run without a session id never established a provider conversation (for
    example, it may have been cancelled before launch), so it has nothing for
    the provider cleanup command to remove.
    """
    node_by_id = {node.id: node for node in nodes}
    refs: list[ConversationRef] = []
    seen: set[tuple[HarnessKind, str]] = set()

    def add(node: Node | None, session_id: str | None) -> None:
        if node is None or node.agent is None or not session_id:
            return
        key = (node.agent.harness, session_id)
        if key in seen:
            return
        seen.add(key)
        refs.append(ConversationRef(node.agent.harness, session_id, node.id))

    for node in nodes:
        add(node, node.agent.session_id if node.agent else None)
    for run in runs:
        add(node_by_id.get(run.node_id), run.session_id)
    return refs


async def _default_command_runner(
    command: Sequence[str], cwd: Path | None,
) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(cwd) if cwd is not None else None,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    return process.returncode or 0, output.decode(errors="replace").strip()


async def cleanup_conversations(
    refs: Sequence[ConversationRef],
    *,
    cwd: Path | None,
    commands: HarnessCommandFactory | None = None,
    on_progress: ProgressCallback | None = None,
    run_command: CommandRunner = _default_command_runner,
) -> ConversationCleanup:
    """Delete/archive each conversation using only its harness CLI.

    A failed delete is retried with the harness archive command only when that
    fallback exists. A missing fallback is reported as unsupported; no local
    transcript or provider database is inspected or modified by Turn.
    """
    factory = commands or HarnessCommandFactory()
    total = len(refs)
    deleted = archived = failed = unsupported = 0
    errors: list[str] = []

    async def report(index: int, ref: ConversationRef, status: str, message: str) -> None:
        if on_progress is not None:
            result = on_progress(ConversationProgress(
                index, total, ref.harness.value, ref.session_id, status, message,
            ))
            if inspect.isawaitable(result):
                await result

    for index, ref in enumerate(refs, start=1):
        delete_command = factory.conversation_delete_command(ref.harness, ref.session_id)
        archive_command = factory.conversation_archive_command(ref.harness, ref.session_id)
        if delete_command is None:
            if archive_command is None:
                unsupported += 1
                message = f"{ref.harness.value} does not support non-interactive conversation deletion"
                errors.append(f"{ref.harness.value}:{ref.session_id}: {message}")
                await report(index, ref, "unsupported", message)
                continue
            await report(index, ref, "archiving", f"Running {ref.harness.value} conversation archive command")
            try:
                archive_code, archive_output = await run_command(archive_command, cwd)
            except (OSError, asyncio.SubprocessError) as error:
                archive_code, archive_output = 1, str(error)
            if archive_code == 0:
                archived += 1
                await report(index, ref, "archived", "Conversation archived")
                continue
            failed += 1
            message = archive_output or f"archive command exited with status {archive_code}"
            errors.append(f"{ref.harness.value}:{ref.session_id}: {message}")
            await report(index, ref, "failed", message)
            continue

        await report(
            index,
            ref,
            "deleting",
            f"Running {ref.harness.value} conversation delete command",
        )

        try:
            returncode, output = await run_command(delete_command, cwd)
        except (OSError, asyncio.SubprocessError) as error:
            returncode, output = 1, str(error)
        if returncode == 0:
            deleted += 1
            await report(index, ref, "deleted", "Conversation deleted")
            continue

        if archive_command is not None:
            try:
                archive_code, archive_output = await run_command(archive_command, cwd)
            except (OSError, asyncio.SubprocessError) as error:
                archive_code, archive_output = 1, str(error)
            if archive_code == 0:
                archived += 1
                await report(index, ref, "archived", "Delete was unavailable; conversation archived")
                continue
            output = archive_output or output

        failed += 1
        message = output or f"command exited with status {returncode}"
        errors.append(f"{ref.harness.value}:{ref.session_id}: {message}")
        await report(index, ref, "failed", message)

    return ConversationCleanup(total, deleted, archived, failed, unsupported, tuple(errors))
