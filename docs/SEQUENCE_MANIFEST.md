# Sequence manifest

Every multi-shot run (focus stack, bracket, intervalometer, burst, bulb) writes one JSON manifest. The camera can't receive files over Bluetooth, so the manifest is how a photo pipeline learns which images on the card belong together. [phototag](https://github.com/enzomaruffa/phototag) reads it to build stacks.

## Where it is written

- `Documents/Sequences/` in the app container, visible in the Files app under On My iPhone → Open Alpha Link → Sequences. Always written. Each History row can also share the file, for AirDrop.
- Pushed to `phototag receive` on a Mac on the same network, once the app is paired with it. Nothing goes through the internet or iCloud.

### Push to phototag over the local network

`phototag receive` serves HTTP on the LAN (port 8765 by default, or a free port when that one is taken) and advertises it over Bonjour as `_phototag._tcp`, named `phototag on <host>`, with TXT records `v=1` and `path=/v1`. The app browses for that service and resolves it, so the port never has to be typed.

On first run phototag generates a pairing code (6 groups of 4 letters and digits) and prints it with a QR code of `phototag://pair?code=<code>&port=<port>`. Scanning it with the iPhone Camera opens the app and pairs it; the code can also be typed under Mac Sync. Every request except `/healthz` carries `Authorization: Bearer <code>`. Dashes, spaces and case don't matter.

| Route | What it does |
| --- | --- |
| `GET /healthz` | `{"ok": true}`. No code needed. |
| `GET /v1/manifests` | `{"manifests": [{"id": "<id>", "sha256": "<hex>"}]}`, one entry per manifest held. `sha256` is of the stored bytes. |
| `PUT /v1/manifests/{id}` | The manifest file's bytes as the body, at most 1 MB. phototag checks `format`, `version` and that `id` matches the path, then writes the body as-is. `201` when new, `200` when updated or unchanged, with `{"id", "status": "created" \| "updated" \| "unchanged", "sha256"}`. |

Errors: `401` for a missing or wrong code, `400` for a path that isn't a UUID or a body that isn't JSON, `413` over 1 MB, `422` for anything that isn't a version 1 manifest for that id.

The app lists first and PUTs every manifest whose SHA-256 differs from what the receiver holds, so an edit to the annotations is sent again and an unchanged manifest isn't. It remembers what each receiver acknowledged, and History shows each run as Sent to <Mac>, Waiting for phototag or Not paired. It syncs on launch, when it comes to the foreground, after every saved run and after every annotation edit, whenever a receiver is visible.

File name: `<local start yyyyMMdd-HHmmss>-<kind>-<first 8 chars of id>.json`, for example `20261031-140322-focus_stack-3f9a1c2e.json`. The manifest is written when the run ends (completed, cancelled or failed). It is rewritten in place, under the same name and `id`, only when the user edits its `annotations` afterwards; the frames never change. Readers key on `id` and keep the newest content.

## Format (version 1)

```json
{
  "format": "open-alpha-link.sequence",
  "version": 1,
  "id": "3F9A1C2E-8B1D-4C6A-9E0F-2A7B5C4D3E21",
  "kind": "focus_stack",
  "outcome": "completed",
  "app_version": "0.1 (1)",
  "time_zone": "America/Sao_Paulo",
  "started_at": "2026-10-31T17:03:22.104Z",
  "ended_at": "2026-10-31T17:04:10.877Z",
  "camera": {
    "name": "Travel A7 III",
    "model": "ILCE-7M3"
  },
  "plan": {
    "frames": 24,
    "direction": "in",
    "step": 16,
    "nudge_ms": 100,
    "settle_ms": 1500,
    "autofocus": false
  },
  "expected_images": 24,
  "annotations": {
    "title": "Hornero nest, macro",
    "notes": "Window light from the left, 2x macro tube",
    "subject": "Rufous hornero nest",
    "tags": ["macro", "nature"],
    "album": "Macro 2026",
    "lens": "Laowa 100mm f/2.8 2x Ultra Macro APO"
  },
  "frames": [
    {
      "index": 1,
      "pressed_at": "2026-10-31T17:03:22.210Z",
      "released_at": "2026-10-31T17:03:22.490Z",
      "shutter_confirmed": true,
      "ready_confirmed": true
    }
  ]
}
```

