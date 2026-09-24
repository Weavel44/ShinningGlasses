# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A suite of standalone Python CLI tools to drive **Heaton "Shining Glasses"** LED eyewear
directly from a Windows PC over Bluetooth LE — no mobile app. The glasses speak the same
protocol as the **Shining Mask** (same manufacturer), reverse-engineered here from the
`GoneUp/mask-go` reference project and from HCI snoop captures.

- Display resolution: **36 columns × 12 px**. Text wider than 36 px auto-scrolls.
- No build/lint/test tooling — these are hardware-interaction scripts
  verified by watching the physical glasses.
- **Layout** (all at the repo root):
  - Primary tools live at the root: `glasses_studio.py` (recommended entry point) plus the
    CLIs for capabilities **not yet folded into the studio** — `glasses_text.py` (scrolling
    text), `glasses_audio.py` (real-time audio spectrum) — and `glasses_anim.py` (stored
    self-looping GIF via multi-image DATS; also exposed in the studio as the
    « Envoyer animation (boucle stockée) » button).
  - `tools/` — secondary standalone CLIs: `glasses_status.py` (diagnostics / LED test) plus
    scripts whose logic is now **subsumed by the studio** but which remain runnable on their
    own: `glasses_pixel.py`, `glasses_crop.py`, `glasses_gif.py`, `glasses_realtime.py`.
  - `archive/` — early / one-shot investigation scripts, superseded by the tools above:
    `glasses_ble.py`, `glasses_ctl.py`, `glasses_pixel_test.py`, `analyze_snoop.py`.
  - `assets/` — small synthetic test GIFs (`test_*.gif`).
  - `reference/` — `GattShinningGlasses.txt` (full GATT dump) and `apk/` (the decompiled
    official app `com.icwork.shiningglass`, source of the per-pixel format). **`reference/apk/`
    is git-ignored** (proprietary, ~108 MB) — it exists only locally; never commit it.
- Documentation (in French) is in `README.md` — the public GitHub-facing overview (quick start,
  tool reference, protocol summary, troubleshooting). Keep it in sync when adding tools.
- `.gitignore` also excludes three large third-party GIFs in `assets/`, `btsnoop_hci*.log`
  captures and generated preview PNGs.

## Running the tools

Every script is a **PEP 723 self-contained script**: dependencies are declared in the
`# /// script` header and installed automatically by `uv`. There is no `requirements.txt`,
`pyproject.toml`, or venv to manage — always run via `uv`:

```powershell
uv run <script>.py [args]          # uv fetches Python + deps on first run, then caches
```

Fallback if `uv` is unavailable (or Tkinter is missing under uv):
`pip install bleak pycryptodome pillow numpy soundcard` then `python <script>.py`.

The BLE device address (e.g. `AA:BB:CC:DD:EE:FF`) is the first positional arg for every
device-touching command. `0000fff0` service char `…9600` = commands, `…9601` = notify,
`…960a` = raw image data (see `reference/GattShinningGlasses.txt` for the full GATT dump).

**Only one BLE central may connect at a time** — the mobile app must be closed before any
script runs, or connections hang/fail.

### Command reference

