"""Email the result of one extraction run (CI step).

Reads the TAB-separated summary drive_flow.py wrote (STATUS, name, seconds,
detail) and sends a short report over Gmail SMTP. Silently does nothing when the
mail secrets are absent, so the workflow still succeeds for anyone who hasn't
set them up.

Env:
  GMAIL_USER          the sending Gmail address
  GMAIL_APP_PASSWORD  a Google App Password (NOT the account password)
  NOTIFY_TO           recipient(s), comma-separated (default: GMAIL_USER)
  NOTIFY_ON           "always" (default) or "problems" -- only mail when
                      something was skipped/failed, or the job itself crashed
  RUN_URL, JOB_STATUS optional context from the workflow

Usage: python notify_email.py summary.txt
"""
import os
import smtplib
import sys
from email.message import EmailMessage
from pathlib import Path


def read_summary(path: Path):
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        while len(parts) < 4:
            parts.append("")
        rows.append(parts[:4])
    return rows


def build(rows, job_status: str, run_url: str):
    ok = [r for r in rows if r[0] == "OK"]
    bad = [r for r in rows if r[0] != "OK"]
    crashed = job_status not in ("success", "")

    if crashed and not rows:
        subject = "Drive extract: run FAILED before any image was processed"
    elif bad:
        subject = f"Drive extract: {len(ok)} done, {len(bad)} need attention"
    elif ok:
        subject = f"Drive extract: {len(ok)} image(s) done"
    else:
        subject = "Drive extract: nothing to process"

    lines = []
    if ok:
        lines.append(f"PROCESSED ({len(ok)})")
        lines += [f"  {r[1]}  [{r[2]}s]\n    {r[3]}" for r in ok]
        lines.append("")
    if bad:
        lines.append(f"NOT PROCESSED ({len(bad)}) -- left in Incoming, retried "
                     "on the next upload or a manual run")
        lines += [f"  {r[1]}  [{r[0]}, {r[2]}s]\n    {r[3]}" for r in bad]
        lines.append("")
    if not rows:
        lines.append("No images were handled in this run.")
        lines.append("")
    lines.append(f"Job status: {job_status or 'unknown'}")
    if run_url:
        lines.append(f"Full log:   {run_url}")
    return subject, "\n".join(lines)


def main():
    summary = Path(sys.argv[1] if len(sys.argv) > 1 else "summary.txt")
    user = os.environ.get("GMAIL_USER", "").strip()
    # Google shows the app password as 4 groups of 4; the spaces are display
    # only, so drop all whitespace rather than fail the login on a pasted space.
    pw = "".join(os.environ.get("GMAIL_APP_PASSWORD", "").split())
    if not user or not pw:
        print("[MAIL] GMAIL_USER / GMAIL_APP_PASSWORD not set; skipping email.")
        return
    to = [a.strip() for a in
          (os.environ.get("NOTIFY_TO") or user).split(",") if a.strip()]
    rows = read_summary(summary)
    job_status = os.environ.get("JOB_STATUS", "")
    subject, body = build(rows, job_status, os.environ.get("RUN_URL", ""))

    if os.environ.get("NOTIFY_ON", "always") == "problems":
        if job_status in ("success", "") and all(r[0] == "OK" for r in rows):
            print("[MAIL] nothing wrong and NOTIFY_ON=problems; skipping email.")
            return

    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = user, ", ".join(to), subject
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as smtp:
        smtp.login(user, pw)
        smtp.send_message(msg)
    print(f"[MAIL] sent to {', '.join(to)}: {subject}")


if __name__ == "__main__":
    main()
