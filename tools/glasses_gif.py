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
glasses_gif.py — Joue un GIF animé en temps réel sur les lunettes Heaton (couleur par-pixel).

Exploite le canal temps-réel …960b (mode DIY live) validé par glasses_realtime.py : le firmware
maintient un canevas persistant et accepte plusieurs points par trame. Au lieu de re-téléverser
chaque frame (lent), on n'envoie que la DIFFÉRENCE entre deux frames, groupée par couleur.

Principe :
  1. entrer en mode DIY (commande SMVEW chiffrée sur …9600)
  2. peindre la 1ʳᵉ frame (pixels non noirs), groupée par couleur, sur …960b
  3. pour chaque frame suivante : ne pousser QUE les pixels qui changent, groupés par couleur
  4. répéter à la cadence du GIF, en boucle (Ctrl-C pour arrêter -> sortie propre du mode DIY)

Chaque trame …960b : [len][R][G][B][col0][row0]…  (len = 3 + 2*nb_points, brute, non chiffrée).

Exemples :
    uv run tools/glasses_gif.py AA:BB:CC:DD:EE:FF anim.gif
    uv run tools/glasses_gif.py AA:BB:CC:DD:EE:FF anim.gif --fit contain --threshold 40
    uv run tools/glasses_gif.py AA:BB:CC:DD:EE:FF anim.gif --fps 12 --loops 3
    uv run tools/glasses_gif.py X anim.gif --dry-run          # stats hors-ligne, sans BLE ni lunettes

