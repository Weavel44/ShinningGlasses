#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "bleak>=0.22",
#     "pycryptodome>=3.20",
# ]
# ///
"""
glasses_realtime.py — SONDE du canal temps-réel 960b des lunettes Heaton (mode DIY live).

Décodé dans l'appli : en mode DIY, chaque pixel touché est poussé sur la characteristic …960b
sous forme d'une petite trame BRUTE (non chiffrée) :

    [len][R][G][B][col0][row0][col1][row1]…      len = 3 + 2*nb_points     (col 0-35, row 0-11)

Avant, on entre en DIY via une commande chiffrée sur …9600 ; à la fin on sort (option sauver).
Ce script sert à VALIDER 3 hypothèses avant de bâtir un lecteur GIF par différence de frames :
  1. persistance : les pixels envoyés restent-ils affichés (canevas accumulé) ?
  2. capacité    : combien de points par trame le firmware accepte-t-il (8 ? plus ?) ?
  3. cadence     : peut-on enchaîner les trames rapidement ?

Commandes :
    uv run tools/glasses_realtime.py <addr> diy             # entrer DIY + confirmer (accusé ATT + capture 9601/fd02)
    uv run tools/glasses_realtime.py <addr> corners        # 4 coins colorés, un par un (persistance + mapping)
    uv run tools/glasses_realtime.py <addr> burst 30        # 30 points en UNE trame (test de la capacité)
    uv run tools/glasses_realtime.py <addr> sweep           # un pixel blanc balaie l'écran (cadence)
    uv run tools/glasses_realtime.py <addr> pixel 10 5 255 0 0   # un pixel : col=10 row=5 en rouge
    uv run tools/glasses_realtime.py <addr> exit            # sortir du mode DIY (sans sauver)

⚠️  Ferme l'appli mobile avant : une seule connexion BLE à la fois.
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
CMD_CHAR = "d44bc439-abfd-45a2-b575-925416129600"     # commandes chiffrées (entrer/sortir DIY)
NOTIFY_CHAR = "d44bc439-abfd-45a2-b575-925416129601"
RT_CHAR = "d44bc439-abfd-45a2-b575-92541612960b"      # canal temps-réel (WRITE3), trames brutes
FD02_CHAR = "0000fd02-0000-1000-8000-00805f9b34fb"    # 2e canal notify (vendeur), jamais exploité
KEY = bytes.fromhex("32672f7974ad43451d9c6c894a0e8764")
PAD = 16


def ts():
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def hexdump(b):
    return " ".join(f"{x:02x}" for x in b)


def enc_cmd(op, args=b""):
    body = bytes([len(op) + len(args)]) + op.encode("ascii") + args
    body += b"\x00" * ((PAD - (len(body) % PAD)) % PAD)
    return AES.new(KEY, AES.MODE_ECB).encrypt(body)


# Entrée/sortie DIY : opcode "SMVEW" (0x53 0x4d 0x56 0x45 0x57) + octet mode.
def enter_diy():
    return enc_cmd("SMVEW", bytes([1]))


def exit_diy(save=False):
    return enc_cmd("SMVEW", bytes([2 if save else 0]))


def rt_frame(points, rgb):
    """points: liste de (col, row) ; rgb: (r,g,b). Trame brute pour …960b."""
    r, g, b = rgb
    body = bytearray([3 + 2 * len(points), r & 0xFF, g & 0xFF, b & 0xFF])
    for (col, row) in points:
        body += bytes([col & 0xFF, row & 0xFF])
    return bytes(body)


async def with_glasses(address, coro):
    async with BleakClient(address) as client:
        # notifications : …9601 (chiffré) + …fd02 (brut) pour capter tout accusé caché
        def on_notify_9601(_c, data):
            data = bytes(data)
            print(f"[{ts()}] NOTIF 9601 (brut) <- {hexdump(data)}")
            if data and len(data) % 16 == 0:
                clear = AES.new(KEY, AES.MODE_ECB).decrypt(data)
                n = clear[0]
                if 1 + n <= len(clear):
                    print(f"[{ts()}]        9601 déchiffré -> \"{clear[1:1+n].decode('ascii','replace')}\"")

        def on_notify_fd02(_c, data):
            print(f"[{ts()}] NOTIF fd02 <- {hexdump(bytes(data))}")

        try:
            await client.start_notify(NOTIFY_CHAR, on_notify_9601)
        except Exception as e:
            print(f"[{ts()}] (abonnement 9601 impossible : {e})")
        try:
            await client.start_notify(FD02_CHAR, on_notify_fd02)
        except Exception as e:
            print(f"[{ts()}] (abonnement fd02 impossible : {e})")
        await coro(client)


async def send_cmd(client, data, label):
    print(f"[{ts()}] CMD  {label} -> {hexdump(data[:8])}…")
    await client.write_gatt_char(CMD_CHAR, data, response=True)
    await asyncio.sleep(0.2)


async def send_rt(client, frame, label=""):
    print(f"[{ts()}] 960b {label} ({len(frame)} o) -> {hexdump(frame)}")
    await client.write_gatt_char(RT_CHAR, frame, response=False)


# ---------------------------------------------------------------------------
# Scénarios de test
# ---------------------------------------------------------------------------
async def scenario_hold(client):
    """Peint un écran BLANC puis GARDE la connexion 20 s SANS rien renvoyer.
    Tranche la question : le DIY tient-il pendant la connexion, ou revient-il en animation ?"""
    await send_cmd(client, enter_diy(), "entrer DIY")
    await asyncio.sleep(0.5)
    mtu = 23
    try:
        mtu = client.mtu_size or 23
    except Exception:
        pass
    mp = max(1, (mtu - 3 - 4) // 2)
    pts = [(c, r) for c in range(OUT_W) for r in range(OUT_H)]
    print(f"[{ts()}] peinture ÉCRAN BLANC complet ({len(pts)} pixels)…")
    for k in range(0, len(pts), mp):
        chunk = pts[k:k + mp]
        body = bytearray([3 + 2 * len(chunk), 255, 255, 255])
        for (col, row) in chunk:
            body += bytes([col, row])
        await client.write_gatt_char(RT_CHAR, bytes(body), response=True)
    print(f"[{ts()}] ✅ Écran blanc envoyé. Je NE renvoie plus rien pendant 20 s (connexion maintenue).")
    print("   >>> REGARDE LES LUNETTES : l'écran reste-t-il BLANC, ou l'animation revient-elle ?")
    for s in range(20, 0, -1):
        print(f"   … maintien {s:2d} s (toujours connecté, aucune donnée envoyée)", end="\r")
        await asyncio.sleep(1)
    print(f"\n[{ts()}] Fin du maintien. (La déconnexion qui suit fera normalement revenir l'animation.)")


async def scenario_diy(client):
    """Entre en DIY proprement et rapporte TOUTE forme de confirmation disponible."""
    print(f"[{ts()}] écriture SMVEW (entrer DIY) en response=True (accusé ATT requis)…")
    try:
        await client.write_gatt_char(CMD_CHAR, enter_diy(), response=True)
        print(f"[{ts()}] ✅ ACCUSÉ ATT : le firmware a bien REÇU la commande d'entrée DIY.")
    except Exception as e:
        print(f"[{ts()}] ❌ écriture refusée : {e}")
        return
    print(f"[{ts()}] écoute 3 s d'un éventuel accusé applicatif (9601 / fd02)…")
    await asyncio.sleep(3.0)
    print(f"[{ts()}] peinture d'un pixel BLANC témoin en (0,0) (response=True)…")
    await client.write_gatt_char(RT_CHAR, rt_frame([(0, 0)], (255, 255, 255)), response=True)
    await asyncio.sleep(1.0)
    print(f"\n[{ts()}] Conclusion :")
    print("   • Une ligne 'NOTIF 9601/fd02' ci-dessus = accusé applicatif d'entrée DIY (retour de code).")
    print("   • Sinon : seul l'accusé ATT confirme la réception (le firmware n'émet pas d'ack DIY).")
    print("   • Le pixel blanc en haut-gauche allumé = mode DIY réellement actif et affichage vivant.")


async def scenario_corners(client):
    await send_cmd(client, enter_diy(), "entrer DIY")
    await asyncio.sleep(0.5)
    seq = [((0, 0), (255, 0, 0), "ROUGE haut-gauche"),
           ((OUT_W - 1, 0), (0, 255, 0), "VERT haut-droit"),
           ((0, OUT_H - 1), (0, 0, 255), "BLEU bas-gauche"),
           ((OUT_W - 1, OUT_H - 1), (255, 255, 255), "BLANC bas-droit")]
    for (col, row), rgb, desc in seq:
        print(f"[{ts()}] --> {desc}  (col={col}, row={row})")
        await send_rt(client, rt_frame([(col, row)], rgb), desc)
        await asyncio.sleep(1.5)
    print(f"\n[{ts()}] ✅ Fini. QUESTION : vois-tu les 4 coins allumés EN MÊME TEMPS (persistance) ?")
    print("   Et le rouge est-il bien en haut-gauche, etc. ? Les pixels apparus restent-ils ?")


async def scenario_burst(client, n):
    await send_cmd(client, enter_diy(), "entrer DIY")
    await asyncio.sleep(0.5)
    # n points sur la ligne du haut (row 0), colonnes 0..n-1, en UNE seule trame
    n = max(1, min(n, OUT_W))
    pts = [(c, 0) for c in range(n)]
    frame = rt_frame(pts, (255, 128, 0))
    print(f"[{ts()}] trame unique de {n} points ({len(frame)} o) — teste la capacité par trame")
    await send_rt(client, frame, f"{n} points")
    await asyncio.sleep(0.3)
    print(f"\n[{ts()}] ✅ QUESTION : combien de pixels ORANGE sont allumés sur la ligne du haut ?")
    print(f"   {n} attendus. Si seulement 8 -> le firmware plafonne à 8 points/trame.")


async def scenario_sweep(client):
    await send_cmd(client, enter_diy(), "entrer DIY")
    await asyncio.sleep(0.4)
    print(f"[{ts()}] balayage d'un pixel blanc, ligne du milieu — observe la fluidité")
    t0 = asyncio.get_event_loop().time()
    for col in range(OUT_W):
        await send_rt(client, rt_frame([(col, OUT_H // 2)], (255, 255, 255)))
        await asyncio.sleep(0.03)
    dt = asyncio.get_event_loop().time() - t0
    print(f"\n[{ts()}] ✅ {OUT_W} trames en {dt:.2f}s ({OUT_W/dt:.1f} trames/s).")
    print("   QUESTION : le balayage est-il fluide ? laisse-t-il une traînée (persistance) ou 1 seul point ?")


async def scenario_pixel(client, col, row, rgb):
    await send_cmd(client, enter_diy(), "entrer DIY")
    await asyncio.sleep(0.3)
    await send_rt(client, rt_frame([(col, row)], rgb), f"col={col} row={row} rgb={rgb}")
    print(f"\n[{ts()}] ✅ Pixel envoyé.")


async def scenario_exit(client, save=False):
    await send_cmd(client, exit_diy(save), f"sortir DIY (save={save})")


def main():
    a = sys.argv[1:]
    if len(a) < 2:
        sys.exit(__doc__)
    address, cmd = a[0], a[1]

    if cmd == "hold":
        coro = scenario_hold
    elif cmd == "diy":
        coro = scenario_diy
    elif cmd == "corners":
        coro = scenario_corners
    elif cmd == "burst":
        n = int(a[2]) if len(a) > 2 else 20
        coro = lambda c: scenario_burst(c, n)
    elif cmd == "sweep":
        coro = scenario_sweep
    elif cmd == "pixel":
        if len(a) < 7:
            sys.exit("Usage : pixel <col> <row> <r> <g> <b>")
        col, row, r, g, b = map(int, a[2:7])
        coro = lambda c: scenario_pixel(c, col, row, (r, g, b))
    elif cmd == "exit":
        save = len(a) > 2 and a[2] == "save"
        coro = lambda c: scenario_exit(c, save)
    else:
        sys.exit(f"Commande inconnue : {cmd}")

    try:
        asyncio.run(with_glasses(address, coro))
    except asyncio.TimeoutError:
        sys.exit("Timeout — appli mobile fermée ? lunettes allumées ?")
    except KeyboardInterrupt:
        print("\nArrêt.")


if __name__ == "__main__":
    main()
