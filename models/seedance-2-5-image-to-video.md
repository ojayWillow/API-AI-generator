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

## Script call

```
python scripts/kie.py video --image generations/<slug>.png --prompt "..." --model 2.5 --duration 8 --resolution 480p --approve
```

## Notes

- Failed jobs cost 0 credits. Read `failMsg`, fix the input, resubmit with a fresh go.
- If Seedance 2.5 is unavailable on kie.ai, fall back to Pro and tell the user.
