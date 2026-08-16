"""Pure renderers for graph-owned architectural metadata."""
from __future__ import annotations

from turn.domain.schemas import ArchitectureSection, ArchitectureSpec


def architecture_spec_text(spec: ArchitectureSpec) -> str:
    """Render the graph metadata into a worker-readable architecture brief."""
    lines = [f"# {spec.title}", "", "## Executive summary", spec.executive_summary]
    for title, value in (("Approach", spec.approach), ("Strategy", spec.strategy)):
        if value:
            lines.extend(["", f"## {title}", value])
    if spec.filesystem_structure:
        lines.extend([
            "",
            "## Project filesystem structure",
            "```text",
            spec.filesystem_structure.strip(),
            "```",
        ])
    if spec.research_sources:
        lines.extend(["", "## Research sources"])
        lines.extend(f"- {source}" for source in spec.research_sources)
    _bullets(lines, "Architecture principles", spec.architecture_principles)
    _bullets(lines, "Requirements", spec.requirements)
    _bullets(lines, "Constraints", spec.constraints)
    if spec.decisions:
        lines.extend(["", "## Decisions"])
        for decision in spec.decisions:
            lines.extend([
                f"### {decision.title}",
                decision.decision,
                f"Rationale: {decision.rationale}",
            ])
            _bullets(lines, "Consequences", decision.consequences, level=4)
    if spec.risks:
        lines.extend(["", "## Risks"])
        for risk in spec.risks:
            lines.extend([f"### {risk.title}", risk.description])
            if risk.mitigation:
                lines.append(f"Mitigation: {risk.mitigation}")
    _render_sections(lines, spec.sections)
    _bullets(lines, "Acceptance criteria", spec.acceptance_criteria)
    if spec.diagrams:
        lines.extend(["", "## Diagrams"])
        for diagram in spec.diagrams:
            lines.extend([f"### {diagram.title}", diagram.description or ""])
            lines.append(
                "Diagram nodes: "
                + ", ".join(node.label for node in diagram.nodes)
            )
            for edge in diagram.edges:
                label = f" ({edge.label})" if edge.label else ""
                lines.append(f"- {edge.src} -> {edge.dst}{label}")
    return "\n".join(lines).strip()


def _bullets(lines: list[str], title: str, values: list[str], *, level: int = 2) -> None:
    if not values:
        return
    lines.extend(["", f"{'#' * level} {title}"])
    lines.extend(f"- {value}" for value in values)


def _render_sections(lines: list[str], sections: list[ArchitectureSection], level: int = 2) -> None:
    for section in sections:
        lines.extend(["", f"{'#' * level} {section.title}"])
        if section.content:
            lines.append(section.content)
        _render_sections(lines, section.subsections, min(level + 1, 6))
