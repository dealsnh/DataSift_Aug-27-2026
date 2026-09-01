"""
Automatic Google Drive upload for pipeline output files.

Default-ON wrapper around `drive_uploader.upload_file`. The point is that nobody
has to remember a flag: any script that finishes writing a CSV calls
`upload_outputs(...)` and the file lands in Drive if Drive is configured.

Three deliberate choices:

  * BEST EFFORT, NEVER FATAL. A Drive outage must not lose a pull that cost real
    money in Enformion matches and scrape time. Every failure is caught, reported
    on stdout, and returned in the result so the caller can still say what happened.
  * IT SAYS WHY IT SKIPPED. Silent no-ops are how the notification bug in this repo
    survived 19 days. An unconfigured Drive prints the exact missing variable rather
    than nothing at all.
  * BLANK-SAFE ENV READS. `os.getenv(key, default)` returns "" for a key that is
    present-but-empty in .env, which defeats the default. Same trap that took down
    every `python src/main.py` command via a blank DROPBOX_POLL_INTERVAL.
"""
from __future__ import annotations

import os
from pathlib import Path

FOLDER_ID_VAR = "GOOGLE_DRIVE_FOLDER_ID"
KEY_VAR = "GOOGLE_SERVICE_ACCOUNT_KEY"


def _env(key: str) -> str:
    """Blank-safe read: a key present with an empty value is treated as unset."""
    return (os.getenv(key) or "").strip()


def is_configured() -> tuple[bool, str]:
    """(configured, human-readable reason if not)."""
    missing = [v for v in (FOLDER_ID_VAR, KEY_VAR) if not _env(v)]
    if missing:
        return False, f"{' and '.join(missing)} not set in .env"
    return True, ""


def upload_outputs(paths, subfolder_note: str = "", verbose: bool = True,
                   csv_only: bool = True) -> dict:
    """Upload each path to the configured Drive folder.

    CSV ONLY by default (Ty, 2026-09-02): the JSON siblings are the machine-readable
    working copies and just clutter the Drive folder. Callers still pass both paths
    so the filter lives in one place; pass csv_only=False to send everything.

    Returns {"uploaded": [(name, link)], "failed": [(name, error)], "skipped": reason}.
    Never raises.
    """
    result: dict = {"uploaded": [], "failed": [], "skipped": ""}

    ok, why = is_configured()
    if not ok:
        result["skipped"] = why
        if verbose:
            print(f"\n  Google Drive upload skipped: {why}")
            print(f"  (set {FOLDER_ID_VAR} and {KEY_VAR} in .env to enable)")
        return result

    try:
        from drive_uploader import upload_file
    except ImportError as e:            # pragma: no cover - dependency guard
        result["skipped"] = f"drive_uploader unavailable: {e}"
        if verbose:
            print(f"\n  Google Drive upload skipped: {result['skipped']}")
        return result

    folder_id = _env(FOLDER_ID_VAR)
    key_b64 = _env(KEY_VAR)

    real = [Path(p) for p in paths if p and Path(p).exists()]
    if csv_only:
        skipped_types = [p.name for p in real if p.suffix.lower() != ".csv"]
        real = [p for p in real if p.suffix.lower() == ".csv"]
        if skipped_types and verbose:
            print(f"  (not uploading {', '.join(skipped_types)} - CSV only)")
    if not real:
        result["skipped"] = "no output files to upload"
        if verbose:
            print(f"\n  Google Drive upload skipped: {result['skipped']}")
        return result

    if verbose:
        print(f"\n  uploading {len(real)} file(s) to Google Drive"
              + (f" [{subfolder_note}]" if subfolder_note else ""))
    for p in real:
        try:
            link = upload_file(p, folder_id, key_b64)
            if link:
                result["uploaded"].append((p.name, link))
                if verbose:
                    print(f"    OK   {p.name}  ->  {link}")
            else:
                result["failed"].append((p.name, "uploader returned no link"))
                if verbose:
                    print(f"    FAIL {p.name}  (uploader returned no link)")
        except Exception as e:                      # noqa: BLE001 - best effort
            result["failed"].append((p.name, str(e)[:200]))
            if verbose:
                print(f"    FAIL {p.name}  {str(e)[:160]}")
    return result


def doctor() -> int:
    """`python src/drive_autoupload.py` -- verify credentials without uploading."""
    ok, why = is_configured()
    print("Google Drive auto-upload wiring check")
    print(f"  {FOLDER_ID_VAR:30} {'set' if _env(FOLDER_ID_VAR) else 'MISSING'}")
    print(f"  {KEY_VAR:30} {'set' if _env(KEY_VAR) else 'MISSING'}")
    if not ok:
        print(f"\n  NOT CONFIGURED: {why}")
        return 2

    try:
        import base64
        import json as _json
        from drive_uploader import _build_service
        info = _json.loads(base64.b64decode(_env(KEY_VAR)))
        print(f"  service account               {info.get('client_email', '?')}")
        svc = _build_service(_env(KEY_VAR))
        meta = svc.files().get(fileId=_env(FOLDER_ID_VAR),
                               fields="id,name,mimeType",
                               supportsAllDrives=True).execute()
        print(f"  target folder                 {meta.get('name')!r} ({meta.get('id')})")
        print("\n  OK: credentials valid and the folder is reachable.")
        return 0
    except Exception as e:                          # noqa: BLE001
        print(f"\n  FAILED: {str(e)[:400]}")
        print("\n  Most common cause: the Drive folder has not been SHARED with the")
        print("  service account email above (give it Editor). The key can be perfectly")
        print("  valid and this still fails.")
        return 1


if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    raise SystemExit(doctor())
