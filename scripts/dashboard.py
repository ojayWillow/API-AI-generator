#!/usr/bin/env python3
"""
dashboard.py - local web dashboard for the /generate skill.

    python scripts/dashboard.py [--port 8787] [--open] [--generations DIR]

Serves public/index.html on http://127.0.0.1:<port> and a small JSON API that
wraps kie.py. Standard library only. Binds to localhost only. The same page and
API contract are served on Vercel by api/index.py.

API
  GET  /api/state            credits, spend, caps, daily spend, generations, refs, jobs
  POST /api/refresh-credits  force a credit re-check
  POST /api/quote            {kind, model, duration, resolution}
  POST /api/generate         {kind, prompt, model, count, draft, ...}  -> starts 1..4 jobs
  POST /api/finalize         {id: <draft task id>, approve}  -> renders a 2.5 draft at 1080p
  GET  /api/tasks?id=<job>   one job's status
  POST /api/item             {id, rating, note, hidden}  -> review metadata
  POST /api/upload           {name, data (base64), kind: ref|still} -> saves a file
  GET  /media/<file>         serves a file from the generations folder
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import kie  # noqa: E402  (sibling module)

MEDIA_EXT = {".png", ".jpg", ".jpeg", ".webp", ".mp4", ".mov", ".webm"}
VIDEO_EXT = {".mp4", ".mov", ".webm"}
EXTRA_KEYS = ("group", "negative", "fields", "seed", "parent_id", "refs", "rating", "note", "hidden", "variant")

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
    if not k or k.startswith("your_"):
        os.environ.pop("KIE_API_KEY", None)
        kie.load_env()  # pick up a key saved to .env after the dashboard started
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
    days: dict[str, dict] = {}
    for e in kie.read_ledger():
        ts = e.get("created_at", "")
        if ts.startswith(month):
            d = days.setdefault(ts[:10], {"usd": 0.0, "runs": 0})
            d["usd"] += float(e.get("usd", 0) or 0)
            d["runs"] += 1
    out = []
    for day in range(1, now.day + 1):
        key = f"{month}-{day:02d}"
        v = days.get(key, {"usd": 0.0, "runs": 0})
        out.append({"date": key, "usd": round(v["usd"], 4), "runs": v["runs"]})
    return out


def refs_dir() -> Path:
    d = kie.gen_dir() / "refs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def list_refs() -> list[dict]:
    out = []
    for p in sorted(refs_dir().iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if p.is_file() and p.suffix.lower() in MEDIA_EXT and p.suffix.lower() not in VIDEO_EXT:
            out.append({"name": p.name, "path": str(p), "url": "/media/refs/" + quote(p.name)})
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
        item = {
            "id": meta.get("task_id") or p.name,
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
        }
        for k in EXTRA_KEYS:
            item[k] = meta.get(k)
        items.append(item)
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
        "refs": list_refs(),
        "pricing": {"image": pricing.get("image", {}), "video": pricing.get("video", {})},
        "jobs": jobs,
    }


# ---------------------------------------------------------------------------
# review metadata and uploads
# ---------------------------------------------------------------------------
def find_sidecar(item_id: str) -> Path | None:
    d = kie.gen_dir()
    direct = d / item_id
    if direct.exists() and direct.with_suffix(direct.suffix + ".json").exists():
        return direct.with_suffix(direct.suffix + ".json")
    for side in d.glob("*.json"):
        if side.name == "ledger.json":
            continue
        try:
            meta = json.loads(side.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if meta.get("task_id") == item_id:
            return side
    return None


def apply_extras(item_id: str, extras: dict) -> bool:
    """Merge extra keys into an item's sidecar and its ledger entry."""
    extras = {k: v for k, v in extras.items() if k in EXTRA_KEYS}
    side = find_sidecar(item_id)
    if side:
        try:
            meta = json.loads(side.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            meta = {}
        meta.update(extras)
        side.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    entries = kie.read_ledger()
    hit = False
    for e in entries:
        if e.get("task_id") == item_id:
            e.update(extras)
            hit = True
    if hit:
        kie.ledger_path().write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")
    return bool(side or hit)


def safe_name(name: str, default_ext: str) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", (name or "").strip()).strip("-.") or "upload"
    if "." not in base:
        base += "." + default_ext
    return base[:80]


def save_upload(p: dict) -> dict:
    data = p.get("data") or ""
    if "," in data and data.strip().startswith("data:"):
        data = data.split(",", 1)[1]
    try:
        raw = base64.b64decode(data, validate=False)
    except Exception:  # noqa: BLE001
        raise ValueError("upload data must be base64")
    if not raw:
        raise ValueError("empty upload")
    if len(raw) > 25 * 1024 * 1024:
        raise ValueError("upload larger than 25 MB")
    ext = "png" if raw[:8] == b"\x89PNG\r\n\x1a\n" else "jpg" if raw[:2] == b"\xff\xd8" else "png"
    name = safe_name(p.get("name") or "", ext)
    kind = p.get("kind") or "ref"
    dest_dir = refs_dir() if kind == "ref" else kie.gen_dir()
    dest = dest_dir / name
    i = 1
    while dest.exists():
        dest = dest_dir / f"{Path(name).stem}-{i}{Path(name).suffix}"
        i += 1
    dest.write_bytes(raw)
    rel = f"refs/{dest.name}" if kind == "ref" else dest.name
    info = kie.inspect_file(dest)
    return {"ok": True, "name": dest.name, "path": str(dest), "url": "/media/" + quote(rel), "media": info}


# ---------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------
def resolve_image(ref: str) -> str:
    ref = ref.strip()
    if ref.startswith("/media/"):
        ref = unquote(ref[len("/media/"):])
    if re.match(r"^https?://", ref) or Path(ref).is_absolute():
        return ref
    candidate = kie.gen_dir() / ref
    return str(candidate) if candidate.exists() else ref


def build_args(p: dict) -> list[str]:
    kind = p.get("kind")
    prompt = (p.get("prompt") or "").strip()
    negative = (p.get("negative") or "").strip()
    if kind not in ("image", "video"):
        raise ValueError("kind must be image or video")
    if not prompt:
        raise ValueError("prompt is required")
    if negative:
        prompt = f"{prompt}. Avoid: {negative}"
    args = [kind, "--prompt", prompt]
    if kind == "image":
        for k, flag in (("model", "--model"), ("aspect", "--aspect"), ("resolution", "--resolution"),
                        ("format", "--format"), ("name", "--name"), ("preset", "--preset")):
            if p.get(k):
                args += [flag, str(p[k])]
        for r in p.get("refs") or []:
            if str(r).strip():
                args += ["--ref", resolve_image(str(r))]
    else:
        if not (p.get("image") or "").strip():
            raise ValueError("a source still is required for video")
        args += ["--image", resolve_image(p["image"])]
        for k, flag in (("model", "--model"), ("duration", "--duration"), ("resolution", "--resolution"),
                        ("name", "--name"), ("preset", "--preset")):
            if p.get(k):
                args += [flag, str(p[k])]
        if p.get("seed") not in (None, "", -1, "-1"):
            args += ["--seed", str(int(p["seed"]))]
        if p.get("draft"):
            args.append("--draft")
        if p.get("audio") is False:
            args.append("--no-audio")
        if p.get("camera_fixed"):
            args.append("--camera-fixed")
        if p.get("approve"):
            args.append("--approve")
    if p.get("override_cap"):
        args.append("--override-cap")
    return args


def start_finalize(p: dict) -> dict:
    draft_id = str(p.get("id") or "").strip()
    if not draft_id:
        raise ValueError("draft id is required")
    args = ["finalize", "--draft-task-id", draft_id]
    if p.get("name"):
        args += ["--name", str(p["name"])]
    if p.get("duration"):
        args += ["--duration", str(p["duration"])]
    if p.get("approve"):
        args.append("--approve")
    if p.get("override_cap"):
        args.append("--override-cap")
    job = {"id": uuid.uuid4().hex[:8], "kind": "final", "model": "2.5-final", "prompt": p.get("prompt") or "",
           "status": "running", "kie_state": "rendering 1080p", "started_at": time.time(), "finished_at": None,
           "result": None, "log": "", "parent_id": draft_id}
    with JOBS_LOCK:
        JOBS[job["id"]] = job

    def worker() -> None:
        try:
            code, data, err = run_kie(args, timeout=2400)
            job["result"], job["log"] = data, err[-4000:]
            job["status"] = {0: "done", 2: "approval_required", 3: "cap_exceeded"}.get(code, "failed")
        except Exception as e:  # noqa: BLE001
            job["status"], job["result"] = "failed", {"ok": False, "error": str(e)}
        finally:
            job["finished_at"] = time.time()
            CREDITS["checked_at"] = 0

    threading.Thread(target=worker, daemon=True).start()
    return job


def start_jobs(p: dict) -> list[dict]:
    allow_multi = p.get("kind") == "image" or (p.get("kind") == "video" and p.get("draft"))
    count = max(1, min(4, int(p.get("count") or 1))) if allow_multi else 1
    group = p.get("group") or uuid.uuid4().hex[:8]
    jobs = []
    for i in range(count):
        q = dict(p)
        if count > 1 and p.get("name"):
            q["name"] = f"{p['name']}-v{i + 1}"
        args = build_args(q)
        extras = {
            "group": group, "negative": (p.get("negative") or "").strip() or None,
            "fields": p.get("fields") or None, "parent_id": p.get("parent_id") or None,
            "refs": [str(r) for r in (p.get("refs") or []) if str(r).strip()] or None,
            "variant": i + 1 if count > 1 else None, "rating": None, "note": None, "hidden": False,
        }
        if p.get("kind") == "video" and p.get("seed") not in (None, "", -1, "-1"):
            extras["seed"] = int(p["seed"])
        job = {
            "id": uuid.uuid4().hex[:8], "kind": p["kind"], "model": p.get("model"),
            "prompt": (p.get("prompt") or "").strip(), "status": "running", "kie_state": "running",
            "started_at": time.time(), "finished_at": None, "result": None, "log": "", "group": group,
        }
        with JOBS_LOCK:
            JOBS[job["id"]] = job
        jobs.append(job)

        def worker(job=job, args=args, extras=extras) -> None:
            try:
                code, data, err = run_kie(args)
                job["result"] = data
                job["log"] = err[-4000:]
                job["status"] = {0: "done", 2: "approval_required", 3: "cap_exceeded"}.get(code, "failed")
                if code == 0 and data.get("task_id"):
                    apply_extras(data["task_id"], extras)
                    job["item_id"] = data["task_id"]
            except subprocess.TimeoutExpired:
                job["status"], job["result"] = "failed", {"ok": False, "error": "timed out"}
            except Exception as e:  # noqa: BLE001
                job["status"], job["result"] = "failed", {"ok": False, "error": str(e)}
            finally:
                job["finished_at"] = time.time()
                CREDITS["checked_at"] = 0

        threading.Thread(target=worker, daemon=True).start()
    return jobs


def quote_cost(p: dict) -> dict:
    kind = p.get("kind")
    args = ["quote", "--kind", str(kind)]
    if p.get("model"):
        args += ["--model", str(p["model"])]
    if kind in ("video", "final") and p.get("duration"):
        args += ["--duration", str(p["duration"])]
    if p.get("resolution"):
        args += ["--resolution", "480p" if (kind == "video" and p.get("draft")) else str(p["resolution"])]
    _, data, _ = run_kie(args, timeout=60)
    allow_multi = kind == "image" or (kind == "video" and p.get("draft"))
    count = max(1, min(4, int(p.get("count") or 1))) if allow_multi else 1
    if data.get("ok") and count > 1:
        data["count"] = count
        data["credits_est_total"] = data["credits_est"] * count
        data["usd_est_total"] = round(data["usd_est"] * count, 4)
    return data


# ---------------------------------------------------------------------------
# http
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "generate-dashboard/1.1"

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
        if path == "/presets.json":
            return self._file(HERE.parent / "public" / "presets.json", "application/json; charset=utf-8")
        if path == "/app.js":
            return self._file(HERE.parent / "public" / "app.js", "text/javascript; charset=utf-8")
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
                jobs = start_jobs(body)
                return self._json({"ok": True, "jobs": jobs, "job": jobs[0]})
            if path == "/api/finalize":
                job = start_finalize(body)
                return self._json({"ok": True, "jobs": [job], "job": job})
            if path == "/api/item":
                ok = apply_extras(str(body.get("id") or ""), body)
                return self._json({"ok": ok, "error": None if ok else "item not found"}, 200 if ok else 404)
            if path == "/api/upload":
                return self._json(save_upload(body))
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
        self.send_header("Access-Control-Allow-Origin", "*")
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
    try:  # already running? reuse it instead of starting a second copy
        with urllib.request.urlopen(url + "/api/state", timeout=2) as r:
            if r.status == 200:
                print(f"generate dashboard is already running: {url}", flush=True)
                if a.open:
                    webbrowser.open(url)
                return
    except Exception:  # noqa: BLE001
        pass

    class Server(ThreadingHTTPServer):
        allow_reuse_address = os.name != "nt"  # on Windows reuse lets several copies share a port

    try:
        srv = Server(("127.0.0.1", a.port), Handler)
    except OSError as e:
        print(f"cannot start on port {a.port}: {e}. Is another program using it? Try --port 8790", flush=True)
        sys.exit(1)
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
