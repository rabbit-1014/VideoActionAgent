"""Offline artifact preview over saved subtitles. Not a formal product result.

Input is the saved diagnostic file ``.local/handoff/source-subtitle-observed.json`` (raw
OpenCLI rows). Output is ``.local/out-02-preview.md`` plus the structured candidate
``.local/out-02-preview-draft.json``, both marked "保存字幕离线预览，非正式产品成果".

``--provider fake`` (default) runs a deterministic fake provider that only demonstrates
the output contract; it is NOT model output. ``--provider deepseek`` performs one real
call via DeepSeekProvider and requires DEEPSEEK_API_KEY in the environment.
"""

import argparse
import json
from pathlib import Path

from video_action_agent.artifact_builder import build_method_list, render_markdown
from video_action_agent.deepseek_provider import DeepSeekProvider, ProviderError
from video_action_agent.source_access import VideoRef, parse_reply, payload_hash

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OBSERVED = PROJECT_ROOT / ".local" / "handoff" / "source-subtitle-observed.json"
DEFAULT_MARKDOWN = PROJECT_ROOT / ".local" / "out-02-preview.md"
DEFAULT_DRAFT = PROJECT_ROOT / ".local" / "out-02-preview-draft.json"

FIRST_REQUEST = (
    "把这条视频讲的三种剪映加字幕方法整理成分步骤清单，标出视频时间点；"
    "现行软件界面是否仍相同另行标注"
)
FIRST_SOURCE = {"bvid": "BV1gD421u75J", "page": 1}
PREVIEW_NOTICE = "保存字幕离线预览，非正式产品成果"


class FakeProvider:
    """Deterministic demo of the output contract. Not a model, not product evidence."""

    model_id = "fake-demo-provider-not-a-model"

    def complete(self, system: str, user: str) -> str:
        del system
        payload = json.loads(user)
        segments = payload["segments"]

        def evidence(text, indices):
            return {
                "text": text,
                "segment_indices": indices,
                "start": min(segments[index - 1]["start"] for index in indices),
                "end": max(segments[index - 1]["end"] for index in indices),
            }

        methods = [
            {
                "title": "新建文本，逐条手动输入字幕",
                "applicability": "适合字幕不多的视频；视频提到给几分钟长的视频加字幕时此法过于笨拙。",
                "steps": [
                    evidence("用剪映打开需要加字幕的视频", [6]),
                    evidence("在界面左上角“文本”栏中新建一个文本", [8]),
                    evidence("把文本放到时间轴，边听音频边手动打上字幕", [9, 10]),
                    evidence("调整字幕的大小和位置", [11]),
                ],
                "checkpoints": [
                    evidence("开始前确认字幕数量不多，能接受边听边手动输入的耗时", [12, 13, 14]),
                    evidence("逐条检查字幕的大小和位置是否合适", [11]),
                ],
            },
            {
                "title": "选中人声音频，右键“识别字幕”自动识别",
                "applicability": "需要联网且网络良好；需要有可选中的人声音频；"
                "视频提到普通话还行时识别结果基本可用（准确率未核实）。",
                "steps": [
                    evidence("选中人声部分的音频", [18]),
                    evidence("右键选择“识别字幕”", [19]),
                    evidence("点击之后等待软件自动识别完成", [22, 23, 24]),
                    evidence("核对识别结果并修改错别字", [25, 28, 29]),
                    evidence("按需调整字幕的后续排版", [30, 31]),
                ],
                "checkpoints": [
                    evidence("确认已联网且网络保持良好（识别字幕功能需要联网）", [20, 21]),
                    evidence("识别完成后逐条核对识别文字", [25]),
                ],
            },
            {
                "title": "智能字幕 · 文稿匹配",
                "applicability": "需要事先准备好与字幕匹配的文稿。",
                "steps": [
                    evidence("在“文本”栏点击“智能字幕”", [38]),
                    evidence("选择“文稿匹配”", [39]),
                    evidence("打开文本输入框后复制文稿并粘贴进来", [40, 41, 42]),
                    evidence("点击“开始匹配”，等待软件匹配字幕", [43, 44]),
                    evidence("匹配完成后检查字幕", [45, 46]),
                    evidence("对断句不好的字幕手动修改", [47, 48]),
                ],
                "checkpoints": [
                    evidence("匹配完成后重点检查断句是否合理", [46, 47]),
                    evidence("粘贴的文稿须与需要匹配的字幕内容一致", [36]),
                ],
            },
        ]
        for method in methods:
            method["steps"] = [
                {"order": position, **item}
                for position, item in enumerate(method["steps"], start=1)
            ]
        reply = {
            "user_request": payload["user_request"],
            "source_ref": payload["source_ref"],
            "payload_hash": payload["payload_hash"],
            "methods": methods,
            "unverified": [
                (
                    "视频中的宣传性表述（如“基本没有错别字”“效率上提升了很多”）未作为操作依据；"
                    "识别准确率与实际效果未核实。"
                ),
                "字幕为来源返回的片段文本，未与视频原音逐字核对，可能存在转写错误。",
            ],
        }
        return json.dumps(reply, ensure_ascii=False)


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline artifact preview over saved subtitles.")
    parser.add_argument("--provider", choices=["fake", "deepseek"], default="fake")
    parser.add_argument("--observed", type=Path, default=DEFAULT_OBSERVED)
    parser.add_argument("--output", type=Path, default=DEFAULT_MARKDOWN)
    parser.add_argument("--json-output", type=Path, default=DEFAULT_DRAFT)
    args = parser.parse_args()

    raw = args.observed.read_bytes()
    segments = parse_reply("subtitle", VideoRef.parse(FIRST_SOURCE["bvid"]), raw)["segments"]
    digest = payload_hash(raw)
    provider = FakeProvider() if args.provider == "fake" else DeepSeekProvider()
    try:
        draft = build_method_list(segments, FIRST_REQUEST, FIRST_SOURCE, digest, provider)
        markdown = render_markdown(draft)
    except ProviderError as exc:
        print(f"provider failed: {exc.code}")
        return 1

    real_call = args.provider == "deepseek"
    header = (
        f"> ⚠️ **{PREVIEW_NOTICE}**\n"
        f"> 输入为保存的字幕片段 `{args.observed.name}`（诊断材料），非实时来源；"
        f"提供方为{'DeepSeek 真实调用（仍为未验收候选）' if real_call else '假提供方（演示输出契约，不是模型真实产出）'}。\n"
        f"> 是否真实调用 API：{'是' if real_call else '否'}\n\n"
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(header + markdown, encoding="utf-8")
    args.json_output.write_text(
        json.dumps({"preview_notice": PREVIEW_NOTICE, "draft": draft}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"preview markdown: {args.output}")
    print(f"structured draft: {args.json_output}")
    print(f"methods: {len(draft['methods'])}; real API call: {'yes' if real_call else 'no'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
