"""
Vercel function for the /generate dashboard.

Same API contract as scripts/dashboard.py, but stateless: outputs and the
spend ledger live in a Vercel Blob store, generation runs on kie.ai, and the
browser polls /api/tasks?id=<kieTaskId> until a job finishes.

Environment variables (Vercel project settings):
  KIE_API_KEY             required, from https://kie.ai/api-key
  BLOB_READ_WRITE_TOKEN   injected when a Blob store is connected to the project
  SESSION_CAP_USD         default 10 (per calendar day, UTC)
  MONTHLY_CAP_USD         default 50

Routes (vercel.json rewrites /api/<op> to this file with ?op=<op>):
  GET  state | tasks?id=   POST quote | generate | finalize | item | upload | refresh-credits
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler

KIE = "https://api.kie.ai"
BLOB = "https://blob.vercel-storage.com"
USD_PER_CREDIT = 0.005

PRICING = {
    "image": {
        "nano-banana-2": {"id": "nano-banana-2", "credits": {"1K": 8, "2K": 10, "4K": 14}},
        "gpt-image-2": {"id": "gpt-image-2-text-to-image", "credits": {"1K": 10, "2K": 12, "4K": 16}},
    },
    "video": {
        "pro": {"id": "bytedance/v1-pro-image-to-video", "credits_per_second": 14, "durations": [5, 10],
                "resolutions": ["1080p"]},
        "lite": {"id": "bytedance/v1-lite-image-to-video", "credits_per_second": 10, "durations": [5, 10],
                 "resolutions": ["1080p"]},
        # kie.ai beta rates for Seedance 2.5 with image/text input, per generated second
        "2.5": {"id": "bytedance/seedance-2-5", "credits_per_second": {"480p": 28, "720p": 63, "1080p": 158},
                "durations": [4, 5, 6, 8, 10, 12, 15], "resolutions": ["480p", "720p", "1080p"],
                "default_resolution": "480p"},
        # renders an existing 480p draft at 1080p with the same motion; kie.ai publishes no separate rate
        "2.5-final": {"id": "bytedance/seedance-2-5-draft", "credits_per_second": 158},
    },
}


class ApiError(Exception):
    def __init__(self, msg: str, status: int = 400, **extra):
        super().__init__(msg)
        self.status, self.extra = status, extra


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def key_present() -> bool:
    k = os.environ.get("KIE_API_KEY", "").strip()
    return bool(k) and not k.startswith("your_")


def caps() -> tuple[float, float]:
    return float(os.environ.get("SESSION_CAP_USD", "10")), float(os.environ.get("MONTHLY_CAP_USD", "50"))


def slugify(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:limit].rstrip("-") or "gen"


def http(method: str, url: str, body=None, headers: dict | None = None, raw: bool = False, timeout: int = 60):
    if isinstance(body, (bytes, bytearray)):
        data = bytes(body)
    elif body is not None:
        data = json.dumps(body).encode("utf-8")
    else:
        data = None
    h = dict(headers or {})
    if data is not None and not isinstance(body, (bytes, bytearray)):
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = r.read()
    except urllib.error.HTTPError as e:
        raise ApiError(f"HTTP {e.code} from {url.split('?')[0]}: {e.read()[:300].decode('utf-8', 'replace')}", 502)
    except urllib.error.URLError as e:
        raise ApiError(f"network error calling {url.split('?')[0]}: {e.reason}", 502)
    if raw:
        return out
    try:
        return json.loads(out or b"{}")
    except json.JSONDecodeError:
        raise ApiError(f"non-JSON response from {url.split('?')[0]}", 502)


# ---------------------------------------------------------------------------
# kie.ai
# ---------------------------------------------------------------------------
def kie(method: str, path: str, body=None, timeout: int = 60):
    if not key_present():
        raise ApiError("KIE_API_KEY is not set in the Vercel project environment", 500)
    return http(method, KIE + path, body, {"Authorization": f"Bearer {os.environ['KIE_API_KEY'].strip()}"}, timeout=timeout)


def kie_credits() -> dict:
    try:
        r = kie("GET", "/api/v1/chat/credit", timeout=15)
        if r.get("code") != 200:
            return {"value": None, "usd": None, "error": r.get("msg") or "credit check failed"}
        v = r.get("data")
        if isinstance(v, dict):
            v = v.get("credits", v)
        return {"value": v, "usd": round(float(v) * USD_PER_CREDIT, 2), "error": None}
    except (ApiError, TypeError, ValueError) as e:
        return {"value": None, "usd": None, "error": str(e)}


# ---------------------------------------------------------------------------
# Vercel Blob (REST)
# ---------------------------------------------------------------------------
def blob_headers(extra: dict | None = None) -> dict:
    tok = os.environ.get("BLOB_READ_WRITE_TOKEN", "")
    if not tok:
        raise ApiError("BLOB_READ_WRITE_TOKEN is not set; connect a Blob store to this Vercel project", 500)
    h = {"authorization": f"Bearer {tok}", "x-api-version": "7"}
    h.update(extra or {})
    return h


def blob_put(pathname: str, data: bytes, content_type: str, max_age: int = 31536000) -> dict:
    return http("PUT", f"{BLOB}/{urllib.parse.quote(pathname)}", data,
                blob_headers({"x-content-type": content_type, "x-add-random-suffix": "0",
                              "x-allow-overwrite": "1", "x-cache-control-max-age": str(max_age)}), timeout=240)


def blob_list(prefix: str, limit: int = 1000) -> list[dict]:
    r = http("GET", f"{BLOB}/?prefix={urllib.parse.quote(prefix)}&limit={limit}", headers=blob_headers())
    return r.get("blobs", [])


def blob_delete(urls: list[str]) -> None:
    if urls:
        http("POST", f"{BLOB}/delete", {"urls": urls}, blob_headers())


def blob_read(url: str) -> bytes:
    sep = "&" if "?" in url else "?"
    return http("GET", f"{url}{sep}ts={int(time.time() * 1000)}", raw=True)


def blob_read_json(url: str):
    try:
        return json.loads(blob_read(url).decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


# ---------------------------------------------------------------------------
# ledger
# ---------------------------------------------------------------------------
def ledger_read() -> list[dict]:
    blobs = [b for b in blob_list("ledger.json", 5) if b.get("pathname") == "ledger.json"]
    if not blobs:
        return []
    data = blob_read_json(blobs[0]["url"])
    return data if isinstance(data, list) else []


def ledger_write(entries: list[dict]) -> None:
    blob_put("ledger.json", json.dumps(entries, ensure_ascii=False).encode("utf-8"), "application/json", max_age=60)


def ledger_append(entry: dict) -> None:
    entries = ledger_read()
    entries.append(entry)
    ledger_write(entries)


EXTRA_KEYS = ("group", "negative", "fields", "seed", "parent_id", "refs", "rating", "note", "hidden", "variant")


def update_item(p: dict) -> dict:
    item_id = str(p.get("id") or "")
    if not item_id:
        raise ApiError("id is required")
    entries = ledger_read()
    hit = False
    for e in entries:
        if e.get("task_id") == item_id:
            for k in ("rating", "note", "hidden"):
                if k in p:
                    e[k] = p[k]
            hit = True
    if not hit:
        raise ApiError("item not found", 404)
    ledger_write(entries)
    return {"ok": True}


def safe_name(name: str, default_ext: str) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", (name or "").strip()).strip("-.") or "upload"
    if "." not in base:
        base += "." + default_ext
    return base[:80]


def save_upload(p: dict) -> dict:
    import base64
    data = p.get("data") or ""
    if "," in data and data.strip().startswith("data:"):
        data = data.split(",", 1)[1]
    try:
        raw = base64.b64decode(data, validate=False)
    except Exception:  # noqa: BLE001
        raise ApiError("upload data must be base64")
    if not raw:
        raise ApiError("empty upload")
    if len(raw) > 4 * 1024 * 1024:
        raise ApiError("upload larger than 4 MB; the page should downscale it first")
    ext = "png" if raw[:8] == b"\x89PNG\r\n\x1a\n" else "jpg" if raw[:2] == b"\xff\xd8" else "png"
    name = safe_name(p.get("name") or "", ext)
    kind = p.get("kind") or "ref"
    folder = "refs" if kind == "ref" else "generations"
    stem, suffix = name.rsplit(".", 1)
    pathname = f"{folder}/{stem}-{int(time.time()) % 100000}.{suffix}"
    ctype = "image/png" if ext == "png" else "image/jpeg"
    stored = blob_put(pathname, raw, ctype)
    return {"ok": True, "name": pathname.rsplit("/", 1)[-1], "url": stored.get("url"), "media": inspect_bytes(raw, "image")}


def list_refs() -> list[dict]:
    out = []
    for b in blob_list("refs/", 200):
        out.append({"name": b.get("pathname", "").rsplit("/", 1)[-1], "url": b.get("url"), "uploaded_at": b.get("uploadedAt")})
    out.sort(key=lambda r: r.get("uploaded_at") or "", reverse=True)
    return out


def spend_summary(entries: list[dict]) -> dict:
    n = now()
    today, month = n.date().isoformat(), n.strftime("%Y-%m")
    t_usd = m_usd = 0.0
    t_n = m_n = 0
    for e in entries:
        ts = e.get("created_at", "")
        usd = float(e.get("usd", 0) or 0)
        if ts.startswith(month):
            m_usd += usd
            m_n += 1
        if ts.startswith(today):
            t_usd += usd
            t_n += 1
    s_cap, m_cap = caps()
    return {"today_usd": round(t_usd, 4), "today_runs": t_n, "session_cap_usd": s_cap,
            "month_usd": round(m_usd, 4), "month_runs": m_n, "monthly_cap_usd": m_cap,
            "remaining_today_usd": round(s_cap - t_usd, 4), "remaining_month_usd": round(m_cap - m_usd, 4)}


def daily_spend(entries: list[dict]) -> list[dict]:
    n = now()
    month = n.strftime("%Y-%m")
    days: dict[str, dict] = {}
    for e in entries:
        ts = e.get("created_at", "")
        if ts.startswith(month):
            d = days.setdefault(ts[:10], {"usd": 0.0, "runs": 0})
            d["usd"] += float(e.get("usd", 0) or 0)
            d["runs"] += 1
    return [{"date": f"{month}-{day:02d}", "usd": round(days.get(f"{month}-{day:02d}", {}).get("usd", 0.0), 4),
             "runs": days.get(f"{month}-{day:02d}", {}).get("runs", 0)} for day in range(1, n.day + 1)]


def enforce_caps(entries: list[dict], est_usd: float, override: bool) -> dict:
    s = spend_summary(entries)
    over = []
    if s["today_usd"] + est_usd > s["session_cap_usd"]:
        over.append(f"session cap ${s['session_cap_usd']:.2f} (spent today ${s['today_usd']:.2f}, run ${est_usd:.2f})")
    if s["month_usd"] + est_usd > s["monthly_cap_usd"]:
        over.append(f"monthly cap ${s['monthly_cap_usd']:.2f} (spent this month ${s['month_usd']:.2f}, run ${est_usd:.2f})")
    if over and not override:
        raise ApiError("This run would exceed the " + " and ".join(over) + ". Tick 'Override spend cap' to run anyway.",
                       200, cap_exceeded=True, spend=s)
    return s


# ---------------------------------------------------------------------------
# media inspection (bytes)
# ---------------------------------------------------------------------------
def image_size(b: bytes) -> tuple[int, int] | None:
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return struct.unpack(">II", b[16:24])
    if b[:2] == b"\xff\xd8":
        i = 2
        while i + 4 <= len(b):
            if b[i] != 0xFF:
                return None
            m = b[i + 1]
            if m == 0xD8 or 0xD0 <= m <= 0xD7 or m == 0x01:
                i += 2
                continue
            seg = struct.unpack(">H", b[i + 2:i + 4])[0]
            if 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):
                h, w = struct.unpack(">HH", b[i + 5:i + 9])
                return w, h
            i += 2 + seg
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        chunk = b[12:16]
        if chunk == b"VP8X":
            return 1 + int.from_bytes(b[24:27], "little"), 1 + int.from_bytes(b[27:30], "little")
        if chunk == b"VP8 ":
            w, h = struct.unpack("<HH", b[26:30])
            return w & 0x3FFF, h & 0x3FFF
        if chunk == b"VP8L":
            v = int.from_bytes(b[21:25], "little")
            return (v & 0x3FFF) + 1, ((v >> 14) & 0x3FFF) + 1
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


def mp4_info(buf: bytes) -> dict:
    info: dict = {}
    if buf[4:8] != b"ftyp":
        return info
    for kind, s, e in _boxes(buf, 0, len(buf)):
        if kind != b"moov":
            continue
        for k2, s2, e2 in _boxes(buf, s, e):
            if k2 == b"mvhd":
                v = buf[s2]
                ts, dur = struct.unpack(">IQ", buf[s2 + 20:s2 + 32]) if v == 1 else struct.unpack(">II", buf[s2 + 12:s2 + 20])
                if ts:
                    info["duration_s"] = round(dur / ts, 2)
            elif k2 == b"trak":
                for k3, s3, _ in _boxes(buf, s2, e2):
                    if k3 == b"tkhd":
                        off = s3 + (88 if buf[s3] == 1 else 76)
                        w, h = struct.unpack(">II", buf[off:off + 8])
                        w, h = w >> 16, h >> 16
                        if w and h and "width" not in info:
                            info["width"], info["height"] = w, h
    return info


def inspect_bytes(b: bytes, kind: str) -> dict:
    res: dict = {"bytes": len(b)}
    if kind == "image":
        sz = image_size(b)
        if sz:
            res["width"], res["height"] = sz
    else:
        res.update(mp4_info(b))
    if "width" in res:
        w, h = res["width"], res["height"]
        res["orientation"] = "vertical" if h > w else ("horizontal" if w > h else "square")
        res["is_1080x1920"] = (w, h) == (1080, 1920)
    return res


# ---------------------------------------------------------------------------
# quoting and tasks
# ---------------------------------------------------------------------------
def quote(p: dict) -> dict:
    kind = p.get("kind")
    if kind == "final":
        try:
            dur = int(round(float(p.get("duration") or 0)))
        except (TypeError, ValueError):
            dur = 0
        if dur <= 0:
            raise ApiError("duration is required to price a 1080p render")
        pr = PRICING["video"]["2.5-final"]
        c = int(pr["credits_per_second"] * dur)
        return {"kind": "final", "model": "2.5-final", "model_id": pr["id"], "duration_s": dur, "resolution": "1080p",
                "credits_est": c, "usd_est": round(c * USD_PER_CREDIT, 4)}
    if kind == "image":
        model = p.get("model") or "nano-banana-2"
        res = p.get("resolution") or "1K"
        pr = PRICING["image"].get(model)
        if not pr or res not in pr["credits"]:
            raise ApiError(f"unknown image model/resolution {model}/{res}")
        c = pr["credits"][res]
        return {"kind": "image", "model": model, "model_id": pr["id"], "resolution": res,
                "credits_est": c, "usd_est": round(c * USD_PER_CREDIT, 4)}
    if kind == "video":
        model = p.get("model") or "2.5"
        pr = PRICING["video"].get(model)
        if not pr or model == "2.5-final":
            raise ApiError(f"unknown video model {model}")
        if p.get("draft"):
            if model != "2.5":
                raise ApiError("draft mode is a Seedance 2.5 feature")
            p = {**p, "resolution": "480p"}
        try:
            req = float(p.get("duration") or 10)
        except (TypeError, ValueError):
            raise ApiError("duration must be a number")
        allowed = pr["durations"]
        dur = int(req) if req in allowed else min(allowed, key=lambda a: (abs(a - req), a))
        resolutions = pr.get("resolutions") or ["1080p"]
        res = p.get("resolution") if p.get("resolution") in resolutions else (pr.get("default_resolution") or resolutions[-1])
        rate = pr["credits_per_second"]
        if isinstance(rate, dict):
            rate = rate.get(res) or max(rate.values())
        c = int(rate * dur)
        q = {"kind": "video", "model": model, "model_id": pr["id"], "duration_s": dur, "resolution": res,
             "credits_est": c, "usd_est": round(c * USD_PER_CREDIT, 4)}
        if dur != req:
            q["note"] = f"{model} only supports {allowed}s; {req:g}s rounded to {dur}s"
        return q
    raise ApiError("kind must be image or video")


def task_path(task_id: str) -> str:
    return f"tasks/{task_id}.json"


def task_save(job: dict) -> None:
    blob_put(task_path(job["id"]), json.dumps(job, ensure_ascii=False).encode("utf-8"), "application/json", max_age=60)


def task_load(task_id: str) -> tuple[dict | None, str | None]:
    for b in blob_list(task_path(task_id), 5):
        if b.get("pathname") == task_path(task_id):
            return blob_read_json(b["url"]), b["url"]
    return None, None


def generate(p: dict) -> list[dict]:
    prompt = (p.get("prompt") or "").strip()
    negative = (p.get("negative") or "").strip()
    if not prompt:
        raise ApiError("prompt is required")
    q = quote(p)
    allow_multi = q["kind"] == "image" or (q["kind"] == "video" and p.get("draft"))
    count = max(1, min(4, int(p.get("count") or 1))) if allow_multi else 1
    entries = ledger_read()
    if q["kind"] == "video" and not p.get("approve"):
        raise ApiError("Paid video run needs approval. Tick 'I approve this paid run' and generate again.",
                       200, approval_required=True, quote=q, spend=spend_summary(entries))
    enforce_caps(entries, q["usd_est"] * count, bool(p.get("override_cap")))
    group = p.get("group") or uuid.uuid4().hex[:8]
    full_prompt = f"{prompt}. Avoid: {negative}" if negative else prompt
    try:
        seed = int(p.get("seed")) if p.get("seed") not in (None, "", -1, "-1") else -1
    except (TypeError, ValueError):
        seed = -1

    if q["kind"] == "image":
        refs = [str(r).strip() for r in (p.get("refs") or []) if str(r).strip()]
        for r in refs:
            if not re.match(r"^https?://", r):
                raise ApiError("reference images must be public URLs on the hosted dashboard")
        aspect = p.get("aspect") or "9:16"
        if q["model"] == "nano-banana-2":
            inp = {"prompt": full_prompt, "image_input": refs, "aspect_ratio": aspect,
                   "resolution": q["resolution"], "output_format": "png"}
        else:
            inp = {"prompt": full_prompt, "aspect_ratio": aspect, "resolution": q["resolution"]}
        params = {"aspect_ratio": aspect, "resolution": q["resolution"], "refs": refs}
        input_image = None
    else:
        refs = []
        image = (p.get("image") or "").strip()
        if not re.match(r"^https?://", image):
            raise ApiError("source still must be a public URL; click Animate on a gallery image")
        draft = bool(p.get("draft")) and q["model"] == "2.5"
        audio = p.get("audio") is not False
        if q["model"] == "2.5":
            inp = {"prompt": full_prompt, "first_frame_url": image, "aspect_ratio": "adaptive",
                   "duration": q["duration_s"], "resolution": q["resolution"], "generate_audio": audio,
                   "output_format": "mp4"}
            if draft:
                inp["draft"] = True
        else:
            inp = {"prompt": full_prompt, "image_url": image, "resolution": "1080p", "duration": str(q["duration_s"]),
                   "camera_fixed": bool(p.get("camera_fixed")), "seed": seed}
        params = {"duration_s": q["duration_s"], "resolution": q["resolution"], "camera_fixed": bool(p.get("camera_fixed")),
                  "seed": seed, "draft": draft, "generate_audio": audio}
        input_image = image

    jobs = []
    for i in range(count):
        r = kie("POST", "/api/v1/jobs/createTask", {"model": q["model_id"], "input": inp})
        task_id = (r.get("data") or {}).get("taskId")
        if r.get("code") != 200 or not task_id:
            if jobs:
                break  # keep the ones that were accepted
            raise ApiError(f"kie.ai createTask failed: {r.get('msg') or r}", 502)
        name = slugify(p.get("name") or prompt)
        if count > 1:
            name = f"{name}-v{i + 1}"
        job = {
            "id": task_id, "kind": q["kind"], "model": q["model"], "model_id": q["model_id"], "prompt": prompt,
            "params": params, "name": name, "preset": p.get("preset") or None,
            "input_image": input_image, "credits_est": q["credits_est"], "usd_est": q["usd_est"],
            "status": "running", "kie_state": "submitted", "started_at": time.time(),
            "created_at": now().isoformat(timespec="seconds"), "result": None,
            "group": group, "negative": negative or None, "fields": p.get("fields") or None,
            "seed": seed if (q["kind"] == "video" and seed != -1) else None,
            "parent_id": p.get("parent_id") or None, "refs": refs or None,
            "variant": i + 1 if count > 1 else None,
        }
        task_save(job)
        jobs.append(job)
    return jobs


def finalize(p: dict) -> dict:
    """Render a 480p Seedance 2.5 draft at 1080p. The draft task id stays valid for 7 days."""
    draft_id = str(p.get("id") or "").strip()
    if not draft_id:
        raise ApiError("draft id is required")
    entries = ledger_read()
    src = next((e for e in entries if e.get("task_id") == draft_id), None)
    if not src:
        raise ApiError("draft not found in the ledger", 404)
    if not ((src.get("params") or {}).get("draft")):
        raise ApiError("that clip is not a 480p draft; only drafts can be rendered at 1080p")
    if any(e.get("parent_id") == draft_id and e.get("model") == "2.5-final" for e in entries):
        raise ApiError("this draft already has a 1080p final")
    dur = (src.get("params") or {}).get("duration_s") or p.get("duration")
    q = quote({"kind": "final", "duration": dur})
    if not p.get("approve"):
        raise ApiError("Paid 1080p render needs approval.", 200, approval_required=True, quote=q, spend=spend_summary(entries))
    enforce_caps(entries, q["usd_est"], bool(p.get("override_cap")))
    r = kie("POST", "/api/v1/jobs/createTask",
            {"model": q["model_id"], "input": {"draft_task_id": draft_id, "resolution": "1080p", "output_format": "mp4"}})
    task_id = (r.get("data") or {}).get("taskId")
    if r.get("code") != 200 or not task_id:
        raise ApiError(f"kie.ai createTask failed: {r.get('msg') or r}", 502)
    base = (src.get("name") or "draft").rsplit(".", 1)[0]
    job = {
        "id": task_id, "kind": "video", "model": "2.5-final", "model_id": q["model_id"], "prompt": src.get("prompt") or "",
        "params": {"duration_s": q["duration_s"], "resolution": "1080p", "draft_task_id": draft_id, "draft": False,
                   "generate_audio": (src.get("params") or {}).get("generate_audio", True)},
        "name": f"{base}-1080p", "preset": src.get("preset"), "input_image": src.get("input_image"),
        "credits_est": q["credits_est"], "usd_est": q["usd_est"], "status": "running", "kie_state": "rendering 1080p",
        "started_at": time.time(), "created_at": now().isoformat(timespec="seconds"), "result": None,
        "group": src.get("group"), "negative": src.get("negative"), "fields": src.get("fields"), "seed": None,
        "parent_id": draft_id, "refs": None, "variant": None,
    }
    task_save(job)
    return job


def poll_task(task_id: str) -> dict:
    job, url = task_load(task_id)
    if not job:
        return {"ok": False, "gone": True, "error": "unknown or already finished task"}
    if job.get("status") != "running":
        return {"ok": True, "job": job}
    r = kie("GET", f"/api/v1/jobs/recordInfo?taskId={urllib.parse.quote(task_id)}")
    d = r.get("data") or {}
    state = d.get("state")
    if state in ("waiting", "queuing", "generating", None):
        job["kie_state"] = state or "waiting"
        return {"ok": True, "job": job}
    if state == "fail":
        job.update(status="failed", kie_state="fail", finished_at=time.time(),
                   result={"ok": False, "error": f"kie.ai task failed ({d.get('failCode')}): {d.get('failMsg')}. Failed tasks are not billed."})
        blob_delete([url])
        return {"ok": True, "job": job}

    # success: guard against two pollers finalizing at once
    if job.get("finalizing_at") and time.time() - job["finalizing_at"] < 300:
        job["kie_state"] = "saving"
        return {"ok": True, "job": job}
    job["finalizing_at"] = time.time()
    task_save(job)

    res = d.get("response")
    if not res:
        try:
            res = json.loads(d.get("resultJson") or "{}")
        except json.JSONDecodeError:
            res = {}
    urls = res.get("resultUrls") or []
    if not urls:
        raise ApiError("task succeeded but returned no resultUrls", 502)
    src = urls[0]
    ext = (urllib.parse.urlparse(src).path.rsplit(".", 1)[-1] or "").lower()
    if ext not in {"png", "jpg", "jpeg", "webp", "mp4", "mov", "webm"}:
        ext = "png" if job["kind"] == "image" else "mp4"
    data = http("GET", src, raw=True, timeout=240)
    pathname = f"generations/{job['name']}-{task_id[-6:]}.{ext}"
    ctype = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp",
             "mp4": "video/mp4", "mov": "video/quicktime", "webm": "video/webm"}[ext]
    stored = blob_put(pathname, data, ctype)
    media = inspect_bytes(data, job["kind"])
    consumed = d.get("creditsConsumed")
    credits = float(consumed) if consumed is not None else float(job["credits_est"])
    entry = {
        "created_at": now().isoformat(timespec="seconds"), "kind": job["kind"], "model": job["model"],
        "model_id": job["model_id"], "task_id": task_id, "prompt": job["prompt"], "params": job["params"],
        "name": pathname.rsplit("/", 1)[-1], "file": pathname, "url": stored.get("url"), "source_url": src,
        "credits_est": job["credits_est"], "credits_consumed": consumed, "usd": round(credits * USD_PER_CREDIT, 4),
        "preset": job.get("preset"), "input_image": job.get("input_image"), "media": media,
        "rating": None, "note": None, "hidden": False,
    }
    for k in ("group", "negative", "fields", "seed", "parent_id", "refs", "variant"):
        entry[k] = job.get(k)
    ledger_append(entry)
    blob_delete([url])
    job.update(status="done", kie_state="success", finished_at=time.time(),
               result={"ok": True, "url": entry["url"], "file": entry["name"], "media": media,
                       "credits_consumed": consumed, "usd": entry["usd"]})
    return {"ok": True, "job": job}


def state() -> dict:
    setup_error = None
    entries: list[dict] = []
    jobs: list[dict] = []
    try:
        entries = ledger_read()
        for b in blob_list("tasks/", 100):
            j = blob_read_json(b["url"])
            if isinstance(j, dict) and j.get("status") == "running":
                jobs.append(j)
    except ApiError as e:
        setup_error = str(e)
    gens = []
    for e in reversed(entries):
        if e.get("url"):
            item = {"id": e.get("task_id"), "name": e.get("name"), "url": e["url"], "kind": e.get("kind"),
                    "prompt": e.get("prompt"), "model": e.get("model"), "model_id": e.get("model_id"),
                    "usd": e.get("usd"), "credits_consumed": e.get("credits_consumed"),
                    "created_at": e.get("created_at"), "params": e.get("params"), "preset": e.get("preset"),
                    "input_image": e.get("input_image"), "task_id": e.get("task_id"), "media": e.get("media")}
            for k in EXTRA_KEYS:
                item[k] = e.get(k)
            gens.append(item)
    refs: list[dict] = []
    if not setup_error:
        try:
            refs = list_refs()
        except ApiError:
            refs = []
    return {
        "ok": True, "mode": "vercel", "now": now().isoformat(timespec="seconds"),
        "key_present": key_present(), "setup_error": setup_error,
        "credits": kie_credits() if key_present() else {"value": None, "usd": None, "error": "KIE_API_KEY is not set"},
        "usd_per_credit": USD_PER_CREDIT, "spend": spend_summary(entries), "daily": daily_spend(entries),
        "generations": gens, "refs": refs, "pricing": PRICING, "jobs": jobs,
    }


# ---------------------------------------------------------------------------
# handler
# ---------------------------------------------------------------------------
class handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # keep function logs quiet
        pass

    def _send(self, obj: dict, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _op(self) -> tuple[str, dict]:
        u = urllib.parse.urlparse(self.path)
        qs = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        op = qs.get("op")
        if not op:
            m = re.match(r"^/api/([a-z-]+)", u.path)
            op = m.group(1) if m else ""
        return op, qs

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            b = json.loads(raw.decode("utf-8") or "{}")
            return b if isinstance(b, dict) else {}
        except json.JSONDecodeError:
            return {}

    def _run(self, fn) -> None:
        try:
            self._send(fn())
        except ApiError as e:
            self._send({"ok": False, "error": str(e), **e.extra}, e.status)
        except Exception as e:  # noqa: BLE001
            self._send({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    def do_GET(self) -> None:
        op, qs = self._op()
        if op == "state":
            return self._run(state)
        if op == "tasks":
            return self._run(lambda: poll_task(qs.get("id", "")))
        self._send({"ok": False, "error": f"unknown operation '{op}'"}, 404)

    def do_POST(self) -> None:
        op, _ = self._op()
        body = self._body()
        if op == "quote":
            def _quote():
                q = quote(body)
                allow_multi = q["kind"] == "image" or (q["kind"] == "video" and body.get("draft"))
                count = max(1, min(4, int(body.get("count") or 1))) if allow_multi else 1
                if count > 1:
                    q.update(count=count, credits_est_total=q["credits_est"] * count,
                             usd_est_total=round(q["usd_est"] * count, 4))
                return {"ok": True, **q, "spend": spend_summary(ledger_read())}
            return self._run(_quote)
        if op == "generate":
            def _gen():
                jobs = generate(body)
                return {"ok": True, "jobs": jobs, "job": jobs[0]}
            return self._run(_gen)
        if op == "finalize":
            def _fin():
                job = finalize(body)
                return {"ok": True, "jobs": [job], "job": job}
            return self._run(_fin)
        if op == "item":
            return self._run(lambda: update_item(body))
        if op == "upload":
            return self._run(lambda: save_upload(body))
        if op == "refresh-credits":
            return self._run(lambda: {"ok": True, "credits": kie_credits()})
        self._send({"ok": False, "error": f"unknown operation '{op}'"}, 404)
