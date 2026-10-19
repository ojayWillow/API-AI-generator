# Kling 3.0 (Kuaishou, via kie.ai)

The character model. It is the only model on kie.ai that takes a **start
frame, an end frame and saved characters in one request**, so a face stays
locked while the shot lands exactly on the end still. Seedance 2.5 cannot mix
frames with characters.

- kie.ai model id: `kling-3.0/video` (same createTask / recordInfo flow as Seedance 2.5)
- Script alias: `--model kling-3.0`
- Cost per second (kie.ai, https://kie.ai/kling-3-0):

  | resolution | `mode` | no sound | with sound |
  |---|---|---|---|
  | 720p | `std` | 14 cr ($0.07) | 20 cr ($0.10) |
  | 1080p | `pro` | 18 cr ($0.09) | 27 cr ($0.135) |
  | 4K | `4K` | 67 cr ($0.335) | 67 cr ($0.335) |

  A 10 s 1080p clip with sound is about $1.35. The same clip as a Seedance 2.5
  1080p final is about $7.90.
- No draft mode and no Finalize. 720p is the cheap try; re-run the keeper at 1080p.
- No seed or camera-fixed option.

## Input fields

| field | values | notes |
|---|---|---|
| `prompt` | string, up to 2,500 chars | characters are addressed as `@name` |
| `image_urls` | 1 or 2 public URLs | 1 = start frame, 2 = start + end frame. Output takes the start frame's aspect ratio |
| `kling_elements` | up to 3 objects | `{name, description, element_input_urls}`; 2-4 photos (JPG/PNG, max 10 MB each) or 1 clip of 3-8 s |
| `aspect_ratio` | `9:16`, `16:9`, `1:1` | only when there is no start frame (Cast mode) |
| `duration` | `"3"` to `"15"` | a string |
| `mode` | `std`, `pro`, `4K` | the script maps 720p / 1080p / 4K to these |
| `sound` | bool | required; sound costs more in std and pro |
| `multi_shots` | bool | required; the script sends `false`. Multi-shot (up to 5 shots, start frame only) is not wired in yet |

## How the script maps a saved character

- `name` is the character's `@tag` with anything that is not a letter, digit
  or underscore turned into `_` (so `@anna-b` becomes `@anna_b` in the prompt).
- `description` is `Name: identity line`.
- `element_input_urls` is the first 4 photos. **A character needs at least 2
  photos for Kling**; the script stops with a clear error otherwise.
- If the prompt never mentions the tag, the script appends `Featuring @name.`

## Modes

| mode | sends | use it for |
|---|---|---|
| `first` | `image_urls: [start]` + characters | continue a shot from its last frame with the faces locked |
| `first-last` | `image_urls: [start, end]` + characters | the shot lands on an exact end still with the faces locked |
| `reference` | `aspect_ratio` + characters, no frame | a character in a fresh shot |

Kling takes no loose reference images and no reference clips; use Seedance
2.5 Cast mode for those.

## Caveats

- kie.ai's schema allows two frames plus `kling_elements`, but none of its
  examples send all three together. Test once at 720p (about $0.50 for 5 s)
  before a big batch.
- kie.ai may return `lastFrameUrl` in the result. The script saves it as
  `<clip>.last.png` when present. If it is missing, the dashboard captures the
  last frame in the browser.
- Small details (scars, jewelry) can drift over long chains of clips. Re-anchor
  with a fresh end still every few shots.
