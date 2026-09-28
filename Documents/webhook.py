#!/usr/bin/env python3
"""Fixed-scope Gitea push webhook receiver for DataPipeline on host 209.

Production defaults:

* endpoint: ``http://172.20.0.1:4014/gitea/datapipeline``;
* repository/ref: ``yangtianyu/datapipeline`` and ``refs/heads/master``;
* secret file: ``/home/xinzhi/DataPipeline-deploy/webhook/secret``;
* fixed job: ``/usr/local/libexec/datapipeline-webhook-job {after}``.

The request can select only a validated commit SHA. The host-side command and
working directory come from the service configuration, never from the payload.
The fixed job receives the SHA through ``CI_HEAD_SHA``; an exact ``{after}``
argument placeholder is also supported for the reviewed DataPipeline job.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import threading
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Mapping
from urllib.parse import urlsplit


LOGGER = logging.getLogger("datapipeline.webhook")
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
ZERO_SHA = "0" * 40
MAX_PAYLOAD_BYTES = 1024 * 1024

DEFAULT_BIND = "172.20.0.1"
DEFAULT_PORT = 4014
DEFAULT_PATH = "/gitea/datapipeline"
DEFAULT_REPOSITORY = "yangtianyu/datapipeline"
DEFAULT_ALLOWED_REFS = "refs/heads/master"
DEFAULT_SECRET_FILE = Path("/home/xinzhi/DataPipeline-deploy/webhook/secret")
DEFAULT_COMMAND = "/usr/local/libexec/datapipeline-webhook-job {after}"
DEFAULT_WORKDIR = Path("/usr/local/libexec")
DEFAULT_LOG_FILE = Path(
    "/home/xinzhi/DataPipeline-deploy/webhook/logs/webhook-receiver.log"
)


class WebhookRejection(ValueError):
    """A request that must be rejected with a specific HTTP status."""

    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class WebhookConfig:
    bind: str
    port: int
    path: str
    secret: str
    allowed_refs: frozenset[str]
    repository: str
    command: tuple[str, ...]
    workdir: Path
    log_file: Path

    @classmethod
    def from_environment(cls) -> "WebhookConfig":
        secret = os.environ.get("WEBHOOK_SECRET", "").strip()
        if not secret:
            secret_path = Path(
                os.environ.get("WEBHOOK_SECRET_FILE", str(DEFAULT_SECRET_FILE))
            ).expanduser()
            if not secret_path.is_absolute():
                raise RuntimeError("WEBHOOK_SECRET_FILE must be an absolute path")
            if secret_path.is_symlink() or not secret_path.is_file():
                raise RuntimeError(
                    "WEBHOOK_SECRET or a regular WEBHOOK_SECRET_FILE is required"
                )
            secret_stat = secret_path.stat()
            if secret_stat.st_uid != os.getuid():
                raise RuntimeError("WEBHOOK_SECRET_FILE must be owned by the service user")
            if stat.S_IMODE(secret_stat.st_mode) & 0o077:
                raise RuntimeError("WEBHOOK_SECRET_FILE permissions must be 0600 or stricter")
            secret = secret_path.read_text(encoding="utf-8").strip()
            if not secret:
                raise RuntimeError("WEBHOOK_SECRET_FILE must not be empty")

        raw_refs = os.environ.get("WEBHOOK_ALLOWED_REFS", DEFAULT_ALLOWED_REFS)
        allowed_refs = frozenset(value.strip() for value in raw_refs.split(",") if value.strip())
        if not allowed_refs:
            raise RuntimeError("WEBHOOK_ALLOWED_REFS must contain at least one ref")
        if any(not value.startswith("refs/heads/") for value in allowed_refs):
            raise RuntimeError("WEBHOOK_ALLOWED_REFS may contain only refs/heads/* values")
        if allowed_refs != frozenset({DEFAULT_ALLOWED_REFS}):
            raise RuntimeError(
                f"WEBHOOK_ALLOWED_REFS must be exactly {DEFAULT_ALLOWED_REFS}"
            )

        raw_command = os.environ.get("WEBHOOK_COMMAND", DEFAULT_COMMAND)
        command = tuple(shlex.split(raw_command))
        if not command:
            raise RuntimeError("WEBHOOK_COMMAND must not be empty")
        if len(command) > 2 or any(value != "{after}" for value in command[1:]):
            raise RuntimeError(
                "WEBHOOK_COMMAND must directly name one audited executable and may "
                "optionally end with {after}"
            )
        executable = Path(command[0])
        if not executable.is_absolute():
            raise RuntimeError("WEBHOOK_COMMAND must start with an absolute executable path")
        if executable.is_symlink() or not executable.is_file():
            raise RuntimeError("WEBHOOK_COMMAND executable must be a regular non-symlink file")
        executable_stat = executable.stat()
        executable_mode = stat.S_IMODE(executable_stat.st_mode)
        if executable_stat.st_uid != 0:
            raise RuntimeError("WEBHOOK_COMMAND executable must be owned by root")
        if executable_mode & 0o022:
            raise RuntimeError("WEBHOOK_COMMAND executable must not be group/world writable")
        if not executable_mode & 0o111:
            raise RuntimeError("WEBHOOK_COMMAND executable must be executable")

        try:
            port = int(os.environ.get("WEBHOOK_PORT", str(DEFAULT_PORT)))
        except ValueError as exc:
            raise RuntimeError("WEBHOOK_PORT must be an integer") from exc
        if not 1 <= port <= 65535:
            raise RuntimeError("WEBHOOK_PORT must be between 1 and 65535")

        path = os.environ.get("WEBHOOK_PATH", DEFAULT_PATH).strip()
        if not path.startswith("/") or path == "/":
            raise RuntimeError("WEBHOOK_PATH must be a non-root path starting with '/'")

        repository = os.environ.get("WEBHOOK_REPOSITORY", DEFAULT_REPOSITORY).strip()
        if repository.casefold() != DEFAULT_REPOSITORY:
            raise RuntimeError(
                f"WEBHOOK_REPOSITORY must be exactly {DEFAULT_REPOSITORY}"
            )

        workdir = Path(os.environ.get("WEBHOOK_WORKDIR", str(DEFAULT_WORKDIR))).resolve()
        if not workdir.is_dir():
            raise RuntimeError("WEBHOOK_WORKDIR must be an existing directory")

        bind = os.environ.get("WEBHOOK_BIND", DEFAULT_BIND).strip()
        if not bind:
            raise RuntimeError("WEBHOOK_BIND must not be empty")

        return cls(
            bind=bind,
            port=port,
            path=path,
            secret=secret,
            allowed_refs=allowed_refs,
            repository=repository.casefold(),
            command=command,
            workdir=workdir,
            log_file=Path(
                os.environ.get("WEBHOOK_LOG_FILE", str(DEFAULT_LOG_FILE))
            ).resolve(),
        )


@dataclass(frozen=True)
class PushEvent:
    before: str
    after: str
    ref: str
    repository: str
    delivery_id: str


def _header(headers: Mapping[str, str], name: str) -> str:
    wanted = name.casefold()
    for key, value in headers.items():
        if key.casefold() == wanted:
            return value.strip()
    return ""


def verify_signature(body: bytes, signature: str, secret: str) -> bool:
    """Verify Gitea's lowercase, unprefixed HMAC-SHA256 signature."""

    signature = signature.strip().lower()
    if signature.startswith("sha256="):
        signature = signature.removeprefix("sha256=")
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return bool(signature) and hmac.compare_digest(expected, signature)


