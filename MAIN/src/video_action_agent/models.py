from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


class TaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    schema_version: Literal[1] = 1
    message: str = Field(min_length=1, max_length=16000)
    source: str | None = Field(default=None, max_length=4096)
    output_kind: Literal["method_list"] | None = None
    source_mode: Literal["subtitle", "audio_transcription"] = "subtitle"
    local_source_actions: list[Literal["audio_transcription"]] = Field(
        default_factory=list, max_length=1
    )
    local_browser_actions: list[Literal["video", "subtitle"]] = Field(
        default_factory=list, max_length=2
    )

    @model_validator(mode="after")
    def _local_browser_scope(self):
        if len(set(self.local_browser_actions)) != len(self.local_browser_actions):
            raise ValueError("Local browser actions must be distinct")
        if self.local_browser_actions or self.local_source_actions:
            from .source_access import SourceAccessError, VideoRef

            try:
                VideoRef.parse(self.source)
            except SourceAccessError as exc:
                raise ValueError("Local browser actions require a supported video source") from exc
        if self.source_mode == "audio_transcription":
            if self.local_source_actions != ["audio_transcription"]:
                raise ValueError("Audio transcription requires independent explicit local source access")
        elif self.local_source_actions:
            raise ValueError("Audio access requires the explicit audio transcription mode")
        if (self.output_kind == "method_list" and self.source_mode == "subtitle"
                and "subtitle" not in self.local_browser_actions):
            raise ValueError("A method list from video requires declared subtitle access")
        return self


class RevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    request: TaskInput


class ArtifactAcceptanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact_id: UUID
    expected_revision: int = Field(ge=1)


class DispatchSessionProof(BaseModel):
    """Observable evidence that a browser session is connectable right now.

    Every field must come from an actual return the caller observed. This model
    only checks shape and self-consistency: passing it is never proof by itself,
    and a caller that cannot fill it truthfully must not receive a send right.
    ``connection_state='unverified'`` or an ``auto_allow`` permission record is
    explicitly not accepted as evidence.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    schema_version: Literal[1] = 1
    observed_at: str = Field(min_length=1, max_length=64)
    adapter_version: str = Field(min_length=1, max_length=128)
    browser_session_ref: str = Field(min_length=8, max_length=512)
    observed_connection_state: Literal["verified"]
    observation_command: str = Field(min_length=1, max_length=512)
    observation_return: str = Field(min_length=1, max_length=4000)

    @model_validator(mode="after")
    def _return_must_not_claim_unverified(self):
        lowered = self.observation_return.lower()
        if "unverified" in lowered or "auto_allow" in lowered:
            raise ValueError(
                "Session proof cannot be built from an unverified state or an auto_allow record"
            )
        return self


class DispatchAudioProof(BaseModel):
    """Observed anonymous exact-part metadata, never browser login evidence."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)
    bvid: str = Field(pattern=r"^BV[0-9A-Za-z]{10}$")
    page: int = Field(gt=0, strict=True)
    cid: int = Field(gt=0, strict=True)
    part_title: str = Field(min_length=1, max_length=2000)
    collection_title: str = Field(max_length=4000)
    duration_seconds: float = Field(gt=0, le=7200)
    stream_duration_seconds: float = Field(gt=0, le=7200)
    observed_at: str = Field(min_length=1, max_length=64)
    access_mode: Literal["anonymous_public_client"]
    identity_evidence: Literal["view.pages + exact page_index/cid request + CDN path CID"]
    browser_session_ref: None

    @model_validator(mode="after")
    def _duration_identity(self):
        if abs(self.duration_seconds - self.stream_duration_seconds) > 2:
            raise ValueError("Exact part and audio durations disagree")
        return self


