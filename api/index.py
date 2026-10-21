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
  GET  state | tasks?id=   POST quote | generate | finalize | item | upload | character | refresh-credits
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
                # with a reference video kie.ai bills input + output seconds at this lower rate
                "credits_per_second_with_video": {"480p": 17, "720p": 38, "1080p": 95},
                "durations": [4, 5, 6, 8, 10, 12, 15], "resolutions": ["480p", "720p", "1080p"],
                "default_resolution": "480p"},
        # renders an existing 480p draft at 1080p with the same motion; kie.ai publishes no separate rate
        "2.5-final": {"id": "bytedance/seedance-2-5-draft", "credits_per_second": 158},
        # Kling 3.0: start/end frames and up to 3 characters in one request; std = 720p, pro = 1080p
        "kling-3.0": {"id": "kling-3.0/video", "credits_per_second": {"720p": 14, "1080p": 18, "4K": 67},
                      "credits_per_second_with_sound": {"720p": 20, "1080p": 27, "4K": 67},
                      "durations": list(range(3, 16)), "resolutions": ["720p", "1080p", "4K"],
                      "default_resolution": "720p", "modes": {"720p": "std", "1080p": "pro", "4K": "4K"},
                      "aspects": ["9:16", "16:9", "1:1"], "max_characters": 3},
        # Wan 3.0: Seedance's three modes at about a fifth of the price; references are Image1.., Video1 (no '@').
        # With a reference clip kie.ai bills input + output seconds at the same rate.
        "wan-3.0": {"id": "wan/3-0-video", "credits_per_second": {"480p": 8, "720p": 16, "1080p": 32},
                    "bills_input_video": True, "durations": list(range(2, 31)),
                    "resolutions": ["480p", "720p", "1080p"], "default_resolution": "720p",
                    "aspects": ["9:16", "16:9", "1:1", "4:3", "3:4"], "max_refs": 10,
                    "min_ref_video_s": 1, "max_ref_video_s": 15, "max_total_s": 30},
        # MiniMax H3: animates a start frame or a start + end frame; sound is always on
        "minimax-h3": {"id": "minimax-h3/image-to-video", "credits_per_second": {"768p": 8, "2K": 13},
                       "durations": list(range(4, 16)), "resolutions": ["768p", "2K"], "default_resolution": "2K"},
    },
}
KLING = "kling-3.0"
WAN = "wan-3.0"
H3 = "minimax-h3"


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


# Operation budget (Hobby: 2,000 advanced ops = put/list/copy, 10,000 simple ops = uncached reads per month).
# This module never calls list(). All state lives in one "index.json" read by its public URL
# (one simple op) and written with one put per change.
def blob_base() -> str:
    base = os.environ.get("BLOB_BASE_URL", "").strip().rstrip("/")
    if base:
        return base
    parts = os.environ.get("BLOB_READ_WRITE_TOKEN", "").split("_")
    if len(parts) >= 5 and parts[0] == "vercel" and parts[1] == "blob":
        return f"https://{parts[3].lower()}.public.blob.vercel-storage.com"
    raise ApiError("cannot work out the Blob store URL; set BLOB_BASE_URL or connect a Blob store", 500)


def blob_get_json(pathname: str):
    """Read a JSON blob by URL. Returns None when it does not exist."""
    try:
        raw = http("GET", f"{blob_base()}/{pathname}?ts={int(time.time() * 1000)}", raw=True)
    except ApiError as e:
        if "HTTP 404" in str(e):
            return None
        raise
    try:
        return json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


# ---------------------------------------------------------------------------
# ledger
# ---------------------------------------------------------------------------
def dedupe(entries: list[dict]) -> list[dict]:
    """One ledger row per kie task. A second finalize of the same task must never double-count spend."""
    seen: set = set()
    out = []
    for e in entries:
        key = e.get("task_id") or id(e)
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


INDEX = {"data": None, "at": 0.0}
INDEX_TTL = 20  # seconds a warm instance trusts its copy for reads


def index_read(force: bool = False) -> dict:
    if not force and INDEX["data"] is not None and time.time() - INDEX["at"] < INDEX_TTL:
        return INDEX["data"]
    data = blob_get_json("index.json")
    if not isinstance(data, dict):
        legacy = blob_get_json("ledger.json")  # one-time migration from the old layout
        data = {"ledger": legacy if isinstance(legacy, list) else []}
    data.setdefault("ledger", [])
    data.setdefault("tasks", {})
    data.setdefault("refs", [])
    data.setdefault("characters", [])
    data["ledger"] = dedupe(data["ledger"])
    INDEX.update(data=data, at=time.time())
    return data


