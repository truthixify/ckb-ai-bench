"""Authenticated LLM readiness for the operator preflight.

The readiness request authenticates because a healthy protected endpoint rejects an anonymous
`GET /models` even when the configured credential is valid.

The credential reaches the request through the process environment and a header, never through a
command-line argument: argv is world-readable through `ps` on a shared host. Nothing
provider-controlled — body, header, redirect target, or transport exception text — reaches the
operator's output, so a readiness line can be pasted into an issue safely.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ckbbench.config import resolve_llm_api_base, resolve_llm_api_key
from ckbbench.run.model_profile import ModelProfileError, safe_api_base

MODELS_PATH = "/models"
# No weaker than the readiness bound this replaced.
TIMEOUT_SECONDS = 5.0

READY = "ready"
AUTH_REJECTED = "auth_rejected"
HTTP_FAILURE = "http_failure"
UNREACHABLE = "unreachable"
UNSAFE_BASE = "unsafe_base"


@dataclass(frozen=True)
class Readiness:
    """One classified readiness observation. Every field is safe to print.

    `endpoint` is the VALIDATED base, never the raw configured value: a configured base can carry
    userinfo or another secret, and the operator line is meant to be pasteable.
    """

    state: str
    detail: str
    status: int | None = None
    endpoint: str | None = None

    @property
    def ready(self) -> bool:
        return self.state == READY

    def line(self) -> str:
        """The operator-facing summary. Fixed text plus a status code, never provider content."""
        body = self.detail if self.status is None else f"{self.detail} (HTTP {self.status})"
        return f"{self.endpoint} {body}" if self.endpoint else body


def models_url(api_base: str) -> str:
    """The one readiness URL for an API root.

    A root base and a `/v1` base both gain exactly `/models`; the shared safe-base rule rejects an
    endpoint-shaped base, so a configured `.../models` cannot become `.../models/models`.
    """
    return f"{safe_api_base(api_base)}{MODELS_PATH}"


def _default_client(timeout: float) -> Any:
    import httpx

    return httpx.Client(
        transport=httpx.HTTPTransport(retries=0), follow_redirects=False, timeout=timeout
    )


def check_llm_readiness(*, api_base: str | None = None, api_key: str | None = None,
                        client: Any | None = None,
                        timeout: float = TIMEOUT_SECONDS) -> Readiness:
    """One authenticated GET. No retry, no redirect, no body read.

    `api_key` defaults to the production resolver so the probe and the model cannot disagree about
    which credential this environment means. Every failure — including client construction and
    cleanup — leaves through the same sanitizing boundary, so no raw exception text, configured
    path, proxy URL, endpoint or credential can escape as a traceback.
    """
    base = api_base if api_base is not None else resolve_llm_api_base()
    try:
        url = models_url(base)
    except ModelProfileError as exc:
        # `exc` names the field and the rule, never the supplied value.
        return Readiness(UNSAFE_BASE, f"configured endpoint is unusable: {exc}")
    safe_base = url[: -len(MODELS_PATH)]

    key = api_key if api_key is not None else resolve_llm_api_key()
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}

    owned = client is None
    http = client
    try:
        # Construction is inside the boundary: a bad SSL_CERT_FILE, proxy setting or missing
        # transport would otherwise escape as a traceback carrying that configuration.
        if http is None:
            http = _default_client(timeout)
        with http.stream("GET", url, headers=headers) as response:
            status = response.status_code
    except Exception as exc:  # noqa: BLE001 - any failure here is still "not usable right now"
        return _transport_failure(exc, safe_base)
    finally:
        if owned and http is not None:
            try:
                http.close()
            except Exception:  # noqa: BLE001 - a cleanup failure must not override the result
                pass

    if 200 <= status < 300:
        return Readiness(READY, "ready", status, safe_base)
    if status in (401, 403):
        return Readiness(AUTH_REJECTED, "authentication rejected; check CKBBENCH_LLM_API_KEY",
                         status, safe_base)
    if 300 <= status < 400:
        # Refused, not followed: a redirect can move a credentialed request to another origin.
        return Readiness(HTTP_FAILURE, "endpoint attempted a redirect", status, safe_base)
    return Readiness(HTTP_FAILURE, "endpoint returned an unusable status", status, safe_base)


def _transport_failure(exc: BaseException, endpoint: str | None = None) -> Readiness:
    """Class name only. A transport exception's text can carry a URL, a body, or a header."""
    return Readiness(UNREACHABLE, f"endpoint unreachable ({type(exc).__name__})", None, endpoint)


def main(argv: list[str] | None = None) -> int:
    """Print one safe readiness line. Exit 0 only when the endpoint is ready.

    This takes NO arguments. Both the endpoint and the credential come from the environment, so
    neither can appear in argv, and a rejected argument is never echoed back: argparse's default
    unknown-argument error would print `--api-key <value>` straight to stderr.
    """
    import sys

    supplied = sys.argv[1:] if argv is None else list(argv)
    if supplied:
        print("REFUSED: this check takes no arguments; its endpoint and credential come from the "
              "environment", file=sys.stderr)
        return 2

    result = check_llm_readiness()
    print(result.line())
    return 0 if result.ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
