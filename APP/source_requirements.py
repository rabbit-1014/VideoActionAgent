"""A narrow source-sufficiency guard for explicit complete, exact requests.

This recognizes literal requirements, not every intent. Partial delivery remains
possible when requested; a missing match never proves that the material is enough.
"""
import re

_PARTIAL = re.compile(
    r"(?:能确认|已确认|已知|确定的).{0,12}(?:先给|先提供|即可|就行|先整理)"
    r"|(?:先|只|仅).{0,6}(?:整理|提供|给出|给我).{0,6}(?:已确认|能确认|已知|确定的)"
    r"|(?:允许|接受).{0,4}部分|部分(?:即可|也可以|就行)"
    r"|(?:不必|无需|不需要|不用).{0,3}完整(?!视频|音频|字幕|转写|原文|课程)|可以不完整"
)
_COMPLETE_EXACT = re.compile(
    r"(?:全部|所有|完整)[^，,。；;！？!?\n]{0,24}(?:准确|精确)(?:的)?(?:名称|参数|数值|步骤|操作稿)"
)
_NEGATED = re.compile(r"(?:不需要|不要求|不是要|无需|不必|不用|不要).{0,5}$")
_PARTIAL_DENIED = re.compile(r"(?:不|拒绝).{0,5}$")


def complete_exact_requirement(request):
    """Return the user's literal constraint, or None; never infer a new goal."""
    for clause in re.split(r"[，,。；;！？!?\n]", request):
        for partial in _PARTIAL.finditer(clause):
            if not _PARTIAL_DENIED.search(clause[:partial.start()]):
                return None
    for clause in re.split(r"[，,。；;！？!?\n]", request):
        match = _COMPLETE_EXACT.search(clause)
        if match and not _NEGATED.search(clause[:match.start()]):
            return match.group(0)
    return None
