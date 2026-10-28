"""Strict ASR source envelope; recorded recognition is never verified audio truth."""

import json
import re
from hashlib import sha256

from pydantic import ValidationError

from .local_transcriber import TranscriptionError, validate_result
from .models import DispatchAudioProof
from .source_access import SourceAccessError

ASR_ADAPTER_VERSION = "local-audio-asr-v1"
ASR_PARSE_VERSION = "asr-envelope-v1"
ASR_POLICY = "accepted-local-bilibili-audio-asr-v1"


def source_scope(request, action):
    if action == "audio_transcription":
        return (request.get("local_source_actions") or []) if request.get("source_mode") == action else []
    if request.get("source_mode", "subtitle") != "subtitle":
        return []
    return request.get("local_browser_actions") or []


def material_source_ref(material):
    ref = {"bvid": material["bvid"], "page": material["page"]}
    if material["action"] == "audio_transcription":
        provenance = dict(material["provenance"])
        # Full per-window diagnostics stay in the formal material. The model
        # needs an immutable compact reference, not a large table to echo twice.
        manifest = provenance.pop("chunk_manifest", None)
        if manifest is not None:
            encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":"),
                                 ensure_ascii=True, allow_nan=False).encode()
            provenance["chunk_manifest_sha256"] = sha256(encoded).hexdigest()
        ref.update(kind="audio_transcription", cid=material["provenance"]["cid"],
                   provenance=provenance)
    return ref


def parse_transcribed_reply(ref, raw, expected_proof):
    try:
        data = json.loads(raw)
        if set(data) != {"schema_version", "source_evidence", "transcription"} or data["schema_version"] != 1:
            raise ValueError()
        proof = DispatchAudioProof.model_validate(data["source_evidence"]).model_dump()
        expected = DispatchAudioProof.model_validate(expected_proof).model_dump()
        if proof != expected or (proof["bvid"], proof["page"]) != (ref.bvid, ref.page):
            raise ValueError()
        result = data["transcription"]
        audio_hash = result["audio_sha256"]
        if not re.fullmatch(r"[0-9a-f]{64}", audio_hash):
            raise ValueError()
        validate_result(result, audio_hash, 7200)
        if (result["parameters"].get("chunk_seconds") is not None
                and (not result.get("chunk_manifest") or not result.get("chunk_coverage"))):
            raise ValueError()
        if (not re.fullmatch(r"[0-9a-f]{64}", result["asr_input_sha256"])
                or result["asr_input_encoding"] != "pcm_f32le_16000_mono"
                or result["asr_input_hash_basis"] != "actual_worker_array"
                or abs(result["duration"] - proof["stream_duration_seconds"]) > 2):
            raise ValueError()
        provenance = {key: value for key, value in result.items() if key != "segments"}
        provenance.update(cid=proof["cid"], source_evidence=proof,
                          parse_version=ASR_PARSE_VERSION)
        return {"kind": "audio_transcription", "trust": "untrusted_source_content",
                "segments": result["segments"], "provenance": provenance}
    except (ValueError, TypeError, KeyError, ValidationError, TranscriptionError):
        raise SourceAccessError("invalid_asr_reply") from None