```powershell
# Phase 1 — discovery / GATT mapping (archived — superseded by glasses_status.py --scan)
uv run archive/glasses_ble.py scan [-t 15]            # list BLE devices by RSSI
uv run archive/glasses_ble.py dump <addr>             # enumerate services/characteristics
uv run archive/glasses_ble.py monitor <addr> -d 60    # dump + log notifications

# Diagnostics — is the device alive / are the LEDs OK?
uv run tools/glasses_status.py --scan                 # is it powered on / advertising?
uv run tools/glasses_status.py <addr>                 # connect + read GATT (read-only)
uv run tools/glasses_status.py <addr> --test          # + visual LED test (full white/R/G/B)

# Phase 2 — direct commands (archived)
uv run archive/glasses_ctl.py <addr> light 200        # brightness 0-255
uv run archive/glasses_ctl.py <addr> image 0 | anim 3 # factory images/animations
uv run archive/glasses_ctl.py <addr> mode scroll-left # steady|blink|scroll-left|scroll-right
uv run archive/glasses_ctl.py <addr> color 255 0 0    # RGB (byte order unconfirmed)
uv run archive/glasses_ctl.py <addr> check            # count stored DIY images
uv run archive/glasses_ctl.py <addr> raw IMAG 02      # escape hatch: raw OP + hex args

# Phase 3 — upload custom text/image
uv run glasses_text.py <addr> --text "SALUT" [--color 255 0 0] [--mode scroll-left] [--speed 6]
uv run glasses_text.py <addr> --image logo.png
uv run glasses_text.py <addr> --text "X" --preview out.png   # render PNG, send NOTHING
uv run glasses_text.py <addr> --text "X" -v                  # verbose handshake trace

# Phase 4 — crop/resize an image to 36×12 (interactive GUI, live preview, integrated send)
uv run tools/glasses_crop.py [image.png] [<addr>]     # aspect-locked 3:1 crop, save PNG or send

# Phase 5 — per-pixel true color (SOLVED — format recovered from the decompiled app)
uv run tools/glasses_pixel.py <addr> --pattern        # 4-corner witness (proves per-pixel)
uv run tools/glasses_pixel.py <addr> --image logo.png # full RGB image, one color per pixel
uv run tools/glasses_pixel.py <addr> --image x.png --preview out.png   # render only, send NOTHING
# legacy probes (archived — superseded by glasses_pixel.py):
uv run archive/analyze_snoop.py btsnoop_hci.log       # decode an Android HCI capture
uv run archive/glasses_pixel_test.py list             # blind per-pixel format hypotheses

# Phase 6 — real-time audio spectrum
uv run glasses_audio.py <addr> [--color 0 255 0] [--gain 1.5]
uv run glasses_audio.py --list                        # list audio outputs

# Phase 7 — real-time DIY canvas (…960b) — ⚠️ can HANG the firmware, prefer Phase 8 for GIFs
uv run tools/glasses_realtime.py <addr> corners       # probe the 960b live channel
uv run tools/glasses_gif.py <addr> anim.gif [--fit cover] [--fps 12] [--loops 0]
uv run tools/glasses_gif.py X anim.gif --dry-run      # offline diff stats, no BLE

# Phase 8 — GIF as a STORED, self-looping animation (multi-image DATS upload — safe, persistent)
uv run glasses_anim.py <addr> anim.gif [--frames 20] [--snap] [--speed 6]
uv run glasses_anim.py X anim.gif --contact sheet.png   # numbered contact sheet to pick frames
uv run glasses_anim.py <addr> anim.gif --select 0,4,8,12,16   # send exactly these frames
uv run glasses_anim.py X anim.gif --dry-run             # offline stats, no BLE

# Unified GUI — does everything (BLE scan, crop, still image + GIF, per-pixel)
uv run glasses_studio.py [file]                       # all-in-one Tkinter app
```

`glasses_studio.py` is the **unified GUI** and the recommended entry point: BLE scan/select
(persistent connection on a background asyncio thread), interactive 3:1 crop, live 36×12 preview,
still image and animated GIF. **Crucial firmware quirk: the …960b live-DIY canvas has a short
inactivity TIMEOUT** — if no frames arrive for a few seconds the glasses revert to the factory
animation on their own (confirmed on hardware, connection stays up). The **DATS path does NOT time
out** (uploaded image persists like a factory image). So the studio splits the two: a **still image
is sent via DATS** (`payload_from_pixels`, type `0x01`) → persistent, no DIY, no timeout; a **GIF is
streamed on …960b** (enter DIY, per-frame diff) → the continuous stream keeps it alive, and on stop
the **last frame is re-sent via DATS** to freeze it durably. The **↻ Réafficher** button re-sends
the current frame via DATS to recover the display after a physical-button press (which, like a BLE
disconnect, drops the glasses back to the factory animation). GIF playback uses fast unacked diffs with periodic acked
**keyframes** (full repaints, default every 16 frames) to self-heal any dropped pixel, plus two
color-reduction levers that also shrink the per-frame diff: **posterize** (uniform bit-depth cut)
and **snap** (map each output pixel to the nearest color in a median-cut palette of the source —
kills Lanczos's invented edge gradients *and* makes neighbouring frames land on identical colors,
so far fewer pixels change). It subsumes `glasses_crop.py` (crop + still send), `glasses_gif.py`
(GIF diff player, also usable headless via `--dry-run`, and now with `--snap`/`--posterize`/
`--keyframe`/`--reliable`), and `glasses_realtime.py` (the 960b channel probe), which remain as
standalone CLIs.

### The real-time DIY channel (`glasses_realtime.py`, `glasses_gif.py`, `glasses_studio.py`)

Recovered from the app's `DiyActivity`/`LedViewDiy` and confirmed on hardware. Distinct from the
DATS upload: enter DIY with `SMVEW` (`0x01` enter / `0x00` exit / `0x02` exit+save, AES on …9600),
then write **raw, unencrypted** frames to **…960b**: `[len][R][G][B][col][row][col][row]…` with
`len = 3 + 2·npoints` (col 0-35, row 0-11; note the wire order is col-then-row). The firmware keeps
a **persistent canvas** and accepts **many points per frame** (not just 8), so GIF playback only
streams the per-frame diff, grouped by target color — see `diff_groups`/`groups_to_frames`.

