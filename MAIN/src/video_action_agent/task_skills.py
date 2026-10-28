"""Fixed task methods; no model planner or user-controlled skill paths."""

import hashlib
from dataclasses import dataclass
from pathlib import Path

REGISTRY = {"structured_notes": "learning-notes", "method_list": "operational-material",
            "source_answer": "source-answer"}
VERSIONS = {"structured_notes": 2, "method_list": 2, "source_answer": 1}


@dataclass(frozen=True)
class TaskSkill:
    skill_id: str
    version: int
    instructions: str

    def record(self):
        return {"id": self.skill_id, "version": self.version, "instructions": self.instructions,
                "sha256": hashlib.sha256(self.instructions.encode("utf-8")).hexdigest()}

    def metadata(self):
        return {key: value for key, value in self.record().items() if key != "instructions"}


def load_skill(output_kind):
    try:
        identity = REGISTRY[output_kind]
    except (KeyError, TypeError):
        raise ValueError("Unknown task purpose") from None
    path = Path(__file__).parent / "task_skills" / identity / "SKILL.md"
    return TaskSkill(identity, VERSIONS[output_kind], path.read_text(encoding="utf-8"))


def stored_skill(record, output_kind):
    """Check frozen content without substituting today's Skill into an old job."""
    if record is None:
        return None
    if (not isinstance(record, dict)
            or set(record) != {"id", "version", "instructions", "sha256"}
            or record["id"] != REGISTRY.get(output_kind)
            or type(record["version"]) is not int or record["version"] < 1
            or not isinstance(record["instructions"], str)
            or not 1 <= len(record["instructions"]) <= 20_000):
        raise ValueError("Invalid frozen task skill")
    skill = TaskSkill(record["id"], record["version"], record["instructions"])
    if skill.record() != record:
        raise ValueError("Invalid frozen task skill")
    return skill


class SkillProvider:
    def __init__(self, provider, skill):
        self._provider, self._skill = provider, skill
        self.model_id = provider.model_id

    def complete(self, system, user):
        # The original identity/output contract remains authoritative.
        instructions = (system + "\n\n以下是已选定的任务方法；严格输出合同优先，"
                        "方法不得增加字段或放宽来源围栏。\n" + self._skill.instructions)
        return self._provider.complete(instructions, user)


def render_reading(candidate):
    """A display projection of an already validated candidate, never a quality verdict."""
    from .notes_builder import _text

    title = (candidate["title"] if candidate["kind"] == "structured_notes"
             else "针对母版的回答" if candidate["kind"] == "source_answer" else "方法清单")
    lines = ["# " + _text(title), ""]
    if candidate["kind"] == "structured_notes":
        sections = [(f"note-topic-{number}", f"{number}. {section['title']}", section["items"])
                    for number, section in enumerate(candidate["sections"], 1)]
        for key, label in (("terms", "核心术语"), ("examples", "例子")):
            if candidate.get(key):
                sections.append((f"note-{key}", label, candidate[key]))
        lines.extend(["## 目录", ""])
        lines.extend(f"- [{_text(label)}](#{anchor})" for anchor, label, _ in sections)
        if candidate.get("unverified"):
            lines.append("- [使用前需要注意](#note-limits)")
        lines.append("")
        for anchor, label, items in sections:
            # All anchors are program constants; model strings remain escaped text.
            lines.extend([f'<a id="{anchor}"></a>', "", "## " + _text(label), ""])
            step_number = 0
            for item in items:
                layout = item.get("layout", "paragraph")
                if layout != "step":
                    step_number = 0
                label = "**" + _text(item["label"]) + "**：" if item.get("label") else ""
                text = label + _text(item["text"])
                if layout in ("bullet", "step"):
                    step_number += layout == "step"
                    prefix = "- " if layout == "bullet" else str(step_number) + ". "
                    text = prefix + text.replace("\n", "\n  ")
                lines.extend([text, ""])
    elif candidate["kind"] == "source_answer":
        labels = {"answered": "按材料整理的回答", "partial": "部分回答，仍缺材料",
                  "not_in_source": "现有材料不足以回答"}
        lines.extend(["## 你的问题", "", _text(candidate["user_request"]), "",
                      "## " + labels[candidate["source_status"]], ""])
        for item in candidate["answer"]:
            lines.extend([_text(item["text"]), ""])
        if candidate["missing"]:
            lines.extend(["## 还缺哪些材料", ""])
            lines.extend("- " + _text(item) for item in candidate["missing"])
            lines.append("")
    elif candidate["kind"] == "method_list":
        for method in candidate["methods"]:
            lines.extend(["## " + _text(method["title"]), "",
                          "适用条件：" + _text(method["applicability"]), ""])
            for step in method["steps"]:
                lines.extend([str(step["order"]) + ". " + _text(step["text"]), ""])
            if method["checkpoints"]:
                lines.extend(["### 检查点", ""])
                for item in method["checkpoints"]:
                    lines.extend(["- " + _text(item["text"]), ""])
    else:
        raise ValueError("Unknown candidate purpose")
    if candidate.get("unverified"):
        if candidate["kind"] == "structured_notes":
            lines.extend(['<a id="note-limits"></a>', ""])
        lines.extend(["## 使用前需要注意", ""])
        for item in candidate["unverified"]:
            text = item["text"] if isinstance(item, dict) else item
            lines.extend(["- " + _text(text), ""])
    return "\n".join(lines).rstrip() + "\n"