⚠️  Ferme l'appli mobile avant : une seule connexion BLE à la fois.
"""

import argparse
import asyncio
import sys
from datetime import datetime

try:
    from PIL import Image, ImageSequence
except ImportError:
    sys.exit("pillow manquant : utilise 'uv run'")
try:
    from Crypto.Cipher import AES
except ImportError:
    sys.exit("pycryptodome manquant : utilise 'uv run'")

OUT_W, OUT_H = 36, 12
N = OUT_W * OUT_H
CMD_CHAR = "d44bc439-abfd-45a2-b575-925416129600"
NOTIFY_CHAR = "d44bc439-abfd-45a2-b575-925416129601"
RT_CHAR = "d44bc439-abfd-45a2-b575-92541612960b"
KEY = bytes.fromhex("32672f7974ad43451d9c6c894a0e8764")
PAD = 16
BLACK = (0, 0, 0)


def ts():
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


# --- protocole ---
def enc_cmd(op, args=b""):
    body = bytes([len(op) + len(args)]) + op.encode("ascii") + args
    body += b"\x00" * ((PAD - (len(body) % PAD)) % PAD)
    return AES.new(KEY, AES.MODE_ECB).encrypt(body)


def enter_diy():
    return enc_cmd("SMVEW", bytes([1]))


def exit_diy(save=False):
    return enc_cmd("SMVEW", bytes([2 if save else 0]))


def rt_frame(points, rgb):
    """points: liste de (col, row) ; rgb: (r,g,b). Trame brute pour …960b (ordre fil = col, row)."""
    r, g, b = rgb
    body = bytearray([3 + 2 * len(points), r & 0xFF, g & 0xFF, b & 0xFF])
    for (col, row) in points:
        body += bytes([col & 0xFF, row & 0xFF])
    return bytes(body)


# --- chargement du GIF ---
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
    out = Image.new("RGB", (tw, th), BLACK)          # contain : centrer sur fond noir
    out.paste(r, ((tw - nw) // 2, (th - nh) // 2))
    return out


def frame_pixels(img36, threshold, posterize=0):
    """Renvoie une liste de N couleurs en ordre colonne-major (index = col*OUT_H + row).
    posterize : si 1..7, réduit chaque canal à ce nombre de bits (moins de couleurs -> moins
    d'écritures/frame -> plus de FPS)."""
    px = img36.load()
    q = (8 - posterize) if 0 < posterize < 8 else 0
    out = []
    for x in range(OUT_W):
        for y in range(OUT_H):
            r, g, b = px[x, y]
            if (r + g + b) > threshold:
                out.append(((r >> q) << q, (g >> q) << q, (b >> q) << q) if q else (r, g, b))
            else:
                out.append(BLACK)
    return out


def build_palette(full_frames, colors):
    """Palette médian-cut des vraies couleurs de la source (échantillon de frames)."""
    w, h = full_frames[0].size
    step = max(1, len(full_frames) // 24)
    sample = full_frames[::step]
    montage = Image.new("RGB", (w, h * len(sample)))
    for i, f in enumerate(sample):
        montage.paste(f, (0, i * h))
    return montage.convert("P", palette=Image.ADAPTIVE, colors=max(2, min(256, colors)))


def load_gif(path, fit, threshold, resample, posterize=0, snap=False, colors=48):
    """Renvoie (frames, durations) : frames = listes de N couleurs ; durations en secondes."""
    im = Image.open(path)
    full, durations = [], []
    base = Image.new("RGBA", im.size, (0, 0, 0, 0))
    for fr in ImageSequence.Iterator(im):
        base = base.copy()
        base.alpha_composite(fr.convert("RGBA"))       # gère la disposition « ne pas effacer »
        full.append(base.convert("RGB"))
        durations.append(max(0.02, fr.info.get("duration", 100) / 1000.0))
    pal = build_palette(full, colors) if snap else None
    frames = []
    for f in full:
        small = fit_36x12(f, fit, resample)
        if pal is not None:                            # snap : couleur source la plus proche
            small = small.quantize(palette=pal, dither=Image.Dither.NONE).convert("RGB")
        frames.append(frame_pixels(small, threshold, posterize))
    return frames, durations


def idx_to_colrow(i):
    return i // OUT_H, i % OUT_H


def diff_groups(prev, cur):
    """Pixels changés entre prev et cur, groupés par couleur cible : {rgb: [(col,row), …]}."""
    groups = {}
    for i in range(N):
        if prev[i] != cur[i]:
            groups.setdefault(cur[i], []).append(idx_to_colrow(i))
    return groups


def groups_to_frames(groups, max_points):
    """Sérialise les groupes en trames …960b (découpe si un groupe dépasse max_points)."""
    out = []
    for rgb, pts in groups.items():
        for k in range(0, len(pts), max_points):
            out.append(rt_frame(pts[k:k + max_points], rgb))
    return out


# --- lecture ---
async def play(address, frames, durations, max_points, loops, save_on_exit, verbose,
               keyframe=16, reliable=False):
    from bleak import BleakClient
    async with BleakClient(address) as client:
        try:
            mtu = client.mtu_size or 23
        except Exception:
            mtu = 23
        max_points = min(max_points, max(1, (mtu - 3 - 4) // 2))
        mode = "fiable (accusé)" if reliable else f"rapide (diff sans accusé, keyframe /{keyframe})"
        print(f"[{ts()}] connecté (MTU {mtu} → {max_points} pts/trame) ; mode {mode} ; entrée DIY")
        await client.write_gatt_char(CMD_CHAR, enter_diy(), response=True)
        await asyncio.sleep(0.4)
        loop = asyncio.get_event_loop()
        displayed = [None] * N      # None ≠ toute couleur -> 1re frame = les N LED peintes en entier
        n = fc = 0
        try:
            while loops == 0 or n < loops:
                for f, (cur, dur) in enumerate(zip(frames, durations)):
                    t0 = loop.time()
                    # keyframe : rafraîchissement complet (accusé) pour réparer d'éventuelles pertes
                    is_key = (fc % keyframe == 0)
                    prev = [None] * N if is_key else displayed
                    resp = True if (is_key or reliable) else False
                    packets = groups_to_frames(diff_groups(prev, cur), max_points)
                    for pkt in packets:
                        await client.write_gatt_char(RT_CHAR, pkt, response=resp)
                        if not resp:                   # cadence anti-saturation firmware
                            await asyncio.sleep(0.004)
                    displayed = cur
                    fc += 1
                    dt = loop.time() - t0
                    if verbose:
                        print(f"[{ts()}] boucle {n+1} frame {f+1}/{len(frames)}"
                              f"{' [KEY]' if is_key else ''} : {len(packets)} trames en {dt*1000:.0f} ms")
                    await asyncio.sleep(max(0, dur - dt))
                n += 1
        except asyncio.CancelledError:
            raise
        finally:
            print(f"\n[{ts()}] sortie du mode DIY (save={save_on_exit})")
            try:
                await client.write_gatt_char(CMD_CHAR, exit_diy(save_on_exit), response=True)
            except Exception:
                pass


def dry_run(frames, durations, max_points, keyframe=16):
    total_writes, total_changed, peak = 0, 0, 0
    displayed = [None] * N          # 1re frame = rafraîchissement complet des N LED
    for fc, cur in enumerate(frames):
        prev = [None] * N if (fc % keyframe == 0) else displayed
        groups = diff_groups(prev, cur)
        changed = sum(len(v) for v in groups.values())
        writes = len(groups_to_frames(groups, max_points))
        peak = max(peak, writes)
        total_changed += changed
        total_writes += writes
        displayed = cur
    nf = len(frames)
    avg_dur = sum(durations) / nf
    print(f"Frames               : {nf}")
    print(f"Durée moy./frame     : {avg_dur*1000:.0f} ms  (GIF ~{1/avg_dur:.1f} fps)")
    print(f"Pixels changés moy.  : {total_changed/nf:.0f} / frame  (sur {N})")
    print(f"Trames 960b moy.     : {total_writes/nf:.1f} / frame  (keyframe /{keyframe} inclus)")
    print(f"Trames 960b max      : {peak} / frame  (frame la plus lourde)")
    print(f"Trames 960b total    : {total_writes}")
    print("Astuce FPS : --posterize 5 (moins de couleurs), --resample nearest, ou augmente "
          "--keyframe (rafraîchit moins souvent = plus rapide, un peu moins auto-réparant).")


def main():
    ap = argparse.ArgumentParser(description="Joue un GIF animé en per-pixel temps réel (canal 960b).")
    ap.add_argument("address", help="adresse BLE des lunettes (ignorée avec --dry-run)")
    ap.add_argument("gif", help="chemin du GIF animé")
    ap.add_argument("--fit", choices=["cover", "contain", "stretch"], default="cover",
                    help="cadrage vers 36x12 (défaut cover = remplit et recadre)")
    ap.add_argument("--resample", choices=["nearest", "lanczos"], default="lanczos",
                    help="rééchantillonnage (lanczos = lissé (défaut) ; nearest = pixel-art net)")
    ap.add_argument("--threshold", type=int, default=0,
                    help="seuil de luminance (R+G+B) sous lequel un pixel est éteint")
    ap.add_argument("--fps", type=float, default=0,
                    help="force la cadence (sinon on suit les durées du GIF)")
    ap.add_argument("--loops", type=int, default=0, help="nombre de boucles (0 = infini)")
    ap.add_argument("--max-points", type=int, default=120, help="points max par trame 960b")
    ap.add_argument("--keyframe", type=int, default=16,
                    help="rafraîchissement complet (accusé) toutes les N frames (auto-réparation)")
    ap.add_argument("--reliable", action="store_true",
                    help="tout en écriture accusée (plus lent, plus sûr)")
    ap.add_argument("--posterize", type=int, default=0,
                    help="réduit chaque canal à N bits (1..7) pour accélérer (moins de couleurs)")
    ap.add_argument("--snap", action="store_true",
                    help="force chaque pixel vers la couleur source la plus proche (tranché, moins d'updates)")
    ap.add_argument("--colors", type=int, default=48, help="taille de palette pour --snap")
    ap.add_argument("--save", action="store_true", help="sauver l'image à la sortie du mode DIY")
    ap.add_argument("--dry-run", action="store_true", help="stats hors-ligne, sans BLE")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    resample = Image.NEAREST if args.resample == "nearest" else Image.LANCZOS
    frames, durations = load_gif(args.gif, args.fit, args.threshold, resample,
                                 args.posterize, args.snap, args.colors)
    if not frames:
        sys.exit("Aucune frame lue dans le GIF.")
    if args.fps > 0:
        durations = [1.0 / args.fps] * len(frames)

    if args.dry_run:
        dry_run(frames, durations, args.max_points, args.keyframe)
        return

    print(f"[{ts()}] {len(frames)} frames chargées — Ctrl-C pour arrêter")
    try:
        asyncio.run(play(args.address, frames, durations, args.max_points,
                         args.loops, args.save, args.verbose, args.keyframe, args.reliable))
    except KeyboardInterrupt:
        print("\nArrêt demandé.")
    except asyncio.TimeoutError:
        sys.exit("Timeout — appli mobile fermée ? lunettes allumées ?")


if __name__ == "__main__":
    main()
