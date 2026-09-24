#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "bleak>=0.22",
#     "pycryptodome>=3.20",
# ]
# ///
"""
glasses_pixel_test.py — Sonde EXPÉRIMENTALE du format couleur par-pixel des lunettes Heaton.

On ne connaît pas le format exact de l'upload couleur DIY. Cet outil envoie un MOTIF TÉMOIN
à 4 coins colorés, en essayant différentes hypothèses de format. Tu regardes les lunettes et
tu me dis laquelle affiche le motif correctement.

    uv run archive/glasses_pixel_test.py list                       # liste les variantes
    uv run archive/glasses_pixel_test.py AA:BB:CC:DD:EE:FF 0        # variante 0 (référence par-colonne)
    uv run archive/glasses_pixel_test.py AA:BB:CC:DD:EE:FF 1        # variante 1, etc.

MOTIF TÉMOIN (sur l'afficheur 36x12) :
    coin HAUT-GAUCHE   = ROUGE
    coin BAS-GAUCHE    = BLEU        (même colonne que le rouge -> teste la couleur par pixel)
    coin HAUT-DROIT    = VERT
    coin BAS-DROIT     = BLANC
    tout le reste      = éteint

CE QU'ON APPREND :
  • Si une variante montre 4 coins de 4 couleurs distinctes -> COULEUR PAR PIXEL débloquée 🎉
    (le rouge en haut ET le bleu en bas de la MÊME colonne = preuve du par-pixel)
  • La variante 0 (par-colonne, connue) sert de référence : ses colonnes de coin seront
    d'une seule couleur (rouge+bleu fusionnés) — c'est normal.
  • L'emplacement réel des couleurs révèle l'ordre (ligne/colonne) et les miroirs.

⚠️ Ferme l'appli mobile avant. En cas d'affichage bizarre : éteins/rallume les lunettes.
"""

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

OUT_W, OUT_H = 36, 12
KEY = bytes.fromhex("32672f7974ad43451d9c6c894a0e8764")
CMD_CHAR = "d44bc439-abfd-45a2-b575-925416129600"
NOTIFY_CHAR = "d44bc439-abfd-45a2-b575-925416129601"
DATA_CHAR = "d44bc439-abfd-45a2-b575-92541612960a"
MAX_PACKET, PAD = 100, 16

# motif témoin : (x, y) -> (r, g, b)
PATTERN = {
    (0, 0): (255, 0, 0),        # haut-gauche  ROUGE
    (0, OUT_H - 1): (0, 0, 255),      # bas-gauche   BLEU
    (OUT_W - 1, 0): (0, 255, 0),      # haut-droit   VERT
    (OUT_W - 1, OUT_H - 1): (255, 255, 255),  # bas-droit BLANC
}

# variantes : (description, dict de paramètres)
#   color: 'percolumn' | 'perpixel'
#   order: 'col' (x puis y) | 'row' (y puis x)   [perpixel]
#   rgb:   'RGB' | 'BGR' | 'GRB'
#   fmt:   octet de format dans DATS (mask-go=0x00)
#   bitmap:'full' (tout allumé) | 'mask' (seulement les 4 pixels)
VARIANTS = [
    ("Référence PAR-COLONNE (connue) — colonnes de coin unicolores attendues",
     dict(color="percolumn", rgb="RGB", fmt=0x00, bitmap="mask")),
    ("PAR-PIXEL colonne-major RGB, format=00, bitmap plein",
     dict(color="perpixel", order="col", rgb="RGB", fmt=0x00, bitmap="full")),
    ("PAR-PIXEL colonne-major RGB, format=01, bitmap plein",
     dict(color="perpixel", order="col", rgb="RGB", fmt=0x01, bitmap="full")),
    ("PAR-PIXEL ligne-major  RGB, format=00, bitmap plein",
     dict(color="perpixel", order="row", rgb="RGB", fmt=0x00, bitmap="full")),
    ("PAR-PIXEL ligne-major  RGB, format=01, bitmap plein",
     dict(color="perpixel", order="row", rgb="RGB", fmt=0x01, bitmap="full")),
    ("PAR-PIXEL colonne-major RGB, format=00, bitmap = motif",
     dict(color="perpixel", order="col", rgb="RGB", fmt=0x00, bitmap="mask")),
    ("PAR-PIXEL colonne-major GRB, format=00, bitmap plein",
     dict(color="perpixel", order="col", rgb="GRB", fmt=0x00, bitmap="full")),
    ("PAR-PIXEL colonne-major BGR, format=00, bitmap plein",
     dict(color="perpixel", order="col", rgb="BGR", fmt=0x00, bitmap="full")),
]


def ts():
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


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


def rgb_bytes(c, order):
    r, g, b = c
    return {"RGB": bytes([r, g, b]),
            "BGR": bytes([b, g, r]),
            "GRB": bytes([g, r, b])}[order]


