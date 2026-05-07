<!--
  README for Apocalypse Drive.
  No em dashes. Anywhere. Ever.
  Hard rule, scan before commit.
-->

<p align="center">
  <img src="docs/banner.svg" alt="Apocalypse Offline Knowledge Terminal" width="100%"/>
</p>

<p align="center">
  <a href="https://github.com/hratterman/apocalypse-drive/stargazers"><img src="https://img.shields.io/github/stars/hratterman/apocalypse-drive?style=for-the-badge&color=ff8800&labelColor=0a0500&logo=github&logoColor=ffb347" alt="stars"/></a>
  <img src="https://img.shields.io/badge/license-MIT-ffb347?style=for-the-badge&labelColor=0a0500" alt="license"/>
  <img src="https://img.shields.io/badge/runs%20on-macOS%20%7C%20Linux%20%7C%20Windows-ffb347?style=for-the-badge&labelColor=0a0500" alt="platforms"/>
  <img src="https://img.shields.io/badge/internet%20required-no-4ade80?style=for-the-badge&labelColor=0a0500" alt="offline"/>
  <img src="https://img.shields.io/badge/llm-llama%203.x-ffb347?style=for-the-badge&labelColor=0a0500" alt="llama"/>
</p>

<p align="center">
  <b>A self-contained offline knowledge library on a USB drive.</b><br/>
  <i>Wikipedia, Khan Academy, Stack Overflow, Project Gutenberg, and a working AI to talk to them.</i>
</p>

<p align="center">
  <a href="#install-the-easy-way">Install</a> ·
  <a href="#whats-on-the-drive">What's on the drive</a> ·
  <a href="#how-it-works">How it works</a> ·
  <a href="#themes">Themes</a> ·
  <a href="#headless-install">Headless install</a>
</p>

---

## Install (the easy way)

Download for your OS, double-click, walk through the setup wizard. The wizard runs in your browser at `localhost:8888` and stays out of your way after.

