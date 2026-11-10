"""The Project_Registry, read and written through the proxy's own ``/projects`` API.

The registry (create, list, delete, membership) lives in the proxy (``ui/server/projects/registry.ts``). The agent
calls the SAME routes the browser calls, with the turn's forwarded User_Token, so there is one implementation: the
proxy's Cognito check fixes the Actor and the registry's name, owner and member rules decide everything. Nothing here
chooses an Actor or skips a check.

The address is ``UI_PUBLIC_ORIGIN`` (``https://<CloudFront domain>``): an SSM parameter under
``RUNTIME_ARN_PARAM_PATH`` read lazily by :mod:`runtime_arns` (the ALB is origin-locked, so CloudFront is the only
way in). It is never a model-supplied URL, it must be https, and redirects are not followed.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

ORIGIN_NAME = "UI_PUBLIC_ORIGIN"
_TIMEOUT = (3.05, 20)  # connect, read (seconds)


def _origin() -> str:
    import runtime_arns  # noqa: PLC0415 - lazy: stdlib + boto3 only

    origin = (runtime_arns.get(ORIGIN_NAME) or "").strip().rstrip("/")
    parts = urlsplit(origin)
    if parts.scheme != "https" or not parts.hostname or parts.path or parts.query:
        return ""
    return origin


def _default_token() -> str:
    from .assets import _default_token_source  # noqa: PLC0415

    return _default_token_source()


def _unavailable(message: str) -> dict:
    return {"error": "unavailable", "message": message}


class RegistryClient:
    """``list`` / ``create`` against ``/projects``. Every method returns a dict and never raises."""

    def __init__(self, *, origin: Optional[Callable[[], str]] = None, token: Optional[Callable[[], str]] = None,
                 session: Any = None):
        self._origin = origin or _origin
        self._token = token or _default_token
        self._session = session

    def _http(self) -> Any:
        if self._session is None:
            import requests  # noqa: PLC0415

            self._session = requests.Session()
        return self._session

    def _call(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        origin = self._origin()
        if not origin:
            return _unavailable("Projects are not reachable from the agent here (no UI_PUBLIC_ORIGIN).")
        token = self._token()
        if not token:
            return {"error": "unauthenticated", "message": "No signed-in user on this turn; ask the user to retry."}
        try:
            resp = self._http().request(
                method, f"{origin}{path}", json=body, timeout=_TIMEOUT, allow_redirects=False,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
        except Exception as exc:  # noqa: BLE001 - the model gets a plain refusal, not a stack
            logger.warning("project registry %s %s failed: %s", method, path, type(exc).__name__)
            return _unavailable(f"The project service is unavailable ({type(exc).__name__}); try again.")
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001 - CloudFront's SPA fallback answers HTML for an unlisted path
            data = None
        if not isinstance(data, dict):
            logger.warning("project registry %s %s: status %s, not JSON", method, path, resp.status_code)
            return _unavailable("The project service gave an unexpected reply; try again.")
        if 200 <= resp.status_code < 300:
            return data
        err = data.get("error")
        if isinstance(err, dict):
            return {"error": str(err.get("code") or "error"), "message": str(err.get("message") or "")}
        return {"error": str(err or f"http_{resp.status_code}"), "message": ""}

    def list(self) -> dict:
        """``{projects: [{projectId, name, role, personal, indexing?, activeAssetCount?}]}`` or ``{error}``."""
        out = self._call("GET", "/projects")
        if "error" in out:
            return out
        rows = out.get("projects")
        return {"projects": [p for p in rows if isinstance(p, dict)] if isinstance(rows, list) else []}

    def create(self, name: str, *, description: str = "", indexing: bool = True) -> dict:
        """``{project: {projectId, name, ...}}`` or ``{error}``. The registry validates the name."""
        body: dict = {"name": name, "indexing": bool(indexing)}
        if description:
            body["description"] = description
        return self._call("POST", "/projects", body)
