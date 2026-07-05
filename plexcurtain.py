#!/usr/bin/env python3
"""plexcurtain — atomically hide/restore Plex libraries by moving their DB records to an attic.

Hide:    stop Plex, back up the library DB, move every row belonging to the
         configured sections (plus their on-disk artwork bundles) into
         ~/Library/Application Support/Plexcurtain/attic.db, restart Plex.
Restore: the exact inverse. Nothing is rescanned, so all metadata survives:
         watch states, custom posters, edits, added-dates, collections.

Media files are never touched.

Usage: plexcurtain.py hide|restore|toggle|apply|status|list-sections [--force] [--swiftbar]
       plexcurtain.py check|uncheck <library name>
       (--force skips the active-streams check)

The selected set (config.json "sections") is only consulted by hide. Restore
always brings back everything in the attic, so selection edits made while
hidden can never orphan records; 'apply' reconciles a changed selection by
restoring everything and re-hiding the new set.
"""

import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------- paths/config

PMS_APP = "/Applications/Plex Media Server.app"
PLEX_SQLITE = f"{PMS_APP}/Contents/MacOS/Plex SQLite"
PMS_SUPPORT = Path(
    os.environ.get(
        "PLEXCURTAIN_PMS_DIR",
        Path.home() / "Library/Application Support/Plex Media Server",
    )
)
DB = PMS_SUPPORT / "Plug-in Support/Databases/com.plexapp.plugins.library.db"

DATA = Path(
    os.environ.get("PLEXCURTAIN_DATA_DIR", Path.home() / "Library/Application Support/Plexcurtain")
)
ATTIC = DATA / "attic.db"
BACKUPS = DATA / "backups"
BUNDLES = DATA / "bundles"
LOCK = DATA / "lock"
CONFIG = DATA / "config.json"

DEFAULT_CONFIG = {"sections": [], "keep_backups": 3}

# Tables moved wholesale, with the WHERE clause selecting a hidden section's rows.
# Temp tables mv_sections/mv_items/mv_media/mv_parts/mv_streams/mv_clusters are
# built first (see hide()).
MOVED_TABLES = [
    ("library_sections", "id IN (SELECT id FROM mv_sections)"),
    ("section_locations", "library_section_id IN (SELECT id FROM mv_sections)"),
    ("directories", "library_section_id IN (SELECT id FROM mv_sections)"),
    ("metadata_items", "id IN (SELECT id FROM mv_items)"),
    ("media_items", "id IN (SELECT id FROM mv_media)"),
    ("media_parts", "id IN (SELECT id FROM mv_parts)"),
    ("media_streams", "id IN (SELECT id FROM mv_streams)"),
    ("taggings", "metadata_item_id IN (SELECT id FROM mv_items)"),
    (
        "metadata_relations",
        "metadata_item_id IN (SELECT id FROM mv_items)"
        " OR related_metadata_item_id IN (SELECT id FROM mv_items)",
    ),
    ("play_queue_generators", "metadata_item_id IN (SELECT id FROM mv_items)"),
    ("media_item_settings", "media_item_id IN (SELECT id FROM mv_media)"),
    ("media_part_settings", "media_part_id IN (SELECT id FROM mv_parts)"),
    ("media_stream_settings", "media_stream_id IN (SELECT id FROM mv_streams)"),
    ("metadata_item_accounts", "metadata_item_id IN (SELECT id FROM mv_items)"),
    ("versioned_metadata_items", "metadata_item_id IN (SELECT id FROM mv_items)"),
    ("metadata_item_views", "library_section_id IN (SELECT id FROM mv_sections)"),
    ("external_metadata_items", "library_section_id IN (SELECT id FROM mv_sections)"),
    ("library_section_permissions", "library_section_id IN (SELECT id FROM mv_sections)"),
    ("media_subscriptions", "target_library_section_id IN (SELECT id FROM mv_sections)"),
    ("metadata_item_clusters", "library_section_id IN (SELECT id FROM mv_sections)"),
    ("metadata_item_clusterings", "metadata_item_cluster_id IN (SELECT id FROM mv_clusters)"),
]

# Ephemeral rows deleted on hide and not restored (Plex regenerates play queues).
DELETED_TABLES = [
    ("play_queue_items", "metadata_item_id IN (SELECT id FROM mv_items)"),
]


def die(msg, code=1):
    print(f"plexcurtain: {msg}", file=sys.stderr)
    sys.exit(code)


