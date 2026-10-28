"""Frozen line edits and the local lifecycle of source-bound explanations."""
import html
import json
import re

import addressed_amendments
from bridge_contract import BridgeError
from explanation_tool import ExplanationError, validate_arguments, svg_capabilities
from model_material import model_material

_CALL_ID = re.compile(r'[a-z][a-z0-9_]{0,30}\Z')
_MARKER_PREFIX = re.compile(r'<!--\s*/?\s*vaa-explanation\s*:', re.I)
_MARKER = re.compile(r'<!--(?P<close>/?)vaa-explanation:(?P<id>[a-z][a-z0-9_]{0,30})-->')
_HTML = re.compile(r'<\s*(?:!|\?|/?[a-zA-Z][^>]*>)')
_DANGEROUS_LINK = re.compile(r'(?:\]\(|\]:|<)<?(?:javascript|vbscript|data|file):', re.I)


def _fail(code):
    raise BridgeError('explanation_amendment_' + code)


def _segment_count(material):
    try:
        segments = material['transcription']['segments']
    except (KeyError, TypeError):
        _fail('material_invalid')
    if not isinstance(segments, list):
        _fail('material_invalid')
    return len(segments)


def _arguments(value, segment_count):
    try:
        return validate_arguments(value, segment_count)
    except ExplanationError as exc:
        raise BridgeError('explanation_amendment_arguments_invalid') from exc


def _prepare(parent, bound_explanations, segment_count):
    try:
        rows = addressed_amendments._lines(parent)
    except (AttributeError, UnicodeEncodeError):
        _fail('parent_invalid')
    if not isinstance(bound_explanations, list) or len(bound_explanations) > 16:
        _fail('bindings_invalid')
    bound = {}
    for entry in bound_explanations:
        if not isinstance(entry, dict) or set(entry) != {'call_id', 'arguments'}:
            _fail('binding_invalid')
        call_id = entry['call_id']
        if not isinstance(call_id, str) or not _CALL_ID.fullmatch(call_id) or call_id in bound:
            _fail('binding_invalid')
        bound[call_id] = _arguments(entry['arguments'], segment_count)

    blocks, opened = {}, None
    for prefix in _MARKER_PREFIX.finditer(parent):
        match = _MARKER.match(parent, prefix.start())
        if match is None or match['id'] not in bound:
            _fail('parent_block_invalid')
        call_id = match['id']
        if not match['close']:
            if opened is not None or call_id in blocks:
                _fail('parent_block_not_unique')
            opened = (call_id, match.start())
        else:
            if opened is None or opened[0] != call_id:
                _fail('parent_block_invalid')
            blocks[call_id] = {'start': opened[1], 'end': match.end()}
            opened = None
    if opened is not None or set(blocks) != set(bound):
        _fail('parent_block_invalid')
    return rows, bound, blocks


def _protected(start, end, blocks):
    return any(start < block['end'] and end > block['start'] for block in blocks.values())


def _safe_text(value):
    if not isinstance(value, str):
        _fail('replacement_markup_forbidden')
    # Normalize only for detection. Preserve the exact accepted replacement bytes.
    # Markdown can unescape a destination's colon before handing it to the browser.
    target_text = html.unescape(value).replace('\\:', ':')
    target_text = re.sub(r'[\s\x00-\x20\x7f-\x9f]', '', target_text)
    if (not isinstance(value, str) or 'vaa-explanation' in value.lower()
            or re.search(r'\[\[\s*tool\s*:', value, re.I)
            or '![' in value or _HTML.search(value) or _DANGEROUS_LINK.search(target_text)):
        _fail('replacement_markup_forbidden')


def _safe_arguments(value, segment_count):
    arguments = _arguments(value, segment_count)
    for key in ('title', 'purpose', 'subject'):
        _safe_text(arguments[key])
    for panel in arguments['panels']:
        _safe_text(panel['title'])
        _safe_text(panel['explanation'])
    return arguments


def _capacity(bound, reserved_call_ids, max_new_explanations):
    if reserved_call_ids is None:
        reserved_call_ids = []
    if (not isinstance(reserved_call_ids, list)
            or any(not isinstance(value, str) or not _CALL_ID.fullmatch(value) for value in reserved_call_ids)
            or len(set(reserved_call_ids)) != len(reserved_call_ids)):
        _fail('reserved_ids_invalid')
    occupied = set(reserved_call_ids) | set(bound)
    remaining = max(0, 2 - len(occupied))
    if max_new_explanations is not None:
        if type(max_new_explanations) is not int or not 0 <= max_new_explanations <= 2:
            _fail('capacity_invalid')
        remaining = min(remaining, max_new_explanations)
    return occupied, remaining


