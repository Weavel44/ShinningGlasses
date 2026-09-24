#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "bleak>=0.22",
#     "pycryptodome>=3.20",
#     "pillow>=10.0",
# ]
# ///
"""
glasses_anim.py — Upload un GIF comme ANIMATION STOCKÉE dans les lunettes Heaton (boucle autonome).

Approche sûre et persistante, décodée de l'appli officielle (classe DiyMutiAgreement, écran
« sélection multi-images ») : au lieu de streamer en temps réel sur …960b (canal qui a un timeout
firmware ET peut figer les lunettes), on TÉLÉVERSE chaque frame comme une image DIY stockée, via le
chemin DATS fiable, puis on demande aux lunettes de les ENCHAÎNER en boucle. L'animation tourne alors
en autonomie sur les lunettes (probablement même après déconnexion), sans aucun flux temps réel.

Protocole (tout chiffré sur …9600, données sur …960a, chaque étape acquittée sur …9601) :
    MANY [count][0x01]                        -> "MANYOK"
    pour chaque frame (max 20) :
        DATS [total:u16be][bitmap:u16be][0x01] -> "DATSOK"
        chunks [len+1][idx][data] (98 o)        -> "REOK" (chacun)
        DATCP                                    -> "DATCPOK"   (image stockée)
    MANCPOK                                    -> fin de la série
    SPEED [v] (option) ; PLAY                  -> lecture en boucle des images stockées

    uv run glasses_anim.py AA:BB:CC:DD:EE:FF anim.gif [--frames 20] [--speed 6] [--snap]
    uv run glasses_anim.py AA:BB:CC:DD:EE:FF anim.gif --fit contain --threshold 40
    uv run glasses_anim.py X anim.gif --dry-run     # hors-ligne : frames échantillonnées, taille, durée

⚠️  Max 20 frames (limite de l'appli). Un GIF plus long est échantillonné uniformément.
⚠️  Ferme l'appli mobile avant : une seule connexion BLE à la fois.
"""

import argparse
import asyncio
import sys
from datetime import datetime

try:
    from PIL import Image, ImageSequence, ImageDraw
except ImportError:
    sys.exit("pillow manquant : utilise 'uv run'")
try:
    from Crypto.Cipher import AES
except ImportError:
    sys.exit("pycryptodome manquant : utilise 'uv run'")

OUT_W, OUT_H = 36, 12
NPIX = OUT_W * OUT_H
MAX_FRAMES = 20
CMD_CHAR = "d44bc439-abfd-45a2-b575-925416129600"
NOTIFY_CHAR = "d44bc439-abfd-45a2-b575-925416129601"
DATA_CHAR = "d44bc439-abfd-45a2-b575-92541612960a"
KEY = bytes.fromhex("32672f7974ad43451d9c6c894a0e8764")
MAX_PACKET, PAD, DIY_TYPE = 100, 16, 0x01
BLACK = (0, 0, 0)


def ts():
    return datetime.now().strftime("%H:%M:%S")


# --- protocole ---
def enc_cmd(op, args=b""):
    body = bytes([len(op) + len(args)]) + op.encode("ascii") + args
    body += b"\x00" * ((PAD - (len(body) % PAD)) % PAD)
    return AES.new(KEY, AES.MODE_ECB).encrypt(body)


def dec_notify(data):
    data = bytes(data)
    if not data or len(data) % 16:
        return ""
    clear = AES.new(KEY, AES.MODE_ECB).decrypt(data)
    n = clear[0]
    return clear[1:1 + n].decode("ascii", "replace") if 1 + n <= len(clear) else ""


def many_cmd(count):
    return enc_cmd("MANY", bytes([count & 0xFF, 0x01]))          # {6,'M','A','N','Y',count,1}


def mancpok_cmd():
    return enc_cmd("MANCPOK")                                    # {7,'M','A','N','C','P','O','K'}


def play_cmd():
    return enc_cmd("PLAY")                                       # {4,'P','L','A','Y'}


def speed_cmd(v):
    return enc_cmd("SPEED", bytes([v & 0xFF]))                   # {6,'S','P','E','E','D',v}


def build_dats(total_len, bitmap_len):
    args = total_len.to_bytes(2, "big") + bitmap_len.to_bytes(2, "big") + bytes([DIY_TYPE])
    return enc_cmd("DATS", args)


def payload_from_pixels(pixels):
    """pixels : NPIX (r,g,b) en ordre colonne-major (index = col*OUT_H + row) -> bitmap+couleurs."""
    bitmap = bytearray()
    for x in range(OUT_W):
        b0 = b1 = 0
        for y in range(OUT_H):
            if pixels[x * OUT_H + y] != BLACK:
                if y < 8:
                    b0 |= 1 << (7 - y)
                else:
                    b1 |= 1 << (7 - (y - 8))
        bitmap += bytes([b0, b1])
    colors = bytearray()
    for c in pixels:
        colors += bytes([c[0] & 0xFF, c[1] & 0xFF, c[2] & 0xFF])
    return bytes(bitmap) + bytes(colors), len(bitmap)


