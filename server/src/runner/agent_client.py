"""HTTP client for the Windows bench agent.

Deliberately thin. Retries only on connection-level errors, never on an
HTTP status: re-POSTing a job after a 500 could start a second flash, and
`POST /jobs` is idempotent on job_id precisely so the *caller* can decide
to retry.

Uses `requests` when available and falls back to urllib, so `agent-ping`
works on a box where dependencies are not installed yet.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    import requests
    HAVE_REQUESTS = True
except ImportError:
    HAVE_REQUESTS = False

CONNECT_TIMEOUT = 5
READ_TIMEOUT = 30


class AgentError(Exception):
    """Any failure talking to an agent."""

    def __init__(self, message, status=None, payload=None):
        super().__init__(message)
        self.status = status
        self.payload = payload or {}


class AgentUnreachable(AgentError):
    """Connection-level failure. Distinct from an HTTP error because the
    scheduler treats 'agent is down' and 'agent said no' very differently:
    the first retries next tick, the second is a real answer."""


class AgentClient:
    def __init__(self, base_url: str, token: str | None,
                 connect_timeout: int = CONNECT_TIMEOUT,
                 read_timeout: int = READ_TIMEOUT, retries: int = 2):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.retries = retries
        self._session = requests.Session() if HAVE_REQUESTS else None

    # -- plumbing -------------------------------------------------------
    def _headers(self) -> dict:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _request(self, method: str, path: str, body=None, raw=False,
                 read_timeout=None):
        url = f"{self.base_url}{path}"
        timeout = read_timeout or self.read_timeout
        last_error = None

        for attempt in range(1, self.retries + 1):
            try:
                return self._once(method, url, body, raw, timeout)
            except AgentUnreachable as e:
                # Only connection failures are retried. An HTTP status is an
                # answer, and retrying a job POST on a 5xx could start a
                # second flash.
                last_error = e
                if attempt < self.retries:
                    time.sleep(min(2 ** attempt, 8))
        raise last_error

    def _once(self, method, url, body, raw, timeout):
        data = json.dumps(body).encode() if body is not None else None
        headers = self._headers()
        if data:
            headers["Content-Type"] = "application/json"

        if self._session is not None:
            try:
                resp = self._session.request(
                    method, url, data=data, headers=headers,
                    timeout=(self.connect_timeout, timeout),
                )
            except requests.exceptions.RequestException as e:
                raise AgentUnreachable(f"{url}: {e}") from e
            content, status = resp.content, resp.status_code
        else:
            req = urllib.request.Request(url, data=data, headers=headers,
                                         method=method)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    content, status = resp.read(), resp.status
            except urllib.error.HTTPError as e:
                content, status = e.read(), e.code
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                raise AgentUnreachable(f"{url}: {e}") from e

        if raw:
            if status >= 400:
                raise AgentError(f"{url}: HTTP {status}", status=status)
            return content

        try:
            payload = json.loads(content) if content else {}
        except json.JSONDecodeError:
            snippet = content[:400].decode(errors="replace")
            raise AgentError(f"{url}: non-JSON response: {snippet}", status=status)

        if status >= 400:
            raise AgentError(
                f"{url}: HTTP {status}: {payload.get('error', payload)}",
                status=status, payload=payload,
            )
        return payload

    # -- protocol -------------------------------------------------------
    def healthz(self) -> dict:
        return self._request("GET", "/healthz", read_timeout=20)

    def latest_build(self, device_id: str) -> dict:
        query = urllib.parse.urlencode({"device_id": device_id})
        # Shells a PowerShell directory listing over SMB on the far side.
        return self._request("GET", f"/latest-build?{query}", read_timeout=200)

    def submit_job(self, job_id, device_id, **request) -> dict:
        body = {"job_id": str(job_id), "device_id": device_id, **request}
        return self._request("POST", "/jobs", body=body)

    def get_job(self, job_id) -> dict:
        return self._request("GET", f"/jobs/{job_id}")

    def list_jobs(self) -> dict:
        return self._request("GET", "/jobs")

    def tail_log(self, job_id, offset: int = 0) -> dict:
        return self._request("GET", f"/jobs/{job_id}/log?offset={int(offset)}")

    def manifest(self, job_id) -> dict:
        return self._request("GET", f"/jobs/{job_id}/artifacts")

    def fetch_artifact(self, job_id, relpath: str) -> bytes:
        quoted = urllib.parse.quote(relpath)
        return self._request(
            "GET", f"/jobs/{job_id}/artifacts/{quoted}", raw=True, read_timeout=300
        )

    def cancel(self, job_id) -> dict:
        return self._request("POST", f"/jobs/{job_id}/cancel", body={})

    def recover(self, device_id: str) -> dict:
        return self._request(
            "POST", "/recover", body={"device_id": device_id}, read_timeout=620
        )


def client_for(device, token=None) -> AgentClient:
    from ..utils.credential_manager import get_agent_token

    return AgentClient(device.agent_url, token or get_agent_token(device.agent_url))


def assess_health(health: dict) -> tuple:
    """Turn a /healthz payload into (ok, reasons).

    This is the scheduler's step-5 gate: verifying the bench host is capable
    *before* committing to a run, so "the agent died" or "the TAC is in
    session 0" is a clean skip-with-reason at 01:00 rather than a failed
    flash discovered 40 minutes in.
    """
    reasons = []
    if health.get("session_interactive") is False:
        reasons.append(
            f"agent is in session {health.get('session_id')} with no interactive "
            "window station; the TAC COM server will not work (is it running as "
            "a Windows Service?)"
        )
    count = health.get("tac_device_count")
    if count is None:
        reasons.append(
            f"TAC probe failed: {health.get('tac_error') or 'unknown error'}"
        )
    elif count == 0:
        reasons.append("no Alpaca TAC device enumerated; check the USB connection")
    if health.get("pcat_present") is False:
        reasons.append("PCAT.exe not found at the configured path")
    free = health.get("artifact_root_free_gb")
    if free is not None and free < 5:
        reasons.append(f"only {free} GB free under artifact_root")
    return (not reasons), reasons
