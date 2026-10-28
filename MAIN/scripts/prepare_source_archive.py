"""Prepare a conservative local source archive; dry-run unless --copy is supplied."""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit

MAIN = Path(r"C:\Users\泽\Documents\VideoActionAgent")
APP = Path(r"D:\CodexWork\VideoActionAgent-purpose-output-20261015")
VIS = Path(r"D:\CodexWork\VideoActionAgent-visual-intake-20261015")
DOWN = Path(r"D:\CodexWork\VideoActionAgent-downstream-20261014")

ALLOWED_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".mjs", ".cjs", ".jsx", ".css", ".html", ".md", ".toml", ".ps1", ".cmd", ".txt"}
LOCK_FILES = {"uv.lock", "pnpm-lock.yaml", "package-lock.json", "yarn.lock"}
CONFIG_FILES = {
    "pyproject.toml", "alembic.ini", "package.json", "tsconfig.json", "tsconfig.app.json",
    "tsconfig.node.json", "vite.config.ts", "vite.purpose.config.ts", "postcss.config.js",
    "tailwind.config.js", "tailwind.config.ts", "tailwind.config.cjs", "index.html",
    "builtin_providers.json",
}
PUBLIC_ASSET_JSON_FILES = {"404.json", "download.json", "error.json", "idle.json", "loading.json"}
LICENSE_NAMES = {"license", "license.md", "license.txt", "license.rst"}
EXCLUDED_DIRS = {
    ".git", ".venv", "venv", "node_modules", "runtime", "tmp", ".local", "__pycache__",
    ".pytest_cache", ".ruff_cache", "dist", "build", "coverage", ".next", ".cache",
}
EXCLUDED_FILES = {".env", ".env.local", ".env.development", ".env.production", ".env.test"}
PRIVATE_INSTRUCTION_FILES = {
    "agents.md", "claude.md", "claude.local.md", "copilot-instructions.md",
    "gemini.md", "cline.md", "cursor.md", ".cursorrules", "windsurfrules",
}
SECRET_PATTERNS = (
    ("private key material", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----", re.I)),
    ("cloud access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{30,}\b")),
    ("Stripe secret", re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    ("OpenAI-style token", re.compile(r"\bsk-[A-Za-z0-9_-]{24,}\b")),
    ("credential assignment", re.compile(
        r"(?im)[\"']?\b(?:[A-Z][A-Z0-9]*_)*(?:api[_-]?key|access[_-]?token|client[_-]?secret|password|passwd|auth[_-]?token)\b[\"']?"
        r"\s*[:=]\s*(['\"])([^\r\n'\"]{12,})\1"
    )),
)
URL_USERINFO_PATTERN = re.compile(r"(?i)https?://[^/\s:@]+:[^/\s@]+@[^/\s\"'<>]+")
# This reviewed test intentionally puts synthetic userinfo on the real site to
# prove userinfo is rejected separately from the host check. Any byte change
# removes the exception; all other credential patterns still apply.
REVIEWED_SYNTHETIC_URL_FIXTURES = {
    "MAIN/tests/test_source_access.py": "db0bd14d8f8323ff73e015b1b4ba1e50d4e0ccdbd57401b9fa91344d3682de41",
}
# These offline tests deliberately inject fake credentials to check environment
# isolation or redaction. Only the credential-assignment pattern is exempted,
# and only for the exact reviewed bytes; every other secret pattern still runs.
REVIEWED_SYNTHETIC_CREDENTIAL_FIXTURES = {
    "APP/test_codex_exec_provider.py": "366b340f0b8fe343187e0de3654000ad5a7a530db5d6fbbe99bef8c745517c3a",
    "APP/test_saved_region_transport.py": "aae725a2da6393a8cba1c1ad0f5aef5508528d773e44a2fad8982a9c3af3037d",
    "MAIN/tests/test_zhipu_provider.py": "66c1b2b8cc91b1930f2f7979039e82f7ef9878771ac2cb1e6bc385dff0c6303f",
}


class ArchiveError(Exception):
    """A fail-closed archive validation error."""


@dataclass(frozen=True)
class SourceSpec:
    group: str
    root: Path
    include_dirs: tuple[str, ...] = ()
    include_root_files: tuple[str, ...] = ()
    include_root_test_python: bool = False
    allow_public_assets: tuple[str, ...] = ()
    include_public_asset_files: tuple[str, ...] = ()


@dataclass(frozen=True)
class Entry:
    source_group: str
    relative_path: str
    source_path: Path


def default_specs() -> list[SourceSpec]:
    main_config = tuple(sorted(CONFIG_FILES | LOCK_FILES | {
        "LICENSE", "LICENSE.md", "Start-VideoAssistant.cmd",
        "docs/version-delivery-contract.md", "docs/default-processing.md",
    }))
    return [
        SourceSpec("MAIN", MAIN, ("src", "tests", "scripts", "alembic"), main_config),
        SourceSpec(
            "APP", APP, ("tests", "skills"),
            (
                "Start-Purpose.ps1", "purpose_app.py", "addressed_amendments.py",
                "codex_exec_provider.py", "codex_writer_instructions.txt", "configure_codex_writer.py",
                "deepseek_native_provider.py", "diagram_tool.py", "explanation_amendments.py",
                "explanation_tool.py", "followup_context.py",
                "goal_native.py", "goal_strategy.py", "image_filter.py", "method_layout.py",
                "model_material.py", "native_amendment.py", "native_purposes.py", "native_scope.py",
                "purpose_outputs.py", "reused_native.py", "revision_visual_context.py",
                "saved_region_analysis.py", "saved_region_transport.py", "saved_visuals.py",
                "seed_sources.py", "source_literals.py", "source_requirements.py",
                "source_review.py", "source_review_patches.py", "task_editor.py",
                "text_amendments.py", "typed_outputs.py",
            ),
            include_root_test_python=True,
        ),
        SourceSpec(
            "VIS", VIS, ("tests", "skills"),
            (
                "cached_capture.py", "frame_text_runtime.py", "frame_text_worker.py",
                "frame_capture.py", "frame_worker.py", "native_engine.py", "native_notes.py",
                "purpose_prompt.py", "vision_context.py",
                "vision_pipeline.py", "vision_policy.py", "visual_grounding.py", "visual_text_context.py",
            ),
            include_root_test_python=True,
        ),
        SourceSpec(
            "DOWN", DOWN,
            ("BiliNote/backend/app", "BiliNote/BillNote_frontend/src"),
            (
                "bridge_contract.py", "BiliNote/LICENSE", "BiliNote/backend/requirements.txt",
                "BiliNote/BillNote_frontend/package.json",
                "BiliNote/BillNote_frontend/pnpm-lock.yaml",
                "BiliNote/BillNote_frontend/package-lock.json",
                "BiliNote/BillNote_frontend/yarn.lock",
                "BiliNote/BillNote_frontend/tsconfig.json",
                "BiliNote/BillNote_frontend/tsconfig.app.json",
                "BiliNote/BillNote_frontend/tsconfig.node.json",
                "BiliNote/BillNote_frontend/vite.config.ts",
                "BiliNote/BillNote_frontend/vite.purpose.config.ts",
                "BiliNote/BillNote_frontend/postcss.config.js",
                "BiliNote/BillNote_frontend/tailwind.config.js",
                "BiliNote/BillNote_frontend/tailwind.config.ts",
                "BiliNote/BillNote_frontend/tailwind.config.cjs",
                "BiliNote/BillNote_frontend/purpose.html",
                "BiliNote/BillNote_frontend/index.html",
            ),
            allow_public_assets=("BiliNote/BillNote_frontend/public",),
            include_public_asset_files=(
                "BiliNote/BillNote_frontend/src/assets/customAI.png",
                "BiliNote/BillNote_frontend/src/assets/icon.svg",
                "BiliNote/BillNote_frontend/src/assets/Lottie/404.json",
                "BiliNote/BillNote_frontend/src/assets/Lottie/download.json",
                "BiliNote/BillNote_frontend/src/assets/Lottie/Error.json",
                "BiliNote/BillNote_frontend/src/assets/Lottie/idle.json",
                "BiliNote/BillNote_frontend/src/assets/Lottie/loading.json",
            ),
        ),
    ]


def _is_reparse(path: Path) -> bool:
    try:
        st = path.lstat()
    except OSError as exc:
        raise ArchiveError(f"cannot inspect path: {path}") from exc
    attrs = getattr(st, "st_file_attributes", 0)
    return stat.S_ISLNK(st.st_mode) or bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _excluded(path: Path, *, public_asset: bool = False) -> bool:
    names = [part.casefold() for part in path.parts]
    if any(name in EXCLUDED_DIRS for name in names):
        return True
    name = path.name.casefold()
    if (
        name in PRIVATE_INSTRUCTION_FILES
        or name.startswith(("agents.", "claude."))
        or name.endswith(".instructions.md")
    ):
        return True
    if name in EXCLUDED_FILES or name.startswith(".env."):
        return True
    denied_suffixes = {".db", ".sqlite", ".sqlite3", ".log", ".wav", ".mp3", ".mp4", ".m4a", ".webm", ".onnx", ".pt", ".pth", ".safetensors"}
    public_static = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".woff", ".woff2", ".ttf"}
    if path.suffix.casefold() in denied_suffixes or (not public_asset and path.suffix.casefold() in public_static):
        return True
    if name.endswith(".json") and any(x in name for x in ("transcript", "transcription", "session", "cookie", "auth", "credential", "secret", "token")):
        return True
    return False


