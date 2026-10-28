"""Project only same-frame OCR-corroborated visual text candidates."""
import math
import re
import unicodedata
from copy import deepcopy

_POLICY = "same_frame_ocr_text_intersection_v1"
_ROLE = "unverified_corroborated_text_candidates"
_LIMITS = [
    "视觉模型与OCR仍可能共同误字；一致文字仍是未验证候选。",
    "不能据此确认软件版别、素材属性、动作、颜色或连线。",
]
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_FRAMES = 3
_MAX_TEXT_ITEMS = 32
_MAX_TEXT_LENGTH = 600


def _fail():
    raise ValueError("invalid_visual_grounding_input")


def _mapping(value):
    if not isinstance(value, dict):
        _fail()
    return value


def _text(value, *, nonempty=False):
    if (not isinstance(value, str) or len(value) > _MAX_TEXT_LENGTH
            or (nonempty and not value)):
        _fail()
    return value


def _finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, ValueError):
        return False
def _box(value):
    """Accept JSON XYXY boxes or four-point polygons with finite coordinates."""
    if isinstance(value, dict):
        if set(value) != {"x1", "y1", "x2", "y2"}:
            _fail()
        if not all(_finite_number(value[key]) for key in ("x1", "y1", "x2", "y2")):
            _fail()
        return deepcopy(value)
    if not isinstance(value, list) or len(value) != 4:
        _fail()
    if all(_finite_number(item) for item in value):
        return deepcopy(value)
    if all(isinstance(point, list) and len(point) == 2
           and all(_finite_number(coordinate) for coordinate in point) for point in value):
        return deepcopy(value)
    _fail()


def _normalized(value):
    value = unicodedata.normalize("NFKC", value).casefold()
    keep_punctuation = ".+-/%:"
    return "".join(
        char for char in value
        if not char.isspace()
        and (char in keep_punctuation or not unicodedata.category(char).startswith("P"))
    )
def _validate_observation(observation):
    value = _mapping(observation)
    frames = value.get("frames")
    if not isinstance(frames, list) or len(frames) > _MAX_FRAMES:
        _fail()
    result = []
    seen = set()
    for frame in frames:
        item = _mapping(frame)
        frame_id = item.get("frame_id")
        visible_text = item.get("visible_text")
        if (not isinstance(frame_id, str) or not frame_id or len(frame_id) > 120
                or frame_id in seen or not isinstance(visible_text, list)
                or len(visible_text) > _MAX_TEXT_ITEMS):
            _fail()
        seen.add(frame_id)
        result.append((frame_id, [_text(text) for text in visible_text]))
    return result


def _validate_ocr(ocr_frames):
    if not isinstance(ocr_frames, list) or len(ocr_frames) > _MAX_FRAMES:
        _fail()
    result = {}
    for frame in ocr_frames:
        item = _mapping(frame)
        frame_id = item.get("frame_id")
        frame_sha256 = item.get("frame_sha256")
        state = item.get("state")
        lines = item.get("lines")
        if (not isinstance(frame_id, str) or not frame_id or frame_id in result
                or not isinstance(frame_sha256, str) or not _SHA256.fullmatch(frame_sha256)
                or not isinstance(state, str) or state not in {"available", "empty", "unavailable"}
                or not isinstance(lines, list)):
            _fail()
        clean_lines = []
        for line in lines:
            entry = _mapping(line)
            text = _text(entry.get("text"), nonempty=True)
            box = _box(entry.get("box"))
            score = entry.get("score")
            if "score" not in entry or (score is not None and not _finite_number(score)):
                _fail()
            clean_lines.append((text, box))
        result[frame_id] = (frame_sha256, state, clean_lines)
    return result


def build_text_projection(observation, ocr_frames):
    """Return only exact or one-way substring text matches from the same frame.

    Both model and OCR results remain unverified candidates. Unsupported visual
    descriptions and model-supplied limits are deliberately omitted.
    """
    visual_frames = _validate_observation(observation)
    ocr_by_frame = _validate_ocr(ocr_frames)
    visual_ids = {frame_id for frame_id, _ in visual_frames}
    if visual_ids != set(ocr_by_frame) or len(visual_frames) != len(ocr_by_frame):
        _fail()

    frames = []
    for frame_id, candidates in visual_frames:
        frame_sha256, state, lines = ocr_by_frame[frame_id]
        projected = []
        if state == "available":
            for candidate in candidates:
                normalized_candidate = _normalized(candidate)
                if not normalized_candidate:
                    continue
                for ocr_text, box in lines:
                    normalized_ocr = _normalized(ocr_text)
                    if normalized_ocr and normalized_candidate in normalized_ocr:
                        projected.append({"text": candidate, "box": deepcopy(box)})
                        break
        frames.append({
            "frame_id": frame_id,
            "frame_sha256": frame_sha256,
            "ocr_state": state,
            "text_candidates": projected,
        })

    return {
        "schema_version": 1,
        "policy": _POLICY,
        "role": _ROLE,
        "frames": frames,
        "limits": list(_LIMITS),
        "quality_approved": False,
    }