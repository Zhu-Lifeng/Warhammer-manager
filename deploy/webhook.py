"""GitHub push webhook receiver.

Run as a small Flask service (via gunicorn under systemd) bound to a
non-privileged port on the VPS, e.g. 127.0.0.1:9001 behind nginx.

Required env vars:
  WH40K_WEBHOOK_SECRET   shared secret configured in the GitHub webhook UI
  WH40K_DEPLOY_SCRIPT    absolute path to deploy.sh (defaults to ./deploy.sh)
  WH40K_DEPLOY_BRANCH    branch that should trigger deploys (defaults to main)

The receiver only kicks off the deploy script — it does NOT touch the main
app's data directory. All version control happens against the repo checkout;
user data lives in WH40K_DATA_DIR which the deploy script never touches.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import subprocess
import threading
from pathlib import Path

from flask import Flask, abort, request

SECRET = os.environ.get("WH40K_WEBHOOK_SECRET", "").encode()
DEPLOY_SCRIPT = Path(
    os.environ.get("WH40K_DEPLOY_SCRIPT")
    or (Path(__file__).resolve().parent / "deploy.sh")
).resolve()
DEPLOY_BRANCH = os.environ.get("WH40K_DEPLOY_BRANCH", "main")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("wh40k-webhook")

app = Flask(__name__)


def _verify(signature: str | None, body: bytes) -> bool:
    if not SECRET:
        log.error("WH40K_WEBHOOK_SECRET is not set — refusing all requests")
        return False
    if not signature or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(SECRET, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _run_deploy() -> None:
    log.info("running %s", DEPLOY_SCRIPT)
    try:
        result = subprocess.run(
            ["/bin/bash", str(DEPLOY_SCRIPT)],
            capture_output=True, text=True, timeout=600,
        )
        log.info("deploy rc=%s\nstdout:\n%s\nstderr:\n%s",
                 result.returncode, result.stdout, result.stderr)
    except Exception:
        log.exception("deploy script crashed")


@app.route("/healthz")
def healthz():
    return {"ok": True, "branch": DEPLOY_BRANCH, "script": str(DEPLOY_SCRIPT)}


@app.route("/github-webhook", methods=["POST"])
def github_webhook():
    raw = request.get_data()
    if not _verify(request.headers.get("X-Hub-Signature-256"), raw):
        log.warning("rejected webhook: bad signature from %s", request.remote_addr)
        abort(401)

    event = request.headers.get("X-GitHub-Event", "")
    if event == "ping":
        return {"pong": True}
    if event != "push":
        return {"ignored": event}

    payload = request.get_json(silent=True) or {}
    ref = payload.get("ref", "")
    expected_ref = f"refs/heads/{DEPLOY_BRANCH}"
    if ref != expected_ref:
        log.info("ignoring push to %s (want %s)", ref, expected_ref)
        return {"ignored_ref": ref}

    # Fire-and-forget so GitHub gets a fast 200 and doesn't retry.
    threading.Thread(target=_run_deploy, daemon=True).start()
    return {"queued": True}


if __name__ == "__main__":
    # Dev-only entry point. Production uses gunicorn (see warhammer-webhook.service).
    app.run(host="127.0.0.1", port=9001)
