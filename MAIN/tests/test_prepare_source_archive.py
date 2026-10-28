import ast
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "prepare_source_archive.py"
spec = importlib.util.spec_from_file_location("prepare_source_archive", SCRIPT)
archive = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = archive
spec.loader.exec_module(archive)


def test_plan_keeps_allowed_source_and_license_but_excludes_private_runtime(tmp_path):
    source = tmp_path / "source"
    (source / "src").mkdir(parents=True)
    (source / "tests").mkdir()
    (source / "skills").mkdir()
    (source / "public").mkdir()
    (source / ".venv" / "Lib").mkdir(parents=True)
    (source / "runtime").mkdir()
    (source / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (source / "tests" / "test_app.py").write_text("assert True\n", encoding="utf-8")
    reserved_url = "https://" + "test-user" + ":" + "test-password" + "@service.example.com/path"
    field_name = "api" + "_" + "key"
    (source / "tests" / "safe_url_fixture.py").write_text(
        field_name + " = self.config.api_key\n" + "test_url = " + repr(reserved_url) + "\n",
        encoding="utf-8",
    )
    (source / "skills" / "guide.md").write_text("guide\n", encoding="utf-8")
    (source / "LICENSE").write_text("MIT License\n", encoding="utf-8")
    (source / "public" / "logo.png").write_bytes(b"approved public artwork")
    (source / "public" / "notice.svg").write_text(
        '<svg><metadata ' + field_name + '=' + json.dumps('svg-metadata-marker') + ' /></svg>\n', encoding="utf-8"
    )
    (source / "src" / "AGENTS.md").write_text("private instructions", encoding="utf-8")
    (source / "src" / "AGENTS.local.md").write_text("private instructions", encoding="utf-8")
    (source / "src" / "CLAUDE.md").write_text("private instructions", encoding="utf-8")
    (source / ".env").write_text("TOKEN=private\n", encoding="utf-8")
    (source / ".venv" / "Lib" / "secret.py").write_text("private\n", encoding="utf-8")
    (source / "runtime" / "transcript.json").write_text("{}", encoding="utf-8")
    (source / "runtime" / "audio.wav").write_bytes(b"not copied")

    spec = archive.SourceSpec(
        "MAIN", source, include_dirs=("src", "tests", "skills"),
        include_root_files=("LICENSE",), allow_public_assets=("public",),
    )
    entries = archive.build_plan([spec])
    paths = {entry.relative_path for entry in entries}

    assert "MAIN/src/app.py" in paths
    assert "MAIN/tests/test_app.py" in paths
    assert "MAIN/skills/guide.md" in paths
    assert "MAIN/LICENSE" in paths
    assert "MAIN/public/logo.png" in paths
    assert "MAIN/public/notice.svg" in paths
    assert not any(
        Path(path).name.casefold().startswith(("agents.", "claude.")) for path in paths
    )
    assert not any(".env" in path or ".venv" in path or "runtime" in path for path in paths)
    assert not any(path.endswith((".json", ".wav")) for path in paths)

    findings = archive.scan_secrets(entries)
    assert (str(source / "public" / "notice.svg"), "credential assignment") in findings
    assert not any(path.endswith("safe_url_fixture.py") for path, _ in findings)

    synthetic_secret = "sk-" + "A" * 28
    synthetic_real_url = "https://" + "synthetic-user" + ":" + "synthetic-password" + "@service.com/path"
    bad_fixture = tmp_path / "synthetic_bad.py"
    bad_fixture.write_text(
        field_name + " = " + repr(synthetic_secret) + "\n" + "endpoint = " + repr(synthetic_real_url) + "\n",
        encoding="utf-8",
    )
    bad_findings = archive.scan_secrets([archive.Entry("SYNTHETIC", "bad.py", bad_fixture)])
    reasons = {reason for _, reason in bad_findings}
    assert "credential assignment" in reasons
    assert "OpenAI-style token" in reasons
    assert "URL-embedded credentials" in reasons
    assert synthetic_secret not in repr(bad_findings)
    assert "synthetic-password" not in repr(bad_findings)

    down_specs = [item for item in archive.default_specs() if item.group == "DOWN"]
    assert len(down_specs) == 1
    assert down_specs[0].root == archive.DOWN
    assert "BiliNote/BillNote_frontend/src" in down_specs[0].include_dirs


def test_destination_and_source_reject_nested_and_reparse_aliases(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    child = source / "inside"
    child.mkdir()
    with __import__("pytest").raises(archive.ArchiveError):
        archive.validate_destination(child, [source])

    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "keep.txt").write_text("keep", encoding="utf-8")
    with __import__("pytest").raises(archive.ArchiveError):
        archive.validate_destination(occupied, [source])

    def create_junction(link, target):
        command = f"New-Item -ItemType Junction -Path '{link}' -Target '{target}' | Out-Null"
        return subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", command],
            capture_output=True, text=True, check=False,
        )

    destination_target = tmp_path / "destination-target"
    destination_target.mkdir()
    destination_alias = tmp_path / "destination-alias"
    result = create_junction(destination_alias, destination_target)
    if result.returncode:
        pytest.skip("Windows junction creation is unavailable; alias checks were skipped explicitly")
    with pytest.raises(archive.ArchiveError):
        archive.validate_destination(destination_alias / "new", [source])

    selected = source / "selected"
    selected.mkdir()
    junction_target = tmp_path / "source-target"
    (junction_target / "deep").mkdir(parents=True)
    (junction_target / "deep" / "module.py").write_text("pass\n", encoding="utf-8")
    source_alias = selected / "middle"
    result = create_junction(source_alias, junction_target)
    if result.returncode:
        pytest.skip("Windows source junction creation is unavailable; source alias check was skipped explicitly")
    with pytest.raises(archive.ArchiveError):
        archive.build_plan([
            archive.SourceSpec("MAIN", source, include_dirs=("selected/middle/deep",))
        ])


