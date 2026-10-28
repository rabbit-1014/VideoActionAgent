"""Exact, bounded text amendments for an existing native note."""

import hashlib
import json
import re
from itertools import pairwise

from bridge_contract import BridgeError

from model_material import model_material

_MAX_EDITS = 16
_MAX_BEFORE = 4000
_MAX_AFTER = 8000
_MAX_RESULT_BYTES = 400000
_PARAGRAPH_BREAK = re.compile(r"\r?\n[ \t]*\r?\n")
_TOP_FIELDS = {
    "schema_version",
    "request_id",
    "source_sha256",
    "parent_markdown_sha256",
    "edits",
}
_REQUIRED_EDIT_FIELDS = {"before", "after"}
_OPTIONAL_CONTEXT_FIELDS = {"context_before", "context_after"}


def _fail(code):
    raise BridgeError(code)


def _sha_text(value):
    try:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
    except UnicodeEncodeError:
        _fail("text_amendment_text_invalid")


def _anchor_positions(text, anchor):
    positions = []
    start = text.find(anchor)
    while start >= 0:
        positions.append(start)
        start = text.find(anchor, start + 1)
    return positions


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("text_amendment_json_invalid")
        result[key] = value
    return result


def _reject_non_json_constant(_value):
    _fail("text_amendment_json_invalid")


