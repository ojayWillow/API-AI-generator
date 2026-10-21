# MiniMax H3 (via kie.ai)

The model for bringing a still to life. It was #1 for image-to-video on both
Artificial Analysis and LMArena in October 2026, and kie.ai sells it at half
of MiniMax's own price. Stereo sound is always on.

- kie.ai model id: `minimax-h3/image-to-video` (same createTask / recordInfo flow as Seedance 2.5)
- Script alias: `--model minimax-h3`
- Cost per second (kie.ai, https://kie.ai/minimax-h3), sound included:

  | resolution | credits | USD | 10 s clip |
  |---|---|---|---|
  | 768p | 8 | $0.04 | $0.40 |
  | 2K | 13 | $0.065 | $0.65 |

- No draft mode and no Finalize. 2K is the default because it costs so little more.

## Input fields

| field | values | notes |
|---|---|---|
| `prompt` | string | |
| `first_frame_url` | public URL | start frame; the output takes its aspect ratio |
| `last_frame_url` | public URL | optional end frame |
| `duration` | 4-15 (integer) | |
| `resolution` | `768P`, `2K` | the script upper-cases `--resolution` |

There is no aspect ratio, seed, camera-fixed or sound field.

## Modes

| mode | sends | use it for |
|---|---|---|
| `first` | `first_frame_url` | animate a still |
| `first-last` | `first_frame_url` + `last_frame_url` | the shot lands on an exact end still |

H3 takes **no characters, reference images or clips**. To keep a face, put the
person into the start (and end) still, or use Kling 3.0. kie.ai lists a
separate 9-image reference-to-video endpoint, but publishes no model id for it
yet, so the script does not use it.

## Caveats

- In one song test it invented a section of music. Mute the clip and lay the
  real track on top when the audio matters.
- kie.ai does not return a last-frame URL. The dashboard captures the last
  frame in the browser for "Extend from last frame".