def _is_allowed_file(path: Path, *, public_asset: bool = False) -> bool:
    if _excluded(path, public_asset=public_asset) or not path.is_file():
        return False
    name = path.name.casefold()
    if name in LICENSE_NAMES:
        return True
    if path.suffix.casefold() in ALLOWED_SUFFIXES:
        return True
    if name in LOCK_FILES:
        return True
    if name in CONFIG_FILES and path.suffix.casefold() == ".json":
        return True
    return public_asset and path.suffix.casefold() in {
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".woff", ".woff2", ".ttf"
    } or public_asset and path.suffix.casefold() == ".json" and name in PUBLIC_ASSET_JSON_FILES


def _assert_no_reparse_chain(path: Path, stop: Path | None = None) -> None:
    """Reject a selected path reached through a symlink or junction before resolving it."""
    current = Path(os.path.abspath(os.fspath(path)))
    stop_lexical = Path(os.path.abspath(os.fspath(stop))) if stop is not None else None
    while True:
        if current.exists() or current.is_symlink():
            if _is_reparse(current):
                raise ArchiveError(f"reparse point rejected: {current}")
        if stop_lexical is not None and current == stop_lexical:
            return
        parent = current.parent
        if parent == current:
            if stop_lexical is not None:
                raise ArchiveError(f"source path escaped root: {path}")
            return
        current = parent


