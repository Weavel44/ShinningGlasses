#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "bleak>=0.22",
#     "pycryptodome>=3.20",
#     "numpy>=1.24",
#     "soundcard>=0.4.3",
# ]
# ///
"""
glasses_audio.py — Visualiseur spectre temps réel sur les lunettes LED Heaton (36x12).

Capte le son que JOUE ton PC (loopback WASAPI), calcule un spectre en 36 barres, et l'envoie
aux lunettes en continu. Chaque colonne = une barre (montée/descente).

    uv run glasses_audio.py AA:BB:CC:DD:EE:FF
    uv run glasses_audio.py AA:BB:CC:DD:EE:FF --color 0 255 0     # barres vertes
    uv run glasses_audio.py AA:BB:CC:DD:EE:FF --gain 1.5          # plus sensible
    uv run glasses_audio.py --list                                # lister les sorties audio

Notes :
  • Lance de la musique sur le PC : les barres suivent la sortie audio par défaut.
  • Le débit BLE est le facteur limitant : le FPS réel est affiché en continu. 5-10 fps
    est réaliste ; ce sera plus « pulsé » que « fluide » (limite du protocole, pas du code).
  • Ctrl-C pour arrêter proprement.
  ⚠️ Ferme l'appli mobile avant (une seule connexion BLE à la fois).
"""

import argparse
import asyncio
import sys
import threading
import time

try:
    import numpy as np
except ImportError:
    sys.exit("numpy manquant : utilise 'uv run'")
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

SR = 48000          # fréquence d'échantillonnage
FFT_SIZE = 2048     # taille de fenêtre FFT
FMIN, FMAX = 45.0, 16000.0   # plage de fréquences affichée

# état partagé audio -> BLE
_bars = np.zeros(OUT_W, dtype=float)   # niveaux 0..1 par colonne
_bars_lock = threading.Lock()
_stop = threading.Event()


# ============================================================ PROTOCOLE
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


def build_dats(total_len, bitmap_len):
    return enc_cmd("DATS", total_len.to_bytes(2, "big") + bitmap_len.to_bytes(2, "big") + b"\x00")


def chunk_packets(payload):
    packets, i, idx, maxd = [], 0, 0, MAX_PACKET - 2
    while i < len(payload):
        data = payload[i:i + maxd]
        packets.append(bytes([len(data) + 1, idx]) + data)
        i += len(data)
        idx += 1
    return packets


def bars_to_payload(levels, color):
    """levels: array 36 (0..1) -> payload (bitmap + couleurs par colonne)."""
    bpc = (OUT_H + 7) // 8
    bitmap = bytearray()
    colors = bytearray()
    r, g, b = color
    for x in range(OUT_W):
        h = int(round(levels[x] * OUT_H))
        h = max(0, min(OUT_H, h))
        # barre par le bas : allume les 'h' lignes du bas (y = OUT_H-h .. OUT_H-1)
        col_on = [1 if y >= OUT_H - h else 0 for y in range(OUT_H)]
        for gi in range(bpc):
            byte = 0
            for k in range(8):
                y = 8 * gi + k
                if y < OUT_H and col_on[y]:
                    byte |= 1 << (7 - k)
            bitmap.append(byte)
        colors += bytes([r, g, b]) if h else bytes([0, 0, 0])
    return bytes(bitmap) + bytes(colors), len(bitmap)


# ============================================================ DSP (bandes)
def make_band_edges():
    return np.logspace(np.log10(FMIN), np.log10(FMAX), OUT_W + 1)


def compute_bars(mono, edges, freqs, prev, gain, floor_db=-70.0):
    """mono -> 36 niveaux 0..1 avec lissage attaque/descente."""
    win = np.hanning(len(mono))
    mag = np.abs(np.fft.rfft(mono * win))
    levels = np.zeros(OUT_W)
    for i in range(OUT_W):
        sel = (freqs >= edges[i]) & (freqs < edges[i + 1])
        if np.any(sel):
            levels[i] = np.sqrt(np.mean(mag[sel] ** 2))
    # échelle log (dB) -> 0..1
    db = 20 * np.log10(levels + 1e-9)
    norm = np.clip((db - floor_db) / (-floor_db), 0, 1) * gain
    norm = np.clip(norm, 0, 1)
    # lissage : attaque rapide, descente lente
    out = np.where(norm > prev, norm, prev * 0.72 + norm * 0.28)
    return out