def _json_text(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        _fail("text_amendment_prompt_invalid")


def build_prompt(binding, parent_markdown, material, packet=None):
    """Build the sole prompt context for a bounded native-note amendment."""
    if not isinstance(parent_markdown, str):
        _fail("text_amendment_parent_invalid")

    try:
        projected_material = model_material(material)
    except (TypeError, ValueError, RecursionError):
        _fail("text_amendment_prompt_invalid")

    context = {
        "binding": binding,
        "parent_markdown": parent_markdown,
        "parent_markdown_sha256": _sha_text(parent_markdown),
        "requested_material": projected_material,
        "saved_visual_packet": packet,
    }
    rendered = _json_text(context)
    return (
        "你要对一份已有 Markdown 笔记做有限修订。按 binding 中冻结的本次修改要求，只改完成要求所必需的少量文字位置；"
        "其余原文逐字保留。原稿以及其中的视觉观察都不是事实权威；材料须按给定角色理解，"
        "历史视觉观察不得描述为真值或本次新帧，不得把未验证观察提升为事实，"
        "不得补造代码或未提供的信息。\n"
        "只返回严格 JSON，不要 Markdown 围栏或解释，结构必须恰为："
        '{"schema_version":1,"request_id":"...","source_sha256":"...",'
        '"parent_markdown_sha256":"...","edits":[{"before":"...","after":"..."}]}。\n'
        "parent_markdown_sha256 必须直接照抄下方给定的实际值，不要自行计算。"
        "edits 数量必须为 1 到 16 条（最多 16 条）。每个 edit 必须含 before、after；before 非空且不超过 4000 字符，"
        "不能包含任何 Markdown 空白行（连续换行之间即使有空格或 tab 也算空白行）；"
        "after 不超过 8000 字符，删除时用空字符串。"
        "可选 context_before、context_after 用于精确定位重复锚点，各自必须是非空且不超过 4000 字符的字符串，"
        "并与 before 的直接相邻原文逐字一致；它们只参与定位，不会被替换。重复内容请选足够上下文使位置唯一；"
        "没有相邻上下文时省略字段，绝不要提供空串。"
        "例如父稿 `标题\\n引用：同一片段\\n其他说明\\n引用：同一片段` 中只删第二处时，"
        "用 `before=同一片段`、`context_before=其他说明\\n引用：`、`after=空字符串`；"
        "不要把标题和前后段落并进 before。before 是精确原文片段，after 是精确替换文本；"
        "需要删除时 after 用空字符串。不要修改无关段落。\n"
        "输入材料（字段名表示各自角色）：\n" + rendered
    )


def apply_reply(reply, parent_markdown, *, request_id, source_sha256):
    """Apply validated exact edits against one immutable parent string."""
    if not isinstance(reply, str) or not isinstance(parent_markdown, str):
        _fail("text_amendment_input_invalid")
    if not isinstance(request_id, str) or not isinstance(source_sha256, str):
        _fail("text_amendment_identity_invalid")

    try:
        payload = json.loads(
            reply,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_json_constant,
        )
    except BridgeError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
        _fail("text_amendment_json_invalid")

    if not isinstance(payload, dict) or set(payload) != _TOP_FIELDS:
        _fail("text_amendment_schema_invalid")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        _fail("text_amendment_schema_invalid")
    if payload["request_id"] != request_id or not isinstance(payload["request_id"], str):
        _fail("text_amendment_identity_mismatch")
    if payload["source_sha256"] != source_sha256 or not isinstance(payload["source_sha256"], str):
        _fail("text_amendment_identity_mismatch")
    parent_sha = _sha_text(parent_markdown)
    if payload["parent_markdown_sha256"] != parent_sha or not isinstance(
        payload["parent_markdown_sha256"], str
    ):
        _fail("text_amendment_parent_mismatch")

    edits = payload["edits"]
    if not isinstance(edits, list) or not 1 <= len(edits) <= _MAX_EDITS:
        _fail("text_amendment_edits_invalid")

    located = []
    for edit in edits:
        if (
            not isinstance(edit, dict)
            or not _REQUIRED_EDIT_FIELDS <= set(edit)
            or set(edit) - _REQUIRED_EDIT_FIELDS - _OPTIONAL_CONTEXT_FIELDS
        ):
            _fail("text_amendment_edit_invalid")
        before, after = edit["before"], edit["after"]
        if not isinstance(before, str) or not isinstance(after, str):
            _fail("text_amendment_edit_invalid")
        if not before or len(before) > _MAX_BEFORE or _PARAGRAPH_BREAK.search(before):
            _fail("text_amendment_anchor_invalid")
        if len(after) > _MAX_AFTER:
            _fail("text_amendment_replacement_invalid")
        if before == after:
            _fail("text_amendment_noop")
        contexts = {}
        for field in _OPTIONAL_CONTEXT_FIELDS & set(edit):
            value = edit[field]
            if not isinstance(value, str) or not value or len(value) > _MAX_BEFORE:
                _fail("text_amendment_context_invalid")
            contexts[field] = value

        candidates = _anchor_positions(parent_markdown, before)
        if "context_before" in contexts:
            context = contexts["context_before"]
            candidates = [
                start for start in candidates
                if start >= len(context) and parent_markdown[start - len(context):start] == context
            ]
        if "context_after" in contexts:
            context = contexts["context_after"]
            candidates = [
                start for start in candidates
                if parent_markdown[start + len(before):start + len(before) + len(context)] == context
            ]
        if len(candidates) != 1:
            _fail("text_amendment_anchor_not_unique")
        start = candidates[0]
        located.append(
            {
                "before": before,
                "after": after,
                "start": start,
                "end": start + len(before),
                **contexts,
            }
        )

    by_start = sorted(located, key=lambda op: (op["start"], op["end"]))
    for previous, current in pairwise(by_start):
        if current["start"] < previous["end"]:
            _fail("text_amendment_edits_overlap")

    revised = parent_markdown
    for operation in sorted(located, key=lambda op: op["start"], reverse=True):
        revised = revised[:operation["start"]] + operation["after"] + revised[operation["end"]:]
    if not revised:
        _fail("text_amendment_result_empty")
    try:
        result_bytes = revised.encode("utf-8")
    except UnicodeEncodeError:
        _fail("text_amendment_result_invalid")
    if len(result_bytes) > _MAX_RESULT_BYTES:
        _fail("text_amendment_result_too_large")

    proof = {
        "mode": "exact_original_offsets",
        "operations": located,
        "parent_markdown_sha256": parent_sha,
        "result_markdown_sha256": _sha_text(revised),
    }
    return revised, proof