| OS | Download | Size |
|---|---|---|
| **All-in-one (portable)** | [apocalypse-drive.zip](https://github.com/hratterman/apocalypse-drive/releases/latest) | ~150 MB |
| **macOS** (Apple Silicon + Intel) | [Apocalypse-macOS.dmg](https://github.com/hratterman/apocalypse-drive/releases/latest) | ~50 MB |
| **Windows 10/11** | [Apocalypse-Windows-Setup.exe](https://github.com/hratterman/apocalypse-drive/releases/latest) | ~50 MB |
| **Linux x86_64** | [Apocalypse-Linux-x86_64.tar.gz](https://github.com/hratterman/apocalypse-drive/releases/latest) | ~50 MB |

The installer is small. ZIM files (Wikipedia and friends) are downloaded on demand by the setup wizard, so you only get what you ask for.

> **First launch on macOS** hits Gatekeeper because the app isn't code-signed. Right-click `Apocalypse.app`, choose Open, confirm. Once.<br/>
> **First launch on Windows** may flash SmartScreen. Click "More info" then "Run anyway". Once.

### Use it as a portable drive (any OS, no internet)

The `apocalypse-drive.zip` bundle contains the macOS, Windows, and Linux launchers in one archive. Workflow:

1. Format a USB drive as **exFAT** (works on macOS + Windows + Linux out of the box).
2. Unzip `apocalypse-drive.zip` to the drive root.
3. On any computer, open the matching OS subfolder and run that launcher. The same drive works on a Mac at home, a friend's PC, a Linux laptop on a plane.

The first run wizard now has a Step 1 drive picker. Point it at the `apocalypse/` folder on the USB drive (or anywhere else), and ZIMs and the local LLM all land there. No internet needed at runtime once the drive is populated.

If you don't want our app at all, you can still read every ZIM with any [Kiwix reader](https://kiwix.org). The data on the drive is open format.

### What you'll see

After install, an amber `A` icon shows up in your menu bar (macOS) or system tray (Windows/Linux). Click it to open the app. On first run it auto-opens the setup wizard:

<p align="center">
  <img src="docs/screenshots/wizard-step1-bundles.png" alt="Setup wizard, step 1: pick a starter bundle" width="48%"/>
  <img src="docs/screenshots/wizard-step2-zims.png" alt="Setup wizard, step 2: pick individual ZIMs with live size meter" width="48%"/>
</p>

Pick a starter pack, or check individual ZIMs. The disk meter updates live so you know what'll fit. Click Start Download, walk away. Come back to a working library.

### Manage what's on the drive

The Library page lets you add or remove ZIMs anytime. No reinstalls, no terminal.

<p align="center">
  <img src="docs/screenshots/admin-library.png" alt="Library page: stats, downloads, installed, and available ZIMs" width="80%"/>
</p>

---

## What's on the drive

Every ZIM is curated from [library.kiwix.org](https://library.kiwix.org). All of these can be added or removed from the Library page anytime.

| Pack | Size | What's in it |
|---|---|---|
| **Survival Pack** | ~64 GB | Wikipedia (top 1M), WikiMed, iFixit, Army Field Manuals, Self-Reliance, Wikivoyage |
| **Student Pack** | ~248 GB | Wikipedia (top 1M), Khan Academy, Wikibooks, Wiktionary, CrashCourse |
| **Developer Pack** | ~105 GB | Stack Overflow, Ask Ubuntu, Super User, Server Fault, Unix SE, Electronics SE, Cheatography, Math SE |
| **Everything** | ~1.07 TB | All 40 curated ZIMs across 7 categories |

Or pick à la carte from any of these:

<table>
<tr><td><b>Reference</b></td><td>Wikipedia (full or top 1M), Wiktionary, Wikiquote</td></tr>
<tr><td><b>Medical & Survival</b></td><td>WikiMed, iFixit, Survivor Library, Self-Reliance, Army Field Manuals, Canadian Prepper</td></tr>
<tr><td><b>Education</b></td><td>Khan Academy, CrashCourse, Wikibooks, Wikiversity, PhET Simulations, VOA Learning English</td></tr>
<tr><td><b>Literature</b></td><td>Project Gutenberg (70,000+ books), Wikisource</td></tr>
<tr><td><b>Tech & Programming</b></td><td>Stack Overflow, Ask Ubuntu, Super User, Server Fault, Unix SE, Electronics SE, DIY SE, Cheatography, MediaWiki docs</td></tr>
<tr><td><b>Science & Math</b></td><td>Math SE, Physics SE, Wikispecies, NASA APOD, TeX/LaTeX SE, Blender SE</td></tr>
<tr><td><b>Travel & Geography</b></td><td>Wikivoyage, GIS SE</td></tr>
<tr><td><b>Culture & Talks</b></td><td>TED Talks (full or topic-filtered), Wikinews</td></tr>
</table>

Plus a local LLM (Llama 3.2 3B or Llama 3.1 8B via [Mozilla llamafile](https://github.com/Mozilla-Ocho/llamafile)) that answers questions using the ZIMs as a knowledge base.

---

## How it works

<p align="center">
  <img src="docs/pipeline.svg" alt="RAG pipeline diagram" width="100%"/>
</p>

When you ask a question, four things happen in sequence:

1. **Predict titles.** The local LLM sees your question with a few-shot prompt and emits 1 to 3 Wikipedia article titles it thinks are relevant.
2. **Resolve.** A custom Python shim opens the predicted titles directly in the ZIM, walking redirects and disambiguation. Title-prediction beats keyword search by a wide margin on question-shaped queries (e.g. "how do I build a bridge?" lands on `Bridge`, not `Build` (the software-build article)).
3. **Score and extract.** Each candidate gets ranked by lede content overlap. The best one has its densest paragraph extracted, not just the lede.
4. **Generate.** The same LLM gets the question plus the extracted context and writes a real multi-paragraph answer with citations back to the source ZIMs.

End to end, ~22 to 40 seconds on a 3B model, faster on hardware that can run 8B.

### Drive layout

<p align="center">
  <img src="docs/layout.svg" alt="Drive layout: kiwix/, llm/, bin/, logs/" width="100%"/>
</p>

---

## Themes

Five themes ship with the app. Switch from the Apocalypse main page anytime.

<p align="center">
  <img src="docs/screenshots/screenshot-terminal.png" alt="Terminal theme: amber phosphor with CRT scanlines" width="48%"/>
  <img src="docs/screenshots/screenshot-paper.png" alt="Paper theme: warm cream with serif" width="48%"/>
</p>

<p align="center">
  <img src="docs/screenshots/screenshot-forest.png" alt="Forest theme: muted greens" width="48%"/>
  <img src="docs/screenshots/screenshot-solarized.png" alt="Solarized theme: classic gentle palette" width="48%"/>
</p>

<p align="center">
  <i>And one for the lols:</i><br/>
  <img src="docs/screenshots/screenshot-geocities.png" alt="Geocities 1997 theme: tiled stars, marquee, Comic Sans, visitor counter" width="80%"/>
</p>

The "calm" themes (Paper, Forest, Solarized) have zero animation. Terminal has the CRT effects. Geocities is the joke.

---

## Headless install

If you're on a server, a Linux box without a desktop, or you just prefer terminal, there's a headless install path. It does the same thing as the GUI, just without the GUI.

```bash
git clone https://github.com/hratterman/apocalypse-drive.git
cd apocalypse-drive
./install.sh /Volumes/MyDrive/apocalypse
```

Then walk through the prompts. ZIM downloads start at the end. Walk away for a few hours, come back to a working library at `localhost:8888`.

---

## Why a custom shim?

The official `kiwix-serve` binary doesn't work with Wikipedia ZIMs on macOS+exFAT (you get `MMapException` because exFAT doesn't support `mmap` of files larger than 4 GB). Wikipedia is 124 GB. So the app uses its own Python shim built on `libzim` that does direct random reads instead of mmap. The shim also adds the RAG pipeline, the setup wizard, and the admin page. Everything lives in `bin/kiwix_shim.py` and `bin/setup_routes.py`.

---

## Live catalog and full Kiwix browse

The app pulls live metadata from Kiwix every time it runs, so URLs and sizes for the curated 40 ZIMs always point at the freshest available version. If Kiwix is unreachable (no internet, server down) the app silently falls back to the bundled catalog and keeps working.

The Library page also has a **Browse All Kiwix** section that searches the entire live Kiwix library (currently ~3,500 entries across every language and topic Kiwix publishes). Anything they offer, you can install with one click. Curated bundles are a good starting point. Browse is for when you want everything else.

![Browse all Kiwix](docs/screenshots/admin-browse-kiwix.png)

---

## Customization

| Want to | Edit |
|---|---|
| Add or remove ZIMs from the catalog | `data/catalog.json` |
| Change the bundle presets | `data/catalog.json` (the `bundles` block) |
| Tweak the RAG prompt | `bin/kiwix_shim.py` (search for `predict_titles` or `_answer`) |
| Restyle the wizard | `bin/templates/setup.html` |
| Restyle the search UI | `Apocalypse.html` |
| Add a new theme | `Apocalypse.html` (CSS variables + theme name in `THEMES` array) |

---

## Limitations

- Search inside ZIMs (full-text search) works on most ZIMs but not all. Title-based and LLM-based lookups always work.
- The 3B model is fast but mediocre on dense academic questions. Pick the 8B model if you have 16+ GB RAM.
- Stack Overflow ZIM is read-only and doesn't include comments, only the question and accepted answer.
- ZIMs are dated. The Wikipedia in your drive is whatever was current the day you downloaded it. Re-download to refresh.
- iOS isn't supported. Apple won't allow this kind of app in the App Store and side-loading is too painful for v1.

---

## Built on top of

- [Kiwix](https://kiwix.org) and [openZIM](https://openzim.org) for the ZIM format and the actual content
- [Mozilla llamafile](https://github.com/Mozilla-Ocho/llamafile) for the local LLM runtime
- [Meta Llama 3](https://llama.meta.com) for the model itself
- [libzim](https://github.com/openzim/python-libzim) for Python bindings
- [pystray](https://github.com/moses-palmer/pystray) for the cross-platform tray
- [PyInstaller](https://pyinstaller.org) for the self-contained app bundles
- Every Wikipedia editor, Khan Academy lecturer, and Stack Overflow answer-writer who made the actual content

---

## License

[MIT](LICENSE).

The bundled ZIMs and the LLM each have their own licenses (mostly CC-BY-SA for ZIMs and Llama 3 Community License for the model). Don't sell this. Do whatever else you want.

---

<p align="center">
  Built by <a href="https://henryratterman.com">Henry Ratterman</a> in Bloomington, Indiana<br/>
  <sub>Marketing major. Builds things anyway.</sub>
</p>
