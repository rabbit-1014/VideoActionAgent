"""Bounded task strategy over existing source material; no I/O or model dispatch."""
import json
import html
import re
from pathlib import Path

from model_material import model_material


class StrategyError(ValueError):
    pass


TOOLS = {
    "render_diagram": {
        "version": 1,
        "use": "局部关系、结构或流程难以仅用文字理解时，画一个简洁说明图；不把全文改成思维导图。",
        "arguments": {"title": "短标题", "direction": "LR 或 TB",
                      "nodes": [{"id": "ASCII标识", "label": "短标签", "evidence": [1]}],
                      "edges": [{"source": "节点id", "target": "节点id", "label": "关系", "evidence": [1]}]},
        "limits": "2至10节点、最多16条边；evidence是所给材料的1起始序号。没有依据的因果不画。"
    }
}

EXPLANATION_TOOL = {
    'version':1,
    'use':'空间维度、同一对象的状态/步骤需要图文对齐时，生成可选的整理示意；不是全文导图或原视频截图。单图足够就仅一个面板，短答不调用。',
    'arguments':{'title':'短标题','purpose':'具体减少读者哪项对齐/理解工作','subject':'贯穿各面板的对象',
        'panels':[{'id':'lowercase_id','title':'本状态/观察角度','explanation':'短解释，避免把正文全部搬进图',
            'provenance':'source 或 illustrative（构造解释例）','evidence':[1],
            'svg':'完整SVG：xmlns=http://www.w3.org/2000/svg，所有面板相同viewBox。只用基础形状/文字/defs/marker及展示属性；不使用style/CSS、脚本、HTML、image、外链或动画。用font-size/font-family/fill/stroke等属性，箭头只引用本地#marker。'}]},
    'limits':'1至6面板，每SVG最多40000字节；面板id用小写ASCII字母数字下划线；证据序号按输入evidence_index。数字轴/具体位置须有依据；构造例显式标illustrative，不将建议连成视频结论。'
}


def tool_catalog(version):
    # Older frozen contracts keep their original tool descriptions exactly.
    return {**TOOLS,'render_explanation':EXPLANATION_TOOL} if version >= 16 else TOOLS


