"""A validated saved-source snapshot for viewing, never a new online read."""

from datetime import UTC, datetime

from .source_candidate_bundle import CandidateBundleError, _render, read_bundle


class SourcePreviewError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def load_source_preview(directory):
    try:
        bundle = read_bundle(directory)
        envelope = bundle["source_envelope"]
        proof, transcription = envelope["source_evidence"], envelope["transcription"]
        markdown, srt = _render(bundle)
        return {
            "state": bundle["state"], "formal_task": False, "completed": False,
            "acquisition": bundle["acquisition"],
            "source_ref": {"kind": "audio_transcription", **{
                key: proof[key] for key in ("bvid", "page", "cid", "part_title")
            }},
            "source_observed_at": proof["observed_at"],
            "loaded_at": datetime.now(UTC).isoformat(),
            "duration": transcription["duration"],
            "timestamp_kind": transcription["timestamp_kind"],
            "segments": transcription["segments"], "markdown": markdown, "srt": srt,
        }
    except (CandidateBundleError, OSError, KeyError, TypeError, ValueError):
        raise SourcePreviewError("source_bundle_unavailable") from None
