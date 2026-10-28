"""Conservative exclusion of frames explicitly observed as blank/unreadable code areas."""
import json
import re
from pathlib import Path

from bridge_contract import atomic
from vision_pipeline import VisionPipeline


def eligible_frame_ids(observation):
    result = set()
    for item in observation["frames"]:
        descriptions = "\n".join(item["observations"] + item["uncertain"])
        if re.search(r"空白|占位符|没有可见.*代码|代码.*无法辨|无法辨.*代码", descriptions):
            continue
        if item["observations"]:
            result.add(item["frame_id"])
    return result


class FilteredVision(VisionPipeline):
    def generate(self, identity, material, request, style, client, output):
        path = super().generate(identity, material, request, style, client, output)
        row = client.service.ledger.get(identity)
        if not row.get("vision_manifest_sha256"):
            return path
        packet = json.loads((self.root / identity / "visual-material.json").read_bytes())
        keep = eligible_frame_ids(packet["observation"])
        excluded = [frame for frame in packet["frames"] if frame["id"] not in keep]
        result = json.loads(Path(path).read_bytes())
        before = result["markdown"]
        for frame in excluded:
            filename = re.escape(Path(frame["path"]).name)
            result["markdown"] = re.sub(r"!\[[^\]]*\]\(/bridge/assets/" + re.escape(identity) + "/" + filename + r"\)", "", result["markdown"])
        if before != result["markdown"]:
            atomic(Path(output) / (identity + ".before-empty-frame-filter.json"), json.loads(Path(path).read_bytes()))
            atomic(path, result)
            (Path(output) / (identity + "_markdown.md")).write_text(result["markdown"], encoding="utf-8")
        client.service.ledger.material(identity)
        client.service.ledger.update(identity, vision_images_used=[i for i in row.get("vision_images_used", []) if i in keep])
        return path
