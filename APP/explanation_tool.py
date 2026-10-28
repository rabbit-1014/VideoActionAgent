"""Validate and persist source-bound, inert SVG explanation panels."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation
from pathlib import Path


class ExplanationError(ValueError):
    """An explanation payload or target path is outside the local safe contract."""


_ARGUMENT_KEYS = {"title", "purpose", "subject", "panels"}
_PANEL_KEYS = {"id", "title", "explanation", "provenance", "evidence", "svg"}
_SVG_NS = "http://www.w3.org/2000/svg"
_MAX_SVG_BYTES = 40_000
_MAX_SOURCE_BYTES = 512_000
_MAX_PANELS = 6
_MAX_ELEMENTS = 512
_MAX_DEPTH = 24
_MAX_COORDINATE = Decimal("1000000")
_MAX_VIEWBOX_SIDE = Decimal("8192")
_ID_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}\Z")
_PANEL_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,30}\Z")
_STEM_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_NUMBER_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?\Z")
_VIEWBOX_SPLIT_RE = re.compile(r"[\s,]+")
_XML_DECL_RE = re.compile(r"<\?xml\s+version=(?:\"1\.0\"|'1\.0')(?:\s+encoding=(?:\"UTF-8\"|'UTF-8'))?\s*\?>\Z", re.I)

_COMMON_ATTRS = {
    "id", "fill", "stroke", "stroke-width", "stroke-linecap", "stroke-linejoin",
    "opacity", "fill-opacity", "stroke-opacity", "transform",
}
_TEXT_PRESENTATION = {"font-size", "font-family", "font-weight", "text-anchor", "dominant-baseline"}
_ATTRS_BY_TAG = {
    "svg": _COMMON_ATTRS | _TEXT_PRESENTATION | {"viewBox", "width", "height", "preserveAspectRatio", "role", "aria-label"},
    "g": _COMMON_ATTRS | _TEXT_PRESENTATION | {"aria-label"},
    "defs": {"id"},
    "marker": _COMMON_ATTRS | {"markerWidth", "markerHeight", "markerUnits", "refX", "refY", "orient"},
    "rect": _COMMON_ATTRS | {"x", "y", "width", "height", "rx", "ry"},
    "circle": _COMMON_ATTRS | {"cx", "cy", "r"},
    "ellipse": _COMMON_ATTRS | {"cx", "cy", "rx", "ry"},
    "line": _COMMON_ATTRS | {"x1", "y1", "x2", "y2", "marker-start", "marker-mid", "marker-end"},
    "polyline": _COMMON_ATTRS | {"points", "marker-start", "marker-mid", "marker-end"},
    "polygon": _COMMON_ATTRS | {"points", "marker-start", "marker-mid", "marker-end"},
    "path": _COMMON_ATTRS | {"d", "marker-start", "marker-mid", "marker-end"},
    "text": _COMMON_ATTRS | {"x", "y", "dx", "dy", "font-size", "font-family", "font-weight",
                              "text-anchor", "dominant-baseline"},
    "tspan": _COMMON_ATTRS | {"x", "y", "dx", "dy", "font-size", "font-family", "font-weight",
                               "text-anchor", "dominant-baseline"},
    "title": {"id"},
    "desc": {"id"},
}


def svg_capabilities():
    """Describe the actual inert SVG subset, so callers do not guess attributes."""
    return {"namespace": _SVG_NS, "element_attributes": {tag: sorted(attrs) for tag, attrs in _ATTRS_BY_TAG.items()},
            "constraints": "同一工具的各面板viewBox相同；不使用style/CSS、事件、脚本、HTML、图片、外链或动画；marker仅可url(#本地id)。"}
_MARKER_ATTRS = {"marker-start", "marker-mid", "marker-end"}
_GEOMETRY_ATTRS = {
    "x", "y", "width", "height", "rx", "ry", "cx", "cy", "r", "x1", "y1", "x2", "y2",
    "dx", "dy", "stroke-width", "opacity", "fill-opacity", "stroke-opacity", "font-size",
    "markerWidth", "markerHeight", "refX", "refY",
}


def _fail(code: str) -> None:
    raise ExplanationError(code)


def _text(value, field: str, limit: int) -> str:
    if type(value) is not str:
        _fail(f"invalid_{field}")
    value = value.strip()
    if not value or len(value) > limit:
        _fail(f"invalid_{field}")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        _fail(f"invalid_{field}")
    if any(ord(char) < 0x20 and char not in "\t\n\r" for char in value):
        _fail(f"invalid_{field}")
    return value


def _parse_viewbox(value: str):
    parts = [part for part in _VIEWBOX_SPLIT_RE.split(value.strip()) if part]
    if len(parts) != 4 or any(not _NUMBER_RE.fullmatch(part) for part in parts):
        _fail("invalid_svg_viewbox")
    try:
        numbers = tuple(Decimal(part) for part in parts)
    except InvalidOperation:
        _fail("invalid_svg_viewbox")
    if any(not item.is_finite() for item in numbers):
        _fail("invalid_svg_viewbox")
    x, y, width, height = numbers
    if width <= 0 or height <= 0 or width > _MAX_VIEWBOX_SIDE or height > _MAX_VIEWBOX_SIDE:
        _fail("invalid_svg_viewbox")
    if any(abs(item) > _MAX_COORDINATE for item in (x, y, x + width, y + height)):
        _fail("invalid_svg_viewbox")
    return numbers


def _parse_svg(svg: str):
    if type(svg) is not str:
        _fail("invalid_svg")
    try:
        raw = svg.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        _fail("invalid_svg")
    if not raw or len(raw) > _MAX_SVG_BYTES or b"\x00" in raw:
        _fail("invalid_svg_size")
    if re.search(r"<!|<\?", svg, re.I):
        # A single ordinary XML declaration is harmless; all other declarations/PIs are rejected.
        stripped = svg.lstrip("\ufeff \t\r\n")
        if not stripped.startswith("<?xml"):
            _fail("invalid_svg_declaration")
        end = stripped.find("?>")
        if end < 0 or end > 120 or not _XML_DECL_RE.fullmatch(stripped[:end + 2]):
            _fail("invalid_svg_declaration")
        stripped = stripped[end + 2:]
        if re.search(r"<!|<\?", stripped, re.I):
            _fail("invalid_svg_declaration")
        svg_to_parse = stripped
    else:
        svg_to_parse = svg
    try:
        root = ET.fromstring(svg_to_parse)
    except (ET.ParseError, ValueError):
        _fail("invalid_svg_xml")
    if root.tag != f"{{{_SVG_NS}}}svg":
        _fail("invalid_svg_root")
    if root.attrib.get("viewBox") is None:
        _fail("invalid_svg_viewbox")

    viewbox = _parse_viewbox(root.attrib["viewBox"])
    ids = {}
    marker_refs = []
    count = 0
    stack = [(root, 1)]
    while stack:
        element, depth = stack.pop()
        count += 1
        if depth > _MAX_DEPTH or count > _MAX_ELEMENTS:
            _fail("svg_complexity_limit")
        if not isinstance(element.tag, str) or not element.tag.startswith(f"{{{_SVG_NS}}}"):
            _fail("invalid_svg_element")
        tag = element.tag.rsplit("}", 1)[-1]
        if tag not in _ATTRS_BY_TAG or (tag == "svg" and element is not root):
            _fail("invalid_svg_element")
        allowed = _ATTRS_BY_TAG[tag]
        for key, value in element.attrib.items():
            if not isinstance(key, str) or key.startswith("{"):
                _fail("invalid_svg_attribute")
            lowered = key.lower()
            if lowered.startswith("on") or lowered == "style" or key not in allowed:
                _fail("invalid_svg_attribute")
            if "url(" in value.lower() or "javascript:" in value.lower() or "data:" in value.lower():
                if key not in _MARKER_ATTRS:
                    _fail("external_svg_resource")
            if key in _GEOMETRY_ATTRS:
                if not _NUMBER_RE.fullmatch(value.strip()):
                    _fail("invalid_svg_number")
                try:
                    number = Decimal(value.strip())
                except InvalidOperation:
                    _fail("invalid_svg_number")
                if not number.is_finite() or abs(number) > _MAX_COORDINATE:
                    _fail("invalid_svg_number")
            if key == "id":
                if not _ID_RE.fullmatch(value) or value in ids:
                    _fail("invalid_svg_id")
                ids[value] = tag
            if key in _MARKER_ATTRS:
                if value == "none":
                    continue
                match = re.fullmatch(r"url\(#([A-Za-z_][A-Za-z0-9_.-]{0,63})\)", value.strip())
                if not match:
                    _fail("external_svg_resource")
                marker_refs.append(match.group(1))
        if tag == "svg":
            for attr in ("width", "height"):
                if attr in element.attrib:
                    value = element.attrib[attr].strip()
                    if not _NUMBER_RE.fullmatch(value):
                        _fail("invalid_svg_number")
                    number = Decimal(value)
                    if not number.is_finite() or number <= 0 or number > _MAX_VIEWBOX_SIDE:
                        _fail("invalid_svg_number")
        if tag in {"title", "desc"} and len(list(element)):
            _fail("invalid_svg_text_element")
        for child in reversed(list(element)):
            stack.append((child, depth + 1))
    for marker_id in marker_refs:
        if ids.get(marker_id) != "marker":
            _fail("invalid_svg_marker_reference")
    return raw, viewbox


def validate_arguments(value, segment_count):
    """Validate the exact tool contract and return a detached canonical payload."""
    if type(segment_count) is not int or segment_count < 0:
        _fail("invalid_segment_count")
    if type(value) is not dict or set(value) != _ARGUMENT_KEYS:
        _fail("invalid_arguments")
    title = _text(value["title"], "title", 100)
    purpose = _text(value["purpose"], "purpose", 500)
    subject = _text(value["subject"], "subject", 120)
    panels = value["panels"]
    if type(panels) is not list or not 1 <= len(panels) <= _MAX_PANELS:
        _fail("invalid_panels")
    result_panels = []
    seen_ids = set()
    common_viewbox = None
    for panel in panels:
        if type(panel) is not dict or set(panel) != _PANEL_KEYS:
            _fail("invalid_panel_shape")
        panel_id = panel["id"]
        if type(panel_id) is not str or not _PANEL_ID_RE.fullmatch(panel_id) or panel_id in seen_ids:
            _fail("invalid_panel_id")
        seen_ids.add(panel_id)
        panel_title = _text(panel["title"], "panel_title", 80)
        explanation = _text(panel["explanation"], "explanation", 1800)
        provenance = panel["provenance"]
        if type(provenance) is not str or provenance not in {"source", "illustrative"}:
            _fail("invalid_provenance")
        evidence = panel["evidence"]
        if type(evidence) is not list or not 1 <= len(evidence) <= 12:
            _fail("invalid_evidence")
        if any(type(index) is not int or not 1 <= index <= segment_count for index in evidence):
            _fail("invalid_evidence")
        if len(set(evidence)) != len(evidence):
            _fail("duplicate_evidence")
        svg = panel["svg"]
        _, viewbox = _parse_svg(svg)
        if common_viewbox is None:
            common_viewbox = viewbox
        elif viewbox != common_viewbox:
            _fail("inconsistent_svg_viewbox")
        result_panels.append({
            "id": panel_id,
            "title": panel_title,
            "explanation": explanation,
            "provenance": provenance,
            "evidence": list(evidence),
            "svg": svg,
        })
    return {"title": title, "purpose": purpose, "subject": subject, "panels": result_panels}


def _is_reparse_point(info) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & reparse_flag)


def _safe_directory(directory) -> Path:
    try:
        raw_path = os.fspath(directory)
    except TypeError:
        _fail("invalid_output_directory")
    if not isinstance(raw_path, str) or not raw_path:
        _fail("invalid_output_directory")
    if ".." in Path(raw_path).parts:
        _fail("unsafe_output_directory")
    path = Path(os.path.abspath(raw_path))
    anchor = Path(path.anchor)
    current = anchor
    try:
        if not current.exists():
            _fail("output_directory_unavailable")
        if _is_reparse_point(current.lstat()):
            _fail("unsafe_output_directory")
        for part in path.parts[1:]:
            current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                try:
                    current.mkdir()
                except FileExistsError:
                    pass
                info = current.lstat()
            if _is_reparse_point(info) or not stat.S_ISDIR(info.st_mode):
                _fail("unsafe_output_directory")
    except (OSError, ValueError) as exc:
        raise ExplanationError("output_directory_unavailable") from exc
    return path


def _write_exclusive(path: Path, data: bytes):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise ExplanationError("asset_already_exists") from exc
    try:
        opened = os.fstat(descriptor)
        identity = (opened.st_dev, opened.st_ino)
    except OSError as exc:
        os.close(descriptor)
        raise ExplanationError("asset_write_failed") from exc
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        try:
            current = path.lstat()
            if (not _is_reparse_point(current) and stat.S_ISREG(current.st_mode)
                    and (current.st_dev, current.st_ino) == identity):
                path.unlink()
        except OSError:
            pass
        raise ExplanationError("asset_write_failed") from exc
    return identity


def _cleanup_created(directory: Path, created):
    for path, expected in reversed(created):
        try:
            current = path.lstat()
            if (not _is_reparse_point(current) and stat.S_ISREG(current.st_mode)
                    and (current.st_dev, current.st_ino) == expected):
                path.unlink()
        except OSError:
            pass


def render(value, directory, stem):
    """Persist one validated SVG per panel plus a shared canonical JSON source."""
    if type(value) is not dict:
        _fail("invalid_arguments")
    max_evidence = 0
    try:
        for panel in value["panels"]:
            if type(panel) is dict and type(panel.get("evidence")) is list:
                max_evidence = max(max_evidence, *(panel["evidence"] or [0]))
    except (KeyError, TypeError, ValueError):
        _fail("invalid_arguments")
    canonical = validate_arguments(value, max_evidence)
    if type(stem) is not str or not _STEM_RE.fullmatch(stem):
        _fail("invalid_stem")
    target = _safe_directory(directory)
    source_filename = stem + ".json"
    output_specs = [(source_filename, None)]
    for panel in canonical["panels"]:
        output_specs.append((f"{stem}_{panel['id']}.svg", panel))
    paths = [target / name for name, _ in output_specs]
    if len({path.name for path in paths}) != len(paths):
        _fail("asset_name_collision")
    for path in paths:
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ExplanationError("asset_path_unavailable") from exc
        _fail("asset_already_exists")

    source_bytes = (json.dumps(canonical, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    if len(source_bytes) > _MAX_SOURCE_BYTES:
        _fail("parameter_source_too_large")
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    rendered = []
    for filename, panel in output_specs[1:]:
        svg_bytes, _ = _parse_svg(panel["svg"])
        rendered.append((filename, panel, svg_bytes))

    created = []
    try:
        for path, data in [(paths[0], source_bytes),
                           *[(target / filename, data) for filename, _, data in rendered]]:
            identity = _write_exclusive(path, data)
            created.append((path, identity))
            info = path.lstat()
            if (_is_reparse_point(info) or not stat.S_ISREG(info.st_mode)
                    or (info.st_dev, info.st_ino) != identity):
                _fail("unsafe_asset_path")
    except Exception as exc:
        _cleanup_created(target, created)
        if isinstance(exc, ExplanationError):
            raise
        if isinstance(exc, OSError):
            raise ExplanationError("asset_write_failed") from exc
        raise

    assets = []
    for filename, panel, data in rendered:
        assets.append({
            "role": "derived_explanation",
            "filename": filename,
            "sha256": hashlib.sha256(data).hexdigest(),
            "source_filename": source_filename,
            "source_sha256": source_sha256,
            "panel_id": panel["id"],
        })
    return assets

