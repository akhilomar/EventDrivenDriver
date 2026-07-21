"""
Automated Google Drive flow:
  1. Look in an INPUT Drive folder for image files.
  2. Download each, run the icon/text extraction pipeline locally.
  3. Upload the result folder (icons/, icons_4k/, numbered/, text.txt, sheets)
     into an OUTPUT Drive folder.
  4. Move the original image into an ARCHIVE Drive folder.

Run once (process everything currently in the input folder):
  python drive_flow.py --input "Incoming" --archive "Archive" --output "Extracted" --res4k

Event mode (auto-process each new upload to the input folder within ~seconds):
  python drive_flow.py --input "Incoming" --archive "Archive" --output "Extracted" \
      --res4k --watch --interval 15

Folder args accept a Drive folder NAME (found or created) or a folder ID.
Requires 'credentials.json' (OAuth desktop client, Drive API enabled). First run
opens a browser for consent and saves 'token_flow.json'. Needs full Drive scope.
"""
import argparse
import io
import os
import subprocess
import sys
import time
from pathlib import Path

PIPELINE = str(Path(__file__).resolve().parent / "pipeline.py")

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

SCOPES = ["https://www.googleapis.com/auth/drive"]
TOKEN = "token_flow.json"
SCRATCH = Path("_drive_scratch")
IMAGE_MIMES = ("image/png", "image/jpeg", "image/jpg", "image/webp")


