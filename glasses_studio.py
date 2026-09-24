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
glasses_studio.py — Interface unifiée pour les lunettes LED Heaton (couleur PAR-PIXEL).

Un seul outil qui fait tout :
  • Scan BLE : trouve les lunettes automatiquement et les propose dans une liste (+ rafraîchir).
  • Charge une IMAGE ou un GIF animé (PNG/JPG/GIF/BMP/WebP).
  • Cadrage interactif verrouillé au ratio 3:1 (36x12), avec aperçu 36x12 en direct.
  • Envoi d'une image fixe en per-pixel (handshake DATS, type 0x01).
  • Lecture d'un GIF animé en temps réel : diff frame-à-frame streamé sur le canal …960b
    (mode DIY live), sans re-téléverser chaque image.

Formats de protocole (décodés depuis l'appli officielle com.icwork.shiningglass, validés hardware) :
  • image fixe : bitmap(72) + couleurs(432*3) ; DATS [total:u16be][bitmap:u16be][0x01] -> …9600 ;
                 trames [len+1][idx][data] -> …960a ; DATCP.  Pas de MODE ensuite.
  • temps réel : entrer DIY (SMVEW…01) ; trames brutes [len][R][G][B][col][row]… -> …960b ; sortir.

    uv run glasses_studio.py                      # ouvre l'interface
    uv run glasses_studio.py anim.gif             # préouvre un fichier

⚠️  Ferme l'appli mobile avant : une seule connexion BLE centrale à la fois.
⚠️  Si l'UI ne s'ouvre pas sous 'uv' (Tkinter absent), lance avec le Python système :
    pip install bleak pycryptodome pillow ; python glasses_studio.py
"""

import asyncio
import sys
import threading
from datetime import datetime

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:
    sys.exit("Tkinter absent : lance avec le Python système "
             "(pip install bleak pycryptodome pillow ; python glasses_studio.py)")
try:
    from PIL import Image, ImageSequence, ImageTk
except ImportError:
    sys.exit("pillow manquant : utilise 'uv run' (ou pip install pillow)")
try:
    from Crypto.Cipher import AES
except ImportError:
    sys.exit("pycryptodome manquant : utilise 'uv run'")

OUT_W, OUT_H = 36, 12
NPIX = OUT_W * OUT_H
ASPECT = OUT_W / OUT_H
BLACK = (0, 0, 0)
CANVAS_W, CANVAS_H = 820, 380
PREV_SCALE = 12

CMD_CHAR = "d44bc439-abfd-45a2-b575-925416129600"
NOTIFY_CHAR = "d44bc439-abfd-45a2-b575-925416129601"
DATA_CHAR = "d44bc439-abfd-45a2-b575-92541612960a"      # trames image (DATS)
RT_CHAR = "d44bc439-abfd-45a2-b575-92541612960b"        # canal temps réel (DIY live)
KEY = bytes.fromhex("32672f7974ad43451d9c6c894a0e8764")
MAX_PACKET, PAD, DIY_TYPE = 100, 16, 0x01
RT_PACE = 0.004          # cadence mini entre 2 trames 960b non accusées (anti-saturation firmware)


def ts():
    return datetime.now().strftime("%H:%M:%S")


# ---------------------------------------------------------------------------
# Protocole (primitives communes, décodées de l'appli)
# ---------------------------------------------------------------------------
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
    args = total_len.to_bytes(2, "big") + bitmap_len.to_bytes(2, "big") + bytes([DIY_TYPE])
    return enc_cmd("DATS", args)


def enter_diy():
    return enc_cmd("SMVEW", bytes([1]))


def exit_diy(save=False):
    return enc_cmd("SMVEW", bytes([2 if save else 0]))


def payload_from_pixels(pixels):
    """pixels : liste de NPIX (r,g,b) en ordre colonne-major (index = col*OUT_H + row).
    Renvoie (payload = bitmap+couleurs, bitmap_len) pour le handshake DATS."""
    bitmap = bytearray()
    for x in range(OUT_W):
        b0 = b1 = 0
        for y in range(OUT_H):
            if pixels[x * OUT_H + y] != BLACK:
                if y < 8:
                    b0 |= 1 << (7 - y)
                else:
                    b1 |= 1 << (7 - (y - 8))
        bitmap += bytes([b0, b1])
    colors = bytearray()
    for c in pixels:
        colors += bytes([c[0] & 0xFF, c[1] & 0xFF, c[2] & 0xFF])
    return bytes(bitmap) + bytes(colors), len(bitmap)


def chunk_packets(payload):
    maxd = MAX_PACKET - 2
    out, i, idx = [], 0, 0
    while i < len(payload):
        data = payload[i:i + maxd]
        out.append(bytes([len(data) + 1, idx]) + data)
        i += len(data)
        idx += 1
    return out


def rt_frame(points, rgb):
    r, g, b = rgb
    body = bytearray([3 + 2 * len(points), r & 0xFF, g & 0xFF, b & 0xFF])
    for (col, row) in points:
        body += bytes([col & 0xFF, row & 0xFF])
    return bytes(body)


def diff_groups(prev, cur):
    groups = {}
    for i in range(NPIX):
        if prev[i] != cur[i]:
            groups.setdefault(cur[i], []).append((i // OUT_H, i % OUT_H))
    return groups


def groups_to_frames(groups, max_points):
    out = []
    for rgb, pts in groups.items():
        for k in range(0, len(pts), max_points):
            out.append(rt_frame(pts[k:k + max_points], rgb))
    return out


# --- commandes animation stockée multi-images (chemin DATS, sûr et persistant) ---
MAX_ANIM_FRAMES = 20        # limite de l'appli (ImageSelectActivity : curIndex == 20)


def many_cmd(count):
    return enc_cmd("MANY", bytes([count & 0xFF, 0x01]))     # {6,'M','A','N','Y',count,1}


def mancpok_cmd():
    return enc_cmd("MANCPOK")                               # {7,'M','A','N','C','P','O','K'}


def play_cmd():
    return enc_cmd("PLAY")                                  # {4,'P','L','A','Y'}


def speed_cmd(v):
    return enc_cmd("SPEED", bytes([v & 0xFF]))              # {6,'S','P','E','E','D',v}


# ---------------------------------------------------------------------------
# Worker BLE : boucle asyncio dédiée dans un thread
# ---------------------------------------------------------------------------
class BleWorker:
    def __init__(self, status_cb):
        self.status = status_cb           # marshale déjà vers le thread UI
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self._run, daemon=True).start()
        self.client = None
        self.notify_q = None
        self.gif_task = None
        self.mtu = 23
        self.in_diy = False

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    @property
    def connected(self):
        return self.client is not None and self.client.is_connected

    async def scan(self):
        from bleak import BleakScanner
        found = await BleakScanner.discover(timeout=6.0)
        return [(d.name or "?", d.address) for d in found]

    async def connect(self, address):
        from bleak import BleakClient
        self.client = BleakClient(address)
        await self.client.connect()
        try:
            self.mtu = self.client.mtu_size or 23
        except Exception:
            self.mtu = 23
        self.notify_q = asyncio.Queue()

        def cb(_c, data):
            r = dec_notify(data)
            if r:
                self.loop.call_soon_threadsafe(self.notify_q.put_nowait, r)
        try:
            await self.client.start_notify(NOTIFY_CHAR, cb)
        except Exception:
            pass
        self.status(f"Connecté à {address} (MTU {self.mtu})")

    def max_points(self, cap=120):
        # trame 960b = 4 o d'en-tête + 2 par point ; l'ATT ampute 3 o du MTU négocié
        return min(cap, max(1, (self.mtu - 3 - 4) // 2))

    async def _enter_diy(self):
        """Entre (ou ré-affirme) le mode DIY. À rappeler avant chaque action au cas où le bouton
        physique aurait basculé les lunettes en animation d'usine."""
        await self.client.write_gatt_char(CMD_CHAR, enter_diy(), response=True)
        await asyncio.sleep(0.3)
        self.in_diy = True

    async def _paint_full(self, pixels):
        """Peint les 432 LED (diff contre [None] -> tout est peint), en écriture accusée."""
        mp = self.max_points()
        for pkt in groups_to_frames(diff_groups([None] * NPIX, pixels), mp):
            await self.client.write_gatt_char(RT_CHAR, pkt, response=True)

    async def disconnect(self):
        if self.gif_task:
            self.gif_task.cancel()
            self.gif_task = None
        if self.client:
            if self.in_diy:                            # ne sortir du DIY que si on y était (GIF)
                try:
                    await self.client.write_gatt_char(CMD_CHAR, exit_diy(False), response=True)
                except Exception:
                    pass
            try:
                await self.client.disconnect()
            except Exception:
                pass
        self.client = None
        self.in_diy = False
        self.status("Déconnecté")

    async def _wait(self, exp, timeout=6.0):
        while True:
            r = await asyncio.wait_for(self.notify_q.get(), timeout=timeout)
            if r.startswith(exp):
                return r

    async def send_image(self, pixels):
        # image fixe = upload DATS (type 0x01) -> affichage PERSISTANT, sans timeout ni mode DIY
        payload, blen = payload_from_pixels(pixels)
        packets = chunk_packets(payload)
        await self.client.write_gatt_char(CMD_CHAR, build_dats(len(payload), blen), response=True)
        await self._wait("DATSOK")
        self.status(f"DATSOK · envoi {len(packets)} trames…")
        for pkt in packets:
            await self.client.write_gatt_char(DATA_CHAR, pkt, response=False)
            await self._wait("REOK")
        await self.client.write_gatt_char(CMD_CHAR, enc_cmd("DATCP"), response=True)
        await self._wait("DATCPOK")
        self.status("✅ Image affichée (persistante).")

    async def upload_anim(self, frames, speed):
        """Upload multi-images (MANY -> N×DATS -> MANCPOK -> SPEED/PLAY) : animation STOCKÉE qui
        boucle en autonomie sur les lunettes. 100 % DATS, aucun 960b -> pas de crash, persistant."""
        n = len(frames)
        self.status(f"MANY {n} — annonce de la série…")
        await self.client.write_gatt_char(CMD_CHAR, many_cmd(n), response=True)
        await self._wait("MANYOK")
        for i, pixels in enumerate(frames):
            payload, blen = payload_from_pixels(pixels)
            packets = chunk_packets(payload)
            await self.client.write_gatt_char(CMD_CHAR, build_dats(len(payload), blen), response=True)
            await self._wait("DATSOK")
            for pkt in packets:
                await self.client.write_gatt_char(DATA_CHAR, pkt, response=False)
                await self._wait("REOK")
            await self.client.write_gatt_char(CMD_CHAR, enc_cmd("DATCP"), response=True)
            await self._wait("DATCPOK")
            self.status(f"Frame {i + 1}/{n} stockée…")
        await self.client.write_gatt_char(CMD_CHAR, mancpok_cmd(), response=True)
        try:
            await self._wait("MANCP", timeout=8.0)
        except asyncio.TimeoutError:
            pass
        if speed is not None:
            await self.client.write_gatt_char(CMD_CHAR, speed_cmd(speed), response=True)
        await self.client.write_gatt_char(CMD_CHAR, play_cmd(), response=True)
        self.status(f"✅ Animation stockée ({n} frames) — boucle autonome sur les lunettes.")

    async def play_gif(self, frames, durations, max_points, loops, on_stop,
                       keyframe=16, reliable=False):
        max_points = self.max_points(max_points)
        mode = "fiable" if reliable else f"rapide (keyframe /{keyframe})"
        self.status(f"MTU {self.mtu} → {max_points} pts/trame · mode {mode}")
        await self._enter_diy()                        # ré-affirme le mode DIY
        # La 1re frame (et chaque keyframe) peint les 432 LED : diff contre [None] ≠ toute couleur.
        # Les diffs intermédiaires partent en write-without-response (rapide) ; le keyframe accusé
        # répare toute goutte perdue -> état garanti correct périodiquement.
        displayed = [None] * NPIX
        try:
            n = fc = 0
            while loops == 0 or n < loops:
                for f, (cur, dur) in enumerate(zip(frames, durations)):
                    t0 = self.loop.time()
                    is_key = (fc % keyframe == 0)
                    if is_key and fc > 0:              # ré-affirme le DIY (récupère un appui bouton)
                        await self.client.write_gatt_char(CMD_CHAR, enter_diy(), response=True)
                    prev = [None] * NPIX if is_key else displayed
                    resp = True if (is_key or reliable) else False
                    for pkt in groups_to_frames(diff_groups(prev, cur), max_points):
                        await self.client.write_gatt_char(RT_CHAR, pkt, response=resp)
                        if not resp:                   # cadence les trames non accusées
                            await asyncio.sleep(RT_PACE)
                    displayed = cur
                    fc += 1
                    self.status(f"GIF · boucle {n+1} · frame {f+1}/{len(frames)}"
                                f"{' [KEY]' if is_key else ''}")
                    await asyncio.sleep(max(0, dur - (self.loop.time() - t0)))
                n += 1
        except asyncio.CancelledError:
            pass
        finally:
            # le canevas 960b a un TIMEOUT firmware (retour animation si plus de flux). Pour figer
            # la dernière image durablement, on la ré-envoie en DATS (affichage persistant).
            try:
                if displayed and isinstance(displayed[0], tuple):
                    await self.send_image(displayed)
                    self.status("GIF arrêté — dernière image figée (DATS, persistante).")
                else:
                    self.status("GIF arrêté.")
            except Exception:
                self.status("GIF arrêté.")
            on_stop()


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------
class Studio:
    HANDLE = 14

    def __init__(self, root, path=None):
        self.root = root
        root.title("glasses_studio — lunettes Heaton (per-pixel · image & GIF)")
        self.worker = BleWorker(lambda m: self.root.after(0, lambda: self.status.config(text=m)))

        self.src_frames = []     # frames PIL RGB pleine résolution
        self.durations = []
        self.scale = 1.0
        self.box = [0, 0, 0, 0]  # cadre en coords image : x, y, w, h (w = 3h)
        self.drag = None
        self.tkimg = self.tkprev = None
        self.dev_map = {}
        self.playing = False
        self.anim_busy = False   # upload d'animation stockée en cours
        self.palette = None      # palette PIL "P" des couleurs source (pour le snap)

        # --- barre fichier ---
        top = tk.Frame(root); top.pack(fill="x", padx=6, pady=4)
        tk.Button(top, text="📁  Choisir image / GIF…", command=self.open_dialog).pack(side="left")
        tk.Button(top, text="Enregistrer PNG 36×12…", command=self.save_png).pack(side="left", padx=4)
        tk.Label(top, text="  Seuil:").pack(side="left")
        self.threshold = tk.IntVar(value=0)
        th = tk.Spinbox(top, from_=0, to=765, increment=15, width=5,
                        textvariable=self.threshold, command=self.refresh)
        th.pack(side="left")
        th.bind("<KeyRelease>", lambda e: self.safe_refresh())
        th.bind("<FocusOut>", lambda e: self.safe_refresh())
        tk.Label(top, text="  Rééch.:").pack(side="left")
        self.resample = tk.StringVar(value="lanczos")
        rs = ttk.Combobox(top, textvariable=self.resample, values=["lanczos", "nearest"],
                          width=8, state="readonly")
        rs.pack(side="left")
        rs.bind("<<ComboboxSelected>>", lambda e: self.refresh())
        tk.Label(top, text="  Postér. (bits, 0=off):").pack(side="left")
        self.posterize = tk.IntVar(value=0)
        po = tk.Spinbox(top, from_=0, to=7, width=4, textvariable=self.posterize,
                        command=self.refresh)
        po.pack(side="left")
        po.bind("<KeyRelease>", lambda e: self.safe_refresh())
        po.bind("<FocusOut>", lambda e: self.safe_refresh())
        self.snap = tk.BooleanVar(value=True)
        tk.Checkbutton(top, text="Snap couleurs source", variable=self.snap,
                       command=self.refresh).pack(side="left", padx=(8, 0))
        tk.Label(top, text="Couleurs:").pack(side="left")
        self.snap_colors = tk.IntVar(value=48)
        sc = tk.Spinbox(top, from_=2, to=256, increment=8, width=5,
                        textvariable=self.snap_colors, command=self.rebuild_palette)
        sc.pack(side="left")
        sc.bind("<KeyRelease>", lambda e: self.rebuild_palette())
        sc.bind("<FocusOut>", lambda e: self.rebuild_palette())

        # --- barre connexion ---
        conn = tk.Frame(root); conn.pack(fill="x", padx=6, pady=(0, 4))
        tk.Label(conn, text="Lunettes :").pack(side="left")
        self.dev_var = tk.StringVar()
        self.dev_combo = ttk.Combobox(conn, textvariable=self.dev_var, width=34)
        self.dev_combo.pack(side="left", padx=4)
        self.scan_btn = tk.Button(conn, text="🔍 Rechercher / Rafraîchir", command=self.scan)
        self.scan_btn.pack(side="left", padx=4)
        self.conn_btn = tk.Button(conn, text="Connecter", command=self.toggle_conn)
        self.conn_btn.pack(side="left", padx=4)
        self.diy_btn = tk.Button(conn, text="↻ Réafficher", command=self.reactivate_diy)
        self.diy_btn.pack(side="left", padx=4)

        # --- zone centrale ---
        mid = tk.Frame(root); mid.pack(padx=6, pady=4)
        self.canvas = tk.Canvas(mid, width=CANVAS_W, height=CANVAS_H, bg="#222",
                                highlightthickness=0)
        self.canvas.pack(side="left")
        right = tk.Frame(mid); right.pack(side="left", padx=8, anchor="n")
        tk.Label(right, text="Aperçu 36×12").pack()
        self.preview = tk.Canvas(right, width=OUT_W * PREV_SCALE, height=OUT_H * PREV_SCALE,
                                 bg="black", highlightthickness=1, highlightbackground="#555")
        self.preview.pack(pady=4)
        self.info = tk.Label(right, text="", justify="left", fg="#555")
        self.info.pack(anchor="w")

        # --- barre actions ---
        act = tk.Frame(right); act.pack(anchor="w", pady=6)
        self.send_btn = tk.Button(act, text="Envoyer image", command=self.send_image)
        self.send_btn.grid(row=0, column=0, columnspan=2, sticky="we", pady=2)
        # --- Animation STOCKÉE (chemin sûr, recommandé pour les GIF) ---
        tk.Label(act, text="— Animation stockée (recommandé) —", fg="#2a7").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        tk.Label(act, text="1 frame /X :").grid(row=2, column=0, sticky="w")
        self.every = tk.IntVar(value=1)
        tk.Spinbox(act, from_=1, to=99, width=5, textvariable=self.every,
                   command=self.update_anim_info).grid(row=2, column=1, sticky="w")
        tk.Label(act, text=f"Max (≤{MAX_ANIM_FRAMES}) :").grid(row=3, column=0, sticky="w")
        self.anim_max = tk.IntVar(value=MAX_ANIM_FRAMES)
        tk.Spinbox(act, from_=1, to=MAX_ANIM_FRAMES, width=5, textvariable=self.anim_max,
                   command=self.update_anim_info).grid(row=3, column=1, sticky="w")
        tk.Label(act, text="Vitesse (0=déf) :").grid(row=4, column=0, sticky="w")
        self.anim_speed = tk.IntVar(value=0)
        tk.Spinbox(act, from_=0, to=255, width=5, textvariable=self.anim_speed).grid(row=4, column=1, sticky="w")
        self.anim_btn = tk.Button(act, text="⬆ Envoyer animation (boucle stockée)",
                                  command=self.upload_anim)
        self.anim_btn.grid(row=5, column=0, columnspan=2, sticky="we", pady=2)
        self.anim_info = tk.Label(act, text="", fg="#888")
        self.anim_info.grid(row=6, column=0, columnspan=2, sticky="w")

        # --- Lecture live 960b (rapide mais RISQUÉE : peut figer le firmware) ---
        tk.Label(act, text="— Live 960b (risqué) —", fg="#a63").grid(
            row=7, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.play_btn = tk.Button(act, text="▶ Live GIF", command=self.play_gif)
        self.play_btn.grid(row=8, column=0, sticky="we", pady=2)
        self.stop_btn = tk.Button(act, text="⏹ Stop", command=self.stop_gif, state="disabled")
        self.stop_btn.grid(row=8, column=1, sticky="we", pady=2)
        tk.Label(act, text="Boucles (0=∞):").grid(row=9, column=0, sticky="w")
        self.loops = tk.IntVar(value=0)
        tk.Spinbox(act, from_=0, to=999, width=5, textvariable=self.loops).grid(row=9, column=1, sticky="w")
        tk.Label(act, text="FPS (0=GIF):").grid(row=10, column=0, sticky="w")
        self.fps = tk.DoubleVar(value=0)
        tk.Spinbox(act, from_=0, to=60, increment=1, width=5, textvariable=self.fps).grid(row=10, column=1, sticky="w")
        tk.Label(act, text="Keyframe /N:").grid(row=11, column=0, sticky="w")
        self.keyframe = tk.IntVar(value=16)
        tk.Spinbox(act, from_=1, to=999, width=5, textvariable=self.keyframe).grid(row=11, column=1, sticky="w")
        self.reliable = tk.BooleanVar(value=False)
        tk.Checkbutton(act, text="Mode fiable (plus lent)", variable=self.reliable).grid(
            row=12, column=0, columnspan=2, sticky="w")

        self.status = tk.Label(root, text="Choisis une image ou un GIF pour commencer.", anchor="w")
        self.status.pack(fill="x", padx=6, pady=(0, 6))

        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_move)
        self.canvas.bind("<ButtonRelease-1>", lambda e: setattr(self, "drag", None))
        self.canvas.bind("<MouseWheel>", lambda e: self.zoom(1.1 if e.delta > 0 else 1 / 1.1))
        self.canvas.bind("<Button-4>", lambda e: self.zoom(1.1))
        self.canvas.bind("<Button-5>", lambda e: self.zoom(1 / 1.1))
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.update_buttons()
        if path:
            self.load(path)
        else:
            self.show_placeholder()

    # --- placeholder / chargement ---
    def show_placeholder(self):
        self.canvas.delete("all")
        self.canvas.create_text(CANVAS_W // 2, CANVAS_H // 2,
                                text="📁  Choisis une image ou un GIF",
                                fill="#888", font=("Segoe UI", 15))

    def open_dialog(self):
        path = filedialog.askopenfilename(
            filetypes=[("Images / GIF", "*.png *.jpg *.jpeg *.gif *.bmp *.webp"), ("Tous", "*.*")])
        if path:
            self.load(path)

    def load(self, path):
        if self.playing:                # stoppe une lecture en cours avant de charger un autre fichier
            self.stop_gif()
        try:
            im = Image.open(path)
            frames, durs = [], []
            base = Image.new("RGBA", im.size, (0, 0, 0, 0))
            for fr in ImageSequence.Iterator(im):
                base = base.copy()
                base.alpha_composite(fr.convert("RGBA"))
                frames.append(base.convert("RGB"))
                durs.append(max(0.02, fr.info.get("duration", 100) / 1000.0))
        except Exception as e:
            messagebox.showerror("Erreur", f"Impossible d'ouvrir :\n{e}")
            return
        self.src_frames, self.durations = frames, durs
        w, h = frames[0].size
        self.scale = min(CANVAS_W / w, CANVAS_H / h)
        if w / h > ASPECT:
            bh, bw = h, int(h * ASPECT)
        else:
            bw, bh = w, int(w / ASPECT)
        self.box = [(w - bw) // 2, (h - bh) // 2, bw, bh]
        self.build_palette()
        kind = "GIF animé" if len(frames) > 1 else "image"
        self.status.config(text=f"{path}  —  {w}×{h}  ({kind}, {len(frames)} frame(s))")
        self.update_buttons()
        self.update_anim_info()
        self.refresh()

    def build_palette(self):
        """Construit la palette des vraies couleurs de la source (médian-cut sur toutes les
        frames échantillonnées). Sert au 'snap' : chaque pixel rendu est forcé vers la couleur
        source la plus proche -> pas de teinte inventée + moins de couleurs distinctes."""
        self.palette = None
        if not self.src_frames:
            return
        k = max(2, min(256, self.snap_colors.get()))
        w, h = self.src_frames[0].size
        step = max(1, len(self.src_frames) // 24)       # jusqu'à ~24 frames échantillonnées
        sample = self.src_frames[::step]
        montage = Image.new("RGB", (w, h * len(sample)))
        for i, f in enumerate(sample):
            montage.paste(f, (0, i * h))
        self.palette = montage.convert("P", palette=Image.ADAPTIVE, colors=k)

    def rebuild_palette(self):
        try:
            self.build_palette()
            self.safe_refresh()
        except (tk.TclError, ValueError):
            pass

    # --- géométrie du cadre ---
    def i2c(self, x, y):
        return x * self.scale, y * self.scale

    def c2i(self, cx, cy):
        return cx / self.scale, cy / self.scale

    def clamp(self):
        w, h = self.src_frames[0].size
        bx, by, bw, bh = self.box
        bw = max(6, min(bw, w))
        bh = max(2, min(int(round(bw / ASPECT)), h))
        bw = int(round(bh * ASPECT))
        bx = max(0, min(bx, w - bw)); by = max(0, min(by, h - bh))
        self.box = [int(bx), int(by), int(bw), int(bh)]

    def corners(self):
        bx, by, bw, bh = self.box
        return {"nw": self.i2c(bx, by), "ne": self.i2c(bx + bw, by),
                "sw": self.i2c(bx, by + bh), "se": self.i2c(bx + bw, by + bh)}

    def on_press(self, e):
        if not self.src_frames:
            return
        for name, (cx, cy) in self.corners().items():
            if abs(e.x - cx) <= self.HANDLE and abs(e.y - cy) <= self.HANDLE:
                opp = {"nw": "se", "ne": "sw", "sw": "ne", "se": "nw"}[name]
                self.drag = ("resize", *self.corners()[opp]); return
        bx, by, bw, bh = self.box
        ix, iy = self.c2i(e.x, e.y)
        if bx <= ix <= bx + bw and by <= iy <= by + bh:
            self.drag = ("move", ix - bx, iy - by)

    def on_move(self, e):
        if not self.drag or not self.src_frames:
            return
        if self.drag[0] == "move":
            _, ox, oy = self.drag
            ix, iy = self.c2i(e.x, e.y)
            self.box[0], self.box[1] = int(ix - ox), int(iy - oy)
        else:
            _, ax, ay = self.drag
            aix, aiy = self.c2i(ax, ay); mix, miy = self.c2i(e.x, e.y)
            bw = abs(mix - aix); bh = bw / ASPECT
            bx = min(aix, mix); by = aiy - bh if miy < aiy else aiy
            self.box = [bx, by, bw, bh]
        self.clamp(); self.refresh()

    def zoom(self, f):
        if not self.src_frames:
            return
        bx, by, bw, bh = self.box
        cx, cy = bx + bw / 2, by + bh / 2
        bw *= f; bh = bw / ASPECT
        self.box = [cx - bw / 2, cy - bh / 2, bw, bh]
        self.clamp(); self.refresh()

    # --- rendu ---
    def _resample(self):
        return Image.NEAREST if self.resample.get() == "nearest" else Image.LANCZOS

    def render_frame(self, idx):
        """Recadre la frame idx selon le cadre et renvoie la liste de NPIX couleurs (col-major)."""
        bx, by, bw, bh = self.box
        crop = self.src_frames[idx].crop((bx, by, bx + bw, by + bh)).resize(
            (OUT_W, OUT_H), self._resample())
        if self.snap.get() and self.palette is not None:
            # force chaque pixel vers la couleur source la plus proche (sans tramage)
            crop = crop.quantize(palette=self.palette, dither=Image.Dither.NONE).convert("RGB")
        px = crop.load()
        thr = self.threshold.get()
        p = self.posterize.get()
        q = (8 - p) if 0 < p < 8 else 0
        out = []
        for x in range(OUT_W):
            for y in range(OUT_H):
                r, g, b = px[x, y]
                if (r + g + b) > thr:
                    out.append(((r >> q) << q, (g >> q) << q, (b >> q) << q) if q else (r, g, b))
                else:
                    out.append(BLACK)
        return out

    def safe_refresh(self):
        """Rafraîchit l'aperçu en tolérant un champ en cours de saisie (vide/incomplet)."""
        try:
            self.refresh()
        except (tk.TclError, ValueError):
            pass

    def refresh(self):
        if not self.src_frames:
            return
        w, h = self.src_frames[0].size
        disp = self.src_frames[0].resize((max(1, int(w * self.scale)), max(1, int(h * self.scale))),
                                         Image.LANCZOS)
        self.tkimg = ImageTk.PhotoImage(disp)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self.tkimg)
        bx, by = self.i2c(self.box[0], self.box[1])
        ex, ey = self.i2c(self.box[0] + self.box[2], self.box[1] + self.box[3])
        for x0, y0, x1, y1 in [(0, 0, CANVAS_W, by), (0, ey, CANVAS_W, CANVAS_H),
                               (0, by, bx, ey), (ex, by, CANVAS_W, ey)]:
            self.canvas.create_rectangle(x0, y0, x1, y1, fill="#000", stipple="gray50", width=0)
        self.canvas.create_rectangle(bx, by, ex, ey, outline="#4af", width=2)
        for cx, cy in self.corners().values():
            self.canvas.create_rectangle(cx - 4, cy - 4, cx + 4, cy + 4, fill="#4af", outline="")

        pixels = self.render_frame(0)
        big = Image.new("RGB", (OUT_W, OUT_H))
        big.putdata([pixels[x * OUT_H + y] for y in range(OUT_H) for x in range(OUT_W)])
        big = big.resize((OUT_W * PREV_SCALE, OUT_H * PREV_SCALE), Image.NEAREST)
        self.tkprev = ImageTk.PhotoImage(big)
        self.preview.create_image(0, 0, anchor="nw", image=self.tkprev)
        lit = sum(1 for c in pixels if c != BLACK)
        self.info.config(text=f"{lit}/{NPIX} pixels allumés")

    # --- scan / connexion ---
    def scan(self):
        self.scan_btn.config(state="disabled", text="Recherche…")
        self.status.config(text="Scan BLE (~6 s)…")
        fut = self.worker.submit(self.worker.scan())
        fut.add_done_callback(lambda f: self.root.after(0, lambda: self._scan_done(f)))

    def _scan_done(self, fut):
        self.scan_btn.config(state="normal", text="🔍 Rechercher / Rafraîchir")
        try:
            devices = fut.result()
        except Exception as e:
            self.status.config(text=f"⚠️ Scan échoué : {e}"); return
        is_g = lambda nm: "glass" in (nm or "").lower()
        devices.sort(key=lambda nd: (not is_g(nd[0]), nd[0]))
        self.dev_map = {}; values = []
        for name, addr in devices:
            disp = f"{name}  ({addr})"; self.dev_map[disp] = addr; values.append(disp)
        self.dev_combo["values"] = values
        glasses = [v for v in values if is_g(v)]
        if glasses:
            self.dev_var.set(glasses[0])
            self.status.config(text=f"{len(devices)} appareils · lunettes : {glasses[0]}")
        elif values:
            self.status.config(text=f"{len(devices)} appareils — aucune « GLASSES », choisis dans la liste.")
        else:
            self.status.config(text="Aucun appareil BLE. Lunettes allumées ? Réessaie.")

    def resolve_address(self):
        disp = self.dev_var.get().strip()
        return self.dev_map.get(disp, disp).strip()

    def toggle_conn(self):
        if self.worker.connected:
            self.worker.submit(self.worker.disconnect())
            self.root.after(300, self.update_buttons)
        else:
            addr = self.resolve_address()
            if not addr:
                messagebox.showwarning("Adresse manquante", "Choisis les lunettes (Rechercher) ou saisis l'adresse.")
                return
            self.conn_btn.config(state="disabled", text="Connexion…")
            fut = self.worker.submit(self.worker.connect(addr))
            fut.add_done_callback(lambda f: self.root.after(0, lambda: self._conn_done(f)))

    def _conn_done(self, fut):
        try:
            fut.result()
        except Exception as e:
            self.status.config(text=f"⚠️ Connexion échouée : {e}")
        self.update_buttons()

    def reactivate_diy(self):
        """Ré-affiche l'image courante en DATS (persistant) — pour récupérer l'affichage après un
        appui sur le bouton physique (qui repasse les lunettes en animation d'usine)."""
        if not self._ensure_connected() or self.playing:
            return
        pixels = self.render_frame(0) if self.src_frames else [BLACK] * NPIX
        self.diy_btn.config(state="disabled")
        fut = self.worker.submit(self.worker.send_image(pixels))
        fut.add_done_callback(lambda f: self.root.after(0, lambda: self._simple_done(f, self.diy_btn)))

    def _ensure_connected(self):
        if self.worker.connected:
            return True
        messagebox.showinfo("Non connecté", "Connecte-toi d'abord aux lunettes (bouton Connecter).")
        return False

    # --- actions ---
    def send_image(self):
        if not self.src_frames or not self._ensure_connected():
            return
        pixels = self.render_frame(0)
        self.send_btn.config(state="disabled")
        fut = self.worker.submit(self.worker.send_image(pixels))
        fut.add_done_callback(lambda f: self.root.after(0, lambda: self._simple_done(f, self.send_btn)))

    def _simple_done(self, fut, btn):
        try:
            fut.result()
        except Exception as e:
            self.status.config(text=f"⚠️ Échec : {e}")
        self.update_buttons()

    def play_gif(self):
        if not self.src_frames or not self._ensure_connected():
            return
        frames = [self.render_frame(i) for i in range(len(self.src_frames))]
        durs = self.durations
        if self.fps.get() > 0:
            durs = [1.0 / self.fps.get()] * len(frames)
        self.playing = True
        self.update_buttons()
        self.worker.gif_task = self.worker.submit(
            self.worker.play_gif(frames, durs, 120, self.loops.get(),
                                 lambda: self.root.after(0, self._gif_stopped),
                                 keyframe=max(1, self.keyframe.get()), reliable=self.reliable.get()))

    def stop_gif(self):
        if self.worker.gif_task:
            self.worker.loop.call_soon_threadsafe(self.worker.gif_task.cancel)

    def _gif_stopped(self):
        self.playing = False
        self.worker.gif_task = None
        self.update_buttons()

    # --- animation stockée (multi-images DATS) ---
    def anim_indices(self):
        """Indices des frames retenues : 1 sur X, plafonné au max (<=20)."""
        if not self.src_frames:
            return []
        try:
            every = max(1, self.every.get())
            cap = max(1, min(MAX_ANIM_FRAMES, self.anim_max.get()))
        except (tk.TclError, ValueError):
            every, cap = 1, MAX_ANIM_FRAMES
        return list(range(0, len(self.src_frames), every))[:cap]

    def update_anim_info(self):
        if not self.src_frames:
            self.anim_info.config(text="")
            return
        n = len(self.anim_indices())
        self.anim_info.config(text=f"→ {n} frame(s) envoyée(s) sur {len(self.src_frames)}")

    def upload_anim(self):
        if not self.src_frames or not self._ensure_connected() or self.playing or self.anim_busy:
            return
        idxs = self.anim_indices()
        if not idxs:
            return
        frames = [self.render_frame(i) for i in idxs]
        speed = self.anim_speed.get() if self.anim_speed.get() > 0 else None
        self.anim_busy = True
        self.anim_btn.config(text="⬆ Upload en cours…")
        self.update_buttons()
        fut = self.worker.submit(self.worker.upload_anim(frames, speed))
        fut.add_done_callback(lambda f: self.root.after(0, lambda: self._anim_done(f)))

    def _anim_done(self, fut):
        self.anim_busy = False
        self.anim_btn.config(text="⬆ Envoyer animation (boucle stockée)")
        try:
            fut.result()
        except Exception as e:
            self.status.config(text=f"⚠️ Échec animation : {e}")
        self.update_buttons()

    def update_buttons(self):
        connected = self.worker.connected
        has = bool(self.src_frames)
        is_gif = has and len(self.src_frames) > 1
        busy = self.playing or self.anim_busy
        self.conn_btn.config(text="Déconnecter" if connected else "Connecter", state="normal")
        self.diy_btn.config(state="normal" if (connected and not busy) else "disabled")
        self.send_btn.config(state="normal" if (has and connected and not busy) else "disabled")
        self.anim_btn.config(state="normal" if (is_gif and connected and not busy) else "disabled")
        self.play_btn.config(state="normal" if (is_gif and connected and not busy) else "disabled")
        self.stop_btn.config(state="normal" if self.playing else "disabled")

    def save_png(self):
        if not self.src_frames:
            return
        path = filedialog.asksaveasfilename(defaultextension=".png", filetypes=[("PNG", "*.png")])
        if path:
            bx, by, bw, bh = self.box
            self.src_frames[0].crop((bx, by, bx + bw, by + bh)).resize(
                (OUT_W, OUT_H), self._resample()).save(path)
            self.status.config(text=f"Enregistré : {path}")

    def on_close(self):
        """Fermeture propre : arrêt du GIF (sortie DIY), puis déconnexion, puis destruction."""
        self.root.protocol("WM_DELETE_WINDOW", lambda: None)   # ignore un 2e clic sur la croix
        self.status.config(text="Fermeture… déconnexion propre en cours")

        if not self.worker.connected and not self.worker.gif_task:
            self.root.destroy()
            return

        # 1) stopper le GIF s'il tourne -> son bloc finally sort du mode DIY
        if self.worker.gif_task and not self.worker.gif_task.done():
            self.worker.loop.call_soon_threadsafe(self.worker.gif_task.cancel)

        # 2) après un court délai (le temps que la sortie DIY parte), déconnecter puis fermer
        def do_disconnect():
            fut = self.worker.submit(self.worker.disconnect())

            def check():
                if fut.done():
                    self.root.destroy()
                else:
                    self.root.after(80, check)
            self.root.after(80, check)

        self.root.after(350, do_disconnect)


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else None
    root = tk.Tk()
    Studio(root, path=path)
    root.mainloop()


if __name__ == "__main__":
    main()
