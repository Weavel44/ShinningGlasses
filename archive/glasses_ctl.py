#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "bleak>=0.22",
#     "pycryptodome>=3.20",
# ]
# ///
"""
glasses_ctl.py — Contrôle des lunettes LED Heaton (Shining Glasses / protocole Shining Mask)

Lancement (uv gère Python + dépendances tout seul) :
    uv run archive/glasses_ctl.py <adresse> <commande> [args]

Exemples :
    uv run archive/glasses_ctl.py AA:BB:CC:DD:EE:FF light 128
    uv run archive/glasses_ctl.py AA:BB:CC:DD:EE:FF image 0
    uv run archive/glasses_ctl.py AA:BB:CC:DD:EE:FF anim 3
    uv run archive/glasses_ctl.py AA:BB:CC:DD:EE:FF mode scroll-left
    uv run archive/glasses_ctl.py AA:BB:CC:DD:EE:FF speed 5
    uv run archive/glasses_ctl.py AA:BB:CC:DD:EE:FF color 255 0 0
    uv run archive/glasses_ctl.py AA:BB:CC:DD:EE:FF raw IMAG 02       # échappatoire : OP + args hex
    uv run archive/glasses_ctl.py AA:BB:CC:DD:EE:FF check             # nombre d'images DIY stockées

Protocole (identique au Shining Mask) :
  - Commandes AES-128-ECB, clé statique, écrites sur la caractéristique …9600
  - Trame en clair : [1 octet longueur = len(OP)+len(args)] + OP(ASCII) + args, padding 0x00 -> 16
  - Réponses lues sur la caractéristique notify …9601

⚠️ Ferme l'appli mobile avant : une seule connexion à la fois.
"""

import argparse
import asyncio
import sys
from datetime import datetime

try:
    from bleak import BleakClient
except ImportError:
    sys.exit("bleak manquant :  utilise 'uv run', ou 'pip install bleak'")
try:
    from Crypto.Cipher import AES
except ImportError:
    sys.exit("pycryptodome manquant :  utilise 'uv run', ou 'pip install pycryptodome'")


# --- UUID (confirmés par le dump des lunettes) ---
CMD_CHAR = "d44bc439-abfd-45a2-b575-925416129600"   # commandes chiffrées
NOTIFY_CHAR = "d44bc439-abfd-45a2-b575-925416129601"  # réponses
# DATA_CHAR = "d44bc439-abfd-45a2-b575-92541612960a"  # upload image (étape suivante)

KEY = bytes.fromhex("32672f7974ad43451d9c6c894a0e8764")

MODES = {
    "steady": 0x01,
    "blink": 0x02,
    "scroll-left": 0x03,
    "scroll-right": 0x04,
    "steady2": 0x05,
}


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def enc_cmd(op: str, args: bytes = b"") -> bytes:
    """Construit et chiffre une trame de commande (AES-128-ECB)."""
    body = bytes([len(op) + len(args)]) + op.encode("ascii") + args
    pad = (16 - (len(body) % 16)) % 16
    body += b"\x00" * pad
    return AES.new(KEY, AES.MODE_ECB).encrypt(body)


def build_frame(cmd: str, values: list[str]) -> tuple[str, bytes]:
    """Retourne (op, args) pour la commande CLI demandée."""
    def u8(x: str) -> int:
        n = int(x, 0)
        if not 0 <= n <= 255:
            sys.exit(f"Valeur hors plage 0-255 : {x}")
        return n

    if cmd == "light":
        return "LIGHT", bytes([u8(values[0])])
    if cmd == "image":
        return "IMAG", bytes([u8(values[0])])
    if cmd == "anim":
        return "ANIM", bytes([u8(values[0])])
    if cmd == "speed":
        return "SPEED", bytes([u8(values[0])])
    if cmd == "mode":
        name = values[0].lower()
        if name not in MODES:
            sys.exit(f"Mode inconnu. Choix : {', '.join(MODES)}")
        return "MODE", bytes([MODES[name]])
    if cmd == "color":
        if len(values) != 3:
            sys.exit("color attend 3 valeurs : R G B (0-255 chacune)")
        r, g, b = (u8(v) for v in values)
        # Format observé sur le masque : FC + <flag 00/01> + 3 octets couleur.
        # L'ordre des octets couleur est à confirmer sur les lunettes (voir README).
        return "FC", bytes([0x01, r, g, b])
    if cmd == "check":
        return "CHEC", b""
    if cmd == "raw":
        if not values:
            sys.exit("raw attend au moins un OP, ex :  raw IMAG 02")
        op = values[0]
        args = bytes(int(v, 16) for v in values[1:])
        return op, args
    sys.exit(f"Commande inconnue : {cmd}")


async def run(address: str, op: str, args: bytes, wait: float):
    packet = enc_cmd(op, args)
    print(f"[{ts()}] {op} args={args.hex(' ') or '(aucun)'}  -> chiffré {packet.hex(' ')}")

    async with BleakClient(address) as client:
        replies = []

        def on_notify(_c, data: bytearray):
            b = bytes(data)
            txt = "".join(chr(x) if 32 <= x < 127 else "." for x in b)
            replies.append(b)
            print(f"[{ts()}] NOTIF: {b.hex(' ')}   | {txt}")

        try:
            await client.start_notify(NOTIFY_CHAR, on_notify)
        except Exception as e:
            print(f"[{ts()}] (notify indispo : {e})")

        await client.write_gatt_char(CMD_CHAR, packet, response=True)
        print(f"[{ts()}] Commande envoyée. Écoute des réponses {wait:.1f}s…")
        await asyncio.sleep(wait)

        try:
            await client.stop_notify(NOTIFY_CHAR)
        except Exception:
            pass

    if not replies:
        print("(aucune réponse — normal pour light/image/anim/mode ; "
              "vérifie l'effet sur les lunettes)")


def main():
    p = argparse.ArgumentParser(description="Contrôle des lunettes LED Heaton")
    p.add_argument("address", help="adresse BLE, ex AA:BB:CC:DD:EE:FF")
    p.add_argument("command",
                   help="light|image|anim|speed|mode|color|check|raw")
    p.add_argument("values", nargs="*", help="arguments de la commande")
    p.add_argument("-w", "--wait", type=float, default=1.5,
                   help="durée d'écoute des réponses (s)")
    args = p.parse_args()

    op, cmd_args = build_frame(args.command.lower(), args.values)
    try:
        asyncio.run(run(args.address, op, cmd_args, args.wait))
    except KeyboardInterrupt:
        print("\nArrêt.")


if __name__ == "__main__":
    main()
