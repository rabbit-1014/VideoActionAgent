import json
import re
import subprocess
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from .source_access import OPENCLI_VERSION

_MAX_OUTPUT_BYTES = 64 * 1024
_PROBE_TIMEOUT_SECONDS = 10


class SessionProbeError(RuntimeError):
    """A local browser session could not be verified from current CLI returns."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class LocalOpenCliSessionProbe:
    """Explicitly observe local OpenCLI connectivity and Bilibili auth state.

    The probe never starts a browser, opens a tab, or retries. It only returns a
    dispatch proof when both bounded OpenCLI checks return the expected values.
    """

    def __init__(
        self,
        node: str,
        entry: Path,
        *,
        runner: Callable = subprocess.run,
        clock: Callable[[], str] | None = None,
    ):
        package = entry.parents[2] / "package.json"
        try:
            version = json.loads(package.read_text(encoding="utf-8"))["version"]
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise SessionProbeError("adapter_unavailable") from exc
        if version != OPENCLI_VERSION:
            raise SessionProbeError("adapter_version_changed")
        self.node = node
        self.entry = entry
        self.runner = runner
        self.clock = clock or (lambda: datetime.now(UTC).isoformat())

    def observe(self) -> dict[str, object]:
        doctor = self._run([self.node, str(self.entry), "doctor"], json_reply=False)
        bridge = re.search(r"(?m)^\[OK\] Extension: connected \(v([0-9.]+)\)$", doctor)
        if bridge is None or not re.search(
            r"(?m)^\[OK\] Connectivity: connected in [0-9.]+s$", doctor
        ):
            raise SessionProbeError("connection_unverified")
        auth = self._run(
            [self.node, str(self.entry), "auth", "status", "--site", "bilibili", "-f", "json"],
            json_reply=True,
        )
        if (
            not isinstance(auth, list) or len(auth) != 1
            or not isinstance(auth[0], dict) or auth[0].get("site") != "bilibili"
            or auth[0].get("status") != "logged_in" or auth[0].get("logged_in") is not True
            or auth[0].get("error") not in (None, "")
        ):
            raise SessionProbeError("authentication_unverified")

        observed_at = self.clock()
        # This random opaque identifier correlates this observation without
        # exposing browser profile names, account data, or a platform session ID.
        session_ref = f"opencli-{uuid.uuid4().hex}"
        return {
            "schema_version": 1,
            "observed_at": observed_at,
            "adapter_version": OPENCLI_VERSION,
            "browser_session_ref": session_ref,
            "observed_connection_state": "verified",
            "observation_command": "opencli doctor; opencli auth status --site bilibili -f json",
            "observation_return": (
                f"doctor bridge=connected v{bridge.group(1)} "
                "connectivity=connected; bilibili auth logged_in=true"
            ),
        }

    def _run(self, args: list[str], *, json_reply: bool):
        try:
            result = self.runner(
                args,
                capture_output=True,
                timeout=_PROBE_TIMEOUT_SECONDS,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SessionProbeError("probe_timeout") from exc
        except OSError as exc:
            raise SessionProbeError("adapter_unavailable") from exc
        if result.returncode != 0:
            raise SessionProbeError("probe_failed")
        output = result.stdout
        if isinstance(output, str):
            output = output.encode("utf-8")
        if not isinstance(output, bytes) or len(output) > _MAX_OUTPUT_BYTES:
            raise SessionProbeError("invalid_probe_reply")
        try:
            decoded = output.decode("utf-8")
            payload = json.loads(decoded) if json_reply else decoded
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise SessionProbeError("invalid_probe_reply") from exc
        return payload
