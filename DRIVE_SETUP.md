# Google Drive Auto-Extraction Flow — Setup & Usage

Automated flow: drop images in a Drive folder → the pipeline downloads each,
extracts icons + 4K icons + text + sheets → uploads the results to Drive →
moves the original image into an Archive folder.

---

## 1. Google Drive folders to create

Create **one** folder in your Google Drive for uploads:

- **`Incoming`** — you upload your source images here. **(You must create this.)**

Two more folders are used, but the pipeline **creates them automatically** if
they don't exist (you can also pre-create them):

- **`Archive`** — originals are moved here after processing.
- **`Extracted`** — result folders (one per image) are written here.

You can name them anything; just pass the names to the command (below).

---

## 2. One-time Google API configuration

You need an OAuth credential so the script can access *your* Drive.

1. Go to <https://console.cloud.google.com/> and create (or select) a project.
2. **APIs & Services → Library →** search **"Google Drive API" → Enable**.
3. **APIs & Services → OAuth consent screen:** choose **External**, fill the app
   name + your email, and under **Test users** add **shyamnarayan4104@gmail.com**.
   (Test mode is fine; no verification needed for personal use.)
4. **APIs & Services → Credentials → Create Credentials → OAuth client ID →**
   application type **Desktop app** → Create.
5. **Download JSON**, rename it to **`credentials.json`**, and place it in:
   `D:\image_text\docling\credentials.json`

That's it. The first run opens a browser to sign in and approve; a `token_flow.json`
is saved so you won't log in again.

---

## 3. Run the flow

Always use the project's virtual environment. Open a terminal in
`D:\image_text\docling` and run:

**Process everything currently in `Incoming` (one pass):**
```
<!-- .\venv\Scripts\python.exe drive_flow.py --input "Incoming" --archive "Archive" --output "Extracted" --res4k -->

.\venv\Scripts\python.exe drive_flow.py --input "Incoming" --archive "Archive" --output "Extracted" --res4k --timeout 1200
```

**Event mode (auto-process each new upload, reacts within ~15s):**
```
.\venv\Scripts\python.exe drive_flow.py --input "Incoming" --archive "Archive" --output "Extracted" --res4k --watch --interval 15
```
Leave this running. Every time you drop an image into `Incoming`, it is picked up
automatically (usually within `--interval` seconds), extracted, uploaded to
`Extracted`, and the original moved to `Archive`. It uses Google Drive's *changes
feed*, so each check is cheap — only new/changed items are fetched, not the whole
folder. Anything already in `Incoming` when you start is processed first. Ctrl+C to
stop. (True push webhooks would need a public server + tunnel, impractical on a
laptop; this polling gives the same auto-trigger behavior.)

Notes:
- Drop the `--res4k` flag if you don't need the 4K smartboard icons (much faster).
- Folder arguments accept a folder **name** or a Drive **folder ID**.
- On the very first run a browser opens for Google sign-in (one time only).

### Per-image timeout (so the laptop never gets stuck)

Each image is processed in a **separate subprocess** that is **killed if it runs
longer than `--timeout` seconds** (default **600**). Timed-out images are skipped
and **left in `Incoming`** for a later retry; the run then moves to the next image.

```
# give each image up to 20 minutes, else skip it
.\venv\Scripts\python.exe drive_flow.py --input "Incoming" --archive "Archive" --output "Extracted" --res4k --timeout 1200
```

The run prints the **elapsed time per image**, e.g.
`[DONE] lungs.png in 312.4s -> <link>; original archived.`
or `[SKIP] lungs.png: timed out after 1200s. Left in input for retry.`

**Choosing a timeout:** on CPU, extraction is ~1–3 min and each 4K icon adds
~1–1.5 min, so an image with many icons + `--res4k` can take 8–15 min. Use
`--timeout 1200` (20 min) with `--res4k`, or a shorter value like `--timeout 300`
without 4K. A slow/throttling laptop runs slower — raise the timeout if good
images are being skipped.

---

## 4. What you get, per image

In Drive under `Extracted/<image name>/`:
```
<image name>/
├── icons/            # isolated icons (1-based: icon_1.png ...)
├── icons_4k/         # 4K (3840px) smartboard icons
├── numbered/         # numbered + name-tagged copies
├── text.txt          # full extracted text, column-aware reading order
├── <name>_dark.png   # dark contact sheet
└── <name>_numbered.png
```
The original image is moved from `Incoming` to `Archive`.

**Validate the text** at `Extracted/<image name>/text.txt` (or locally at
`output/<image name>/text.txt` if you run `pipeline.py` directly).

Optional item names: put a `names.txt` in the local output folder
(`1 = Lungs`, `2 = Heart`, …) and re-run to label the numbered sheet/files.

---

## 5. Run locally without Drive (optional)

To process a local image straight from disk:
```
.\venv\Scripts\python.exe pipeline.py "Human Respiratory system.png" --res4k
```
Output goes to `output/<image name>/`.

---

## Troubleshooting

- **`Need 'credentials.json'`** → complete step 2.
- **`access_denied` on login** → add your email as a Test user (step 3).
- **Symlink / WinError 1314** → the scripts set `HF_HUB_DISABLE_SYMLINKS=1`; it's
  also persisted as a user env var. If you hit it, set it in the shell first:
  `$env:HF_HUB_DISABLE_SYMLINKS = "1"`.
- **Slow 4K** → CPU-only here; each icon's 4K pass takes ~1 min. Runs in the
  background fine; a GPU would make it near-instant.
```