def _walk_selected(base: Path, *, public_asset: bool = False) -> Iterable[Path]:
    if not base.exists() and not base.is_symlink():
        return
    if _is_reparse(base):
        raise ArchiveError(f"reparse point rejected: {base}")
    if base.is_file():
        if _is_allowed_file(base, public_asset=public_asset):
            yield base
        return
    for current, dirs, files in os.walk(base, topdown=True, followlinks=False):
        cur = Path(current)
        kept = []
        for dirname in dirs:
            child = cur / dirname
            if dirname.casefold() in EXCLUDED_DIRS:
                continue
            if _is_reparse(child):
                raise ArchiveError(f"reparse point rejected: {child}")
            kept.append(dirname)
        dirs[:] = kept
        for filename in files:
            candidate = cur / filename
            if _is_reparse(candidate):
                raise ArchiveError(f"reparse point rejected: {candidate}")
            if _is_allowed_file(candidate, public_asset=public_asset):
                yield candidate


def build_plan(specs: Iterable[SourceSpec]) -> list[Entry]:
    entries: dict[str, Entry] = {}
    for spec in specs:
        if not spec.root.exists() and not spec.root.is_symlink():
            continue
        if _is_reparse(spec.root):
            raise ArchiveError(f"reparse source root rejected: {spec.root}")
        selected: list[tuple[Path, bool]] = []
        for name in spec.include_dirs:
            selected.append((spec.root / name, False))
        for name in spec.include_root_files:
            selected.append((spec.root / name, False))
        if spec.include_root_test_python:
            selected.extend((p, False) for p in spec.root.glob("test_*.py"))
        for name in spec.allow_public_assets:
            selected.append((spec.root / name, True))
        for name in spec.include_public_asset_files:
            selected.append((spec.root / name, True))
        for base, public_asset in selected:
            _assert_no_reparse_chain(base, spec.root)
            for path in _walk_selected(base, public_asset=public_asset):
                rel = path.relative_to(spec.root).as_posix()
                # The script can be in its own scripts source tree; copying itself is harmless and finite.
                archive_rel = f"{spec.group}/{rel}"
                prior = entries.get(archive_rel)
                entry = Entry(spec.group, archive_rel, path)
                if prior and prior.source_path != path:
                    raise ArchiveError(f"duplicate archive path: {archive_rel}")
                entries[archive_rel] = entry
    return sorted(entries.values(), key=lambda e: e.relative_path.casefold())


