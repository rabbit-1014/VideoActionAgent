"""One explicit-purpose facade; existing native and structured generators remain authoritative."""
import hashlib
import json
import re
import threading
from functools import wraps
from pathlib import Path
from uuid import UUID

from bridge_contract import BridgeError, atomic, digest
from native_notes import STATE_MAP
from video_action_agent.notes_service import OUTPUT_FILES, NotesServiceError, _source
from video_action_agent.source_access import VideoRef

from followup_context import LINK_KEYS, snapshot, validate_context, validate_link
from goal_native import goal_profile, policy, validate_policy
from native_purposes import explicit_audio_only, profile, validate_profile
from text_amendments import apply_reply
from typed_outputs import TypedOutputs


def read_operation(function):
    """Deduplicate parent verification only within one bounded synchronous read."""
    @wraps(function)
    def read(self, *args, **kwargs):
        outer = getattr(self._reading, "parents", None) is None
        if outer:
            self._reading.parents = {}
        try:
            return function(self, *args, **kwargs)
        finally:
            if outer:
                del self._reading.parents
    return read


class PurposeOutputs:
    def __init__(self, owner, root, sources, native, pipeline, provider_factory, *, saved_natives=None,
                 saved_visuals=None, saved_pipeline=None, goal_policy_factory=None):
        self.owner, self.output_root, self._capture = owner, Path(root), sources
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.bindings = self.output_root / ".purposes"
        self.bindings.mkdir(exist_ok=True)
        self.native, self.pipeline = native, pipeline
        self.saved_natives = saved_natives or {}
        self.saved_visuals, self.saved_pipeline = saved_visuals, saved_pipeline
        self._goal_policy = goal_policy_factory or policy
        self.goals = None
        self._lock = threading.RLock()
        self._reading = threading.local()
        self._admitting = set()
        self.typed = TypedOutputs(owner, self.output_root / "typed", sources, provider_factory, self.checkpoint)

    def binding(self, identity):
        try:
            identity = str(UUID(str(identity)))
            path = self.bindings / (identity + ".json")
            value = json.loads(path.read_bytes())
            local_only = (value.get("allow_local_edits") is True and value.get("allow_model_text") is False
                          and value.get("engine") == "native"
                          and value.get("revision_strategy") == "literal_text_edits_v1")
            if (value["owner"] != self.owner or value["request_id"] != identity
                    or value["output_kind"] not in OUTPUT_FILES
                    or not (value["allow_model_text"] is True or local_only)
                    or ("allow_local_edits" in value and not local_only)):
                raise ValueError()
            revision = value.get("revision_request")
            if revision is not None and (not isinstance(revision, dict)
                        or set(revision) != {"parent_job_id", "expected_parent_fingerprint", "instruction"}
                        or str(UUID(revision["parent_job_id"])) != revision["parent_job_id"]
                        or (not self.is_native(value) and value.get("service_source_id") != "artifact:" + revision["parent_job_id"])
                        or not isinstance(revision["expected_parent_fingerprint"], str)
                        or not re.fullmatch(r"[0-9a-f]{64}", revision["expected_parent_fingerprint"])
                        or not isinstance(revision["instruction"], str)
                        or not 1 <= len(revision["instruction"].strip()) <= 4000):
                raise ValueError()
            if value.get("engine") == "native":
                validate_profile(value["native_profile"], value["output_kind"])
                if revision is not None or 'native_lineage' in value:
                    lineage = value.get('native_lineage')
                    if (not isinstance(lineage, dict)
                            or set(lineage) != {'root_job_id', 'version', 'parent_job_id'}
                            or not isinstance(lineage['root_job_id'], str)
                            or str(UUID(lineage['root_job_id'])) != lineage['root_job_id']
                            or type(lineage['version']) is not int):
                        raise ValueError()
                    if revision is None:
                        if (lineage['root_job_id'] != identity or lineage['version'] != 1
                                or lineage['parent_job_id'] is not None):
                            raise ValueError()
                    elif (not 2 <= lineage['version'] <= 64
                            or not isinstance(lineage['parent_job_id'], str)
                            or str(UUID(lineage['parent_job_id'])) != lineage['parent_job_id']
                            or lineage['parent_job_id'] != revision['parent_job_id']):
                        raise ValueError()
                if "goal_policy" in value:
                    validate_policy(value["goal_policy"])
                    if value["native_profile"]["version"] != 6:
                        raise ValueError()
                elif value["native_profile"]["version"] == 6:
                    raise ValueError()
                if value["material_mode"] not in {"audio_only", "fresh_visual", "saved_visual"}:
                    raise ValueError()
            strategy = value.get("revision_strategy")
            if 'followup_context' in value:
                if not self.is_native(value):
                    raise ValueError()
                validate_context(value['followup_context'], identity)
            if strategy is not None and (not self.is_native(value)
                    or revision is None or strategy not in {"exact_text_edits_v1","literal_text_edits_v1","line_text_edits_v1"}):
                raise ValueError()
            if strategy == "literal_text_edits_v1":
                if not isinstance(value.get("exact_edits"),list) or not 1 <= len(value["exact_edits"]) <= 16:
                    raise ValueError()
            elif "exact_edits" in value:
                raise ValueError()
            return value
        except (OSError, ValueError, KeyError, TypeError):
            raise NotesServiceError("output_not_found") from None

    @read_operation
    def checkpoint(self, identity):
        value = self.binding(identity)
        try:
            bundle = self._capture.bundle(value["source_id"])
            if digest(bundle) != value["source_sha256"]:
                raise ValueError()
            if not self.is_native(value):
                journal = self.typed._store.load(value["request_id"])
                if journal:
                    request = journal["request"]
                    revision = value.get("revision_request")
                    expected_source = value["service_source_id"] if revision else value["source_id"]
                    if (request["source_id"] != expected_source
                            or any(request[key] != value[key] for key in ("user_request", "output_kind"))
                            or request["model_text_authorized"] is not True
                            or request["source_digest"] != _source(bundle, value["output_kind"])[2]):
                        raise ValueError()
                    if revision:
                        actual = request.get("revision", {})
                        if (actual.get("parent_job_id") != revision["parent_job_id"]
                                or actual.get("parent_record_sha256") != revision["expected_parent_fingerprint"]
                                or actual.get("instruction") != revision["instruction"]):
                            raise ValueError()
            else:
                row = self._service(value)._row(value["request_id"])
                if (row["master_id"], row["user_request"]) != (value["source_id"], value["user_request"]):
                    raise ValueError()
                if value.get("visual_material") is not None:
                    if self.saved_visuals is None:
                        raise ValueError()
                    self.saved_visuals.load(value["visual_material"], value["source_id"], bundle)
                if value.get("revision_request"):
                    self._native_parent(value)
        except (ValueError, KeyError, OSError):
            raise NotesServiceError("output_source_changed") from None
        return bundle

    def _service(self, binding):
        if self.is_native(binding):
            if binding["request_id"] in self.saved_natives:
                return self.saved_natives[binding["request_id"]][0]
            if self.native is None:
                raise NotesServiceError("notes_unavailable")
            return self.native
        return self.typed

    @staticmethod
    def is_native(binding):
        return binding.get("engine", "native" if binding["output_kind"] == "structured_notes" else "typed") == "native"

    def _visual_scope(self, identity, source_id, request, bundle):
        intents = getattr(self.pipeline, "intents", None)
        return None if intents is None else intents.get(identity,
            {"owner":self.owner,"master_id":source_id,"user_request":request}, bundle["source_envelope"])

    def _native_revision(self, binding, result):
        lineage = binding.get("native_lineage", {"root_job_id":binding["request_id"], "version":1, "parent_job_id":None})
        return {**lineage, "record_fingerprint":digest({"binding":binding,"result":result})}

    def _native_parent_result(self, binding):
        revision = binding["revision_request"]
        parent = self.binding(revision["parent_job_id"])
        if (not self.is_native(parent) or any(parent[k] != binding[k] for k in
                ("owner", "source_id", "source_sha256", "user_request", "output_kind"))):
            raise NotesServiceError("revision_parent_changed")
        cache = getattr(self._reading, "parents", {})
        parent_id = parent["request_id"]
        if parent_id not in cache:
            cache[parent_id] = self._service(parent).candidate(parent_id)["result"]
        result = cache[parent_id]
        if self._native_revision(parent, result)["record_fingerprint"] != revision["expected_parent_fingerprint"]:
            raise NotesServiceError("revision_parent_changed")
        return result

    def _native_parent(self, binding):
        return self._native_parent_result(binding)["markdown"]

    @read_operation
    def native_context(self, identity):
        binding = self.binding(identity)
        bundle = self.checkpoint(identity)
        packet = (self.saved_visuals.load(binding["visual_material"], binding["source_id"], bundle)
                  if binding.get("visual_material") is not None else None)
        previous = self._native_parent(binding) if binding.get("revision_request") else None
        return binding, packet, previous

    @read_operation
    def amendment_context(self, identity):
        binding, packet, _ = self.native_context(identity)
        previous = self._native_parent_result(binding)
        if binding.get('goal_policy', {}).get('version', 0) >= 16:
            parent_id = binding['revision_request']['parent_job_id']
            manifest = self.goals.validate(self._service(self.binding(parent_id))._row(parent_id))
            bound = [{'call_id': c['id'], 'arguments': c['arguments']} for c in manifest['solution']['tool_calls']
                     if c['name'] == 'render_explanation' and any(a['call_id'] == c['id'] for a in manifest['assets'])]
            packet = {**(packet or {}), 'bound_explanations': bound,
                'reserved_call_ids': [c['id'] for c in manifest['solution']['tool_calls']],
                'max_new_explanations': max(0, 2 - len(manifest['solution']['tool_calls']))}
        if binding.get('revision_strategy') != 'line_text_edits_v1' or packet is None:
            return previous, packet
        if binding.get('visual_material') is None:
            return previous, packet
        from revision_visual_context import augment_packet
        root_id = binding['native_lineage']['root_job_id']
        root = self.binding(root_id)
        cache = self._reading.parents
        if root_id not in cache:
            cache[root_id] = self._service(root).candidate(root_id)['result']
        row = self._service(root)._row(root_id)
        packet = augment_packet(binding, packet, root_binding=root, row=row,
                                directory=Path(self._pipeline_for(root).root)/root_id)
        return previous, packet

    def _pipeline_for(self, binding):
        if binding["request_id"] in self.saved_natives:
            return self.saved_natives[binding["request_id"]][1]
        return self.saved_pipeline if binding.get("material_mode") == "saved_visual" else self.pipeline

    def adopt_saved_native(self, identity):
        service, _ = self.saved_natives[identity]
        row = service._row(identity)
        bundle = self._capture.bundle(row["master_id"])
        if row["master_sha256"] != digest(bundle):
            raise NotesServiceError("output_source_changed")
        service.candidate(identity)
        value = {"owner": self.owner, "request_id": identity, "source_id": row["master_id"],
                 "user_request": row["user_request"], "output_kind": "structured_notes",
                 "source_sha256": digest(bundle), "allow_model_text": True}
        path = self.bindings / (identity + ".json")
        if path.exists() and self.binding(identity) != value:
            raise NotesServiceError("output_request_conflict")
        if not path.exists():
            atomic(path, value)

    def start(self, request_id, source_id, user_request, *, output_kind="structured_notes", allow_model_text=False,
              use_saved_visuals=False, goal_led=None, allow_model_images=False, followup=None):
        if allow_model_text is not True:
            raise NotesServiceError("model_text_permission_required")
        try:
            identity, source_id = str(UUID(str(request_id))), str(UUID(str(source_id)))
            if (goal_led is not None and type(goal_led) is not bool) or type(use_saved_visuals) is not bool or type(allow_model_images) is not bool or (allow_model_images and not use_saved_visuals) or not isinstance(output_kind, str) or output_kind not in OUTPUT_FILES or not isinstance(user_request, str) or not 1 <= len(user_request.strip()) <= 4000:
                raise ValueError()
            bundle = self._capture.bundle(source_id)
            if followup is not None:
                validate_link(followup)
        except (ValueError, TypeError):
            raise NotesServiceError("invalid_output_request") from None
        binding = {"owner": self.owner, "request_id": identity, "source_id": source_id,
                   "user_request": user_request.strip(), "output_kind": output_kind,
                   "source_sha256": digest(bundle), "allow_model_text": True}
        with self._lock:
            path = self.bindings / (identity + ".json")
            scope = self._visual_scope(identity, source_id, user_request.strip(), bundle)
            media = bool(scope and (scope["allow_local_video_read"] or scope["allow_model_images"]))
            # New requests reuse the installed note engine for every text purpose.
            # An existing request keeps its frozen engine, including failed attempts.
            native = self.native is not None or output_kind == "structured_notes" or use_saved_visuals or media
            if path.exists():
                old = self.binding(identity)
                if (any(old[key] != value for key,value in binding.items())
                        or (old.get("material_mode") == "saved_visual") != use_saved_visuals
                        or old.get("allow_model_images", False) != allow_model_images
                        or (goal_led is not None and ("goal_policy" in old) != (goal_led and self.goals is not None and native))
                        or old.get("revision_request") is not None):
                    raise NotesServiceError("output_request_conflict")
                prior_link = old.get('followup_context')
                if (None if prior_link is None else {k: prior_link[k] for k in LINK_KEYS}) != followup:
                    raise NotesServiceError('output_request_conflict')
                binding = old
            else:
                if self._admitting or any(row["state"] in {"queued", "running", "cancel_requested"}
                                           for row in self.overview()["jobs"]):
                    raise NotesServiceError("outputs_busy")
                # Immutable metadata before service work; a failed admission does not authorize replay elsewhere.
                binding["engine"] = "native" if native else "typed"
                if followup is not None:
                    binding['followup_context'] = self._followup_snapshot(identity, source_id, followup, native)
                if native:
                    binding.update(native_profile=profile(output_kind, user_request.strip()),
                        material_mode="saved_visual" if use_saved_visuals else "fresh_visual" if media else "audio_only")
                    if goal_led is not False and self.goals is not None:
                        binding.update(native_profile=goal_profile(), goal_policy=self._goal_policy())
                    if use_saved_visuals:
                        binding["visual_material"] = self.saved_visuals.describe(source_id,bundle) if self.saved_visuals else None
                        if allow_model_images:
                            if binding["visual_material"] is None:
                                raise NotesServiceError("saved_visual_material_unavailable")
                            binding["allow_model_images"] = True
                atomic(path, binding)
            self._admitting.add(identity)
        try:
            self._service(binding).start(identity, source_id, user_request,
                output_kind="structured_notes" if self.is_native(binding) else output_kind, allow_model_text=True)
        finally:
            with self._lock:
                self._admitting.discard(identity)
        return self.get(identity)

    @read_operation
    def _followup_snapshot(self, identity, source_id, link, native):
        if not native or link['parent_job_id'] == identity:
            raise NotesServiceError('invalid_followup_request')
        try:
            parent = self.binding(link['parent_job_id'])
            if not self.is_native(parent) or parent['source_id'] != source_id:
                raise NotesServiceError('followup_parent_changed')
            candidate = self.candidate(parent['request_id'])
            if candidate['revision']['record_fingerprint'] != link['expected_parent_fingerprint']:
                raise NotesServiceError('followup_parent_changed')
            return snapshot(link, parent, candidate, identity)
        except NotesServiceError:
            raise NotesServiceError('followup_parent_changed') from None
        except ValueError as exc:
            code = 'followup_context_limit' if str(exc) == 'followup_context_limit' else 'invalid_followup_request'
            raise NotesServiceError(code) from None

    def revise(self, parent_job_id, request_id, expected_parent_fingerprint, instruction, *, allow_model_text=False,
               exact_edits=None, allow_local_edits=False):
        if type(allow_local_edits) is not bool or (allow_local_edits and (
                allow_model_text is not False or exact_edits is None)):
            raise NotesServiceError("invalid_local_edit_permission")
        if not allow_local_edits and allow_model_text is not True:
            raise NotesServiceError("model_text_permission_required")
        try:
            parent_id, identity = str(UUID(str(parent_job_id))), str(UUID(str(request_id)))
            if (not isinstance(instruction, str) or not 1 <= len(instruction.strip()) <= 4000
                    or not isinstance(expected_parent_fingerprint, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", expected_parent_fingerprint)):
                raise ValueError()
        except (ValueError, TypeError):
            raise NotesServiceError("invalid_revision_request") from None
        with self._lock, self.typed._lock:
            parent_binding = self.binding(parent_id)
            self.checkpoint(parent_id)
            if self.is_native(parent_binding):
                return self._revise_native(parent_binding, identity, expected_parent_fingerprint, instruction.strip(),
                                          exact_edits=exact_edits, allow_model_text=allow_model_text,
                                          allow_local_edits=allow_local_edits)
            if exact_edits is not None:
                raise NotesServiceError("local_edits_require_native_parent")
            parent = self.typed.candidate(parent_id)
            if parent["revision"]["record_fingerprint"] != expected_parent_fingerprint:
                raise NotesServiceError("revision_parent_changed")
            binding = {key: parent_binding[key] for key in (
                "owner", "source_id", "user_request", "output_kind", "source_sha256", "allow_model_text")}
            binding.update(request_id=identity, service_source_id="artifact:" + parent_id,
                revision_request={"parent_job_id": parent_id, "expected_parent_fingerprint": expected_parent_fingerprint,
                                  "instruction": instruction.strip()})
            path = self.bindings / (identity + ".json")
            if path.exists():
                if self.binding(identity) != binding:
                    raise NotesServiceError("output_request_conflict")
            else:
                if self._admitting or any(row["state"] in {"queued", "running", "cancel_requested"}
                                           for row in self.overview()["jobs"]):
                    raise NotesServiceError("outputs_busy")
                head, _ = self.typed._family_inventory(parent["revision"]["root_job_id"])
                if head != parent_id:
                    raise NotesServiceError("revision_parent_superseded")
                atomic(path, binding)
            self.typed.revise(parent_id, identity, expected_parent_fingerprint, instruction, allow_model_text=True)
        return self.get(identity)

    def _revise_native(self, parent_binding, identity, fingerprint, instruction, *, exact_edits=None,
                       allow_model_text=True, allow_local_edits=False):
        parent_id = parent_binding["request_id"]
        result = self._service(parent_binding).candidate(parent_id)["result"]
        parent = self._native_revision(parent_binding,result)
        if parent["record_fingerprint"] != fingerprint:
            raise NotesServiceError("revision_parent_changed")
        literal = None
        if exact_edits is not None:
            try:
                literal = json.loads(json.dumps(exact_edits,ensure_ascii=False,allow_nan=False))
                apply_reply(json.dumps({"schema_version":1,"request_id":identity,
                    "source_sha256":parent_binding["source_sha256"],
                    "parent_markdown_sha256":hashlib.sha256(result["markdown"].encode()).hexdigest(),
                    "edits":literal},ensure_ascii=False,allow_nan=False),result["markdown"],
                    request_id=identity,source_sha256=parent_binding["source_sha256"])
            except (BridgeError,TypeError,ValueError):
                raise NotesServiceError("invalid_exact_edits") from None
        binding = {key:parent_binding[key] for key in
                   ("owner","source_id","user_request","output_kind","source_sha256","allow_model_text")}
        binding["allow_model_text"] = allow_model_text
        if allow_local_edits:
            binding["allow_local_edits"] = True
        binding.update(request_id=identity, engine="native", material_mode="saved_visual",
            revision_strategy="line_text_edits_v1",
            native_profile=parent_binding.get("native_profile") or profile(parent_binding["output_kind"]),
            revision_request={"parent_job_id":parent_id,"expected_parent_fingerprint":fingerprint,"instruction":instruction},
            native_lineage={"root_job_id":parent["root_job_id"],"version":parent["version"]+1,"parent_job_id":parent_id})
        if "goal_policy" in parent_binding:
            binding["goal_policy"] = parent_binding["goal_policy"]
        if 'followup_context' in parent_binding:
            binding['followup_context'] = parent_binding['followup_context']
        if literal is not None:
            binding.update(revision_strategy="literal_text_edits_v1",exact_edits=literal)
        bundle = self._capture.bundle(binding["source_id"])
        row = self._service(parent_binding)._row(parent_id)
        binding["visual_material"] = parent_binding.get("visual_material")
        if binding["visual_material"] is None and row.get("vision_manifest_sha256") and self.saved_visuals:
            binding["visual_material"] = self.saved_visuals.describe(binding["source_id"],bundle,donor_id=parent_id)
        path = self.bindings / (identity+".json")
        if path.exists():
            old = self.binding(identity)
            if "revision_strategy" not in old:
                binding.pop("revision_strategy",None)
            elif literal is None and old["revision_strategy"] in {"exact_text_edits_v1", "line_text_edits_v1"}:
                binding['revision_strategy'] = old['revision_strategy']
            binding["native_lineage"] = old.get("native_lineage")
            if old != binding:
                raise NotesServiceError("output_request_conflict")
        else:
            if self._admitting or any(v["state"] in {"queued","running","cancel_requested"} for v in self.overview()["jobs"]):
                raise NotesServiceError("outputs_busy")
            members = []
            for candidate_path in self.bindings.glob("*.json"):
                try:
                    candidate = json.loads(candidate_path.read_bytes())
                    if not isinstance(candidate, dict):
                        raise TypeError()
                except (OSError, ValueError, TypeError, AttributeError):
                    raise NotesServiceError('output_not_found') from None
                # Only a provably separate initial root can be excluded before
                # policy validation. Never skip damaged/ambiguous revision data.
                try:
                    canonical_id = str(UUID(candidate_path.stem)) == candidate_path.stem
                except ValueError:
                    canonical_id = False
                independent = (canonical_id and candidate.get('owner') == self.owner
                    and candidate.get('request_id') == candidate_path.stem
                    and candidate_path.stem != parent['root_job_id']
                    and all(key not in candidate for key in
                            ('native_lineage', 'revision_request', 'revision_strategy')))
                if independent:
                    continue
                checked = self.binding(candidate_path.stem)
                lineage = checked.get('native_lineage', {})
                if not isinstance(lineage, dict):
                    raise NotesServiceError('output_not_found')
                if lineage.get('root_job_id') == parent['root_job_id']:
                    members.append(checked)
            versions = [v["native_lineage"]["version"] for v in members]
            published = [parent["version"]]
            for member in members:
                service = self._service(member)
                if not (service.ledger.root/(member["request_id"]+".json")).exists():
                    continue  # An admission interrupted before creating the native row still consumes its number.
                row = service._row(member["request_id"])
                if row["state"] == "available":
                    service.candidate(member["request_id"])
                    published.append(member["native_lineage"]["version"])
            if max(published) > parent["version"]:
                raise NotesServiceError("revision_parent_superseded")
            next_version = max([parent["version"],*versions])+1
            if next_version > 64:
                raise NotesServiceError("invalid_revision_request")
            binding["native_lineage"]["version"] = next_version
            atomic(path,binding)
        self.native.start(identity,binding["source_id"],binding["user_request"],allow_model_text=allow_model_text,
                          allow_local_edits=allow_local_edits)
        return self.get(identity)

    @read_operation
    def get(self, identity):
        binding = self.binding(identity)
        identity = binding["request_id"]
        view = self._service(binding).get(identity)
        if 'followup_context' in binding:
            view['followup'] = {key: binding['followup_context'][key] for key in LINK_KEYS}
        proof = self.checkpoint(identity)["source_envelope"]["source_evidence"]
        if not self.is_native(binding):
            trace = self.typed.trace(identity)
            view = {**view, "model_calls": trace["model_calls"]}
            if view["state"] not in {"available", "canceled", "queued", "running"} and trace.get("state") in {"dispatching", "outcome_unknown"}:
                view.update(state="interrupted", error_code="model_outcome_unknown")
        else:
            row = self._service(binding)._row(identity)
            view['generation_model'] = ((row.get('model_measurements') or {}).get('generation') or {}).get('model') or (
                '本地修改' if binding.get('revision_strategy') == 'literal_text_edits_v1' else
                'glm-5.3' if binding.get('revision_strategy') else
                binding.get('goal_policy',{}).get('generation_provider',{}).get('model','glm-5.3'))
            if "goal_policy" in binding and row["state"] == "available":
                if self.goals is None:
                    raise NotesServiceError("goal_strategy_unavailable")
                solution = self.goals.validate(row)["solution"]
                view.update(solution_status=solution["status"], solution_gaps=solution["gaps"], goal_led=True,
                    proposed_deliverable=solution["proposed_deliverable"],
                    revision_note=solution.get("revision_note"))
            view.update(vision_state=row.get("vision_state"), vision_model_calls=row.get("vision_model_calls", 0))
            if row.get("vision_state") in {"not_authorized", "frames_unavailable", "outcome_unknown", "invalid_visual_reply", "rejected"}:
                view["vision_note"] = "本次成果仅依据声音文字，画面中的细节可能缺失。"
            missing_saved = binding.get("material_mode") == "saved_visual" and binding.get("visual_material") is None
            if missing_saved and binding.get("revision_request"):
                origin = self.binding(binding["native_lineage"]["root_job_id"])
                origin_state = self._service(origin)._row(origin["request_id"]).get("vision_state")
                if origin.get("material_mode") == "audio_only" and (
                        origin_state in {None, "not_needed"} or explicit_audio_only(origin["user_request"])):
                    missing_saved = False
                    view.pop("vision_note", None)
                    view["vision_state"] = "audio_only_requested"
            if missing_saved:
                view["vision_note"] = "已存画面材料不足，本次仅依据声音文字；依赖画面的内容无法确认。"
            if binding.get("material_mode") == "audio_only" and explicit_audio_only(binding["user_request"]):
                view.pop("vision_note", None)
                view["vision_state"] = "audio_only_requested"
            if "goal_policy" in binding and binding.get("material_mode") == "audio_only":
                # The goal result names material gaps when they affect the request.
                view.pop("vision_note", None)
                view["vision_state"] = "audio_only_available"
        return {**view, "job_id": identity, "user_request": binding["user_request"],
                "revision_strategy":binding.get("revision_strategy"),
                "source_id": binding["source_id"], "master_id": binding["source_id"],
                "output_kind": binding["output_kind"],
                "goal_led": "goal_policy" in binding,
                "source_url": f"https://www.bilibili.com/video/{proof['bvid']}?p={proof['page']}",
                "result_available": view["state"] == "available", "quality_approved": False,
                "formal_task": False, "completed": False}

    @read_operation
    def candidate(self, identity):
        binding = self.binding(identity)
        view = self.get(identity)
        if view["state"] != "available":
            raise NotesServiceError("output_not_ready")
        if self.is_native(binding):
            result = self._service(binding).candidate(identity)["result"]
            explanations = (self.goals.explanations(self._service(binding), identity)
                            if binding.get('goal_policy', {}).get('version', 0) >= 16 else [])
            if 'followup_context' in binding:
                # Read-only projection of the validated, admission-time snapshot.
                # Clients never supply these bodies when continuing a conversation.
                view['conversation_turns'] = json.loads(json.dumps(binding['followup_context']['turns']))
            roles = (["asr_transcription", "saved_frame_model_observation"] if binding.get("visual_material") else
                     ["asr_transcription", "frame_model_observation"] if self._service(binding)._row(identity).get("vision_manifest_sha256") else
                     ["asr_transcription_only"])
            return {**view, "result":result,"revision":self._native_revision(binding,result),"material_roles":roles,
                    "explanations": explanations,
                    "local_edits_available":True,
                    "generation_engine":("local_exact_edits_with_bilinote_save" if binding.get("revision_strategy")=="literal_text_edits_v1"
                                         else "localized_amendment_with_bilinote_save" if binding.get("revision_strategy")
                                         else "pinned_bilinote_native")}
        projection = self.typed.candidate(identity)
        envelope = self.checkpoint(identity)["source_envelope"]
        proof = envelope["source_evidence"]
        markdown = projection["reading_markdown"].replace("# 针对母版的回答", "# 问题回答", 1)
        result = {"markdown": markdown, "transcript": envelope["transcription"],
                  "audio_meta": {"title": proof["part_title"], "duration": proof["stream_duration_seconds"],
                                 "platform": "bilibili", "video_id": proof["bvid"], "cover_url": "",
                                 "raw_info": {"webpage_url": view["source_url"]}}}
        return {**view, "result": result, "source_status": projection["candidate"].get("source_status"),
                "material_roles": ["asr_transcription_only"], "revision": projection["revision"]}

    def current(self):
        # Flow admission needs liveness, not permission to read every saved result.
        jobs = self.overview()["jobs"]
        active = next((job for job in jobs if job["state"] in {"queued", "running"}), None)
        return {"enabled": True, "job": active or (jobs[-1] if jobs else None)}

    def history(self):
        jobs = []
        for path in self.bindings.glob("*.json"):
            try:
                jobs.append(self.get(path.stem))
            except (NotesServiceError, BridgeError):
                # Preserve identity and an honest unavailable state instead of hiding a corrupted source.
                try:
                    binding = self.binding(path.stem)
                    jobs.append({"job_id": path.stem, "state": "interrupted", "error_code": "output_source_changed",
                                 "user_request": binding["user_request"], "output_kind": binding["output_kind"],
                                 "source_url": "", "formal_task": False, "completed": False, "result_available": False})
                except NotesServiceError:
                    continue
        return {"jobs": jobs}

    def overview(self):
        """Stored metadata only; never a verified candidate or a grant to reuse it."""
        jobs = []
        with self._lock:
            for path in self.bindings.glob('*.json'):
                try:
                    binding = self.binding(path.stem)
                    identity = binding['request_id']
                    if self.is_native(binding):
                        service = self._service(binding)
                        row = service._row(identity)
                        state = STATE_MAP.get(row['state'],'failed')
                        thread = service._threads.get(identity)
                        restored = state in {'queued','running'} and (not thread or not thread.is_alive())
                        source_url = row['source_url']
                        lineage = binding.get('native_lineage',{'root_job_id':identity,'version':1,'parent_job_id':None})
                        calls = row['model_calls']
                    else:
                        with self.typed._lock:
                            live = self.typed._jobs.get(identity)
                            record = None if live else self.typed._store.load(identity)
                            if not live and record is None:
                                continue
                            row = dict(live['view'] if live else record['view'])
                            thread = live.get('thread') if live else None
                        state = row['state']
                        restored = state in {'queued','running','cancel_requested'} and (not thread or not thread.is_alive())
                        source_url = f"https://www.bilibili.com/video/{row['bvid']}?p={row['page']}"
                        lineage = {key:row.get(key,identity if key=='root_job_id' else 1 if key=='version' else None)
                                   for key in ('root_job_id','version','parent_job_id')}
                        calls = self.typed.trace(identity)['model_calls']
                    ref = VideoRef.parse(source_url)
                    jobs.append({'job_id':identity,'request_id':identity,'source_id':binding['source_id'],
                        'master_id':binding['source_id'],'output_kind':binding['output_kind'],
                        'goal_led':'goal_policy' in binding,
                        'user_request':binding['user_request'],'bvid':ref.bvid,'page':ref.page,
                        'source_url':source_url,'created_at':row.get('created_at',''),
                        'state':'interrupted' if restored else state,
                        'error_code':'previous_run_unconfirmed' if restored else row.get('error_code'),
                        'revision':lineage,'model_calls':calls,'result_available':False,
                        'read_unverified':True,'formal_task':False,'completed':False,'quality_approved':False})
                except (NotesServiceError,BridgeError,ValueError,OSError,KeyError,TypeError):
                    continue
        return {'jobs':jobs}

    @read_operation
    def download(self, identity):
        binding = self.binding(identity)
        if self.is_native(binding):
            self.checkpoint(identity)
            service = self._service(binding)
            data,mime,name = self._pipeline_for(binding).download(service, identity)
            if "goal_policy" in binding:
                if self.goals is None:
                    raise NotesServiceError("goal_strategy_unavailable")
                data,mime,name = self.goals.export(service, identity, (data,mime,name))
                return data,mime,"content.zip" if mime == "application/zip" else "content.md"
            if binding["output_kind"] != "structured_notes":
                name = OUTPUT_FILES[binding["output_kind"]].replace(".md",".zip") if name.endswith(".zip") else OUTPUT_FILES[binding["output_kind"]]
            return data,mime,name
        markdown = self.candidate(identity)["result"]["markdown"]
        return markdown.encode("utf-8"), "text/markdown; charset=utf-8", OUTPUT_FILES[binding["output_kind"]]

    @read_operation
    def asset(self, identity, filename):
        binding = self.binding(identity)
        self.checkpoint(identity)
        if not self.is_native(binding):
            raise NotesServiceError("output_asset_not_found")
        if "goal_policy" in binding and filename.startswith("goal_"):
            if self.goals is None:
                raise NotesServiceError("goal_strategy_unavailable")
            return self.goals.read_asset(self._service(binding), identity, filename)
        pipeline = self._pipeline_for(binding)
        return pipeline.read_asset(self._service(binding), identity, filename)

    def matches_request(self, identity, source_id, user_request, output_kind):
        binding = self.binding(identity)
        return (binding["source_id"], binding["user_request"], binding["output_kind"]) == (source_id, user_request, output_kind)

    def cancel(self, identity):
        binding = self.binding(identity)
        self._service(binding).cancel(identity)
        return self.get(identity)

    def wait(self, identity, timeout=.1):
        self._service(self.binding(identity)).wait(identity, timeout)

    def close(self):
        self.typed.close()
        if self.native is not None:
            self.native.close()
