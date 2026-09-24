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
glasses_text.py — Affiche du texte (ou une image) sur les lunettes LED Heaton.

Portage fidèle du protocole Shining Mask (projet GoneUp/mask-go) :
  1. rendu du texte en bitmap "colonne"          (Pillow)
  2. commande DATS chiffrée (AES-128-ECB) -> …9600
  3. attente DATSOK sur …9601
  4. envoi des chunks bruts sur …960a, chacun acquitté par REOK
  5. commande DATCP chiffrée -> …9600, fin sur DATCPOK

Exemples :
    uv run glasses_text.py AA:BB:CC:DD:EE:FF --text "SALUT"
    uv run glasses_text.py AA:BB:CC:DD:EE:FF --text "GUTS" --color 255 0 0
    uv run glasses_text.py AA:BB:CC:DD:EE:FF --image mon_logo.png
    uv run glasses_text.py AA:BB:CC:DD:EE:FF --text "TEST" --height 12

⚠️  HAUTEUR D'AFFICHAGE : réglée par défaut sur 12 px (mesurée sur ces lunettes). Le texte est
    centré verticalement et la police s'ajuste toute seule. Change --height seulement pour
    d'autres lunettes. --preview écrit un PNG du bitmap rendu (sans rien envoyer) pour vérifier.

