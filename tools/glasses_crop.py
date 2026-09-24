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
glasses_crop.py — Cadre/redimensionne une image en 36x12 pour les lunettes Heaton, avec aperçu
en direct et envoi couleur PAR-PIXEL intégré.

L'afficheur fait 36x12 (ratio 3:1). Une image quelconque, envoyée telle quelle, est écrasée. Cet
outil te laisse choisir visuellement la zone à garder (cadre verrouillé au ratio 3:1), voir le
rendu 36x12 en direct, puis l'enregistrer en PNG et/ou l'envoyer directement aux lunettes.

    uv run tools/glasses_crop.py                       # ouvre l'interface
    uv run tools/glasses_crop.py mon_image.png   # ouvre en préchargeant l'image
    uv run tools/glasses_crop.py img.png AA:BB:CC:DD:EE:FF  # + préremplit l'adresse BLE

Souris :  glisser dans le cadre = déplacer · glisser un coin = redimensionner (ratio gardé) ·
          molette = agrandir/réduire le cadre.

⚠️  Si l'interface ne s'ouvre pas sous 'uv' (Tkinter absent), lance avec le Python système :
    pip install bleak pycryptodome pillow   puis   python tools/glasses_crop.py
⚠️  Ferme l'appli mobile avant d'envoyer : une seule connexion BLE à la fois.
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
             "(pip install bleak pycryptodome pillow ; python tools/glasses_crop.py)")
try:
    from PIL import Image, ImageTk
except ImportError:
    sys.exit("pillow manquant : utilise 'uv run' (ou pip install pillow)")
try:
    from Crypto.Cipher import AES
except ImportError:
    sys.exit("pycryptodome manquant : utilise 'uv run'")

OUT_W, OUT_H = 36, 12          # afficheur des lunettes
ASPECT = OUT_W / OUT_H         # 3.0
CANVAS_W, CANVAS_H = 840, 480  # zone d'édition max
PREV_SCALE = 12                # agrandissement de l'aperçu 36x12

# --- protocole (copié de glasses_pixel.py, primitives identiques) ---
CMD_CHAR = "d44bc439-abfd-45a2-b575-925416129600"
NOTIFY_CHAR = "d44bc439-abfd-45a2-b575-925416129601"
DATA_CHAR = "d44bc439-abfd-45a2-b575-92541612960a"
KEY = bytes.fromhex("32672f7974ad43451d9c6c894a0e8764")
MAX_PACKET, PAD, DIY_TYPE = 100, 16, 0x01


def ts():
    return datetime.now().strftime("%H:%M:%S")


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


def build_payload(img36, threshold):
    """img36 : PIL Image 36x12 RGB. Renvoie (payload, bitmap_len)."""
    px = img36.load()
    bitmap = bytearray()
    for x in range(OUT_W):
        b0 = b1 = 0
        for y in range(OUT_H):
            r, g, b = px[x, y]
            if (r + g + b) > threshold:
                if y < 8:
                    b0 |= 1 << (7 - y)
                else:
                    b1 |= 1 << (7 - (y - 8))
        bitmap += bytes([b0, b1])
    colors = bytearray()
    for x in range(OUT_W):
        for y in range(OUT_H):
            r, g, b = px[x, y]
            colors += bytes([r & 0xFF, g & 0xFF, b & 0xFF])
    return bytes(bitmap) + bytes(colors), len(bitmap)


def chunk_packets(payload):
    maxd = MAX_PACKET - 2
    packets, i, idx = [], 0, 0
    while i < len(payload):
        data = payload[i:i + maxd]
        packets.append(bytes([len(data) + 1, idx]) + data)
        i += len(data)
        idx += 1
    return packets


async def send_payload(address, payload, bitmap_len, log):
    packets = chunk_packets(payload)
    from bleak import BleakClient
    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def on_notify(_c, data):
        loop.call_soon_threadsafe(queue.put_nowait, dec_notify(data))

    async def wait_for(exp, timeout=6.0):
        while True:
            r = await asyncio.wait_for(queue.get(), timeout=timeout)
            if r.startswith(exp):
                return r

    log(f"Connexion à {address}…")
    async with BleakClient(address) as client:
        await client.start_notify(NOTIFY_CHAR, on_notify)
        await client.write_gatt_char(CMD_CHAR, build_dats(len(payload), bitmap_len), response=True)
        await wait_for("DATSOK")
        log(f"DATSOK · envoi {len(packets)} trames…")
        for n, pkt in enumerate(packets):
            await client.write_gatt_char(DATA_CHAR, pkt, response=False)
            await wait_for("REOK")
        await client.write_gatt_char(CMD_CHAR, enc_cmd("DATCP"), response=True)
        await wait_for("DATCPOK")
        log("✅ DATCPOK — image affichée.")
        await asyncio.sleep(0.2)
        try:
            await client.stop_notify(NOTIFY_CHAR)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------
