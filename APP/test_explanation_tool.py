import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from explanation_tool import ExplanationError, render, validate_arguments


def _svg(view_box="0 0 640 360"):
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="' + view_box + '">'
        '<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto">'
        '<path d="M0,0 L0,6 L7,3 z" fill="#334155"/></marker></defs>'
        '<g font-family="Arial, sans-serif" font-size="18" text-anchor="start">'
        '<rect x="20" y="20" width="200" height="80" rx="8" fill="#eef2ff" stroke="#334155"/>'
        '<text x="30" y="55" font-size="18">会议承诺</text>'
        '<line x1="220" y1="60" x2="320" y2="60" stroke="#334155" marker-end="url(#arrow)"/>'
        '<text x="330" y="65" font-size="18">责任人</text></g></svg>'
    )


def _args(svg_a=None, svg_b=None):
    return {
        "title": "关系链示例",
        "purpose": "说明信息如何支持后续核查",
        "subject": "一次客户交付承诺",
        "panels": [
            {"id": "promise", "title": "先记录承诺", "explanation": "把承诺连到客户、项目、负责人和期限。",
             "provenance": "illustrative", "evidence": [1, 2], "svg": svg_a or _svg()},
            {"id": "check", "title": "再核对结果", "explanation": "对照合同、交付记录与客户反馈。",
             "provenance": "source", "evidence": [75, 76, 77], "svg": svg_b or _svg()},
        ],
    }


class ExplanationToolTests(unittest.TestCase):
    def test_valid_multi_panel_render_saves_exact_assets_and_source_hashes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            canonical = validate_arguments(_args(), segment_count=140)
            assets = render(canonical, root / "assets", "explanation")

            self.assertEqual(len(assets), 2)
            source_path = root / "assets" / "explanation.json"
            source_bytes = source_path.read_bytes()
            source_sha = hashlib.sha256(source_bytes).hexdigest()
            source = json.loads(source_bytes)
            self.assertEqual(source["panels"][0]["id"], "promise")
            for asset in assets:
                svg_bytes = (source_path.parent / asset["filename"]).read_bytes()
                self.assertEqual(asset["role"], "derived_explanation")
                self.assertEqual(asset["source_filename"], "explanation.json")
                self.assertEqual(asset["source_sha256"], source_sha)
                self.assertEqual(asset["sha256"], hashlib.sha256(svg_bytes).hexdigest())
                panel = next(p for p in source["panels"] if p["id"] == asset["panel_id"])
                self.assertEqual(svg_bytes.decode("utf-8"), panel["svg"])

    def test_malicious_or_oversized_svg_is_rejected_before_any_write(self):
        attacks = [
            _svg().replace("<svg ", '<svg onload="alert(1)" ', 1),
            _svg().replace("<g ", '<g onclick="alert(1)" ', 1),
            _svg().replace("</svg>", "<script>alert(1)</script></svg>"),
            _svg().replace("</svg>", '<foreignObject><div>bad</div></foreignObject></svg>'),
            _svg().replace("</svg>", '<image href="https://example.invalid/a.png"/></svg>'),
            _svg().replace("</svg>", '<a href="https://example.invalid">bad</a></svg>'),
            _svg().replace("</svg>", '<animate attributeName="x" to="2"/></svg>'),
            _svg().replace("<svg ", '<svg style="color:red" ', 1),
            _svg().replace("</svg>", '<path d="M0 0" marker-end="url(https://example.invalid/a.svg#m)"/></svg>'),
            '<!DOCTYPE svg [<!ENTITY x "boom">]>' + _svg(),
            "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 1 1\">" + " " * 40001 + "</svg>",
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, bad_svg in enumerate(attacks):
                directory = root / f"case-{index}"
                value = _args(svg_a=bad_svg)
                with self.assertRaises(ExplanationError):
                    render(validate_arguments(value, segment_count=140), directory, "bad")
                self.assertFalse(directory.exists() and list(directory.iterdir()))

    def test_invalid_source_indices_schema_and_panel_shapes_are_rejected(self):
        for evidence in ([0], [141], [True]):
            value = _args()
            value["panels"][0]["evidence"] = evidence
            with self.subTest(evidence=evidence), self.assertRaises(ExplanationError):
                validate_arguments(value, segment_count=140)

        value = _args()
        value["panels"][0]["unexpected"] = "extra"
        with self.assertRaises(ExplanationError):
            validate_arguments(value, segment_count=140)

        value = _args(svg_b=_svg("0 0 800 600"))
        with self.assertRaises(ExplanationError):
            validate_arguments(value, segment_count=140)

    def test_exclusive_create_never_overwrites_and_rejects_symlink_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "assets"
            directory.mkdir()
            existing = directory / "explanation_promise.svg"
            existing.write_bytes(b"keep-original")
            with self.assertRaises(ExplanationError):
                render(validate_arguments(_args(), segment_count=140), directory, "explanation")
            self.assertEqual(existing.read_bytes(), b"keep-original")
            self.assertFalse((directory / "explanation.json").exists())

            outside = root / "outside"
            outside.mkdir()
            link = root / "linked-assets"
            try:
                os.symlink(outside, link, target_is_directory=True)
            except (OSError, NotImplementedError):
                if os.name != "nt":
                    self.skipTest("directory symlinks are unavailable in this environment")
                command = (
                    f"New-Item -ItemType Junction -Path '{str(link).replace(chr(39), chr(39) * 2)}' "
                    f"-Target '{str(outside).replace(chr(39), chr(39) * 2)}' | Out-Null"
                )
                result = subprocess.run(
                    ["powershell.exe", "-NoProfile", "-Command", command],
                    capture_output=True, check=False,
                )
                if result.returncode != 0:
                    self.skipTest("directory symlink/junction creation is unavailable")
            with self.assertRaises(ExplanationError):
                render(validate_arguments(_args(), segment_count=140), link, "explanation")
            self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