⚠️  Ferme l'appli mobile avant : une seule connexion BLE à la fois.
"""

import argparse
import asyncio
import sys
from datetime import datetime

try:
    from bleak import BleakClient
except ImportError:
    sys.exit("bleak manquant :  utilise 'uv run'")
try:
    from Crypto.Cipher import AES
except ImportError:
    sys.exit("pycryptodome manquant :  utilise 'uv run'")
try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    sys.exit("pillow manquant :  utilise 'uv run'")


# --- UUID (confirmés par le dump des lunettes) ---
CMD_CHAR = "d44bc439-abfd-45a2-b575-925416129600"    # commandes chiffrées
NOTIFY_CHAR = "d44bc439-abfd-45a2-b575-925416129601"  # réponses (chiffrées)
DATA_CHAR = "d44bc439-abfd-45a2-b575-92541612960a"    # données image (en clair)

KEY = bytes.fromhex("32672f7974ad43451d9c6c894a0e8764")
MAX_PACKET = 100          # taille max d'un paquet accepté sur …960a
PAD = 16                  # taille de bloc AES / padding des commandes
DISPLAY_W = 36            # largeur de l'afficheur des lunettes (colonnes)
DISPLAY_H = 12            # hauteur de l'afficheur des lunettes (px)

MODES = {"steady": 0x01, "blink": 0x02, "scroll-left": 0x03, "scroll-right": 0x04}


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


# ----------------------------------------------------------- chiffrement
def enc_cmd(op: str, args: bytes = b"") -> bytes:
    body = bytes([len(op) + len(args)]) + op.encode("ascii") + args
    body += b"\x00" * ((PAD - (len(body) % PAD)) % PAD)
    return AES.new(KEY, AES.MODE_ECB).encrypt(body)


def dec_notify(data: bytes) -> str:
    """Déchiffre une notification et extrait la réponse ASCII ([len][texte…])."""
    if len(data) % 16 != 0 or not data:
        return ""
    clear = AES.new(KEY, AES.MODE_ECB).decrypt(bytes(data))
    n = clear[0]
    if 1 + n > len(clear):
        return ""
    return clear[1:1 + n].decode("ascii", errors="replace")


# ----------------------------------------------------------- rendu bitmap
def render_columns(text: str, height: int, font_size: int, font_path: str):
    """Rend le texte en colonnes de pixels on/off (comme mask-go)."""
    # Choix de la police : chemin fourni, sinon Arial (Windows), sinon défaut Pillow.
    font = None
    candidates = [font_path] if font_path else []
    candidates += [r"C:\Windows\Fonts\arial.ttf", "DejaVuSans.ttf", "arial.ttf"]
    for c in candidates:
        if not c:
            continue
        try:
            font = ImageFont.truetype(c, font_size)
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()

    # bornes réelles du texte
    tmp = Image.new("L", (1, 1))
    d = ImageDraw.Draw(tmp)
    bbox = d.textbbox((0, 0), text, font=font)
    width = max(1, bbox[2] - bbox[0])
    glyph_h = bbox[3] - bbox[1]

    img = Image.new("L", (width, height), 0)              # fond noir
    draw = ImageDraw.Draw(img)
    # centrage vertical du bloc de texte dans la hauteur d'affichage
    y_off = (height - glyph_h) // 2 - bbox[1]
    draw.text((-bbox[0], y_off), text, fill=255, font=font)

    px = img.load()
    columns = []  # colonnes de 'height' valeurs 0/1
    for x in range(width):
        col = [1 if px[x, y] > 100 else 0 for y in range(height)]
        columns.append(col)
    return columns, img


def image_columns(path: str, height: int):
    """Charge une image, la met à la hauteur voulue, seuille en noir/blanc."""
    img = Image.open(path).convert("L")
    w = max(1, round(img.width * height / img.height))
    img = img.resize((w, height))
    px = img.load()
    columns = [[1 if px[x, y] > 100 else 0 for y in range(height)]
               for x in range(w)]
    return columns, img


# ----------------------------------------------------------- encodage protocole
def encode_bitmap(columns, height: int) -> bytes:
    """Colonnes on/off -> octets. ceil(h/8) octets/colonne, ligne du haut = bit de poids fort."""
    bpc = (height + 7) // 8
    out = bytearray()
    for col in columns:
        for g in range(bpc):
            b = 0
            for k in range(8):
                row = 8 * g + k
                if row < height and col[row]:
                    b |= 1 << (7 - k)
            out.append(b)
    return bytes(out)


def encode_colors(n_cols: int, rgb) -> bytes:
    r, g, b = rgb
    return bytes([r, g, b]) * n_cols


def build_dats(total_len: int, bitmap_len: int) -> bytes:
    args = total_len.to_bytes(2, "big") + bitmap_len.to_bytes(2, "big") + b"\x00"
    return enc_cmd("DATS", args)


def chunk_packets(payload: bytes):
    """Découpe en paquets [len+1][index][<=98 octets] pour …960a."""
    packets, i, idx = [], 0, 0
    maxd = MAX_PACKET - 2
    while i < len(payload):
        data = payload[i:i + maxd]
        packets.append(bytes([len(data) + 1, idx]) + data)
        i += len(data)
        idx += 1
    return packets


# ----------------------------------------------------------- upload BLE
async def upload(address, payload, bitmap_len, n_cols, mode_code, speed, wait, verbose):
    packets = chunk_packets(payload)
    total = len(payload)
    print(f"[{ts()}] total={total} o, bitmap={bitmap_len} o, "
          f"colors={total - bitmap_len} o, {len(packets)} paquet(s)")

    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def on_notify(_c, data: bytearray):
        resp = dec_notify(bytes(data))
        if verbose:
            print(f"[{ts()}] NOTIF <- {resp!r}")
        loop.call_soon_threadsafe(queue.put_nowait, resp)

    async def wait_for(expected):
        while True:
            resp = await asyncio.wait_for(queue.get(), timeout=wait)
            if resp.startswith(expected):
                return resp

    async with BleakClient(address) as client:
        await client.start_notify(NOTIFY_CHAR, on_notify)

        # 1) DATS
        await client.write_gatt_char(CMD_CHAR, build_dats(total, bitmap_len), response=True)
        print(f"[{ts()}] DATS envoyé, attente DATSOK…")
        await wait_for("DATSOK")

        # 2) paquets, chacun acquitté par REOK
        for n, pkt in enumerate(packets):
            await client.write_gatt_char(DATA_CHAR, pkt, response=False)
            if verbose:
                print(f"[{ts()}] paquet {n} envoyé ({len(pkt)} o), attente REOK…")
            await wait_for("REOK")

        # 3) DATCP -> DATCPOK
        await client.write_gatt_char(CMD_CHAR, enc_cmd("DATCP"), response=True)
        print(f"[{ts()}] DATCP envoyé, attente DATCPOK…")
        await wait_for("DATCPOK")

        # vitesse (optionnelle) puis mode d'affichage
        if speed is not None:
            await client.write_gatt_char(CMD_CHAR, enc_cmd("SPEED", bytes([speed])), response=True)
            await asyncio.sleep(0.15)
        await client.write_gatt_char(CMD_CHAR, enc_cmd("MODE", bytes([mode_code])), response=True)
        await asyncio.sleep(0.3)
        try:
            await client.stop_notify(NOTIFY_CHAR)
        except Exception:
            pass

    print(f"[{ts()}] ✅ Upload terminé.")


# ----------------------------------------------------------- main
def main():
    p = argparse.ArgumentParser(description="Afficher texte/image sur lunettes LED Heaton")
    p.add_argument("address", help="adresse BLE, ex AA:BB:CC:DD:EE:FF")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--text", help="texte à afficher")
    src.add_argument("--image", help="chemin d'une image à afficher")
    p.add_argument("--height", type=int, default=12, help="hauteur d'affichage en px (défaut 12)")
    p.add_argument("--font-size", type=int, default=0, help="taille de police (0 = auto = hauteur)")
    p.add_argument("--font", default="", help="chemin d'un .ttf (sinon Arial/DejaVu/défaut)")
    p.add_argument("--color", type=int, nargs=3, metavar=("R", "G", "B"),
                   default=[255, 255, 255], help="couleur RGB (défaut blanc)")
    p.add_argument("--mode", choices=["auto", "steady", "blink", "scroll-left", "scroll-right"],
                   default="auto",
                   help="mode d'affichage (auto = défilement si le texte dépasse 36 px)")
    p.add_argument("--speed", type=int, default=None, metavar="N",
                   help="vitesse de défilement 0-255 (optionnel)")
    p.add_argument("--preview", metavar="PNG",
                   help="écrit le bitmap rendu dans ce PNG et n'envoie RIEN")
    p.add_argument("-w", "--wait", type=float, default=5.0, help="timeout par réponse (s)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    for v in args.color:
        if not 0 <= v <= 255:
            sys.exit("--color doit être 3 valeurs 0-255")

    font_size = args.font_size if args.font_size > 0 else args.height
    if args.text is not None:
        columns, img = render_columns(args.text, args.height, font_size, args.font)
    else:
        columns, img = image_columns(args.image, args.height)

    if not columns:
        sys.exit("Rien à afficher (texte/image vide).")

    if args.preview:
        img.save(args.preview)
        print(f"Aperçu écrit dans {args.preview} — {len(columns)} colonnes x {args.height} px. "
              f"(rien envoyé)")
        return

    bitmap = encode_bitmap(columns, args.height)
    colors = encode_colors(len(columns), args.color)
    payload = bitmap + colors

    n = len(columns)
    if args.mode == "auto":
        mode = "scroll-left" if n > DISPLAY_W else "steady"
    else:
        mode = args.mode
    mode_code = MODES[mode]

    fit = "tient sur l'écran" if n <= DISPLAY_W else f"dépasse {DISPLAY_W} px → défilement"
    print(f"[{ts()}] {n} colonnes ({fit}) | mode: {mode}")
    if args.speed is not None and not 0 <= args.speed <= 255:
        sys.exit("--speed doit être 0-255")

    try:
        asyncio.run(upload(args.address, payload, len(bitmap), n,
                           mode_code, args.speed, args.wait, args.verbose))
    except asyncio.TimeoutError:
        sys.exit(f"[{ts()}] ⏱️  Pas de réponse attendue à temps. "
                 f"Réessaie (l'appli mobile est-elle bien fermée ?).")
    except KeyboardInterrupt:
        print("\nArrêt.")


if __name__ == "__main__":
    main()