def _placeholder(call_id):
    return f'<!--vaa-explanation:{call_id}-->\n[[TOOL:{call_id}]]\n<!--/vaa-explanation:{call_id}-->'


def build_prompt(binding, parent, material, packet=None):
    """Show frozen addresses and parameter ownership, with no permission to rewrite owned blocks."""
    if packet is None:
        packet = {}
    if not isinstance(packet, dict):
        _fail('bindings_invalid')
    rows, bound, blocks = _prepare(parent, packet.get('bound_explanations', []), _segment_count(material))
    _, remaining = _capacity(bound, packet.get('reserved_call_ids'), packet.get('max_new_explanations'))
    visual = {key: value for key, value in packet.items()
              if key not in ('bound_explanations', 'reserved_call_ids', 'max_new_explanations')}
    if packet.get('generation_region_projection') is not None:
        from native_purposes import _readable_layers
        visual = {'role': '历史保存帧的未核验机器分层观察，本次没有重新看图',
                  'new_visual_calls': 0,
                  'observations': _readable_layers(packet['generation_region_projection'])}
    context = {
        'original_user_request': binding['user_request'],
        'requested_material': model_material(material),
        'saved_visual_packet': visual or None,
        'parent_lines': [{'line_id': row['line_id'], 'text': row['text'],
                          'editable': not _protected(row['start'], row['end'], blocks)} for row in rows],
        'bound_explanations': [{'call_id': call_id, 'arguments': arguments} for call_id, arguments in bound.items()],
        'max_new_explanations': remaining,
        'svg_capabilities': svg_capabilities(),
        'revision_instruction': binding['revision_request']['instruction'],
    }
    return (
        '对同一视频的已保存原稿做局部修改，其余内容逐字保留。原稿、图和历史机器观察不是事实权威；'
        '讲者观点、解释性构造例和建议必须区分，不能新增视频事实或补造原画面。'
        '只返回严格JSON：{"edits":[{"line_id":"L0001","after":"整行替换文字"}],'
        '"explanation_updates":[],"new_explanations":[],"explanation_removals":[]}。'
        '四数组各可0条，合计至少1项；edits最多16条。当前可用render_explanation工具完整arguments为'
        '{"title":"标题≤100字符","purpose":"用途≤500字符","subject":"同一对象≤120字符",'
        '"panels":[{"id":"ASCII标识","title":"面板标题≤80字符","explanation":"说明≤1800字符",'
        '"provenance":"source或illustrative","evidence":[1],"svg":"完整SVG字符串"}]}。'
        'explanation_updates每项恰有call_id与arguments，只能更新已有调用，每个call_id最多一次；'
        'explanation_removals是要整块删除的已有call_id数组，不重复且不能同时更新该调用。'
        'new_explanations每项恰有after_line_id与完整arguments，在该父稿行末端插入新解释，call_id由运行时生成。'
        '插入目标须为editable=true、非空且不超过4000字符的父稿行，不能插在解释块内部或标记上。'
        '新增最多2条且不超过max_new_explanations加本次移除数；每个插入行最多一次，不得同时用edits修改该行。'
        '工具调用额度只是执行上限，不能据此倒推用户需要。按本次请求比较文字与图，哪个足够清楚就用哪个。'
        'edits只能选editable=true的非空原行，不能改解释块内部或标记；after最多8000字符，可含换行，删除用空串。'
        '图或图旁说明的修改必须返回该调用完整新arguments，未改字段照抄；只改完成用户要求所需的部分。'
        'arguments遵守原有字段与SVG约束；1至6个panel，id唯一且不超31个ASCII字符，所有SVG保持相同viewBox。'
        'source仍是依据本次材料整理的示意，不是原画面；illustrative是构造例。evidence只引用本次有效evidence_index。'
        'SVG不得有脚本、HTML、外链、事件或动画。普通文字不得注入HTML、图片、TOOL占位或解释块标记。'
        '不要返回原句、身份或哈希，不加代码围栏；图改动仅为候选，不宣称质量通过。输入：\n'
        + json.dumps(context, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    )


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail('json_invalid')
        result[key] = value
    return result


def _constant(_value):
    _fail('json_invalid')


def apply_reply(reply, parent, *, request_id, source_sha256, bound_explanations, segment_count,
                reserved_call_ids=None, max_new_explanations=None):
    """Apply every operation against the same frozen parent; render updated blocks later."""
    rows, bound, blocks = _prepare(parent, bound_explanations, segment_count)
    occupied, remaining = _capacity(bound, reserved_call_ids, max_new_explanations)
    if not isinstance(request_id, str) or not request_id or not isinstance(source_sha256, str) or not source_sha256:
        _fail('identity_invalid')
    try:
        payload = json.loads(reply, object_pairs_hook=_object, parse_constant=_constant)
    except (ValueError, TypeError, RecursionError):
        _fail('json_invalid')
    if (not isinstance(payload, dict) or 'edits' not in payload
            or set(payload) - {'edits', 'explanation_updates', 'new_explanations', 'explanation_removals'}):
        _fail('schema_invalid')
    edits, updates = payload['edits'], payload.get('explanation_updates', [])
    additions, removals = payload.get('new_explanations', []), payload.get('explanation_removals', [])
    if (not isinstance(edits, list) or len(edits) > 16 or not isinstance(updates, list)
            or len(updates) > len(bound) or not isinstance(additions, list) or len(additions) > 2
            or not isinstance(removals, list) or len(removals) > len(bound)
            or not (edits or updates or additions or removals)):
        _fail('operations_invalid')
    operations = []
    if edits:
        _, line_proof = addressed_amendments.apply_reply(
            json.dumps({'edits': edits}, ensure_ascii=False, allow_nan=False), parent,
            request_id=request_id, source_sha256=source_sha256)
        for operation in line_proof['operations']:
            if _protected(operation['start'], operation['end'], blocks):
                _fail('owned_block_edit_forbidden')
            _safe_text(operation['after'])
            operations.append({'kind': 'line', **operation})

    canonical_updates, seen = [], set()
    for update in updates:
        if not isinstance(update, dict) or set(update) != {'call_id', 'arguments'}:
            _fail('update_invalid')
        call_id = update['call_id']
        if not isinstance(call_id, str) or call_id not in bound or call_id in seen:
            _fail('update_address_invalid')
        arguments = _safe_arguments(update['arguments'], segment_count)
        if arguments == bound[call_id]:
            _fail('update_noop')
        seen.add(call_id)
        canonical_updates.append({'call_id': call_id, 'arguments': arguments})
        block = blocks[call_id]
        operations.append({'kind': 'explanation', 'call_id': call_id, **block,
                           'before': parent[block['start']:block['end']],
                           'after': _placeholder(call_id)})

    canonical_removals = []
    for call_id in removals:
        if (not isinstance(call_id, str) or call_id not in bound
                or call_id in seen or call_id in canonical_removals):
            _fail('removal_address_invalid')
        canonical_removals.append(call_id)
        block = blocks[call_id]
        operations.append({'kind': 'explanation_removal', 'call_id': call_id, **block,
                           'before': parent[block['start']:block['end']], 'after': ''})
    if len(additions) > remaining + len(canonical_removals):
        _fail('capacity_exceeded')

    row_index = {row['line_id']: row for row in rows}
    edited_lines = {operation['line_id'] for operation in operations if operation['kind'] == 'line'}
    canonical_additions, added_lines = [], set()
    for addition in additions:
        if not isinstance(addition, dict) or set(addition) != {'after_line_id', 'arguments'}:
            _fail('addition_invalid')
        line_id = addition['after_line_id']
        if (not isinstance(line_id, str) or line_id not in row_index
                or line_id in added_lines or line_id in edited_lines):
            _fail('addition_address_invalid')
        row = row_index[line_id]
        if (not row['text'].strip() or len(row['text']) > 4000
                or _protected(row['start'], row['end'], blocks)):
            _fail('addition_address_invalid')
        arguments = _safe_arguments(addition['arguments'], segment_count)
        number = 1
        while f'added_explanation_{number}' in occupied:
            number += 1
        call_id = f'added_explanation_{number}'
        if not _CALL_ID.fullmatch(call_id):
            _fail('addition_id_exhausted')
        occupied.add(call_id)
        added_lines.add(line_id)
        canonical_additions.append({'call_id': call_id, 'after_line_id': line_id, 'arguments': arguments})
        operations.append({'kind': 'explanation_addition', 'call_id': call_id, 'after_line_id': line_id,
                           'start': row['end'], 'end': row['end'], 'before': '',
                           'after': '\n' + _placeholder(call_id)})
    revised = parent
    for operation in sorted(operations, key=lambda item: item['start'], reverse=True):
        revised = revised[:operation['start']] + operation['after'] + revised[operation['end']:]
    try:
        if not revised.strip() or len(revised.encode('utf-8')) > 400000:
            _fail('result_invalid')
    except UnicodeEncodeError:
        _fail('result_invalid')
    return revised, {'mode': 'frozen_explanation_edits', 'request_id': request_id,
                     'source_sha256': source_sha256, 'operations': operations,
                     'explanation_updates': canonical_updates,
                     'explanation_additions': canonical_additions,
                     'explanation_removals': canonical_removals,
                     'parent_markdown_sha256': addressed_amendments._sha(parent),
                     'result_markdown_sha256': addressed_amendments._sha(revised)}
