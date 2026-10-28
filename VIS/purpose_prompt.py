"""Adapt the pinned template, never replace text inside source material or requests."""
from bridge_contract import BridgeError

_RULES = {
    "你是一个专业的笔记助手，擅长将视频转录内容整理成清晰、有条理且信息丰富的笔记。":
        "你按本次用户指定的用途整理视频材料，交付可直接使用的内容。",
    "1. **完整信息**：记录尽可能多的相关细节，确保内容全面。":
        "1. **目标内完整**：先遵守本次用户要求的用途、范围、篇幅和格式，只保留完成该目的必需的信息。明确要求详细时保留充分细节；明确要求简短时不扩写背景、评价和其它方法。",
    "3. **保留关键细节**：保留重要事实、示例、结论和建议。(如果额外重要的任务有格式需求可以不遵守)":
        "3. **保留关键细节**：保留所选目标必需的前提、关键动作与顺序、结果和限制；不能为了简短省掉必要筛选与选择，也不添加未要求的例子或建议。",
    "5. 视频中提及的数学公式必须保留，并以 LaTeX 语法形式呈现，适合 Markdown 渲染。":
        "5. 与本次用途相关的数学公式以 LaTeX 语法保留，不为覆盖全部视频而加入无关公式。",
}
_LIST_START = '请注意，在生成 Markdown 时，避免将编号标题'
_LIST_END = '视频分段（格式：开始时间 - 内容）：'
_LIST_RULE = '章节使用 Markdown 标题，顺序步骤使用标准有序列表（如 1. 操作），不要转义列表编号。\n\n'


def bound_prompt_version(client, identity):
    version = getattr(client, 'purpose_prompt_version', None)
    if version is None:
        ledger = getattr(getattr(client, 'service', None), 'ledger', None)
        getter = getattr(ledger, 'scope_value', None)
        version = getter(identity).get('native_profile', {}).get('version', 1) if callable(getter) else 1
    if type(version) is not int or version not in (1, 2, 3, 4, 5, 6):
        raise BridgeError('unsupported_native_purpose_prompt')
    return version


def _adapt_template(template):
    if any(template.count(rule) != 1 for rule in _RULES):
        raise BridgeError('native_purpose_template_mismatch')
    start, end = template.find(_LIST_START), template.find(_LIST_END)
    if start < 0 or end <= start:
        raise BridgeError('native_purpose_template_mismatch')
    adapted = template[:start] + _LIST_RULE + template[end:]
    for old, new in _RULES.items():
        adapted = adapted.replace(old, new, 1)
    return adapted


def adapt_messages(messages, template, *, video_title, segment_text, tags):
    """Replace only the native-rendered template prefix; retain the entire suffix."""
    fields = {'video_title': video_title, 'segment_text': segment_text, 'tags': tags}
    before = template.format(**fields)
    after = _adapt_template(template).format(**fields)
    if len(messages) != 1 or messages[0].get('role') != 'user':
        raise BridgeError('native_purpose_template_mismatch')

    def replace_prefix(text):
        if not isinstance(text, str) or not text.startswith(before):
            raise BridgeError('native_purpose_template_mismatch')
        return after + text[len(before):]

    content = messages[0].get('content')
    if isinstance(content, str):
        content = replace_prefix(content)
    elif (isinstance(content, list) and content and content[0].get('type') == 'text'):
        content = [{**content[0], 'text': replace_prefix(content[0].get('text'))}, *content[1:]]
    else:
        raise BridgeError('native_purpose_template_mismatch')
    return [{**messages[0], 'content': content}]


def purpose_gpt_class(original, template):
    class PurposeGPT(original):
        def create_messages(self, segments, **kwargs):
            messages = super().create_messages(segments, **kwargs)
            return adapt_messages(messages, template, video_title=kwargs.get('title'),
                segment_text=self._build_segment_text(segments), tags=kwargs.get('tags'))
    return PurposeGPT