def index_mutate(fn) -> dict:
    """Fresh read, apply fn(data), one put. fn may raise to abort without writing."""
    data = index_read(force=True)
    fn(data)
    data["ledger"] = dedupe(data["ledger"])
    blob_put("index.json", json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json", max_age=60)
    INDEX.update(data=data, at=time.time())
    return data


def ledger_read() -> list[dict]:
    return index_read()["ledger"]


FINALIZING: set = set()  # per warm instance, blocks concurrent finalization of one task


EXTRA_KEYS = ("group", "negative", "fields", "seed", "parent_id", "refs", "rating", "note", "hidden", "variant",
              "characters", "end_image", "ref_video", "last_frame_url", "sent_prompt")


def update_item(p: dict) -> dict:
    item_id = str(p.get("id") or "")
    if not item_id:
        raise ApiError("id is required")
    def patch(d: dict) -> None:
        hit = False
        for e in d["ledger"]:
            if e.get("task_id") == item_id:
                for k in ("rating", "note", "hidden"):
                    if k in p:
                        e[k] = p[k]
                hit = True
        if not hit:
            raise ApiError("item not found", 404)
    index_mutate(patch)
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
    name = pathname.rsplit("/", 1)[-1]
    if kind == "ref":
        rec = {"name": name, "url": stored.get("url"), "uploaded_at": now().isoformat(timespec="seconds")}
        index_mutate(lambda d: d["refs"].insert(0, rec))
    return {"ok": True, "name": name, "url": stored.get("url"), "media": inspect_bytes(raw, "image")}


# ---------------------------------------------------------------------------
# characters: a name, an identity line and 1-8 reference images, reused across generations
# ---------------------------------------------------------------------------
MAX_CHAR_IMAGES = 4  # sent per character; the first images hold identity best
MAX_PHOTOS = 8  # stored per character
MAX_REFS = {"nano-banana-2": 14, "seedance": 30, WAN: 10}  # reference images kie.ai accepts per request


def save_character(p: dict) -> dict:
    cid = str(p.get("id") or "").strip()
    if p.get("delete"):
        def drop(d: dict) -> None:
            kept = [c for c in d["characters"] if c.get("id") != cid]
            if len(kept) == len(d["characters"]):
                raise ApiError("character not found", 404)
            d["characters"] = kept
        index_mutate(drop)
        return {"ok": True, "deleted": cid}
    name = (p.get("name") or "").strip()
    images = [str(i).strip() for i in (p.get("images") or []) if str(i).strip()]
    if not name:
        raise ApiError("a character needs a name")
    if not images:
        raise ApiError("a character needs at least one reference image")
    if any(not re.match(r"^https?://", i) for i in images):
        raise ApiError("character images must be public URLs on the hosted dashboard")
    if len(images) > MAX_PHOTOS:
        raise ApiError(f"a character holds up to {MAX_PHOTOS} photos; remove one first")
    out: dict = {}
    def upsert(d: dict) -> None:
        stamp = now().isoformat(timespec="seconds")
        hit = next((c for c in d["characters"] if c.get("id") == cid), None) if cid else None
        if hit is None:
            tag, taken, i = slugify(name, 24), {c.get("tag") for c in d["characters"]}, 2
            base = tag
            while tag in taken:
                tag, i = f"{base}-{i}", i + 1
            # ids are never reused, so old generations never attach to a new character with the same name
            hit = {"id": f"{tag}-{uuid.uuid4().hex[:6]}", "tag": tag, "created_at": stamp}
            d["characters"].append(hit)
        hit.update(name=name, description=(p.get("description") or "").strip(), images=images, updated_at=stamp)
        out.update(hit)
    index_mutate(upsert)
    return {"ok": True, "character": out}


def characters_by_id(ids: list) -> list[dict]:
    # a fresh read when characters are used, so a paid run never sends photos removed seconds ago
    known = {c.get("id"): c for c in index_read(force=bool(ids))["characters"]}
    chars = []
    for i in ids or []:
        c = known.get(str(i))
        if not c:
            raise ApiError(f"unknown character '{i}'")
        chars.append(c)
    return chars


def photos_per_character(chars: list[dict], n_refs: int, cap: int) -> int:
    """How many photos each character can send so characters + extra refs fit under the model's cap."""
    if n_refs > cap:
        raise ApiError(f"too many reference images: {n_refs}, the model takes {cap}")
    if not chars:
        return MAX_CHAR_IMAGES
    sent = lambda k: sum(min(len(c.get("images") or []), k) for c in chars) + n_refs
    per = next((k for k in range(MAX_CHAR_IMAGES, 0, -1) if sent(k) <= cap), 0)  # trim only what doesn't fit
    if per < 1:
        raise ApiError(f"too many references: {len(chars)} characters plus {n_refs} images, the model takes {cap}")
    return per


def ref_names(prompt: str, at: str) -> str:
    """Same as scripts/kie.py: Seedance reads '@Image1' / '@Video1', Wan reads 'Image1' / 'Video1'."""
    if at:
        return re.sub(r"(?<![@\w])(Image|Video)(\d+)\b", r"@\1\2", prompt)
    return re.sub(r"@(Image|Video)(\d+)\b", r"\1\2", prompt)


def character_images(chars: list[dict], per: int = MAX_CHAR_IMAGES) -> list[str]:
    out = []
    for c in chars:
        out += [str(i) for i in (c.get("images") or [])[:per]]
    return out


def cast_prompt(prompt: str, chars: list[dict], first_index: int, video_tags: bool,
                per: int = MAX_CHAR_IMAGES, at: str = "@") -> str:
    """Same logic as scripts/kie.py: bind each character to its images and lock identity (Wan: at="")."""
    if not chars:
        return prompt
    lines, idx = [], first_index
    for c in chars:
        n = min(len(c.get("images") or []), per)
        nums = list(range(idx, idx + n))
        label = " ".join(f"{at}Image{i}" for i in nums) if video_tags else \
            ("reference image " if n == 1 else "reference images ") + ", ".join(str(i) for i in nums)
        name = c.get("name") or c.get("tag") or "character"
        if c.get("tag"):
            # @anna must not match inside @anna-2; a lambda keeps backslashes in names literal
            prompt = re.sub("@" + re.escape(c["tag"]) + r"(?![\w-])", lambda _m: name, prompt, flags=re.IGNORECASE)
        desc = (c.get("description") or "").strip().rstrip(".")
        lines.append(f"{name} is shown in {label}" + (f": {desc}." if desc else "."))
        idx += n
    body = prompt.strip()
    if body and body[-1] not in ".!?":
        body += "."
    lock = "Keep every character's face, hair, skin tone, build and outfit identical to their reference images."
    return " ".join(lines) + " " + body + " " + lock


def kling_elements(prompt: str, chars: list[dict]) -> tuple[str, list[dict]]:
    """Same logic as scripts/kie.py: saved characters become Kling 3.0 elements addressed as @name."""
    if len(chars) > PRICING["video"][KLING]["max_characters"]:
        raise ApiError(f"Kling 3.0 takes up to {PRICING['video'][KLING]['max_characters']} characters per clip")
    elements, missing, used = [], [], set()
    for c in chars:
        label = c.get("name") or c.get("tag") or "character"
        imgs = [str(i) for i in (c.get("images") or [])][:MAX_CHAR_IMAGES]
        if len(imgs) < 2:
            raise ApiError(f"Kling 3.0 needs at least 2 photos of {label}; add another in Cast")
        name = re.sub(r"\W+", "_", str(c.get("tag") or label)).strip("_").lower() or "character"
        while name in used:
            name += "_2"
        used.add(name)
        pat = "@" + re.escape(c.get("tag") or "") + r"(?![\w-])"
        if c.get("tag") and re.search(pat, prompt, flags=re.IGNORECASE):
            prompt = re.sub(pat, lambda _m: "@" + name, prompt, flags=re.IGNORECASE)
        else:
            missing.append("@" + name)
        desc = (c.get("description") or "").strip().rstrip(".")
        elements.append({"name": name, "description": f"{label}: {desc}" if desc else label,
                         "element_input_urls": imgs})
    if missing:
        body = prompt.strip()
        if body and body[-1] not in ".!?":
            body += "."
        prompt = f"{body} Featuring {', '.join(missing)}."
    return prompt, elements


def list_refs() -> list[dict]:
    return index_read()["refs"][:200]


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
        ref_s = 0.0
        if p.get("mode") == "reference" and p.get("ref_video"):
            # kie.ai bills the clip's seconds too; never fall back to the output-only price
            try:
                ref_s = float(p.get("ref_video_s") or 0)
            except (TypeError, ValueError):
                ref_s = 0.0
            lo, cap = pr.get("min_ref_video_s") or 0, pr.get("max_ref_video_s") or 30
            if not (0 < ref_s and lo <= ref_s <= cap):
                rng = f"{lo:g}-{cap} s range" if lo else f"{cap} s limit"
                raise ApiError(f"reference clip length is unknown or outside {model}'s {rng}; pass ref_video_s")
        try:
            req = float(p.get("duration") or 10)
        except (TypeError, ValueError):
            raise ApiError("duration must be a number")
        allowed = pr["durations"]
        dur = int(req) if req in allowed else min(allowed, key=lambda a: (abs(a - req), a))
        resolutions = pr.get("resolutions") or ["1080p"]
        if p.get("resolution") and p.get("resolution") not in resolutions:
            raise ApiError(f"{model} renders {', '.join(resolutions)}, not {p.get('resolution')}")
        res = p.get("resolution") or pr.get("default_resolution") or resolutions[-1]
        rate, billed = pr["credits_per_second"], dur
        if p.get("audio") is not False and pr.get("credits_per_second_with_sound"):
            rate = pr["credits_per_second_with_sound"]  # Kling charges more with sound
        if ref_s and (pr.get("credits_per_second_with_video") or pr.get("bills_input_video")):
            # Seedance bills input + output seconds at a lower rate, Wan at the same rate
            rate, billed = pr.get("credits_per_second_with_video") or rate, dur + ref_s
        if ref_s and pr.get("max_total_s") and dur + ref_s > pr["max_total_s"]:
            raise ApiError(f"{model} caps the reference clip plus the new part at {pr['max_total_s']} s; "
                           f"ask for {int(pr['max_total_s'] - ref_s)} s or less")
        if isinstance(rate, dict):
            rate = rate.get(res) or max(rate.values())
        c = int(round(rate * billed))
        q = {"kind": "video", "model": model, "model_id": pr["id"], "duration_s": dur, "resolution": res,
             "credits_est": c, "usd_est": round(c * USD_PER_CREDIT, 4)}
        if ref_s:
            q["ref_video_s"] = round(ref_s, 2)
        if dur != req:
            q["note"] = f"{model} only supports {allowed}s; {req:g}s rounded to {dur}s"
        return q
    raise ApiError("kind must be image or video")


def tasks_add(new_jobs: list[dict]) -> None:
    def add(d: dict) -> None:
        for j in new_jobs:
            d["tasks"][j["id"]] = j
    index_mutate(add)


def task_load(task_id: str) -> dict | None:
    return index_read(force=True)["tasks"].get(task_id)


def task_remove(task_id: str) -> None:
    index_mutate(lambda d: d["tasks"].pop(task_id, None))


def generate(p: dict) -> list[dict]:
    prompt = (p.get("prompt") or "").strip()
    negative = (p.get("negative") or "").strip()
    if not prompt:
        raise ApiError("prompt is required")
    chars = characters_by_id(p.get("characters") or [])
    if chars and p.get("kind") == "image" and (p.get("model") or "nano-banana-2") != "nano-banana-2":
        p = {**p, "model": "nano-banana-2"}  # characters need reference images
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

    sent_prompt = None
    end_image = ref_video = None
    if q["kind"] == "image":
        refs = [str(r).strip() for r in (p.get("refs") or []) if str(r).strip()]
        for r in refs:
            if not re.match(r"^https?://", r):
                raise ApiError("reference images must be public URLs on the hosted dashboard")
        per = photos_per_character(chars, len(refs), MAX_REFS["nano-banana-2"])
        sent_refs = character_images(chars, per) + refs  # job["refs"] keeps only the user's own refs
        if chars:
            full_prompt = sent_prompt = cast_prompt(full_prompt, chars, 1, video_tags=False, per=per)
        aspect = p.get("aspect") or "9:16"
        if q["model"] == "nano-banana-2":
            inp = {"prompt": full_prompt, "image_input": sent_refs, "aspect_ratio": aspect,
                   "resolution": q["resolution"], "output_format": "png"}
        else:
            inp = {"prompt": full_prompt, "aspect_ratio": aspect, "resolution": q["resolution"]}
        params = {"aspect_ratio": aspect, "resolution": q["resolution"], "refs": refs}
        input_image = None
    else:
        mode = p.get("mode") or "first"
        kling, wan, h3 = q["model"] == KLING, q["model"] == WAN, q["model"] == H3
        if mode not in ("first", "first-last", "reference"):
            raise ApiError("mode must be first, first-last or reference")
        if mode != "first" and q["model"] not in ("2.5", WAN, KLING, H3):
            raise ApiError("start + end frames and references need Seedance 2.5, Wan 3.0, Kling 3.0 or MiniMax H3")
        if mode == "reference" and h3:
            raise ApiError("MiniMax H3 animates frames only; use Wan 3.0 or Kling 3.0 for Cast mode")
        is_url = lambda u: bool(re.match(r"^https?://", u or ""))
        refs = [str(r).strip() for r in (p.get("refs") or []) if str(r).strip()] if mode == "reference" else []
        if kling and (refs or (p.get("ref_video") or "").strip()):
            raise ApiError("Kling 3.0 takes characters, not loose reference images or clips")
        if h3 and chars:
            raise ApiError("MiniMax H3 takes no characters. Put the cast into the start and end stills, or use Kling 3.0.")
        image = (p.get("image") or "").strip() if mode != "reference" else ""
        if mode != "reference" and not is_url(image):
            raise ApiError("start frame must be a public URL; click Animate on a gallery image")
        if mode == "first-last":
            end_image = (p.get("end_image") or "").strip()
            if not is_url(end_image):
                raise ApiError("end frame must be a public URL; use 'Use as end frame' on a gallery image")
        elements = []
        if kling:
            if mode == "reference" and not chars:
                raise ApiError("Cast mode needs at least one character")
            if mode == "reference" and (p.get("aspect") or "9:16") not in PRICING["video"][KLING]["aspects"]:
                raise ApiError("Kling 3.0 takes 9:16, 16:9 or 1:1")
            if chars:
                before = full_prompt
                full_prompt, elements = kling_elements(full_prompt, chars)
                if any(not is_url(u) for e in elements for u in e["element_input_urls"]):
                    raise ApiError("character photos must be public URLs on the hosted dashboard")
                sent_prompt = full_prompt if full_prompt != before else None
        elif mode == "reference":
            ref_video = (p.get("ref_video") or "").strip() or None
            if any(not is_url(r) for r in refs + ([ref_video] if ref_video else [])):
                raise ApiError("references must be public URLs on the hosted dashboard")
            if wan and (p.get("aspect") or "9:16") not in PRICING["video"][WAN]["aspects"]:
                raise ApiError("Wan 3.0 takes 9:16, 16:9, 1:1, 4:3 or 3:4")
            per = photos_per_character(chars, len(refs), MAX_REFS[WAN if wan else "seedance"])
            sent_refs = character_images(chars, per) + refs
            if not sent_refs and not ref_video:
                raise ApiError("reference mode needs at least one character, reference image or reference clip")
            before = full_prompt
            full_prompt = ref_names(cast_prompt(full_prompt, chars, 1, video_tags=True, per=per,
                                                at="" if wan else "@"), "" if wan else "@")
            sent_prompt = full_prompt if full_prompt != before else None
        elif chars:
            raise ApiError(f"{'Wan 3.0' if wan else 'Seedance'} cannot mix characters with a start frame. Lock the "
                           "character into the start and end stills instead, switch to Cast mode, or use Kling 3.0.")
        draft = bool(p.get("draft")) and q["model"] == "2.5"
        audio = h3 or p.get("audio") is not False  # MiniMax H3 always renders sound
        if q["model"] == "2.5":
            inp = {"prompt": full_prompt, "duration": q["duration_s"], "resolution": q["resolution"],
                   "generate_audio": audio, "output_format": "mp4", "return_last_frame": True}
            if mode == "reference":
                inp["aspect_ratio"] = p.get("aspect") or "9:16"
                if sent_refs:
                    inp["reference_image_urls"] = sent_refs
                if ref_video:
                    inp["reference_video_urls"] = [ref_video]
            else:
                inp.update(first_frame_url=image, aspect_ratio="adaptive")
                if end_image:
                    inp["last_frame_url"] = end_image
            if draft:
                inp["draft"] = True
        elif kling:
            inp = {"prompt": full_prompt, "sound": audio, "duration": str(q["duration_s"]),
                   "mode": PRICING["video"][KLING]["modes"][q["resolution"]], "multi_shots": False}
            if mode == "reference":
                inp["aspect_ratio"] = p.get("aspect") or "9:16"
            else:
                inp["image_urls"] = [image] + ([end_image] if end_image else [])
            if elements:
                inp["kling_elements"] = elements
        elif wan:
            inp = {"prompt": full_prompt, "duration": q["duration_s"], "resolution": q["resolution"].upper(), "audio": audio}
            if mode == "reference":
                inp["aspect_ratio"] = p.get("aspect") or "9:16"
                if sent_refs:
                    inp["reference_image_urls"] = sent_refs
                if ref_video:
                    inp["reference_video_urls"] = [ref_video]
            else:
                inp.update(first_frame_url=image, aspect_ratio="adaptive")
                if end_image:
                    inp["last_frame_url"] = end_image
            if seed != -1:
                inp["seed"] = seed
        elif h3:
            inp = {"prompt": full_prompt, "duration": q["duration_s"], "resolution": q["resolution"].upper(),
                   "first_frame_url": image}
            if end_image:
                inp["last_frame_url"] = end_image
        else:
            inp = {"prompt": full_prompt, "image_url": image, "resolution": "1080p", "duration": str(q["duration_s"]),
                   "camera_fixed": bool(p.get("camera_fixed")), "seed": seed}
        params = {"duration_s": q["duration_s"], "resolution": q["resolution"], "camera_fixed": bool(p.get("camera_fixed")),
                  "seed": seed, "draft": draft, "generate_audio": audio, "mode": mode,
                  "ref_video_s": q.get("ref_video_s"), "aspect_ratio": inp.get("aspect_ratio")}
        input_image = image or None

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
            "characters": [c.get("id") for c in chars] or None, "end_image": end_image, "ref_video": ref_video,
            "sent_prompt": sent_prompt,
        }
        jobs.append(job)
    tasks_add(jobs)  # one write for the whole batch
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
                   "generate_audio": (src.get("params") or {}).get("generate_audio", True),
                   **{k: (src.get("params") or {}).get(k) for k in ("mode", "aspect_ratio", "ref_video_s")}},
        "name": f"{base}-1080p", "preset": src.get("preset"), "input_image": src.get("input_image"),
        "credits_est": q["credits_est"], "usd_est": q["usd_est"], "status": "running", "kie_state": "rendering 1080p",
        "started_at": time.time(), "created_at": now().isoformat(timespec="seconds"), "result": None,
        "group": src.get("group"), "negative": src.get("negative"), "fields": src.get("fields"), "seed": None,
        "parent_id": draft_id, "refs": src.get("refs"), "variant": None,
        **{k: src.get(k) for k in ("characters", "end_image", "ref_video", "sent_prompt")},
    }
    tasks_add([job])
    return job


