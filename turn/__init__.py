"""Turn — a live, editable workgraph.

The kernel is deliberately small: a versioned graph (Nodes + Edges), a runner
that schedules runnable work, and worker adapters that return one of four
outcomes. Domain behaviour emerges from the root objective, the generated
graph, the selected workers, and attached skills.
"""

__version__ = "0.1.0"
