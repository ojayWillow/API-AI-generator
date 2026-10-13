# Preset: app-demo

"Problem, then solution" clips where an app on a phone screen is the fix.
Frames contain readable UI, so the still uses gpt-image-2.

- aspect_ratio: 9:16
- still resolution: 2K (text must stay sharp)
- image model: gpt-image-2
- video model: pro, 5 s (short, punchy)
- reference images: none (gpt-image-2 text-to-image takes none)

## Locked prompt fragment

```
over-the-shoulder shot of a hand holding a modern smartphone, screen fills the
lower two thirds of the frame, clean app interface with large readable type,
soft studio light, vertical 9:16, no watermark
```

Put the exact on-screen copy in quotes inside the prompt, for example:
`the screen shows a card that reads "Scheduled for 5:27 PM"`.

## Locked motion fragment

```
camera fixed, thumb taps the screen once, subtle screen glow change, keep all
text readable, no camera movement
```

Pass `--camera-fixed` on the video call for this preset.

## Four story formats

1. Problem then solution: frame 1 the mess, frame 2 the app fixing it.
2. Before and after: same framing, two stills, two clips.
3. "5 tools for X" countdown: five stills, your product last.
4. Relatable short story: one POV still, 10 s, caption carries the story.