| Field | Meaning |
| --- | --- |
| `format`, `version` | Always `open-alpha-link.sequence` and `1`. Readers reject other values. |
| `id` | UUID, uppercase, unique per run. |
| `kind` | `focus_stack`, `exposure_bracket`, `intervalometer`, `burst` or `bulb`. |
| `outcome` | `completed`, `cancelled` or `failed`. A cancelled run still lists the frames it shot. |
| `time_zone` | The phone's IANA zone at the start. Sony bodies write `DateTimeOriginal` as local wall-clock time with no offset, so readers need this to compare. |
| `started_at`, `ended_at` | UTC, ISO 8601 with milliseconds and `Z`. Phone clock. |
| `camera.name` | The name the user sees in the app. |
| `camera.model` | The Bluetooth model string (`ILCE-7M3`), which matches EXIF `Model`. `null` when unknown. |
| `plan` | The tool's settings. Keys depend on `kind`. Informational only. |
| `expected_images` | How many images the camera should have written, or `null` when the app can't know (a held burst). A held bracket is one press that writes `plan.frames` images. |
| `annotations` | What the user typed on the phone. Always present; every field is optional (`null` or `[]`). Entered before the run or edited afterwards from History. |
| `annotations.title` | Short name for the set. Readers write it as the image title. |
| `annotations.notes` | Free text. Readers append it to the image description. |
| `annotations.subject` | What was photographed, in the user's words. Readers pass it to any AI analysis as a hint and keep it in the description. |
| `annotations.tags` | Keywords, lowercase. Readers write them as-is, without any review step. |
| `annotations.album` | Album to add the images to in the photo library. |
| `annotations.lens` | Lens name. Manual and adapted lenses report nothing over EXIF, so readers write it to `LensModel` only when the image has no lens model. |
| `frames` | One entry per shutter press, in order. `pressed_at` is when the full press was sent, `released_at` when it was released. Both UTC, phone clock. |
| `shutter_confirmed`, `ready_confirmed` | Whether the camera's status notifications confirmed the exposure and the return to ready. An unconfirmed frame may be missing on the card. |

## Matching photos to a manifest

Timestamps come from the phone. The camera clock is set by hand and usually drifts by seconds to minutes, so a reader has to look for the best clock offset rather than compare times directly.

1. Convert each frame's `pressed_at`/`released_at` to local wall-clock time in `time_zone`.
2. Candidates are photos whose EXIF `Model` equals `camera.model` (skip this filter when it is `null`), with a capture time within `started_at − max_skew` to `ended_at + max_skew`. Use `SubSecTimeOriginal` when present.
3. For each candidate offset in `[−max_skew, +max_skew]`, a frame matches a photo when the photo's time shifted by the offset falls within `[pressed_at − 1.5 s, released_at + 1.5 s]`. A RAW+JPEG pair, or several images inside one held press, all match the same frame.
4. Pick the offset range that matches the most frames, then the one that places the most images, then the one closest to zero. Report the middle of that range: with whole-second EXIF times, every offset in it is equally likely.
5. Accept the match when at least 80% of the frames matched. A held press counts as matched only if it gained at least one image.

## What a reader writes into each matched image

- The annotations above.
- Keywords for the kind (`focus_stack`, `exposure_bracket`, `timelapse` for an intervalometer, `burst`, `bulb`) and `stack_<first 8 chars of id, lowercase>`.
- The image's position in the set (image number / image count, so a held bracket reads `3/5` even though it is one press) and the plan in `UserComment`, for example `focus_stack 7/24, in, step 16, nudge 100 ms, settle 1500 ms`. Grouping uses the `stack_` keyword, because no EXIF or XMP field for it is portable across photo libraries.

EXIF times have whole-second resolution without sub-seconds, so a 1.5 s tolerance is the floor. Two sequences shot back to back can only be told apart if the gap between them is larger than the skew window.
