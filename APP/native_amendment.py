"""One exact amendment proposal over immutable native results and native file saving."""
import hashlib
import json
from pathlib import Path

from bridge_contract import BridgeError, atomic, digest

import addressed_amendments
from text_amendments import apply_reply, build_prompt


class NativeAmendment:
    def __init__(self, generate, save, binding, parent, record, root, local_ready=None):
        self.generate, self.save, self.binding, self.parent, self.record = (
            generate, save, binding, parent, record
        )
        self.root = Path(root)
        self.local_ready = local_ready

    def __call__(
        self,
        identity,
        material,
        request,
        style,
        client,
        output,
        *,
        extras_override=None,
        format_override=None,
    ):
        binding = self.binding(identity)
        strategy = binding.get("revision_strategy")
        if strategy not in ("exact_text_edits_v1", "literal_text_edits_v1", "line_text_edits_v1"):
            return self.generate(
                identity,
                material,
                request,
                style,
                client,
                output,
                extras_override=extras_override,
                format_override=format_override,
            )

        previous, packet = self.parent(identity)
        parent_markdown = previous["markdown"]
        if strategy == "literal_text_edits_v1":
            if not callable(self.local_ready):
                raise BridgeError("literal_local_ready_unavailable")
            reply = json.dumps(
                {
                    "schema_version": 1,
                    "request_id": identity,
                    "source_sha256": binding["source_sha256"],
                    "parent_markdown_sha256": hashlib.sha256(
                        parent_markdown.encode("utf-8")
                    ).hexdigest(),
                    "edits": binding["exact_edits"],
                },
                ensure_ascii=False,
                allow_nan=False,
            )
            markdown, proof = apply_reply(
                reply,
                parent_markdown,
                request_id=identity,
                source_sha256=binding["source_sha256"],
            )
            self.local_ready(identity)
            execution_mode = "local_exact_edits"
        else:
            from followup_context import material_context
            addressed = strategy == 'line_text_edits_v1'
            version = binding.get('goal_policy', {}).get('version')
            explanation_edit = addressed and type(version) is int and version >= 16
            if explanation_edit:
                import explanation_amendments
                make_prompt, apply = explanation_amendments.build_prompt, explanation_amendments.apply_reply
            else:
                make_prompt = addressed_amendments.build_prompt if addressed else build_prompt
                apply = addressed_amendments.apply_reply if addressed else apply_reply
            prompt = make_prompt(binding, parent_markdown, material_context(material, binding), packet=packet)
            reply = client.chat.completions.create(
                model=(binding['goal_policy']['generation_provider']['model']
                    if type(version) is int and version >= 16 else 'glm-5.3'),
                messages=[{"role": "user", "content": prompt}]
            )
            amendment_options = {}
            if explanation_edit:
                explanation_packet = packet or {}
                amendment_options = {'bound_explanations': explanation_packet.get('bound_explanations', []),
                                     'reserved_call_ids': explanation_packet.get('reserved_call_ids'),
                                     'max_new_explanations': explanation_packet.get('max_new_explanations'),
                                     'segment_count': len(material['transcription']['segments'])}
            markdown, proof = apply(
                reply.choices[0].message.content,
                parent_markdown,
                request_id=identity,
                source_sha256=binding["source_sha256"],
                **amendment_options,
            )
            execution_mode = "model_proposed_edits"

        proof = {
            **proof,
            "execution_mode": execution_mode,
            "purpose_scope_sha256": digest(binding),
            "semantic_quality_approved": False,
            "result_before_asset_rebinding_sha256": hashlib.sha256(
                markdown.encode("utf-8")
            ).hexdigest(),
        }
        if strategy == 'line_text_edits_v1' and packet is not None and packet.get('saved_region_reuse_reference'):
            proof['reused_region_reference'] = packet['saved_region_reuse_reference']
        path = self.root / identity / "amendment.json"
        atomic(path, proof)
        self.record(identity, hashlib.sha256(path.read_bytes()).hexdigest())
        return self.save(identity, {**previous, "markdown": markdown}, output)
