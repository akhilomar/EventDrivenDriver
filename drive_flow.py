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
import http.client
import io
import os
import shutil
import socket
import ssl
import subprocess
import sys
import time
from pathlib import Path

PIPELINE = str(Path(__file__).resolve().parent / "pipeline.py")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from names import safe_name

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

SCOPES = ["https://www.googleapis.com/auth/drive"]
TOKEN = "token_flow.json"
SCRATCH = Path("_drive_scratch")
IMAGE_MIMES = ("image/png", "image/jpeg", "image/jpg", "image/webp")


def _drop_connections(h):
    """Discard httplib2's pooled sockets so the next call dials fresh.
    `h` may be an AuthorizedHttp wrapper or a raw httplib2.Http."""
    for obj in (h, getattr(h, "http", None)):
        conns = getattr(obj, "connections", None)
        if isinstance(conns, dict):
            for c in list(conns.values()):
                try:
                    c.close()
                except Exception:
                    pass
            conns.clear()


def execute(req, tries=6):
    """Run a Drive API request with retries that survive dead sockets.

    Extraction takes minutes per image, so the pooled HTTPS connection is often
    stale by the time we upload -> ssl.SSLEOFError / BrokenPipe on the first
    call. googleapiclient's own num_retries covers transient 5xx/rate limits;
    the outer loop additionally drops httplib2's cached connections so the next
    attempt dials a fresh socket.
    """
    import random
    for attempt in range(tries):
        try:
            return req.execute(num_retries=4)
        except (ssl.SSLError, socket.error, http.client.HTTPException,
                OSError) as e:
            if attempt == tries - 1:
                raise
            _drop_connections(req.http)
            wait = min(2 ** attempt, 30) + random.random()
            print(f"[RETRY] {type(e).__name__}: {e} -- retrying in {wait:.1f}s "
                  f"({attempt + 1}/{tries - 1})")
            time.sleep(wait)


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
        meta = execute(svc.files().get(fileId=name_or_id, fields="id,mimeType"))
        if meta["mimeType"] == "application/vnd.google-apps.folder":
            return meta["id"]
    except Exception:
        pass
    q = ("mimeType='application/vnd.google-apps.folder' and trashed=false and "
         f"name='{name_or_id}'")
    hits = execute(svc.files().list(
        q=q, pageSize=50,
        fields="files(id,name,ownedByMe,capabilities(canAddChildren))"
    )).get("files", [])

    if create:
        # need a WRITABLE folder — prefer one you own; ignore read-only shares.
        writable = [f for f in hits if f.get("capabilities", {}).get("canAddChildren")]
        owned = [f for f in writable if f.get("ownedByMe")]
        pick = owned or writable
        if pick:
            return pick[0]["id"]
        # no writable match (e.g. only a view-only shared folder) -> make our own
        folder = execute(svc.files().create(
            body={"name": name_or_id, "mimeType": "application/vnd.google-apps.folder"},
            fields="id"))
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
        resp = execute(svc.files().list(
            q=q, fields="nextPageToken, files(id,name,mimeType)", pageToken=tok))
        out.extend(resp.get("files", []))
        tok = resp.get("nextPageToken")
        if not tok:
            break
    return out


def download(svc, file_id, dest: Path, tries=4):
    dest.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(tries):
        req = svc.files().get_media(fileId=file_id)
        buf = io.FileIO(str(dest), "wb")
        try:
            dl = MediaIoBaseDownload(buf, req)
            done = False
            while not done:
                _, done = dl.next_chunk(num_retries=4)
            return
        except (ssl.SSLError, socket.error, http.client.HTTPException, OSError) as e:
            if attempt == tries - 1:
                raise
            _drop_connections(req.http)
            print(f"[RETRY] download {type(e).__name__}: {e}")
            time.sleep(2 ** attempt)
        finally:
            buf.close()


def upload_file(svc, f: Path, parent_id: str, tries=5):
    """Upload one file, rebuilding the media object per attempt (a consumed
    MediaFileUpload cannot be replayed after a mid-stream socket death)."""
    for attempt in range(tries):
        try:
            media = MediaFileUpload(str(f), resumable=True, chunksize=8 * 1024 * 1024)
            req = svc.files().create(body={"name": f.name, "parents": [parent_id]},
                                     media_body=media, fields="id")
            resp = None
            while resp is None:
                _, resp = req.next_chunk(num_retries=4)
            return resp
        except (ssl.SSLError, socket.error, http.client.HTTPException, OSError) as e:
            if attempt == tries - 1:
                raise
            _drop_connections(svc._http)
            print(f"[RETRY] upload {f.name}: {type(e).__name__}: {e}")
            time.sleep(2 ** attempt)