def split_result(res: dict, urls: list, kind: str) -> tuple:
    """Main output plus the Seedance 2.5 last frame (return_last_frame). Same logic as scripts/kie.py."""
    ext = lambda u: (urllib.parse.urlparse(u).path.rsplit(".", 1)[-1] or "").lower()
    if kind != "video":
        return urls[0], None
    vids = [u for u in urls if ext(u) in {"mp4", "mov", "webm"}]
    src = vids[0] if vids else urls[0]
    last = None
    for k, v in res.items():
        if "last" in k.lower() and "frame" in k.lower():
            # kie.ai documents Seedance 2.x as {resultUrls: [], lastFrameUrl: []}: a list, not a string
            v = next((x for x in v if isinstance(x, str)), None) if isinstance(v, list) else v
            if isinstance(v, str) and v.startswith("http"):
                last = v
    if not last:
        imgs = [u for u in urls if u != src and ext(u) in {"png", "jpg", "jpeg", "webp"}]
        last = imgs[0] if imgs else None
    return src, last


def poll_task(task_id: str) -> dict:
    job = task_load(task_id)
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
        task_remove(task_id)
        return {"ok": True, "job": job}

    # success: guard against two pollers finalizing at once (in-memory for a warm instance, index flag otherwise)
    if task_id in FINALIZING or (job.get("finalizing_at") and time.time() - job["finalizing_at"] < 300):
        job["kie_state"] = "saving"
        return {"ok": True, "job": job}
    if any(e.get("task_id") == task_id for e in index_read()["ledger"]):
        task_remove(task_id)
        return {"ok": False, "gone": True, "error": "already saved"}
    FINALIZING.add(task_id)  # the ledger dedupe makes a rare double save harmless
    try:
        return save_result(task_id, job, d)
    finally:
        FINALIZING.discard(task_id)  # always released, whatever fails while saving