class SourceMaterialSegment(BaseModel):
    """One time-pointed segment of a controlled source read.

    The stored shape mirrors what ``source_access.parse_reply`` returns: the
    index is assigned on write so a caller can never disagree with the parser.
    ASR segments remain recognition candidates; this shape proves no accuracy.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)
    index: int | None = Field(default=None, ge=0)
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    text: str = Field(min_length=1, max_length=8000)

    @model_validator(mode="after")
    def _ordered_time_points(self):
        if self.end < self.start:
            raise ValueError("Segment end must not precede its start")
        return self


class SourceSettlementInput(BaseModel):
    """Formal adjudication input for one source operation result.

    ``raw_reply`` is the only content input: segments are taken from the reply by
    the existing parser, never supplied alongside it.
    """

    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    operation_id: Annotated[str, StringConstraints(strip_whitespace=True)] = Field(
        min_length=1, max_length=64
    )
    action: Literal["video", "subtitle", "audio_transcription"]
    bvid: Annotated[str, StringConstraints(strip_whitespace=True)] = Field(
        min_length=1, max_length=64
    )
    page: int = Field(ge=1)
    # Verbatim reply bytes: the stored hash must be the hash of what actually came
    # back, so this one field is never normalized (no whitespace stripping).
    raw_reply: str = Field(min_length=1, max_length=2_000_000)
    adapter_version: Annotated[str, StringConstraints(strip_whitespace=True)] = Field(
        min_length=1, max_length=128
    )
    parse_version: Annotated[str, StringConstraints(strip_whitespace=True)] = Field(
        min_length=1, max_length=128
    )


class SourceUnknownInput(BaseModel):
    """Basis for an unknown outcome that must not be read as 'no subtitles'."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    schema_version: Literal[1] = 1
    operation_id: str = Field(min_length=1, max_length=64)
    code: Literal[
        "adapter_timeout", "adapter_failed", "dispatch_possible_identity_conflict",
        "crash_after_dispatch",
    ]
    evidence: str = Field(min_length=1, max_length=8000)
    remaining_legal_paths: list[str] = Field(default_factory=list, max_length=32)
    waiting_condition: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def _paths_are_named(self):
        if any(not item.strip() for item in self.remaining_legal_paths):
            raise ValueError("remaining_legal_paths items must not be blank")
        if self.remaining_legal_paths and not (self.waiting_condition or "").strip():
            raise ValueError(
                "A remaining legal path needs a real waiting condition, not a silent active Goal"
            )
        return self


class CancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)


class GoalResult(BaseModel):
    """One observable result; its basis is marked, never silently assumed."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=2000)
    acceptance: str = Field(min_length=1, max_length=4000)
    basis: Literal["user_utterance", "retractable_inference"]
    evidence: str = Field(min_length=1, max_length=4000)


class GoalUnknown(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    item: str = Field(min_length=1, max_length=2000)
    resolution: str = Field(min_length=1, max_length=2000)


class GoalCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    user_goal: str = Field(min_length=1, max_length=16000)
    entry_context: str | None = Field(default=None, max_length=8000)
    required_results: list[GoalResult] = Field(default_factory=list, max_length=32)
    optional_results: list[GoalResult] = Field(default_factory=list, max_length=32)
    constraints: list[str] = Field(default_factory=list, max_length=32)
    risks: list[str] = Field(default_factory=list, max_length=32)
    unknowns: list[GoalUnknown] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def _no_blank_notes(self):
        for name in ("constraints", "risks"):
            if any(not item.strip() for item in getattr(self, name)):
                raise ValueError(f"{name} items must not be blank")
        return self


class FormationReview(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    version: str = Field(min_length=1, max_length=128)
    passed: bool
    input_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"[0-9a-f]{64}")
    candidate_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"[0-9a-f]{64}")


class FormationBlocker(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    question: str = Field(min_length=1, max_length=4000)
    waiting_condition: str = Field(min_length=1, max_length=4000)
    responsible_party: str = Field(min_length=1, max_length=200)
    advanceable: bool
    exit_path: str = Field(min_length=1, max_length=4000)


class UnformableReason(BaseModel):
    # Source read failures and model timeouts are not product/policy authority.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    kind: Literal["product_capability", "policy"]
    reason: str = Field(min_length=1, max_length=4000)


class FormationCommand(BaseModel):
    """Versioned internal input to the formation controller, never user-editable."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    schema_version: Literal[1] = 1
    decision: Literal["accept", "needs_user", "unformable"]
    input_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"[0-9a-f]{64}")
    candidate_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"[0-9a-f]{64}")
    candidate: GoalCandidate
    input_contract_review: FormationReview
    policy_review: FormationReview
    blocker: FormationBlocker | None = None
    authority: UnformableReason | None = None

    @model_validator(mode="after")
    def _exit_fields_match_decision(self):
        if self.decision == "accept":
            if not self.candidate.required_results:
                raise ValueError("accept requires a non-empty required result set")
            if self.blocker is not None or self.authority is not None:
                raise ValueError("accept carries neither blocker nor authority reason")
        elif self.decision == "needs_user":
            if self.blocker is None or self.authority is not None:
                raise ValueError("needs_user requires exactly one goal-formation blocker")
        else:
            if self.authority is None or self.blocker is not None:
                raise ValueError("unformable requires a product capability or policy reason")
        return self
