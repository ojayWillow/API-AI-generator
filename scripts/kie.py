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
           [--resolution 1K|2K|4K] [--ref URL_OR_PATH ...] [--character ID ...] [--name slug]
  upload   --file PATH                      host a local file on kie.ai (24 h)
  video    --prompt P [--model 2.5|wan-3.0|minimax-h3|kling-3.0|pro|lite] [--draft]
           [--resolution 480p|720p|768p|1080p|2K|4K] [--duration N] [--name slug] --approve,
           plus one of three modes:
             --mode first       --image START            (default; the only mode for pro/lite)
             --mode first-last  --image START --end-image END
             --mode reference   [--character ID ...] [--ref IMG ...] [--ref-video MP4]
           Seedance 2.5 and Wan 3.0 take characters in reference mode only. Kling 3.0 takes up
           to 3 --character in every mode, so start + end frames and faces lock together.
           MiniMax H3 animates frames only (first, first-last); no characters.
  characters list | add --name N --image P [--image P ...] [--description D] | remove --id ID
  finalize --draft-task-id ID --approve      render a 2.5 draft at 1080p
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
        "kling-3.0": {"id": "kling-3.0/video", "credits_per_second": {"720p": 14, "1080p": 18, "4K": 67},
                      "credits_per_second_with_sound": {"720p": 20, "1080p": 27, "4K": 67},
                      "durations": list(range(3, 16)), "resolutions": ["720p", "1080p", "4K"],
                      "default_resolution": "720p", "modes": {"720p": "std", "1080p": "pro", "4K": "4K"}},
        "wan-3.0": {"id": "wan/3-0-video", "credits_per_second": {"480p": 8, "720p": 16, "1080p": 32},
                    "bills_input_video": True, "durations": list(range(2, 31)),
                    "resolutions": ["480p", "720p", "1080p"], "default_resolution": "720p",
                    "aspects": ["9:16", "16:9", "1:1", "4:3", "3:4"], "max_refs": 10,
                    "min_ref_video_s": 1, "max_ref_video_s": 15, "max_total_s": 30},
        "minimax-h3": {"id": "minimax-h3/image-to-video", "credits_per_second": {"768p": 8, "2K": 13},
                       "durations": list(range(4, 16)), "resolutions": ["768p", "2K"], "default_resolution": "2K"},
    },
}
KLING = "kling-3.0"
KLING_ASPECTS = ("9:16", "16:9", "1:1")
KLING_MAX_CHARACTERS = 3
WAN = "wan-3.0"  # Seedance's modes at about a fifth of the price; references are Image1.., Video1 without '@'
WAN_ASPECTS = ("9:16", "16:9", "1:1", "4:3", "3:4")
H3 = "minimax-h3"  # frames only (start, or start + end); audio is always on


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
def characters_path() -> Path:
    return gen_dir() / "characters.json"


def read_characters() -> list[dict]:
    p = characters_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return [c for c in data if isinstance(c, dict)] if isinstance(data, list) else []


def write_characters(chars: list[dict]) -> None:
    characters_path().write_text(json.dumps(chars, indent=2, ensure_ascii=False), encoding="utf-8")


MAX_PHOTOS = 8  # stored per character; the first MAX_CHAR_IMAGES are sent


def new_character_id(tag: str) -> str:
    """Never reuse an id, so generations made with a deleted character don't attach to a new one."""
    return f"{tag}-{os.urandom(3).hex()}"


def find_character(key: str) -> dict:
    k = (key or "").strip().lstrip("@").lower()
    for c in read_characters():
        if k in (str(c.get("id", "")).lower(), str(c.get("tag", "")).lower(), str(c.get("name", "")).lower()):
            return c
    die(f"unknown character '{key}'. Run: kie.py characters list")


MAX_CHAR_IMAGES = 4  # per character; the first images hold identity best
MAX_REFS = {"nano-banana-2": 14, "seedance": 30, WAN: 10}  # reference images kie.ai accepts per request


