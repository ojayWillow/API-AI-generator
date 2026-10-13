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
| Video, default | `pro` | `bytedance/v1-pro-image-to-video` | 14 credits/s, $0.70 per 10 s |
| Video, budget | `lite` | `bytedance/v1-lite-image-to-video` | 10 credits/s, $0.50 per 10 s |
| Video, newest, only when the user asks for it | `2.5` | `bytedance/seedance-2-5` | 28 cr/s at 480p, 63 at 720p, 158 at 1080p ($7.90 per 10 s) |

Read the matching recipe in `models/` before the first use of a model in a
session. Seedance 1.0 accepts only 5 or 10 seconds; the script rounds other
values and reports it. Video orientation is inherited from the still, so the
still decides the aspect ratio.

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
python <skill>/scripts/kie.py quote --kind video --model pro --duration 10
```
Show the user: model, duration, estimated cost, and remaining budget for today
and the month. Then wait. One "go" approves exactly one run. Never batch paid
runs on one approval.

If the quote exits with code 3 the run would exceed a cap. Tell the user and
only pass `--override-cap` after they explicitly agree.

### 5. Animate
```
python <skill>/scripts/kie.py video --image generations/<slug>.png --prompt "<short motion description>" --model pro --duration 10 --name <slug> --approve
```
The script uploads the still to kie.ai's file host, submits the job, polls
every 15 seconds, downloads the mp4, reads its real dimensions and duration,
writes a sidecar `.json`, and appends the ledger. Motion prompts should be
short and physical: camera move, subject motion, atmosphere. Keep the point
of view identical to the still.

### 6. Verify and report
- Check the JSON `media` block: dimensions, duration, orientation.
- If ffmpeg is installed, `kie.py inspect --file <mp4> --frame` extracts a
  frame you can Read to confirm it matches. Without ffmpeg, say so and ask
  the user to open the file.
- Report: file path, model, actual credits consumed, dollars, remaining
  budget. If anything looks wrong, say it failed rather than handing over a
  bad file.

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
