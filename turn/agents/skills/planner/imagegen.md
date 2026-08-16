# Concept image generation skill

Use this project-scoped skill when a visual product needs a concrete concept
to guide implementation or verification. Generate a small, purposeful visual
reference only when it reduces ambiguity; do not generate decorative images.
Save project-bound images under `.turn/concepts/` and link them from the
project document with ordinary Markdown when useful. The worker that creates
or changes the image reports that path through the normal Turn CLI artifact
array; do not use a filesystem handoff for status or completion. Describe which
graph invariants the image communicates.
The image is evidence for workers and verifiers, not a substitute for a
runnable implementation.