def service():
    """Build a Drive client.

    Headless (CI): if env var DRIVE_TOKEN_JSON holds the token JSON, use it and
    never open a browser. Local: read/refresh token_flow.json, or run the browser
    consent flow once.
    """
    import json
    creds = None
    tok_env = os.environ.get("DRIVE_TOKEN_JSON")
    headless = tok_env is not None
    if tok_env:
        creds = Credentials.from_authorized_user_info(json.loads(tok_env), SCOPES)
    elif Path(TOKEN).exists():
        creds = Credentials.from_authorized_user_file(TOKEN, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        elif headless:
            raise SystemExit(
                "DRIVE_TOKEN_JSON is set but has no valid/refreshable token. "
                "Publish the OAuth app (Testing -> In production) so the refresh "
                "token doesn't expire, regenerate token_flow.json locally, and "
                "update the GitHub secret.")
        else:
            if not Path("credentials.json").exists():
                raise SystemExit("Need 'credentials.json' (OAuth desktop client).")
            creds = InstalledAppFlow.from_client_secrets_file(
                "credentials.json", SCOPES).run_local_server(port=0)
        if not headless:
            Path(TOKEN).write_text(creds.to_json())
    return build("drive", "v3", credentials=creds)


def resolve_folder(svc, name_or_id, create=True):
    """Accept a folder id or name. If a name and it doesn't exist, optionally create."""
    # treat as id if it looks like one and resolves
    try:
        meta = svc.files().get(fileId=name_or_id, fields="id,mimeType").execute()
        if meta["mimeType"] == "application/vnd.google-apps.folder":
            return meta["id"]
    except Exception:
        pass
    q = ("mimeType='application/vnd.google-apps.folder' and trashed=false and "
         f"name='{name_or_id}'")
    hits = svc.files().list(
        q=q, pageSize=50,
        fields="files(id,name,ownedByMe,capabilities(canAddChildren))"
    ).execute().get("files", [])

    if create:
        # need a WRITABLE folder — prefer one you own; ignore read-only shares.
        writable = [f for f in hits if f.get("capabilities", {}).get("canAddChildren")]
        owned = [f for f in writable if f.get("ownedByMe")]
        pick = owned or writable
        if pick:
            return pick[0]["id"]
        # no writable match (e.g. only a view-only shared folder) -> make our own
        folder = svc.files().create(
            body={"name": name_or_id, "mimeType": "application/vnd.google-apps.folder"},
            fields="id").execute()
        print(f"[DRIVE] created writable folder '{name_or_id}' ({folder['id']})")
        return folder["id"]

    # input folder: only need read access — any visible match works, prefer owned
    if hits:
        owned = [f for f in hits if f.get("ownedByMe")]
        return (owned or hits)[0]["id"]
    raise SystemExit(f"Drive folder not found: {name_or_id}")


def list_images(svc, folder_id):
    q = f"'{folder_id}' in parents and trashed=false and (" + \
        " or ".join(f"mimeType='{m}'" for m in IMAGE_MIMES) + ")"
    out = []
    tok = None
    while True:
        resp = svc.files().list(q=q, fields="nextPageToken, files(id,name,mimeType)",
                                pageToken=tok).execute()
        out.extend(resp.get("files", []))
        tok = resp.get("nextPageToken")
        if not tok:
            break
    return out


def download(svc, file_id, dest: Path):
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = svc.files().get_media(fileId=file_id)
    buf = io.FileIO(str(dest), "wb")
    dl = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = dl.next_chunk()
    buf.close()


def upload_folder(svc, local: Path, parent_id: str):
    top = svc.files().create(
        body={"name": local.name, "parents": [parent_id],
              "mimeType": "application/vnd.google-apps.folder"},
        fields="id, webViewLink").execute()
    for f in sorted(local.glob("*")):
        if f.is_file():
            svc.files().create(body={"name": f.name, "parents": [top["id"]]},
                               media_body=MediaFileUpload(str(f))).execute()
    for subname in ("icons", "icons_4k", "numbered"):
        sub = local / subname
        if sub.is_dir() and any(sub.glob("*.png")):
            sid = svc.files().create(
                body={"name": subname, "parents": [top["id"]],
                      "mimeType": "application/vnd.google-apps.folder"},
                fields="id").execute()["id"]
            for f in sorted(sub.glob("*.png")):
                svc.files().create(body={"name": f.name, "parents": [sid]},
                                   media_body=MediaFileUpload(str(f))).execute()
    return top.get("webViewLink")


def move_file(svc, file_id, new_parent, old_parent):
    svc.files().update(fileId=file_id, addParents=new_parent,
                       removeParents=old_parent, fields="id").execute()


def process_with_timeout(img_path, outbase, res4k, timeout):
    """Run extraction as a fresh `pipeline.py` subprocess and kill it if it
    exceeds `timeout` seconds, so the machine never stays stuck on one image.
    A separate process (vs in-process threads) also avoids OpenMP conflicts and
    runs at full speed."""
    cmd = [sys.executable, PIPELINE, str(img_path), "--outdir", str(outbase)]
    if res4k:
        cmd.append("--res4k")
    env = dict(os.environ)
    env["HF_HUB_DISABLE_SYMLINKS"] = "1"
    env["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
    try:
        subprocess.run(cmd, timeout=timeout, env=env, check=True)
    except subprocess.TimeoutExpired:
        return None, f"timed out after {timeout}s"
    except subprocess.CalledProcessError as e:
        return None, f"pipeline failed (exit {e.returncode})"
    out = Path(outbase) / Path(img_path).stem
    return (out, None) if out.exists() else (None, "no output produced")


def process_one(svc, img, input_id, archive_id, output_id, res4k, timeout):
    """Download, extract, upload, archive a single Drive image dict."""
    print(f"\n=== {img['name']} ===")
    t0 = time.time()
    local_img = SCRATCH / img["name"]
    download(svc, img["id"], local_img)
    out_folder, err = process_with_timeout(local_img, SCRATCH / "out", res4k, timeout)
    if err:
        print(f"[SKIP] {img['name']}: {err}. Left in input folder for retry. "
              f"Elapsed {time.time() - t0:.1f}s")
        return False
    link = upload_folder(svc, out_folder, output_id)
    move_file(svc, img["id"], archive_id, input_id)
    print(f"[DONE] {img['name']} in {time.time() - t0:.1f}s "
          f"-> {link}; original archived.")
    return True


def run_once(svc, input_id, archive_id, output_id, res4k, timeout):
    images = list_images(svc, input_id)
    if not images:
        print("[DRIVE] no images in input folder.")
        return 0
    for img in images:
        process_one(svc, img, input_id, archive_id, output_id, res4k, timeout)
    return len(images)


def watch_events(svc, input_id, archive_id, output_id, res4k, timeout, interval):
    """Event-style watcher using Drive's Changes feed: reacts within `interval`
    seconds of an upload to the input folder, fetching only deltas (cheap) rather
    than re-listing the whole folder each poll."""
    # process anything already sitting in the folder first
    print("[WATCH] clearing any existing images in input folder...")
    run_once(svc, input_id, archive_id, output_id, res4k, timeout)

    page_token = svc.changes().getStartPageToken().execute()["startPageToken"]
    print(f"[WATCH] event mode: polling changes every {interval}s "
          f"({timeout}s/image timeout). Drop images in the input folder — "
          "they process automatically. Ctrl+C to stop.")
    while True:
        try:
            token = page_token
            while token:
                resp = svc.changes().list(
                    pageToken=token, spaces="drive", pageSize=100,
                    fields=("newStartPageToken, nextPageToken, "
                            "changes(removed, file(id,name,mimeType,parents,trashed))")
                ).execute()
                for ch in resp.get("changes", []):
                    if ch.get("removed"):
                        continue
                    f = ch.get("file") or {}
                    if (not f.get("trashed") and f.get("mimeType") in IMAGE_MIMES
                            and input_id in (f.get("parents") or [])):
                        process_one(svc, {"id": f["id"], "name": f["name"]},
                                    input_id, archive_id, output_id, res4k, timeout)
                if "nextPageToken" in resp:
                    token = resp["nextPageToken"]
                else:
                    page_token = resp["newStartPageToken"]
                    token = None
        except KeyboardInterrupt:
            print("\n[WATCH] stopped.")
            return
        except Exception as e:  # transient API hiccup — keep watching
            print(f"[WATCH] poll error ({e}); retrying in {interval}s.")
        time.sleep(interval)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="input Drive folder (name or id)")
    ap.add_argument("--archive", required=True, help="archive Drive folder (name or id)")
    ap.add_argument("--output", required=True, help="output Drive folder (name or id)")
    ap.add_argument("--res4k", action="store_true")
    ap.add_argument("--timeout", type=int, default=600,
                    help="max seconds to process one image before skipping it (default 600)")
    ap.add_argument("--watch", action="store_true",
                    help="event mode: auto-process new uploads to the input folder")
    ap.add_argument("--interval", type=int, default=15,
                    help="seconds between change checks when --watch (default 15)")
    ap.add_argument("--file-id", default="",
                    help="process only this Drive file id, then exit (event/CI mode)")
    args = ap.parse_args()

    svc = service()
    input_id = resolve_folder(svc, args.input, create=False)
    archive_id = resolve_folder(svc, args.archive, create=True)
    output_id = resolve_folder(svc, args.output, create=True)

    # Event/CI mode: a single file id was pushed (e.g. from Apps Script). Process
    # just that one file and exit — this is what a GitHub Actions run does.
    if args.file_id:
        meta = svc.files().get(
            fileId=args.file_id, fields="id,name,mimeType").execute()
        if meta.get("mimeType") not in IMAGE_MIMES:
            print(f"[SKIP] {meta.get('name')}: not an image ({meta.get('mimeType')}).")
            return
        process_one(svc, {"id": meta["id"], "name": meta["name"]},
                    input_id, archive_id, output_id, args.res4k, args.timeout)
        return

    if args.watch:
        watch_events(svc, input_id, archive_id, output_id,
                     args.res4k, args.timeout, args.interval)
    else:
        run_once(svc, input_id, archive_id, output_id, args.res4k, args.timeout)


if __name__ == "__main__":
    main()
