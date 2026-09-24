#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "pycryptodome>=3.20",
# ]
# ///
"""
analyze_snoop.py — Décode un journal Bluetooth (btsnoop_hci.log) pour comprendre le format
d'upload d'image de l'appli Shining Mask/Glasses, en particulier le mode DIY couleur.

    uv run archive/analyze_snoop.py btsnoop_hci.log

Ce que fait l'outil :
  • parcourt la capture, extrait les écritures GATT (ATT Write) et les notifications
  • déchiffre (AES-128-ECB) les commandes envoyées sur la caractéristique de commande
    -> affiche DATS / DATCP / MODE / FC / … en clair
  • réassemble les paquets de données bruts (canal image)
  • à partir du DATS (longueur totale + longueur bitmap), en DÉDUIT la taille du tableau
    couleur et indique :
        - couleur PAR COLONNE   si  colors == colonnes*3
        - couleur PAR PIXEL     si  colors == colonnes*lignes*3
        - autre schéma          sinon (à inspecter)

Astuce capture (Android) : Options développeur -> « Activer le journal de trace Bluetooth HCI ».
Dessiner un motif DIY couleur DISTINCTIF, l'envoyer, puis récupérer le fichier
(bug report, ou /sdcard/.../btsnoop_hci.log selon l'appareil).
"""

import struct
import sys

try:
    from Crypto.Cipher import AES
except ImportError:
    sys.exit("pycryptodome manquant :  utilise 'uv run'")

KEY = bytes.fromhex("32672f7974ad43451d9c6c894a0e8764")
DISPLAY_H = 12   # hauteur connue des lunettes (pour le test par-pixel)

ATT_WRITE_REQ = 0x12
ATT_WRITE_CMD = 0x52
ATT_NOTIFY = 0x1B
ATT_HVCONF = 0x1D


def try_decrypt(value: bytes):
    """Si value est un multiple de 16 et déchiffre en [len][ASCII op…], renvoie (op, args, clear)."""
    if not value or len(value) % 16:
        return None
    try:
        clear = AES.new(KEY, AES.MODE_ECB).decrypt(value)
    except Exception:
        return None
    n = clear[0]
    if n == 0 or 1 + n > len(clear):
        return None
    body = clear[1:1 + n]
    # op = préfixe alphabétique majuscule
    op = bytearray()
    for c in body:
        if 65 <= c <= 90 or 48 <= c <= 57:
            op.append(c)
        else:
            break
    if not op:
        return None
    return op.decode(), bytes(body[len(op):]), clear


def parse_btsnoop(path):
    """Générateur de (direction, hci_bytes). direction: 'TX' (host->ctrl) ou 'RX'."""
    with open(path, "rb") as f:
        hdr = f.read(16)
        if hdr[:8] != b"btsnoop\x00":
            sys.exit("Ce n'est pas un fichier btsnoop (en-tête manquant).")
        _ver, datalink = struct.unpack(">II", hdr[8:16])
        while True:
            rec = f.read(24)
            if len(rec) < 24:
                break
            orig_len, incl_len, flags, _drops, _ts = struct.unpack(">IIIIq", rec)
            data = f.read(incl_len)
            if len(data) < incl_len:
                break
            direction = "RX" if (flags & 0x01) else "TX"
            yield direction, datalink, data


def extract_att(datalink, data):
    """Extrait (opcode, handle, value) d'un paquet HCI ACL contenant de l'ATT, sinon None."""
    # datalink 1002 = H4 (1er octet = type paquet), 1001 = HCI nu
    buf = data
    if datalink == 1002:
        if not buf:
            return None
        ptype = buf[0]
        buf = buf[1:]
        if ptype != 0x02:      # 0x02 = ACL Data
            return None
    # ACL header : handle+flags (2, LE), total_len (2, LE)
    if len(buf) < 4:
        return None
    _acl_handle_flags, acl_len = struct.unpack("<HH", buf[:4])
    l2 = buf[4:4 + acl_len]
    # L2CAP header : length (2 LE), CID (2 LE)
    if len(l2) < 4:
        return None
    _l2len, cid = struct.unpack("<HH", l2[:4])
    if cid != 0x0004:          # 0x0004 = ATT
        return None
    att = l2[4:]
    if len(att) < 1:
        return None
    opcode = att[0]
    if opcode in (ATT_WRITE_REQ, ATT_WRITE_CMD):
        if len(att) < 3:
            return None
        handle = struct.unpack("<H", att[1:3])[0]
        return opcode, handle, att[3:]
    if opcode in (ATT_NOTIFY, ATT_HVCONF):
        if len(att) < 3:
            return None
        handle = struct.unpack("<H", att[1:3])[0]
        return opcode, handle, att[3:]
    return None


