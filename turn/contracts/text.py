"""Text crossing from provider terminals into durable Turn state."""
from __future__ import annotations

import re


_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_CSI = re.compile(r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]")
_ESC = re.compile(r"\x1b(?:[@-_]|\([^\r\n])")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_SPACE = re.compile(r"[ \t]+")
_BLANKS = re.compile(r"\n{3,}")


def sanitize_control_text(value: object, *, limit: int = 4000) -> str:
    """Return readable bounded text safe for persistent business state.

    Terminal streams remain raw in the terminal transport. This function is
    only for provider feedback, errors, summaries, and reasons that become
    durable Turn state.
    """
    text = "" if value is None else str(value)
    text = _OSC.sub("", text)
    text = _CSI.sub("", text)
    text = _ESC.sub("", text)
    text = _CONTROL.sub("", text)
    text = text.replace("\r", "\n")
    lines = [_SPACE.sub(" ", line).strip() for line in text.split("\n")]
    text = _BLANKS.sub("\n\n", "\n".join(lines)).strip()
    if len(text) > limit:
        text = text[: max(0, limit - 1)].rstrip() + "…"
    return text
