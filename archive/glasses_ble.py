#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "bleak>=0.22",
# ]
# ///
"""
glasses_ble.py — Outil de reconnaissance BLE pour lunettes LED Heaton (Shining Glasses)

Lancement recommandé (uv gère Python + bleak tout seul) :
    uv run archive/glasses_ble.py scan

Trois sous-commandes :
  scan             Liste les périphériques BLE (nom, adresse, RSSI, service UUIDs, manufacturer data)
  dump   <cible>   Se connecte et énumère tous les services / caractéristiques / descripteurs
  monitor <cible>  Dump puis s'abonne à toutes les caractéristiques notify/indicate et logge les trames

<cible> = adresse MAC (Linux/Windows) ou UUID (macOS), OU un fragment de nom (--by-name).

Dépendance :  pip install bleak
Sous Linux, nécessite BlueZ + un adaptateur BT (vérifier avec `bluetoothctl list`).

Astuce RE : lance `monitor` en parallèle de l'appli mobile officielle n'est PAS possible
(un seul central par périphérique). Pour capturer les trames émises PAR l'appli, utilise
plutôt le HCI snoop log Android + Wireshark. Ce script sert à cartographier le GATT et à
écouter/rejouer côté PC une fois l'appli déconnectée.
"""

import argparse
import asyncio
import sys
from datetime import datetime

try:
    from bleak import BleakScanner, BleakClient
    from bleak.backends.characteristic import BleakGATTCharacteristic
except ImportError:
    sys.exit("bleak manquant :  pip install bleak")


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def hexdump(data: bytes) -> str:
    if not data:
        return "(vide)"
    h = data.hex(" ")
    printable = "".join(chr(b) if 32 <= b < 127 else "." for b in data)
    return f"{h}   | {printable}"


# ---------------------------------------------------------------- scan
async def cmd_scan(timeout: float):
    print(f"[{ts()}] Scan BLE pendant {timeout:.0f}s…\n")
    found = {}

    def on_detect(device, adv):
        found[device.address] = (device, adv)

    scanner = BleakScanner(detection_callback=on_detect)
    await scanner.start()
    await asyncio.sleep(timeout)
    await scanner.stop()

    if not found:
        print("Aucun périphérique. Lunettes allumées ? Pas en charge ? BT actif ?")
        return

    # tri par RSSI décroissant (les plus proches en premier)
    for addr, (dev, adv) in sorted(
        found.items(), key=lambda kv: kv[1][1].rssi or -999, reverse=True
    ):
        name = dev.name or adv.local_name or "(sans nom)"
        rssi = adv.rssi
        print(f"● {name}")
        print(f"    adresse : {addr}")
        print(f"    RSSI    : {rssi} dBm")
        if adv.service_uuids:
            print(f"    services: {', '.join(adv.service_uuids)}")
        if adv.manufacturer_data:
            for cid, val in adv.manufacturer_data.items():
                print(f"    mfr[{cid:#06x}]: {val.hex(' ')}")
        print()

    print("→ Repère l'entrée qui apparaît/disparaît quand tu allumes/éteins les lunettes.")
    print("  Puis :  python archive/glasses_ble.py dump <adresse>")


# ------------------------------------------------------ résolution cible
async def resolve_target(target: str, by_name: bool, timeout: float) -> str:
    if not by_name:
        return target
    print(f"[{ts()}] Recherche d'un périphérique contenant « {target} »…")
    dev = await BleakScanner.find_device_by_filter(
        lambda d, adv: target.lower() in ((d.name or adv.local_name or "").lower()),
        timeout=timeout,
    )
    if dev is None:
        sys.exit(f"Aucun périphérique nommé « {target} » trouvé.")
    print(f"→ trouvé : {dev.name} [{dev.address}]\n")
    return dev.address


# ---------------------------------------------------------------- dump
def _props(char: BleakGATTCharacteristic) -> str:
    return ",".join(char.properties)