def test_default_plan_keeps_runtime_closure_and_backend_requirements_without_history_helpers(tmp_path, monkeypatch):
    main = tmp_path / "main"
    app = tmp_path / "app"
    vis = tmp_path / "vis"
    down = tmp_path / "down"

    for base, relpaths in (
        (main, (
            "scripts/prepare_source_archive.py", "docs/version-delivery-contract.md",
            "docs/default-processing.md", "docs/private-development-record.md",
        )),
        (app, (
            "purpose_app.py", "goal_native.py", "native_scope.py", "test_goal_admission.py",
            "skills/notes/SKILL.md", "activate_task.py", "recover_status.py", "observe_outputs.py",
        )),
        (vis, (
            "native_engine.py", "native_notes.py", "vision_pipeline.py", "vision_policy.py",
            "cached_capture.py", "test_visual_pipeline.py", "finalize_delivery.py", "live_observation.py",
        )),
        (down, (
            "BiliNote/LICENSE", "BiliNote/backend/app/main.py", "BiliNote/backend/requirements.txt",
            "BiliNote/backend/app/db/builtin_providers.json",
            "BiliNote/BillNote_frontend/src/PurposeBridge.tsx",
            "BiliNote/BillNote_frontend/src/assets/customAI.png",
            "BiliNote/BillNote_frontend/src/assets/icon.svg",
            "BiliNote/BillNote_frontend/src/assets/react.svg",
            "BiliNote/BillNote_frontend/src/assets/wechat.png",
            "BiliNote/BillNote_frontend/src/assets/Lottie/404.json",
            "BiliNote/BillNote_frontend/src/assets/Lottie/download.json",
            "BiliNote/BillNote_frontend/src/assets/Lottie/Error.json",
            "BiliNote/BillNote_frontend/src/assets/Lottie/idle.json",
            "BiliNote/BillNote_frontend/src/assets/Lottie/loading.json",
        )),
    ):
        for relpath in relpaths:
            path = base / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture\n", encoding="utf-8")

    monkeypatch.setattr(archive, "MAIN", main)
    monkeypatch.setattr(archive, "APP", app)
    monkeypatch.setattr(archive, "VIS", vis)
    monkeypatch.setattr(archive, "DOWN", down)

    paths = {entry.relative_path for entry in archive.build_plan(archive.default_specs())}

    assert {
        "MAIN/docs/version-delivery-contract.md", "MAIN/docs/default-processing.md",
        "APP/purpose_app.py", "APP/goal_native.py", "APP/native_scope.py",
        "APP/test_goal_admission.py", "APP/skills/notes/SKILL.md",
        "VIS/native_engine.py", "VIS/native_notes.py", "VIS/vision_pipeline.py",
        "VIS/vision_policy.py", "VIS/cached_capture.py", "VIS/test_visual_pipeline.py",
        "DOWN/BiliNote/LICENSE", "DOWN/BiliNote/backend/app/main.py",
        "DOWN/BiliNote/backend/requirements.txt", "DOWN/BiliNote/backend/app/db/builtin_providers.json",
        "DOWN/BiliNote/BillNote_frontend/src/PurposeBridge.tsx",
        "DOWN/BiliNote/BillNote_frontend/src/assets/customAI.png",
        "DOWN/BiliNote/BillNote_frontend/src/assets/icon.svg",
        "DOWN/BiliNote/BillNote_frontend/src/assets/Lottie/404.json",
        "DOWN/BiliNote/BillNote_frontend/src/assets/Lottie/download.json",
        "DOWN/BiliNote/BillNote_frontend/src/assets/Lottie/Error.json",
        "DOWN/BiliNote/BillNote_frontend/src/assets/Lottie/idle.json",
        "DOWN/BiliNote/BillNote_frontend/src/assets/Lottie/loading.json",
    } <= paths
    assert not {
        "MAIN/docs/private-development-record.md",
        "APP/activate_task.py", "APP/recover_status.py", "APP/observe_outputs.py",
        "VIS/finalize_delivery.py", "VIS/live_observation.py",
    } & paths
    assert "DOWN/BiliNote/BillNote_frontend/src/assets/react.svg" not in paths
    assert "DOWN/BiliNote/BillNote_frontend/src/assets/wechat.png" not in paths
    assert archive._is_allowed_file(down / "BiliNote/backend/requirements.txt")


