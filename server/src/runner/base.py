"""The Runner protocol.

A Runner is "something that can execute a bootbench job for a device". There
is exactly one real implementation (`agent_client.AgentClient`, over HTTP)
and one fake (`local_fake.LocalFakeRunner`, in memory). `server.yaml:
runner_backend` selects.

The protocol is deliberately the agent's HTTP surface, verb for verb, so
the fake cannot drift into being easier to satisfy than the real thing.
It is also direction-agnostic: if lab policy ever forbids inbound
connections to the bench host, a long-poll runner implements the same
methods with the same payloads and nothing above this line changes.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Runner(Protocol):
    def healthz(self) -> dict:
        """Liveness plus capability assertion. See agent_client.assess_health."""

    def latest_build(self, device_id: str) -> dict:
        """Device-free build probe. Touches only the build share."""

    def submit_job(self, job_id, device_id, **request) -> dict:
        """Start a job. Idempotent on job_id: a re-POST of a known id returns
        the existing record rather than launching a second run, which is what
        makes a coordinator-side retry safe."""

    def get_job(self, job_id) -> dict:
        """Current state, plus the parsed --json-status doc once it exists."""

    def tail_log(self, job_id, offset: int = 0) -> dict:
        """{offset, chunk, size, eof}. One byte-offset mechanism end to end:
        agent -> server -> browser."""

    def manifest(self, job_id) -> dict:
        """[{relpath, size, sha256, kind}]. Doubles as the allowlist for
        fetch_artifact, so path traversal is structurally impossible rather
        than filtered."""

    def fetch_artifact(self, job_id, relpath: str) -> bytes:
        """Raw bytes of one manifest entry."""

    def cancel(self, job_id) -> dict:
        """Request cancellation. Honored only at safe points -- never mid-flash."""

    def recover(self, device_id: str) -> dict:
        """flash --recover for a board left in EDL."""


# Terminal job states as reported by the agent. The server maps these onto
# its own run_status enum during ingestion (`done` splits into success vs
# partial depending on boots_recorded).
TERMINAL_STATES = frozenset({"done", "failed", "orphaned"})


def is_terminal(state: str) -> bool:
    return state in TERMINAL_STATES


def job_timeout_seconds(num_boots: int, boot_timeout: int) -> int:
    """How long to wait before calling a job wedged.

    Generous on purpose: a timeout should mean "stuck", not "slow night".
    2*num_boots boots at boot_timeout plus ~4 min of reboot/login/pull
    overhead each, plus 20 min for the flash and 10 for everything else.
    ~1h42m at the defaults (3 boots, 480 s).
    """
    return 2 * num_boots * (boot_timeout + 240) + 1200 + 600