async def dump_gatt(client: BleakClient, do_read: bool = True):
    print(f"[{ts()}] Connecté. Énumération du GATT :\n")
    for service in client.services:
        print(f"┌─ Service {service.uuid}")
        if service.description and service.description != "Unknown":
            print(f"│    ({service.description})")
        for char in service.characteristics:
            print(f"│  • Char {char.uuid}  [{_props(char)}]  handle={char.handle}")
            if char.description and char.description != "Unknown":
                print(f"│      desc: {char.description}")
            if do_read and "read" in char.properties:
                try:
                    val = await client.read_gatt_char(char)
                    print(f"│      read: {hexdump(bytes(val))}")
                except Exception as e:
                    print(f"│      read: <erreur: {e}>")
            for desc in char.descriptors:
                print(f"│      descr {desc.uuid}  handle={desc.handle}")
        print("└─")
    print()


async def cmd_dump(target: str):
    async with BleakClient(target) as client:
        await dump_gatt(client, do_read=True)
    print("Terminé. Note les UUID en 'write'/'write-without-response' : c'est là que")
    print("l'appli envoie texte, images et animations aux lunettes.")


# ---------------------------------------------------------------- monitor
async def cmd_monitor(target: str, duration: float):
    async with BleakClient(target) as client:
        await dump_gatt(client, do_read=True)

        def make_handler(uuid):
            def handler(_char, data: bytearray):
                print(f"[{ts()}] NOTIF {uuid}: {hexdump(bytes(data))}")
            return handler

        subscribed = []
        for service in client.services:
            for char in service.characteristics:
                if "notify" in char.properties or "indicate" in char.properties:
                    try:
                        await client.start_notify(char, make_handler(char.uuid))
                        subscribed.append(char)
                        print(f"[{ts()}] Abonné à {char.uuid}")
                    except Exception as e:
                        print(f"[{ts()}] Abonnement {char.uuid} échoué : {e}")

        if not subscribed:
            print("Aucune caractéristique notify/indicate. Rien à écouter.")
            return

        print(f"\n[{ts()}] Écoute {duration:.0f}s (Ctrl-C pour arrêter)…\n")
        try:
            await asyncio.sleep(duration)
        except asyncio.CancelledError:
            pass
        finally:
            for char in subscribed:
                try:
                    await client.stop_notify(char)
                except Exception:
                    pass


# ---------------------------------------------------------------- main
def main():
    p = argparse.ArgumentParser(description="Reconnaissance BLE lunettes LED Heaton")
    sub = p.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("scan", help="lister les périphériques BLE")
    ps.add_argument("-t", "--timeout", type=float, default=8.0)

    pd = sub.add_parser("dump", help="dump du GATT")
    pd.add_argument("target", help="adresse MAC/UUID, ou fragment de nom avec --by-name")
    pd.add_argument("--by-name", action="store_true", help="target est un fragment de nom")
    pd.add_argument("-t", "--timeout", type=float, default=8.0)

    pm = sub.add_parser("monitor", help="dump + écoute des notifications")
    pm.add_argument("target")
    pm.add_argument("--by-name", action="store_true")
    pm.add_argument("-t", "--timeout", type=float, default=8.0, help="timeout de recherche")
    pm.add_argument("-d", "--duration", type=float, default=60.0, help="durée d'écoute (s)")

    args = p.parse_args()

    if args.cmd == "scan":
        asyncio.run(cmd_scan(args.timeout))
    elif args.cmd == "dump":
        tgt = asyncio.run(resolve_target(args.target, args.by_name, args.timeout))
        asyncio.run(cmd_dump(tgt))
    elif args.cmd == "monitor":
        tgt = asyncio.run(resolve_target(args.target, args.by_name, args.timeout))
        try:
            asyncio.run(cmd_monitor(tgt, args.duration))
        except KeyboardInterrupt:
            print("\nArrêt.")


if __name__ == "__main__":
    main()
