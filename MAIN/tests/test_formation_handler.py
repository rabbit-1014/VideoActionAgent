import pytest

from video_action_agent.formation_handler import build_formation_command
from video_action_agent.models import FormationCommand
from video_action_agent.runtime import digest


class Claim:
    def __init__(self, request):
        self.request = request


def request(message="Organize this tutorial"):
    return {
        "schema_version": 1,
        "message": message,
        "source": "BV1gD421u75J",
        "output_kind": "method_list",
        "local_browser_actions": ["subtitle"],
    }


def test_explicit_subtitle_method_list_builds_bound_scoped_command():
    original = request("Organize this tutorial into a practical method list")

    command = build_formation_command(Claim(original))
    parsed = FormationCommand.model_validate(command)

    assert parsed.decision == "accept"
    assert parsed.input_fingerprint == digest(original)
    assert parsed.candidate.user_goal == original["message"]
    assert [item.title for item in parsed.candidate.required_results] == [
        "Method steps",
        "Applicable preconditions",
        "Checkpoints",
        "Subtitle timestamps",
        "Unverified current interface status",
    ]
    assert parsed.candidate.entry_context == "Bilibili BV1gD421u75J, page 1"
    assert parsed.input_contract_review.version == "deterministic-scoped-validation-v1"
    assert parsed.policy_review.version == "deterministic-scoped-validation-v1"
    assert "semantic" in " ".join(parsed.candidate.risks).lower()


@pytest.mark.parametrize(
    "overrides",
    [
        {"output_kind": None},
        {"source": "https://example.com/video/BV1gD421u75J"},
        {"local_browser_actions": ["video"]},
        {"source": "not a Bilibili source"},
    ],
)
def test_missing_explicit_scope_fails_closed(overrides):
    payload = {**request(), **overrides}

    command = FormationCommand.model_validate(build_formation_command(Claim(payload)))

    assert command.decision == "unformable"
    assert command.authority.kind == "policy"
    assert command.input_contract_review.passed
    assert command.policy_review.passed


def test_detected_extra_natural_language_requirement_fails_closed_and_is_preserved():
    original = request("Organize this tutorial and also translate the subtitles")

    command = FormationCommand.model_validate(build_formation_command(Claim(original)))

    assert command.decision == "unformable"
    assert command.candidate.user_goal == original["message"]
    assert "additional requirement" in command.authority.reason.lower()
    assert "also" in command.authority.reason.lower()
    assert command.input_contract_review.version == "deterministic-scoped-validation-v1"
