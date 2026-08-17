"""Harness capability and command knowledge.

Workers and planners depend on this catalog instead of embedding provider
flags in orchestration code. The catalog is deliberately explicit: an unknown
harness is an error, never an implicit substitution.
"""
from __future__ import annotations

from dataclasses import dataclass

from turn.domain.schemas import AgentConfig, HarnessKind
from turn.mcp.runtime import codex_mcp_overrides


@dataclass(frozen=True)
class HarnessDefinition:
    id: str
    label: str
    binary: str
    reasoning: tuple[str, ...]
    # Capabilities supplied by the harness itself, independent of procured
    # MCPs. This is a small declared profile today; runtime discovery can
    # replace it later without changing planner procurement semantics.
    capabilities: tuple[str, ...] = ()
    supports_sessions: bool = True
    supports_tools: bool = True


class HarnessCatalog:
    def __init__(self, definitions: tuple[HarnessDefinition, ...]):
        self._definitions = {definition.id: definition for definition in definitions}

    def definition(self, harness: str | HarnessKind) -> HarnessDefinition:
        key = harness.value if isinstance(harness, HarnessKind) else str(harness)
        try:
            return self._definitions[key]
        except KeyError as error:
            raise ValueError(f"unsupported harness: {key}") from error

    def as_dict(self) -> dict[str, dict[str, object]]:
        return {
            definition.id: {
                "label": definition.label,
                "binary": definition.binary,
                "reasoning": list(definition.reasoning),
                "capabilities": list(definition.capabilities),
                "supports_sessions": definition.supports_sessions,
                "supports_tools": definition.supports_tools,
            }
            for definition in self._definitions.values()
        }


REAL_HARNESS_CATALOG = HarnessCatalog(
    (
        HarnessDefinition(
            "codex", "Codex", "codex", ("default", "low", "medium", "high", "xhigh", "max"),
            capabilities=("browser", "computer-use"),
        ),
        HarnessDefinition(
            "claude", "Claude Code", "claude", ("default", "low", "medium", "high", "xhigh", "max"),
            capabilities=("browser", "computer-use"),
        ),
        HarnessDefinition("opencode", "OpenCode", "opencode", ("default", "low", "medium", "high", "max")),
        HarnessDefinition("pi", "Pi", "pi", ("default", "low", "medium", "high", "xhigh", "max")),
    )
)

# Provider discovery belongs to the same catalog as provider commands. The
# server may expose discovered models, but it never invents model IDs when a
# selected provider cannot report them.
MODEL_DISCOVERY_COMMANDS = {
    "opencode": ["opencode", "models"],
    "pi": ["pi", "--offline", "--list-models"],
}

