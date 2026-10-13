# Seedance 2.5, image-to-video (ByteDance, via kie.ai)

Newest Seedance. Use only when the user explicitly asks for it. Different
request shape from 1.0.

- kie.ai model id: `bytedance/seedance-2-5`
- Script alias: `--model 2.5`
- Cost (kie.ai beta rates, image or text input, per generated second):
  480p 28 credits ($0.14), 720p 63 credits ($0.315), 1080p 158 credits ($0.79).
  A 10-second 1080p clip is about $7.90, ten times a Seedance 1.0 Pro clip.
  With a reference video as input the rates are lower but billed on input
  plus output seconds. Verify at https://kie.ai/seedance-2-5
- Workflow that keeps cost sane: draft at 480p (`--resolution 480p`, about
  $1.40 per 10 s), pick the keeper, re-run only that one at 1080p.

## Input fields

| field | values | notes |
|---|---|---|
| `prompt` | string | short motion description |
| `first_frame_url` | single public URL | the approved still (not `image_url`, not an array) |
| `aspect_ratio` | `adaptive` | required for first-frame tasks; any other value returns 422 |
| `duration` | number (seconds) | e.g. `8`, sent as a number, not a string |
| `resolution` | `480p`, `720p`, `1080p` | default 480p for drafts; pass `--resolution 1080p` for the final |

| `draft` | bool | 480p only. The resulting task id can be rendered at 1080p for 7 days |
| `generate_audio` | bool | default true; synchronized sound. `--no-audio` turns it off |

## Draft, then final (the normal loop)

```
python scripts/kie.py video --image generations/<slug>.png --prompt "..." --model 2.5 --draft --duration 10 --approve
python scripts/kie.py finalize --draft-task-id <task_id from the draft sidecar> --approve
```

`finalize` calls model `bytedance/seedance-2-5-draft` with
`{"draft_task_id": ..., "resolution": "1080p", "output_format": "mp4"}`. No
prompt or duration is sent; the draft's settings are reused, so the motion is
identical. kie.ai publishes no separate rate for this render; the skill quotes
the 1080p rate (158 credits per second) and the ledger stores the real charge.

## Direct render (skips the draft)

```
python scripts/kie.py video --image generations/<slug>.png --prompt "..." --model 2.5 --duration 8 --resolution 720p --approve
```

## Notes

- Failed jobs cost 0 credits. Read `failMsg`, fix the input, resubmit with a fresh go.
- If Seedance 2.5 is unavailable on kie.ai, fall back to Pro and tell the user.