def main():
    if len(sys.argv) < 2:
        sys.exit("Usage : uv run archive/analyze_snoop.py btsnoop_hci.log")
    path = sys.argv[1]

    writes = []          # (handle, value)  côté host->device
    notifs = []          # (handle, value)  côté device->host
    data_packets = []    # (index, payload) reconstruits depuis le canal image
    dats_info = None     # (total_len, bitmap_len)

    n_seen = 0
    for direction, datalink, data in parse_btsnoop(path):
        att = extract_att(datalink, data)
        if not att:
            continue
        opcode, handle, value = att
        n_seen += 1
        if opcode in (ATT_NOTIFY, ATT_HVCONF):
            notifs.append((handle, value))
        else:
            writes.append((handle, value))

    if n_seen == 0:
        sys.exit("Aucun paquet ATT trouvé. Datalink non géré, ou capture vide/tronquée.\n"
                 "Vérifie que c'est bien un btsnoop_hci.log Android (datalink H4/1002).")

    print(f"== {len(writes)} écritures GATT, {len(notifs)} notifications ==\n")

    print("---- COMMANDES (host -> lunettes) ----")
    handles_cmd = set()
    handles_data = set()
    for handle, value in writes:
        dec = try_decrypt(value)
        if dec:
            op, args, _clear = dec
            handles_cmd.add(handle)
            extra = ""
            if op == "DATS" and len(args) >= 4:
                total_len = int.from_bytes(args[0:2], "big")
                bitmap_len = int.from_bytes(args[2:4], "big")
                dats_info = (total_len, bitmap_len)
                extra = f"   -> total={total_len} o, bitmap={bitmap_len} o"
            print(f"  handle 0x{handle:04x}  {op:6} args={args.hex(' ') or '-'}{extra}")
        else:
            # paquet de données brut : [len][index][data…]
            handles_data.add(handle)
            if len(value) >= 2:
                pkt_len, idx = value[0], value[1]
                payload = value[2:2 + max(0, pkt_len - 1)]
                data_packets.append((idx, payload))
                print(f"  handle 0x{handle:04x}  DATA  idx={idx:3} len={pkt_len:3} "
                      f"{payload[:16].hex(' ')}{'…' if len(payload) > 16 else ''}")
            else:
                print(f"  handle 0x{handle:04x}  ??    {value.hex(' ')}")

    print("\n---- NOTIFICATIONS (lunettes -> host) ----")
    for handle, value in notifs:
        dec = try_decrypt(value)
        if dec:
            op, args, _ = dec
            print(f"  handle 0x{handle:04x}  {op} {args.hex(' ')}")
        else:
            txt = "".join(chr(b) if 32 <= b < 127 else "." for b in value)
            print(f"  handle 0x{handle:04x}  {value.hex(' ')}  | {txt}")

    # -------- reconstruction du buffer uploadé --------
    print("\n---- ANALYSE DU FORMAT ----")
    if not dats_info:
        print("Pas de commande DATS trouvée : impossible de déduire le format.")
        return
    total_len, bitmap_len = dats_info
    color_len = total_len - bitmap_len
    print(f"DATS : total={total_len} o, bitmap={bitmap_len} o, couleur={color_len} o")

    # On suppose l'afficheur 36 de large : bitmap = colonnes * ceil(H/8)
    bpc = (DISPLAY_H + 7) // 8
    if bitmap_len % bpc == 0:
        cols = bitmap_len // bpc
        print(f"Colonnes déduites (bitmap / {bpc}) : {cols}")
        if color_len == cols * 3:
            print("  => COULEUR PAR COLONNE (comme mask-go). Une couleur par colonne.")
        elif color_len == cols * DISPLAY_H * 3:
            print(f"  => 🎉 COULEUR PAR PIXEL ! ({cols}×{DISPLAY_H}×3). "
                  f"C'est le format full-couleur recherché.")
        elif cols and color_len % cols == 0:
            per_col = color_len // cols
            print(f"  => {per_col} octets de couleur par colonne "
                  f"(soit {per_col/3:.1f} couleurs/colonne ?). À inspecter.")
        else:
            print("  => Schéma couleur non standard. Colle-moi la sortie complète.")
    else:
        print("bitmap_len non divisible par le format colonne attendu — l'encodage bitmap "
              "diffère peut-être en mode DIY. Colle-moi la sortie complète.")

    # buffer reconstitué (pour analyse fine)
    if data_packets:
        data_packets.sort(key=lambda p: p[0])
        buf = b"".join(p for _, p in data_packets)
        print(f"\nBuffer données réassemblé : {len(buf)} o (attendu ~{total_len}).")
        if len(buf) >= bitmap_len:
            print(f"  bitmap  = {buf[:bitmap_len].hex()}")
            print(f"  couleur = {buf[bitmap_len:bitmap_len+min(color_len,60)].hex()}"
                  f"{'…' if color_len > 60 else ''}")


if __name__ == "__main__":
    main()