def _ast_local_import_closure(roots, seeds):
    modules = {}
    for group, root in roots.items():
        for path in root.glob("*.py"):
            modules.setdefault(path.stem, (group, path))

    pending = list(seeds)
    seen = set()
    while pending:
        module = pending.pop()
        if module in seen or module not in modules:
            continue
        seen.add(module)
        _, path = modules[module]
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            candidates = []
            if isinstance(node, ast.Import):
                candidates.extend(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                candidates.append(node.module.split(".", 1)[0])
                candidates.extend(alias.name.split(".", 1)[0] for alias in node.names)
            pending.extend(candidate for candidate in candidates if candidate in modules)
    return {modules[name] for name in seen}


def test_real_entry_ast_and_dynamic_file_closure_are_selected():
    roots = {"APP": archive.APP, "VIS": archive.VIS}
    if any(not root.is_dir() for root in (*roots.values(), archive.DOWN)):
        pytest.skip("the original APP/VIS/DOWN source trees are required for this local archive-closure audit")
    runtime_closure = _ast_local_import_closure(
        roots,
        {"purpose_app", "native_engine", "native_notes", "vision_pipeline", "vision_policy", "cached_capture"},
    )
    selected = {
        (entry.source_group, entry.source_path.resolve())
        for entry in archive.build_plan(archive.default_specs())
    }

    missing = sorted(
        f"{group}/{path.name}"
        for group, path in runtime_closure
        if (group, path.resolve()) not in selected
    )
    assert not missing, f"AST-reachable local runtime modules not selected: {missing}"

    dynamic_dependencies = (
        ("APP", archive.APP / "explanation_tool.py"),
        ("APP", archive.APP / "explanation_amendments.py"),
        ("APP", archive.APP / "diagram_tool.py"),
        ("APP", archive.APP / "configure_codex_writer.py"),
        ("APP", archive.APP / "codex_writer_instructions.txt"),
        ("VIS", archive.VIS / "frame_text_worker.py"),
        ("VIS", archive.VIS / "frame_capture.py"),
        ("VIS", archive.VIS / "frame_worker.py"),
        ("DOWN", archive.DOWN / "BiliNote/backend/app/db/builtin_providers.json"),
        ("DOWN", archive.DOWN / "BiliNote/BillNote_frontend/src/assets/customAI.png"),
        ("DOWN", archive.DOWN / "BiliNote/BillNote_frontend/src/assets/icon.svg"),
        ("DOWN", archive.DOWN / "BiliNote/BillNote_frontend/src/assets/Lottie/404.json"),
        ("DOWN", archive.DOWN / "BiliNote/BillNote_frontend/src/assets/Lottie/download.json"),
        ("DOWN", archive.DOWN / "BiliNote/BillNote_frontend/src/assets/Lottie/Error.json"),
        ("DOWN", archive.DOWN / "BiliNote/BillNote_frontend/src/assets/Lottie/idle.json"),
        ("DOWN", archive.DOWN / "BiliNote/BillNote_frontend/src/assets/Lottie/loading.json"),
    )
    missing_dynamic = [f"{group}/{path.name}" for group, path in dynamic_dependencies if (group, path.resolve()) not in selected]
    assert not missing_dynamic, f"known string/path dynamic dependencies not selected: {missing_dynamic}"
    unreferenced_upstream_assets = (
        archive.DOWN / "BiliNote/BillNote_frontend/src/assets/react.svg",
        archive.DOWN / "BiliNote/BillNote_frontend/src/assets/wechat.png",
    )
    assert not {("DOWN", path.resolve()) for path in unreferenced_upstream_assets} & selected


def test_secret_scan_detects_prefixed_provider_key_literal_but_not_environment_lookup(tmp_path):
    json_source = tmp_path / "provider_config.json"
    python_source = tmp_path / "provider_config.py"
    environment_source = tmp_path / "provider_environment.py"
    synthetic_json_key = "synthetic-json-provider-key-value"
    synthetic_python_key = "synthetic-python-provider-key-value"
    json_key_name = "DEEPSEEK" + "_API_KEY"
    python_key_name = "ZHIPUAI" + "_API_KEY"
    json_source.write_text(json.dumps({json_key_name: synthetic_json_key}) + "\n", encoding="utf-8")
    python_source.write_text(python_key_name + " = " + repr(synthetic_python_key) + "\n", encoding="utf-8")
    environment_source.write_text("ZHIPUAI_API_KEY = os.getenv('ZHIPUAI_API_KEY')\n", encoding="utf-8")

    findings = archive.scan_secrets([
        archive.Entry("APP", json_source.name, json_source),
        archive.Entry("APP", python_source.name, python_source),
        archive.Entry("APP", environment_source.name, environment_source),
    ])

    assert findings == [
        (str(json_source), "credential assignment"),
        (str(python_source), "credential assignment"),
    ]
    assert synthetic_json_key not in repr(findings)
    assert synthetic_python_key not in repr(findings)


def test_reviewed_synthetic_credentials_require_exact_file_hash_and_keep_other_patterns(tmp_path, monkeypatch):
    source = tmp_path / "reviewed_fixture.py"
    field_name = "api" + "_key"
    synthetic_key = "synthetic-credential-fixture"
    synthetic_token = "sk-" + "B" * 28
    source.write_text(
        field_name + " = " + repr(synthetic_key) + "\n" + "token = " + repr(synthetic_token) + "\n",
        encoding="utf-8",
    )
    entry = archive.Entry("APP", "APP/reviewed_fixture.py", source)
    monkeypatch.setattr(archive, "REVIEWED_SYNTHETIC_CREDENTIAL_FIXTURES", {
        entry.relative_path: hashlib.sha256(source.read_bytes()).hexdigest(),
    }, raising=False)

    assert archive.scan_secrets([entry]) == [(str(source), "OpenAI-style token")]
    unreviewed_entry = archive.Entry("APP", "APP/unreviewed_fixture.py", source)
    assert {reason for _, reason in archive.scan_secrets([unreviewed_entry])} == {
        "credential assignment", "OpenAI-style token",
    }

    source.write_text(source.read_text(encoding="utf-8") + "# changed after review\n", encoding="utf-8")
    assert {reason for _, reason in archive.scan_secrets([entry])} == {
        "credential assignment", "OpenAI-style token",
    }



