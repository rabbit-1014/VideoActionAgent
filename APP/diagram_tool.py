"""Bounded, local Graphviz rendering for validated task diagrams."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

from goal_strategy import StrategyError, diagram_arguments

_NODE = Path(r"C:\Program Files\nodejs\node.exe")
_RENDERER = Path(__file__).with_name("render_diagram.cjs")
_STEM = re.compile(r"[a-z][a-z0-9_]{0,30}\Z")
LAYOUT_VERSION = 2


class DiagramError(ValueError):
    """Stable public error for rejected or failed diagram renders."""


def _wrap(text: str, width: int = 20) -> list[str]:
    normalized = " ".join(text.split())
    lines, line, units = [], "", 0
    for char in re.findall(r"[A-Za-z0-9_]+|.", normalized):
        size = sum(2 if ord(c) > 127 else 1 for c in char)
        if units + size > width and line:
            lines.append(line)
            line, units = "", 0
        line, units = line + char, units + size
    return [*lines, line] if line else lines or [""]


def _dot_quote(text: str) -> str:
    """Quote DOT string content, preserving only our own line-break escapes."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("\r", " ").replace("\n", " ")
    return '"' + escaped + '"'


def _label(text: str, width: int = 20) -> str:
    return '"' + r"\n".join(_dot_quote(line)[1:-1] for line in _wrap(text, width)) + '"'


def _build_dot(arguments: dict) -> str:
    lines = [
        "digraph task_diagram {",
        f"  rankdir={arguments['direction']};",
        f'  graph [label={_label(arguments["title"])}, labelloc=t, fontsize=18, fontname="Microsoft YaHei", bgcolor="white", pad="0.34", nodesep="1.0", ranksep="0.85", splines=spline];',
        '  node [shape=box, style="rounded,filled", fillcolor="#F2F6FC", color="#526783", penwidth=1.2, fontname="Microsoft YaHei", fontsize=16, margin="0.18,0.12"];',
        '  edge [color="#71839A", fontcolor="#34465E", fontname="Microsoft YaHei", fontsize=13, arrowsize=0.75];',
    ]
    for node in arguments["nodes"]:
        # WASM Graphviz cannot measure the Windows CJK font used by rasterization.
        # Reserve a conservative width instead of letting Chinese labels spill out.
        units = max(sum(2 if ord(c) > 127 else 1 for c in part) for part in _wrap(node["label"]))
        minimum_width = (units * 16 * .58 + 30) / 72
        lines.append(f"  {node['id']} [label={_label(node['label'])}, width={minimum_width:.3f}];")
    for edge in arguments["edges"]:
        lines.append(
            f"  {edge['source']} -> {edge['target']} [label={_label(edge['label'], 14)}];"
        )
    lines.append("}")
    return "\n".join(lines) + "\n"


def _segment_count(arguments) -> int:
    indices = []
    if isinstance(arguments, dict):
        for group_name in ("nodes", "edges"):
            group = arguments.get(group_name)
            if isinstance(group, list):
                for item in group:
                    if isinstance(item, dict):
                        evidence = item.get("evidence")
                        if isinstance(evidence, list):
                            indices.extend(i for i in evidence if type(i) is int)
    return max([1, *indices])


def render(arguments, directory: Path, stem: str) -> dict:
    """Render validated data to local PNG and editable DOT source."""
    if not isinstance(stem, str) or not _STEM.fullmatch(stem):
        raise DiagramError("invalid_diagram_stem")
    try:
        validated = diagram_arguments(arguments, _segment_count(arguments))
    except (StrategyError, TypeError, KeyError, AttributeError) as exc:
        raise DiagramError("invalid_diagram") from exc

    try:
        dot = _build_dot(validated)
        completed = subprocess.run(
            [str(_NODE), str(_RENDERER)],
            input=json.dumps({"dot": dot}, ensure_ascii=False),
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=30,
            check=False,
            shell=False,
        )
        if completed.returncode != 0:
            raise DiagramError("diagram_render_failed")
        result = json.loads(completed.stdout)
        png = bytes.fromhex(result["png_hex"])
        width, height = result["width"], result["height"]
        if (
            not png.startswith(b"\x89PNG\r\n\x1a\n")
            or type(width) is not int
            or type(height) is not int
            or width <= 0
            or height <= 0
            or width > 1400
            or height > 1200
        ):
            raise DiagramError("diagram_dimensions_rejected")
        target_dir = Path(directory)
        if not target_dir.is_dir() or target_dir.is_symlink() or target_dir.is_junction():
            raise DiagramError("diagram_directory_unavailable")
        image_name, source_name = f"{stem}.png", f"{stem}.dot"
        if any((target_dir / name).exists() or (target_dir / name).is_symlink()
               for name in (image_name, source_name)):
            raise DiagramError("diagram_target_already_exists")
        (target_dir / source_name).write_text(dot, encoding="utf-8", newline="\n")
        (target_dir / image_name).write_bytes(png)
        return {
            "filename": image_name,
            "sha256": hashlib.sha256(png).hexdigest(),
            "width": width,
            "height": height,
            "source_filename": source_name,
            "source_sha256": hashlib.sha256(dot.encode("utf-8")).hexdigest(),
            "layout_version": LAYOUT_VERSION,
        }
    except DiagramError:
        raise
    except subprocess.TimeoutExpired as exc:
        raise DiagramError("diagram_render_timeout") from exc
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise DiagramError("diagram_render_failed") from exc