def load_config():
    if CONFIG.exists():
        return {**DEFAULT_CONFIG, **json.loads(CONFIG.read_text())}
    DATA.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n")
    return dict(DEFAULT_CONFIG)


def save_sections(sections):
    """Update only the section list, preserving any other config keys."""
    raw = json.loads(CONFIG.read_text()) if CONFIG.exists() else dict(DEFAULT_CONFIG)
    raw["sections"] = sorted(set(sections))
    DATA.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(raw, indent=2) + "\n")


# ---------------------------------------------------------------- sqlite

def sql(db, script, readonly=False):
    """Run a SQL script through Plex's own sqlite (required: custom FTS tokenizer)."""
    uri = f"file:{db}?mode=ro" if readonly else str(db)
    proc = subprocess.run(
        [PLEX_SQLITE, uri],
        input=".bail on\n" + script,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"sqlite failed: {proc.stderr.strip()}\n--- script was:\n{script[:2000]}")
    return proc.stdout


def sql_rows(db, query):
    out = sql(db, f".mode list\n.separator |\n{query}\n", readonly=True)
    return [line.split("|") for line in out.splitlines() if line]


def table_columns(db, table, schema="main"):
    return [r[0] for r in sql_rows(db, f"SELECT name FROM {schema}.pragma_table_info('{table}');")]


# ---------------------------------------------------------------- plex process

NO_SERVER = bool(os.environ.get("PLEXCURTAIN_NO_SERVER"))  # test mode: never touch the PMS process


def pms_running():
    if NO_SERVER:
        return False
    return (
        subprocess.run(["pgrep", "-x", "Plex Media Server"], capture_output=True).returncode == 0
    )


def plex_token():
    plist = Path.home() / "Library/Preferences/com.plexapp.plexmediaserver.plist"
    try:
        return plistlib.loads(plist.read_bytes()).get("PlexOnlineToken")
    except Exception:
        return None


def active_sessions():
    token = plex_token()
    if not token:
        return 0
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:32400/status/sessions?X-Plex-Token={token}"
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode()
        for part in body.split():
            if part.startswith('size="'):
                return int(part.split('"')[1])
    except Exception:
        return 0
    return 0


def stop_pms():
    if NO_SERVER or not pms_running():
        return False
    subprocess.run(
        ["osascript", "-e", 'tell application "Plex Media Server" to quit'],
        capture_output=True,
    )
    for _ in range(120):
        if not pms_running():
            time.sleep(2)  # let file handles/WAL settle
            return True
        time.sleep(0.5)
    die("Plex Media Server did not quit within 60s; aborting (nothing was changed)")


def start_pms():
    subprocess.run(["open", "-g", "-a", "Plex Media Server"], capture_output=True)


# ---------------------------------------------------------------- backups

def backup_db():
    BACKUPS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = BACKUPS / f"library-{stamp}.db"
    shutil.copy2(DB, dest)
    for suffix in ("-wal", "-shm"):
        side = Path(str(DB) + suffix)
        if side.exists() and side.stat().st_size > 0:
            shutil.copy2(side, BACKUPS / f"library-{stamp}.db{suffix}")
    return dest


def trim_backups(keep):
    backups = sorted(BACKUPS.glob("library-*.db"))
    for old in backups[:-keep] if keep else []:
        for f in BACKUPS.glob(old.name + "*"):
            f.unlink()


# ---------------------------------------------------------------- state

def hidden_sections():
    """Sections currently in the attic: [(id, name, hidden_at)]."""
    if not ATTIC.exists():
        return []
    try:
        return sql_rows(ATTIC, "SELECT id, name, hidden_at FROM plexcurtain_sections;")
    except RuntimeError:
        return []


