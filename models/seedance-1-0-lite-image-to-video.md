# Seedance 1.0 Lite, image-to-video (ByteDance, via kie.ai)

Budget option. Same request format as Pro, lighter motion quality. Good for
tests, drafts, or high-volume background clips.

- kie.ai model id: `bytedance/v1-lite-image-to-video`
- Script alias: `--model lite`
- Cost: 10 credits per second at 1080p. 5 s = 50 credits ($0.25), 10 s = 100 credits ($0.50).

## Input fields

Identical to Pro: `prompt`, `image_url`, `resolution` (`1080p`), `duration`
(`"5"` or `"10"`), `camera_fixed`, `seed`. No aspect ratio; the still decides.

## Script call

```
python scripts/kie.py video --image generations/<slug>.png --prompt "..." --model lite --duration 5 --approve
```

Use Lite when the user asks for cheap, draft, test, or budget. Otherwise
default to Pro.
