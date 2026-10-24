"""A standard-library client for hcd's /api/dispatch routes, used by hc and by other programs."""

import http.client
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .state import endpoint_path, state_dir


class Unavailable(Exception):
    """hcd is not running or dispatch is off; nothing was sent."""


class LostContact(Exception):
    """The connection broke after the request was written; it may have been applied."""


class Refused(Exception):
    """hcd answered with an error (bad request, unknown target, too late to cancel)."""


def request(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    timeout: float = 10.0,
    root: Path | None = None,
) -> dict[str, Any]:
    endpoint_file = endpoint_path(root or state_dir())
    try:
        endpoint = json.loads(endpoint_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Unavailable(f"no endpoint file {endpoint_file} ({exc.__class__.__name__})") from exc
    req = urllib.request.Request(
        f"http://127.0.0.1:{endpoint['port']}/api/dispatch/{path}",
        method=method,
        data=None if body is None else json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {endpoint['token']}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = json.loads(exc.read() or b"{}").get("detail", exc.reason)
        if exc.code == 503:
            raise Unavailable(detail) from exc
        raise Refused(detail) from exc
    except urllib.error.URLError as exc:
        # Refused before anything was sent: the server is not there.
        raise Unavailable(f"cannot reach hcd on port {endpoint['port']}: {exc.reason}") from exc
    except (TimeoutError, ConnectionError, http.client.HTTPException) as exc:
        raise LostContact(f"hcd stopped responding: {exc!r}") from exc
