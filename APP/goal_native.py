"""One goal-led request through the existing native dispatch, save and read fences."""
import copy
import hashlib
import html
import io
import json
import re
import shutil
import zipfile
from pathlib import Path
from uuid import UUID

from bridge_contract import BridgeError, atomic, digest
from goal_strategy import StrategyError, instructions, prompt, tool_catalog, validate_solution

_REVIEW_SEPARATOR = "\n\n<!-- source-review-policy-v1 -->\n"
_EDITOR_SEPARATOR = "\n\n<!-- task-editor-policy-v1 -->\n"


def policy_skill(value, stage="generate"):
    if value["version"] < 3:
        return value["skill"]
    generation, review = value["skill"].split(_REVIEW_SEPARATOR)
    if value['version'] >= 9:
        review, editor = review.split(_EDITOR_SEPARATOR)
        if stage == 'task_editor':
            return editor
    return review if stage == "review" else generation


def policy(writer='deepseek'):
    from source_review_patches import PATCH_POLICY
    from task_editor import EDITOR_POLICY
    review = PATCH_POLICY
    value = {"version": 14, "skill": instructions() + _REVIEW_SEPARATOR + review + _EDITOR_SEPARATOR + EDITOR_POLICY,
             "tools": copy.deepcopy(tool_catalog(14)),
             "generation_provider": {'id':'deepseek_official','model':'deepseek-v4-pro','reasoning_effort':'high'}}
    if writer == 'codex':
        value.update(version=16, tools=copy.deepcopy(tool_catalog(16)),
            generation_provider={'id':'codex_local','model':'gpt-6.1-sol',
            'reasoning_effort':'high','transport_version':1})
    elif writer != 'deepseek':
        raise ValueError('invalid_generation_provider')
    return {**value, "sha256": digest(value)}


def validate_policy(value):
    keys = {'version','skill','tools','sha256'}
    if isinstance(value,dict) and value.get('version') in (11, 12, 13, 14, 15, 16):
        keys.add('generation_provider')
    if (not isinstance(value, dict) or set(value) != keys
            or type(value["version"]) is not int or value["version"] not in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16)
            or not isinstance(value["skill"], str)
            or not 1 <= len(value["skill"]) <= 8000 or value["tools"] != tool_catalog(value['version'])
            or digest({k: v for k, v in value.items() if k != "sha256"}) != value["sha256"]):
        raise ValueError("invalid_goal_policy")
    if value['version'] in (11,12,13,14) and value['generation_provider'] != {
            'id':'deepseek_official','model':'deepseek-v4-pro','reasoning_effort':'high'}:
        raise ValueError('invalid_goal_policy')
    if value['version'] in (15,16) and value['generation_provider'] != {
            'id':'codex_local','model':'gpt-6.1-sol','reasoning_effort':'high','transport_version':1}:
        raise ValueError('invalid_goal_policy')
    if value["version"] >= 3 and (value["skill"].count(_REVIEW_SEPARATOR) != 1
            or not all(part.strip() for part in value["skill"].split(_REVIEW_SEPARATOR))):
        raise ValueError("invalid_goal_policy")
    if value['version'] >= 9 and (value['skill'].count(_EDITOR_SEPARATOR) != 1
            or not all(part.strip() for part in value['skill'].split(_EDITOR_SEPARATOR))):
        raise ValueError('invalid_goal_policy')


def goal_profile():
    value = {"version": 6, "style": "minimal", "instructions": "按冻结的用户目的与任务策略交付，存储用途不限定结果形式。"}
    return {**value, "sha256": digest(value)}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _plain_markdown(value):
    return re.sub(r'([\\`*_{}\[\]()#+.!|>~-])', r'\\\1', html.escape(value, quote=False))


