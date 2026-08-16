"""Harness capability and command knowledge.

Workers and planners depend on this catalog instead of embedding provider
flags in orchestration code. The catalog is deliberately explicit: an unknown
harness is an error, never an implicit substitution.
"""
from __future__ import annotations

from dataclasses import dataclass

from turn.domain.schemas import AgentConfig, HarnessKind


@dataclass(frozen=True)
class HarnessDefinition:
    id: str
    label: str
    binary: str
    reasoning: tuple[str, ...]
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
                "supports_sessions": definition.supports_sessions,
                "supports_tools": definition.supports_tools,
            }
            for definition in self._definitions.values()
        }


REAL_HARNESS_CATALOG = HarnessCatalog(
    (
        HarnessDefinition("codex", "Codex", "codex", ("default", "low", "medium", "high", "xhigh", "max")),
        HarnessDefinition("claude", "Claude Code", "claude", ("default", "low", "medium", "high", "xhigh", "max")),
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

    def __init__(self, *, codex_binary: str = "codex", codex_args: list[str] | None = None):
        self.codex_binary = codex_binary
        self.codex_args = list(codex_args or [])

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
    ) -> list[str]:
        model = agent.model
        reasoning = agent.reasoning.value
        session = agent.session_id
        if harness == HarnessKind.CLAUDE:
            cmd = ["claude"] if native else ["claude", "-p", "--output-format", "stream-json", "--verbose"]
            if session:
                cmd += ["--resume", session] if resume else ["--session-id", session]
            elif not native:
                raise ValueError("Claude requires a session id")
            if model:
                cmd += ["--model", model]
            if reasoning != "default":
                cmd += ["--effort", reasoning]
            if agent.permission.value == "full":
                cmd.append("--dangerously-skip-permissions")
            elif agent.permission.value == "workspace":
                cmd += ["--permission-mode", "acceptEdits"]
            if agent.tools:
                cmd += ["--allowedTools", *agent.tools]
            return cmd if native else [*cmd, "-" if prompt_via_stdin else prompt]
        if harness == HarnessKind.OPENCODE:
            cmd = ["opencode", cwd] if native else ["opencode", "run", "--format", "json", "--dir", cwd]
            if session:
                cmd += ["--session", session]
            if model:
                cmd += ["--model", model]
            if reasoning != "default":
                cmd += ["--variant", reasoning]
            if agent.permission.value != "ask":
                cmd.append("--auto")
            return cmd if native else [*cmd, "-" if prompt_via_stdin else prompt]
        if harness == HarnessKind.PI:
            cmd = ["pi"] if native else ["pi", "-p", "--mode", "json"]
            if session:
                cmd += ["--session" if resume else "--session-id", session]
            if model:
                cmd += ["--model", model]
            if reasoning != "default":
                cmd += ["--thinking", reasoning]
            if agent.permission.value != "ask":
                cmd.append("--approve")
            if agent.tools:
                cmd += ["--tools", ",".join(agent.tools)]
            return cmd if native else [*cmd, "-" if prompt_via_stdin else prompt]
        raise ValueError(f"unsupported generic harness: {harness}")

    def planner_command(
        self, agent: AgentConfig, prompt: str, *, cwd: str, native: bool, resume: bool,
        prompt_via_stdin: bool = False,
    ) -> list[str]:
        if agent.harness == HarnessKind.OPENCODE:
            cmd = ["opencode", cwd] if native else ["opencode", "run", "--auto"]
            if agent.session_id:
                cmd += ["--session", agent.session_id]
            if model := agent.model:
                cmd += ["--model", model]
            if agent.reasoning.value != "default":
                cmd += ["--variant", agent.reasoning.value]
            if native:
                cmd.append("--auto")
            return cmd if native else [*cmd, "-" if prompt_via_stdin else prompt]
        if agent.harness == HarnessKind.PI:
            cmd = ["pi"] if native else ["pi", "--print", "--mode", "text", "--approve"]
            if agent.session_id:
                cmd += ["--session" if native and resume else "--session-id", agent.session_id]
            if model := agent.model:
                cmd += ["--model", model]
            if agent.reasoning.value != "default":
                cmd += ["--thinking", agent.reasoning.value]
            if native:
                cmd.append("--approve")
            return cmd if native else [*cmd, "-" if prompt_via_stdin else prompt]
        if agent.harness == HarnessKind.CLAUDE:
            cmd = ["claude"] if native else ["claude", "--print", "--output-format", "text", "--permission-mode", "acceptEdits"]
            if agent.session_id:
                cmd += ["--resume", agent.session_id] if resume else ["--session-id", agent.session_id]
            if model := agent.model:
                cmd += ["--model", model]
            if agent.reasoning.value != "default":
                cmd += ["--effort", agent.reasoning.value]
            return cmd if native else [*cmd, "-" if prompt_via_stdin else prompt]
        raise ValueError(f"planner harness '{agent.harness.value}' is unsupported")

    def reconnect_command(self, agent: AgentConfig, cwd: str, session_id: str) -> list[str] | None:
        if agent.harness == HarnessKind.CODEX:
            permission = (
                ["--dangerously-bypass-approvals-and-sandbox"]
                if agent.permission.value == "full"
                else ["-s", "workspace-write"]
                if agent.permission.value == "ask"
                else ["--approve-for-me"]
            )
            native_args = [
                arg for arg in self.codex_args
                if arg not in {"--skip-git-repo-check", "exec", "resume"}
                and "bypass" not in arg
            ]
            model = ["--model", agent.model] if agent.model else []
            thinking = (["-c", f'model_reasoning_effort="{agent.reasoning.value}"']
                        if agent.reasoning.value != "default" else [])
            return [self.codex_binary, "resume", *model, *thinking, *permission,
                    "--no-alt-screen", "-C", cwd, *native_args, session_id]
        if agent.harness == HarnessKind.PI:
            return ["pi", "--session", session_id, *(["--model", agent.model] if agent.model else [])]
        if agent.harness == HarnessKind.OPENCODE:
            command = ["opencode", "--session", session_id, cwd]
            if agent.model:
                command += ["--model", agent.model]
            if agent.permission.value != "ask":
                command.append("--auto")
            return command
        if agent.harness == HarnessKind.CLAUDE:
            command = ["claude", "--resume", session_id]
            if agent.model:
                command += ["--model", agent.model]
            if agent.permission.value == "full":
                command.append("--dangerously-skip-permissions")
            elif agent.permission.value == "workspace":
                command += ["--permission-mode", "acceptEdits"]
            return command
        return None
