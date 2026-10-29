"""Read objective evidence without executing tasks or changing their repository."""

import hashlib
import json
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

MAX_PROBES = 100


def check(task: dict, *, max_probes: int = MAX_PROBES) -> dict:
    if isinstance(max_probes, bool) or not isinstance(max_probes, int):
        raise TypeError("max_probes must be an integer")
    if not 1 <= max_probes <= MAX_PROBES:
        raise ValueError("max_probes must be between 1 and 100")
    evidence = task.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        return {"ok": False, "reason": "no evidence", "results": [], "probe_count": 1}
    if len(evidence) > max_probes:
        return {"ok": False, "reason": "probe budget exhausted", "results": [], "probe_count": 0}
    results = []
    count = 0
    for pointer in evidence:
        count += 1
        try:
            if not isinstance(pointer, str) or not pointer.strip():
                raise ValueError("invalid evidence pointer")
            parts = shlex.split(pointer)
            kind = parts[0]
            cwd = Path(task.get("cwd") or ".")
            result = {"pointer": pointer, "kind": kind}
            match kind:
                case "file" if len(parts) in {2, 3}:
                    path = cwd / parts[1]
                    if not path.is_file():
                        raise ValueError(f"evidence missing: {pointer}")
                    if len(parts) == 3:
                        if not re.fullmatch(r"[0-9a-fA-F]{64}", parts[2]):
                            raise ValueError(f"invalid sha256: {pointer}")
                        with path.open("rb") as stream:
                            digest = hashlib.file_digest(stream, "sha256").hexdigest()
                        if digest != parts[2].lower():
                            raise ValueError(f"sha256 mismatch: {pointer}")
                        result["sha256"] = digest
                case "log" if len(parts) == 3:
                    path = cwd / parts[1]
                    if (
                        not path.is_file()
                        or parts[2] not in path.read_text(encoding="utf-8").splitlines()
                    ):
                        raise ValueError(f"evidence missing: {pointer}")
                    result["line"] = parts[2]
                case "pr" if len(parts) == 2:
                    reference = re.fullmatch(
                        r"([\w.-]+(?:/[\w.-]+)?)#([1-9]\d*)", parts[1], re.ASCII
                    )
                    if not reference:
                        raise ValueError(f"invalid PR reference: {pointer}")
                    gh = shutil.which("gh")
                    if gh is None:
                        raise ValueError("probe error: gh unavailable")
                    repo, number = reference.groups()
                    args = [gh, "pr", "view", number, "--json", "state,body,mergedAt,url"]
                    if "/" in repo:
                        args.extend(["--repo", repo])
                    proc = subprocess.run(
                        args,
                        cwd=cwd,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        timeout=10,
                        check=False,
                    )
                    if proc.returncode:
                        raise ValueError("probe error: gh failed")
                    pr = json.loads(proc.stdout)
                    location = urlsplit(pr["url"])
                    path = location.path.strip("/").split("/")
                    expected = repo.split("/")
                    if (
                        location.scheme != "https"
                        or len(path) != 4
                        or path[2:] != ["pull", number]
                        or [p.lower() for p in path[2 - len(expected) : 2]]
                        != [p.lower() for p in expected]
                    ):
                        raise ValueError("probe error: PR identity mismatch")
                    if pr["state"] not in {"OPEN", "MERGED"}:
                        raise ValueError(f"PR not open or merged: {pointer}")
                    if not re.search(r"^local-ci:\s*\S", pr["body"], re.MULTILINE):
                        raise ValueError(f"local-ci missing: {pointer}")
                    result.update(
                        state=pr["state"], merged=pr["mergedAt"] is not None, url=pr["url"]
                    )
                case _:
                    raise ValueError(f"unsupported evidence: {pointer}")
            results.append(result)
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            IndexError,
            subprocess.TimeoutExpired,
        ) as exc:
            return {"ok": False, "reason": str(exc), "results": results, "probe_count": count}
    return {"ok": True, "reason": "", "results": results, "probe_count": count}


def log_report(result: dict) -> bool:
    return (
        result["ok"]
        and bool(result["results"])
        and all(item["kind"] == "log" for item in result["results"])
    )