def photos_per_character(chars: list[dict], n_refs: int, cap: int) -> int:
    """How many photos each character can send so characters + extra refs fit under the model's cap."""
    if n_refs > cap:
        die(f"too many reference images: {n_refs}, the model takes {cap}")
    if not chars:
        return MAX_CHAR_IMAGES
    sent = lambda k: sum(min(len(c.get("images") or []), k) for c in chars) + n_refs
    per = next((k for k in range(MAX_CHAR_IMAGES, 0, -1) if sent(k) <= cap), 0)  # trim only what doesn't fit
    if per < 1:
        die(f"too many references: {len(chars)} characters plus {n_refs} images, the model takes {cap}")
    return per


def ref_names(prompt: str, at: str) -> str:
    """Write Image1/Video1 references the way the model reads them: Seedance '@Image1', Wan 'Image1'."""
    if at:
        return re.sub(r"(?<![@\w])(Image|Video)(\d+)\b", r"@\1\2", prompt)
    return re.sub(r"@(Image|Video)(\d+)\b", r"\1\2", prompt)


def tag_pattern(tag: str) -> str:
    return "@" + re.escape(tag) + r"(?![\w-])"  # @anna must not match inside @anna-2


def cast_prompt(prompt: str, chars: list[dict], first_index: int, video_tags: bool,
                per: int = MAX_CHAR_IMAGES, at: str = "@") -> str:
    """Bind each character to its reference images and keep identity locked.

    Seedance 2.5 addresses reference images as @Image1..@ImageN by position, so
    "@reiks" in the prompt becomes "Reiks (@Image1 @Image2)". Wan 3.0 uses the same
    names without the '@' (at=""). Nano Banana has no tag syntax, so images are
    named by number instead. Same logic as api/index.py.
    """
    if not chars:
        return prompt
    lines, idx = [], first_index
    for c in chars:
        n = min(len(c.get("images") or []), per)
        nums = list(range(idx, idx + n))
        label = " ".join(f"{at}Image{i}" for i in nums) if video_tags else \
            ("reference image " if n == 1 else "reference images ") + ", ".join(str(i) for i in nums)
        name = c.get("name") or c.get("tag") or "character"
        tag = c.get("tag") or ""
        if tag:
            prompt = re.sub(tag_pattern(tag), lambda _m: name, prompt, flags=re.IGNORECASE)
        desc = (c.get("description") or "").strip().rstrip(".")
        lines.append(f"{name} is shown in {label}" + (f": {desc}." if desc else "."))
        idx += n
    lock = "Keep every character's face, hair, skin tone, build and outfit identical to their reference images."
    body = prompt.strip()
    if body and body[-1] not in ".!?":
        body += "."
    return " ".join(lines) + " " + body + " " + lock


def character_images(chars: list[dict], per: int = MAX_CHAR_IMAGES) -> list[str]:
    out = []
    for c in chars:
        out += [str(i) for i in (c.get("images") or [])[:per]]
    return out


def kling_elements(prompt: str, chars: list[dict]) -> tuple[str, list[dict]]:
    """Turn saved characters into Kling 3.0 elements and point the prompt's @tags at them.

    Kling takes up to 3 elements of 2-4 photos each and addresses them as @name
    in the prompt. Unlike Seedance, they work together with start and end frames.
    Image paths are returned as given; the caller uploads them. Same logic as api/index.py.
    """
    if len(chars) > KLING_MAX_CHARACTERS:
        die(f"Kling 3.0 takes up to {KLING_MAX_CHARACTERS} characters per clip")
    elements, missing, used = [], [], set()
    for c in chars:
        label = c.get("name") or c.get("tag") or "character"
        imgs = [str(i) for i in (c.get("images") or [])][:MAX_CHAR_IMAGES]
        if len(imgs) < 2:
            die(f"Kling 3.0 needs at least 2 photos of {label}; add another to the character first")
        name = re.sub(r"\W+", "_", str(c.get("tag") or label)).strip("_").lower() or "character"
        while name in used:
            name += "_2"
        used.add(name)
        tag = c.get("tag") or ""
        if tag and re.search(tag_pattern(tag), prompt, flags=re.IGNORECASE):
            prompt = re.sub(tag_pattern(tag), lambda _m: "@" + name, prompt, flags=re.IGNORECASE)
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


