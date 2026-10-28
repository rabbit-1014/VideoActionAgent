"""Read a previously saved same-source visual packet without reacquisition."""
import hashlib
import json
import re
from pathlib import Path
from uuid import UUID

from bridge_contract import BridgeError, digest

_DESCRIPTOR_KEYS = {
    "owner", "source_id", "donor_id", "source_sha256", "packet_sha256", "scope_sha256"
}
_HASH = re.compile(r"^[0-9a-f]{64}$")
_HISTORICAL_ROLE = "unverified_current_frame_model_observation"


class SavedVisuals:
    """Select and load only validated visual packets attached to an owned saved note."""

    def __init__(self, owner, donors, *, lookup=None):
        if (not isinstance(owner, str) or not owner.strip() or not callable(donors)
                or (lookup is not None and not callable(lookup))):
            raise ValueError("owner and donor lookup are required")
        self.owner = owner
        self.donors = donors
        self.lookup = lookup

    def _entries(self, source_id, donor_id):
        if donor_id is not None and self.lookup is not None:
            entry = self.lookup(source_id, donor_id)
            return [] if entry is None else [entry]
        return list(self.donors(source_id))

    @staticmethod
    def _identity(value):
        try:
            normalized = str(UUID(str(value)))
        except (ValueError, TypeError, AttributeError):
            raise BridgeError("saved_visual_identity_invalid") from None
        if normalized != str(value).lower():
            raise BridgeError("saved_visual_identity_invalid")
        return normalized

    @staticmethod
    def _evidence(bundle):
        try:
            evidence = bundle["source_envelope"]["source_evidence"]
            values = {key: evidence[key] for key in ("bvid", "page", "cid")}
        except (TypeError, KeyError):
            raise BridgeError("saved_visual_source_invalid") from None
        if (not isinstance(values["bvid"], str) or not values["bvid"]
                or type(values["page"]) is not int or values["page"] < 1
                or type(values["cid"]) is not int or values["cid"] < 1):
            raise BridgeError("saved_visual_source_invalid")
        return values

    def _one(self, source_id, bundle, donor_id, entry):
        try:
            listed_id, service, pipeline = entry
        except (TypeError, ValueError):
            raise BridgeError("saved_visual_donor_invalid") from None
        listed_id = self._identity(listed_id)
        if donor_id is not None and listed_id != donor_id:
            raise BridgeError("saved_visual_donor_mismatch")
        if getattr(service, "owner", None) != self.owner:
            raise BridgeError("saved_visual_owner_mismatch")
        row = service._row(listed_id)
        if row.get("owner") != self.owner:
            raise BridgeError("saved_visual_owner_mismatch")
        if row.get("master_id") != source_id:
            raise BridgeError("saved_visual_source_mismatch")
        source_sha256 = digest(bundle)
        if row.get("master_sha256") != source_sha256:
            raise BridgeError("saved_visual_source_mismatch")
        if row.get("state") != "available":
            raise BridgeError("saved_visual_donor_unavailable")
        packet_sha256 = row.get("vision_manifest_sha256")
        if not isinstance(packet_sha256, str) or not _HASH.fullmatch(packet_sha256):
            raise BridgeError("saved_visual_packet_unavailable")

        # Reuse native source/result admission and the original visual scope, packet,
        # frame-integrity and current source checks before exposing any saved JSON.
        service.candidate(listed_id)
        pipeline.validate_published(row)
        directory = Path(pipeline.root) / listed_id
        packet_path = directory / "visual-material.json"
        if (directory.is_symlink() or directory.is_junction()
                or packet_path.is_symlink() or packet_path.is_junction()
                or not packet_path.is_file()
                or packet_path.resolve().parent != directory.resolve()):
            raise BridgeError("saved_visual_packet_unavailable")
        raw = packet_path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != packet_sha256:
            raise BridgeError("saved_visual_packet_changed")
        try:
            packet = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise BridgeError("saved_visual_packet_invalid") from None
        if not isinstance(packet, dict):
            raise BridgeError("saved_visual_packet_invalid")
        evidence = self._evidence(bundle)
        if (packet.get("source_sha256") != source_sha256
                or packet.get("scope_sha256") != row.get("scope_sha256", packet.get("scope_sha256"))
                or packet.get("source") != evidence
                or packet.get("role") != _HISTORICAL_ROLE
                or packet.get("quality_approved") is not False
                or not isinstance(packet.get("frames"), list)
                or not packet["frames"]):
            raise BridgeError("saved_visual_packet_mismatch")
        scope_sha256 = packet.get("scope_sha256")
        if not isinstance(scope_sha256, str) or not _HASH.fullmatch(scope_sha256):
            raise BridgeError("saved_visual_scope_invalid")
        descriptor = {
            "owner": self.owner,
            "source_id": source_id,
            "donor_id": listed_id,
            "source_sha256": source_sha256,
            "packet_sha256": packet_sha256,
            "scope_sha256": scope_sha256,
        }
        return descriptor, packet

    def describe(self, source_id, bundle, *, donor_id=None):
        source_id = self._identity(source_id)
        exact_donor = self._identity(donor_id) if donor_id is not None else None
        entries = self._entries(source_id, exact_donor)
        if not entries:
            if exact_donor is not None and self.lookup is not None:
                raise BridgeError("saved_visual_donor_not_found")
            return None
        if exact_donor is not None:
            for entry in entries:
                try:
                    identity = self._identity(entry[0])
                except (TypeError, IndexError, BridgeError):
                    raise BridgeError("saved_visual_donor_invalid") from None
                if identity == exact_donor:
                    return self._one(source_id, bundle, exact_donor, entry)[0]
            raise BridgeError("saved_visual_donor_not_found")
        for entry in entries:
            descriptor, _ = self._one(source_id, bundle, None, entry)
            if descriptor:
                return descriptor
        return None

    def load(self, descriptor, source_id, bundle):
        source_id = self._identity(source_id)
        if (not isinstance(descriptor, dict) or set(descriptor) != _DESCRIPTOR_KEYS
                or descriptor.get("owner") != self.owner
                or descriptor.get("source_id") != source_id):
            raise BridgeError("saved_visual_descriptor_mismatch")
        donor_id = self._identity(descriptor.get("donor_id"))
        for entry in self._entries(source_id, donor_id):
            if self._identity(entry[0]) == donor_id:
                current, packet = self._one(source_id, bundle, donor_id, entry)
                if current != descriptor:
                    raise BridgeError("saved_visual_descriptor_changed")
                return packet
        raise BridgeError("saved_visual_donor_not_found")
