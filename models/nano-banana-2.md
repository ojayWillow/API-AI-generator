# nano-banana-2 (Google, via kie.ai)

Default image model. Fast, cheap, strong with reference images. Use for every
frame that does not need readable text.

- kie.ai model id: `nano-banana-2`
- Estimated cost: 1K about 8 credits ($0.04), 2K about 10 credits ($0.05).
  Verify at https://kie.ai/pricing
- Endpoint: `POST https://api.kie.ai/api/v1/jobs/createTask` (the script does this)

## Input fields

| field | values | notes |
|---|---|---|
| `prompt` | string | full scene description |
| `image_input` | array of public URLs | reference images; empty for text-only. Max 10 MB each. Local paths are uploaded automatically by `--ref` |
| `aspect_ratio` | `9:16`, `16:9`, `1:1`, `4:5`, `auto` | `9:16` for vertical social |
| `resolution` | `1K`, `2K`, `4K` | `1K` for drafts, `2K` for finals |
| `output_format` | `png`, `jpg` | `png` default |

## Script call

```
python scripts/kie.py image --prompt "..." --model nano-banana-2 --aspect 9:16 --resolution 1K --ref https://host/ref.jpg
```

## Prompting tips

- Lead with the subject, then setting, lighting, camera (lens, angle, POV),
  mood, and style. End with "no text, no watermark, no logo".
- For a POV series, say "first-person POV, hands visible" and keep that exact
  phrase in the preset.
- Describe one clear moment. A still that is busy becomes a clip that is busy.

## Gotchas

- If kie.ai returns "model not found", the id changed. Check the kie.ai model
  page and update `scripts/pricing.json`.
- Result URLs are temporary. The script downloads the file immediately. For
  animation, the script re-uploads the local file rather than passing the
  temporary kie URL, which can fail with an unsupported-image error.
