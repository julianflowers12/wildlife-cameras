#!/usr/bin/env python3
"""
rpi-cam-server.py

Features
- Always-on live preview (MJPEG) at /stream.mjpg
- Web UI at /
- Stills from live preview (no pipeline stop) at /api/capture_still
- Async MP4 clip recording at /api/record_clip (doesn't freeze preview)
- Optional motion detection that triggers clips
- Media browser at /media/ and direct file links at /media/<filename>
- Correct colours (uses BGR888 end-to-end for OpenCV)

Notes
- Saves files into ./media next to this script by default
  or use environment variable RPI_CAM_BASE_DIR=/path/to/media
"""

import os
import time
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from flask import (
    Flask,
    jsonify,
    request,
    render_template_string,
    send_from_directory,
    Response,
)

from picamera2 import Picamera2
from picamera2.encoders import H264Encoder
from picamera2.outputs import FfmpegOutput

import cv2

# ---------------- Boot status / progress ----------------
from threading import Event
_boot = {"step": "starting", "percent": 0, "ready": False, "errors": []}
_boot_ready_evt = Event()


# ---------------- Camera Manager ----------------

class CameraManager:
    """
    Handles:
      - Always-on preview frames for MJPEG
      - Still capture (from live preview so preview doesn't vanish)
      - Video clips (MP4 via FfmpegOutput)
      - Optional motion detection that triggers clips
    """

    def __init__(self, base_dir: Optional[str] = None):
        # Initialise camera
        self.picam2 = Picamera2()

        # Where to save stills and clips
        if base_dir is None:
            base_dir = os.environ.get("RPI_CAM_BASE_DIR")
        if base_dir is None:
            base_dir = str(Path(__file__).resolve().parent / "media")

        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

        # Locks
        self._frame_lock = threading.Lock()    # protects _preview_frame
        self._camera_lock = threading.Lock()   # serialises Picamera2 ops (critical)
        self._record_lock = threading.Lock()   # protects recording state

        # Shared state
        self._preview_frame = None
        self._preview_running = False
        self._recording = False

        self._motion_enabled = False
        self._motion_thread = None
        self._motion_stop_evt = threading.Event()

        # Single configuration used for preview AND recording
        # IMPORTANT: BGR888 so OpenCV gets correct colours without conversion.
        self.video_config = self.picam2.create_video_configuration(
            main={"size": (1280, 720), "format": "RGB888"},
        )
        with self._camera_lock:
            self.picam2.configure(self.video_config)

        # Start preview immediately so any connection sees live video
        self.start_preview()

    # ---------- Preview ----------

    def start_preview(self):
        with self._frame_lock:
            if self._preview_running:
                return
            self._preview_running = True

        with self._camera_lock:
            self.picam2.start()

        t = threading.Thread(target=self._preview_loop, daemon=True)
        t.start()

    def _restart_camera(self):
        """Robustly restart camera pipeline in the configured mode."""
        with self._camera_lock:
            try:
                self.picam2.stop_recording()
            except Exception:
                pass
            try:
                self.picam2.stop()
            except Exception:
                pass
            self.picam2.configure(self.video_config)
            self.picam2.start()

    def _preview_loop(self):
        # Continuously grab frames for preview + stills + motion detection
        while True:
            with self._frame_lock:
                if not self._preview_running:
                    break

            try:
                with self._camera_lock:
                    frame = self.picam2.capture_array("main")
                with self._frame_lock:
                    self._preview_frame = frame
            except Exception:
                # If capture fails, restart and retry
                time.sleep(0.1)
                try:
                    self._restart_camera()
                except Exception:
                    time.sleep(0.5)

    def mjpeg_generator(self):
        """Generator for Flask MJPEG endpoint."""
        while True:
            with self._frame_lock:
                frame = None if self._preview_frame is None else self._preview_frame.copy()

            if frame is None:
                time.sleep(0.02)
                continue

            ok, jpeg = cv2.imencode(".jpg", frame)
            if not ok:
                time.sleep(0.01)
                continue

            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n" + jpeg.tobytes() + b"\r\n"
            )
            time.sleep(0.03)  # ~30 fps

    # ---------- Stills (from preview, no pipeline stop) ----------

    def capture_still(self) -> Path:
        """
        Capture a still image from the current preview frame.
        This does not interrupt the camera/preview.
        """
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self.base_dir / f"still_{ts}.jpg"

        with self._frame_lock:
            frame = None if self._preview_frame is None else self._preview_frame.copy()

        # Fallback: if no preview frame yet, capture directly
        if frame is None:
            with self._camera_lock:
                frame = self.picam2.capture_array("main")

        cv2.imwrite(str(path), frame)
        return path

    # ---------- Clip recording (ASYNC-friendly) ----------

    def record_clip(self, duration: int = 30) -> Optional[Path]:
        """
        Record a clip of `duration` seconds to MP4 via ffmpeg.
        Safe to call from a background thread. Uses camera lock to avoid preview freeze.
        """
        with self._record_lock:
            if self._recording:
                return None
            self._recording = True

        output = None
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = self.base_dir / f"clip_{ts}.mp4"

            encoder = H264Encoder(bitrate=10_000_000)
            output = FfmpegOutput(str(path))

            # Start/stop recording under camera lock to avoid clashing with capture_array
            with self._camera_lock:
                self.picam2.start_recording(encoder, output)

            time.sleep(max(1, int(duration)))

            with self._camera_lock:
                self.picam2.stop_recording()

            # Optional “belt & braces”: ensure preview keeps flowing after recording
            # (helps with some libcamera/picam2 versions)
            try:
                self._restart_camera()
            except Exception:
                pass

            return path

        finally:
            try:
                if output is not None and hasattr(output, "close"):
                    output.close()
            except Exception:
                pass

            with self._record_lock:
                self._recording = False

    def start_record_clip_async(self, duration: int = 30) -> bool:
        """
        Starts recording in a daemon thread and returns immediately.
        Returns False if already recording.
        """
        with self._record_lock:
            if self._recording:
                return False

        threading.Thread(target=self.record_clip, args=(duration,), daemon=True).start()
        return True

    # ---------- Motion detection ----------

    def enable_motion(self):
        self._motion_enabled = True
        if self._motion_thread is None or not self._motion_thread.is_alive():
            self._motion_stop_evt.clear()
            self._motion_thread = threading.Thread(target=self._motion_loop, daemon=True)
            self._motion_thread.start()

    def disable_motion(self):
        self._motion_enabled = False
        self._motion_stop_evt.set()

    def _motion_loop(self):
        prev_gray = None
        cool_down_until = 0

        while not self._motion_stop_evt.is_set():
            with self._frame_lock:
                frame = None if self._preview_frame is None else self._preview_frame.copy()

            if frame is None:
                time.sleep(0.1)
                continue

            # frame is BGR (correct for OpenCV)
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            gray = cv2.GaussianBlur(gray, (21, 21), 0)

            if prev_gray is None:
                prev_gray = gray
                time.sleep(0.1)
                continue

            diff = cv2.absdiff(prev_gray, gray)
            thresh = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)[1]
            thresh = cv2.dilate(thresh, None, iterations=2)
            contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

            motion_detected = any(cv2.contourArea(c) > 1500 for c in contours)

            now = time.time()
            if motion_detected and now > cool_down_until:
                # Fire a 30s recording in background
                self.start_record_clip_async(30)
                cool_down_until = now + 40  # seconds cooldown

            prev_gray = gray
            time.sleep(0.1)


