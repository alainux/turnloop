---
name: turn-product-coherence
description: Keep independently produced work coherent as one user-facing product.
---

# Product coherence

Use this skill whenever a worker changes a product that has multiple modules,
surfaces, or interaction states.

## Read the real contract first

- Read the graph, the current node's instructions, the architecture metadata,
  and the prerequisite outputs before changing files.
- Treat the original user outcome as the acceptance contract. Do not replace a
  requested product with a framework, mock, disconnected demo, or collection of
  technically valid parts.
- Identify the single entry point, the canonical state model, and the user
  journey that connects the major modules.

## Preserve one product

- Every visible surface must be driven by the same runtime state and must be
  reachable from the documented entry point.
- A visual element, overlay, control, asset, or route must have a clear role in
  the requested experience. Remove or wire unrelated placeholder surfaces.
- Keep module boundaries explicit, but reconcile names, events, IDs, coordinate
  systems, and lifecycle rules at the composition boundary.
- Do not report completion while a required interaction is only implied,
  static, unreachable, or represented by placeholder geometry/content.

## Prove the result

- Exercise the real launch command and the shortest complete user journey.
- For browser products, inspect the rendered page and interaction states, not
  only source code or a successful build.
- Record the concrete invariant or user-visible behavior that was checked in
  the Turn result or verification decision. Keep protocol handoffs concise;
  durable implementation evidence belongs in the project files.
