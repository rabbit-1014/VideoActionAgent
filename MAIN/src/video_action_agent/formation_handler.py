"""Small deterministic Goal formation path for explicitly scoped method lists.

This handler validates only a narrow input shape and declared local policy scope.
It does not interpret arbitrary prose or claim semantic fidelity to it.
"""

import re

from pydantic import ValidationError

from .models import FormationCommand, GoalCandidate, TaskInput
from .runtime import digest
from .source_access import SourceAccessError, VideoRef

REVIEW_VERSION = "deterministic-scoped-validation-v1"

# These are explicit extra tasks that the method-list result contract does not cover.
# Unknown prose remains intact in user_goal; this guard is deliberately conservative.
_UNSUPPORTED_REQUIREMENT = re.compile(
    r"\b(?:also|additionally|translate|compare|download|publish|email|export|"
    r"recommend|fact[- ]?check|verify)\b|另外|此外|顺便|同时|还要|并且",
    re.IGNORECASE,
)


def _candidate(message, source_ref=None, *, risks=()):
    return GoalCandidate(
        user_goal=message if isinstance(message, str) and message else "Unusable task input",
        entry_context=(
            f"Bilibili {source_ref.bvid}, page {source_ref.page}"
            if source_ref is not None
            else None
        ),
        required_results=[],
        optional_results=[],
        constraints=[],
        risks=list(risks),
        unknowns=[],
    )


def _reviews(input_fingerprint, candidate_fingerprint, passed):
    review = {
        "version": REVIEW_VERSION,
        "passed": passed,
        "input_fingerprint": input_fingerprint,
        "candidate_fingerprint": candidate_fingerprint,
    }
    return {"input_contract_review": review, "policy_review": review}


def _unformable(request, reason, input_fingerprint):
    message = request.get("message") if isinstance(request, dict) else None
    candidate = _candidate(message)
    candidate_payload = candidate.model_dump()
    candidate_fingerprint = digest(candidate_payload)
    return FormationCommand(
        decision="unformable",
        input_fingerprint=input_fingerprint,
        candidate_fingerprint=candidate_fingerprint,
        candidate=candidate,
        # A passed review means the deterministic check correctly classified the
        # request as out of scope; it does not mean the task itself was accepted.
        **_reviews(input_fingerprint, candidate_fingerprint, True),
        authority={"kind": "policy", "reason": reason},
    ).model_dump()


def build_formation_command(claim):
    """Build a versioned, source-free Goal command from a live claim's input only.

    The caller remains responsible for committing this command via
    ``Runtime.form_decision``, which owns lease, task-owner, and revision fences.
    """
    request = claim.request
    input_fingerprint = digest(request)
    try:
        parsed = TaskInput.model_validate(request)
    except (ValidationError, TypeError, ValueError):
        return _unformable(
            request, "Task input does not match the supported contract.", input_fingerprint
        )

    if parsed.output_kind != "method_list":
        return _unformable(
            request,
            "This deterministic path requires the explicit output_kind=method_list.",
            input_fingerprint,
        )

    if parsed.source_mode == "subtitle" and "subtitle" not in parsed.local_browser_actions:
        return _unformable(
            request,
            "This deterministic path requires the explicitly declared local subtitle scope.",
            input_fingerprint,
        )

    try:
        source_ref = VideoRef.parse(parsed.source)
    except SourceAccessError:
        return _unformable(
            request,
            "This deterministic path requires a supported Bilibili video source.",
            input_fingerprint,
        )

    original_message = request["message"]
    extra = _UNSUPPORTED_REQUIREMENT.search(original_message)
    if extra:
        return _unformable(
            request,
            "The message includes an additional requirement this method-list path does not cover: "
            f"{extra.group(0)}. Submit a separately scoped request.",
            input_fingerprint,
        )

    candidate = GoalCandidate(
        user_goal=original_message,
        entry_context=f"Bilibili {source_ref.bvid}, page {source_ref.page}",
        required_results=[
            {
                "title": "Method steps",
                "acceptance": "Present the method as ordered, actionable steps grounded in the source subtitles.",
                "basis": "user_utterance",
                "evidence": "output_kind=method_list",
            },
            {
                "title": "Applicable preconditions",
                "acceptance": "State preconditions when the subtitles support them; mark unsupported details unknown.",
                "basis": "user_utterance",
                "evidence": "output_kind=method_list",
            },
            {
                "title": "Checkpoints",
                "acceptance": "Include observable checkpoints supported by the subtitles; do not invent them.",
                "basis": "user_utterance",
                "evidence": "output_kind=method_list",
            },
            {
                "title": "Subtitle timestamps",
                "acceptance": "Attach relevant subtitle time points to steps and checkpoints.",
                "basis": "user_utterance",
                "evidence": f"output_kind=method_list; source={source_ref.bvid}, page={source_ref.page}",
            },
            {
                "title": "Unverified current interface status",
                "acceptance": "Do not claim the current application interface was observed or verified.",
                "basis": "user_utterance",
                "evidence": "Only local subtitle access is explicitly authorized",
            },
        ],
        constraints=[
            "Use only source subtitles for video-specific claims; treat missing evidence as unknown."
        ],
        risks=[
            (
                "Deterministic scoped validation checks the explicit output type, supported source shape, "
                "and declared local subtitle policy only; it does not prove semantic fidelity to arbitrary "
                "natural-language requirements."
            )
        ],
        unknowns=[
            {
                "item": "Whether subtitles contain enough evidence for every step or precondition",
                "resolution": "Determine from the later source material; preserve gaps as unknowns.",
            }
        ],
    )
    if parsed.source_mode == "audio_transcription":
        candidate.constraints = [
            "Use only the local ASR transcript; recognition may be wrong and does not prove original audio accuracy.",
            "Time references are VAD segment ranges, not word alignment or platform subtitles.",
            "No remote audio upload; only the exact declared BVID/page audio may be read.",
        ]
        for result in candidate.required_results:
            result.title = result.title.replace("Subtitle timestamps", "VAD segment time ranges")
            result.acceptance = result.acceptance.replace("source subtitles", "local ASR transcript").replace("subtitles", "ASR transcript").replace("subtitle time points", "VAD segment time ranges")
            result.evidence = result.evidence.replace("Only local subtitle access is explicitly authorized", "Only exact local audio transcription access is explicitly authorized")
        candidate.risks.append("ASR text and segment boundaries are unverified recognition candidates.")
        candidate.unknowns[0].item = "Whether recognized speech provides enough evidence; audio accuracy is unverified"
    candidate_payload = candidate.model_dump()
    candidate_fingerprint = digest(candidate_payload)
    return FormationCommand(
        decision="accept",
        input_fingerprint=input_fingerprint,
        candidate_fingerprint=candidate_fingerprint,
        candidate=candidate,
        **_reviews(input_fingerprint, candidate_fingerprint, True),
    ).model_dump()
