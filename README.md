# Apocalypse Drive

> A self-contained offline knowledge library — Wikipedia, Stack Exchange, Project Gutenberg, Khan Academy — with a local LLM that answers questions using those sources as context. Works on a USB drive, on any computer, without internet.

![terminal screenshot](docs/screenshot-terminal.png)

## What it is

A 2 TB external drive that, when plugged into any Mac, Linux, or Windows machine, gives you:

- **Local Wikipedia** (115 GB, full text, no images) — every English article
- **Stack Exchange** — Stack Overflow + 6 other Stack Exchange sites (~95 GB)
- **Project Gutenberg** — 70,000+ public domain books (107 GB)
- **Khan Academy** — full course content (168 GB)
- **Wikivoyage / Wikibooks / Wikisource / Wikispecies** (~28 GB)
- **A local LLM** (Llama 3.2 3B or 3.1 8B) that runs offline and answers questions using the above as sources

You ask a question. The system asks the LLM what Wikipedia article(s) might answer it, fetches those articles from the offline ZIMs, scores relevance, extracts focused sections, and generates a sourced answer with citations. Total round-trip on a recent Mac: 20-40 seconds. **No internet required.**

Built for: power outages, travel, hostile networks, places where you don't trust the cloud, and the vague unease that the world's reference shelf shouldn't live on someone else's server.

## Quick start

You need an empty external drive (~600 GB recommended for the full library, ~100 GB if you skip Khan Academy + Gutenberg) formatted as exFAT (cross-platform) or your native filesystem.

```bash
git clone https://github.com/hratterman/apocalypse-drive.git
cd apocalypse-drive
./install.sh /Volumes/MyDrive/apocalypse
```

The installer will:

1. Create the directory layout
2. Download `kiwix-serve` binaries for 5 platforms (~230 MB)
3. Download the LLM (~2.7 GB for the 3B, ~7.6 GB for both 3B + 8B)
4. Set up cross-platform launchers
5. Optionally kick off the ZIM downloads (this takes hours — go to bed, it'll be done)

When it's done, plug the drive into any computer:

| OS      | Launcher to double-click |
| ------- | ------------------------ |
| macOS   | `Apocalypse.command`     |
| Linux   | `Apocalypse.sh`          |
| Windows | `Apocalypse.bat`         |

The launcher boots two local services and opens the UI in your browser.

## Themes

Five themes, switchable from the top-right of the UI, persist via localStorage.

| | |
|---|---|
| **Terminal** — amber phosphor on black, CRT scanlines, flicker, blinking cursor. The default. | **Paper** — warm cream + serif text, calm and library-like, no animation. |
| ![terminal](docs/screenshot-terminal.png) | ![paper](docs/screenshot-paper.png) |
| **Forest** — quiet greens, sans-serif, grounded and easy on the eyes. | **Solarized** — the classic gentle palette, monospace. |
| ![forest](docs/screenshot-forest.png) | ![solarized](docs/screenshot-solarized.png) |
| **Geocities 1997** — Comic Sans, scrolling marquee, under-construction banner, visitor counter, Win95 ridge bevels. The full nostalgic chaos. | |
| ![geocities](docs/screenshot-geocities.png) | |

## How the RAG actually works

The naïve version: keyword search → top results → feed to LLM. That returns garbage when you ask "how does Tylenol work?" because the search engine ranks the Chicago Tylenol murders article above Paracetamol.

This system uses a smarter pipeline:

1. **LLM predicts titles**. With few-shot examples, the 3B model is great at this. *"how do I build a bridge?"* → `["Bridge", "Civil engineering", "Structural engineering"]`. *"why is the sky blue?"* → `["Rayleigh scattering", "Diffuse sky radiation", "Sky"]`.
2. **Resolve titles to ZIM paths**, in priority order (Wikipedia → Wikipedia Medical → Wikivoyage → Wikibooks). Tries multiple capitalizations, follows redirects (`Acetaminophen` → `Paracetamol`, `World_War_2` → `World_War_II`).
3. **Backup keyword search** runs only if the LLM gave fewer than 3 hits.
4. **Relevance scoring** drops obvious garbage. The "Build" article wouldn't survive — its lede contains zero bridge-related words.
5. **Section extraction** finds the densest cluster of question-words within the article (better than just dumping the lede).
6. **Final LLM pass** generates the answer using ONLY the focused sections, with strict citation rules.

The whole pipeline lives in `bin/kiwix_shim.py` as a single ~700-line Python HTTP server using libzim and llamafile's OpenAI-compatible API. The HTML is one self-contained file with five color themes (terminal, paper, forest, solarized, geocities-1997).

## Why a custom shim?

`kiwix-serve` (the standard binary) throws `MMapException` when reading ZIMs from exFAT-formatted drives on macOS. exFAT doesn't support `mmap()` properly, and there's no fix coming. The Python `libzim` binding doesn't use `mmap`, so this project ships a minimal HTTP server that serves the same endpoints (`/search`, `/content/<book>/<path>`) using `libzim` directly — plus the `/answer` endpoint that does the full RAG pipeline.

On Linux/Windows, the launcher uses `kiwix-serve` because it's faster. On macOS, it uses the Python shim. The user doesn't notice.

## Customization

- **Pick fewer ZIMs**: edit `kiwix/download.sh` and comment out the ones you don't want
- **Use a different model**: drop any `.llamafile` into `llm/` and the launcher will pick it up
- **Change the theme**: top-right of the UI, persists in localStorage
- **Adjust the RAG prompt**: edit the `TITLE_PREDICTION_SYSTEM` constant in `bin/kiwix_shim.py`

## Limitations

- ZIMs are **not** updated automatically. Re-run `download.sh` periodically to refresh.
- The 3B model is fast but occasionally hallucinates citations. The 8B is more reliable but needs 16+ GB RAM.
- Search latency depends on drive speed. USB-C SSDs (~500 MB/s read) feel snappy; spinning USB-A drives (~80 MB/s) feel sluggish for the first query (caches warm up).
- Only English ZIMs are supported in the default download script. To add other languages, edit `kiwix/download.sh`.

## License

MIT. Ship it, fork it, use it however.

## Credits

Built on top of:

- [Kiwix](https://kiwix.org) — the offline-content ecosystem and the ZIM file format
- [llamafile](https://github.com/Mozilla-Ocho/llamafile) — single-file LLM distribution
- [Llama 3.x](https://llama.meta.com) — Meta's open-weights models
- [libzim](https://github.com/openzim/libzim) — Python bindings for ZIM access
