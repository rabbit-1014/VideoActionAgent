"""One optional, immutable documentary reference to one exact saved candidate."""

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from uuid import UUID


class ReferenceUnavailable(Exception):
    def __init__(self, code, status):
        super().__init__(code)
        self.code, self.status = code, status


@dataclass(frozen=True)
class SavedReference:
    candidate_id: UUID
    record_fingerprint: str
    version: int
    path: Path
    payload_sha256: str
    title: str
    source_url: str
    source_document_updated: str

    def read(self, candidate_id, candidate):
        if str(candidate_id) != str(self.candidate_id):
            raise ReferenceUnavailable("reference_not_associated", 404)
        revision = candidate.get("revision") or {}
        if (type(revision.get("version")) is not int or revision["version"] != self.version
                or revision.get("record_fingerprint") != self.record_fingerprint):
            raise ReferenceUnavailable("reference_version_unavailable", 409)
        try:
            if any(part.is_symlink() or part.is_junction()
                   for part in (self.path, *self.path.parents)):
                raise OSError("Reference must be a regular local file")
            with self.path.open("rb") as stream:
                payload = stream.read(65537)
            if len(payload) > 65536 or sha256(payload).hexdigest() != self.payload_sha256:
                raise ValueError("Reference bytes changed")
            payload.decode("utf-8")
        except (OSError, ValueError) as exc:
            raise ReferenceUnavailable("reference_unavailable", 503) from exc
        return payload

    def metadata(self, candidate_id, candidate):
        if str(candidate_id) != str(self.candidate_id):
            return None
        state = "available"
        try:
            self.read(candidate_id, candidate)
        except ReferenceUnavailable:
            state = "unavailable"
        return self.describe(state)

    def describe(self, state="available"):
        return {"state": state, "candidate_id": str(self.candidate_id),
                "record_fingerprint": self.record_fingerprint, "version": self.version,
                "payload_sha256": self.payload_sha256, "title": self.title,
                "source_role": "independent_external_reference_not_video_statement",
                "source_url": self.source_url,
                "source_document_updated": self.source_document_updated}


SUBTITLE_REFERENCE = SavedReference(
    candidate_id=UUID("f3d00792-32ac-409b-9936-154f14521343"),
    record_fingerprint="5f8f1a23782d467885521f61180dc5358a5011eb902d0a100c863179197c8fa1",
    version=7,
    path=Path(__file__).resolve().parents[2] / "SUBTITLE-METHOD-REFERENCE-2026-10-07.md",
    payload_sha256="e3bc5b4d43c90001fac31578981b3a4db5443c8840aaf6b56b96c8c945e354b7",
    title="选择剪映识别字幕前，先确认能否上传音视频",
    source_url="https://lv.ulikecam.com/clause/policy/pr",
    source_document_updated="2022-08-10",
)