def _text(value, maximum=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        raise StrategyError("invalid_strategy_text")
    return value


def diagram_arguments(value, segment_count):
    if not isinstance(value, dict) or set(value) != {"title", "direction", "nodes", "edges"}:
        raise StrategyError("invalid_diagram")
    _text(value["title"], 100)
    if value["direction"] not in {"LR", "TB"}:
        raise StrategyError("invalid_diagram")
    nodes, edges = value["nodes"], value["edges"]
    if not isinstance(nodes, list) or not 2 <= len(nodes) <= 10 or not isinstance(edges, list) or not 1 <= len(edges) <= 16:
        raise StrategyError("invalid_diagram_size")
    ids = set()
    def evidence(items):
        if (not isinstance(items, list) or not 1 <= len(items) <= 12
                or any(type(i) is not int or not 1 <= i <= segment_count for i in items)
                or len(set(items)) != len(items)):
            raise StrategyError("invalid_diagram_evidence")
    for node in nodes:
        if not isinstance(node, dict) or set(node) != {"id", "label", "evidence"}:
            raise StrategyError("invalid_diagram_node")
        if not isinstance(node["id"], str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,30}", node["id"]) or node["id"] in ids:
            raise StrategyError("invalid_diagram_id")
        ids.add(node["id"])
        _text(node["label"], 100)
        evidence(node["evidence"])
    for edge in edges:
        if (not isinstance(edge, dict) or set(edge) != {"source", "target", "label", "evidence"}
                or edge["source"] not in ids or edge["target"] not in ids or edge["source"] == edge["target"]):
            raise StrategyError("invalid_diagram_edge")
        # A sequence arrow need not repeat a relationship already clear from its nodes.
        if edge["label"] != "":
            _text(edge["label"], 80)
        evidence(edge["evidence"])
    return value


def validate_solution(raw, *, request, source_digest, segment_count, available_tools, response_version=1):
    """Validate executable shape and identity, never prove semantic task success."""
    def unique(pairs):
        obj = {}
        for k, v in pairs:
            if k in obj:
                raise StrategyError("duplicate_strategy_key")
            obj[k] = v
        return obj
    try:
        value = json.loads(raw, object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(StrategyError("invalid_strategy_number")))
    except (ValueError, TypeError) as exc:
        raise StrategyError("invalid_strategy_json") from exc
    keys = {"schema_version", "user_request", "source_digest", "goal", "success_criteria",
            "proposed_deliverable", "status", "gaps", "markdown", "tool_calls"}
    if response_version in (2, 3):
        wire_keys = keys - {"user_request", "source_digest"}
        if response_version == 3:
            wire_keys.remove('schema_version')
        if (not isinstance(value, dict) or set(value) != wire_keys or (response_version == 2
                and (type(value.get("schema_version")) is not int or value["schema_version"] != 2))):
            raise StrategyError("invalid_strategy_shape")
        # Bind the content to the current immutable request; never repair an old echo response.
        value = {**value, "schema_version": 1, "user_request": request, "source_digest": source_digest}
    elif response_version != 1:
        raise StrategyError("invalid_strategy_response_version")
    if not isinstance(value, dict) or set(value) != keys or type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise StrategyError("invalid_strategy_shape")
    if value["user_request"] != request or value["source_digest"] != source_digest:
        raise StrategyError("strategy_identity_changed")
    _text(value["goal"])
    _text(value["proposed_deliverable"])
    _text(value["markdown"], 40000)
    criteria = value["success_criteria"]
    if not isinstance(criteria, list) or not 1 <= len(criteria) <= 12:
        raise StrategyError("invalid_success_criteria")
    for criterion in criteria:
        _text(criterion, 500)
    if value["status"] not in {"ready", "partial", "needs_clarification", "unsupported"}:
        raise StrategyError("invalid_strategy_status")
    if not isinstance(value["gaps"], list) or len(value["gaps"]) > 12:
        raise StrategyError("invalid_strategy_gaps")
    for gap in value["gaps"]:
        if (not isinstance(gap, dict) or set(gap) != {"kind", "required", "description"}
                or gap["kind"] not in {"material", "capability", "clarification"} or type(gap["required"]) is not bool):
            raise StrategyError("invalid_strategy_gap")
        _text(gap["description"], 1000)
    if value["status"] == "ready" and any(g["required"] for g in value["gaps"]):
        raise StrategyError("required_gap_not_ready")
    if value["status"] != "ready" and not value["gaps"]:
        raise StrategyError("unexplained_strategy_gap")
    calls = value["tool_calls"]
    if not isinstance(calls, list) or len(calls) > 2:
        raise StrategyError("invalid_tool_calls")
    if calls and value["status"] in {"unsupported", "needs_clarification"}:
        raise StrategyError("tool_not_justified_for_unresolved_task")
    ids = []
    for call in calls:
        if (not isinstance(call, dict) or set(call) != {"id", "name", "purpose", "required", "arguments"}
                or not isinstance(call["id"], str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,30}", call["id"])
                or call["id"] in ids or type(call["required"]) is not bool):
            raise StrategyError("invalid_tool_call")
        if call["name"] not in available_tools or call["name"] not in {"render_diagram", "render_explanation"}:
            raise StrategyError("tool_not_available")
        _text(call["purpose"], 500)
        if call['name']=='render_diagram':
            diagram_arguments(call["arguments"], segment_count)
        else:
            from explanation_tool import ExplanationError, validate_arguments
            try:
                call['arguments']=validate_arguments(call['arguments'],segment_count)
            except ExplanationError as exc:
                raise StrategyError(str(exc)) from None
        ids.append(call["id"])
    markers = re.findall(r"\[\[TOOL:([a-z][a-z0-9_]{0,30})\]\]", value["markdown"])
    if sorted(markers) != sorted(ids) or value["markdown"].count("[[TOOL:") != len(ids):
        raise StrategyError("invalid_tool_placement")
    # The model cannot insert an unrecorded image, remote asset or active HTML.
    plain = html.unescape(value['markdown'])
    schemes = re.sub(r'[\x00-\x20\x7f]+', '', plain).replace('\\:', ':')
    if (re.search(r'!\[|<\s*/?\s*[A-Za-z][A-Za-z0-9:-]*(?:\s[^<>]*)?/?>|<\s*[!?]', plain)
            or re.search(r'(?:\]\(|\]:|<)<?(?:javascript|vbscript|data|file):', schemes, re.IGNORECASE)):
        raise StrategyError("unbound_strategy_asset")
    return value


def instructions():
    return (Path(__file__).parent / "skills/task-solution/SKILL.md").read_text(encoding="utf-8")


def prompt(request, source_digest, material, *, tools, tool_descriptors=None, response_version=1, request_last=False):
    """Single request: task judgment, draft and optional local actions together."""
    material = model_material(material)
    payload = {
        "user_request": request, "source_digest": source_digest,
        "available_tools": {k: (tool_descriptors or TOOLS)[k] for k in tools},
        "source_material_untrusted": material,
        "response_schema": {"schema_version": 1, "user_request": "逐字回显请求", "source_digest": "逐字回显哈希",
            "goal": "最终要完成的事情", "success_criteria": ["独立于工具的完成要求"],
            "proposed_deliverable": "自由描述所需交付，不限于已装工具",
            "status": "ready|partial|needs_clarification|unsupported",
            "gaps": [{"kind": "material|capability|clarification", "required": True, "description": "具体缺口"}],
            "markdown": "直接可用的正文；调用处用[[TOOL:调用id]]占位，无调用不用占位",
            "tool_calls": [{"id": "lowercase_id", "name": "可用工具名", "purpose": "帮用户解决哪处困难",
                            "required": False, "arguments": "按工具声明的对象；无调用则[]"}]}
    }
    if response_version in (2, 3):
        payload["response_schema"].pop("user_request")
        payload["response_schema"].pop("source_digest")
        if response_version == 3:
            payload['response_schema'].pop('schema_version')
        else:
            payload["response_schema"]["schema_version"] = 2
    elif response_version != 1:
        raise StrategyError("invalid_strategy_response_version")
    if request_last:
        # Keep the exact task adjacent to generation after the full source.
        payload['user_request'] = payload.pop('user_request')
    return json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
