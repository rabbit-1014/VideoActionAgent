"""Fixed purpose routing and immutable user-submitted local media scope."""
import json
import math
import re
import threading
from pathlib import Path
from uuid import UUID

from bridge_contract import BridgeError, atomic, digest
from video_action_agent.source_access import VideoRef


def visual_plan(material, request):
    spoken_only = any(term in request for term in ("只整理讲者口头", "只整理口头", "不分析画面", "仅转写", "只要转写"))
    no_figures = ("纯文字", "不要截图", "不加图片", "不需要图片", "不需要截图", "无需截图", "不用截图")
    visual_request = request
    for term in no_figures:
        visual_request = visual_request.replace(term, "")
    explicit = re.search(r"代码|界面|图表|公式|操作|演示|画面|截图", visual_request)
    light = any(term in request for term in ("简短摘要", "一句话", "只要摘要"))
    combined = visual_request + material["source_evidence"].get("part_title", "")
    if not explicit:
        combined += "\n" + "\n".join(segment["text"] for segment in material["transcription"]["segments"])
    needed = not spoken_only and not (light and not explicit) and bool(
        re.search(r"代码|界面|图表|公式|画面|截图|点击|窗口|函数|markdown|neo4j", combined, re.IGNORECASE))
    figures = not any(term in request for term in no_figures)
    if not needed:
        return {"needed": False, "times": [], "figures_allowed": figures, "rule_version": "purpose-v2"}
    duration = material["source_evidence"]["stream_duration_seconds"]
    if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
        raise BridgeError("visual_duration_unavailable")
    segments = material["transcription"]["segments"]
    choices = []
    for index in range(3):
        left, right = duration * index / 3, duration * (index + 1) / 3
        center = (left + right) / 2
        candidates = [segment for segment in segments
                      if left <= (segment["start"] + segment["end"]) / 2 < right
                      and re.search(r"代码|函数|定义|实现|点击|窗口|图表|公式|参数|提取|抽取|消解", segment["text"])]
        if candidates:
            nearest = min(candidates, key=lambda s: abs((s["start"] + s["end"]) / 2 - center))
            at = (nearest["start"] + nearest["end"]) / 2
        else:
            at = center
        choices.append(round(min(at, max(0, duration - 0.2)), 2))
    return {"needed": True, "times": sorted(set(choices)), "figures_allowed": figures,
            "rule_version": "purpose-v2"}


class VisualIntents:
    def __init__(self, root, owner):
        self.root, self.owner = Path(root), owner
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

    def bind(self, note_id, source_id, source, user_request, *, allow_local_video_read=False, allow_model_images=False):
        identity, source_id = str(UUID(note_id)), str(UUID(source_id))
        ref = VideoRef.parse(source)
        if type(allow_local_video_read) is not bool or type(allow_model_images) is not bool:
            raise BridgeError("invalid_visual_scope")
        value = {"schema_version": 1, "owner": self.owner, "notes_job_id": identity,
                 "source_job_id": source_id, "source": {"bvid": ref.bvid, "page": ref.page},
                 "user_request": user_request.strip(), "allow_local_video_read": allow_local_video_read,
                 "allow_model_images": allow_model_images,
                 "basis": "explicit_local_candidate_request_not_formal_runtime_permission"}
        with self.lock:
            path = self.root / (identity + ".json")
            if path.exists():
                if json.loads(path.read_bytes()) != value:
                    raise BridgeError("visual_request_conflict")
            else:
                atomic(path, value)
        return value

    def get(self, identity, row, material):
        path = self.root / (str(UUID(identity)) + ".json")
        if not path.is_file():
            return None
        value = json.loads(path.read_bytes())
        proof = material["source_evidence"]
        if (value["owner"] != self.owner or value["owner"] != row["owner"]
                or value["notes_job_id"] != identity or value["source_job_id"] != row["master_id"]
                or value["user_request"] != row["user_request"]
                or value["source"] != {"bvid": proof["bvid"], "page": proof["page"]}):
            raise BridgeError("visual_scope_mismatch")
        return value

    def fingerprint(self, value):
        return digest(value)
