/**
 * Free Drive watcher — runs on Google's servers (Apps Script), not your laptop.
 *
 * Every minute it checks the Incoming folder. When a NEW image appears it fires a
 * GitHub `repository_dispatch` event, which starts the GitHub Actions pipeline for
 * that one file. Nothing heavy runs here — this is just the free event source.
 *
 * SETUP (one time):
 *   1. Go to https://script.google.com  ->  New project. Paste this file in.
 *   2. Fill in the three CONFIG values below (folder id + your GitHub user/repo).
 *   3. Project Settings -> Script properties -> add property:
 *          name:  GITHUB_TOKEN
 *          value: a GitHub PAT (classic) with the "repo" scope
 *   4. Run `installTrigger` once (authorize when prompted). Done.
 *
 * To stop: run `removeTriggers`.
 */

// ---------------- CONFIG ----------------
const INCOMING_FOLDER_ID = 'PASTE_INCOMING_FOLDER_ID_HERE';
const GITHUB_OWNER = 'YOUR_GITHUB_USERNAME';
const GITHUB_REPO  = 'YOUR_REPO_NAME';
// ----------------------------------------

/** Time-driven trigger target: scan Incoming and dispatch any new images. */
function checkIncoming() {
  const props = PropertiesService.getScriptProperties();
  const seen = JSON.parse(props.getProperty('seen') || '{}');
  const now = Date.now();

  const folder = DriveApp.getFolderById(INCOMING_FOLDER_ID);
  const files = folder.getFiles();
  while (files.hasNext()) {
    const f = files.next();
    if (f.getMimeType().indexOf('image/') !== 0) continue;   // images only
    const id = f.getId();
    if (seen[id]) continue;                                  // already dispatched
    try {
      dispatch(id, f.getName());
      seen[id] = now;
    } catch (e) {
      console.error('dispatch failed for ' + f.getName() + ': ' + e);
    }
  }

  // forget entries older than 24h so a failed/re-uploaded file can retry later
  for (const k in seen) if (now - seen[k] > 86400000) delete seen[k];
  props.setProperty('seen', JSON.stringify(seen));
}

/** Fire a GitHub repository_dispatch event for one Drive file. */
function dispatch(fileId, fileName) {
  const token = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
  if (!token) throw new Error('Missing GITHUB_TOKEN script property.');
  const url = 'https://api.github.com/repos/' + GITHUB_OWNER + '/' + GITHUB_REPO + '/dispatches';
  const resp = UrlFetchApp.fetch(url, {
    method: 'post',
    contentType: 'application/json',
    headers: { Authorization: 'Bearer ' + token, Accept: 'application/vnd.github+json' },
    payload: JSON.stringify({
      event_type: 'drive-upload',
      client_payload: { file_id: fileId, file_name: fileName }
    }),
    muteHttpExceptions: true
  });
  const code = resp.getResponseCode();
  console.log('dispatch ' + fileName + ' -> HTTP ' + code);
  if (code >= 300) throw new Error('GitHub API ' + code + ': ' + resp.getContentText());
}

/** Run once to start the every-minute watcher. */
function installTrigger() {
  removeTriggers();
  ScriptApp.newTrigger('checkIncoming').timeBased().everyMinutes(1).create();
  console.log('Watcher installed: checking Incoming every minute.');
}

/** Run once to stop the watcher. */
function removeTriggers() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'checkIncoming') ScriptApp.deleteTrigger(t);
  });
  console.log('Watcher stopped.');
}
