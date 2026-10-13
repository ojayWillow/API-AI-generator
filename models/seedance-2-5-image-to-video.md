# Seedance 2.5, image-to-video (ByteDance, via kie.ai)

Newest Seedance. Use only when the user explicitly asks for it. Different
request shape from 1.0.

- kie.ai model id: `bytedance/seedance-2-5`
- Script alias: `--model 2.5`
- Cost: per-second rate changes; `scripts/pricing.json` holds an estimate.
  Check the live 1080p rate on https://kie.ai/pricing before quoting and
  update the file if it differs.

## Input fields

| field | values | notes |
|---|---|---|
| `prompt` | string | short motion description |
| `first_frame_url` | single public URL | the approved still (not `image_url`, not an array) |
| `aspect_ratio` | `adaptive` | required for first-frame tasks; any other value returns 422 |
| `duration` | number (seconds) | e.g. `8`, sent as a number, not a string |
| `resolution` | `1080p` | API also accepts 480p and 720p; this skill does not use them |

## Script call

```
python scripts/kie.py video --image generations/<slug>.png --prompt "..." --model 2.5 --duration 8 --approve
```

## Notes

- Failed jobs cost 0 credits. Read `failMsg`, fix the input, resubmit with a fresh go.
- If Seedance 2.5 is unavailable on kie.ai, fall back to Pro and tell the user.
