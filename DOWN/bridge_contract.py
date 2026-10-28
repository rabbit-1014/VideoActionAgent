"""Small source/intent boundary around an unchanged native note workflow."""
import hashlib
import json
import math
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4


class BridgeError(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     allow_nan=False).encode()).hexdigest()


def atomic(path, value):
    temporary = path.with_name(path.name + ".tmp-" + uuid4().hex)
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                    allow_nan=False), encoding="utf-8")
    os.replace(temporary, path)


class BridgeLedger:
    def __init__(self, root, registry):
        self.root, self.registry = Path(root), registry
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

    def _path(self, identity):
        try:
            identity = str(UUID(str(identity)))
        except (ValueError, TypeError):
            raise BridgeError("invalid_request_id") from None
        return self.root / (identity + ".json")

    def _master(self, key):
        spec = self.registry.get(key)
        if spec is None:
            raise BridgeError("master_unavailable")
        raw = Path(spec["path"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != spec["sha256"]:
            raise BridgeError("master_changed")
        material = json.loads(raw)["source_envelope"]
        segments = material["transcription"]["segments"]
        if not segments or any(not isinstance(s["text"], str) or not s["text"].strip()
            or not all(type(s[k]) in (int, float) and math.isfinite(s[k]) for k in ("start", "end"))
            or s["start"] < 0 or s["end"] < s["start"] for s in segments):
            raise BridgeError("invalid_master")
        return material, spec["sha256"]

    def admit(self, identity, master_id, user_request, *, allow_model_text=False,
              allow_local_edits=False, style="tutorial"):
        if type(allow_local_edits) is not bool or (allow_local_edits and allow_model_text is not False):
            raise BridgeError("invalid_local_edit_permission")
        if not allow_local_edits and allow_model_text is not True:
            raise BridgeError("model_text_permission_required")
        if not isinstance(user_request, str) or not user_request.strip() or len(user_request) > 4000:
            raise BridgeError("invalid_user_request")
        if style not in ("tutorial", "detailed", "minimal", "task_oriented"):
            raise BridgeError("invalid_style")
        path = self._path(identity)
        with self.lock:
            material, source_hash = self._master(master_id)
            signature_value = {"master_id": master_id, "master_sha256": source_hash,
                "user_request": user_request, "style": style, "model": "glm-5.3"}
            if allow_local_edits:
                signature_value["execution_mode"] = "local_exact_edits"
            signature = digest(signature_value)
            if path.exists():
                old = json.loads(path.read_text(encoding="utf-8"))
                if old["signature"] != signature:
                    raise BridgeError("request_conflict")
                return old, False
            row = {"request_id": path.stem, "master_id": master_id,
                "master_sha256": source_hash, "user_request": user_request,
                "style": style, "signature": signature, "state": "admitted",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "source_url": "https://www.bilibili.com/video/" + material["source_evidence"]["bvid"]
                              + "?p=" + str(material["source_evidence"]["page"]),
                "completed": False, "quality_approved": False, "formal_task": False,
                "model_calls": 0, "source_calls": 0, "asr_calls": 0}
            if allow_local_edits:
                row.update(execution_mode="local_exact_edits", model_text_authorized=False,
                           local_edits_authorized=True)
            atomic(path, row)
            return row, True

    def get(self, identity):
        with self.lock:
            path = self._path(identity)
            if not path.exists():
                raise BridgeError("request_not_found")
            return json.loads(path.read_text(encoding="utf-8"))

    def update(self, identity, **fields):
        with self.lock:
            row = self.get(identity)
            if row["state"] == "cancelled":
                fields.pop("state", None)
                fields.pop("native_result_sha256", None)
                fields.pop("markdown_sha256", None)
            row.update(fields)
            atomic(self._path(identity), row)
            return row

    def dispatch(self, identity):
        with self.lock:
            row = self.get(identity)
            if row.get("local_edits_authorized") is True or row.get("model_text_authorized") is False:
                raise BridgeError("local_edits_model_dispatch_forbidden")
            if row["state"] == "cancelled":
                raise BridgeError("cancelled")
            if row["state"] != "admitted" or row["model_calls"] != 0:
                raise BridgeError("model_call_not_admitted")
            self.material(identity)
            return self.update(identity, state="model_dispatched", model_calls=1)

    def cancel(self, identity):
        with self.lock:
            row = self.get(identity)
            if row["state"] == "available":
                raise BridgeError("already_saved")
            return self.update(identity, state="cancelled")

    def publish(self, identity, **fields):
        with self.lock:
            row = self.get(identity)
            if row["state"] == "cancelled":
                raise BridgeError("cancelled")
            if row["state"] != "reply_received":
                raise BridgeError("native_result_not_admitted")
            self.material(identity)
            return self.update(identity, state="available", **fields)

    def material(self, identity):
        row = self.get(identity)
        material, source_hash = self._master(row["master_id"])
        if source_hash != row["master_sha256"]:
            raise BridgeError("master_changed")
        return material
