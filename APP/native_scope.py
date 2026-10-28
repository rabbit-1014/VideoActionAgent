"""Bind native note jobs and every current-material read to an immutable purpose."""

from bridge_contract import BridgeError, digest
from native_notes import CaptureLedger, NativeNotes
from video_action_agent.notes_service import NotesServiceError

_NO_PROFILE = object()
_NATIVE_STYLES = frozenset({"tutorial", "detailed", "minimal", "task_oriented"})


class ScopedCaptureLedger(CaptureLedger):
    """Capture ledger that records and rechecks the Root-owned purpose binding."""

    def __init__(self, root, sources, owner, scope_value):
        super().__init__(root, sources, owner)
        self.scope_value = scope_value

    def _scope_binding(self, identity):
        try:
            binding = self.scope_value(identity)
            if not isinstance(binding, dict):
                raise TypeError("scope binding must be an object")
            return binding
        except Exception:  # noqa: BLE001 -- do not expose callback details
            raise BridgeError("purpose_scope_unavailable") from None

    def _binding_digest(self, binding):
        try:
            return digest(binding)
        except Exception:  # noqa: BLE001 -- do not expose binding details
            raise BridgeError("purpose_scope_unavailable") from None

    def _scope_digest(self, identity):
        return self._binding_digest(self._scope_binding(identity))

    def assert_scope(self, row):
        stored = row.get("purpose_scope_sha256")
        if not isinstance(stored, str) or not stored:
            raise BridgeError("purpose_scope_missing")
        identity = row.get("request_id")
        if not isinstance(identity, str) or not identity:
            raise BridgeError("purpose_scope_missing")
        if stored != self._scope_digest(identity):
            raise BridgeError("purpose_scope_changed")

    def admit(self, *args, **kwargs):
        with self.lock:
            identity = args[0] if args else kwargs.get("identity")
            binding = self._scope_binding(identity)
            local_only = binding.get("allow_local_edits") is True
            if local_only != (kwargs.get("allow_local_edits") is True) or (local_only and (
                    binding.get("allow_model_text") is not False or kwargs.get("allow_model_text") is not False
                    or binding.get("revision_strategy") != "literal_text_edits_v1")):
                raise BridgeError("local_edits_not_admitted")
            scope_hash = self._binding_digest(binding)
            profile = binding.get("native_profile", _NO_PROFILE)
            if profile is _NO_PROFILE:
                bound_style = "tutorial"
            elif isinstance(profile, dict) and profile.get("style") in _NATIVE_STYLES:
                bound_style = profile["style"]
            else:
                raise BridgeError("native_profile_invalid")
            if kwargs.get("style", bound_style) != bound_style:
                raise BridgeError("native_style_scope_conflict")
            kwargs["style"] = bound_style
            row, fresh = super().admit(*args, **kwargs)
            if fresh:
                row = self.update(row["request_id"], purpose_scope_sha256=scope_hash)
                if binding.get("allow_model_text") is True and kwargs.get("allow_model_text") is True:
                    row = self.update(row["request_id"], model_text_authorized=True)
            else:
                self.assert_scope(row)
            return row, fresh

    def material(self, identity):
        with self.lock:
            material = super().material(identity)
            row = super().get(identity)
            self.assert_scope(row)
            return material

    def dispatch(self, identity):
        with self.lock:
            row = self.get(identity)
            self.assert_scope(row)
            if self._scope_binding(identity).get("revision_strategy") == "literal_text_edits_v1":
                raise BridgeError("local_edits_model_dispatch_forbidden")
            return super().dispatch(identity)

    def dispatch_review(self, identity):
        """One new-policy source review after a received draft, never a retry."""
        with self.lock:
            row = self.get(identity)
            self.assert_scope(row)
            binding = self._scope_binding(identity)
            if row["state"] == "cancelled":
                raise BridgeError("cancelled")
            if (binding.get("goal_policy", {}).get("version") not in (3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16)
                    or "revision_request" in binding or "revision_strategy" in binding
                    or binding.get("allow_model_text") is not True
                    or row.get("model_text_authorized") is not True
                    or row.get("local_edits_authorized") is True
                    or row["state"] != "reply_received" or row["model_calls"] != 1
                    or row.get("source_review_dispatched")):
                raise BridgeError("source_review_not_admitted")
            self.material(identity)
            return self.update(identity, state="model_dispatched", model_calls=2,
                               source_review_dispatched=True, source_review_stage="source_review_v1")

    def local_edits_ready(self, identity):
        """Record one authorized local application without a model dispatch."""
        with self.lock:
            row = self.get(identity)
            self.assert_scope(row)
            if self._scope_binding(identity).get("revision_strategy") != "literal_text_edits_v1":
                raise BridgeError("local_edits_not_admitted")
            if self._scope_binding(identity).get("allow_local_edits") is True and (
                    row.get("model_text_authorized") is not False or row.get("local_edits_authorized") is not True):
                raise BridgeError("local_edits_not_admitted")
            if row["state"] != "admitted" or row["model_calls"] != 0:
                raise BridgeError("local_edits_not_admitted")
            self.material(identity)
            return self.update(identity,state="reply_received",execution_mode="local_exact_edits",
                local_edits_prepared=True)


class ScopedNativeNotes(NativeNotes):
    """Existing NativeNotes behavior with a mandatory frozen-purpose fence."""

    def __init__(self, owner, output_root, sources, generate, transport, *,
                 scope_value, result_guard=None):
        if not callable(scope_value):
            raise TypeError("scope_value callable required")
        super().__init__(owner, output_root, sources, generate, transport,
                         result_guard=result_guard)
        self.ledger = ScopedCaptureLedger(
            self.ledger.root, self._capture, self.owner, scope_value
        )

    def _row(self, identity):
        try:
            row = self.ledger.get(identity)
            if row.get("owner") != self.owner:
                raise BridgeError("owned_request_not_found")
            self.ledger.assert_scope(row)
            return row
        except (BridgeError, OSError, ValueError):
            raise NotesServiceError("notes_not_found") from None
