"""Lightweight MetaGPT-style ActionNode compiler and [CONTENT] JSON extractor.

This isolates MetaGPT's ActionNode compilation and tagged-JSON output parsing
pattern without pulling in MetaGPT 0.8.2's broken/monolithic dependency tree
(e.g. missing ``lancedb==0.4.0`` on PyPI and eager ``semantic_kernel`` /
cloud-SDK imports in ``metagpt.__init__`` and ``metagpt.actions.__init__``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final, get_args, get_origin

TAG: Final[str] = "CONTENT"
LANGUAGE_CONSTRAINT: Final[str] = (
    "Language: Write specification prose in the user requirement language while "
    "preserving exact repository paths, symbol names, and schema enum keys in English."
)
FORMAT_CONSTRAINT: Final[str] = (
    f"Format: Output a single valid JSON object wrapped inside [{TAG}][/{TAG}] tags "
    "matching the format example, with no extra keys or commentary."
)

SIMPLE_TEMPLATE: Final[str] = """
## context
{context}

-----

## format example
{example}

## nodes: "<node>: <type>  # <instruction>"
{instruction}

## constraint
{constraint}

## action
Follow instructions of every node carefully, ground every file path and symbol in the repository context, and emit valid JSON wrapped in [CONTENT]...[/CONTENT].
""".strip()

_CONTENT_BLOCK_RE = re.compile(r"\[CONTENT\]\s*(.*?)\s*\[/CONTENT\]", re.DOTALL | re.IGNORECASE)
_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


def format_type_hint(expected_type: Any) -> str:
    """Return a concise readable type description for prompt instructions."""
    origin = get_origin(expected_type)
    if origin is not None:
        args = ", ".join(repr(arg) if isinstance(arg, str) else format_type_hint(arg) for arg in get_args(expected_type))
        name = getattr(origin, "__name__", None) or getattr(origin, "_name", None) or str(origin).replace("typing.", "")
        return f"{name}[{args}]"
    if isinstance(expected_type, type):
        return expected_type.__name__
    return str(expected_type)


@dataclass(frozen=True)
class ActionNode:
    """Declarative schema node following MetaGPT's ActionNode prompt pattern."""

    key: str
    expected_type: Any
    instruction: str
    example: Any = ""
    children: tuple["ActionNode", ...] = field(default=())

    def compile_instruction(self) -> str:
        """Render child node instructions as markdown bullet points."""
        nodes = self.children or (self,)
        return "\n".join(
            f"- {node.key}: {format_type_hint(node.expected_type)}  # {node.instruction}"
            for node in nodes
        )

    def compile_example(self, *, tag: str = TAG) -> str:
        """Render an example JSON payload wrapped in MetaGPT [CONTENT] tags."""
        nodes = self.children or (self,)
        payload = {node.key: node.example for node in nodes}
        rendered = json.dumps(payload, indent=2, ensure_ascii=False)
        return f"[{tag}]\n{rendered}\n[/{tag}]" if tag else rendered

    def compile(
        self,
        *,
        context: str,
        constraints: Sequence[str] = (LANGUAGE_CONSTRAINT, FORMAT_CONSTRAINT),
        template: str = SIMPLE_TEMPLATE,
    ) -> str:
        """Compile context, schema example, node instructions, and constraints into a prompt."""
        return template.format(
            context=context.strip(),
            example=self.compile_example(),
            instruction=self.compile_instruction(),
            constraint="\n".join(constraints),
        )


def _strip_optional_json_fence(block: str) -> str:
    stripped = block.strip()
    fence_match = _CODE_FENCE_RE.fullmatch(stripped)
    if fence_match:
        return fence_match.group(1).strip()
    return stripped


def _extract_balanced_json_object(text: str) -> str | None:
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        ch = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def extract_content_json(raw_text: str, *, tag: str = TAG) -> dict[str, Any]:
    """Extract and decode the structured JSON object from an LLM response."""
    if not isinstance(raw_text, str) or not raw_text.strip():
        raise ValueError("LLM response was empty; expected a [CONTENT]{...}[/CONTENT] JSON object.")

    candidates: list[str] = []
    if tag.upper() == TAG:
        tag_matches = _CONTENT_BLOCK_RE.findall(raw_text)
    else:
        custom_re = re.compile(
            rf"\[{re.escape(tag)}\]\s*(.*?)\s*\[/{re.escape(tag)}\]",
            re.DOTALL | re.IGNORECASE,
        )
        tag_matches = custom_re.findall(raw_text)

    for match in tag_matches:
        candidates.append(_strip_optional_json_fence(match))
    for fence_match in _CODE_FENCE_RE.findall(raw_text):
        candidates.append(fence_match.strip())
    balanced = _extract_balanced_json_object(raw_text)
    if balanced is not None:
        candidates.append(balanced.strip())
    candidates.append(raw_text.strip())

    last_error: Exception | None = None
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue
        if isinstance(parsed, dict):
            return parsed
        last_error = ValueError(f"Expected JSON object in [{tag}], got {type(parsed).__name__}.")

    detail = f" ({last_error})" if last_error else ""
    raise ValueError(f"Could not extract a valid JSON object from LLM response{detail}.")
