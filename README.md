# generate: a pay-per-use Claude skill that replaces a Higgsfield subscription

A [Claude Code skill](https://agentskills.io) that makes images and short
vertical videos through [kie.ai](https://kie.ai), the pay-as-you-go API that
hosts the same models Higgsfield wraps (Nano Banana 2, GPT Image 2, Seedance).
You pay per generation instead of per month: a still is about 4 cents and a
10-second 1080p clip is about 70 cents.

Inspired by the RoboNuggets video
["This 1 Claude Skill fully replaces your Higgsfield Subscription"](https://www.youtube.com/watch?v=9C4TRbucmhQ).

## What it does

1. You type `/generate a POV of waking up as a Roman senator, vertical`.
2. Claude drafts a still on a cheap image model.
3. Claude looks at the still and runs a QA gate: size, orientation, no stray text, matches the brief.
4. Claude quotes the clip cost and your remaining budget, then waits for "go".
5. Claude animates the still, downloads the mp4, checks its real dimensions and duration, and logs the spend.

Spend caps (default $10 per day, $50 per month) are enforced by the script, and
every run is recorded in `generations/ledger.json`.

## Install

Requirements: Python 3.9+ (standard library only), a kie.ai API key with a few
dollars of credit. ffmpeg is optional (frame extraction for QA).

```bash
git clone https://github.com/ojayWillow/claude-generate-skill ~/.claude/skills/generate
```

On Windows PowerShell:

```powershell
git clone https://github.com/ojayWillow/claude-generate-skill "$env:USERPROFILE\.claude\skills\generate"
```

Then:

1. Copy `.env.example` to `.env` inside the skill folder.
2. Paste your key from https://kie.ai/api-key into `KIE_API_KEY=`.
3. In Claude Code, run `/generate` and describe what you want.

The same folder works in `~/.codex/skills/`, `~/.openclaw/skills/` and
`~/.hermes/skills/`.

## Layout

```
generate/
  SKILL.md                 the instructions Claude follows
  scripts/kie.py           CLI: credits, quote, image, upload, video, status, ledger, inspect
  scripts/dashboard.py     local dashboard server (stdlib) on http://127.0.0.1:8787
  scripts/pricing.json     editable cost estimates per model
  public/index.html        the dashboard page (served locally and on Vercel)
  api/index.py             Vercel function: same API backed by kie.ai + Vercel Blob
  vercel.json              function duration and /api/<op> rewrite
  models/*.md              one recipe per model (ids, fields, gotchas)
  presets/*.md             locked style fragments for consistent series
  .env.example             copy to .env
  generations/             outputs, sidecar JSON, ledger.json (git-ignored)
```

## Dashboard

A single page shows your kie.ai credit balance, today's and this month's spend
against the caps, a per-day spend chart, and a review board of every still and
clip. The create panel is built for iteration, not single shots:

- **Prompt builder.** Subject, setting, camera, lighting, mood, details (or
  camera move, subject motion, atmosphere, keep-still for video) compose the
  final prompt, with the chosen preset's locked fragment and a negative prompt.
  Presets live in `public/presets.json`.
- **Variations.** Generate 1 to 4 stills from one prompt at once. They show up
  as a set; "Pick" stars one and rejects the rest.
- **Reference images.** Upload faces or products once, select up to three per
  still, or use any board image as a reference for character consistency.
- **Review board.** Star, reject, note, search, filter to starred, hide or show
  rejected, select two and compare side by side, click to view large.
- **One-click follow-ups.** Animate, Tweak (reload into the builder), Use as
  ref, 16:9 and 1:1 reframes, a 2K or 4K version, and for clips Redo and Extend
  (captures the last frame in the browser and uses it as the next source still).
- **Draft then finalize for Seedance 2.5.** Clips are made as 480p drafts
  (28 credits per second, so about $1.40 per 10 s), one to four at a time.
  Press Finalize on the keeper and kie.ai re-renders that exact draft at 1080p
  (158 credits per second, about $7.90 per 10 s) with the same motion. Draft
  ids stay valid for 7 days.

It runs two ways from the same `public/index.html`:

**Locally** (reads your `generations/` folder, runs `kie.py` for you):

```bash
python scripts/dashboard.py --open
```

**On Vercel** (`api/index.py` + a Vercel Blob store, so outputs and the ledger
live in the cloud and the dashboard works from any device):

1. Import this repo into Vercel (framework "Other", output directory `public`).
2. Storage, create a Blob store and connect it to the project; this injects
   `BLOB_READ_WRITE_TOKEN`.
3. Settings, Environment Variables: add `KIE_API_KEY`, and optionally
   `SESSION_CAP_USD` and `MONTHLY_CAP_USD`.
4. Settings, Deployment Protection: turn on Vercel Authentication for all
   deployments, so only you can open a page that spends your credits.
5. Deploy. The browser polls `/api/tasks?id=` until kie.ai finishes, then the
   function copies the result into Blob and appends the ledger.

Both modes use the same API: `GET /api/state`, `POST /api/quote`,
`POST /api/generate` (with `count`, `refs`, `negative`, `fields`, `seed`),
`GET /api/tasks?id=`, `POST /api/item` (rating, note, hidden), `POST /api/upload`.

## Using the script directly

```bash
python scripts/kie.py credits
python scripts/kie.py image --prompt "a cozy cabin at dusk, no text" --aspect 9:16
python scripts/kie.py quote --kind video --model pro --duration 10
python scripts/kie.py video --image generations/cabin.png --prompt "snow falls, slow push-in" --approve
python scripts/kie.py ledger
```

Every command prints JSON. Exit code 2 means the run needs `--approve`, exit
code 3 means it would exceed a spend cap.

## Models

| Task | Model | kie.ai id |
|---|---|---|
| Image (default) | Nano Banana 2 | `nano-banana-2` |
| Image with readable text or UI | GPT Image 2 | `gpt-image-2-text-to-image` |
| Video (default) | Seedance 1.0 Pro | `bytedance/v1-pro-image-to-video` |
| Video (budget) | Seedance 1.0 Lite | `bytedance/v1-lite-image-to-video` |
| Video (on request) | Seedance 2.5 | `bytedance/seedance-2-5` |

Prices in `scripts/pricing.json` are estimates. The ledger stores the real
`creditsConsumed` that kie.ai reports after each job. Check
https://kie.ai/pricing when rates change.

## Publishing

The clip is a local mp4. To schedule it to social platforms, pair this skill
with a publishing tool such as the Blotato MCP server:

```bash
claude mcp add --transport http Blotato https://mcp.blotato.com/mcp
```

## License

MIT