def upload_folder(svc, local: Path, parent_id: str):
    top = execute(svc.files().create(
        body={"name": local.name, "parents": [parent_id],
              "mimeType": "application/vnd.google-apps.folder"},
        fields="id, webViewLink"))
    for f in sorted(local.glob("*")):
        if f.is_file():
            upload_file(svc, f, top["id"])
    for subname in ("icons", "icons_4k", "numbered"):
        sub = local / subname
        if sub.is_dir() and any(sub.glob("*.png")):
            sid = execute(svc.files().create(
                body={"name": subname, "parents": [top["id"]],
                      "mimeType": "application/vnd.google-apps.folder"},
                fields="id"))["id"]
            for f in sorted(sub.glob("*.png")):
                upload_file(svc, f, sid)
    return top.get("webViewLink")


def move_file(svc, file_id, new_parent, old_parent):
    execute(svc.files().update(fileId=file_id, addParents=new_parent,
                               removeParents=old_parent, fields="id"))


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
    # same sanitiser the pipeline used to name the folder: a Drive filename can
    # end in a space, which Windows drops when it creates the directory.
    out = Path(outbase) / safe_name(img_path)
    return (out, None) if out.exists() else (None, "no output produced")


def process_one(svc, img, input_id, archive_id, output_id, res4k, timeout,
                keep_scratch=False):
    """Download, extract, upload, archive a single Drive image dict.

    Never raises: a failure on one image is reported and the image is left in
    the input folder so the next run retries it, instead of aborting the batch.
    """
    print(f"\n=== {img['name']} ===")
    t0 = time.time()
    local_img = SCRATCH / img["name"]
    out_folder = None
    try:
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
    except KeyboardInterrupt:
        raise
    except Exception as e:
        print(f"[FAIL] {img['name']}: {type(e).__name__}: {e}. Left in input "
              f"folder for retry. Elapsed {time.time() - t0:.1f}s")
        return False
    finally:
        if not keep_scratch:
            # a long batch otherwise fills the disk with 4K PNGs
            try:
                local_img.unlink(missing_ok=True)
                if out_folder and Path(out_folder).exists():
                    shutil.rmtree(out_folder, ignore_errors=True)
            except Exception:
                pass


def run_once(svc, input_id, archive_id, output_id, res4k, timeout,
             keep_scratch=False):
    images = list_images(svc, input_id)
    if not images:
        print("[DRIVE] no images in input folder.")
        return 0
    print(f"[DRIVE] {len(images)} image(s) to process.")
    ok = 0
    for i, img in enumerate(images, 1):
        print(f"\n--- [{i}/{len(images)}] ---")
        ok += bool(process_one(svc, img, input_id, archive_id, output_id,
                               res4k, timeout, keep_scratch))
    failed = len(images) - ok
    print(f"\n[DRIVE] batch done: {ok} ok, {failed} left in input folder"
          + (" — re-run to retry them." if failed else "."))
    return len(images)


def watch_events(svc, input_id, archive_id, output_id, res4k, timeout, interval,
                 keep_scratch=False):
    """Event-style watcher using Drive's Changes feed: reacts within `interval`
    seconds of an upload to the input folder, fetching only deltas (cheap) rather
    than re-listing the whole folder each poll."""
    # process anything already sitting in the folder first
    print("[WATCH] clearing any existing images in input folder...")
    run_once(svc, input_id, archive_id, output_id, res4k, timeout, keep_scratch)

    page_token = execute(svc.changes().getStartPageToken())["startPageToken"]
    print(f"[WATCH] event mode: polling changes every {interval}s "
          f"({timeout}s/image timeout). Drop images in the input folder — "
          "they process automatically. Ctrl+C to stop.")
    while True:
        try:
            token = page_token
            while token:
                resp = execute(svc.changes().list(
                    pageToken=token, spaces="drive", pageSize=100,
                    fields=("newStartPageToken, nextPageToken, "
                            "changes(removed, file(id,name,mimeType,parents,trashed))")
                ))
                for ch in resp.get("changes", []):
                    if ch.get("removed"):
                        continue
                    f = ch.get("file") or {}
                    if (not f.get("trashed") and f.get("mimeType") in IMAGE_MIMES
                            and input_id in (f.get("parents") or [])):
                        process_one(svc, {"id": f["id"], "name": f["name"]},
                                    input_id, archive_id, output_id, res4k,
                                    timeout, keep_scratch)
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
    ap.add_argument("--keep-scratch", action="store_true",
                    help="keep downloaded images and local results in _drive_scratch "
                         "(default: delete after a successful upload)")
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
        meta = execute(svc.files().get(
            fileId=args.file_id, fields="id,name,mimeType"))
        if meta.get("mimeType") not in IMAGE_MIMES:
            print(f"[SKIP] {meta.get('name')}: not an image ({meta.get('mimeType')}).")
            return
        process_one(svc, {"id": meta["id"], "name": meta["name"]},
                    input_id, archive_id, output_id, args.res4k, args.timeout,
                    args.keep_scratch)
        return

    if args.watch:
        watch_events(svc, input_id, archive_id, output_id,
                     args.res4k, args.timeout, args.interval, args.keep_scratch)
    else:
        run_once(svc, input_id, archive_id, output_id, args.res4k, args.timeout,
                 args.keep_scratch)


if __name__ == "__main__":
    main()
