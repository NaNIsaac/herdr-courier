"""Reconcile evidence through courier's API; this module has no journal writer."""

import math
import threading

from . import probes
from .client import LostContact, Refused, Unavailable, request


def run(*, max_probes: int = 100, after: str | None = None) -> list[dict]:
    from . import cli

    if isinstance(max_probes, bool) or not isinstance(max_probes, int):
        raise TypeError("max_probes must be an integer")
    if not 2 <= max_probes <= probes.MAX_PROBES:
        raise ValueError("max_probes must be between 2 and 100, including close rechecks")
    outcomes = []
    spent = 0
    tasks = [
        t for t in request("GET", "await/list")["tasks"] if t["state"] in {"OPEN", "VERIFYING"}
    ]
    tasks.sort(key=lambda t: (t["state"] != "VERIFYING", t["sent"], t["key"]))
    if after is not None:
        for index, task in enumerate(tasks):
            if task["key"] == after:
                tasks = tasks[index + 1 :] + tasks[: index + 1]
                break
    for task in tasks:
        count = max(1, len(task.get("evidence") or []))
        if spent + 2 * count > max_probes:
            continue
        result = probes.check(task, max_probes=count)
        spent += result["probe_count"]
        outcome = {"key": task["key"], **result, "closed": False}
        if result["ok"] and (task["state"] == "VERIFYING" or probes.log_report(result)):
            spent += count
            outcome["probe_count"] += count
            try:
                outcome["close_exit"] = cli.main(
                    [
                        "await",
                        "close",
                        task["key"],
                        "--by",
                        "probe",
                        "--evidence",
                        "objective check",
                        "--max-probes",
                        str(count),
                        "--quiet",
                    ]
                )
                outcome["closed"] = outcome["close_exit"] == 0
            except SystemExit as exc:
                outcome["close_exit"] = exc.code
        else:
            reason = "artifact found, no report" if result["ok"] else result["reason"]
            if task["state"] == "VERIFYING" or result["ok"]:
                request("POST", "await/flag", {"key": task["key"], "reason": reason})
        outcomes.append(outcome)
    return outcomes


def watch(*, interval: float = 600, max_probes: int = 100, stop=None):
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("interval must be a positive finite number of seconds")
    stop = stop if stop is not None else threading.Event()
    after = None
    while not stop.is_set():
        try:
            outcomes = run(max_probes=max_probes, after=after)
        except (Unavailable, LostContact, Refused) as exc:
            outcomes = [{"error": str(exc), "probe_count": 0}]
        if outcomes and "key" in outcomes[-1]:
            after = outcomes[-1]["key"]
        yield outcomes
        if stop.wait(interval):
            break
