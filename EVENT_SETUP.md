# Event-Driven, Free Auto-Extraction (no laptop, no polling cost)

When you drop an image in your Drive **Incoming** folder, it gets extracted
automatically — with **nothing running on your laptop** and **$0** cost.

```
Upload to Drive/Incoming
      │
      ▼
Apps Script watcher   ← free, runs on Google; fires ONLY on a new file
      │  repository_dispatch
      ▼
GitHub Actions        ← free runner; runs ONLY on real uploads
  runs pipeline.py → uploads to Extracted, moves original to Archive
```

**Why it's free:** the watcher lives on Google (Apps Script, no billing), and
GitHub Actions runs *only when there's an actual new file*, so you spend a few
runner-minutes per image instead of burning your free budget on empty checks.
CPU-only is fine since you're happy to wait up to ~30 min.

---

## One-time setup (≈20 min)

### Step 1 — Stop the 7-day token expiry (important)
Your OAuth app is in **Testing** mode, where refresh tokens die after 7 days —
that would break the unattended job weekly. Fix it once:

1. <https://console.cloud.google.com/auth/audience> → **Publishing status** →
   **Publish app** → confirm. (You'll see an "unverified app" notice — that's
   fine for personal use; you don't need Google verification.)
2. Regenerate a fresh token locally (browser opens once):
   ```
   del token_flow.json
   .\venv\Scripts\python.exe drive_flow.py --input "Incoming" --archive "Archive" --output "Extracted"
   ```
   This recreates `token_flow.json` with a long-lived refresh token.

### Step 2 — Put the project on GitHub (private)
```
cd D:\image_text\docling
git init
git add .
git commit -m "Event-driven Drive extraction pipeline"
```
Create a **private** repo on github.com, then follow its "push existing repo"
lines (something like):
```
git remote add origin https://github.com/<you>/<repo>.git
git branch -M main
git push -u origin main
```
> `.gitignore` already excludes `credentials.json`, `token_flow.json`, `venv/`,
> outputs, test images, and the unused 64 MB model. The 18 MB anime model the
> pipeline needs **is** committed.

### Step 3 — Give GitHub your Drive token (as a secret)
1. Open `token_flow.json`, copy its **entire contents**.
2. GitHub repo → **Settings → Secrets and variables → Actions → New repository
   secret**:
   - **Name:** `DRIVE_TOKEN_JSON`
   - **Value:** paste the JSON.
3. Save. (Secrets are encrypted and never shown again — this is safe.)

### Step 4 — Make a GitHub token for the watcher
The Apps Script needs to trigger the workflow:
1. GitHub → **Settings → Developer settings → Personal access tokens → Tokens
   (classic) → Generate new token (classic)**.
2. Scope: check **`repo`**. Generate and copy it.

### Step 5 — Set up the Apps Script watcher
1. Get your **Incoming folder ID**: open the folder in Drive; the URL is
   `https://drive.google.com/drive/folders/<THIS_IS_THE_ID>`.
2. Go to <https://script.google.com> → **New project**. Delete the sample code
   and paste in `apps_script/drive_watcher.gs` (from this repo).
3. Edit the three CONFIG lines at the top:
   - `INCOMING_FOLDER_ID` → the id from step 5.1
   - `GITHUB_OWNER` → your GitHub username
   - `GITHUB_REPO` → your repo name
4. **Project Settings (gear) → Script properties → Add script property:**
   - **Property:** `GITHUB_TOKEN`
   - **Value:** the token from step 4
5. Back in the editor, pick the function **`installTrigger`** and click **Run**.
   Approve the Google permission prompt. Done.

---

## Using it
Just drop an image into **Incoming**. Within ~1 minute the watcher fires GitHub
Actions; a few minutes to ~30 min later the results appear in **Extracted** and
the original moves to **Archive**.

- Watch progress: your repo → **Actions** tab → the running "Drive extract" job.
- Test manually anytime: Actions tab → "Drive extract" → **Run workflow** (leave
  file id blank to process everything currently in Incoming).
- Watcher logs: script.google.com → your project → **Executions**.

## Cost check
- **Apps Script:** free, well within Google's daily quotas at 1 run/minute.
- **GitHub Actions (private repo):** 2,000 free min/month. Each image ~10–20 min,
  so ~100+ images/month stay free. Switch the repo to public later for unlimited.

## Turning it off
- Pause: script.google.com → run **`removeTriggers`**.
- Resume: run **`installTrigger`** again.

## Troubleshooting
- **Workflow didn't start** → check the watcher's *Executions* log for the
  `dispatch … -> HTTP` line. `HTTP 204` = success; `401/403` = bad `GITHUB_TOKEN`
  scope; `404` = wrong owner/repo name.
- **Actions run fails at auth** → your `DRIVE_TOKEN_JSON` secret is stale; redo
  Steps 1–3 (make sure the app is *Published*, not Testing).
- **Same file processed twice** → shouldn't happen (the watcher remembers
  dispatched ids for 24h), but a failed run leaves the file in Incoming and it
  retries after 24h; or re-run manually.
- **Upload permission error** → `drive_flow.py` auto-creates a writable
  `Extracted` you own; make sure you're not pointing at a view-only shared folder.
