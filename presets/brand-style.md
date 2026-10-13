# Preset: brand-style

Template for a consistent channel look. Copy this file, rename it, and fill in
the blanks. The skill applies it whenever the user names the preset.

- aspect_ratio: 9:16
- still resolution: 2K
- image model: nano-banana-2 (switch to gpt-image-2 for frames with text)
- video model: pro, 10 s
- reference images (public URLs, max 10 MB each, up to 3):
  - 
  - 

## Locked prompt fragment

```
<describe the palette, lighting, lens, texture, era, and mood that every
frame must share. Example: muted teal and amber palette, soft overcast light,
50mm lens, shallow depth of field, film grain, clean minimal composition,
no text, no watermark>
```

## Locked motion fragment

```
<example: slow dolly-in, gentle ambient motion only, no fast moves, no cuts>
```

## Do not

- change the palette or lens between clips in a series
- add on-screen text unless using gpt-image-2 on purpose