def encode_bitmap(on):
    """on: fonction (x,y)->bool. Retourne octets bitmap (ceil(H/8) par colonne)."""
    bpc = (OUT_H + 7) // 8
    out = bytearray()
    for x in range(OUT_W):
        for g in range(bpc):
            byte = 0
            for k in range(8):
                y = 8 * g + k
                if y < OUT_H and on(x, y):
                    byte |= 1 << (7 - k)
            out.append(byte)
    return bytes(out)


def build(variant):
    p = variant
    lit = lambda x, y: (x, y) in PATTERN
    if p["bitmap"] == "full":
        bitmap = encode_bitmap(lambda x, y: True)
    else:
        bitmap = encode_bitmap(lit)

    if p["color"] == "percolumn":
        # une couleur par colonne = couleur du pixel témoin de la colonne (sinon noir)
        colors = bytearray()
        for x in range(OUT_W):
            c = PATTERN.get((x, 0)) or PATTERN.get((x, OUT_H - 1)) or (0, 0, 0)
            colors += rgb_bytes(c, p["rgb"])
        color_bytes = bytes(colors)
    else:
        colors = bytearray()
        if p["order"] == "col":
            for x in range(OUT_W):
                for y in range(OUT_H):
                    colors += rgb_bytes(PATTERN.get((x, y), (0, 0, 0)), p["rgb"])
        else:
            for y in range(OUT_H):
                for x in range(OUT_W):
                    colors += rgb_bytes(PATTERN.get((x, y), (0, 0, 0)), p["rgb"])
        color_bytes = bytes(colors)

    payload = bitmap + color_bytes
    return payload, len(bitmap), p["fmt"]


def build_dats(total_len, bitmap_len, fmt):
    args = total_len.to_bytes(2, "big") + bitmap_len.to_bytes(2, "big") + bytes([fmt])
    return enc_cmd("DATS", args)


def chunk_packets(payload):
    packets, i, idx, maxd = [], 0, 0, MAX_PACKET - 2
    while i < len(payload):
        data = payload[i:i + maxd]
        packets.append(bytes([len(data) + 1, idx]) + data)
        i += len(data)
        idx += 1
    return packets


async def send(address, payload, bitmap_len, fmt):
    packets = chunk_packets(payload)
    print(f"[{ts()}] payload={len(payload)} o (bitmap={bitmap_len}, "
          f"couleur={len(payload) - bitmap_len}), {len(packets)} paquets, format=0x{fmt:02x}")
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
        await client.write_gatt_char(CMD_CHAR, build_dats(len(payload), bitmap_len, fmt), response=True)
        print(f"[{ts()}] DATS envoyé, attente DATSOK…")
        try:
            await wait_for("DATSOK")
        except asyncio.TimeoutError:
            print(f"[{ts()}] ⚠️ Pas de DATSOK — le firmware a peut-être refusé ce format/taille.")
            return
        for n, pkt in enumerate(packets):
            await client.write_gatt_char(DATA_CHAR, pkt, response=False)
            try:
                await wait_for("REOK")
            except asyncio.TimeoutError:
                print(f"[{ts()}] ⚠️ Pas de REOK au paquet {n} — format probablement rejeté.")
                return
        await client.write_gatt_char(CMD_CHAR, enc_cmd("DATCP"), response=True)
        try:
            await wait_for("DATCPOK")
        except asyncio.TimeoutError:
            print(f"[{ts()}] ⚠️ Pas de DATCPOK.")
        await client.write_gatt_char(CMD_CHAR, enc_cmd("MODE", b"\x01"), response=True)
        await asyncio.sleep(0.3)
        try:
            await client.stop_notify(NOTIFY_CHAR)
        except Exception:
            pass
    print(f"[{ts()}] ✅ Variante envoyée. Regarde les lunettes.")


def print_variants():
    print("Variantes disponibles :\n")
    for i, (desc, _) in enumerate(VARIANTS):
        print(f"  {i} : {desc}")
    print("\nMotif attendu si le PAR-PIXEL marche :")
    print("  un coin ROUGE (haut) + BLEU (bas) du même côté, VERT (haut) + BLANC (bas) de l'autre.")
    print("  -> rouge et bleu VISIBLES SÉPARÉMENT = couleur par pixel débloquée.")


def main():
    args = sys.argv[1:]
    if not args or args[0] == "list":
        print_variants()
        return
    if len(args) < 2:
        sys.exit("Usage : uv run archive/glasses_pixel_test.py <adresse> <numéro_variante>\n"
                 "        uv run archive/glasses_pixel_test.py list")
    address = args[0]
    try:
        vi = int(args[1])
        desc, variant = VARIANTS[vi]
    except (ValueError, IndexError):
        sys.exit(f"Variante invalide. 0..{len(VARIANTS) - 1} (ou 'list').")

    print(f"=== Variante {vi} : {desc} ===")
    payload, blen, fmt = build(variant)
    try:
        asyncio.run(send(address, payload, blen, fmt))
    except asyncio.TimeoutError:
        sys.exit("Timeout global — appli mobile fermée ? lunettes allumées ?")
    except KeyboardInterrupt:
        print("\nArrêt.")


if __name__ == "__main__":
    main()