def explanation_markdown(identity, call_id, arguments, assets):
    """Portable, inert fallback. Runtime markers delimit the editable artifact."""
    by_panel = {asset['panel_id']: asset for asset in assets}
    lines = [f'<!--vaa-explanation:{call_id}-->', '### ' + _plain_markdown(arguments['title'])]
    for panel in arguments['panels']:
        asset = by_panel[panel['id']]
        label = '解释性示例' if panel['provenance'] == 'illustrative' else '整理示意'
        lines.extend(['', '#### ' + _plain_markdown(panel['title']), '',
            f'![{label}](/bridge/assets/{identity}/{asset["filename"]})', '',
            _plain_markdown(panel['explanation']), '', label + '，不是原视频截图。'])
    lines.append(f'<!--/vaa-explanation:{call_id}-->')
    return '\n'.join(lines)


class GoalNative:
    def __init__(self, root, binding, ledger, save, *, review=None):
        self.root, self.binding, self.ledger, self.save = Path(root), binding, ledger, save
        self.review = review
        self.root.mkdir(parents=True, exist_ok=True)

    def checkpoint(self, identity):
        self.ledger.material(identity)
        row = self.ledger.get(identity)
        if row["state"] == "cancelled":
            raise BridgeError("cancelled")
        return row

    def directory(self, identity):
        identity = str(UUID(identity))
        directory = self.root / identity
        if (self.root.is_symlink() or self.root.is_junction() or directory.is_symlink()
                or directory.is_junction() or not directory.resolve().is_relative_to(self.root.resolve())):
            raise BridgeError("goal_asset_scope_changed")
        return directory

    def _commit(self, identity, binding, solution, assets):
        self.checkpoint(identity)
        path = self.directory(identity) / "manifest.json"
        atomic(path, {"version": 1, "request_id": identity, "owner": binding["owner"],
            "source_sha256": binding["source_sha256"], "scope_sha256": digest(binding),
            "solution": solution, "assets": assets, "semantic_quality_approved": False})
        self.checkpoint(identity)
        self.ledger.update(identity, goal_manifest_sha256=sha(path))

    def generate(self, identity, material, request, style, client, output, *, extras_override=None):
        from diagram_tool import DiagramError, render
        from explanation_tool import ExplanationError, render as render_explanation
        binding = self.binding(identity)
        validate_policy(binding["goal_policy"])
        self.checkpoint(identity)
        directory = self.directory(identity)
        directory.mkdir(exist_ok=False)
        source = copy.deepcopy(material)
        if extras_override:
            source["supplementary_context_untrusted"] = extras_override
        response_version = 3 if binding['goal_policy']['version'] >= 12 else 2 if binding['goal_policy']['version'] >= 8 else 1
        text = prompt(request, binding["source_sha256"], source,
                      tools=tuple(binding["goal_policy"]["tools"]), tool_descriptors=binding["goal_policy"]["tools"],
                      response_version=response_version, request_last=binding['goal_policy']['version'] >= 14)
        reply = client.chat.completions.create(
            model=binding['goal_policy'].get('generation_provider',{}).get('model','glm-5.3'),
            messages=[{"role": "user", "content": text}])
        self.checkpoint(identity)
        try:
            solution = validate_solution(reply.choices[0].message.content, request=request,
                source_digest=binding["source_sha256"], segment_count=len(material["transcription"]["segments"]),
                available_tools=tuple(binding["goal_policy"]["tools"]),
                response_version=response_version)
        except StrategyError as exc:
            raise BridgeError(str(exc)) from None
        if binding["goal_policy"]["version"] >= 5:
            from source_requirements import complete_exact_requirement
            requirement = complete_exact_requirement(request)
            if requirement and any(g["kind"] == "material" and g["required"] for g in solution["gaps"]):
                atomic(directory / "source-sufficiency-findings.json", {
                    "request_quote": requirement, "source_sha256": binding["source_sha256"],
                    "required_material_gap": True, "draft_retained": True,
                    "semantic_quality_approved": False})
                raise BridgeError("source_insufficient_for_exact_request")
        from source_review import review_focus
        original_solution = solution
        eligible = (binding["goal_policy"]["version"] >= 3 and binding.get("material_mode") == "audio_only"
                    and solution["status"] in {"ready", "partial"} and "revision_request" not in binding
                    and "revision_strategy" not in binding)
        focus = review_focus(request, material, solution) if eligible and binding["goal_policy"]["version"] >= 4 else []
        risky = bool(focus) if binding["goal_policy"]["version"] >= 4 else (
            solution["tool_calls"] or any(g["required"] and g["kind"] == "material" for g in solution["gaps"]))
        if eligible and risky:
            if not callable(self.review):
                raise BridgeError("source_review_unavailable")
            self.checkpoint(identity)
            solution = self.review(identity, binding, material, solution, directory)
            self.checkpoint(identity)
        elif eligible and binding['goal_policy']['version'] == 9:
            from task_editor import editing_reason
            reason = editing_reason(request, material, solution)
            if reason is not None:
                if not callable(self.review):
                    raise BridgeError('task_editor_unavailable')
                self.checkpoint(identity)
                self.ledger.update(identity, task_editing_route=reason)
                solution = self.review(identity, binding, material, solution, directory, kind='task_editor')
                self.checkpoint(identity)
        if eligible and binding["goal_policy"]["version"] >= 4:
            remaining = [f for f in review_focus(request, material, solution, original=original_solution)
                         if f["kind"] == "no_guess"]
            if remaining:
                atomic(directory / "source-review-unresolved.json", {"findings": remaining,
                    "source_sha256": binding["source_sha256"], "semantic_quality_approved": False})
                raise BridgeError("source_inference_unverified")
        if binding["goal_policy"]["version"] >= 2:
            from source_literals import unsupported_identifiers
            unsupported = unsupported_identifiers(solution["markdown"], solution["tool_calls"],
                                                  material["transcription"]["segments"])
            if unsupported:
                self.checkpoint(identity)
                atomic(directory / "source-literal-findings.json", {"version": 1,
                    "source_sha256": binding["source_sha256"],
                    "markdown_sha256": hashlib.sha256(solution["markdown"].encode()).hexdigest(),
                    "unsupported_identifiers": unsupported,
                    "semantic_quality_approved": False})
                raise BridgeError("source_literal_unverified")
        markdown, assets = solution["markdown"], []
        for index, call in enumerate(solution["tool_calls"], 1):
            self.checkpoint(identity)
            try:
                if call['name'] == 'render_explanation':
                    records = render_explanation(call['arguments'], directory, 'goal_explanation_' + str(index))
                    assets.extend({**record, 'call_id': call['id']} for record in records)
                    markdown = markdown.replace('[[TOOL:' + call['id'] + ']]',
                        explanation_markdown(identity, call['id'], call['arguments'], records))
                    self.checkpoint(identity)
                    continue
                record = render(call["arguments"], directory, "goal_diagram_" + str(index))
            except (DiagramError, ExplanationError):
                message = "这处说明图未能生成。"
                markdown = markdown.replace("[[TOOL:" + call["id"] + "]]", message)
                solution["gaps"].append({"kind": "capability", "required": call["required"], "description": message})
                if call["required"]:
                    solution["status"] = "partial"
            else:
                assets.append({**record, "role": "derived_diagram", "call_id": call["id"]})
                markdown = markdown.replace("[[TOOL:" + call["id"] + "]]",
                    "![" + call["arguments"]["title"].replace("]", "") + "](/bridge/assets/" + identity + "/" + record["filename"] + ")")
            self.checkpoint(identity)
        solution["markdown"] = markdown
        self._commit(identity, binding, solution, assets)
        proof = material["source_evidence"]
        result = {"markdown": markdown, "transcript": material["transcription"], "audio_meta": {
            "file_path": "", "cover_url": "", "title": proof["part_title"],
            "duration": material["transcription"]["segments"][-1]["end"], "platform": "bilibili", "video_id": proof["bvid"],
            "raw_info": {"webpage_url": f"https://www.bilibili.com/video/{proof['bvid']}?p={proof['page']}"}}}
        self.checkpoint(identity)
        return self.save(identity, result, output)

    def validate(self, row):
        try:
            return self._validate(row)
        except BridgeError:
            raise
        except (OSError, ValueError, KeyError, TypeError):
            raise BridgeError("goal_result_unavailable") from None

    def _validate(self, row):
        identity = row["request_id"]
        binding = self.binding(identity)
        self.checkpoint(identity)
        directory = self.directory(identity)
        path = directory / "manifest.json"
        if not row.get("goal_manifest_sha256") or path.is_symlink() or sha(path) != row["goal_manifest_sha256"]:
            raise BridgeError("goal_manifest_changed")
        value = json.loads(path.read_bytes())
        if (value["owner"] != binding["owner"] or value["request_id"] != identity
                or value["source_sha256"] != row["master_sha256"] or value["scope_sha256"] != digest(binding)):
            raise BridgeError("goal_scope_changed")
        for asset in value["assets"]:
            if asset['role'] == 'derived_diagram':
                patterns = (r'goal_[a-z][a-z0-9_]{0,30}\.png', r'goal_[a-z][a-z0-9_]{0,30}\.dot')
            elif asset['role'] == 'derived_explanation':
                patterns = (r'goal_explanation_[1-9][0-9]*_[A-Za-z0-9][A-Za-z0-9_-]{0,30}\.svg',
                            r'goal_explanation_[1-9][0-9]*\.json')
            else:
                raise BridgeError("goal_asset_changed")
            for (name, fingerprint), pattern in zip(((asset["filename"], asset["sha256"]),
                    (asset["source_filename"], asset["source_sha256"])), patterns):
                if not isinstance(name, str) or not re.fullmatch(pattern, name):
                    raise BridgeError("goal_asset_changed")
                target = directory / name
                if target.is_symlink() or target.is_junction() or sha(target) != fingerprint:
                    raise BridgeError("goal_asset_changed")
        return value

    def explanations(self, notes, identity):
        notes.candidate(identity)
        manifest = self.validate(notes._row(identity))
        views = []
        for call in manifest['solution']['tool_calls']:
            if call['name'] != 'render_explanation':
                continue
            assets = [a for a in manifest['assets'] if a['role'] == 'derived_explanation' and a['call_id'] == call['id']]
            if not assets:
                continue
            args = call['arguments']
            by_panel = {a['panel_id']: a for a in assets}
            if len(by_panel) != len(args['panels']) or len(assets) != len(by_panel):
                raise BridgeError('goal_asset_changed')
            for asset in assets:
                if json.loads((self.directory(identity) / asset['source_filename']).read_bytes()) != args:
                    raise BridgeError('goal_asset_changed')
            views.append({'call_id': call['id'], 'title': args['title'], 'purpose': args['purpose'],
                'subject': args['subject'], 'panels': [{**{k: p[k] for k in ('id', 'title', 'explanation', 'provenance')},
                    'svg_url': '/api/bridge/assets/' + identity + '/' + by_panel[p['id']]['filename']}
                    for p in args['panels']]})
        return views

    def read_asset(self, notes, identity, filename):
        notes.candidate(identity)
        manifest = self.validate(notes._row(identity))
        if not any(asset["filename"] == filename for asset in manifest["assets"]):
            raise BridgeError("goal_asset_not_found")
        return (self.directory(identity) / filename).read_bytes()

    def after_revision(self, identity, path, parent_notes, *, amendment_path=None):
        """Bind each revised visual to the new immutable result, never stale text."""
        from diagram_tool import LAYOUT_VERSION, DiagramError, render
        from explanation_tool import validate_arguments, render as render_explanation
        binding = self.binding(identity)
        parent_id = binding["revision_request"]["parent_job_id"]
        parent_result = parent_notes.candidate(parent_id)['result']
        parent = self.validate(parent_notes._row(parent_id))
        row = self.checkpoint(identity)
        result = json.loads(Path(path).read_bytes())
        updates, additions, removals = {}, [], set()
        if amendment_path is not None:
            amendment_path = Path(amendment_path)
            if amendment_path.is_symlink() or sha(amendment_path) != row.get('amendment_manifest_sha256'):
                raise BridgeError('amendment_manifest_changed')
            proof = json.loads(amendment_path.read_bytes())
            if (proof.get('purpose_scope_sha256') != digest(binding)
                    or proof.get('parent_markdown_sha256') != hashlib.sha256(parent_result['markdown'].encode()).hexdigest()):
                raise BridgeError('amendment_parent_changed')
            updates = {u['call_id']: u['arguments'] for u in proof.get('explanation_updates', [])}
            additions = proof.get('explanation_additions', [])
            removals = set(proof.get('explanation_removals', []))
        solution = copy.deepcopy(parent['solution'])
        calls = {c['id']: c for c in solution['tool_calls']}
        if (set(updates) & removals or any(key not in calls or calls[key]['name'] != 'render_explanation'
                or not any(a['call_id'] == key for a in parent['assets']) for key in set(updates) | removals)):
            raise BridgeError('unregistered_revision_explanation')
        for call in solution['tool_calls']:
            if call['name'] != 'render_explanation' or not any(a['call_id'] == call['id'] for a in parent['assets']):
                continue
            block = r'<!--vaa-explanation:' + re.escape(call['id']) + r'-->[\s\S]*?<!--/vaa-explanation:' + re.escape(call['id']) + r'-->'
            old_blocks = re.findall(block, parent_result['markdown'])
            new_blocks = re.findall(block, result['markdown'])
            if call['id'] in removals:
                if len(old_blocks) != 1 or new_blocks:
                    raise BridgeError('revision_explanation_removal_invalid')
                continue
            if len(old_blocks) != 1 or len(new_blocks) != 1:
                raise BridgeError('revision_explanation_block_changed')
            if call['id'] not in updates and new_blocks != old_blocks:
                raise BridgeError('revision_explanation_requires_tool_update')
        solution['tool_calls'] = [c for c in solution['tool_calls'] if c['id'] not in removals]
        for addition in additions:
            key, arguments = addition['call_id'], addition['arguments']
            if (not isinstance(key, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,30}', key) or key in calls
                    or result['markdown'].count('[[TOOL:' + key + ']]') != 1):
                raise BridgeError('unregistered_revision_explanation')
            call = {'id': key, 'name': 'render_explanation', 'purpose': arguments['purpose'],
                    'required': False, 'arguments': arguments}
            calls[key] = call
            solution['tool_calls'].append(call)
            updates[key] = arguments
        if len(solution['tool_calls']) > 2:
            raise BridgeError('revision_tool_limit')
        material = self.ledger.material(identity)
        for call_id, arguments in updates.items():
            calls[call_id]['arguments'] = validate_arguments(arguments, len(material['transcription']['segments']))
        if updates:
            from source_literals import unsupported_identifiers
            if unsupported_identifiers('', [calls[key] for key in updates], material['transcription']['segments']):
                raise BridgeError('source_literal_unverified')
            from source_review import review_focus
            revised_visible = {**solution, 'markdown': ''}
            if any(f['kind'] == 'no_guess' for f in review_focus(binding['user_request'], material, revised_visible)):
                raise BridgeError('source_inference_unverified')
        directory = self.directory(identity)
        directory.mkdir(exist_ok=False)
        assets = []
        copied = set()
        next_number = max([int(match[1]) for a in parent['assets']
            if (match := re.fullmatch(r'goal_explanation_([1-9][0-9]*)\.json', a['source_filename']))] or [0])
        for call in solution['tool_calls']:
            if call['id'] not in updates:
                continue
            self.checkpoint(identity)
            old_asset = next((a for a in parent['assets'] if a['call_id'] == call['id']), None)
            if old_asset:
                stem = Path(old_asset['source_filename']).stem
            else:
                next_number += 1
                stem = 'goal_explanation_' + str(next_number)
            records = render_explanation(call['arguments'], directory, stem)
            assets.extend({**record, 'call_id': call['id']} for record in records)
            result['markdown'] = result['markdown'].replace('[[TOOL:' + call['id'] + ']]',
                explanation_markdown(identity, call['id'], call['arguments'], records)
                .removeprefix(f'<!--vaa-explanation:{call["id"]}-->')
                .removesuffix(f'<!--/vaa-explanation:{call["id"]}-->').strip())
        for asset in parent["assets"]:
            if asset['call_id'] in updates or asset['call_id'] in removals:
                continue
            old = f"/bridge/assets/{parent_id}/{asset['filename']}"
            if old not in result["markdown"]:
                continue
            updated = None
            if asset['role'] == 'derived_diagram' and asset.get("layout_version", 1) < LAYOUT_VERSION:
                call = next((c for c in parent["solution"]["tool_calls"] if c["id"] == asset["call_id"]), None)
                if call is not None:
                    self.checkpoint(identity)
                    try:
                        updated = render(call["arguments"], directory, Path(asset["filename"]).stem)
                    except DiagramError:
                        updated = None
            if updated is not None:
                asset = {**asset, **updated}
            else:
                for key in ("filename", "source_filename"):
                    if asset[key] not in copied:
                        shutil.copyfile(self.directory(parent_id) / asset[key], directory / asset[key])
                        copied.add(asset[key])
            assets.append(asset)
            result["markdown"] = result["markdown"].replace(old, f"/bridge/assets/{identity}/{asset['filename']}")
        pattern = r"/bridge/assets/[^/]+/(goal_[A-Za-z0-9_-]+\.(?:png|svg))"
        if any(name not in {a["filename"] for a in assets} for name in re.findall(pattern, result["markdown"])):
            raise BridgeError("unregistered_revision_diagram")
        solution["markdown"] = result["markdown"]
        solution["revision_note"] = "按冻结修改指令保存；图文改动与本版本资源绑定。"
        self._commit(identity, binding, solution, assets)
        self.checkpoint(identity)
        atomic(path, result)
        (Path(path).parent / (identity + "_markdown.md")).write_text(result["markdown"], encoding="utf-8")
        return path

    def export(self, notes, identity, base):
        manifest = self.validate(notes._row(identity))
        markdown = notes.candidate(identity)["result"]["markdown"]
        assets = [a for a in manifest["assets"] if f"/bridge/assets/{identity}/{a['filename']}" in markdown]
        if not assets:
            return base
        data, mime, _ = base
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
            written = set()
            if mime == "application/zip":
                with zipfile.ZipFile(io.BytesIO(data)) as old:
                    for name in old.namelist():
                        if name != "notes.md":
                            archive.writestr(name, old.read(name))
                            written.add(name)
                    markdown = old.read("notes.md").decode("utf-8")
            for asset in assets:
                for name, data in (("images/" + asset["filename"], self.read_asset(notes, identity, asset["filename"])),
                        ("diagrams/" + asset["source_filename"], (self.directory(identity) / asset["source_filename"]).read_bytes())):
                    if name not in written:
                        archive.writestr(name, data)
                        written.add(name)
                markdown = markdown.replace(f"/bridge/assets/{identity}/{asset['filename']}", "images/" + asset["filename"])
            archive.writestr("notes.md", markdown.encode("utf-8"))
        return out.getvalue(), "application/zip", "content.zip"