def server_sections():
    """Names of sections the running server reports; falls back to a read-only
    DB query when the server is down. None if neither works."""
    token = plex_token()
    if token and not NO_SERVER:
        try:
            import xml.etree.ElementTree as ET

            req = urllib.request.Request(
                f"http://127.0.0.1:32400/library/sections?X-Plex-Token={token}"
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                root = ET.fromstring(resp.read())
            return [d.get("title") for d in root.iter("Directory")]
        except Exception:
            pass
    try:
        return [r[0] for r in sql_rows(DB, "SELECT name FROM library_sections ORDER BY name;")]
    except RuntimeError:
        return None


def selection_drift(cfg):
    """(to_hide, to_show): how the desired set differs from what is actually
    hidden. Only meaningful while hidden; names absent from both the server
    and the attic are ignored (nothing to act on)."""
    hidden = {name for _, name, _ in hidden_sections()}
    if not hidden:
        return set(), set()
    available = set(server_sections() or []) | hidden
    want = {s for s in cfg["sections"] if s in available}
    return want - hidden, hidden - want


# ---------------------------------------------------------------- hide

def hide(cfg, force=False):
    if hidden_sections():
        print("already hidden (use 'apply' to change the hidden set)")
        return

    if not cfg["sections"]:
        die("no libraries selected to hide (see 'list-sections' / the menubar checkboxes)")

    if pms_running() and not force:
        n = active_sessions()
        if n:
            die(f"{n} active stream(s) on the server; retry with --force to interrupt them")

    names = cfg["sections"]
    quoted = ",".join("'" + n.replace("'", "''") + "'" for n in names)

    was_running = stop_pms()
    backup = backup_db()
    print(f"backup: {backup}")

    found = sql_rows(DB, f"SELECT id, name FROM library_sections WHERE name IN ({quoted});")
    if not found:
        if was_running:
            start_pms()
        die(f"none of the configured sections exist: {names}")
    missing = set(names) - {name for _, name in found}
    if missing:
        print(f"warning: not present on server, skipping: {sorted(missing)}")
    ids = ",".join(sid for sid, _ in found)

    DATA.mkdir(parents=True, exist_ok=True)

    setup = f"""
CREATE TEMP TABLE mv_sections AS SELECT id FROM library_sections WHERE id IN ({ids});
CREATE TEMP TABLE mv_items AS SELECT id FROM metadata_items WHERE library_section_id IN ({ids});
CREATE TEMP TABLE mv_media AS SELECT id FROM media_items WHERE library_section_id IN ({ids});
CREATE TEMP TABLE mv_parts AS SELECT id FROM media_parts WHERE media_item_id IN (SELECT id FROM mv_media);
CREATE TEMP TABLE mv_streams AS SELECT id FROM media_streams WHERE media_item_id IN (SELECT id FROM mv_media);
CREATE TEMP TABLE mv_clusters AS SELECT id FROM metadata_item_clusters WHERE library_section_id IN ({ids});
"""

    parts = [f"ATTACH DATABASE '{ATTIC}' AS attic;", setup, "BEGIN IMMEDIATE;"]

    parts.append(
        "CREATE TABLE IF NOT EXISTS attic.plexcurtain_sections (id INTEGER, name TEXT, hidden_at TEXT);\n"
        "CREATE TABLE IF NOT EXISTS attic.plexcurtain_schema (table_name TEXT, create_sql TEXT);\n"
        "CREATE TABLE IF NOT EXISTS attic.plexcurtain_assets (kind TEXT, key TEXT);\n"
        "CREATE TABLE IF NOT EXISTS attic.plexcurtain_tags AS SELECT * FROM main.tags WHERE 0;\n"
    )

    for table, cond in MOVED_TABLES:
        parts.append(
            f"CREATE TABLE IF NOT EXISTS attic.{table} AS SELECT * FROM main.{table} WHERE 0;\n"
            f"INSERT INTO attic.{table} SELECT * FROM main.{table} WHERE {cond};\n"
        )
    # snapshot of tags referenced by moved taggings, in case Plex GCs them meanwhile
    parts.append(
        "INSERT INTO attic.plexcurtain_tags SELECT * FROM main.tags WHERE id IN "
        "(SELECT tag_id FROM attic.taggings);\n"
    )
    # asset manifests for the on-disk bundle moves
    parts.append(
        "INSERT INTO attic.plexcurtain_assets SELECT 'media_hash', hash FROM main.media_parts "
        "WHERE id IN (SELECT id FROM mv_parts) AND hash != '';\n"
        "INSERT INTO attic.plexcurtain_assets SELECT 'guid', guid FROM main.metadata_items "
        "WHERE id IN (SELECT id FROM mv_items) AND guid != '';\n"
    )
    # record schema so restore can detect a PMS upgrade that migrated tables
    for table, _ in MOVED_TABLES:
        parts.append(
            "INSERT INTO attic.plexcurtain_schema SELECT name, sql FROM main.sqlite_master "
            f"WHERE name = '{table}';\n"
        )

    # deletes come last: children first is irrelevant (no FK enforcement), but
    # FTS triggers on metadata_items fire here, keeping the search index clean
    for table, cond in DELETED_TABLES:
        parts.append(f"DELETE FROM main.{table} WHERE {cond};\n")
    for table, cond in MOVED_TABLES:
        parts.append(f"DELETE FROM main.{table} WHERE {cond};\n")

    now = datetime.now().isoformat(timespec="seconds")
    for sid, name in found:
        safe = name.replace("'", "''")
        parts.append(
            f"INSERT INTO attic.plexcurtain_sections VALUES ({sid}, '{safe}', '{now}');\n"
        )
    parts.append("COMMIT;\n")

    try:
        sql(DB, "".join(parts))
    except RuntimeError as e:
        print(f"SQL failed, restoring backup: {e}", file=sys.stderr)
        shutil.copy2(backup, DB)
        if was_running:
            start_pms()
        die("hide failed; database restored from backup, nothing hidden")

    moved, missing_bundles = move_bundles_out()
    if was_running:
        start_pms()
    trim_backups(cfg["keep_backups"])
    print(
        f"hidden: {', '.join(name for _, name in found)} "
        f"({len(moved)} artwork bundles atticked, {missing_bundles} had none)"
    )


# ---------------------------------------------------------------- bundles

def bundle_paths_for(kind, key):
    """Yield existing on-disk bundle dirs for a manifest entry."""
    import hashlib

    if kind == "media_hash":
        p = PMS_SUPPORT / "Media/localhost" / key[0] / (key[1:] + ".bundle")
        if p.is_dir():
            yield p
    elif kind == "guid":
        h = hashlib.sha1(key.encode()).hexdigest()
        meta = PMS_SUPPORT / "Metadata"
        if meta.is_dir():
            for typedir in meta.iterdir():
                p = typedir / h[0] / (h[1:] + ".bundle")
                if p.is_dir():
                    yield p


def move_bundles_out():
    assets = sql_rows(ATTIC, "SELECT kind, key FROM plexcurtain_assets;")
    moved, missing = [], 0
    for kind, key in assets:
        found_any = False
        for src in bundle_paths_for(kind, key):
            rel = src.relative_to(PMS_SUPPORT)
            dest = BUNDLES / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not dest.exists():
                shutil.move(str(src), str(dest))
            moved.append(str(rel))
            found_any = True
        if not found_any:
            missing += 1
    return moved, missing


def move_bundles_back():
    if not BUNDLES.is_dir():
        return 0
    n = 0
    for src in sorted(BUNDLES.rglob("*.bundle")):
        if not src.is_dir():
            continue
        dest = PMS_SUPPORT / src.relative_to(BUNDLES)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():  # Plex regenerated it meanwhile; keep the attic copy's content
            shutil.rmtree(str(dest))
        shutil.move(str(src), str(dest))
        n += 1
    # clear now-empty tree
    shutil.rmtree(BUNDLES, ignore_errors=True)
    return n


# ---------------------------------------------------------------- restore

def restore(cfg, force=False):
    hidden = hidden_sections()
    if not hidden:
        print("nothing hidden")
        return

    if pms_running() and not force:
        n = active_sessions()
        if n:
            die(f"{n} active stream(s) on the server; retry with --force to interrupt them")

    was_running = stop_pms()
    backup = backup_db()
    print(f"backup: {backup}")

    # If PMS upgraded and migrated a table while rows were atticked, adding a
    # NOT NULL column without default, the insert below fails and rolls back.
    # Column-intersection inserts absorb the common case (added nullable cols).
    parts = [f"ATTACH DATABASE '{ATTIC}' AS attic;", "BEGIN IMMEDIATE;"]
    for table, _ in MOVED_TABLES:
        main_cols = set(table_columns(DB, table))
        attic_cols = set(table_columns(ATTIC, table))
        cols = ", ".join(
            f'"{c}"' for c in table_columns(ATTIC, table) if c in main_cols
        )
        dropped = attic_cols - main_cols
        if dropped:
            print(f"warning: {table}: columns gone after PMS upgrade, dropping: {sorted(dropped)}")
        parts.append(f"INSERT INTO main.{table} ({cols}) SELECT {cols} FROM attic.{table};\n")
    # resurrect any tags Plex garbage-collected while hidden (ids never reused: AUTOINCREMENT)
    parts.append(
        "INSERT INTO main.tags SELECT * FROM attic.plexcurtain_tags "
        "WHERE id NOT IN (SELECT id FROM main.tags);\n"
    )
    for table, _ in MOVED_TABLES:
        parts.append(f"DELETE FROM attic.{table};\n")
    parts.append(
        "DELETE FROM attic.plexcurtain_sections;\n"
        "DELETE FROM attic.plexcurtain_schema;\n"
        "DELETE FROM attic.plexcurtain_assets;\n"
        "DELETE FROM attic.plexcurtain_tags;\n"
    )
    parts.append("COMMIT;\nVACUUM attic;\n")

    try:
        sql(DB, "".join(parts))
    except RuntimeError as e:
        print(f"SQL failed, restoring backup: {e}", file=sys.stderr)
        shutil.copy2(backup, DB)
        if was_running:
            start_pms()
        die("restore failed; database rolled back to backup, attic left intact")

    n = move_bundles_back()
    if was_running:
        start_pms()
    trim_backups(cfg["keep_backups"])
    print(
        f"restored: {', '.join(name for _, name, _ in hidden)} ({n} artwork bundles back)"
    )


# ---------------------------------------------------------------- selection

def list_sections(cfg):
    """TSV: name <TAB> checked(0/1) <TAB> visible|hidden|missing."""
    hidden = {name for _, name, _ in hidden_sections()}
    on_server = server_sections()
    checked = set(cfg["sections"])
    names = sorted(set(on_server or []) | hidden | checked)
    for name in names:
        if name in hidden:
            state = "hidden"
        elif on_server is not None and name not in on_server:
            state = "missing"
        else:
            state = "visible"
        print(f"{name}\t{1 if name in checked else 0}\t{state}")


def set_checked(cfg, name, checked):
    if not name:
        die("missing library name")
    sections = set(cfg["sections"])
    (sections.add if checked else sections.discard)(name)
    save_sections(sections)
    print(f"{'checked' if checked else 'unchecked'}: {name}")
    if hidden_sections():
        print("note: libraries are currently hidden; run 'apply' to make this take effect now")


def apply_selection(cfg, force=False):
    """Reconcile the hidden set with the current selection, safely: restore
    everything from the attic first, then hide the newly desired set."""
    if not hidden_sections():
        print("nothing hidden; the selection takes effect on the next hide")
        return
    to_hide, to_show = selection_drift(cfg)
    if not to_hide and not to_show:
        print("hidden set already matches the selection")
        return
    restore(cfg, force)
    # re-read: restore just emptied the attic, and the config may name
    # sections that only now exist on the server again
    if cfg["sections"]:
        hide(cfg, force)


# ---------------------------------------------------------------- status / cli

def status(swiftbar=False):
    hidden = hidden_sections()
    if swiftbar:
        print("HIDDEN" if hidden else "VISIBLE")
        for _, name, at in hidden:
            print(f"{name}\t{at}")
        return
    if hidden:
        for _, name, at in hidden:
            print(f"hidden: {name} (since {at})")
    else:
        print("all libraries visible")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    force = "--force" in sys.argv
    swiftbar = "--swiftbar" in sys.argv
    cmd = args[0] if args else "status"

    if not Path(PLEX_SQLITE).exists():
        die(f"Plex SQLite not found at {PLEX_SQLITE}")
    if not DB.exists():
        die(f"Plex database not found at {DB}")

    cfg = load_config()
    DATA.mkdir(parents=True, exist_ok=True)

    # crude single-instance lock
    import fcntl

    lock_fh = open(LOCK, "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        die("another plexcurtain operation is already running")

    if cmd == "hide":
        hide(cfg, force)
    elif cmd == "restore":
        restore(cfg, force)
    elif cmd == "toggle":
        (restore if hidden_sections() else hide)(cfg, force)
    elif cmd == "apply":
        apply_selection(cfg, force)
    elif cmd == "status":
        status(swiftbar)
    elif cmd == "list-sections":
        list_sections(cfg)
    elif cmd == "check":
        set_checked(cfg, args[1] if len(args) > 1 else "", True)
    elif cmd == "uncheck":
        set_checked(cfg, args[1] if len(args) > 1 else "", False)
    else:
        die(
            f"unknown command {cmd!r} "
            "(expected hide|restore|toggle|apply|status|list-sections|check|uncheck)"
        )


if __name__ == "__main__":
    main()
