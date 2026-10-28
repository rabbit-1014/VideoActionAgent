"""Model-facing projection; authoritative source and validation stay intact."""

import json

# These fields describe execution/integrity, not what the speaker said. The
# caller keeps the full material and binds its original digest to the request.
_LOCAL_DIAGNOSTICS = {
    "chunk_manifest", "software", "asr_input_sha256",
    "asr_input_encoding", "asr_input_hash_basis", "runtime_metrics",
}


def model_material(material):
    """Keep all content, scope, warnings and unknown fields; never summarize."""
    source = json.loads(json.dumps(material, ensure_ascii=False, allow_nan=False))
    transcript = source["transcription"]
    for field in _LOCAL_DIAGNOSTICS:
        transcript.pop(field, None)
    transcript["segments"] = [
        {**segment, "evidence_index": index}
        for index, segment in enumerate(transcript["segments"], 1)
    ]
    return source
