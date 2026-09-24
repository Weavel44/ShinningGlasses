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
glasses_pixel.py — Affiche une image en COULEUR PAR-PIXEL (true color) sur les lunettes Heaton.

Contrairement à glasses_text.py (une couleur par colonne), ce script exploite le VRAI format DIY
de l'appli officielle, décodé octet par octet dans son code (classe DiyAgreement). Chaque pixel de
l'afficheur 36x12 reçoit sa propre couleur RGB : les dégradés verticaux (arc-en-ciel…)
s'affichent enfin correctement.

Format DIY (vérité terrain, ground-truth depuis l'APK décompilé) :
  • bitmap  : 72 octets  = 36 colonnes x 2 octets, colonne-major, ligne du haut = bit de poids fort
  • couleurs: 432 triplets RGB = une par pixel, MÊME ordre colonne-major (index = colonne*12 + ligne)
  • payload : bitmap(72) + couleurs(1296) = 1368 octets
  • DATS    : DATS [total:u16be][bitmap:u16be][0x01]   <- dernier octet 0x01 = sélecteur PAR-PIXEL
  • data    : trames de 98 o sur …960a, [len+1][idx][data], chacune acquittée par REOK
  • fin     : DATCP -> DATCPOK.  AUCUN MODE ensuite (sinon le firmware repasse en preset d'usine).

Exemples :
    uv run tools/glasses_pixel.py AA:BB:CC:DD:EE:FF --pattern            # motif témoin 4 coins (preuve)
    uv run tools/glasses_pixel.py AA:BB:CC:DD:EE:FF --image mon_image.png
    uv run tools/glasses_pixel.py AA:BB:CC:DD:EE:FF --image logo.png --preview out.png   # n'envoie RIEN
    uv run tools/glasses_pixel.py AA:BB:CC:DD:EE:FF --pattern -v         # trace détaillée du handshake

⚠️  Ferme l'appli mobile avant : une seule connexion BLE centrale à la fois.
"""

import argparse
import asyncio
import sys
from datetime import datetime

try:
    from bleak import BleakClient
except ImportError:
    sys.exit("bleak manquant : utilise 'uv run'")
try:
    from Crypto.Cipher import AES
except ImportError:
    sys.exit("pycryptodome manquant : utilise 'uv run'")
try:
    from PIL import Image
except ImportError:
    sys.exit("pillow manquant : utilise 'uv run'")


# --- UUID (confirmés par le dump GATT et l'APK) ---
CMD_CHAR = "d44bc439-abfd-45a2-b575-925416129600"     # commandes chiffrées (WRITE1)
NOTIFY_CHAR = "d44bc439-abfd-45a2-b575-925416129601"  # réponses chiffrées (NOTIFY1)
DATA_CHAR = "d44bc439-abfd-45a2-b575-92541612960a"    # trames de données (WRITE2)

KEY = bytes.fromhex("32672f7974ad43451d9c6c894a0e8764")
MAX_PACKET = 100          # taille max d'un paquet sur …960a  (98 o de data + 2 d'en-tête)
PAD = 16                  # bloc AES / padding des commandes
OUT_W, OUT_H = 36, 12     # afficheur des lunettes
DIY_TYPE = 0x01           # 5e octet du DATS : 0x01 = données DIY par-pixel (0x00 = texte par-colonne)


def ts():
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def hexdump(b):
    return " ".join(f"{x:02x}" for x in b)


# ---------------------------------------------------------------------------
# Primitives protocole (identiques aux autres scripts — AES-128-ECB, clé statique)
# ---------------------------------------------------------------------------
def enc_cmd(op, args=b""):
    """Trame commande [len(op)+len(args)] + op(ASCII) + args, padée à 16, puis AES-ECB."""
    body = bytes([len(op) + len(args)]) + op.encode("ascii") + args
    body += b"\x00" * ((PAD - (len(body) % PAD)) % PAD)
    return AES.new(KEY, AES.MODE_ECB).encrypt(body)


def dec_notify(data):
    """Déchiffre une notification …9601 et extrait la réponse ASCII [len][texte]."""
    data = bytes(data)
    if not data or len(data) % 16:
        return ""
    clear = AES.new(KEY, AES.MODE_ECB).decrypt(data)
    n = clear[0]
    return clear[1:1 + n].decode("ascii", "replace") if 1 + n <= len(clear) else ""


def build_dats(total_len, bitmap_len):
    """DATS [total:u16be][bitmap:u16be][DIY_TYPE].  int2Bytes de l'appli = big-endian."""
    args = total_len.to_bytes(2, "big") + bitmap_len.to_bytes(2, "big") + bytes([DIY_TYPE])
    return enc_cmd("DATS", args)


# ---------------------------------------------------------------------------
# Encodage DIY par-pixel (portage fidèle de DiyAgreement.getDiyBytes1236 + getColorArray)
# ---------------------------------------------------------------------------
def encode_bitmap(lit):
    """lit(x,y)->bool. 36 colonnes x 2 octets, ligne du haut = MSB (ordre colonne-major)."""
    out = bytearray()
    for x in range(OUT_W):
        b0 = b1 = 0
        for y in range(OUT_H):
            if lit(x, y):
                if y < 8:
                    b0 |= 1 << (7 - y)
                else:
                    b1 |= 1 << (7 - (y - 8))
        out.append(b0)
        out.append(b1)
    return bytes(out)


def encode_colors(color):
    """color(x,y)->(r,g,b). 432 triplets RGB, ordre colonne-major (index = x*12 + y)."""
    out = bytearray()
    for x in range(OUT_W):
        for y in range(OUT_H):
            r, g, b = color(x, y)
            out += bytes([r & 0xFF, g & 0xFF, b & 0xFF])
    return bytes(out)


def build_payload(color, lit=None, on_threshold=0):
    """Construit le payload DIY complet. `color(x,y)->(r,g,b)`.
    Par défaut un pixel est "allumé" (bitmap=1) si sa luminance dépasse `on_threshold`."""
    if lit is None:
        def lit(x, y):
            r, g, b = color(x, y)
            return (r + g + b) > on_threshold
    bitmap = encode_bitmap(lit)
    colors = encode_colors(color)
    return bitmap + colors, len(bitmap)


def chunk_packets(payload):
    """Découpe en trames [len(data)+1][index][data], 98 o de data max (comme getSendData)."""
    maxd = MAX_PACKET - 2
    packets, i, idx = [], 0, 0
    while i < len(payload):
        data = payload[i:i + maxd]
        packets.append(bytes([len(data) + 1, idx]) + data)
        i += len(data)
        idx += 1
    return packets


# ---------------------------------------------------------------------------
# Sources d'image
# ---------------------------------------------------------------------------
def pattern_source():
    """Motif témoin : ROUGE haut-gauche, BLEU bas-gauche, VERT haut-droit, BLANC bas-droit.
    Rouge et bleu sont dans la MÊME colonne -> s'ils apparaissent séparément = par-pixel prouvé."""
    pts = {
        (0, 0): (255, 0, 0),
        (0, OUT_H - 1): (0, 0, 255),
        (OUT_W - 1, 0): (0, 255, 0),
        (OUT_W - 1, OUT_H - 1): (255, 255, 255),
    }
    return lambda x, y: pts.get((x, y), (0, 0, 0))


def image_source(path):
    """Charge une image, la redimensionne en 36x12 et renvoie color(x,y)->(r,g,b)."""
    img = Image.open(path).convert("RGB").resize((OUT_W, OUT_H), Image.LANCZOS)
    px = img.load()
    return lambda x, y: px[x, y]


def render_preview(color, path):
    """Écrit un PNG agrandi x10 de ce qui serait envoyé (sans toucher aux lunettes)."""
    scale = 10
    img = Image.new("RGB", (OUT_W * scale, OUT_H * scale), (0, 0, 0))
    px = img.load()
    for x in range(OUT_W):
        for y in range(OUT_H):
            c = color(x, y)
            for dx in range(scale):
                for dy in range(scale):
                    px[x * scale + dx, y * scale + dy] = c
    img.save(path)


# ---------------------------------------------------------------------------
# Envoi BLE
# ---------------------------------------------------------------------------
async def send(address, payload, bitmap_len, verbose=False):
    packets = chunk_packets(payload)
    print(f"[{ts()}] payload={len(payload)} o (bitmap={bitmap_len}, couleur={len(payload) - bitmap_len}), "
          f"{len(packets)} trames, DATS type=0x{DIY_TYPE:02x}")
    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def on_notify(_c, data):
        r = dec_notify(data)
        if r:
            print(f"[{ts()}] NOTIF <- {r}")
        loop.call_soon_threadsafe(queue.put_nowait, r)

    async def wait_for(exp, timeout=6.0):
        while True:
            r = await asyncio.wait_for(queue.get(), timeout=timeout)
            if r.startswith(exp):
                return r

    async with BleakClient(address) as client:
        await client.start_notify(NOTIFY_CHAR, on_notify)

        dats = build_dats(len(payload), bitmap_len)
        if verbose:
            print(f"[{ts()}] DATS -> {hexdump(dats)}")
        await client.write_gatt_char(CMD_CHAR, dats, response=True)
        print(f"[{ts()}] DATS envoyé, attente DATSOK…")
        try:
            await wait_for("DATSOK")
        except asyncio.TimeoutError:
            print(f"[{ts()}] ⚠️ Pas de DATSOK — le firmware a refusé la taille/format.")
            return

        for n, pkt in enumerate(packets):
            if verbose:
                print(f"[{ts()}] trame {n} ({len(pkt)} o) -> {hexdump(pkt[:8])}…")
            await client.write_gatt_char(DATA_CHAR, pkt, response=False)
            try:
                await wait_for("REOK")
            except asyncio.TimeoutError:
                print(f"[{ts()}] ⚠️ Pas de REOK à la trame {n} — format rejeté.")
                return

        await client.write_gatt_char(CMD_CHAR, enc_cmd("DATCP"), response=True)
        try:
            await wait_for("DATCPOK")
            print(f"[{ts()}] ✅ DATCPOK — image par-pixel appliquée. Regarde les lunettes.")
        except asyncio.TimeoutError:
            print(f"[{ts()}] ⚠️ Pas de DATCPOK.")
        # PAS de commande MODE : le flux DIY officiel s'arrête ici.
        await asyncio.sleep(0.3)
        try:
            await client.stop_notify(NOTIFY_CHAR)
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser(description="Uploader COULEUR PAR-PIXEL pour lunettes Heaton.")
    ap.add_argument("address", help="adresse BLE des lunettes (ex. AA:BB:CC:DD:EE:FF)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--pattern", action="store_true", help="motif témoin 4 coins (preuve du par-pixel)")
    g.add_argument("--image", metavar="FICHIER", help="image à afficher (redimensionnée en 36x12)")
    ap.add_argument("--threshold", type=int, default=0,
                    help="seuil de luminance (R+G+B) pour allumer un pixel (défaut 0 = tout non-noir)")
    ap.add_argument("--preview", metavar="OUT.png", help="écrit un PNG du rendu et n'envoie RIEN")
    ap.add_argument("-v", "--verbose", action="store_true", help="trace détaillée du handshake")
    args = ap.parse_args()

    color = pattern_source() if args.pattern else image_source(args.image)
    payload, bitmap_len = build_payload(color, on_threshold=args.threshold)

    if args.preview:
        render_preview(color, args.preview)
        print(f"[{ts()}] aperçu écrit : {args.preview} — rien n'a été envoyé aux lunettes.")
        print(f"[{ts()}] payload prêt : {len(payload)} o (bitmap={bitmap_len}, couleur={len(payload) - bitmap_len}).")
        return

    try:
        asyncio.run(send(args.address, payload, bitmap_len, verbose=args.verbose))
    except asyncio.TimeoutError:
        sys.exit("Timeout global — appli mobile fermée ? lunettes allumées ?")
    except KeyboardInterrupt:
        print("\nArrêt.")


if __name__ == "__main__":
    main()
