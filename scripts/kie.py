#!/usr/bin/env python3
"""
kie.py - pay-per-use image and video generation through kie.ai.

Standard library only (no pip installs). Every command prints one JSON object
on stdout so the calling agent can parse it. Human-readable progress goes to
stderr.

Commands
  credits                                   remaining kie.ai credits
  quote    --kind image|video --model M [--duration N] [--resolution R]
  image    --prompt P [--model nano-banana-2|gpt-image-2] [--aspect 9:16]
           [--resolution 1K|2K|4K] [--ref URL_OR_PATH ...] [--name slug]
  upload   --file PATH                      host a local file on kie.ai (24 h)
  video    --image PATH_OR_URL --prompt P [--model pro|lite|2.5]
           [--duration 5|10] [--name slug] --approve
  status   --task-id ID
  ledger   [--all]                          spend summary (today / month)
  inspect  --file PATH                      real width/height (+ duration) of png/jpg/mp4

Environment (read from .env in the skill folder, then the shell):
  KIE_API_KEY        required
  GENERATIONS_DIR    default <skill>/generations
  SESSION_CAP_USD    default 10   (spend allowed per calendar day)
  MONTHLY_CAP_USD    default 50

Exit codes: 0 ok, 1 error, 2 approval required (quote printed), 3 budget cap.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import mimetypes
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
API = "https://api.kie.ai"
UPLOAD_API = "https://kieai.redpandaai.co"
USD_PER_CREDIT = 0.005  # kie.ai: $5 per 1,000 credits

# ---------------------------------------------------------------------------
# Pricing. Estimates only; the ledger stores the real creditsConsumed that
# kie.ai reports after each job. Edit pricing.json next to this script to
# update, and verify at https://kie.ai/pricing
# ---------------------------------------------------------------------------
DEFAULT_PRICING = {
    "_note": "Estimated credits. 1 credit = $0.005. Verify at https://kie.ai/pricing",
    "image": {
        "nano-banana-2": {"id": "nano-banana-2", "credits": {"1K": 8, "2K": 10, "4K": 14}},
        "gpt-image-2": {"id": "gpt-image-2-text-to-image", "credits": {"1K": 10, "2K": 12, "4K": 16}},
    },
    "video": {
        "pro": {"id": "bytedance/v1-pro-image-to-video", "credits_per_second": 14, "durations": [5, 10]},
        "lite": {"id": "bytedance/v1-lite-image-to-video", "credits_per_second": 10, "durations": [5, 10]},
        "2.5": {"id": "bytedance/seedance-2-5", "credits_per_second": 20, "durations": [4, 5, 6, 8, 10, 12]},
    },
}


def load_pricing() -> dict:
    p = SKILL_DIR / "scripts" / "pricing.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            die(f"pricing.json is not valid JSON: {e}")
    return DEFAULT_PRICING


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def out(obj: dict, code: int = 0) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))
    sys.exit(code)


def die(msg: str, code: int = 1, **extra) -> None:
    out({"ok": False, "error": msg, **extra}, code)


def load_env() -> None:
    """Load KEY=VALUE lines from <skill>/.env and ./.env without overriding the shell."""
    for env_path in (SKILL_DIR / ".env", Path.cwd() / ".env"):
        if not env_path.exists():
            continue
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            os.environ.setdefault(k, v)


def api_key() -> str:
    key = os.environ.get("KIE_API_KEY", "").strip()
    if not key or key.startswith("your_"):
        die("KIE_API_KEY is not set. Put it in the skill's .env file (see .env.example) "
            "or export it in your shell. Get a key at https://kie.ai/api-key")
    return key


def gen_dir() -> Path:
    d = Path(os.environ.get("GENERATIONS_DIR") or (SKILL_DIR / "generations")).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def caps() -> tuple[float, float]:
    return (float(os.environ.get("SESSION_CAP_USD", "10")),
            float(os.environ.get("MONTHLY_CAP_USD", "50")))


def slugify(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:limit].rstrip("-") or "gen"


def http_json(method: str, url: str, body: dict | None = None, headers: dict | None = None,
              timeout: int = 60) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    h = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        die(f"HTTP {e.code} from {url}: {raw[:500]}")
    except urllib.error.URLError as e:
        die(f"Network error calling {url}: {e.reason}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        die(f"Non-JSON response from {url}: {raw[:500]}")


def kie(method: str, path: str, body: dict | None = None, base: str = API) -> dict:
    return http_json(method, base + path, body, {"Authorization": f"Bearer {api_key()}"})


def download(url: str, dest: Path) -> Path:
    req = urllib.request.Request(url, headers={"User-Agent": "kie-generate-skill/1.0"})
    with urllib.request.urlopen(req, timeout=300) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)
    return dest


def ext_from_url(url: str, default: str) -> str:
    path = urllib.parse.urlparse(url).path
    ext = Path(path).suffix.lower().lstrip(".")
    return ext if ext in {"png", "jpg", "jpeg", "webp", "mp4", "mov", "webm"} else default


# ---------------------------------------------------------------------------
# ledger
# ---------------------------------------------------------------------------
def ledger_path() -> Path:
    return gen_dir() / "ledger.json"


def read_ledger() -> list[dict]:
    p = ledger_path()
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log(f"warning: {p} is corrupt, starting a fresh ledger (old file kept as .bak)")
        p.rename(p.with_suffix(".json.bak"))
        return []


def append_ledger(entry: dict) -> None:
    entries = read_ledger()
    entries.append(entry)
    ledger_path().write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")


def spend_summary() -> dict:
    now = dt.datetime.now().astimezone()
    today = now.date().isoformat()
    month = now.strftime("%Y-%m")
    today_usd = month_usd = 0.0
    today_n = month_n = 0
    for e in read_ledger():
        ts = e.get("created_at", "")
        usd = float(e.get("usd", 0) or 0)
        if ts.startswith(month):
            month_usd += usd
            month_n += 1
        if ts.startswith(today):
            today_usd += usd
            today_n += 1
    s_cap, m_cap = caps()
    return {
        "today_usd": round(today_usd, 4), "today_runs": today_n, "session_cap_usd": s_cap,
        "month_usd": round(month_usd, 4), "month_runs": month_n, "monthly_cap_usd": m_cap,
        "remaining_today_usd": round(s_cap - today_usd, 4),
        "remaining_month_usd": round(m_cap - month_usd, 4),
    }


def enforce_caps(est_usd: float, override: bool) -> dict:
    s = spend_summary()
    over = []
    if s["today_usd"] + est_usd > s["session_cap_usd"]:
        over.append(f"session cap ${s['session_cap_usd']:.2f} (spent today ${s['today_usd']:.2f}, run ${est_usd:.2f})")
    if s["month_usd"] + est_usd > s["monthly_cap_usd"]:
        over.append(f"monthly cap ${s['monthly_cap_usd']:.2f} (spent this month ${s['month_usd']:.2f}, run ${est_usd:.2f})")
    if over and not override:
        die("This run would exceed the " + " and ".join(over) +
            ". Ask the user; rerun with --override-cap only after they explicitly agree.",
            code=3, spend=s)
    return s


# ---------------------------------------------------------------------------
# media inspection (pure python)
# ---------------------------------------------------------------------------
def image_size(path: Path) -> tuple[int, int] | None:
    with open(path, "rb") as f:
        head = f.read(32)
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            w, h = struct.unpack(">II", head[16:24])
            return w, h
        if head[:2] == b"\xff\xd8":  # JPEG: walk markers to a SOF
            f.seek(2)
            while True:
                marker = f.read(2)
                if len(marker) < 2 or marker[0] != 0xFF:
                    return None
                m = marker[1]
                if m == 0xD8 or 0xD0 <= m <= 0xD7 or m == 0x01:
                    continue
                seg_len = struct.unpack(">H", f.read(2))[0]
                if 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):
                    f.read(1)
                    h, w = struct.unpack(">HH", f.read(4))
                    return w, h
                f.seek(seg_len - 2, 1)
        if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            f.seek(12)
            chunk = f.read(4)
            if chunk == b"VP8X":
                f.seek(4, 1)
                b = f.read(10)
                w = 1 + int.from_bytes(b[4:7], "little")
                h = 1 + int.from_bytes(b[7:10], "little")
                return w, h
            if chunk == b"VP8 ":
                f.seek(4 + 6, 1)
                w, h = struct.unpack("<HH", f.read(4))
                return w & 0x3FFF, h & 0x3FFF
            if chunk == b"VP8L":
                f.seek(4 + 1, 1)
                b = int.from_bytes(f.read(4), "little")
                return (b & 0x3FFF) + 1, ((b >> 14) & 0x3FFF) + 1
    return None


def _boxes(buf: bytes, start: int, end: int):
    pos = start
    while pos + 8 <= end:
        size, kind = struct.unpack(">I4s", buf[pos:pos + 8])
        hdr = 8
        if size == 1:
            size = struct.unpack(">Q", buf[pos + 8:pos + 16])[0]
            hdr = 16
        elif size == 0:
            size = end - pos
        if size < hdr:
            return
        yield kind, pos + hdr, pos + size
        pos += size


def mp4_info(path: Path) -> dict | None:
    """Width/height/duration from the moov box, without ffmpeg."""
    with open(path, "rb") as f:
        buf = f.read()
    if buf[4:8] != b"ftyp":
        return None
    info: dict = {}
    for kind, s, e in _boxes(buf, 0, len(buf)):
        if kind != b"moov":
            continue
        for k2, s2, e2 in _boxes(buf, s, e):
            if k2 == b"mvhd":
                v = buf[s2]
                if v == 1:
                    ts, dur = struct.unpack(">IQ", buf[s2 + 20:s2 + 32])
                else:
                    ts, dur = struct.unpack(">II", buf[s2 + 12:s2 + 20])
                if ts:
                    info["duration_s"] = round(dur / ts, 2)
            elif k2 == b"trak":
                for k3, s3, e3 in _boxes(buf, s2, e2):
                    if k3 == b"tkhd":
                        v = buf[s3]
                        off = s3 + (88 if v == 1 else 76)
                        w, h = struct.unpack(">II", buf[off:off + 8])
                        w, h = w >> 16, h >> 16
                        if w and h and "width" not in info:
                            info["width"], info["height"] = w, h
    return info or None


def inspect_file(path: Path) -> dict:
    if not path.exists():
        die(f"file not found: {path}")
    kind = mimetypes.guess_type(str(path))[0] or ""
    res: dict = {"file": str(path), "bytes": path.stat().st_size, "mime": kind}
    if kind.startswith("image/"):
        sz = image_size(path)
        if sz:
            res["width"], res["height"] = sz
    elif kind.startswith("video/") or path.suffix.lower() in {".mp4", ".mov", ".m4v"}:
        mi = mp4_info(path)
        if mi:
            res.update(mi)
    if "width" in res and "height" in res:
        w, h = res["width"], res["height"]
        res["orientation"] = "vertical" if h > w else ("horizontal" if w > h else "square")
        res["is_1080x1920"] = (w, h) == (1080, 1920)
    return res


def extract_frame(video: Path, dest: Path, at_s: float = 1.0) -> Path | None:
    """Optional QA helper: needs ffmpeg on PATH; returns None if unavailable."""
    ff = shutil.which("ffmpeg")
    if not ff:
        return None
    subprocess.run([ff, "-y", "-loglevel", "error", "-ss", str(at_s), "-i", str(video),
                    "-frames:v", "1", str(dest)], check=False)
    return dest if dest.exists() else None


# ---------------------------------------------------------------------------
# kie.ai calls
# ---------------------------------------------------------------------------
def create_task(model_id: str, inp: dict) -> str:
    r = kie("POST", "/api/v1/jobs/createTask", {"model": model_id, "input": inp})
    if r.get("code") != 200 or not r.get("data", {}).get("taskId"):
        die(f"createTask failed: {r.get('msg') or r}", response=r)
    return r["data"]["taskId"]


def poll_task(task_id: str, every: int, timeout: int) -> dict:
    t0 = time.time()
    last_state = None
    while True:
        r = kie("GET", f"/api/v1/jobs/recordInfo?taskId={urllib.parse.quote(task_id)}")
        d = r.get("data") or {}
        state = d.get("state")
        if state != last_state:
            log(f"  task {task_id}: {state}")
            last_state = state
        if state == "success":
            rj = d.get("response")
            if not rj:
                try:
                    rj = json.loads(d.get("resultJson") or "{}")
                except json.JSONDecodeError:
                    rj = {}
            d["_result"] = rj
            return d
        if state == "fail":
            die(f"kie.ai task failed ({d.get('failCode')}): {d.get('failMsg')} - "
                "failed tasks are not billed", task=d)
        if time.time() - t0 > timeout:
            die(f"timed out after {timeout}s waiting for task {task_id}; "
                f"check later with: kie.py status --task-id {task_id}")
        time.sleep(every)


def upload_file(path: Path) -> str:
    """Host a local file on kie.ai for 24 h and return its public URL."""
    if not path.exists():
        die(f"file not found: {path}")
    if path.stat().st_size > 10 * 1024 * 1024:
        die("upload is limited to 10 MB for base64 uploads; shrink the image first")
    mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    r = kie("POST", "/api/file-base64-upload",
            {"base64Data": f"data:{mime};base64,{b64}", "uploadPath": "generate-skill",
             "fileName": path.name}, base=UPLOAD_API)
    url = (r.get("data") or {}).get("downloadUrl")
    if r.get("code") != 200 or not url:
        die(f"upload failed: {r.get('msg') or r}", response=r)
    return url


def ensure_url(ref: str) -> str:
    if re.match(r"^https?://", ref):
        return ref
    log(f"  uploading {ref} to kie.ai file host (expires in 24 h)")
    return upload_file(Path(ref).expanduser())


# ---------------------------------------------------------------------------
# quoting
# ---------------------------------------------------------------------------
def quote_image(model: str, resolution: str) -> dict:
    pr = load_pricing()["image"]
    if model not in pr:
        die(f"unknown image model '{model}'. Options: {', '.join(pr)}")
    credits = pr[model]["credits"].get(resolution)
    if credits is None:
        die(f"resolution '{resolution}' not priced for {model}. Options: {', '.join(pr[model]['credits'])}")
    return {"kind": "image", "model": model, "model_id": pr[model]["id"], "resolution": resolution,
            "credits_est": credits, "usd_est": round(credits * USD_PER_CREDIT, 4)}


def normalise_duration(model: str, requested: float) -> tuple[int, str | None]:
    allowed = load_pricing()["video"][model]["durations"]
    if requested in allowed:
        return int(requested), None
    nearest = min(allowed, key=lambda a: (abs(a - requested), a))
    return nearest, f"{model} only supports {allowed}s; {requested}s rounded to {nearest}s"


def video_resolution(model: str, requested: str | None) -> str:
    pr = load_pricing()["video"][model]
    allowed = pr.get("resolutions") or ["1080p"]
    if requested in allowed:
        return requested
    return pr.get("default_resolution") or allowed[-1]


def quote_video(model: str, duration: float, resolution: str | None = None) -> dict:
    pr = load_pricing()["video"]
    if model not in pr:
        die(f"unknown video model '{model}'. Options: {', '.join(pr)}")
    dur, note = normalise_duration(model, duration)
    res = video_resolution(model, resolution)
    rate = pr[model]["credits_per_second"]
    if isinstance(rate, dict):
        rate = rate.get(res) or max(rate.values())
    credits = int(rate * dur)
    q = {"kind": "video", "model": model, "model_id": pr[model]["id"], "duration_s": dur,
         "resolution": res, "credits_est": credits, "usd_est": round(credits * USD_PER_CREDIT, 4)}
    if note:
        q["note"] = note
    return q


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
def cmd_credits(_: argparse.Namespace) -> None:
    r = kie("GET", "/api/v1/chat/credit")
    if r.get("code") != 200:
        die(f"credit check failed: {r.get('msg') or r}", response=r)
    credits = r.get("data")
    if isinstance(credits, dict):
        credits = credits.get("credits", credits)
    try:
        usd = round(float(credits) * USD_PER_CREDIT, 2)
    except (TypeError, ValueError):
        usd = None
    out({"ok": True, "credits": credits, "usd_equivalent": usd, "spend": spend_summary()})


def cmd_quote(a: argparse.Namespace) -> None:
    q = quote_image(a.model or "nano-banana-2", a.resolution or "1K") if a.kind == "image" \
        else quote_video(a.model or "pro", a.duration or 10, a.resolution)
    out({"ok": True, **q, "spend": spend_summary()})


def finish(kind: str, task: dict, model_key: str, model_id: str, prompt: str, params: dict,
           name: str, default_ext: str, est_credits: int, extra: dict) -> None:
    urls = task["_result"].get("resultUrls") or []
    if not urls:
        die("task succeeded but returned no resultUrls", task=task)
    src = urls[0]
    dest = gen_dir() / f"{name}.{ext_from_url(src, default_ext)}"
    i = 1
    while dest.exists():
        dest = dest.with_name(f"{name}-{i}{dest.suffix}")
        i += 1
    log(f"  downloading -> {dest}")
    download(src, dest)
    info = inspect_file(dest)
    consumed = task.get("creditsConsumed")
    credits = float(consumed) if consumed is not None else float(est_credits)
    entry = {
        "created_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "kind": kind, "model": model_key, "model_id": model_id, "task_id": task.get("taskId"),
        "prompt": prompt, "params": params, "file": str(dest), "source_url": src,
        "credits_est": est_credits, "credits_consumed": consumed,
        "usd": round(credits * USD_PER_CREDIT, 4), **extra,
    }
    dest.with_suffix(dest.suffix + ".json").write_text(
        json.dumps({**entry, "media": info}, indent=2, ensure_ascii=False), encoding="utf-8")
    append_ledger(entry)
    out({"ok": True, "file": str(dest), "source_url": src, "media": info,
         "credits_consumed": consumed, "usd": entry["usd"], "task_id": task.get("taskId"),
         "spend": spend_summary()})


def cmd_image(a: argparse.Namespace) -> None:
    model = a.model or "nano-banana-2"
    resolution = a.resolution or "1K"
    aspect = a.aspect or "9:16"
    q = quote_image(model, resolution)
    enforce_caps(q["usd_est"], a.override_cap)
    refs = [ensure_url(r) for r in (a.ref or [])]
    if model == "nano-banana-2":
        inp = {"prompt": a.prompt, "image_input": refs, "aspect_ratio": aspect,
               "resolution": resolution, "output_format": a.format or "png"}
    else:  # gpt-image-2-text-to-image
        if refs:
            log("  note: gpt-image-2 text-to-image ignores reference images")
        inp = {"prompt": a.prompt, "aspect_ratio": aspect, "resolution": resolution}
    log(f"Submitting image to {q['model_id']} (~{q['credits_est']} credits / ${q['usd_est']:.3f})")
    task_id = create_task(q["model_id"], inp)
    task = poll_task(task_id, every=10, timeout=600)
    name = a.name or f"{dt.datetime.now():%Y%m%d-%H%M%S}-{slugify(a.prompt)}"
    finish("image", task, model, q["model_id"], a.prompt,
           {"aspect_ratio": aspect, "resolution": resolution, "refs": refs},
           name, "png", q["credits_est"], {"preset": a.preset})


def cmd_upload(a: argparse.Namespace) -> None:
    url = upload_file(Path(a.file).expanduser())
    out({"ok": True, "url": url, "expires": "24h"})


def cmd_video(a: argparse.Namespace) -> None:
    model = a.model or "pro"
    q = quote_video(model, a.duration or 10, a.resolution)
    s = enforce_caps(q["usd_est"], a.override_cap)
    if not a.approve:
        out({"ok": False, "approval_required": True,
             "message": "Paid video run. Show this quote to the user and rerun with --approve "
                        "after they explicitly say go.",
             **q, "spend": s}, code=2)
    still_local = None if re.match(r"^https?://", a.image) else Path(a.image).expanduser()
    if still_local is not None:
        info = inspect_file(still_local)
        if not info.get("width"):
            die(f"could not read image dimensions from {still_local}")
        log(f"  still: {info['width']}x{info['height']} ({info.get('orientation')})")
    image_url = ensure_url(a.image)
    if model == "2.5":
        inp = {"prompt": a.prompt, "first_frame_url": image_url, "aspect_ratio": "adaptive",
               "duration": q["duration_s"], "resolution": q["resolution"]}
    else:
        inp = {"prompt": a.prompt, "image_url": image_url, "resolution": "1080p",
               "duration": str(q["duration_s"]), "camera_fixed": bool(a.camera_fixed), "seed": int(a.seed)}
    if q.get("note"):
        log(f"  {q['note']}")
    log(f"Submitting video to {q['model_id']} ({q['duration_s']}s {q['resolution']}, ~{q['credits_est']} credits / ${q['usd_est']:.2f})")
    task_id = create_task(q["model_id"], inp)
    task = poll_task(task_id, every=15, timeout=1500)
    name = a.name or (still_local.stem if still_local else f"{dt.datetime.now():%Y%m%d-%H%M%S}-{slugify(a.prompt)}")
    finish("video", task, model, q["model_id"], a.prompt,
           {"duration_s": q["duration_s"], "resolution": q["resolution"], "camera_fixed": bool(a.camera_fixed),
            "seed": int(a.seed)},
           name, "mp4", q["credits_est"],
           {"input_image": str(still_local or a.image), "input_image_url": image_url, "preset": a.preset})


def cmd_status(a: argparse.Namespace) -> None:
    r = kie("GET", f"/api/v1/jobs/recordInfo?taskId={urllib.parse.quote(a.task_id)}")
    d = r.get("data") or {}
    res = d.get("response")
    if not res and d.get("resultJson"):
        try:
            res = json.loads(d["resultJson"])
        except json.JSONDecodeError:
            res = None
    out({"ok": r.get("code") == 200, "task_id": a.task_id, "state": d.get("state"), "result": res,
         "fail": {"code": d.get("failCode"), "msg": d.get("failMsg")} if d.get("state") == "fail" else None,
         "credits_consumed": d.get("creditsConsumed")})


def cmd_ledger(a: argparse.Namespace) -> None:
    res = {"ok": True, "ledger": str(ledger_path()), **spend_summary()}
    if a.all:
        res["entries"] = read_ledger()
    out(res)


def cmd_inspect(a: argparse.Namespace) -> None:
    p = Path(a.file).expanduser()
    res = inspect_file(p)
    if a.frame and p.suffix.lower() in {".mp4", ".mov", ".m4v"}:
        frame = extract_frame(p, p.with_suffix(".frame.jpg"), a.frame_at)
        res["frame"] = str(frame) if frame else None
        if not frame:
            res["frame_note"] = "ffmpeg not found on PATH; open the mp4 to check it visually"
    out({"ok": True, **res})


def main() -> None:
    load_env()
    ap = argparse.ArgumentParser(prog="kie.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("credits").set_defaults(fn=cmd_credits)

    p = sub.add_parser("quote")
    p.add_argument("--kind", choices=["image", "video"], required=True)
    p.add_argument("--model")
    p.add_argument("--duration", type=float)
    p.add_argument("--resolution", help="image: 1K/2K/4K; Seedance 2.5 video: 480p/720p/1080p")
    p.set_defaults(fn=cmd_quote)

    p = sub.add_parser("image")
    p.add_argument("--prompt", required=True)
    p.add_argument("--model", choices=["nano-banana-2", "gpt-image-2"])
    p.add_argument("--aspect", help="e.g. 9:16 (default), 16:9, 1:1, auto")
    p.add_argument("--resolution", choices=["1K", "2K", "4K"])
    p.add_argument("--format", choices=["png", "jpg"])
    p.add_argument("--ref", action="append", help="reference image URL or local path (repeatable)")
    p.add_argument("--name", help="output file stem")
    p.add_argument("--preset", help="preset name, recorded in the sidecar")
    p.add_argument("--override-cap", action="store_true")
    p.set_defaults(fn=cmd_image)

    p = sub.add_parser("upload")
    p.add_argument("--file", required=True)
    p.set_defaults(fn=cmd_upload)

    p = sub.add_parser("video")
    p.add_argument("--image", required=True, help="approved still: local path or public URL")
    p.add_argument("--prompt", required=True, help="short motion description")
    p.add_argument("--model", choices=["pro", "lite", "2.5"])
    p.add_argument("--duration", type=float)
    p.add_argument("--resolution", choices=["480p", "720p", "1080p"],
                   help="Seedance 2.5 only: 480p draft (28 cr/s), 720p (63), 1080p (158). Pro/Lite are always 1080p")
    p.add_argument("--camera-fixed", action="store_true")
    p.add_argument("--seed", type=int, default=-1, help="-1 for random; reuse a seed to stay close to a previous clip")
    p.add_argument("--name")
    p.add_argument("--preset")
    p.add_argument("--approve", action="store_true", help="user explicitly approved this paid run")
    p.add_argument("--override-cap", action="store_true")
    p.set_defaults(fn=cmd_video)

    p = sub.add_parser("status")
    p.add_argument("--task-id", required=True)
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("ledger")
    p.add_argument("--all", action="store_true", help="include every entry")
    p.set_defaults(fn=cmd_ledger)

    p = sub.add_parser("inspect")
    p.add_argument("--file", required=True)
    p.add_argument("--frame", action="store_true", help="also extract a frame (needs ffmpeg)")
    p.add_argument("--frame-at", type=float, default=1.0)
    p.set_defaults(fn=cmd_inspect)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
