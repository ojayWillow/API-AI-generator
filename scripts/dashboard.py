#!/usr/bin/env python3
"""
dashboard.py - local web dashboard for the /generate skill.

    python scripts/dashboard.py [--port 8787] [--open] [--generations DIR]

Serves public/index.html on http://127.0.0.1:<port> and a small JSON API that
wraps kie.py. Standard library only. Binds to localhost only. The same page and
API contract are served on Vercel by api/index.py.

API
  GET  /api/state            credits, spend, caps, daily spend, generations, jobs
  POST /api/refresh-credits  force a credit re-check
  POST /api/quote            {kind, model, duration, resolution}
  POST /api/generate         {kind, prompt, model, ...}  -> starts a job
  GET  /api/tasks?id=<job>   one job's status
  GET  /media/<file>         serves a file from the generations folder
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import urllib.parse
from urllib.parse import quote, unquote, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import kie  # noqa: E402  (sibling module)

MEDIA_EXT = {".png", ".jpg", ".jpeg", ".webp", ".mp4", ".mov", ".webm"}
VIDEO_EXT = {".mp4", ".mov", ".webm"}

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
CREDITS = {"value": None, "usd": None, "checked_at": 0.0, "error": None, "checking": False}


# ---------------------------------------------------------------------------
# kie.py wrapper
# ---------------------------------------------------------------------------
def run_kie(args: list[str], timeout: int = 1800) -> tuple[int, dict, str]:
    proc = subprocess.run([sys.executable, str(HERE / "kie.py"), *args],
                          capture_output=True, text=True, timeout=timeout, env=os.environ.copy())
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        data = {"ok": False, "error": (proc.stdout or proc.stderr)[-2000:]}
    return proc.returncode, data, proc.stderr


def key_present() -> bool:
    k = os.environ.get("KIE_API_KEY", "").strip()
    return bool(k) and not k.startswith("your_")


def refresh_credits(force: bool = False) -> None:
    if CREDITS["checking"]:
        return
    if not force and time.time() - CREDITS["checked_at"] < 120:
        return
    if not key_present():
        CREDITS.update(value=None, usd=None, error="KIE_API_KEY is not set", checked_at=time.time())
        return

    def worker() -> None:
        CREDITS["checking"] = True
        try:
            _, data, _ = run_kie(["credits"], timeout=60)
            if data.get("ok"):
                CREDITS.update(value=data.get("credits"), usd=data.get("usd_equivalent"), error=None)
            else:
                CREDITS.update(error=data.get("error") or "credit check failed")
        except Exception as e:  # noqa: BLE001
            CREDITS.update(error=str(e))
        finally:
            CREDITS.update(checked_at=time.time(), checking=False)

    threading.Thread(target=worker, daemon=True).start()


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------
def daily_spend() -> list[dict]:
    now = dt.datetime.now().astimezone()
    month = now.strftime("%Y-%m")
    days = {}
    for e in kie.read_ledger():
        ts = e.get("created_at", "")
        if ts.startswith(month):
            d = ts[:10]
            days.setdefault(d, {"usd": 0.0, "runs": 0})
            days[d]["usd"] += float(e.get("usd", 0) or 0)
            days[d]["runs"] += 1
    out = []
    for day in range(1, now.day + 1):
        key = f"{month}-{day:02d}"
        v = days.get(key, {"usd": 0.0, "runs": 0})
        out.append({"date": key, "usd": round(v["usd"], 4), "runs": v["runs"]})
    return out


def list_generations() -> list[dict]:
    d = kie.gen_dir()
    items = []
    for p in sorted(d.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not p.is_file() or p.suffix.lower() not in MEDIA_EXT:
            continue
        meta = {}
        side = p.with_suffix(p.suffix + ".json")
        if side.exists():
            try:
                meta = json.loads(side.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                meta = {}
        st = p.stat()
        items.append({
            "name": p.name,
            "path": str(p),
            "url": "/media/" + quote(p.name),
            "kind": "video" if p.suffix.lower() in VIDEO_EXT else "image",
            "bytes": st.st_size,
            "modified": dt.datetime.fromtimestamp(st.st_mtime).astimezone().isoformat(timespec="seconds"),
            "prompt": meta.get("prompt"),
            "model": meta.get("model"),
            "model_id": meta.get("model_id"),
            "usd": meta.get("usd"),
            "credits_consumed": meta.get("credits_consumed"),
            "created_at": meta.get("created_at"),
            "params": meta.get("params"),
            "preset": meta.get("preset"),
            "input_image": meta.get("input_image"),
            "task_id": meta.get("task_id"),
            "media": meta.get("media"),
        })
    return items


def state() -> dict:
    refresh_credits()
    with JOBS_LOCK:
        jobs = sorted(JOBS.values(), key=lambda j: j["started_at"], reverse=True)[:50]
    pricing = kie.load_pricing()
    return {
        "ok": True,
        "mode": "local",
        "now": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "key_present": key_present(),
        "credits": {k: v for k, v in CREDITS.items() if k != "checking"},
        "usd_per_credit": kie.USD_PER_CREDIT,
        "spend": kie.spend_summary(),
        "daily": daily_spend(),
        "generations_dir": str(kie.gen_dir()),
        "generations": list_generations(),
        "pricing": {"image": pricing.get("image", {}), "video": pricing.get("video", {})},
        "jobs": jobs,
    }


# ---------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------
def resolve_image(ref: str) -> str:
    ref = ref.strip()
    if re.match(r"^https?://", ref) or Path(ref).is_absolute():
        return ref
    candidate = kie.gen_dir() / ref
    return str(candidate) if candidate.exists() else ref


def build_args(p: dict) -> list[str]:
    kind = p.get("kind")
    prompt = (p.get("prompt") or "").strip()
    if kind not in ("image", "video"):
        raise ValueError("kind must be image or video")
    if not prompt:
        raise ValueError("prompt is required")
    args = [kind, "--prompt", prompt]
    if kind == "image":
        for k, flag in (("model", "--model"), ("aspect", "--aspect"), ("resolution", "--resolution"),
                        ("format", "--format"), ("name", "--name"), ("preset", "--preset")):
            if p.get(k):
                args += [flag, str(p[k])]
        for r in p.get("refs") or []:
            if str(r).strip():
                args += ["--ref", str(r).strip()]
    else:
        if not (p.get("image") or "").strip():
            raise ValueError("a source still is required for video")
        args += ["--image", resolve_image(p["image"])]
        for k, flag in (("model", "--model"), ("duration", "--duration"), ("name", "--name"), ("preset", "--preset")):
            if p.get(k):
                args += [flag, str(p[k])]
        if p.get("camera_fixed"):
            args.append("--camera-fixed")
        if p.get("approve"):
            args.append("--approve")
    if p.get("override_cap"):
        args.append("--override-cap")
    return args


def start_job(p: dict) -> dict:
    args = build_args(p)
    job = {
        "id": uuid.uuid4().hex[:8], "kind": p["kind"], "model": p.get("model"),
        "prompt": p["prompt"].strip(), "status": "running", "kie_state": "running",
        "started_at": time.time(), "finished_at": None, "result": None, "log": "",
    }
    with JOBS_LOCK:
        JOBS[job["id"]] = job

    def worker() -> None:
        try:
            code, data, err = run_kie(args)
            job["result"] = data
            job["log"] = err[-4000:]
            job["status"] = {0: "done", 2: "approval_required", 3: "cap_exceeded"}.get(code, "failed")
        except subprocess.TimeoutExpired:
            job["status"], job["result"] = "failed", {"ok": False, "error": "timed out"}
        except Exception as e:  # noqa: BLE001
            job["status"], job["result"] = "failed", {"ok": False, "error": str(e)}
        finally:
            job["finished_at"] = time.time()
            CREDITS["checked_at"] = 0  # force a refresh after spend

    threading.Thread(target=worker, daemon=True).start()
    return job


def quote_cost(p: dict) -> dict:
    kind = p.get("kind")
    args = ["quote", "--kind", str(kind)]
    if p.get("model"):
        args += ["--model", str(p["model"])]
    if kind == "video" and p.get("duration"):
        args += ["--duration", str(p["duration"])]
    if kind == "image" and p.get("resolution"):
        args += ["--resolution", str(p["resolution"])]
    _, data, _ = run_kie(args, timeout=60)
    return data


# ---------------------------------------------------------------------------
# http
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "generate-dashboard/1.0"

    def log_message(self, fmt, *args):  # quiet
        if args and str(args[1]).startswith(("4", "5")):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _json(self, obj: dict, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return {}

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._file(HERE.parent / "public" / "index.html", "text/html; charset=utf-8")
        if path == "/api/state":
            return self._json(state())
        if path == "/api/tasks":
            qs = urllib.parse.parse_qs(urlparse(self.path).query)
            job_id = (qs.get("id") or [""])[0]
            with JOBS_LOCK:
                job = JOBS.get(job_id)
            return self._json({"ok": True, "job": job} if job else {"ok": False, "gone": True, "error": "unknown job"}, 200 if job else 404)
        if path.startswith("/media/"):
            name = unquote(path[len("/media/"):])
            base = kie.gen_dir().resolve()
            target = (base / name).resolve()
            if base not in target.parents or not target.is_file():
                return self._json({"ok": False, "error": "not found"}, 404)
            return self._file(target, mimetypes.guess_type(str(target))[0] or "application/octet-stream")
        self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        body = self._read_json()
        try:
            if path == "/api/refresh-credits":
                refresh_credits(force=True)
                return self._json({"ok": True})
            if path == "/api/quote":
                return self._json(quote_cost(body))
            if path == "/api/generate":
                job = start_job(body)
                return self._json({"ok": True, "job": job})
        except ValueError as e:
            return self._json({"ok": False, "error": str(e)}, 400)
        self._json({"ok": False, "error": "not found"}, 404)

    def _file(self, path: Path, ctype: str) -> None:
        if not path.exists():
            return self._json({"ok": False, "error": "missing file"}, 404)
        size = path.stat().st_size
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        partial = False
        if rng and rng.startswith("bytes="):
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                a, b = m.groups()
                if a:
                    start = int(a)
                    end = int(b) if b else size - 1
                elif b:
                    start = max(0, size - int(b))
                start, end = max(0, start), min(end, size - 1)
                partial = start <= end
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break
                remaining -= len(chunk)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--open", action="store_true", help="open the dashboard in your browser")
    ap.add_argument("--generations", help="override GENERATIONS_DIR for this run")
    a = ap.parse_args()
    if a.generations:
        os.environ["GENERATIONS_DIR"] = a.generations
    kie.load_env()
    url = f"http://127.0.0.1:{a.port}"
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    print(f"generate dashboard: {url}  (generations: {kie.gen_dir()})", flush=True)
    if not key_present():
        print("warning: KIE_API_KEY is not set; the dashboard will show the ledger but cannot generate", flush=True)
    if a.open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
