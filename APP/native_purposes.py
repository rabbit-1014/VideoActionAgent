"""Purpose hints over pinned BiliNote styles; no new note generator or task planner."""
import re

from bridge_contract import digest
from visual_text_context import text_context_hint

PROFILES = {
    "structured_notes": {"style": "tutorial", "instructions": "按用户要求形成可直接阅读的笔记。沿用标题、目录和主题组织，只有确有帮助才放图片。"},
    "source_answer": {"style": "minimal", "instructions": "直接回答用户关于此视频的问题，必要时分回答与材料不足两部分；不生成课程大纲或无关笔记。视觉模型观察可能误识，不能把含混文字确认成代码。若画面为空白或未展示完整实现，明确无法从画面恢复，不用口述或常识冒充画面已显示的内容。"},
    "method_list": {"style": "task_oriented", "instructions": "按用户目标整理适用条件、操作步骤、关键筛选/排序/选择与检查点。保留动作衔接；未取得完整代码或参数就说明缺口，不把概述称为可复制运行。视觉模型候选与讲者口述分开，不确定的细节不要补造。"},
}

PURPOSE_RULES = (
    "本次用户的用途、范围、篇幅和格式优先于默认笔记模板。"
    "只交付所要求的成品，不附生成说明、效率评价、宣传或材料中未给出的建议。"
    "简短操作卡围绕准备、操作、检查；一页复习围绕主流程、易混点和关键顺序；"
    "详细解释则保留所问机制、例子与区别，不能一律压短。"
    "只列影响本次使用的材料缺口，不罗列所有未获取的图片、代码或未请求的操作。"
)


ONE_PAGE = (
    "本次采用单页复习版式；用户明确要求的内容和结构优先，以下仅是未指定部分的默认布局："
    "一个明确标题、简短目录；适合流程型内容时用‘输入/处理/输出’表概括约三个主要阶段；"
    "挑约三条易混概念或关键提醒，按需说明操作顺序，不为凑满布局添加内容。"
    "明确要求的步骤、例子、区别和必要数值不能因行数或篇幅默认值而删除。"
    "保留材料中完成本次任务必需的用量、时长、温度、设置或阈值；"
    "只省略用户未要求且不影响本次使用的接口细枝末节、重复解释和额外背景。"
    "没有给出的数值不补造，含混处按材料说明。最后仅列确实影响使用的缺口。"
    "正文以约800至1200个中文字符为排版目标（不是事实或质量证明），"
    "用户更具体的篇幅要求优先；对原稿去重后只交付最终Markdown，不输出自检说明。"
)


def one_page_requested(request):
    for match in re.finditer(r"(?:一页|单页)(?:式)?(?:复习|笔记)", request):
        clause = re.split(r"[。！？，,；;\n]", request[:match.start()])[-1]
        if not re.search(r"(?:不要|不需要|不必|不用|无需|别)(?:压缩|限制|整理|写|做|成|为|到|在)*$", clause):
            return True
    return False


def explicit_audio_only(request):
    remaining = re.sub(r"(?:不需要|无需|不要|不用)(?:添加|附加|插入|提供|附|加)?(?:图片|截图|画面)", "", request)
    if re.search(r"画面|图像|视觉|截图|图片", remaining):
        return False
    pattern = r"(?:只|仅)(?:根据|依据|按)(?:讲者|这段|本段|视频|已保存的)?(?:声音|语音)(?:文字|转写|转录)[^。！？，,；;\n]*"
    return any(not re.search(r"画面|图像|视觉|截图", match.group())
               for match in re.finditer(pattern, request))


def profile(kind, request=""):
    value = {"version": 2, **PROFILES[kind]}
    value["instructions"] += PURPOSE_RULES
    if kind == "structured_notes" and one_page_requested(request):
        value.update(version=5, style="minimal", instructions=value["instructions"]+ONE_PAGE)
    return {**value, "sha256": digest(value)}


def validate_profile(value, kind):
    if (not isinstance(value, dict) or set(value) != {"version", "style", "instructions", "sha256"}
            or value["style"] not in {"tutorial", "minimal", "task_oriented"}
            or type(value["version"]) is not int or value["version"] < 1
            or not isinstance(value["instructions"], str) or not 1 <= len(value["instructions"]) <= 4000
            or digest({k:v for k,v in value.items() if k != "sha256"}) != value["sha256"]
            or kind not in PROFILES):
        raise ValueError("invalid_native_profile")


def figures_allowed(binding):
    request = binding["user_request"] + "\n" + binding.get("revision_request", {}).get("instruction", "")
    return not any(term in request for term in ("纯文字", "不要截图", "不加图片", "不要插图", "不插图"))


def _readable_layers(regions):
    """Content view only; the pinned machine observation remains unchanged."""
    labels = {'editor_interface': '编辑器界面', 'preview_content': '素材预览',
              'overlay': '录屏覆盖层', 'unknown': '尚不能判断'}
    frames = []
    for frame in regions['frames']:
        texts = [{'文字': item['text'], '图层候选': labels[item['role']],
                  '原文字框位置': item['bounds']} for item in frame['text_candidates']]
        frames.append({'帧': frame['frame_id'], '是否可见编辑器': frame.get('editor_interface_visible'),
                       '文字观察': texts})
    return {'观察性质': '同源已存帧的机器分层候选，未经人工核验', '画面': frames,
            '限制': ['文字框位置不是界面区域边界；分层候选可能误判。',
                     '静帧不证明整个视频、连续动作、软件版本或素材来源。']}


def extras(binding, *, packet=None, previous=None):
    frozen = binding["native_profile"]
    validate_profile(frozen, binding["output_kind"])
    text = "本次用户要求：" + binding["user_request"] + "\n本次固定用途：" + frozen["instructions"]
    if binding.get('followup_context'):
        from followup_context import text as followup_text
        text += followup_text(binding['followup_context'])
    if packet:
        regions = packet.get('generation_region_projection')
        if regions is not None:
            import json
            if binding.get('goal_policy', {}).get('version', 0) >= 14:
                regions = _readable_layers(regions)
            text += ('\n本次复用同一视频的已存帧并经单独授权重新分析区域，没有重新读取视频或语音识别。'
                     '下列为同帧OCR交叉文字和视觉模型区域候选，区域角色仍未人工核验，'
                     '不能据此证明整个视频、连续动作或软件现行版本。按候选支持程度回答，'
                     'unknown或跨区域文字不作确定归属；位置归属不证明素材来自哪里或是否与教程相关。'
                     '只回答所问类别，不展开无关标识，不回填旧自由视觉描述。\n' +
                     json.dumps(regions, ensure_ascii=False, separators=(',', ':')))
        else:
            text += '\n复用同一视频的已存帧，本次没有重新取帧或调用视觉模型。'
            text += text_context_hint(packet.get('generation_text_projection'))
        if figures_allowed(binding):
            text += "\n仅当有助理解时用[[FRAME:帧ID]]放一张有关图片，不必逐图插入，不在正文展示审计编号。"
    elif binding.get("material_mode") == "saved_visual":
        text += "\n未取得可用的已存画面材料。本次只能依据声音文字，不能声称看到了画面或恢复代码；问题依赖画面时明确材料不足。"
    if previous is not None:
        text += ("\n这是同一原目标的修订，旧稿仅为待改对象，不是额外权威来源。按修改要求改必要处，保留其余有据内容与结构；"
                 "不把修改要求变成新主题，不扩大媒体读取范围。\n修改要求：" + binding["revision_request"]["instruction"] +
                 "\n原稿：\n" + previous)
    return text