Two reliability rules learned the hard way (silent black pixels otherwise): (1) a 960b frame must
fit the negotiated **MTU** — cap points at `(mtu - 7) // 2` (this is exactly why the app caps at 8:
default MTU 23 → 8 points); an over-MTU write-without-response fails **silently** and that pixel
stays black forever (our tracked state thinks it was painted). (2) write 960b with
**`response=True`** — the char is `[write-without-response,write]`, and acked writes guarantee
delivery (and self-pace). The first animation frame paints all 432 LEDs (init tracked state to
`None`, not black) so the display is fully correct regardless of prior canvas contents.

## Architecture

The critical thing to understand: **there is no shared library.** The Shining Mask protocol
primitives are **copy-pasted into each script** (`glasses_ctl.py`, `glasses_text.py`,
`glasses_audio.py`, `glasses_pixel.py`, `glasses_crop.py`, `glasses_realtime.py`, `glasses_gif.py`,
`glasses_studio.py`, `glasses_pixel_test.py`, `analyze_snoop.py`). This is deliberate — each
file must stay runnable in isolation via `uv run` with its own dependency header. When you
change a protocol detail, **you must replicate the edit across every script that defines it**.

The duplicated primitives, identical everywhere:

- `KEY = 32672f7974ad43451d9c6c894a0e8764` — static AES-128-ECB key.
- `enc_cmd(op, args)` — builds a command frame `[len(op)+len(args)] + op(ASCII) + args`,
  zero-padded to a 16-byte multiple, then AES-ECB encrypted. Written to `…9600`.
- `dec_notify(data)` — decrypts a `…9601` notification and extracts the `[len][ASCII]` reply.
- `CMD_CHAR` / `NOTIFY_CHAR` / `DATA_CHAR` UUIDs and `MAX_PACKET=100`, `PAD=16`.

### The image/text upload handshake (`glasses_text.py`, `glasses_audio.py`, `glasses_pixel_test.py`)

This is the core protocol, ported byte-for-byte from mask-go. The payload is
**`bitmap_bytes + color_bytes`**:

1. **Bitmap**: one on/off value per pixel, packed column-major, `ceil(H/8)` bytes per column,
   top row = MSB (`encode_bitmap`).
2. **Colors**: two formats exist, selected by the 5th DATS byte:
   - **per-column** (`0x00`): one RGB triple per column (36 total). Used by `glasses_text.py` /
     `glasses_audio.py`. Vertical gradients collapse to one color per column.
   - **per-pixel** (`0x01`): one RGB triple per pixel (36×12 = 432 total), same column-major
     order as the bitmap (`index = col*12 + row`). Used by `glasses_pixel.py`. This is the DIY
     format the official app uses — **recovered by decompiling the app** (`com.icwork.shiningglass`,
     class `DiyAgreement`), confirmed on hardware. The DIY flow sends **no MODE command** after
     `DATCP` (sending one reverts the glasses to a factory preset).

Upload sequence, each step gated on a decrypted notification:

```
DATS (total_len:u16be, bitmap_len:u16be, 0x00)  →  wait "DATSOK"
for each ~98-byte chunk [len+1][idx][data] on …960a  →  wait "REOK"
DATCP                                            →  wait "DATCPOK"
SPEED (optional), then MODE                      # set scroll/steady behaviour
```

`glasses_audio.py` runs this loop continuously (audio captured on a background thread →
FFT → 36 bars → payload), so the BLE handshake round-trip is the FPS bottleneck (~5–10 fps).

### `analyze_snoop.py` — offline, no BLE

Parses an Android `btsnoop_hci.log`, walks HCI→ACL→L2CAP→ATT framing, decrypts GATT writes to
recover `DATS`/`MODE`/`FC`/etc. in clear, and from `DATS`'s `total_len`/`bitmap_len` **infers
whether the app's color format is per-column or per-pixel**. It is the analysis half of the
per-pixel-color investigation that `glasses_pixel_test.py` probes live.

## Conventions

- **French** is the language of all user-facing strings, docstrings, comments, and error
  messages. Match it when editing.
- Scripts print timestamped, hex-dumped protocol traces (`ts()`, `hexdump()`); keep that style
  for new protocol work — it is the primary debugging surface for a hardware protocol with no
  other observability.
- Prefer adding a `--preview`-style dry-run path for anything that computes a payload, so
  rendering can be verified without the glasses present.