def chunk_packets(payload):
    maxd = MAX_PACKET - 2
    out, i, idx = [], 0, 0
    while i < len(payload):
        data = payload[i:i + maxd]
        out.append(bytes([len(data) + 1, idx]) + data)
        i += len(data)
        idx += 1
    return out


# --- rendu du GIF ---
def fit_36x12(img, mode, resample):
    tw, th = OUT_W, OUT_H
    if mode == "stretch":
        return img.resize((tw, th), resample)
    iw, ih = img.size
    scale = (max if mode == "cover" else min)(tw / iw, th / ih)
    nw, nh = max(1, round(iw * scale)), max(1, round(ih * scale))
    r = img.resize((nw, nh), resample)
    if mode == "cover":
        left, top = (nw - tw) // 2, (nh - th) // 2
        return r.crop((left, top, left + tw, top + th))
    out = Image.new("RGB", (tw, th), BLACK)
    out.paste(r, ((tw - nw) // 2, (th - nh) // 2))
    return out


def frame_pixels(img36, threshold):
    px = img36.load()
    out = []
    for x in range(OUT_W):
        for y in range(OUT_H):
            r, g, b = px[x, y]
            out.append((r, g, b) if (r + g + b) > threshold else BLACK)
    return out


def build_palette(full_frames, colors):
    w, h = full_frames[0].size
    step = max(1, len(full_frames) // 24)
    sample = full_frames[::step]
    montage = Image.new("RGB", (w, h * len(sample)))
    for i, f in enumerate(sample):
        montage.paste(f, (0, i * h))
    return montage.convert("P", palette=Image.ADAPTIVE, colors=max(2, min(256, colors)))


def load_full(path):
    """Charge toutes les frames du GIF en RGB pleine résolution (compositées)."""
    im = Image.open(path)
    full = []
    base = Image.new("RGBA", im.size, (0, 0, 0, 0))
    for fr in ImageSequence.Iterator(im):
        base = base.copy()
        base.alpha_composite(fr.convert("RGBA"))
        full.append(base.convert("RGB"))
    return full


def choose_indices(total, n_max, select):
    """select : liste d'indices explicites (prioritaire) ; sinon échantillonnage uniforme <= n_max."""
    if select:
        idxs = [i for i in select if 0 <= i < total][:MAX_FRAMES]
        return idxs or [0]
    if total > n_max:
        return [round(i * (total - 1) / (n_max - 1)) for i in range(n_max)] if n_max > 1 else [0]
    return list(range(total))


def render_selected(full, idxs, fit, resample, threshold, snap, colors):
    pal = build_palette([full[i] for i in idxs], colors) if snap else None
    frames = []
    for i in idxs:
        small = fit_36x12(full[i], fit, resample)
        if pal is not None:
            small = small.quantize(palette=pal, dither=Image.Dither.NONE).convert("RGB")
        frames.append(frame_pixels(small, threshold))
    return frames


def contact_sheet(full, path):
    """Exporte une planche-contact numérotée de TOUTES les frames source (pour choisir --select)."""
    n = len(full)
    cols = min(8, n)
    rows = (n + cols - 1) // cols
    cw, ch, pad, lbl = OUT_W * 4, OUT_H * 4, 14, 16
    sheet = Image.new("RGB", (cols * (cw + pad) + pad, rows * (ch + pad + lbl) + pad), (20, 20, 20))
    d = ImageDraw.Draw(sheet)
    for k, f in enumerate(full):
        r, c = divmod(k, cols)
        x, y = pad + c * (cw + pad), pad + r * (ch + pad + lbl)
        thumb = fit_36x12(f, "contain", Image.NEAREST).resize((cw, ch), Image.NEAREST)
        sheet.paste(thumb, (x, y))
        d.text((x, y + ch + 2), f"#{k}", fill=(210, 210, 210))
    sheet.save(path)


# --- upload ---
async def upload(address, frames, speed, verbose):
    from bleak import BleakClient
    queue: asyncio.Queue = asyncio.Queue()

    async def run(client):
        loop = asyncio.get_running_loop()

        def on_notify(_c, data):
            r = dec_notify(data)
            if r:
                if verbose:
                    print(f"[{ts()}] NOTIF <- {r}")
                loop.call_soon_threadsafe(queue.put_nowait, r)

        async def wait_for(exp, timeout=8.0):
            while True:
                r = await asyncio.wait_for(queue.get(), timeout=timeout)
                if r.startswith(exp):
                    return r

        await client.start_notify(NOTIFY_CHAR, on_notify)
        n = len(frames)
        print(f"[{ts()}] MANY {n} -> attente MANYOK…")
        await client.write_gatt_char(CMD_CHAR, many_cmd(n), response=True)
        await wait_for("MANYOK")
        for i, pixels in enumerate(frames):
            payload, blen = payload_from_pixels(pixels)
            packets = chunk_packets(payload)
            await client.write_gatt_char(CMD_CHAR, build_dats(len(payload), blen), response=True)
            await wait_for("DATSOK")
            for pkt in packets:
                await client.write_gatt_char(DATA_CHAR, pkt, response=False)
                await wait_for("REOK")
            await client.write_gatt_char(CMD_CHAR, enc_cmd("DATCP"), response=True)
            await wait_for("DATCPOK")
            print(f"[{ts()}] image {i + 1}/{n} stockée ({len(packets)} trames)")
        print(f"[{ts()}] MANCPOK (fin de série)…")
        await client.write_gatt_char(CMD_CHAR, mancpok_cmd(), response=True)
        try:
            await wait_for("MANCP", timeout=8.0)
        except asyncio.TimeoutError:
            print(f"[{ts()}] (pas d'accusé MANCP — on continue quand même)")
        if speed is not None:
            await client.write_gatt_char(CMD_CHAR, speed_cmd(speed), response=True)
            print(f"[{ts()}] SPEED {speed} envoyé")
        await client.write_gatt_char(CMD_CHAR, play_cmd(), response=True)
        print(f"[{ts()}] ✅ PLAY — les {n} images bouclent maintenant sur les lunettes (autonome).")
        await asyncio.sleep(0.3)
        try:
            await client.stop_notify(NOTIFY_CHAR)
        except Exception:
            pass

    async with BleakClient(address) as client:
        await run(client)


def dry_run(frames, total, speed):
    n = len(frames)
    per = len(payload_from_pixels(frames[0])[0])
    chunks = len(chunk_packets(payload_from_pixels(frames[0])[0]))
    print(f"Frames source        : {total}")
    print(f"Frames retenues      : {n}  (max {MAX_FRAMES}{' — échantillonné' if total > n else ''})")
    print(f"Octets / image       : {per}  (bitmap 72 + couleur 1296)")
    print(f"Trames / image       : {chunks}  (98 o utiles + 2 d'en-tête)")
    print(f"Trames totales       : ~{chunks * n}  (chacune acquittée par REOK)")
    print(f"SPEED                : {speed if speed is not None else 'défaut firmware'}")
    print("Note : upload one-shot puis boucle autonome sur les lunettes. Aucun 960b, aucun crash.")


def main():
    ap = argparse.ArgumentParser(description="Upload un GIF comme animation stockée en boucle (DATS).")
    ap.add_argument("address", help="adresse BLE (ignorée avec --dry-run)")
    ap.add_argument("gif", help="GIF animé")
    ap.add_argument("--frames", type=int, default=MAX_FRAMES, help=f"nb max de frames (<= {MAX_FRAMES})")
    ap.add_argument("--select", default="", metavar="i,j,k",
                    help="indices EXACTS des frames à envoyer (ex. 0,4,8,12) — prioritaire sur --frames")
    ap.add_argument("--contact", metavar="OUT.png",
                    help="exporte une planche-contact numérotée de toutes les frames, puis quitte (sans BLE)")
    ap.add_argument("--fit", choices=["cover", "contain", "stretch"], default="cover")
    ap.add_argument("--resample", choices=["nearest", "lanczos"], default="lanczos")
    ap.add_argument("--threshold", type=int, default=0, help="seuil d'extinction (R+G+B)")
    ap.add_argument("--snap", action="store_true", help="force les couleurs vers la palette source")
    ap.add_argument("--colors", type=int, default=48, help="taille de palette pour --snap")
    ap.add_argument("--speed", type=int, default=None, help="vitesse de défilement (option, 0-255)")
    ap.add_argument("--dry-run", action="store_true", help="stats hors-ligne, sans BLE")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    full = load_full(args.gif)
    total = len(full)
    if not total:
        sys.exit("Aucune frame lue.")

    if args.contact:
        contact_sheet(full, args.contact)
        print(f"[{ts()}] planche-contact écrite : {args.contact} ({total} frames, #0..#{total-1}).")
        print(f"        choisis-en <= {MAX_FRAMES} avec --select, ex. --select 0,4,8,12")
        return

    n_max = max(1, min(MAX_FRAMES, args.frames))
    select = []
    if args.select.strip():
        try:
            select = [int(x) for x in args.select.replace(" ", "").split(",") if x != ""]
        except ValueError:
            sys.exit("--select : liste d'entiers séparés par des virgules, ex. 0,4,8,12")
    idxs = choose_indices(total, n_max, select)
    resample = Image.NEAREST if args.resample == "nearest" else Image.LANCZOS
    frames = render_selected(full, idxs, args.fit, resample, args.threshold, args.snap, args.colors)
    print(f"[{ts()}] {total} frames source -> {len(frames)} retenues : indices {idxs}")

    if args.dry_run:
        dry_run(frames, total, args.speed)
        return
    try:
        asyncio.run(upload(args.address, frames, args.speed, args.verbose))
    except asyncio.TimeoutError:
        sys.exit("Timeout — appli mobile fermée ? lunettes allumées ? (une étape n'a pas été acquittée)")
    except KeyboardInterrupt:
        print("\nArrêt.")


if __name__ == "__main__":
    main()
