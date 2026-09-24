#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "bleak>=0.22",
#     "pycryptodome>=3.20",
# ]
# ///
"""
glasses_status.py — Diagnostic des lunettes Heaton : présence BLE, connexion, GATT, test LED.

Utile quand l'écran est noir et qu'on veut savoir si les lunettes sont VIVANTES (allumées,
connectables, LED fonctionnelles) ou réellement en panne. Il n'existe pas de commande batterie
dans le protocole, donc pas de pourcentage — mais on vérifie tout le reste, dont un TEST VISUEL.

    uv run tools/glasses_status.py --scan                 # cherche les lunettes en BLE (sont-elles allumées ?)
    uv run tools/glasses_status.py <addr>                 # statut lecture seule (connexion + GATT), rien d'affiché
    uv run tools/glasses_status.py <addr> --test          # + test LED : plein écran BLANC puis ROUGE/VERT/BLEU

⚠️  Écran noir = très probablement NORMAL (canevas DIY noir, veille, ou batterie à plat) — pas des
    LED cramées. Le --test tranche : si l'écran s'allume en blanc, les LED vont bien.
⚠️  Ferme l'appli mobile avant : une seule connexion BLE à la fois.
"""

import asyncio
import sys
from datetime import datetime

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    sys.exit("bleak manquant : utilise 'uv run'")
try:
    from Crypto.Cipher import AES
except ImportError:
    sys.exit("pycryptodome manquant : utilise 'uv run'")

OUT_W, OUT_H = 36, 12
NPIX = OUT_W * OUT_H
CMD_CHAR = "d44bc439-abfd-45a2-b575-925416129600"
NOTIFY_CHAR = "d44bc439-abfd-45a2-b575-925416129601"
RT_CHAR = "d44bc439-abfd-45a2-b575-92541612960b"
KEY = bytes.fromhex("32672f7974ad43451d9c6c894a0e8764")
PAD = 16


def ts():
    return datetime.now().strftime("%H:%M:%S")


def hexdump(b):
    return " ".join(f"{x:02x}" for x in bytes(b))


def ascii_of(b):
    return "".join(chr(x) if 32 <= x < 127 else "." for x in bytes(b))


def enc_cmd(op, args=b""):
    body = bytes([len(op) + len(args)]) + op.encode("ascii") + args
    body += b"\x00" * ((PAD - (len(body) % PAD)) % PAD)
    return AES.new(KEY, AES.MODE_ECB).encrypt(body)


def enter_diy():
    return enc_cmd("SMVEW", bytes([1]))


def light_cmd(level):
    return enc_cmd("LIGHT", bytes([level & 0xFF]))


def rt_full(color, max_points):
    """Trames …960b pour peindre les 432 LED d'une seule couleur."""
    r, g, b = color
    pts = [(c, row) for c in range(OUT_W) for row in range(OUT_H)]
    out = []
    for k in range(0, len(pts), max_points):
        chunk = pts[k:k + max_points]
        body = bytearray([3 + 2 * len(chunk), r & 0xFF, g & 0xFF, b & 0xFF])
        for (col, row) in chunk:
            body += bytes([col, row])
        out.append(bytes(body))
    return out


async def do_scan(target=None, timeout=8.0):
    print(f"[{ts()}] scan BLE {timeout:.0f}s…")
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    hits = []
    for addr, (dev, adv) in found.items():
        name = dev.name or (adv.local_name if adv else None) or "?"
        rssi = adv.rssi if adv else "?"
        is_glasses = "glass" in name.lower()
        if target and addr.lower() != target.lower():
            continue
        hits.append((name, addr, rssi, is_glasses))
    if target:
        if hits:
            n, a, r, _ = hits[0]
            print(f"[{ts()}] ✅ TROUVÉE : {n} [{a}] RSSI={r} dBm — les lunettes sont ALLUMÉES et visibles.")
        else:
            print(f"[{ts()}] ❌ {target} introuvable — éteintes, en veille, batterie à plat, "
                  "ou déjà connectées à l'appli mobile ?")
        return
    if not hits:
        print(f"[{ts()}] aucun appareil BLE trouvé.")
    for n, a, r, g in sorted(hits, key=lambda h: (not h[3], -(_num(h[2])))):
        print(f"   {'👓' if g else '  '} {n:24s} [{a}] RSSI={r} dBm")


def _num(x):
    try:
        return int(x)
    except Exception:
        return -999


async def do_status(address, test=False):
    print(f"[{ts()}] connexion à {address}…")
    try:
        async with BleakClient(address) as client:
            print(f"[{ts()}] ✅ CONNECTÉ — la pile BLE des lunettes répond (firmware vivant).")
            try:
                print(f"[{ts()}] MTU négocié : {client.mtu_size}")
            except Exception:
                pass

            print(f"\n[{ts()}] --- GATT (caractéristiques lisibles) ---")
            for svc in client.services:
                for ch in svc.characteristics:
                    if "read" in ch.properties:
                        try:
                            val = await client.read_gatt_char(ch)
                            print(f"   {ch.uuid}  = {hexdump(val)}  | {ascii_of(val)}")
                        except Exception as e:
                            print(f"   {ch.uuid}  (lecture impossible : {e})")

            if not test:
                print(f"\n[{ts()}] Statut lecture seule terminé. Ajoute --test pour vérifier les LED.")
                return

            # --- test LED visuel ---
            mtu = 23
            try:
                mtu = client.mtu_size or 23
            except Exception:
                pass
            mp = max(1, (mtu - 3 - 4) // 2)
            print(f"\n[{ts()}] --- TEST LED (luminosité max, mode DIY) ---")
            await client.write_gatt_char(CMD_CHAR, light_cmd(255), response=True)
            await client.write_gatt_char(CMD_CHAR, enter_diy(), response=True)
            await asyncio.sleep(0.4)
            for name, color in [("BLANC", (255, 255, 255)), ("ROUGE", (255, 0, 0)),
                                ("VERT", (0, 255, 0)), ("BLEU", (0, 0, 255)), ("noir", (0, 0, 0))]:
                print(f"[{ts()}] plein écran {name} …")
                for pkt in rt_full(color, mp):
                    await client.write_gatt_char(RT_CHAR, pkt, response=True)
                await asyncio.sleep(1.5)
            print(f"\n[{ts()}] ✅ Test fini. Si l'écran s'est allumé en blanc/R/V/B, les LED VONT BIEN.")
            print("   Un écran resté noir malgré tout = souci d'affichage/alim (ou luminosité 0).")
    except Exception as e:
        print(f"[{ts()}] ❌ Connexion impossible : {e}")
        print("   -> éteintes / en veille / batterie à plat / encore liées à l'appli mobile. "
              "Essaie 'uv run tools/glasses_status.py --scan' d'abord, et recharge-les.")


def main():
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    if args[0] == "--scan":
        asyncio.run(do_scan())
        return
    address = args[0]
    test = "--test" in args[1:]
    try:
        # petit scan de présence d'abord (informatif)
        asyncio.run(do_scan(target=address, timeout=6.0))
        asyncio.run(do_status(address, test=test))
    except asyncio.TimeoutError:
        sys.exit("Timeout — lunettes hors de portée ou éteintes ?")
    except KeyboardInterrupt:
        print("\nArrêt.")


if __name__ == "__main__":
    main()