class HarnessCommandFactory:
    """Build provider commands for workers, planners, and reconnects."""

    def __init__(self, *, codex_binary: str = "codex"):
        self.codex_binary = codex_binary

    def worker_command(
        self,
        harness: HarnessKind,
        agent: AgentConfig,
        prompt: str,
        cwd: str,
        *,
        resume: bool = False,
        native: bool = False,
        prompt_via_stdin: bool = False,
        mcp_config: str | None = None,
    ) -> list[str]:
        model = agent.model
        reasoning = agent.reasoning.value
        session = agent.session_id
        if harness == HarnessKind.CLAUDE:
            cmd = ["claude"] if native else ["claude", "-p", "--output-format", "stream-json", "--verbose"]
            if mcp_config:
                cmd += ["--mcp-config", mcp_config]
            if session:
                cmd += ["--resume", session] if resume else ["--session-id", session]
            elif not native:
                raise ValueError("Claude requires a session id")
            if model:
                cmd += ["--model", model]
            if reasoning != "default":
                cmd += ["--effort", reasoning]
            if agent.tools:
                cmd += ["--allowedTools", *agent.tools]
            # Native Claude starts its interactive session immediately with
            # the positional prompt.  The print adapter keeps stdin support
            # for machine-readable transports.
            return [*cmd, prompt] if native else [*cmd, "-" if prompt_via_stdin else prompt]
        if harness == HarnessKind.OPENCODE:
            cmd = ["opencode", cwd] if native else ["opencode", "run", "--format", "json", "--dir", cwd]
            if session:
                cmd += ["--session", session]
            if model:
                cmd += ["--model", model]
            if reasoning != "default":
                cmd += ["--variant", reasoning]
            # OpenCode's interactive TUI owns the initial prompt through its
            # provider-native --prompt option; do not type into its composer
            # after startup.
            return [*cmd, "--prompt", prompt] if native else [*cmd, "-" if prompt_via_stdin else prompt]
        if harness == HarnessKind.PI:
            cmd = ["pi"] if native else ["pi", "-p", "--mode", "json"]
            if session:
                cmd += ["--session" if resume else "--session-id", session]
            if model:
                cmd += ["--model", model]
            if reasoning != "default":
                cmd += ["--thinking", reasoning]
            if agent.tools:
                cmd += ["--tools", ",".join(agent.tools)]
            # Pi accepts one or more initial messages as positional args and
            # keeps the interactive session alive after processing them.
            return [*cmd, prompt] if native else [*cmd, "-" if prompt_via_stdin else prompt]
        raise ValueError(f"unsupported generic harness: {harness}")

    def planner_command(
        self, agent: AgentConfig, prompt: str, *, cwd: str, native: bool, resume: bool,
        prompt_via_stdin: bool = False,
        mcp_config: str | None = None,
    ) -> list[str]:
        if agent.harness == HarnessKind.OPENCODE:
            cmd = ["opencode", cwd] if native else ["opencode", "run"]
            if agent.session_id:
                cmd += ["--session", agent.session_id]
            if model := agent.model:
                cmd += ["--model", model]
            if agent.reasoning.value != "default":
                cmd += ["--variant", agent.reasoning.value]
            return [*cmd, "--prompt", prompt] if native else [*cmd, "-" if prompt_via_stdin else prompt]
        if agent.harness == HarnessKind.PI:
            cmd = ["pi"] if native else ["pi", "--print", "--mode", "text"]
            if agent.session_id:
                cmd += ["--session" if native and resume else "--session-id", agent.session_id]
            if model := agent.model:
                cmd += ["--model", model]
            if agent.reasoning.value != "default":
                cmd += ["--thinking", agent.reasoning.value]
            return [*cmd, prompt] if native else [*cmd, "-" if prompt_via_stdin else prompt]
        if agent.harness == HarnessKind.CLAUDE:
            cmd = ["claude"] if native else ["claude", "--print", "--output-format", "text"]
            if mcp_config:
                cmd += ["--mcp-config", mcp_config]
            if agent.session_id:
                cmd += ["--resume", agent.session_id] if resume else ["--session-id", agent.session_id]
            if model := agent.model:
                cmd += ["--model", model]
            if agent.reasoning.value != "default":
                cmd += ["--effort", agent.reasoning.value]
            return [*cmd, prompt] if native else [*cmd, "-" if prompt_via_stdin else prompt]
        raise ValueError(f"planner harness '{agent.harness.value}' is unsupported")

    def reconnect_command(
        self,
        agent: AgentConfig,
        cwd: str,
        session_id: str,
        *,
        prompt: str | None = None,
        mcp_config: str | None = None,
    ) -> list[str] | None:
        mcp_flags = [
            item
            for override in codex_mcp_overrides(agent)
            for item in ("-c", override)
        ]
        if agent.harness == HarnessKind.CODEX:
            model = ["--model", agent.model] if agent.model else []
            thinking = (["-c", f'model_reasoning_effort="{agent.reasoning.value}"']
                        if agent.reasoning.value != "default" else [])
            command = [self.codex_binary, "resume", *model, *thinking, *mcp_flags,
                       "--no-alt-screen", "-C", cwd, session_id]
            return [*command, prompt] if prompt is not None else command
        if agent.harness == HarnessKind.PI:
            command = ["pi", "--session", session_id, *(["--model", agent.model] if agent.model else [])]
            return [*command, prompt] if prompt is not None else command
        if agent.harness == HarnessKind.OPENCODE:
            command = (
                ["opencode", cwd, "--session", session_id]
                if prompt is not None
                else ["opencode", "--session", session_id, cwd]
            )
            if agent.model:
                command += ["--model", agent.model]
            if prompt is not None:
                command += ["--prompt", prompt]
            return command
        if agent.harness == HarnessKind.CLAUDE:
            command = ["claude", "--resume", session_id]
            if mcp_config:
                command += ["--mcp-config", mcp_config]
            if agent.model:
                command += ["--model", agent.model]
            return [*command, prompt] if prompt is not None else command
        return None

    def conversation_delete_command(
        self, harness: HarnessKind, session_id: str
    ) -> list[str] | None:
        """Build the provider-supported command for deleting one conversation.

        This is intentionally separate from worker/reconnect commands. A
        project deletion must use the harness's public session-management
        surface; it must never infer or edit a provider's private storage.
        ``None`` means that the installed harness has no supported
        non-interactive operation for this lifecycle action.
        """
        if harness == HarnessKind.CODEX:
            return [self.codex_binary, "delete", session_id, "--force"]
        if harness == HarnessKind.OPENCODE:
            return ["opencode", "session", "delete", session_id]
        return None

    def conversation_archive_command(
        self, harness: HarnessKind, session_id: str
    ) -> list[str] | None:
        """Build a supported archive command used when deletion is unavailable."""
        if harness == HarnessKind.CODEX:
            return [self.codex_binary, "archive", session_id]
        return None