class CropTool:
    HANDLE = 14  # rayon de détection d'un coin (px canvas)

    def __init__(self, root, image_path=None, address=""):
        self.root = root
        root.title("glasses_crop — cadrage 36x12 pour lunettes Heaton")
        self.src = None          # PIL Image originale (RGB)
        self.scale = 1.0         # facteur image -> canvas
        self.box = [0, 0, 0, 0]  # cadre en coords IMAGE : x, y, w, h  (w = 3h)
        self.tkimg = None
        self.tkprev = None
        self.drag = None         # ('move', dx, dy) ou ('resize', anchor_x, anchor_y)

        top = tk.Frame(root)
        top.pack(fill="x", padx=6, pady=4)
        tk.Button(top, text="📁  Choisir une image…", command=self.open_dialog).pack(side="left")
        tk.Button(top, text="Enregistrer PNG 36×12…", command=self.save_png).pack(side="left", padx=4)
        tk.Label(top, text="  Seuil allumage:").pack(side="left")
        self.threshold = tk.IntVar(value=0)
        tk.Spinbox(top, from_=0, to=765, increment=15, width=5,
                   textvariable=self.threshold, command=self.refresh).pack(side="left")
        conn = tk.Frame(root)
        conn.pack(fill="x", padx=6, pady=(0, 4))
        tk.Label(conn, text="Lunettes :").pack(side="left")
        self.dev_var = tk.StringVar(value=address)
        self.dev_map = {}   # "nom (adresse)" -> adresse
        self.dev_combo = ttk.Combobox(conn, textvariable=self.dev_var, width=36)
        self.dev_combo.pack(side="left", padx=4)
        self.dev_combo.bind("<<ComboboxSelected>>", self.on_dev_select)
        self.scan_btn = tk.Button(conn, text="🔍 Rechercher / Rafraîchir", command=self.scan)
        self.scan_btn.pack(side="left", padx=4)
        self.send_btn = tk.Button(conn, text="Envoyer aux lunettes", command=self.send)
        self.send_btn.pack(side="left", padx=4)

        mid = tk.Frame(root)
        mid.pack(padx=6, pady=4)
        self.canvas = tk.Canvas(mid, width=CANVAS_W, height=CANVAS_H, bg="#222",
                                highlightthickness=0)
        self.canvas.pack(side="left")
        right = tk.Frame(mid)
        right.pack(side="left", padx=8, anchor="n")
        tk.Label(right, text="Aperçu 36×12").pack()
        self.preview = tk.Canvas(right, width=OUT_W * PREV_SCALE, height=OUT_H * PREV_SCALE,
                                 bg="black", highlightthickness=1, highlightbackground="#555")
        self.preview.pack(pady=4)

        self.status = tk.Label(root, text="Ouvre une image pour commencer.", anchor="w")
        self.status.pack(fill="x", padx=6, pady=(0, 6))

        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_move)
        self.canvas.bind("<ButtonRelease-1>", lambda e: setattr(self, "drag", None))
        self.canvas.bind("<MouseWheel>", self.on_wheel)      # Windows/Mac
        self.canvas.bind("<Button-4>", lambda e: self.zoom(1.1))  # Linux
        self.canvas.bind("<Button-5>", lambda e: self.zoom(1 / 1.1))

        if image_path:
            self.load(image_path)
        else:
            self.show_placeholder()

    def show_placeholder(self):
        self.canvas.delete("all")
        self.canvas.create_text(
            CANVAS_W // 2, CANVAS_H // 2,
            text="📁  Clique sur « Choisir une image… »\npour charger n'importe quelle image",
            fill="#888", font=("Segoe UI", 15), justify="center")

    # --- chargement / cadre par défaut ---
    def open_dialog(self):
        path = filedialog.askopenfilename(
            filetypes=[("Images", "*.png *.jpg *.jpeg *.gif *.bmp *.webp"), ("Tous", "*.*")])
        if path:
            self.load(path)

    def load(self, path):
        try:
            self.src = Image.open(path).convert("RGB")
        except Exception as e:
            messagebox.showerror("Erreur", f"Impossible d'ouvrir l'image :\n{e}")
            return
        w, h = self.src.size
        self.scale = min(CANVAS_W / w, CANVAS_H / h)
        # cadre 3:1 le plus grand possible, centré
        if w / h > ASPECT:
            bh = h
            bw = int(h * ASPECT)
        else:
            bw = w
            bh = int(w / ASPECT)
        self.box = [(w - bw) // 2, (h - bh) // 2, bw, bh]
        self.status.config(text=f"{path}  —  {w}×{h} px")
        self.refresh()

    # --- géométrie ---
    def img_to_canvas(self, x, y):
        return x * self.scale, y * self.scale

    def canvas_to_img(self, cx, cy):
        return cx / self.scale, cy / self.scale

    def clamp_box(self):
        w, h = self.src.size
        bx, by, bw, bh = self.box
        bw = max(6, min(bw, w))
        bh = max(2, min(int(round(bw / ASPECT)), h))
        bw = int(round(bh * ASPECT))
        bx = max(0, min(bx, w - bw))
        by = max(0, min(by, h - bh))
        self.box = [int(bx), int(by), int(bw), int(bh)]

    # --- interactions ---
    def corners_canvas(self):
        bx, by, bw, bh = self.box
        pts = {"nw": (bx, by), "ne": (bx + bw, by),
               "sw": (bx, by + bh), "se": (bx + bw, by + bh)}
        return {k: self.img_to_canvas(*v) for k, v in pts.items()}

    def on_press(self, e):
        if not self.src:
            return
        for name, (cx, cy) in self.corners_canvas().items():
            if abs(e.x - cx) <= self.HANDLE and abs(e.y - cy) <= self.HANDLE:
                opp = {"nw": "se", "ne": "sw", "sw": "ne", "se": "nw"}[name]
                ax, ay = self.corners_canvas()[opp]
                self.drag = ("resize", ax, ay)
                return
        # sinon : déplacement si on est dans le cadre
        bx, by, bw, bh = self.box
        ix, iy = self.canvas_to_img(e.x, e.y)
        if bx <= ix <= bx + bw and by <= iy <= by + bh:
            self.drag = ("move", ix - bx, iy - by)

    def on_move(self, e):
        if not self.drag or not self.src:
            return
        kind = self.drag[0]
        if kind == "move":
            _, offx, offy = self.drag
            ix, iy = self.canvas_to_img(e.x, e.y)
            self.box[0] = int(ix - offx)
            self.box[1] = int(iy - offy)
        else:  # resize depuis le coin opposé (ancre)
            _, ax, ay = self.drag
            aix, aiy = self.canvas_to_img(ax, ay)
            mix, miy = self.canvas_to_img(e.x, e.y)
            bw = abs(mix - aix)
            bh = bw / ASPECT
            # ancre = coin opposé fixe ; place le cadre par rapport à l'ancre
            bx = min(aix, mix)
            by = aiy - bh if miy < aiy else aiy
            self.box = [bx, by, bw, bh]
        self.clamp_box()
        self.refresh()

    def on_wheel(self, e):
        self.zoom(1.1 if e.delta > 0 else 1 / 1.1)

    def zoom(self, factor):
        if not self.src:
            return
        bx, by, bw, bh = self.box
        cx, cy = bx + bw / 2, by + bh / 2
        bw *= factor
        bh = bw / ASPECT
        self.box = [cx - bw / 2, cy - bh / 2, bw, bh]
        self.clamp_box()
        self.refresh()

    # --- rendu ---
    def cropped_36x12(self):
        bx, by, bw, bh = self.box
        crop = self.src.crop((bx, by, bx + bw, by + bh))
        return crop.resize((OUT_W, OUT_H), Image.LANCZOS)

    def refresh(self):
        if not self.src:
            return
        w, h = self.src.size
        disp = self.src.resize((max(1, int(w * self.scale)), max(1, int(h * self.scale))),
                               Image.LANCZOS)
        self.tkimg = ImageTk.PhotoImage(disp)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self.tkimg)
        bx, by = self.img_to_canvas(self.box[0], self.box[1])
        ex, ey = self.img_to_canvas(self.box[0] + self.box[2], self.box[1] + self.box[3])
        # voile sombre autour du cadre
        self.canvas.create_rectangle(0, 0, CANVAS_W, by, fill="#000", stipple="gray50", width=0)
        self.canvas.create_rectangle(0, ey, CANVAS_W, CANVAS_H, fill="#000", stipple="gray50", width=0)
        self.canvas.create_rectangle(0, by, bx, ey, fill="#000", stipple="gray50", width=0)
        self.canvas.create_rectangle(ex, by, CANVAS_W, ey, fill="#000", stipple="gray50", width=0)
        self.canvas.create_rectangle(bx, by, ex, ey, outline="#4af", width=2)
        for cx, cy in self.corners_canvas().values():
            self.canvas.create_rectangle(cx - 4, cy - 4, cx + 4, cy + 4, fill="#4af", outline="")

        # aperçu 36x12 agrandi (avec seuil d'extinction appliqué)
        prev = self.cropped_36x12()
        big = Image.new("RGB", (OUT_W, OUT_H), (0, 0, 0))
        pp, bb = prev.load(), big.load()
        thr = self.threshold.get()
        for x in range(OUT_W):
            for y in range(OUT_H):
                r, g, b = pp[x, y]
                bb[x, y] = (r, g, b) if (r + g + b) > thr else (0, 0, 0)
        big = big.resize((OUT_W * PREV_SCALE, OUT_H * PREV_SCALE), Image.NEAREST)
        self.tkprev = ImageTk.PhotoImage(big)
        self.preview.create_image(0, 0, anchor="nw", image=self.tkprev)

    # --- sorties ---
    def save_png(self):
        if not self.src:
            return
        path = filedialog.asksaveasfilename(defaultextension=".png",
                                            filetypes=[("PNG", "*.png")])
        if path:
            self.cropped_36x12().save(path)
            self.status.config(text=f"Enregistré : {path}  (36×12)")

    # --- scan BLE ---
    def resolve_address(self):
        disp = self.dev_var.get().strip()
        return self.dev_map.get(disp, disp).strip()

    def on_dev_select(self, _e=None):
        addr = self.dev_map.get(self.dev_var.get())
        if addr:
            self.status.config(text=f"Lunettes sélectionnées : {addr}")

    def scan(self):
        self.scan_btn.config(state="disabled", text="Recherche…")
        self.status.config(text="Scan BLE en cours (~6 s)…")

        def worker():
            error, devices = None, []
            try:
                devices = asyncio.run(self._scan())
            except Exception as e:
                error = str(e)
            self.root.after(0, lambda: self._populate(devices, error))

        threading.Thread(target=worker, daemon=True).start()

    async def _scan(self):
        from bleak import BleakScanner
        found = await BleakScanner.discover(timeout=6.0)
        return [(d.name or "?", d.address) for d in found]

    def _populate(self, devices, error=None):
        self.scan_btn.config(state="normal", text="🔍 Rechercher / Rafraîchir")
        if error:
            self.status.config(text=f"⚠️ Scan échoué : {error}")
            return

        def is_glasses(nm):
            return "glass" in (nm or "").lower()

        devices.sort(key=lambda nd: (not is_glasses(nd[0]), nd[0]))
        self.dev_map = {}
        values = []
        for name, addr in devices:
            disp = f"{name}  ({addr})"
            self.dev_map[disp] = addr
            values.append(disp)
        self.dev_combo["values"] = values
        glasses = [v for v in values if is_glasses(v)]
        if glasses:
            self.dev_var.set(glasses[0])
            self.status.config(text=f"{len(devices)} appareils · lunettes détectées : {glasses[0]}")
        elif values:
            self.status.config(text=f"{len(devices)} appareils trouvés — aucune « GLASSES », choisis dans la liste.")
        else:
            self.status.config(text="Aucun appareil BLE trouvé. Lunettes allumées ? Clique à nouveau pour réessayer.")

    def send(self):
        if not self.src:
            return
        address = self.resolve_address()
        if not address:
            messagebox.showwarning("Adresse manquante",
                                   "Choisis les lunettes (bouton Rechercher) ou saisis l'adresse BLE.")
            return
        payload, bitmap_len = build_payload(self.cropped_36x12(), self.threshold.get())
        self.send_btn.config(state="disabled")

        def log(msg):
            self.root.after(0, lambda: self.status.config(text=f"[{ts()}] {msg}"))

        def worker():
            try:
                asyncio.run(send_payload(address, payload, bitmap_len, log))
            except Exception as e:
                log(f"⚠️ Échec : {e}")
            finally:
                self.root.after(0, lambda: self.send_btn.config(state="normal"))

        threading.Thread(target=worker, daemon=True).start()


def main():
    args = [a for a in sys.argv[1:]]
    image_path = None
    address = ""
    for a in args:
        if ":" in a and len(a) >= 17 and a.count(":") >= 5:
            address = a
        else:
            image_path = a
    root = tk.Tk()
    CropTool(root, image_path=image_path, address=address)
    root.mainloop()


if __name__ == "__main__":
    main()
