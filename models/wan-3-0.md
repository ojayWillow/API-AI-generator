# Wan 3.0 (Alibaba, via kie.ai)

The everyday video model. It takes the same three modes as Seedance 2.5 (start
frame, start + end, or Cast with references and a clip) for about a fifth of
the price, and it was #1 on the Artificial Analysis text-to-video leaderboard
in October 2026. Audio is included in the price.

- kie.ai model id: `wan/3-0-video` (same createTask / recordInfo flow as Seedance 2.5)
- Script alias: `--model wan-3.0`
- Cost per second (kie.ai, https://kie.ai/wan3.0-video), audio included:

  | resolution | credits | USD | 10 s clip |
  |---|---|---|---|
  | 480p | 8 | $0.04 | $0.40 |
  | 720p | 16 | $0.08 | $0.80 |
  | 1080p | 32 | $0.16 | $1.60 |

  The same 10 s as a Seedance 2.5 1080p final is about $7.90.
- With a reference clip, kie.ai bills the clip's seconds plus the new seconds at
  the same rate. Quote with `--ref-video-seconds`.
- No draft mode and no Finalize. 720p is the cheap try; re-run the keeper at 1080p.

## Input fields

| field | values | notes |
|---|---|---|
| `prompt` | string | references are named `Image1`, `Image2`... and `Video1`, **without `@`** |
| `first_frame_url` | public URL | start frame. JPEG/PNG/WebP/BMP, 240-8000 px a side, max 20 MB, no transparent PNG |
| `last_frame_url` | public URL | end frame; needs a start frame |
| `reference_image_urls` | up to 10 URLs | the Cast: character photos first, then loose references |
| `reference_video_urls` | up to 5 clips | each 1-15 s, 15 s in total, MP4/MOV, 240-4096 px a side, max 100 MB |
| `resolution` | `480P`, `720P`, `1080P` | the script upper-cases `--resolution` |
| `aspect_ratio` | `9:16`, `16:9`, `1:1`, `4:3`, `3:4`, `adaptive` | the script sends `adaptive` with frames, so the still decides |
| `duration` | 2-30 (integer) | the clip's seconds plus the new seconds must stay within 30 |
| `audio` | bool | sound is free; `--no-audio` turns it off |
| `seed` | integer | optional |

Frames and references cannot be mixed in one request, the same rule as
Seedance 2.5. Kling 3.0 is still the only model that takes both.

## Modes

| mode | sends | use it for |
|---|---|---|
| `first` | `first_frame_url` | animate a still, or continue a shot from its last frame |
| `first-last` | `first_frame_url` + `last_frame_url` | the shot lands on an exact end still |
| `reference` | `reference_image_urls` / `reference_video_urls` + `aspect_ratio` | characters in a fresh shot, or extend a clip with its cast (`Continue Video1 seamlessly...`) |

## Strengths and caveats (independent tests, Oct 2026)

- Keeps faces and clothes stable through motion; the best physics of the top group.
- Accepted a photoreal person as a start frame that Seedance 2.5 refused.
- Sometimes adds cuts nobody asked for. Write "one continuous shot, no cuts" in the prompt.
- Lip-sync to songs was weak in one test. kie.ai also takes `reference_audio_urls`
  (up to 15 s, named `Audio1`); the script does not send it yet.
- kie.ai does not return a last-frame URL. The dashboard captures the last
  frame in the browser for "Extend from last frame".
