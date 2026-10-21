---
name: generate
description: Generate images and short vertical videos pay-per-use through kie.ai (Nano Banana 2, GPT Image 2, Seedance) instead of a Higgsfield subscription. Drafts a cheap still, runs a QA gate, quotes the cost, waits for approval, then animates it. Use when the user asks to /generate, make an image, make a clip, animate a still, or create faceless social video.
allowed-tools: Read, Write, Edit, Bash, Glob
---

# /generate

Pay-as-you-go image and video generation through kie.ai. One still costs a few
cents, one 10-second 1080p clip costs about 70 cents, and nothing is billed
when a job fails. Every paid video run needs an explicit "go" from the user.

All API work goes through one script so you never hand-write curl calls:

```
python <skill>/scripts/kie.py <command> ...
```

`<skill>` is the folder that contains this SKILL.md. Each command prints one
JSON object on stdout. Exit code 2 means approval is required, 3 means a budget
cap would be exceeded.

## Setup check (once per session)

1. If `<skill>/.env` is missing, tell the user to copy `.env.example` to `.env`
   and paste their kie.ai key (https://kie.ai/api-key). Do not ask them to paste
   the key into chat.
2. Run `kie.py credits`. It confirms the key works and shows remaining credits
   plus today's and this month's spend from `generations/ledger.json`.

## Models

| Use | `--model` | kie.ai id | Est. cost |
|---|---|---|---|
| Image, default | `nano-banana-2` | `nano-banana-2` | ~$0.04 (1K) |
| Image with readable text, signs, posters, app UI | `gpt-image-2` | `gpt-image-2-text-to-image` | ~$0.05 |
| Video, default | `2.5` | `bytedance/seedance-2-5` | 28 cr/s at 480p draft, 63 at 720p, 158 at 1080p |
| Video, 1080p final of a draft | `finalize` command | `bytedance/seedance-2-5-draft` | 158 cr/s (same motion as the draft) |
| Video, characters with start/end frames | `kling-3.0` | `kling-3.0/video` | 20 cr/s at 720p, 27 at 1080p (with sound) |
| Video, best value (Seedance's modes at ~1/5 the price) | `wan-3.0` | `wan/3-0-video` | 16 cr/s at 720p, 32 at 1080p (sound included) |
| Video, animate a still (start or start + end) | `minimax-h3` | `minimax-h3/image-to-video` | 8 cr/s at 768p, 13 at 2K (sound always on) |
| Video, legacy, only if asked | `pro` / `lite` | Seedance 1.0 | 14 / 10 cr/s at 1080p |

Seedance 2.5 is the default and the hero-shot model. Its loop is **draft then finalize**:
make one or more 480p drafts with `--draft` (about $1.40 per 10 s each), let
the user pick, then run `finalize --draft-task-id <id>` to render that exact
draft at 1080p (about $7.90 per 10 s). Never render 1080p directly unless the
user asks for it. Draft ids expire 7 days after creation. Read
`models/seedance-2-5-image-to-video.md` before the first video of a session.
Video orientation is inherited from the still, so the still decides the
aspect ratio.

Use **Kling 3.0** when a saved character must stay recognisable in a shot that
starts or ends on a specific still: it is the only model that takes frames and
characters together, and it is about 6x cheaper than a Seedance 1080p final.
It has no drafts; 720p is the cheap try. Read `models/kling-3-0.md` first.

Use **Wan 3.0** when the user wants Seedance's modes (frames, Cast, extend
with a clip) for less: a 10 s clip is $0.80 at 720p and $1.60 at 1080p with
sound. It names references `Image1` and `Video1` without `@`. Use **MiniMax
H3** to animate a still: start frame or start + end only, no characters,
$0.65 per 10 s at 2K. Neither has drafts. Read `models/wan-3-0.md` or
`models/minimax-h3.md` first. In October 2026 both ranked level with or
above Seedance 2.5 in blind votes, so suggest them when cost matters.

## Workflow

### 1. Understand the request and load a preset
- Default output is vertical 9:16 for Shorts, Reels and TikTok. Use 16:9 or
  1:1 only when asked.
- If the user names a preset, or the request clearly matches one, read
  `presets/<name>.md` and apply its locked prompt fragment, aspect ratio and
  reference images. Never drift from a preset mid-series.
- Decide image model: `gpt-image-2` if the frame must contain readable text,
  otherwise `nano-banana-2`.

### 2. Draft the still
```
python <skill>/scripts/kie.py image --prompt "<detailed scene prompt>" --aspect 9:16 --resolution 1K --name <slug> [--ref <url-or-path>] [--preset <name>]
```
Write the prompt yourself: subject, setting, lighting, camera, mood, and
"no text, no watermark" unless text is wanted. Stills are cheap, so this
step does not need approval. Report the file path and cost from the JSON.

### 3. QA gate (before any video spend)
Use the Read tool on the generated image to look at it, then check:
- orientation and size match the request (the JSON `media` block has
  `width`, `height`, `is_1080x1920`)
- no unintended baked-in text or watermark
- subject, setting and point of view match the request and preset
- the scene can carry 5 or 10 seconds of motion

If any check fails: stop, say which one failed, offer to redraft the still
with a revised prompt. Do not animate a still the user has not seen.

### 4. Quote and approval
```
python <skill>/scripts/kie.py quote --kind video --model 2.5 --duration 10 --resolution 480p
```
Show the user: duration, quality, estimated cost, and remaining budget for
today and the month. Then wait. One "go" approves exactly one run. Never batch
paid runs on one approval.

If the quote exits with code 3 the run would exceed a cap. Tell the user and
only pass `--override-cap` after they explicitly agree.

### 5. Draft the clip (480p)
```
python <skill>/scripts/kie.py video --image generations/<slug>.png --prompt "<motion and sound>" --model 2.5 --draft --duration 10 --name <slug> --approve
```
The script uploads the still to kie.ai's file host, submits the job, polls
every 15 seconds, downloads the mp4, reads its real dimensions and duration,
writes a sidecar `.json`, and appends the ledger. The sidecar's `task_id` is
the draft id. Motion prompts should be short and physical: camera move,
subject motion, atmosphere, sound. Keep the point of view identical to the
still. Make two or three drafts with different motion when the user is
unsure; they are cheap compared with the final.

### 5b. Finalize the keeper (1080p)
```
python <skill>/scripts/kie.py quote --kind final --duration 10
python <skill>/scripts/kie.py finalize --draft-task-id <task_id of the draft> --approve
```
Quote first, wait for "go", then finalize. The final keeps the draft's motion
and audio and is saved as `<draft name>-1080p.mp4` with `parent_id` pointing
at the draft. Only finalize drafts the user has watched and approved.

### 6. Verify and report
- Check the JSON `media` block: dimensions, duration, orientation.
- If ffmpeg is installed, `kie.py inspect --file <mp4> --frame` extracts a
  frame you can Read to confirm it matches. Without ffmpeg, say so and ask
  the user to open the file.
- Report: file path, model, actual credits consumed, dollars, remaining
  budget. If anything looks wrong, say it failed rather than handing over a
  bad file.

## Characters, start + end frames, extending

Saved characters live in `generations/characters.json` (local) or the Blob
index (hosted). Each one is a name, a short `@tag`, one identity line and 1-8
photos; the first 4 are sent, first photo first.

```
python <skill>/scripts/kie.py characters add --name Reiks --description "late 20s, short dark fade, black bomber" --image front.jpg --image three-quarter.jpg --image full-body.jpg
python <skill>/scripts/kie.py characters list
```

- **Still with a character:** `image --character reiks --prompt "@reiks leans on a black car at night"`.
  The script binds `@reiks` to his photos and appends an identity lock.
- **Start + end:** make both stills with the character, then
  `video --mode first-last --image start.png --end-image end.png --prompt "<the move between them>" --draft --approve`.
- **Character in a new clip without a start still:**
  `video --mode reference --character reiks --prompt "@reiks raps to camera, slow orbit" --draft --approve`.
- **Extend a clip:** either `--mode first --image <clip>.last.png` (exact
  continuity, no character lock) or `--mode reference --ref-video <clip>.mp4
  --character reiks --prompt "Continue @Video1 seamlessly. Next: ..."` (keeps
  the face; the reference clip's seconds are billed at the lower with-video
  rate, so quote with `--ref-video-seconds`). The reference clip must be 480p
  or 720p, so extend a 1080p final from its draft.
- **Start + end with the face locked (Kling 3.0):**
  `video --model kling-3.0 --mode first-last --image start.png --end-image end.png --character reiks --prompt "@reiks <the move>" --resolution 720p --approve`.
  The same works with `--mode first --image <clip>.last.png` to continue a shot
  with the cast. Up to 3 characters, each with at least 2 photos.
- **Same modes, a fifth of the price (Wan 3.0):**
  `video --model wan-3.0 --mode reference --character reiks --prompt "@reiks raps to camera, one continuous shot" --resolution 720p --approve`.
  To extend with the cast: `--mode reference --ref-video <clip>.mp4 --ref-video-seconds <s> --character reiks --prompt "Continue Video1 seamlessly. Next: ..."`
  (no `@` before `Video1`; the clip may be up to 15 s and 1080p, and its seconds are billed at the same rate).
- **Animate a still (MiniMax H3):**
  `video --model minimax-h3 --image still.png [--mode first-last --end-image end.png] --prompt "<the move>" --resolution 2K --approve`.

Read the "Three modes" section of `models/seedance-2-5-image-to-video.md`
before using these.

## Budget rules

- `SESSION_CAP_USD` (default $10 per calendar day) and `MONTHLY_CAP_USD`
  (default $50) live in `.env`. The script enforces them.
- `generations/ledger.json` is the source of truth for spend. Never edit it
  by hand.
- Quote before every paid video run. Stills under $0.10 may run without a
  quote, but always report what they cost.
- Failed kie.ai tasks are not billed. Say so when one fails, fix the input,
  and resubmit only with a fresh go.

## Hard rules

- Do not animate before the QA gate and the budget gate both pass.
- Do not exceed a cap without the user's explicit agreement.
- Do not change the point of view, subject or style between still and clip.
- Do not print the API key, and do not put it in any file other than `.env`.
- Hosted kie.ai result URLs expire within hours. The script downloads
  everything immediately; always point the user at the local file.

## Dashboard

`python <skill>/scripts/dashboard.py --open` starts a local page on
http://127.0.0.1:8787 with credits, spend against caps, a daily spend chart,
the gallery, and a generation form. Offer it when the user wants to browse
outputs or watch spend; it uses the same ledger and `kie.py` as this skill.

## Useful commands

```
python <skill>/scripts/kie.py credits
python <skill>/scripts/kie.py ledger            # spend today / this month
python <skill>/scripts/kie.py ledger --all      # every run
python <skill>/scripts/kie.py status --task-id <id>
python <skill>/scripts/kie.py inspect --file <png|jpg|mp4>
python <skill>/scripts/kie.py upload --file <image>   # public URL for 24 h
```