def save_result(task_id: str, job: dict, d: dict) -> dict:
    """Copy a finished kie.ai result into Blob and append the ledger row."""
    res = d.get("response")
    if not res:
        try:
            res = json.loads(d.get("resultJson") or "{}")
        except json.JSONDecodeError:
            res = {}
    urls = res.get("resultUrls") or []
    if not urls:
        raise ApiError("task succeeded but returned no resultUrls", 502)
    src, last_src = split_result(res, urls, job["kind"])
    ext = (urllib.parse.urlparse(src).path.rsplit(".", 1)[-1] or "").lower()
    if ext not in {"png", "jpg", "jpeg", "webp", "mp4", "mov", "webm"}:
        ext = "png" if job["kind"] == "image" else "mp4"
    data = http("GET", src, raw=True, timeout=240)
    pathname = f"generations/{job['name']}-{task_id[-6:]}.{ext}"
    ctype = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp",
             "mp4": "video/mp4", "mov": "video/quicktime", "webm": "video/webm"}[ext]
    stored = blob_put(pathname, data, ctype)
    media = inspect_bytes(data, job["kind"])
    last_frame_url = None
    if last_src:  # one extra put per clip; kie.ai result URLs expire, and extend needs this frame
        try:
            lf = http("GET", last_src, raw=True, timeout=60)
            lext = "jpg" if lf[:2] == b"\xff\xd8" else "png"
            last_frame_url = blob_put(f"generations/{job['name']}-{task_id[-6:]}.last.{lext}", lf,
                                      "image/jpeg" if lext == "jpg" else "image/png").get("url")
        except Exception:  # noqa: BLE001  a missing last frame must never block saving the paid clip
            last_frame_url = None
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
    for k in ("group", "negative", "fields", "seed", "parent_id", "refs", "variant", "characters", "end_image",
              "ref_video", "sent_prompt"):
        entry[k] = job.get(k)
    entry["last_frame_url"] = last_frame_url
    def save(d: dict) -> None:
        if not any(e.get("task_id") == task_id for e in d["ledger"]):
            d["ledger"].append(entry)
        d["tasks"].pop(task_id, None)
    index_mutate(save)  # ledger row and task removal in one write
    job.update(status="done", kie_state="success", finished_at=time.time(),
               result={"ok": True, "url": entry["url"], "file": entry["name"], "media": media,
                       "credits_consumed": consumed, "usd": entry["usd"]})
    return {"ok": True, "job": job}


def state() -> dict:
    setup_error = None
    entries: list[dict] = []
    jobs: list[dict] = []
    try:
        idx = index_read()
        entries = idx["ledger"]
        jobs = [j for j in idx["tasks"].values() if isinstance(j, dict) and j.get("status") == "running"]
    except ApiError as e:
        setup_error = str(e)
        if "blocked" in setup_error.lower() or "HTTP 403" in setup_error:
            setup_error = ("Vercel has blocked the Blob store because the free plan's monthly operation limit was "
                           "used up. History and saving are paused until the limit resets or the plan is upgraded.")
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
    chars: list[dict] = []
    if not setup_error:
        try:
            chars = [{**c, "image_urls": list(c.get("images") or [])} for c in index_read()["characters"]]
        except ApiError:
            chars = []
    return {
        "ok": True, "mode": "vercel", "characters": chars, "now": now().isoformat(timespec="seconds"),
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
        if op == "character":
            return self._run(lambda: save_character(body))
        if op == "refresh-credits":
            return self._run(lambda: {"ok": True, "credits": kie_credits()})
        self._send({"ok": False, "error": f"unknown operation '{op}'"}, 404)
