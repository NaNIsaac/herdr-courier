"""Configured question providers: bounded JSON over stdin/stdout, without a shell."""

import json
import math
import os
import subprocess
import tempfile
import threading
import uuid

from .config import QuestionProvider

MAX_INPUT = 128 * 1024
MAX_OUTPUT = 1024 * 1024
MAX_STDERR = 64 * 1024


class ProviderError(Exception):
    pass


def reject_constant(value: str):
    raise ValueError(f"not a JSON constant: {value}")


def invoke(provider: QuestionProvider, operation: str, body: dict) -> dict:
    encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
    if len(encoded) > MAX_INPUT:
        raise ProviderError("provider request exceeds 128 KiB")
    buffers = [bytearray(), bytearray()]
    overflow = threading.Event()
    try:
        with tempfile.TemporaryFile() as stdin:
            stdin.write(encoded)
            stdin.seek(0)
            process = subprocess.Popen(
                [*(os.path.expandvars(arg) for arg in provider.command), operation],
                stdin=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
            )

            def drain(stream, buffer, limit):
                try:
                    while chunk := stream.read1(8192):
                        if len(buffer) + len(chunk) > limit:
                            overflow.set()
                            try:
                                process.kill()
                            except OSError:
                                pass
                            return
                        buffer.extend(chunk)
                finally:
                    stream.close()

            threads = [
                threading.Thread(target=drain, args=(stream, buffer, limit), daemon=True)
                for stream, buffer, limit in (
                    (process.stdout, buffers[0], MAX_OUTPUT),
                    (process.stderr, buffers[1], MAX_STDERR),
                )
            ]
            for thread in threads:
                thread.start()
            try:
                process.wait(timeout=provider.timeout)
            except subprocess.TimeoutExpired as exc:
                process.kill()
                process.wait()
                raise ProviderError(
                    "provider timed out; get/reconcile before retrying an answer"
                ) from exc
            finally:
                for thread in threads:
                    thread.join(timeout=1)
            if any(thread.is_alive() for thread in threads):
                raise ProviderError("provider output did not close")
    except OSError as exc:
        raise ProviderError(f"cannot run provider: {exc}") from exc
    if overflow.is_set():
        raise ProviderError("provider output exceeds size limit")
    if process.returncode:
        detail = buffers[1].decode("utf-8", errors="replace").strip()[:1000]
        raise ProviderError(f"provider exit {process.returncode}: {detail}")
    try:
        result = json.loads(buffers[0].decode("utf-8"), parse_constant=reject_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ProviderError("provider returned invalid UTF-8 JSON") from exc
    if not isinstance(result, dict):
        raise ProviderError("provider response must be an object")
    return result


def item(value, principal: str, expected: str | None = None) -> dict:
    if not isinstance(value, dict):
        raise ProviderError("provider item must be an object")
    for key in ("id", "from", "to", "text", "state"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise ProviderError(f"provider item needs {key}")
    if len(value["id"]) > 2048 or any(ord(c) < 32 for c in value["id"]):
        raise ProviderError("invalid provider item id")
    created = value.get("created")
    if (
        isinstance(created, bool)
        or not isinstance(created, int | float)
        or not math.isfinite(created)
    ):
        raise ProviderError("provider item needs a finite created timestamp")
    if value["to"] != principal or expected is not None and value["id"] != expected:
        raise ProviderError("provider returned another recipient or item")
    return value


def listing(provider: QuestionProvider, principal: str) -> list[dict]:
    values = invoke(provider, "list", {"principal": principal}).get("items")
    if not isinstance(values, list):
        raise ProviderError("provider list needs items")
    result = [item(value, principal) for value in values]
    if len({value["id"] for value in result}) != len(result):
        raise ProviderError("provider returned duplicate item ids")
    return result


def get(provider: QuestionProvider, principal: str, key: str) -> dict:
    return item(invoke(provider, "get", {"principal": principal, "id": key}), principal, key)


def answer(provider: QuestionProvider, principal: str, key: str, text: str) -> dict:
    current = get(provider, principal, key)
    if current["state"] != "open":
        raise ProviderError(f"question is {current['state']}, not open")
    revision = current.get("revision")
    if not isinstance(revision, str) or not revision:
        raise ProviderError("provider get needs a revision before answering")
    latest = get(provider, principal, key)
    if latest.get("revision") != revision or latest["state"] != "open":
        raise ProviderError("question changed before answering; read it again")
    result = invoke(
        provider,
        "answer",
        {
            "principal": principal,
            "id": key,
            "text": text,
            "request_id": uuid.uuid4().hex,
            "revision": revision,
        },
    )
    if any(
        not isinstance(result.get(field), str) or not result[field]
        for field in ("state", "receipt", "notification")
    ):
        raise ProviderError(
            "provider answer needs state, receipt and notification; reconcile before retrying"
        )
    return result
