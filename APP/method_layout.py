"""One lossless layout repair for observed provider metadata in the methods array."""
import hashlib
import json

from video_action_agent.artifact_builder import _parse_reply_text


def normalize_layout(text, kind):
    if kind != "method_list":
        return text, None
    try:
        value = _parse_reply_text(text)
        methods = value["methods"]
        limits = value.get("unverified", [])
        if not isinstance(methods, list) or not isinstance(limits, list) or any(not isinstance(s, str) for s in limits):
            return text, None
        kept, moved = [], []
        for item in methods:
            if (isinstance(item, dict) and set(item) == {"unverified"} and isinstance(item["unverified"], list)
                    and all(isinstance(s, str) and s.strip() for s in item["unverified"])):
                moved.extend(item["unverified"])
            else:
                kept.append(item)
        if not moved:
            return text, None
        normalized = {**value, "methods": kept, "unverified": limits + moved}
        output = json.dumps(normalized, ensure_ascii=False)
        return output, {"kind": "move_unverified_only_record_to_declared_top_level",
                        "before_sha256": hashlib.sha256(text.encode()).hexdigest(),
                        "after_sha256": hashlib.sha256(output.encode()).hexdigest(),
                        "moved_limits": moved, "method_content_changed": False}
    except (ValueError, KeyError, TypeError):
        return text, None
