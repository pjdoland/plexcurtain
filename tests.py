#!/usr/bin/env python3
"""Test suite for plexcurtain.

Strategy: build a synthetic fixture by copying the local Plex library database
and cloning a handful of existing rows into fake sections ("Curtain Test
Alpha"/"Beta" with fake items, media, streams, taggings, and artwork bundles).
Cloning real rows — with names/guids/hashes overridden — means the fixture
always satisfies the live schema's constraints, even across Plex upgrades,
and no real library data is referenced by this file.

All tests run against temp copies with PLEXCURTAIN_NO_SERVER=1; the real
server, database, and data directory are never touched.

Run: python3 tests.py [-v]
Skips cleanly if Plex Media Server or its database are not present.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent
TOOL = REPO / "plexcurtain.py"
PLEX_SQLITE = Path("/Applications/Plex Media Server.app/Contents/MacOS/Plex SQLite")
LIVE_DB = (
    Path.home()
    / "Library/Application Support/Plex Media Server/Plug-in Support/Databases/com.plexapp.plugins.library.db"
)

ALPHA = "Curtain Test Alpha"
BETA = "Curtain Test Beta"

SNAPSHOT_TABLES = [
    "library_sections", "section_locations", "directories", "metadata_items",
    "media_items", "media_parts", "media_streams", "taggings",
    "metadata_relations", "play_queue_generators", "media_item_settings",
    "media_part_settings", "media_stream_settings", "metadata_item_accounts",
    "versioned_metadata_items", "metadata_item_views", "external_metadata_items",
    "library_section_permissions", "media_subscriptions",
    "metadata_item_clusters", "metadata_item_clusterings", "tags",
    "metadata_item_settings",
]


def psql(db, script):
    proc = subprocess.run(
        [str(PLEX_SQLITE), str(db)],
        input=".bail on\n.mode list\n.separator |\n" + script,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"sqlite failed: {proc.stderr.strip()}")
    return proc.stdout


def rows(db, query):
    return [line.split("|") for line in psql(db, query + "\n").splitlines() if line]


def one(db, query):
    r = rows(db, query)
    return r[0][0] if r else None


def cols(db, table):
    return [r[0] for r in rows(db, f"SELECT name FROM pragma_table_info('{table}');")]


def clone_rows(db, table, where, overrides):
    """INSERT INTO table a copy of the rows matching `where`, with `overrides`
    (column -> SQL expression) replacing selected columns. `id` is omitted so
    AUTOINCREMENT assigns fresh ids. Adapts to whatever schema is live."""
    names = [c for c in cols(db, table) if c != "id"]
    collist = ", ".join(f'"{c}"' for c in names)
    exprs = ", ".join(overrides.get(c, f'"{c}"') for c in names)
    # single invocation: last_insert_rowid() is per-connection
    out = psql(
        db,
        f"INSERT INTO {table} ({collist}) SELECT {exprs} FROM {table} WHERE {where};\n"
        "SELECT last_insert_rowid();",
    )
    return out.strip().splitlines()[-1]


def fake_hash(seed):
    return hashlib.sha1(f"plexcurtain-test-{seed}".encode()).hexdigest()


def build_fixture(db):
    """Add two fake sections with cloned content; return their media hashes."""
    hashes = []
    tag_id = clone_rows(db, "tags", "id = (SELECT min(id) FROM tags)",
                        {"tag": "'Curtain Test Tag'"})
    for name in (ALPHA, BETA):
        sec = clone_rows(
            db, "library_sections",
            "id = (SELECT min(id) FROM library_sections WHERE section_type = 1)",
            {"name": f"'{name}'", "uuid": f"'{fake_hash(name)}'"},
        )
        clone_rows(
            db, "section_locations",
            "id = (SELECT min(id) FROM section_locations)",
            {"library_section_id": str(sec),
             "root_path": f"'/nonexistent/{fake_hash(name)[:8]}'"},
        )
        clone_rows(
            db, "directories",
            "id = (SELECT min(id) FROM directories WHERE library_section_id IS NOT NULL)",
            {"library_section_id": str(sec),
             "path": f"'/nonexistent/{fake_hash(name)[:8]}'"},
        )
        # two movies per section, cloned from any existing movie item
        src_item = "id = (SELECT min(id) FROM metadata_items WHERE metadata_type = 1)"
        for n in range(2):
            seed = f"{name}-{n}"
            item = clone_rows(db, "metadata_items", src_item, {
                "library_section_id": str(sec),
                "guid": f"'local://curtain-test-{fake_hash(seed)[:12]}'",
                "title": f"'Curtain Test Item {seed}'",
                "title_sort": f"'curtain test {seed}'",
                "hash": f"'{fake_hash('item-' + seed)}'",
            })
            media = clone_rows(
                db, "media_items",
                "id = (SELECT min(id) FROM media_items)",
                {"metadata_item_id": str(item), "library_section_id": str(sec)},
            )
            h = fake_hash("part-" + seed)
            hashes.append(h)
            part = clone_rows(
                db, "media_parts",
                "id = (SELECT min(id) FROM media_parts)",
                {"media_item_id": str(media), "hash": f"'{h}'",
                 "file": f"'/nonexistent/{fake_hash(seed)[:8]}.mkv'"},
            )
            clone_rows(
                db, "media_streams",
                "id = (SELECT min(id) FROM media_streams)",
                {"media_item_id": str(media), "media_part_id": str(part)},
            )
            psql(db, f"INSERT INTO taggings (metadata_item_id, tag_id, \"index\", created_at) "
                     f"VALUES ({item}, {tag_id}, 0, datetime('now'));")
            # guid-keyed watch state; must survive hide untouched
            clone_rows(
                db, "metadata_item_settings",
                "id = (SELECT min(id) FROM metadata_item_settings)",
                {"guid": f"'local://curtain-test-{fake_hash(seed)[:12]}'",
                 "view_count": "7"},
            )
    return hashes


def snapshot(db):
    """Cheap content signature of every affected table plus deep checksums."""
    sig = {}
    for t in SNAPSHOT_TABLES:
        sig[t] = one(db, f"SELECT count(*) || ':' || coalesce(sum(id), 0) FROM {t};")
    for t in ("metadata_items", "media_parts", "taggings"):
        out = psql(db, f"SELECT * FROM {t} ORDER BY id;")
        sig[t + "_md5"] = hashlib.md5(out.encode()).hexdigest()
    return sig


class PlexcurtainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not PLEX_SQLITE.exists():
            raise unittest.SkipTest("Plex SQLite binary not found")
        if not LIVE_DB.exists():
            raise unittest.SkipTest("Plex library database not found")
        cls.master = Path(tempfile.mkdtemp(prefix="plexcurtain-master-"))
        cls.master_db = cls.master / "master.db"
        shutil.copyfile(LIVE_DB, cls.master_db)
        cls.hashes = build_fixture(cls.master_db)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.master, ignore_errors=True)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="plexcurtain-test-"))
        self.pms = self.tmp / "pms"
        self.db = self.pms / "Plug-in Support/Databases/com.plexapp.plugins.library.db"
        self.db.parent.mkdir(parents=True)
        # APFS copy-on-write clone when available; falls back to a real copy
        if subprocess.run(["cp", "-c", str(self.master_db), str(self.db)],
                          capture_output=True).returncode != 0:
            shutil.copyfile(self.master_db, self.db)
        self.media = self.pms / "Media/localhost"
        for h in self.hashes:
            bundle = self.media / h[0] / (h[1:] + ".bundle/Contents/Thumbnails")
            bundle.mkdir(parents=True)
            (bundle / "thumb1.jpg").write_bytes(b"fake-jpeg-" + h.encode())
        (self.pms / "Metadata/Movies").mkdir(parents=True)
        self.data = self.tmp / "data"
        self.data.mkdir()
        self.write_config([ALPHA, BETA])
        self.env = {
            **os.environ,
            "PLEXCURTAIN_PMS_DIR": str(self.pms),
            "PLEXCURTAIN_DATA_DIR": str(self.data),
            "PLEXCURTAIN_NO_SERVER": "1",
        }

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_config(self, sections, **extra):
        cfg = {"sections": sections, "keep_backups": 2, **extra}
        (self.data / "config.json").write_text(json.dumps(cfg))

    def run_tool(self, *args, ok=True):
        proc = subprocess.run(
            [sys.executable, str(TOOL), *args],
            capture_output=True, text=True, env=self.env,
        )
        if ok:
            self.assertEqual(
                proc.returncode, 0,
                f"{args} failed:\n{proc.stdout}\n{proc.stderr}",
            )
        return proc

    def section_names(self):
        return {r[0] for r in rows(self.db, "SELECT name FROM library_sections;")}

    # ------------------------------------------------------------ core

    def test_round_trip_is_byte_identical(self):
        before = snapshot(self.db)
        self.run_tool("hide")
        self.assertNotIn(ALPHA, self.section_names())
        self.assertNotIn(BETA, self.section_names())
        self.run_tool("restore")
        self.assertEqual(before, snapshot(self.db))
        # attic must be empty again
        out = self.run_tool("status").stdout
        self.assertIn("all libraries visible", out)

    def test_hide_moves_only_target_sections(self):
        # a real Plex DB has legit section-less items (playlists, collections);
        # what must not change is the number of *orphans* — items pointing at a
        # section id that no longer exists
        orphans = ("SELECT count(*) FROM metadata_items mi WHERE mi.library_section_id IS NOT NULL "
                   "AND NOT EXISTS (SELECT 1 FROM library_sections ls WHERE ls.id = mi.library_section_id)")
        fake_items = (f"SELECT count(*) FROM metadata_items WHERE library_section_id IN "
                      f"(SELECT id FROM library_sections WHERE name IN ('{ALPHA}', '{BETA}'))")
        orphans_before = one(self.db, orphans)
        others_before = int(one(self.db, "SELECT count(*) FROM metadata_items")) - int(one(self.db, fake_items))
        self.assertEqual(one(self.db, fake_items), "4")
        self.run_tool("hide")
        self.assertEqual(one(self.db, orphans), orphans_before,
                         "hide must not orphan any items")
        self.assertEqual(str(others_before), one(self.db, "SELECT count(*) FROM metadata_items"),
                         "items outside the selected libraries must be untouched")

    def test_watch_states_stay_during_hide_and_relink(self):
        q = "SELECT count(*) FROM metadata_item_settings WHERE guid LIKE 'local://curtain-test-%' AND view_count = 7"
        self.assertEqual(one(self.db, q), "4")
        self.run_tool("hide")
        self.assertEqual(one(self.db, q), "4", "guid-keyed watch states must stay in place while hidden")
        self.run_tool("restore")
        self.assertEqual(one(self.db, q), "4")

    def test_rows_added_while_hidden_cause_no_collision(self):
        hidden_ids = set(
            r[0] for r in rows(self.db, f"SELECT mi.id FROM metadata_items mi JOIN library_sections ls "
                                        f"ON ls.id = mi.library_section_id WHERE ls.name IN ('{ALPHA}', '{BETA}')")
        )
        self.run_tool("hide")
        # simulate scanner activity while hidden: new items in a visible library
        new_ids = set()
        for n in range(3):
            new_ids.add(clone_rows(
                self.db, "metadata_items",
                "id = (SELECT min(id) FROM metadata_items WHERE metadata_type = 1)",
                {"guid": f"'local://curtain-new-{n}'", "title": f"'Curtain New {n}'"},
            ))
        self.assertFalse(new_ids & hidden_ids, "AUTOINCREMENT must never reuse hidden ids")
        self.run_tool("restore")  # would exit nonzero on any PK collision
        present = set(
            r[0] for r in rows(self.db, "SELECT id FROM metadata_items WHERE "
                                        "guid LIKE 'local://curtain-%'")
        )
        self.assertTrue((hidden_ids | new_ids) <= present, "restored and new rows must coexist")

    def test_bundles_move_to_attic_and_back(self):
        h = self.hashes[0]
        bundle = self.media / h[0] / (h[1:] + ".bundle")
        attic_bundle = self.data / "bundles/Media/localhost" / h[0] / (h[1:] + ".bundle")
        self.assertTrue(bundle.is_dir())
        self.run_tool("hide")
        self.assertFalse(bundle.exists(), "bundle must leave the Plex tree while hidden")
        self.assertTrue(attic_bundle.is_dir())
        self.run_tool("restore")
        self.assertTrue((bundle / "Contents/Thumbnails/thumb1.jpg").is_file())
        self.assertFalse((self.data / "bundles").exists())

    def test_gc_tag_resurrected_on_restore(self):
        tag_id = one(self.db, "SELECT id FROM tags WHERE tag = 'Curtain Test Tag'")
        self.run_tool("hide")
        psql(self.db, f"DELETE FROM tags WHERE id = {tag_id};")  # simulate Plex GC
        self.run_tool("restore")
        self.assertEqual(
            one(self.db, f"SELECT tag FROM tags WHERE id = {tag_id}"),
            "Curtain Test Tag",
        )

    # ------------------------------------------------------------ guards

    def test_empty_selection_refused(self):
        self.write_config([])
        proc = self.run_tool("hide", ok=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("no libraries selected", proc.stderr)

    def test_hide_when_already_hidden_is_noop(self):
        self.run_tool("hide")
        sig = snapshot(self.db)
        out = self.run_tool("hide").stdout
        self.assertIn("already hidden", out)
        self.assertEqual(sig, snapshot(self.db))

    def test_restore_with_nothing_hidden_is_noop(self):
        sig = snapshot(self.db)
        out = self.run_tool("restore").stdout
        self.assertIn("nothing hidden", out)
        self.assertEqual(sig, snapshot(self.db))

    def test_missing_section_warned_and_skipped(self):
        self.write_config([ALPHA, "No Such Library"])
        proc = self.run_tool("hide")
        self.assertIn("No Such Library", proc.stdout)
        self.assertNotIn(ALPHA, self.section_names())
        self.run_tool("restore")

    def test_sql_failure_leaves_db_intact(self):
        sig = snapshot(self.db)
        (self.data / "attic.db").mkdir()  # ATTACH will fail: path is a directory
        proc = self.run_tool("hide", ok=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(sig, snapshot(self.db), "failed hide must leave the DB untouched")

    # ------------------------------------------------------------ selection

    def test_check_uncheck_roundtrip_preserves_config(self):
        self.write_config([ALPHA], custom_key="kept")
        self.run_tool("check", BETA)
        cfg = json.loads((self.data / "config.json").read_text())
        self.assertEqual(cfg["sections"], sorted([ALPHA, BETA]))
        self.assertEqual(cfg["custom_key"], "kept")
        self.run_tool("uncheck", BETA)
        cfg = json.loads((self.data / "config.json").read_text())
        self.assertEqual(cfg["sections"], [ALPHA])

    def test_list_sections_states(self):
        self.write_config([ALPHA, "No Such Library"])
        self.run_tool("hide")
        state = {r[0]: (r[1], r[2]) for r in
                 (line.split("\t") for line in self.run_tool("list-sections").stdout.splitlines())}
        self.assertEqual(state[ALPHA], ("1", "hidden"))
        self.assertEqual(state[BETA][1], "visible")
        self.assertEqual(state["No Such Library"], ("1", "missing"))

    def test_apply_reconciles_selection_changed_while_hidden(self):
        self.run_tool("hide")
        self.run_tool("uncheck", BETA)
        self.run_tool("apply")
        names = self.section_names()
        self.assertIn(BETA, names, "unchecked section must be visible after apply")
        self.assertNotIn(ALPHA, names, "still-checked section must stay hidden after apply")
        self.run_tool("restore")
        self.assertIn(ALPHA, self.section_names())

    def test_apply_without_drift_is_noop(self):
        self.run_tool("hide")
        sig = snapshot(self.db)
        out = self.run_tool("apply").stdout
        self.assertIn("already matches", out)
        self.assertEqual(sig, snapshot(self.db))

    # ------------------------------------------------------------ upgrades

    def test_restore_survives_added_column(self):
        before = one(self.db, "SELECT count(*) FROM metadata_items")
        self.run_tool("hide")
        # simulate a PMS schema migration while rows are atticked
        psql(self.db, "ALTER TABLE metadata_items ADD COLUMN curtain_test_col INTEGER;")
        self.run_tool("restore")
        self.assertEqual(before, one(self.db, "SELECT count(*) FROM metadata_items"))


if __name__ == "__main__":
    unittest.main()