def upload_stream(path: Path, mime: str) -> str:
    """Multipart upload for files over the 10 MB base64 limit (clips used as reference video)."""
    boundary = "----kie" + os.urandom(8).hex()
    parts = []
    for k, v in (("uploadPath", "generate-skill"), ("fileName", path.name)):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
                 f"Content-Type: {mime}\r\n\r\n".encode() + path.read_bytes() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(UPLOAD_API + "/api/file-stream-upload", data=b"".join(parts), method="POST",
                                 headers={"Authorization": f"Bearer {api_key()}",
                                          "Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            res = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        die(f"stream upload failed: {e}")
    url = (res.get("data") or {}).get("downloadUrl")
    if res.get("code") != 200 or not url:
        die(f"upload failed: {res.get('msg') or res}", response=res)
    return url


def upload_file(path: Path) -> str:
    """Host a local file on kie.ai for 24 h and return its public URL."""
    if not path.exists():
        die(f"file not found: {path}")
    mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    if path.stat().st_size > 10 * 1024 * 1024:
        return upload_stream(path, mime)
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
    if requested:  # never render a different resolution than the one asked for
        die(f"{model} renders {', '.join(allowed)}, not {requested}")
    return pr.get("default_resolution") or allowed[-1]


def quote_video(model: str, duration: float, resolution: str | None = None, ref_video_s: float = 0,
                audio: bool = True) -> dict:
    pr = load_pricing()["video"]
    if model not in pr:
        die(f"unknown video model '{model}'. Options: {', '.join(pr)}")
    dur, note = normalise_duration(model, duration)
    res = video_resolution(model, resolution)
    rate = pr[model]["credits_per_second"]
    if audio and pr[model].get("credits_per_second_with_sound"):
        rate = pr[model]["credits_per_second_with_sound"]  # Kling charges more with sound
    billed = dur
    if ref_video_s and (pr[model].get("credits_per_second_with_video") or pr[model].get("bills_input_video")):
        # kie.ai bills reference-video seconds plus output seconds (Seedance at a lower rate, Wan at the same rate)
        rate = pr[model].get("credits_per_second_with_video") or rate
        billed = dur + float(ref_video_s)
    lo, hi, total = pr[model].get("min_ref_video_s") or 0, pr[model].get("max_ref_video_s"), pr[model].get("max_total_s")
    name = {WAN: "Wan 3.0"}.get(model, model)
    if ref_video_s and hi and not lo <= float(ref_video_s) <= hi:
        die(f"{name} takes reference clips of {lo:g}-{hi} s; this one is {float(ref_video_s):g} s")
    if ref_video_s and total and dur + float(ref_video_s) > total:
        die(f"{name} caps the reference clip plus the new part at {total} s ({float(ref_video_s):g} s in + "
            f"{dur} s out); ask for --duration {int(total - float(ref_video_s))} or less")
    if isinstance(rate, dict):
        rate = rate.get(res) or max(rate.values())
    credits = int(round(rate * billed))
    q = {"kind": "video", "model": model, "model_id": pr[model]["id"], "duration_s": dur,
         "resolution": res, "credits_est": credits, "usd_est": round(credits * USD_PER_CREDIT, 4)}
    if ref_video_s:
        q["ref_video_s"] = round(float(ref_video_s), 2)
    if note:
        q["note"] = note
    return q


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
def quote_final(duration: float) -> dict:
    pr = load_pricing()["video"]["2.5-final"]
    dur = int(round(duration))
    credits = int(pr["credits_per_second"] * dur)
    return {"kind": "final", "model": "2.5-final", "model_id": pr["id"], "duration_s": dur, "resolution": "1080p",
            "credits_est": credits, "usd_est": round(credits * USD_PER_CREDIT, 4)}


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
    if a.kind == "image":
        q = quote_image(a.model or "nano-banana-2", a.resolution or "1K")
    elif a.kind == "final":
        q = quote_final(a.duration or 10)
    else:
        q = quote_video(a.model or "2.5", a.duration or 10, a.resolution, a.ref_video_seconds or 0, not a.no_audio)
    out({"ok": True, **q, "spend": spend_summary()})


def finish(kind: str, task: dict, model_key: str, model_id: str, prompt: str, params: dict,
           name: str, default_ext: str, est_credits: int, extra: dict) -> None:
    urls = task["_result"].get("resultUrls") or []
    if not urls:
        die("task succeeded but returned no resultUrls", task=task)
    src, last_frame = split_result(task["_result"], urls, kind)
    dest = gen_dir() / f"{name}.{ext_from_url(src, default_ext)}"
    i = 1
    while dest.exists():
        dest = dest.with_name(f"{name}-{i}{dest.suffix}")
        i += 1
    log(f"  downloading -> {dest}")
    download(src, dest)
    info = inspect_file(dest)
    if last_frame:
        lf = dest.with_name(f"{dest.stem}.last.{ext_from_url(last_frame, 'png')}")
        try:
            download(last_frame, lf)
            extra = {**extra, "last_frame": lf.name}
        except (urllib.error.URLError, OSError) as e:
            log(f"  could not save the last frame: {e}")
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


VIDEO_EXTS = {"mp4", "mov", "webm"}


def split_result(res: dict, urls: list[str], kind: str) -> tuple[str, str | None]:
    """Pick the main output and, for Seedance 2.5 with return_last_frame, the last-frame image.

    kie.ai's task-detail doc gives the last frame as resultJson.lastFrameUrl (a list).
    Accept a string too, and fall back to an image URL next to the video in resultUrls.
    """
    if kind != "video":
        return urls[0], None
    vids = [u for u in urls if ext_from_url(u, "") in VIDEO_EXTS]
    src = vids[0] if vids else urls[0]
    last = None
    for k, v in res.items():
        if "last" in k.lower() and "frame" in k.lower():
            # kie.ai documents Seedance 2.x as {resultUrls: [], lastFrameUrl: []}: a list, not a string
            v = next((x for x in v if isinstance(x, str)), None) if isinstance(v, list) else v
            if isinstance(v, str) and v.startswith("http"):
                last = v
    if not last:
        imgs = [u for u in urls if u != src and ext_from_url(u, "") in {"png", "jpg", "jpeg", "webp"}]
        last = imgs[0] if imgs else None
    return src, last


def cmd_image(a: argparse.Namespace) -> None:
    model = a.model or "nano-banana-2"
    resolution = a.resolution or "1K"
    aspect = a.aspect or "9:16"
    q = quote_image(model, resolution)
    enforce_caps(q["usd_est"], a.override_cap)
    chars = [find_character(c) for c in (a.character or [])]
    if chars and model != "nano-banana-2":
        log("  note: characters need reference images; switching to nano-banana-2")
        model = "nano-banana-2"
        q = quote_image(model, resolution)
    per = photos_per_character(chars, len(a.ref or []), MAX_REFS["nano-banana-2"])
    prompt = cast_prompt(a.prompt, chars, 1, video_tags=False, per=per)
    refs = [ensure_url(r) for r in character_images(chars, per) + (a.ref or [])]
    if model == "nano-banana-2":
        inp = {"prompt": prompt, "image_input": refs, "aspect_ratio": aspect,
               "resolution": resolution, "output_format": a.format or "png"}
    else:  # gpt-image-2-text-to-image
        if refs:
            log("  note: gpt-image-2 text-to-image ignores reference images")
        inp = {"prompt": prompt, "aspect_ratio": aspect, "resolution": resolution}
    log(f"Submitting image to {q['model_id']} (~{q['credits_est']} credits / ${q['usd_est']:.3f})")
    task_id = create_task(q["model_id"], inp)
    task = poll_task(task_id, every=10, timeout=600)
    name = a.name or f"{dt.datetime.now():%Y%m%d-%H%M%S}-{slugify(a.prompt)}"
    finish("image", task, model, q["model_id"], a.prompt,
           {"aspect_ratio": aspect, "resolution": resolution, "refs": refs},
           name, "png", q["credits_est"],
           {"preset": a.preset, "characters": [c.get("id") for c in chars] or None, "sent_prompt": prompt if chars else None})


def cmd_upload(a: argparse.Namespace) -> None:
    url = upload_file(Path(a.file).expanduser())
    out({"ok": True, "url": url, "expires": "24h"})


def local_or_none(ref: str | None) -> Path | None:
    return None if not ref or re.match(r"^https?://", ref) else Path(ref).expanduser()


def check_still(ref: str, label: str) -> None:
    p = local_or_none(ref)
    if p is not None:
        info = inspect_file(p)
        if not info.get("width"):
            die(f"could not read image dimensions from {p}")
        log(f"  {label}: {info['width']}x{info['height']} ({info.get('orientation')})")


def cmd_video(a: argparse.Namespace) -> None:
    model = a.model or "2.5"
    mode = a.mode or ("first-last" if a.end_image else "reference" if (a.character or a.ref or a.ref_video) and not a.image else "first")
    kling, wan, h3 = model == KLING, model == WAN, model == H3
    if a.draft and model != "2.5":
        die("--draft is a Seedance 2.5 feature; for a cheap try render this model at a lower --resolution")
    if mode != "first" and model not in ("2.5", WAN, KLING, H3):
        die(f"--mode {mode} needs Seedance 2.5, Wan 3.0, Kling 3.0 or MiniMax H3")
    if mode == "reference" and h3:
        die("MiniMax H3 animates frames only: use --mode first or first-last, or --model wan-3.0 / kling-3.0 for characters")
    if mode in ("first", "first-last") and not a.image:
        die("--image (the start frame) is required for this mode")
    if mode == "first-last" and not a.end_image:
        die("--end-image is required for --mode first-last")
    if mode == "reference" and (a.image or a.end_image):
        die("--mode reference takes no --image or --end-image; use --mode first or first-last for frames")
    if kling and (a.ref or a.ref_video):
        die("Kling 3.0 takes characters (--character), not loose --ref images or --ref-video clips")
    if h3 and (a.character or a.ref or a.ref_video):
        die("MiniMax H3 takes no characters or references. Put the cast into the start and end stills, "
            "or use --model kling-3.0 to lock faces with start and end frames.")
    if not (kling or h3) and mode != "reference" and (a.character or a.ref or a.ref_video):
        die(f"{'Wan 3.0' if wan else 'Seedance'} takes characters and references only in --mode reference, never with "
            "a start frame. Use --model kling-3.0 to lock faces with start and end frames, or put the character into the stills.")
    chars = [find_character(c) for c in (a.character or [])]
    if mode == "reference" and not (chars or a.ref or a.ref_video):
        die("--mode reference needs at least one --character" + ("" if kling else ", --ref or --ref-video"))
    if kling and (a.aspect or "9:16") not in KLING_ASPECTS and not a.image:
        die(f"Kling 3.0 takes --aspect {', '.join(KLING_ASPECTS)}")
    if wan and mode == "reference" and (a.aspect or "9:16") not in WAN_ASPECTS:
        die(f"Wan 3.0 takes --aspect {', '.join(WAN_ASPECTS)}")

    ref_video_s = 0.0
    if a.ref_video:
        lv = local_or_none(a.ref_video)
        ref_video_s = a.ref_video_seconds or ((mp4_info(lv) or {}).get("duration_s") if lv else 0) or 0
        if not ref_video_s:
            die("could not read the reference video length; pass --ref-video-seconds")
    # quote_video also checks Wan's clip limits (1-15 s, clip + new part within 30 s)
    q = quote_video(model, a.duration or 10, "480p" if a.draft else a.resolution, ref_video_s, not a.no_audio)
    s = enforce_caps(q["usd_est"], a.override_cap)
    if not a.approve:
        out({"ok": False, "approval_required": True,
             "message": "Paid video run. Show this quote to the user and rerun with --approve "
                        "after they explicitly say go.",
             **q, "spend": s}, code=2)

    prompt, image_url, end_url, refs, video_refs = a.prompt, None, None, [], []
    if mode in ("first", "first-last"):
        check_still(a.image, "start frame")
        image_url = ensure_url(a.image)
    if mode == "first-last":
        check_still(a.end_image, "end frame")
        end_url = ensure_url(a.end_image)
    elements = []
    if kling and chars:
        prompt, elements = kling_elements(prompt, chars)
        for e in elements:
            e["element_input_urls"] = [ensure_url(u) for u in e["element_input_urls"]]
    elif mode == "reference":
        per = photos_per_character(chars, len(a.ref or []), MAX_REFS[WAN if wan else "seedance"])
        refs = [ensure_url(r) for r in character_images(chars, per) + (a.ref or [])]
        video_refs = [ensure_url(a.ref_video)] if a.ref_video else []
        prompt = ref_names(cast_prompt(prompt, chars, 1, video_tags=True, per=per, at="" if wan else "@"),
                           "" if wan else "@")
    audio = h3 or not a.no_audio  # MiniMax H3 always renders sound

    if model == "2.5":
        inp = {"prompt": prompt, "duration": q["duration_s"], "resolution": q["resolution"],
               "generate_audio": not a.no_audio, "output_format": "mp4", "return_last_frame": True}
        if mode == "reference":
            inp["aspect_ratio"] = a.aspect or "9:16"
            if refs:
                inp["reference_image_urls"] = refs
            if video_refs:
                inp["reference_video_urls"] = video_refs
        else:
            inp["first_frame_url"] = image_url
            inp["aspect_ratio"] = "adaptive"
            if end_url:
                inp["last_frame_url"] = end_url
        if a.draft:
            inp["draft"] = True
    elif kling:
        # Kling 3.0: frames and characters in one request; std = 720p, pro = 1080p
        inp = {"prompt": prompt, "sound": not a.no_audio, "duration": str(q["duration_s"]),
               "mode": load_pricing()["video"][KLING]["modes"][q["resolution"]], "multi_shots": False}
        if image_url:
            inp["image_urls"] = [image_url] + ([end_url] if end_url else [])
        else:
            inp["aspect_ratio"] = a.aspect or "9:16"
        if elements:
            inp["kling_elements"] = elements
        if a.camera_fixed or a.seed != -1:
            log("  note: Kling 3.0 has no camera-fixed or seed option; ignored")
    elif wan:
        # Wan 3.0: Seedance's three modes; references are named Image1.., Video1 in the prompt
        inp = {"prompt": prompt, "duration": q["duration_s"], "resolution": q["resolution"].upper(), "audio": audio}
        if mode == "reference":
            inp["aspect_ratio"] = a.aspect or "9:16"
            if refs:
                inp["reference_image_urls"] = refs
            if video_refs:
                inp["reference_video_urls"] = video_refs
        else:
            inp["first_frame_url"] = image_url
            inp["aspect_ratio"] = "adaptive"
            if end_url:
                inp["last_frame_url"] = end_url
        if a.seed != -1:
            inp["seed"] = int(a.seed)
        if a.camera_fixed:
            log("  note: Wan 3.0 has no camera-fixed option; ignored")
    elif h3:
        # MiniMax H3: a start frame, or a start + end frame; 768P or 2K, sound always on
        inp = {"prompt": prompt, "duration": q["duration_s"], "resolution": q["resolution"].upper(),
               "first_frame_url": image_url}
        if end_url:
            inp["last_frame_url"] = end_url
        if a.camera_fixed or a.seed != -1 or a.no_audio:
            log("  note: MiniMax H3 has no camera-fixed, seed or silent option; ignored")
    else:
        inp = {"prompt": prompt, "image_url": image_url, "resolution": "1080p",
               "duration": str(q["duration_s"]), "camera_fixed": bool(a.camera_fixed), "seed": int(a.seed)}
    if q.get("note"):
        log(f"  {q['note']}")
    log(f"Submitting video ({mode}) to {q['model_id']} ({q['duration_s']}s {q['resolution']}, "
        f"~{q['credits_est']} credits / ${q['usd_est']:.2f})")
    task_id = create_task(q["model_id"], inp)
    task = poll_task(task_id, every=15, timeout=1500)
    still_local = local_or_none(a.image)
    # a start frame saved as <clip>.last.png must not name the new clip <clip>.last.mp4
    stem = re.sub(r"(\.last)+$", "", still_local.stem) if still_local else ""
    name = a.name or stem or f"{dt.datetime.now():%Y%m%d-%H%M%S}-{slugify(a.prompt)}"
    finish("video", task, model, q["model_id"], a.prompt,
           {"duration_s": q["duration_s"], "resolution": q["resolution"], "camera_fixed": bool(a.camera_fixed),
            "seed": int(a.seed), "draft": bool(a.draft), "generate_audio": audio, "mode": mode,
            "ref_video_s": ref_video_s or None, "aspect_ratio": inp.get("aspect_ratio")},
           name, "mp4", q["credits_est"],
           {"input_image": a.image, "input_image_url": image_url, "preset": a.preset,
            "end_image": a.end_image, "ref_video": a.ref_video, "refs": (a.ref or None),
            "characters": [c.get("id") for c in chars] or None,
            "sent_prompt": prompt if prompt != a.prompt else None})


def cmd_characters(a: argparse.Namespace) -> None:
    chars = read_characters()
    if a.action == "list":
        out({"ok": True, "file": str(characters_path()), "characters": chars})
    if a.action == "remove":
        c = find_character(a.id)
        write_characters([x for x in chars if x.get("id") != c.get("id")])
        out({"ok": True, "removed": c.get("id")})
    if not a.name or not a.image:
        die("characters add needs --name and at least one --image")
    tag = slugify(a.tag or a.name, 24)
    if any(x.get("tag") == tag for x in chars):
        die(f"a character with tag @{tag} already exists")
    cid = new_character_id(tag)
    images = []
    refs = gen_dir() / "refs"
    refs.mkdir(parents=True, exist_ok=True)
    if len(a.image) > MAX_PHOTOS:
        die(f"a character holds up to {MAX_PHOTOS} photos")
    for i, im in enumerate(a.image, 1):
        p = local_or_none(im)
        if p is None:
            images.append(im)
            continue
        if not p.exists():
            die(f"file not found: {p}")
        dest = refs / f"{cid}-{i}-{p.name}"  # keep a copy next to the outputs so the dashboard can show it
        if p.resolve() != dest.resolve():
            shutil.copyfile(p, dest)
        images.append(str(dest.resolve()))
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    c = {"id": cid, "tag": tag, "name": a.name.strip(), "description": (a.description or "").strip(),
         "images": images, "created_at": now, "updated_at": now}
    write_characters(chars + [c])
    out({"ok": True, "character": c})


def cmd_finalize(a: argparse.Namespace) -> None:
    """Render a Seedance 2.5 draft (480p) at 1080p, keeping its motion. Draft ids are valid 7 days."""
    src = next((e for e in read_ledger() if e.get("task_id") == a.draft_task_id), None)
    dur = a.duration or ((src or {}).get("params") or {}).get("duration_s")
    if not dur:
        die("duration unknown for this draft; pass --duration")
    q = quote_final(float(dur))
    s = enforce_caps(q["usd_est"], a.override_cap)
    if not a.approve:
        out({"ok": False, "approval_required": True,
             "message": "Paid 1080p render. Show this quote to the user and rerun with --approve after they say go.",
             **q, "spend": s}, code=2)
    log(f"Rendering draft {a.draft_task_id} at 1080p (~{q['credits_est']} credits / ${q['usd_est']:.2f})")
    task_id = create_task(q["model_id"], {"draft_task_id": a.draft_task_id, "resolution": "1080p", "output_format": "mp4"})
    task = poll_task(task_id, every=15, timeout=1800)
    base = Path(src["file"]).stem if src and src.get("file") else f"draft-{a.draft_task_id[-6:]}"
    name = a.name or f"{base}-1080p"
    finish("video", task, "2.5-final", q["model_id"], (src or {}).get("prompt") or "",
           {"duration_s": int(dur), "resolution": "1080p", "draft_task_id": a.draft_task_id, "draft": False,
            **{k: ((src or {}).get("params") or {}).get(k) for k in ("mode", "aspect_ratio", "ref_video_s", "generate_audio")}},
           name, "mp4", q["credits_est"],
           {"input_image": (src or {}).get("input_image"), "preset": (src or {}).get("preset"),
            "parent_id": a.draft_task_id, "group": (src or {}).get("group"),
            **{k: (src or {}).get(k) for k in ("characters", "end_image", "ref_video", "refs")}})


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
    p.add_argument("--kind", choices=["image", "video", "final"], required=True)
    p.add_argument("--model")
    p.add_argument("--duration", type=float)
    p.add_argument("--resolution", help="image: 1K/2K/4K; video: 480p/720p/1080p (Seedance, Wan), 720p/1080p/4K (Kling), 768p/2K (MiniMax H3)")
    p.add_argument("--ref-video-seconds", type=float,
                   help="length of a reference video; billed on input + output (Seedance at a lower rate, Wan 3.0 at the same rate)")
    p.add_argument("--no-audio", action="store_true", help="Kling 3.0 costs less without sound; MiniMax H3 always has sound")
    p.set_defaults(fn=cmd_quote)

    p = sub.add_parser("image")
    p.add_argument("--prompt", required=True)
    p.add_argument("--model", choices=["nano-banana-2", "gpt-image-2"])
    p.add_argument("--aspect", help="e.g. 9:16 (default), 16:9, 1:1, auto")
    p.add_argument("--resolution", choices=["1K", "2K", "4K"])
    p.add_argument("--format", choices=["png", "jpg"])
    p.add_argument("--ref", action="append", help="reference image URL or local path (repeatable)")
    p.add_argument("--character", action="append", help="character id or @tag; its images go first (repeatable)")
    p.add_argument("--name", help="output file stem")
    p.add_argument("--preset", help="preset name, recorded in the sidecar")
    p.add_argument("--override-cap", action="store_true")
    p.set_defaults(fn=cmd_image)

    p = sub.add_parser("upload")
    p.add_argument("--file", required=True)
    p.set_defaults(fn=cmd_upload)

    p = sub.add_parser("video")
    p.add_argument("--image", help="start frame: local path or public URL (modes first and first-last)")
    p.add_argument("--mode", choices=["first", "first-last", "reference"],
                   help="start frame only (default), start + end frame, or characters/references (Seedance 2.5, Wan 3.0, Kling 3.0)")
    p.add_argument("--end-image", help="end frame for --mode first-last")
    p.add_argument("--character", action="append",
                   help="character id or @tag (repeatable). Seedance / Wan: --mode reference only. Kling 3.0: any mode, up to 3")
    p.add_argument("--ref", action="append", help="extra reference image (repeatable, --mode reference)")
    p.add_argument("--ref-video", help="reference clip, e.g. the previous shot to continue (--mode reference)")
    p.add_argument("--ref-video-seconds", type=float, help="length of --ref-video when it is a URL")
    p.add_argument("--aspect", help="--mode reference only: 9:16 (default), 16:9, 1:1, 4:3, 3:4, 21:9 (Kling: 9:16, 16:9, 1:1; Wan: no 21:9)")
    p.add_argument("--prompt", required=True, help="short motion description")
    p.add_argument("--model", choices=["2.5", WAN, H3, KLING, "pro", "lite"], help="default 2.5")
    p.add_argument("--duration", type=float)
    p.add_argument("--resolution", choices=["480p", "720p", "768p", "1080p", "2K", "4K"],
                   help="Seedance 2.5: 480p (28 cr/s), 720p (63), 1080p (158). Wan 3.0: 480p (8), 720p (16), 1080p (32). "
                        "MiniMax H3: 768p (8), 2K (13). Kling 3.0: 720p (20 with sound), 1080p (27), 4K (67). "
                        "Pro/Lite are always 1080p")
    p.add_argument("--draft", action="store_true",
                   help="Seedance 2.5 draft mode: 480p preview whose task id can be rendered at 1080p with 'finalize' within 7 days")
    p.add_argument("--no-audio", action="store_true", help="Seedance 2.5 / Wan 3.0 / Kling 3.0: skip generated audio")
    p.add_argument("--camera-fixed", action="store_true")
    p.add_argument("--seed", type=int, default=-1, help="-1 for random; reuse a seed to stay close to a previous clip")
    p.add_argument("--name")
    p.add_argument("--preset")
    p.add_argument("--approve", action="store_true", help="user explicitly approved this paid run")
    p.add_argument("--override-cap", action="store_true")
    p.set_defaults(fn=cmd_video)

    p = sub.add_parser("finalize", help="render a Seedance 2.5 draft at 1080p (same motion)")
    p.add_argument("--draft-task-id", required=True, help="task id of the 480p draft (valid 7 days)")
    p.add_argument("--duration", type=float, help="draft length in seconds, if not in the ledger")
    p.add_argument("--name")
    p.add_argument("--approve", action="store_true", help="user explicitly approved this paid run")
    p.add_argument("--override-cap", action="store_true")
    p.set_defaults(fn=cmd_finalize)

    p = sub.add_parser("characters", help="saved characters reused across generations")
    p.add_argument("action", choices=["list", "add", "remove"])
    p.add_argument("--name")
    p.add_argument("--tag", help="short @tag for prompts; default from the name")
    p.add_argument("--description", help="one line that fixes identity: age, hair, outfit, distinguishing marks")
    p.add_argument("--image", action="append", help="reference image path or URL (repeatable, best 2 to 4)")
    p.add_argument("--id")
    p.set_defaults(fn=cmd_characters)

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
