"""Bounded replacements at runtime-owned addresses in an immutable parent."""
import hashlib
import json

from bridge_contract import BridgeError

from model_material import model_material


def _fail(code):
    raise BridgeError('addressed_amendment_' + code)


def _sha(text):
    try:
        return hashlib.sha256(text.encode('utf-8')).hexdigest()
    except (AttributeError, UnicodeEncodeError):
        _fail('text_invalid')


def _lines(parent):
    if not isinstance(parent, str) or not parent or len(parent.encode('utf-8')) > 400000:
        _fail('parent_invalid')
    result = []
    start = 0
    for number, value in enumerate(parent.splitlines(keepends=True), 1):
        text = value.rstrip('\r\n')
        result.append({'line_id': f'L{number:04d}', 'text': text, 'start': start, 'end': start+len(text)})
        start += len(value)
    return result


def build_prompt(binding, parent, material, packet=None):
    if packet is not None and packet.get('generation_region_projection') is not None:
        from native_purposes import _readable_layers
        packet = {'role': '历史保存帧的未核验机器分层观察，本次没有重新看图',
                  'new_visual_calls': 0,
                  'observations': _readable_layers(packet['generation_region_projection'])}
    context = {
        'original_user_request': binding['user_request'],
        'requested_material': model_material(material),
        'saved_visual_packet': packet,
        'parent_lines': [{'line_id': row['line_id'], 'text': row['text']} for row in _lines(parent)],
        'revision_instruction': binding['revision_request']['instruction'],
    }
    return (
        '对已保存内容做局部修订，保留其他原文。原稿与视觉观察都不是事实权威，材料须按其角色理解。'
        '按最后的 revision_instruction 完成必要修改，不重写整篇，不把讲者观点或产品宣介当作已验证事实；'
        '恢复解释所需的联系与限定即可，不扩成长稿。只返回严格JSON：'
        '{"edits":[{"line_id":"L0001","after":"替换该整行的新文字"}]}。'
        '只返回1至16条edit，每条恰有line_id和after。line_id从父稿抄取，不回显原句、哈希或请求身份。'
        '每个编号最多一次；只能修改非空且不超过4000字符的行；after最多8000字符，可含换行用于补充紧邻说明，'
        '删除时用空字符串；不能修改未请求的位置。不要围栏或解释。输入：\n'
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


def apply_reply(reply, parent, *, request_id, source_sha256):
    """Addresses are interpreted only against this call's frozen parent, never a newer file."""
    rows = {row['line_id']: row for row in _lines(parent)}
    if not isinstance(request_id, str) or not isinstance(source_sha256, str):
        _fail('identity_invalid')
    try:
        payload = json.loads(reply, object_pairs_hook=_object, parse_constant=_constant)
    except (ValueError, TypeError, RecursionError):
        _fail('json_invalid')
    if not isinstance(payload, dict) or set(payload) != {'edits'}:
        _fail('schema_invalid')
    edits = payload['edits']
    if not isinstance(edits, list) or not 1 <= len(edits) <= 16:
        _fail('edits_invalid')
    operations, seen = [], set()
    for edit in edits:
        if not isinstance(edit, dict) or set(edit) != {'line_id', 'after'}:
            _fail('edit_invalid')
        line_id, after = edit['line_id'], edit['after']
        if not isinstance(line_id, str) or line_id not in rows or line_id in seen:
            _fail('address_invalid')
        row = rows[line_id]
        before = row['text']
        if not before.strip() or len(before) > 4000:
            _fail('address_invalid')
        if not isinstance(after, str) or len(after) > 8000 or before == after:
            _fail('replacement_invalid')
        seen.add(line_id)
        operations.append({'line_id': line_id, 'before': before, 'after': after,
                           'start': row['start'], 'end': row['end']})
    revised = parent
    for op in sorted(operations, key=lambda value: value['start'], reverse=True):
        revised = revised[:op['start']] + op['after'] + revised[op['end']:]
    if not revised.strip():
        _fail('result_empty')
    try:
        size = len(revised.encode('utf-8'))
    except UnicodeEncodeError:
        _fail('text_invalid')
    if size > 400000:
        _fail('result_too_large')
    return revised, {'mode': 'frozen_line_offsets', 'request_id': request_id,
                     'source_sha256': source_sha256, 'operations': operations,
                     'parent_markdown_sha256': _sha(parent), 'result_markdown_sha256': _sha(revised)}
