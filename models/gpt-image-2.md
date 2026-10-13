# gpt-image-2 (OpenAI, via kie.ai)

Use only when the frame must contain readable text: a sign, poster, packaging,
phone screen, app UI mockup, caption card. Strongest in-image text rendering.

- kie.ai model id: `gpt-image-2-text-to-image`
- Estimated cost: about 10 credits ($0.05) per image at medium quality.
  Verify at https://kie.ai/pricing

## Input fields

| field | values | notes |
|---|---|---|
| `prompt` | string | put any on-screen copy in quotes so it renders exactly |
| `aspect_ratio` | `9:16`, `16:9`, `1:1` | `9:16` for vertical |
| `resolution` | `1K`, `2K`, `4K` | use `2K` so text stays sharp after animation |

No reference image input on the text-to-image endpoint.

## Script call

```
python scripts/kie.py image --prompt "..." --model gpt-image-2 --aspect 9:16 --resolution 2K
```

## Prompting tips

- Quote the exact text: `a wooden sign that reads "OPEN 24/7"`.
- Keep text short. Five words render reliably; a paragraph does not.
- Describe the surface the text sits on (screen, paper, neon tube) so the
  model picks a believable typeface.