def validate_destination(destination: Path, source_roots: Iterable[Path]) -> None:
    lexical_dest = Path(os.path.abspath(os.fspath(destination.expanduser())))
    _assert_no_reparse_chain(lexical_dest)
    dest = lexical_dest.resolve(strict=False)
    for root in source_roots:
        resolved = root.expanduser().resolve(strict=False)
        if dest == resolved or resolved in dest.parents:
            raise ArchiveError(f"destination is equal to or inside source: {dest}")
    if dest.exists():
        if _is_reparse(dest):
            raise ArchiveError(f"destination reparse point rejected: {dest}")
        if not dest.is_dir() or any(dest.iterdir()):
            raise ArchiveError(f"destination must not exist or must be an empty directory: {dest}")


def scan_secrets(entries: Iterable[Entry]) -> list[tuple[str, str]]:
    findings = []
    binary_asset_suffixes = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".woff", ".woff2", ".ttf"}
    for entry in entries:
        source = entry.source_path
        lower_name = source.name.casefold()
        if (
            lower_name in EXCLUDED_FILES
            or lower_name.startswith(".env.")
            or any(part.casefold() in EXCLUDED_DIRS for part in source.parts)
            or (
                lower_name.endswith(".json")
                and any(marker in lower_name for marker in ("transcript", "session", "cookie", "auth", "credential", "secret", "token"))
            )
        ):
            continue
        if entry.source_path.suffix.casefold() in binary_asset_suffixes:
            continue
        try:
            data = entry.source_path.read_bytes()
            text = data.decode("utf-8-sig")
        except (OSError, UnicodeError) as exc:
            raise ArchiveError(f"cannot scan allowed text file: {entry.source_path}") from exc
        digest = hashlib.sha256(data).hexdigest()
        for reason, pattern in SECRET_PATTERNS:
            if reason == "credential assignment" and REVIEWED_SYNTHETIC_CREDENTIAL_FIXTURES.get(entry.relative_path) == digest:
                continue
            if pattern.search(text):
                findings.append((str(entry.source_path), reason))
        for match in URL_USERINFO_PATTERN.finditer(text):
            if REVIEWED_SYNTHETIC_URL_FIXTURES.get(entry.relative_path) == digest:
                continue
            try:
                host = (urlsplit(match.group(0).rstrip("),.;")).hostname or "").casefold().rstrip(".")
            except ValueError:
                host = ""
            if not _is_reserved_test_host(host):
                findings.append((str(entry.source_path), "URL-embedded credentials"))
    return findings


def _is_reserved_test_host(host: str) -> bool:
    reserved_domains = (".example", ".test", ".invalid", ".localhost")
    if host in {"localhost", "example.com", "example.net", "example.org"}:
        return True
    if host.endswith(reserved_domains) or any(
        host.endswith("." + domain) for domain in ("example.com", "example.net", "example.org")
    ):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _write_archive(destination: Path, entries: list[Entry], specs: list[SourceSpec]) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    manifest_entries = []
    for entry in entries:
        target = destination / Path(entry.relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise ArchiveError(f"refusing to overwrite: {target}")
        content = entry.source_path.read_bytes()
        with target.open("xb") as stream:
            stream.write(content)
        manifest_entries.append({
            "relativepath": entry.relative_path,
            "sha256": hashlib.sha256(content).hexdigest(),
            "source_group": entry.source_group,
        })
    license_present = {}
    for group in sorted({e.source_group for e in entries}):
        license_present[group] = any(
            Path(e.relative_path).name.casefold() in LICENSE_NAMES for e in entries if e.source_group == group
        )
    manifest = {"files": manifest_entries, "third_party_license_presence": license_present}
    target = destination / "SOURCE-MANIFEST.json"
    if target.exists():
        raise ArchiveError(f"refusing to overwrite: {target}")
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--copy", action="store_true", help="copy after validation; default only lists the plan")
    args = parser.parse_args(argv)
    specs = default_specs()
    roots = [s.root for s in specs]
    try:
        validate_destination(args.destination, roots)
        entries = build_plan(specs)
        findings = scan_secrets(entries)
        if findings:
            for path, reason in findings:
                print(f"拒绝：{path} — {reason}", file=sys.stderr)
            return 2
        for entry in entries:
            print(f"{entry.source_group}\t{entry.relative_path}")
        print(f"文件数：{len(entries)}；授权文件存在性见 SOURCE-MANIFEST.json")
        print("密钥扫描采用常见模式启发式，不能替代完整安全审计。")
        print("注意：此源码归集不等于可移植或可启动；盘点中的绝对路径与 seed/runtime 依赖尚未修复。")
        if args.copy:
            _write_archive(args.destination, entries, specs)
            print(f"已复制至：{args.destination.resolve()}")
        else:
            print("当前为 dry-run；使用 --copy 才会复制。")
        return 0
    except ArchiveError as exc:
        print(f"拒绝：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())