# ---------------- Flask app ----------------

app = Flask(__name__)
camera = CameraManager()
_boot.update({"step": "running", "percent": 100, "ready": True})
_boot_ready_evt.set()


INDEX_HTML = """
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>rpi-cam-server</title>
    <style>
      body { font-family: system-ui, -apple-system, BlinkMacSystemFont, sans-serif; margin: 1rem; max-width: 900px; }
      img { max-width: 100%; border: 1px solid #ccc; border-radius: 8px; }
      .controls { margin-top: 1rem; display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: center; }
      button, a.btn {
        padding: 0.5rem 1rem; cursor: pointer; border-radius: 8px; border: 1px solid #888;
        background: #f3f3f3; text-decoration: none; color: #111; display: inline-block;
      }
      button:hover, a.btn:hover { background: #e5e5e5; }
      #status { margin-top: 1rem; font-size: 0.95rem; }
      code { background: #f5f5f5; padding: 0.1rem 0.3rem; border-radius: 5px; }
    </style>
  </head>
  <body>
    <h1>rpi-cam-server</h1>

    <p>
      Live preview is always on. Stills and clips are saved in
      <code>media/</code>.
      <a class="btn" href="/media/" target="_blank" rel="noopener">📁 Browse saved media</a>
    </p>

    <img src="/stream.mjpg" alt="Live preview" />

    <div class="controls">
      <button id="btn-still">Take still</button>
      <button id="btn-clip">Record 30s clip</button>
      <button id="btn-motion-on">Motion: ON</button>
      <button id="btn-motion-off">Motion: OFF</button>
    </div>

    <div id="status"></div>

    <script>
      function setStatus(msg) {
        document.getElementById("status").textContent = msg;
      }

      async function postJSON(url, data) {
        const res = await fetch(url, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(data || {}),
        });
        return res.json();
      }

      document.getElementById("btn-still").onclick = async () => {
        setStatus("Capturing still...");
        try {
          const res = await fetch("/api/capture_still", { method: "POST" });
          const data = await res.json();
          setStatus("Still saved: " + data.file + " (open: /media/" + data.file + ")");
        } catch (e) {
          setStatus("Error capturing still");
        }
      };

      document.getElementById("btn-clip").onclick = async () => {
        setStatus("Starting 30s recording...");
        try {
          const data = await postJSON("/api/record_clip", { duration: 30 });
          setStatus(data.message || "Recording started");
        } catch (e) {
          setStatus("Error starting recording");
        }
      };

      document.getElementById("btn-motion-on").onclick = async () => {
        setStatus("Enabling motion detection...");
        try {
          const data = await postJSON("/api/motion", { mode: "on" });
          setStatus("Motion detection: " + data.motion);
        } catch (e) {
          setStatus("Error enabling motion");
        }
      };

      document.getElementById("btn-motion-off").onclick = async () => {
        setStatus("Disabling motion detection...");
        try {
          const data = await postJSON("/api/motion", { mode: "off" });
          setStatus("Motion detection: " + data.motion);
        } catch (e) {
          setStatus("Error disabling motion");
        }
      };
    </script>
  </body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(INDEX_HTML)


@app.route("/stream.mjpg")
def stream_mjpeg():
    return Response(
        camera.mjpeg_generator(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.route("/api/capture_still", methods=["POST"])
def api_capture_still():
    path = camera.capture_still()
    return jsonify({"status": "ok", "file": path.name})


@app.route("/api/record_clip", methods=["POST"])
def api_record_clip():
    if request.is_json:
        duration = int(request.json.get("duration", 30))
    else:
        duration = int(request.form.get("duration", 30) or 30)

    ok = camera.start_record_clip_async(duration)
    if not ok:
        return jsonify({"status": "busy", "message": "Already recording"}), 409

    return jsonify({"status": "ok", "message": f"Recording {duration}s clip"})


@app.route("/api/motion", methods=["POST"])
def api_motion():
    if request.is_json:
        mode = request.json.get("mode", "off")
    else:
        mode = request.form.get("mode", "off")

    if mode == "on":
        camera.enable_motion()
        return jsonify({"status": "ok", "motion": "on"})
    else:
        camera.disable_motion()
        return jsonify({"status": "ok", "motion": "off"})


@app.route("/media/<path:filename>")
def media_file(filename):
    return send_from_directory(camera.base_dir, filename)


@app.route("/media/")
def media_index():
    files = sorted(os.listdir(camera.base_dir))
    items = []
    for f in files:
        if f.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".mp4", ".h264")):
            items.append(f)

    html = [f"<h1>Media files</h1><p>Folder: <code>{camera.base_dir}</code></p><ul>"]
    for f in items:
        html.append(f'<li><a href="/media/{f}">{f}</a></li>')
    html.append("</ul>")
    return "".join(html)


@app.route("/api/status")
def api_status():
    return jsonify(
        {
            "boot": _boot,
            "recording": camera._recording,
            "motion_enabled": camera._motion_enabled,
            "media_dir": str(camera.base_dir),
        }
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    # threaded=True helps the MJPEG stream stay responsive while other requests run
    app.run(host="0.0.0.0", port=port, threaded=True)
