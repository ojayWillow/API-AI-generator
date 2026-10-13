# Seedance 1.0 Pro, image-to-video (ByteDance, via kie.ai)

Default video model. Animates an approved still into a 5 or 10 second 1080p clip.

- kie.ai model id: `bytedance/v1-pro-image-to-video`
- Script alias: `--model pro`
- Cost: 14 credits per second at 1080p. 5 s = 70 credits ($0.35), 10 s = 140 credits ($0.70).
  Billed only on success; validation errors (400, 402, 422) cost nothing.

## Input fields

| field | values | notes |
|---|---|---|
| `prompt` | string | short motion description |
| `image_url` | single public URL | the approved still. The script uploads local files to kie.ai's host (24 h) |
| `resolution` | `1080p` | the skill never goes below 1080p |
| `duration` | `"5"` or `"10"` (string) | default 10. Other values are rounded: 7.5 and above becomes 10, below becomes 5. The script reports the rounding |
| `camera_fixed` | bool | default false. `--camera-fixed` locks the camera |
| `seed` | int | -1 for random |

No `aspect_ratio` field. A 1080x1920 still produces a 1080x1920 clip.

## Script call

```
python scripts/kie.py video --image generations/<slug>.png --prompt "slow push-in, steam rising, soft hand movement" --model pro --duration 10 --approve
```

## Motion prompt tips

- One camera move plus one subject motion plus one atmospheric element.
- Say what should stay still: "keep the sign readable", "face stays in frame".
- Do not re-describe the scene; the still already carries it.
- Keep the POV identical to the still.

## Polling

The script polls `GET /api/v1/jobs/recordInfo?taskId=...` every 15 s. States:
`waiting`, `queuing`, `generating`, `success`, `fail`. On success the mp4 is at
`resultJson.resultUrls[0]` and `creditsConsumed` holds the real charge.