def audio_thread(gain):
    try:
        import soundcard as sc
    except ImportError:
        print("soundcard manquant : utilise 'uv run'", file=sys.stderr)
        _stop.set()
        return
    try:
        spk = sc.default_speaker()
        mic = sc.get_microphone(id=str(spk.name), include_loopback=True)
    except Exception as e:
        print(f"Impossible d'ouvrir le loopback audio : {e}", file=sys.stderr)
        _stop.set()
        return

    edges = make_band_edges()
    freqs = np.fft.rfftfreq(FFT_SIZE, 1 / SR)
    prev = np.zeros(OUT_W)
    print(f"🎧 Capture loopback : {spk.name}")
    with mic.recorder(samplerate=SR, channels=1, blocksize=FFT_SIZE // 2) as rec:
        while not _stop.is_set():
            data = rec.record(numframes=FFT_SIZE)
            mono = data[:, 0] if data.ndim > 1 else data
            prev = compute_bars(mono, edges, freqs, prev, gain)
            with _bars_lock:
                _bars[:] = prev


# ============================================================ BOUCLE BLE
async def run_ble(address, color):
    from bleak import BleakClient
    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def on_notify(_c, data):
        loop.call_soon_threadsafe(queue.put_nowait, dec_notify(data))

    async def wait_for(exp, timeout=4.0):
        while True:
            r = await asyncio.wait_for(queue.get(), timeout=timeout)
            if r.startswith(exp):
                return r

    async def send_frame(client, levels):
        payload, blen = bars_to_payload(levels, color)
        await client.write_gatt_char(CMD_CHAR, build_dats(len(payload), blen), response=True)
        await wait_for("DATSOK")
        for pkt in chunk_packets(payload):
            await client.write_gatt_char(DATA_CHAR, pkt, response=False)
            await wait_for("REOK")
        await client.write_gatt_char(CMD_CHAR, enc_cmd("DATCP"), response=True)
        await wait_for("DATCPOK")

    print(f"Connexion à {address}…")
    async with BleakClient(address) as client:
        await client.start_notify(NOTIFY_CHAR, on_notify)
        await client.write_gatt_char(CMD_CHAR, enc_cmd("MODE", b"\x01"), response=True)  # steady une fois
        print("Connecté. Lance ta musique. Ctrl-C pour arrêter.\n")

        frames, t0 = 0, time.time()
        while not _stop.is_set():
            with _bars_lock:
                levels = _bars.copy()
            try:
                await send_frame(client, levels)
            except asyncio.TimeoutError:
                print("\n⚠️ Lunettes qui ne répondent plus — on continue…")
                continue
            frames += 1
            now = time.time()
            if now - t0 >= 1.0:
                print(f"\r{frames / (now - t0):4.1f} fps", end="", flush=True)
                frames, t0 = 0, now
        try:
            await client.stop_notify(NOTIFY_CHAR)
        except Exception:
            pass


def list_outputs():
    try:
        import soundcard as sc
    except ImportError:
        sys.exit("soundcard manquant : utilise 'uv run'")
    print("Sortie par défaut :", sc.default_speaker().name)
    print("\nToutes les sorties :")
    for s in sc.all_speakers():
        print("  -", s.name)


def main():
    p = argparse.ArgumentParser(description="Visualiseur spectre audio pour lunettes LED Heaton")
    p.add_argument("address", nargs="?", help="adresse BLE, ex AA:BB:CC:DD:EE:FF")
    p.add_argument("--color", type=int, nargs=3, metavar=("R", "G", "B"),
                   default=[255, 255, 255], help="couleur des barres (défaut blanc)")
    p.add_argument("--gain", type=float, default=1.0, help="sensibilité (défaut 1.0)")
    p.add_argument("--list", action="store_true", help="lister les sorties audio")
    args = p.parse_args()

    if args.list:
        list_outputs()
        return
    if not args.address:
        sys.exit("Donne l'adresse BLE. Ex : uv run glasses_audio.py AA:BB:CC:DD:EE:FF")

    th = threading.Thread(target=audio_thread, args=(args.gain,), daemon=True)
    th.start()
    time.sleep(0.3)
    if _stop.is_set():
        sys.exit("Démarrage audio impossible.")

    try:
        asyncio.run(run_ble(args.address, tuple(args.color)))
    except KeyboardInterrupt:
        print("\nArrêt.")
    finally:
        _stop.set()


if __name__ == "__main__":
    main()
