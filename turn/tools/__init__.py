"""Turn agent tools.

The first and primary tool is a graph explorer: it lets a Codex agent query
the live project workgraph at runtime (what is already planned/built, what
each node produces, how they relate) instead of Turn injecting a frozen
snapshot. Agents invoke it from the shell; the worker sets TURN_PROJECT_ID
and TURN_DATABASE_URL so it targets the right project.
"""
