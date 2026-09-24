# 🕶️ ShiningGlass — piloter les lunettes LED Heaton depuis un PC

Outils Python pour contrôler les lunettes LED **Heaton « Shining Glasses »** directement depuis
un PC Windows en **Bluetooth Low Energy**, sans l'appli mobile : texte défilant, images en
**vraie couleur par pixel**, GIF animés, spectre audio en temps réel.

Les lunettes parlent le même protocole que le **Shining Mask** (même fabricant). Ce protocole a
été reconstitué à partir du projet [GoneUp/mask-go](https://github.com/GoneUp/mask-go), de
captures HCI Bluetooth et de l'analyse de l'appli officielle Android.

- Afficheur : **36 colonnes × 12 pixels**, RGB par pixel.
- Plateforme testée : **Windows 11** (via [bleak](https://github.com/hbldh/bleak), donc a priori
  portable sur Linux/macOS, mais non testé).

> ⚠️ Projet non officiel, sans lien avec le fabricant. Utilisation à tes risques : un mauvais
> usage du canal temps réel (`…960b`) peut figer le firmware jusqu'au redémarrage des lunettes.

---

## 🚀 Démarrage rapide

### 1. Installer `uv` (une seule fois)

Chaque script est **autonome** ([PEP 723](https://peps.python.org/pep-0723/)) : ses dépendances
sont déclarées dans son en-tête et [`uv`](https://docs.astral.sh/uv/) installe Python et les
dépendances tout seul au premier lancement. Rien d'autre à installer.

```powershell
winget install --id=astral-sh.uv -e
# ou : powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Ferme puis rouvre PowerShell, et vérifie avec `uv --version`.

### 2. Préparer les lunettes

- Active le Bluetooth de Windows, allume les lunettes.
- **N'appaire pas** les lunettes dans les paramètres Bluetooth de Windows : les scripts s'y
  connectent eux-mêmes. Si elles sont déjà appairées, supprime l'appareil.
- **Ferme l'appli mobile** : les lunettes n'acceptent qu'**une seule connexion à la fois**.

### 3. Lancer le studio

```powershell
uv run glasses_studio.py
```

Le premier lancement est plus long (téléchargement de Python et des dépendances), les suivants
sont instantanés.

---

## 🎛️ `glasses_studio.py` — l'interface tout-en-un (recommandée)

```powershell
uv run glasses_studio.py              # ouvre l'interface
uv run glasses_studio.py anim.gif     # ouvre directement un fichier
```

- **Scan BLE** : trouve les lunettes et les propose dans une liste, connexion persistante.
- **Image ou GIF** (PNG, JPG, GIF, BMP, WebP) avec **recadrage interactif** verrouillé au ratio
  3:1 et **aperçu 36×12** en direct.
- **Envoyer image** : image fixe en couleur par pixel, **persistante** (comme une image d'usine).
- **Envoyer animation (boucle stockée)** *(recommandé pour les GIF)* : les frames sont
  téléversées dans les lunettes, qui les enchaînent en boucle toutes seules.
- **Live GIF** *(expérimental)* : lecture en streaming sur le canal temps réel, avec réglages de
  FPS, keyframes, postérisation et « snap » des couleurs.
- **↻ Réafficher** : renvoie l'image courante, par exemple après un appui sur le bouton physique
  des lunettes (qui les ramène à l'animation d'usine).
- **Enregistrer PNG 36×12** : exporte le rendu sans rien envoyer.

> 🪟 Si la fenêtre ne s'ouvre pas sous `uv` (Tkinter absent), utilise le Python système :
> `pip install bleak pycryptodome pillow` puis `python glasses_studio.py`.

---

## 🧰 Les autres outils

L'adresse BLE des lunettes (ex. `AA:BB:CC:DD:EE:FF`) est le premier argument de chaque commande.
Pour la trouver : `uv run tools/glasses_status.py --scan`.

### Texte défilant — `glasses_text.py`

```powershell
uv run glasses_text.py <addr> --text "SALUT"                          # défile si > 36 px
uv run glasses_text.py <addr> --text "GUTS" --color 255 0 0           # en rouge
uv run glasses_text.py <addr> --text "GO" --mode scroll-left --speed 6
uv run glasses_text.py <addr> --text "HI" --mode blink                # auto|steady|blink|scroll-left|scroll-right
uv run glasses_text.py <addr> --text "HI" --font-size 10
uv run glasses_text.py <addr> --image logo.png                        # image monochrome
uv run glasses_text.py <addr> --text "X" --preview apercu.png         # rendu PNG, n'envoie RIEN
uv run glasses_text.py <addr> --text "X" -v                           # trace du handshake
```

Ce format n'accepte qu'**une couleur par colonne** ; pour la vraie couleur, utilise le studio ou
`tools/glasses_pixel.py`.

### Spectre audio en temps réel — `glasses_audio.py`

Capture la sortie audio par défaut du PC (loopback), calcule une FFT et affiche 36 barres.

```powershell
uv run glasses_audio.py <addr>                     # barres blanches
uv run glasses_audio.py <addr> --color 0 255 0     # barres vertes
uv run glasses_audio.py <addr> --gain 1.5          # plus sensible
uv run glasses_audio.py --list                     # lister les sorties audio
```

Compter 5 à 10 fps : chaque image impose un aller-retour BLE complet (limite du protocole).

### GIF en animation stockée — `glasses_anim.py`

Même fonction que le bouton du studio, en ligne de commande (20 frames max).

```powershell
uv run glasses_anim.py <addr> anim.gif [--frames 20] [--snap] [--speed 6]
uv run glasses_anim.py X anim.gif --contact planche.png          # planche contact numérotée
uv run glasses_anim.py <addr> anim.gif --select 0,4,8,12,16      # envoyer exactement ces frames
uv run glasses_anim.py X anim.gif --dry-run                      # stats hors-ligne, sans BLE
```

### Outils secondaires — `tools/`

| Script | Rôle |
|---|---|
| `glasses_status.py` | Diagnostic : les lunettes sont-elles allumées, connectables, LED OK ? (`--scan`, `<addr>`, `<addr> --test`) |
| `glasses_pixel.py` | Image couleur par pixel en CLI (`--image`, `--pattern`, `--preview`) |
| `glasses_crop.py` | Ancienne interface de recadrage + envoi (remplacée par le studio) |
| `glasses_gif.py` | Lecteur GIF temps réel en CLI sur `…960b` (`--dry-run` pour des stats hors-ligne) |
| `glasses_realtime.py` | Sonde du canal temps réel `…960b` (`corners`, `sweep`, `pixel`, …) |

### Archives — `archive/`

Premiers scripts d'exploration, conservés pour l'historique : `glasses_ble.py` (scan et dump
GATT), `glasses_ctl.py` (commandes brutes : luminosité, images d'usine, modes, `raw`),
`glasses_pixel_test.py` et `analyze_snoop.py` (décodage d'une capture `btsnoop_hci.log`
Android).

---

## 🔬 Le protocole en bref

Service `0000fff0`, caractéristiques `d44bc439-abfd-45a2-b575-9254161296xx` :

| Caractéristique | Rôle |
|---|---|
| `…9600` | Commandes, chiffrées en **AES-128-ECB** (clé statique, identique à celle du Shining Mask) |
| `…9601` | Notifications (réponses chiffrées : `DATSOK`, `REOK`, `DATCPOK`, …) |
| `…960a` | Données brutes d'image (paquets de ~98 octets) |
| `…960b` | Canevas temps réel du mode DIY (trames non chiffrées) |

Une commande est `[longueur][OP ASCII][args]`, complétée par des zéros jusqu'à un multiple de
16 octets, puis chiffrée.

**Envoi d'une image** (`payload = bitmap + couleurs`) :

```
DATS [total:u16be][bitmap:u16be][type]   → "DATSOK"
paquets [len+1][idx][data] sur …960a     → "REOK" (chacun)
DATCP                                    → "DATCPOK"
```

- `type = 0x00` : une couleur RGB par **colonne** (36 triplets), suivi d'un `MODE`.
- `type = 0x01` : une couleur RGB par **pixel** (432 triplets, ordre colonne par colonne). C'est
  le format DIY de l'appli officielle. **Ne pas envoyer de `MODE` ensuite**, sinon les lunettes
  reviennent à un préréglage d'usine.

**Animation stockée** : `MANY [n][0x01]`, puis *n* envois d'image comme ci-dessus, puis `PLAY`.

**Canal temps réel `…960b`** : entrée en DIY avec `SMVEW 01`, puis trames
`[len][R][G][B][col][row][col][row]…`. Le firmware garde un canevas persistant, mais **revient à
l'animation d'usine après quelques secondes sans trame**. Chaque trame doit tenir dans le MTU
négocié (sinon le pixel est perdu sans erreur), et les écritures doivent être acquittées.

Le dump GATT complet est dans [`reference/GattShinningGlasses.txt`](reference/GattShinningGlasses.txt).

---

## 🆘 Dépannage

| Symptôme | Solution |
|---|---|
| `uv` n'est pas reconnu | Rouvre PowerShell après l'installation |
| Le scan ne trouve rien | Bluetooth activé ? Lunettes allumées et pas en charge ? Essaie `tools/glasses_status.py --scan` |
| La connexion se fige ou échoue | Ferme l'appli mobile, et supprime les lunettes des appareils Bluetooth de Windows |
| `Pas de réponse attendue à temps` | Une autre connexion est active, ou les lunettes sont en veille : rallume-les |
| Écran noir | Souvent normal (canevas DIY noir, veille). `tools/glasses_status.py <addr> --test` affiche du blanc puis R/V/B pour vérifier les LED |
| L'image disparaît après un appui sur le bouton | Normal : clique sur **↻ Réafficher** dans le studio |
| Les lunettes ne répondent plus après un Live GIF | Éteins-les puis rallume-les ; préfère l'animation stockée |

---

## 🙏 Remerciements

- [GoneUp/mask-go](https://github.com/GoneUp/mask-go) pour le protocole Shining Mask (chiffrement,
  handshake `DATS`/`DATCP`).
- [bleak](https://github.com/hbldh/bleak), [Pillow](https://python-pillow.org/),
  [pycryptodome](https://www.pycryptodome.org/), [SoundCard](https://github.com/bastibe/SoundCard).

---

## 📄 Licence

Distribué sous licence [MIT](LICENSE).
