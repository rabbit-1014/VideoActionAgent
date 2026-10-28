"""Reuse pinned BiliNote and saved frames, with separately authorised region analysis."""
import hashlib
import json
import re
import shutil
import types
from pathlib import Path

from bridge_contract import BridgeError, atomic, digest
from frame_text_runtime import read_frame_text
from purpose_prompt import bound_prompt_version
from vision_pipeline import VisionPipeline, sha
from visual_text_context import prepare_text_context, verify_text_context

from image_filter import eligible_frame_ids
from native_purposes import extras, figures_allowed
from saved_region_analysis import analyse_saved_regions, verify_saved_regions


class ReusedNative(VisionPipeline):
    def __init__(self, root, context, native_generate):
        self.root, self.context, self.native = Path(root), context, native_generate
        self.root.mkdir(parents=True, exist_ok=True)

    def generate(self, identity, material, request, style, client, output):
        ledger = client.service.ledger
        def checkpoint():
            if ledger.get(identity)['state']=='cancelled':
                raise BridgeError('cancelled')
        checkpoint()
        binding, packet, previous = self.context(identity)
        directory = self.root / identity
        directory.mkdir(exist_ok=False)
        snapshot = None
        if packet is not None:
            frames = []
            for original in packet["frames"]:
                destination = directory / Path(original["path"]).name
                if destination.exists():
                    raise BridgeError("reused_frame_name_conflict")
                shutil.copyfile(original["path"], destination)
                frames.append({**original,"path":str(destination)})
            self._verify_frames(directory,frames)
            snapshot = {**packet,"frames":frames,"role":"reused_unverified_frame_model_observation",
                "original_scope_sha256":packet["scope_sha256"],"scope_sha256":digest(binding),
                "reuse_descriptor":binding["visual_material"],"new_visual_calls":0}
            atomic(directory / "visual-material.json",snapshot)
            ledger.update(identity,vision_state="available_saved",vision_reused=True,vision_model_calls=0,
                vision_manifest_sha256=sha(directory/"visual-material.json"))
        else:
            ledger.update(identity,vision_state="frames_unavailable",vision_reused=True,vision_model_calls=0)
        original_client = client
        projection = None
        def verify():
            checkpoint()
            current, _, _ = self.context(identity)
            if current != binding:
                raise BridgeError("purpose_scope_changed")
            if snapshot is not None:
                self._verify_frames(directory,snapshot["frames"])
                if sha(directory/"visual-material.json") != ledger.get(identity)["vision_manifest_sha256"]:
                    raise BridgeError("visual_material_changed")
                verify_text_context(directory,ledger.get(identity))
                verify_saved_regions(directory,ledger.get(identity))
        class Guarded:
            def __init__(self):
                self.purpose_prompt_version=bound_prompt_version(original_client,identity)
                self.chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=self.create))
            def create(self,**kwargs):
                verify()
                value=original_client.chat.completions.create(**kwargs)
                verify()
                return value
        if snapshot is not None and binding.get('revision_strategy') not in {'exact_text_edits_v1','literal_text_edits_v1','line_text_edits_v1'}:
            verify()
            reader=getattr(self,'text_reader',read_frame_text)
            projection,text_sha=prepare_text_context(directory,snapshot,sha(directory/'visual-material.json'),reader)
            verify()
            ledger.update(identity,text_grounding_manifest_sha256=text_sha)
        region_projection = None
        if (snapshot is not None and binding.get('allow_model_images') is True
                and binding.get('goal_policy', {}).get('version', 0) >= 8
                and 'revision_request' not in binding):
            verify()
            try:
                region_projection, region_sha, region_metrics = analyse_saved_regions(
                    directory, binding, material, snapshot, checkpoint=verify,
                    transport=getattr(self, 'region_transport', None))
            finally:
                attempt_path = directory / 'saved-region-attempt.json'
                if attempt_path.is_file():
                    attempt = json.loads(attempt_path.read_bytes())
                    ledger.update(identity, saved_region_state=attempt['state'],
                        vision_model_calls=attempt.get('calls'),
                        saved_region_metrics={key: attempt.get(key) for key in
                            ('calls', 'new_visual_calls', 'received', 'network_dispatched',
                             'usage', 'model', 'elapsed_seconds')})
            ledger.update(identity,saved_region_manifest_sha256=region_sha,
                saved_region_metrics=region_metrics,vision_model_calls=region_metrics['calls'])
            verify()
        generation_packet={**snapshot,'generation_text_projection':projection,
                           'generation_region_projection':region_projection} if snapshot is not None else None
        hint = extras(binding,packet=generation_packet,previous=previous)
        if previous:
            hint += "\n原稿中的图片地址只是旧稿记录；需要保留图片时使用本次可用FRAME标记，不复制旧地址或补造外部图片。"
        verify()
        path = self.native(identity,material,request,binding["native_profile"]["style"],Guarded(),output,
            extras_override=hint,format_override=["toc"] if binding["output_kind"]=="structured_notes" else [])
        verify()
        result = json.loads(Path(path).read_bytes())
        original_markdown = result["markdown"]
        keep = eligible_frame_ids(packet["observation"]) if packet and figures_allowed(binding) else set()
        by_id = {frame["id"]:frame for frame in snapshot["frames"] if frame["id"] in keep} if snapshot else {}
        by_name = {Path(frame["path"]).name:frame for frame in by_id.values()}
        exact = binding.get("revision_strategy") in {"exact_text_edits_v1","literal_text_edits_v1","line_text_edits_v1"}
        expected = original_markdown
        if exact:
            parent_prefix = "/bridge/assets/" + binding["revision_request"]["parent_job_id"] + "/"
            def rebind_registered(match):
                frame = by_name.get(match[2][len(parent_prefix):]) if match[2].startswith(parent_prefix) else None
                return ("![" + match[1] + "](/bridge/assets/" + identity + "/" + Path(frame["path"]).name + ")"
                        if frame else match[0])
            expected = re.sub(r"!\[([^\]\n]*)\]\(([^)\n]+)\)", rebind_registered, original_markdown)
        used = []
        def url(frame):
            if frame["id"] not in used:
                used.append(frame["id"])
            return "/bridge/assets/"+identity+"/"+Path(frame["path"]).name
        markdown = re.sub(r"\[\[FRAME:([^\]]+)\]\]",
            lambda match:"![视频画面]("+url(by_id[match[1]])+")" if match[1] in by_id else "",original_markdown)
        def image(match):
            target = match[2]
            if (binding.get("goal_policy") and re.fullmatch(
                    r"/bridge/assets/(?:" + re.escape(identity) + "|" +
                    re.escape(binding.get("revision_request", {}).get("parent_job_id", identity)) +
                    r")/(?:goal_[a-z][a-z0-9_]{0,30}\.png|goal_explanation_[1-9][0-9]*_[A-Za-z0-9][A-Za-z0-9_-]{0,30}\.svg)", target)):
                # Root's goal manifest verifies and rebinds these derived assets after exact edits.
                return match[0]
            frame = by_name.get(target.rsplit("/",1)[-1]) if target.startswith("/bridge/assets/") else None
            return "!["+match[1]+"]("+url(frame)+")" if frame else ""
        markdown = re.sub(r"!\[([^\]\n]*)\]\(([^)\n]+)\)",image,markdown)
        if exact and markdown != expected:
            raise BridgeError("amendment_image_change_requires_exact_edit")
        if markdown != original_markdown:
            atomic(Path(output)/(identity+".before-registered-images.json"),result)
            result["markdown"] = markdown
            atomic(Path(path),result)
            (Path(output)/(identity+"_markdown.md")).write_text(markdown,encoding="utf-8")
        ledger.update(identity,vision_images_used=used)
        verify()
        return path

    def validate_published(self,row):
        binding, original, previous = self.context(row["request_id"])
        if binding.get("revision_strategy") in {"exact_text_edits_v1","literal_text_edits_v1","line_text_edits_v1"}:
            proof_path=self.root/row["request_id"]/"amendment.json"
            if sha(proof_path)!=row.get("amendment_manifest_sha256"):
                raise BridgeError("amendment_manifest_changed")
            proof=json.loads(proof_path.read_bytes())
            if (proof.get("purpose_scope_sha256")!=digest(binding)
                    or proof.get("parent_markdown_sha256")!=hashlib.sha256(previous.encode()).hexdigest()):
                raise BridgeError("amendment_parent_changed")
        if original is None:
            if row.get("vision_manifest_sha256"):
                raise BridgeError("visual_material_changed")
            return
        directory = self.root / row["request_id"]
        path = directory / "visual-material.json"
        if sha(path) != row.get("vision_manifest_sha256"):
            raise BridgeError("visual_material_changed")
        packet = json.loads(path.read_bytes())
        if (packet.get("reuse_descriptor") != binding.get("visual_material")
                or packet.get("source_sha256") != row["master_sha256"]
                or packet.get("scope_sha256") != digest(binding)):
            raise BridgeError("visual_source_changed")
        self._verify_frames(directory,packet["frames"])
        verify_text_context(directory,row)
        verify_saved_regions(directory,row)
