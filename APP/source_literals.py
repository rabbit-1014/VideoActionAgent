"""Strict lexical checks for copyable technical strings."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping
from typing import Any

from explanation_tool import ExplanationError, validate_arguments

_MAX_CODE_BLOCK_CHARS = 8_000
_MAX_INLINE_CHARS = 256
_MAX_RESULTS = 80
_MAX_RESULT_CHARS = 120

_FENCED_BLOCK_RE = re.compile(
    r"^[ \t]*(?P<mark>`{3,}|~{3,})[^\r\n]*\r?\n"
    r"(?P<body>.*?)"
    r"^[ \t]*(?P=mark)[`~]*[ \t]*$",
    re.MULTILINE | re.DOTALL,
)
_INLINE_RE = re.compile(r"(?<!`)`([^`\r\n]+)`(?!`)")
_IDENTIFIER_RE = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z_][A-Za-z0-9_]*(?![A-Za-z0-9_])")
_SQL_PHRASE_RE = re.compile(r"(?<![A-Za-z0-9_])ON\s+SET(?![A-Za-z0-9_])")
_TOOL_PLACEHOLDER_RE = re.compile(r"\[\[TOOL:[^\]]*\]\]", re.IGNORECASE)
_SPOKEN_UNDERSCORE_RE = re.compile(
    r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9]*(?:\s+underscore\s+[A-Za-z][A-Za-z0-9]*)+(?![A-Za-z0-9_])"
)


def _verbatim_in_one_segment(value: str, segments: Iterable[Any]) -> bool:
    for segment in segments:
        if isinstance(segment, Mapping):
            text = segment.get("text")
            if isinstance(text, str) and value in text:
                return True
    return False


def _identifier_in_one_segment(value: str, segments: Iterable[Any]) -> bool:
    if _verbatim_in_one_segment(value, segments):
        return True
    # An explicitly spoken separator can support this spelling. Do not infer
    # separators from spaces, repair ASR words, or reconstruct executable code.
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*_[A-Za-z][A-Za-z0-9]*", value):
        return False
    for segment in segments:
        if not isinstance(segment, Mapping) or not isinstance(segment.get("text"), str):
            continue
        for match in _SPOKEN_UNDERSCORE_RE.finditer(segment["text"]):
            # Match the complete spoken identifier, never a prefix or suffix of
            # a longer chain such as a underscore b underscore c.
            components = match.group(0).split()[::2]
            if "underscore" not in components and "_".join(components) == value:
                return True
    return False


def _extract_visible_field_candidates(value: str) -> set[str]:
    """Extract underscore identifiers and the uppercase SQL phrase ON SET."""
    cleaned = _TOOL_PLACEHOLDER_RE.sub(" ", value)
    found = {match.group(0) for match in _SQL_PHRASE_RE.finditer(cleaned)}
    identifier_text = _SQL_PHRASE_RE.sub(" ", cleaned)
    for match in _IDENTIFIER_RE.finditer(identifier_text):
        token = match.group(0)
        if token.isascii() and "_" in token and any(char.isascii() and char.isalpha() for char in token):
            found.add(token)
    return found


def _tool_visible_strings(tool_calls: Iterable[Any]) -> Iterable[str]:
    for call in tool_calls:
        if not isinstance(call, Mapping):
            continue
        arguments = call.get("arguments")
        if not isinstance(arguments, Mapping):
            continue
        if call.get("name") == "render_explanation":
            panels = arguments.get("panels")
            if not isinstance(panels, list):
                continue
            evidence = [
                index
                for panel in panels
                if isinstance(panel, Mapping) and isinstance(panel.get("evidence"), list)
                for index in panel["evidence"]
                if type(index) is int
            ]
            try:
                canonical = validate_arguments(arguments, max(evidence, default=0))
            except (ExplanationError, TypeError, ValueError, RecursionError):
                continue
            for field in ("title", "purpose", "subject"):
                yield canonical[field]
            for panel in canonical["panels"]:
                yield panel["title"]
                yield panel["explanation"]
                try:
                    svg_root = ET.fromstring(panel["svg"])
                except (ET.ParseError, ValueError, RecursionError):
                    continue
                for element in svg_root.iter():
                    if not isinstance(element.tag, str):
                        continue
                    local_name = element.tag.rsplit("}", 1)[-1]
                    if local_name in {"text", "tspan", "title", "desc"}:
                        direct_text = [element.text]
                        direct_text.extend(child.tail for child in element)
                        for visible in direct_text:
                            if isinstance(visible, str) and visible.strip():
                                yield visible.strip()
            continue
        title = arguments.get("title")
        if isinstance(title, str):
            yield title
        for collection_name in ("nodes", "edges"):
            collection = arguments.get(collection_name, ())
            if isinstance(collection, (list, tuple)):
                for item in collection:
                    if isinstance(item, Mapping):
                        label = item.get("label")
                        if isinstance(label, str):
                            yield label


def unsupported_identifiers(markdown, tool_calls, segments) -> list[str]:
    """Return sorted unique technical strings not copied from a single source.

    Literal presence or an explicitly spoken single underscore is lexical
    evidence only. It does not validate roles or relationships between parameters.
    """
    source_segments = tuple(segments)
    candidates: set[str] = set()
    text = markdown if isinstance(markdown, str) else ""
    code_block_unverified = False

    def check_block(match: re.Match[str]) -> str:
        nonlocal code_block_unverified
        body = match.group("body")
        if body.endswith("\r\n"):
            body = body[:-2]
        elif body.endswith(("\n", "\r")):
            body = body[:-1]
        if len(body) > _MAX_CODE_BLOCK_CHARS or not _verbatim_in_one_segment(body, source_segments):
            code_block_unverified = True
        return " "

    without_blocks = _FENCED_BLOCK_RE.sub(check_block, text)

    for match in _INLINE_RE.finditer(without_blocks):
        value = match.group(1).strip()
        if not value or not value.isascii():
            continue
        if len(value) > _MAX_INLINE_CHARS:
            code_block_unverified = True
        elif any(char.isalpha() or char == "_" for char in value):
            candidates.add(value)

    candidates.update(_extract_visible_field_candidates(without_blocks))
    for value in _tool_visible_strings(tool_calls or ()):
        candidates.update(_extract_visible_field_candidates(value))

    unsupported = [value for value in candidates if not _identifier_in_one_segment(value, source_segments)]
    if code_block_unverified:
        unsupported.append("code_block")
    bounded = sorted({value[:_MAX_RESULT_CHARS] for value in unsupported})
    if len(bounded) > _MAX_RESULTS:
        bounded = bounded[:_MAX_RESULTS]
        if "code_block" not in bounded:
            bounded[-1] = "code_block"
            bounded = sorted(set(bounded))
    return bounded
