#!/usr/bin/env python3
import json
import os
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from flask import Flask, render_template, request, redirect, url_for, jsonify

BASE_DIR = Path(__file__).resolve().parent
REPO_DIR = BASE_DIR.parent
CAMERA_FILE = BASE_DIR / "cameras.txt"
STATE_DIR = BASE_DIR / "state"
STATE_FILE = STATE_DIR / "last_run.json"

UPDATE_SCRIPT = BASE_DIR / "update_all.sh"
SERVICE_NAME = os.environ.get("WILDLIFE_SERVICE", "rpi-cam-server")
SSH_IDENTITY = os.environ.get("WILDLIFE_SSH_KEY", "")  # e.g. /home/julianflowers/.ssh/id_ed25519_hub

# Dashboard bind (defaults: listen on LAN; set to 127.0.0.1 if you only want local)
BIND_HOST = os.environ.get("WILDLIFE_BIND", "0.0.0.0")
BIND_PORT = int(os.environ.get("WILDLIFE_PORT", "5050"))

app = Flask(__name__)

@dataclass
class Camera:
    name: str
    ssh: str
    preview_base: Optional[str] = None  # e.g. http://192.168.68.73:8000

def _load_cameras() -> List[Camera]:
    cams: List[Camera] = []
    if not CAMERA_FILE.exists():
        return cams

    for line in CAMERA_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        # Allow:
        #  - "name, user@host, http://host:port"
        #  - "user@host"
        parts = [p.strip() for p in line.split(",")]

        if len(parts) == 1:
            ssh = parts[0]
            name = ssh.split("@")[-1]
            cams.append(Camera(name=name, ssh=ssh, preview_base=None))
        else:
            name = parts[0]
            ssh = parts[1]
            preview = parts[2] if len(parts) >= 3 and parts[2] else None
            cams.append(Camera(name=name, ssh=ssh, preview_base=preview))
    return cams

def _run(cmd: List[str], timeout: int = 120) -> dict:
    """Run a command and capture stdout/stderr."""
    started = time.time()
    try:
        p = subprocess.run(
            cmd,
            cwd=str(REPO_DIR),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "ok": p.returncode == 0,
            "returncode": p.returncode,
            "stdout": p.stdout[-12000:],  # cap output
            "stderr": p.stderr[-12000:],
            "seconds": round(time.time() - started, 2),
            "cmd": " ".join(shlex.quote(c) for c in cmd),
        }
    except subprocess.TimeoutExpired as e:
        return {
            "ok": False,
            "returncode": None,
            "stdout": (e.stdout or "")[-12000:],
            "stderr": (e.stderr or "Timed out")[-12000:],
            "seconds": round(time.time() - started, 2),
            "cmd": " ".join(shlex.quote(c) for c in cmd),
        }

def _ssh_cmd(host: str, remote_cmd: str) -> List[str]:
    base = ["ssh", "-o", "BatchMode=yes"]
    if SSH_IDENTITY:
        base += ["-i", SSH_IDENTITY]
    base += [host, remote_cmd]
    return base

def _write_state(payload: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload["ts"] = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime())
    STATE_FILE.write_text(json.dumps(payload, indent=2))

def _read_state() -> dict:
    if not STATE_FILE.exists():
        return {"ts": None, "last": None}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"ts": None, "last": None}

@app.get("/")
def index():
    cams = _load_cameras()
    state = _read_state()
    return render_template("index.html", cameras=cams, state=state, service=SERVICE_NAME)

@app.post("/update_all")
def update_all():
    res = _run(["bash", str(UPDATE_SCRIPT)], timeout=600)
    _write_state({"action": "update_all", "result": res})
    return redirect(url_for("index"))

@app.post("/restart/<name>")
def restart(name: str):
    cams = _load_cameras()
    cam = next((c for c in cams if c.name == name), None)
    if not cam:
        _write_state({"action": "restart", "result": {"ok": False, "stderr": f"Unknown camera: {name}"}})
        return redirect(url_for("index"))

    remote = f"sudo systemctl restart {SERVICE_NAME} && systemctl --no-pager --full status {SERVICE_NAME} | head -n 20"
    res = _run(_ssh_cmd(cam.ssh, remote), timeout=120)
    _write_state({"action": f"restart:{name}", "result": res})
    return redirect(url_for("index"))

@app.get("/state.json")
def state_json():
    return jsonify(_read_state())

if __name__ == "__main__":
    app.run(host=BIND_HOST, port=BIND_PORT, debug=False)