def parse_push_event(
    body: bytes,
    headers: Mapping[str, str],
    config: WebhookConfig,
) -> PushEvent:
    if _header(headers, "X-Gitea-Event").casefold() != "push":
        raise WebhookRejection(HTTPStatus.BAD_REQUEST, "only push events are supported")

    if not verify_signature(body, _header(headers, "X-Gitea-Signature"), config.secret):
        raise WebhookRejection(HTTPStatus.UNAUTHORIZED, "invalid webhook signature")

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebhookRejection(HTTPStatus.BAD_REQUEST, "payload must be valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise WebhookRejection(HTTPStatus.BAD_REQUEST, "payload must be a JSON object")

    ref = payload.get("ref")
    before = payload.get("before")
    after = payload.get("after")
    repository_data = payload.get("repository")
    repository = repository_data.get("full_name") if isinstance(repository_data, dict) else None
    if not all(isinstance(value, str) for value in (ref, before, after, repository)):
        raise WebhookRejection(HTTPStatus.BAD_REQUEST, "push payload is missing required fields")
    if ref not in config.allowed_refs:
        raise WebhookRejection(HTTPStatus.FORBIDDEN, "ref is not allowed")
    if repository.casefold() != config.repository:
        raise WebhookRejection(HTTPStatus.FORBIDDEN, "repository is not allowed")
    if not SHA_PATTERN.fullmatch(after):
        raise WebhookRejection(HTTPStatus.BAD_REQUEST, "after must be a 40-character commit SHA")
    if before != "0" * 40 and not SHA_PATTERN.fullmatch(before):
        raise WebhookRejection(HTTPStatus.BAD_REQUEST, "before must be a commit SHA")

    return PushEvent(
        before=before,
        after=after,
        ref=ref,
        repository=repository,
        delivery_id=_header(headers, "X-Gitea-Delivery") or "unknown",
    )


class WebhookServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], config: WebhookConfig) -> None:
        super().__init__(address, WebhookHandler)
        self.config = config

    def start_command(self, event: PushEvent) -> None:
        self.config.log_file.parent.mkdir(parents=True, exist_ok=True)
        child_environment = os.environ.copy()
        child_environment.pop("WEBHOOK_SECRET", None)
        child_environment.pop("WEBHOOK_SECRET_FILE", None)
        child_environment.update(
            {
                "CI_BASE_SHA": event.before,
                "CI_HEAD_SHA": event.after,
                "CI_EVENT_DELIVERY_ID": event.delivery_id,
                "GITEA_REPOSITORY": event.repository,
                "GITEA_REF": event.ref,
            }
        )
        command = [event.after if value == "{after}" else value for value in self.config.command]
        log_fd = os.open(
            self.config.log_file,
            os.O_APPEND
            | os.O_CREAT
            | os.O_WRONLY
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        log_handle = os.fdopen(log_fd, "ab")
        try:
            process = subprocess.Popen(
                command,
                cwd=self.config.workdir,
                env=child_environment,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=os.name != "nt",
                close_fds=True,
            )
        except BaseException:
            log_handle.close()
            raise
        log_handle.close()
        threading.Thread(
            target=self._reap_command,
            args=(process, event),
            daemon=True,
            name=f"datapipeline-ci-{event.after[:12]}",
        ).start()
        LOGGER.info(
            "started fixed CI command: delivery=%s repository=%s ref=%s revision=%s",
            event.delivery_id,
            event.repository,
            event.ref,
            event.after,
        )

    @staticmethod
    def _reap_command(process: subprocess.Popen[bytes], event: PushEvent) -> None:
        return_code = process.wait()
        LOGGER.info(
            "fixed CI command finished: delivery=%s revision=%s return_code=%s",
            event.delivery_id,
            event.after,
            return_code,
        )


class WebhookHandler(BaseHTTPRequestHandler):
    server: WebhookServer

    def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        config = self.server.config
        if urlsplit(self.path).path != config.path:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return

        raw_length = self.headers.get("Content-Length")
        try:
            content_length = int(raw_length) if raw_length is not None else -1
        except ValueError:
            content_length = -1
        if content_length < 0:
            self._send_json(HTTPStatus.LENGTH_REQUIRED, {"error": "content length is required"})
            return
        if content_length > MAX_PAYLOAD_BYTES:
            self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "payload is too large"})
            return

        body = self.rfile.read(content_length)
        if len(body) != content_length:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "incomplete payload"})
            return

        try:
            event = parse_push_event(body, self.headers, config)
        except WebhookRejection as exc:
            self._send_json(exc.status, {"error": str(exc)})
            return

        if event.after == ZERO_SHA:
            self._send_json(
                HTTPStatus.ACCEPTED,
                {"status": "ignored", "reason": "branch deletion"},
            )
            return

        try:
            self.server.start_command(event)
        except OSError:
            LOGGER.exception("could not start fixed CI command")
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "could not start fixed CI command"},
            )
            return

        self._send_json(
            HTTPStatus.ACCEPTED,
            {"status": "accepted", "delivery_id": event.delivery_id, "revision": event.after},
        )

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        if urlsplit(self.path).path == "/healthz":
            self._send_json(
                HTTPStatus.OK,
                {"status": "ok", "service": "datapipeline-webhook"},
            )
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def log_message(self, format: str, *args: object) -> None:
        LOGGER.info("%s - %s", self.address_string(), format % args)

    def _send_json(self, status: HTTPStatus, payload: dict[str, str]) -> None:
        encoded = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def main() -> int:
    try:
        config = WebhookConfig.from_environment()
    except RuntimeError as exc:
        print(f"configuration error: {exc}")
        return 2

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
    server = WebhookServer((config.bind, config.port), config)
    LOGGER.info("listening on %s:%s%s", config.bind, config.port, config.path)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("stopping")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
