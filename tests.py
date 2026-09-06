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
import xml.etree.ElementTree as ET
from pathlib import Path

import plexcurtain  # sibling module; sys.path[0] is this script's directory

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


def clone_media_chain(db, item, section_expr, seed, hashes):
    """Clone media_items -> media_parts -> media_streams for `item`.

    `section_expr` is raw SQL ("NULL" for section-less extras, else a section
    id). Appends the part hash to `hashes` so setUp builds it a bundle."""
    media = clone_rows(db, "media_items", "id = (SELECT min(id) FROM media_items)",
                       {"metadata_item_id": str(item), "library_section_id": section_expr})
    h = fake_hash("part-" + seed)
    hashes.append(h)
    part = clone_rows(db, "media_parts", "id = (SELECT min(id) FROM media_parts)",
                      {"media_item_id": str(media), "hash": f"'{h}'",
                       "file": f"'/nonexistent/{fake_hash(seed)[:8]}.mkv'"})
    clone_rows(db, "media_streams", "id = (SELECT min(id) FROM media_streams)",
               {"media_item_id": str(media), "media_part_id": str(part)})
    return h


def make_extra(db, seed, owners, hashes):
    """Clone a section-less extra (metadata_type 12) owned by `owners`.

    Mirrors how Plex stores featurettes and fetched trailers: no
    library_section_id, no parent_id, reachable only through
    metadata_relations. Returns (item_id, media_part_hash)."""
    item = clone_rows(db, "metadata_items",
                      "id = (SELECT min(id) FROM metadata_items WHERE metadata_type = 1)", {
                          "library_section_id": "NULL",
                          "parent_id": "NULL",
                          "metadata_type": "12",
                          "guid": f"'local://curtain-extra-{fake_hash(seed)[:12]}'",
                          "title": f"'Curtain Test Extra {seed}'",
                          "title_sort": f"'curtain test extra {seed}'",
                          "hash": f"'{fake_hash('extra-' + seed)}'",
                      })
    clone_media_chain(db, item, "NULL", "extra-" + seed, hashes)
    for owner in owners:
        psql(db, "INSERT INTO metadata_relations "
                 "(metadata_item_id, related_metadata_item_id, relation_type, created_at) "
                 f"VALUES ({owner}, {item}, 1, datetime('now'));")
    return item, hashes[-1]


def build_fixture(db):
    """Add two fake sections with cloned content plus section-less extras.

    Returns (media hashes needing bundles, extras info)."""
    hashes = []
    first_item = {}
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
            clone_media_chain(db, item, str(sec), seed, hashes)
            first_item.setdefault(name, item)
            psql(db, f"INSERT INTO taggings (metadata_item_id, tag_id, \"index\", created_at) "
                     f"VALUES ({item}, {tag_id}, 0, datetime('now'));")
            # guid-keyed watch state; must survive hide untouched
            clone_rows(
                db, "metadata_item_settings",
                "id = (SELECT min(id) FROM metadata_item_settings)",
                {"guid": f"'local://curtain-test-{fake_hash(seed)[:12]}'",
                 "view_count": "7"},
            )
    # an extra owned solely by a hidden section: must travel with it
    owned_id, owned_hash = make_extra(db, "owned", [first_item[ALPHA]], hashes)
    # an extra a permanently visible library also points at: must stay put
    outside = one(db, "SELECT min(mi.id) FROM metadata_items mi JOIN library_sections ls "
                      f"ON ls.id = mi.library_section_id WHERE ls.name NOT IN ('{ALPHA}', '{BETA}');")
    if not outside:
        raise unittest.SkipTest("library has no items outside the fixture sections")
    shared_id, shared_hash = make_extra(db, "shared", [first_item[BETA], outside], hashes)
    extras = {
        "owned_id": owned_id, "owned_hash": owned_hash,
        "shared_id": shared_id, "shared_hash": shared_hash,
    }
    return hashes, extras


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
        cls.hashes, cls.extras = build_fixture(cls.master_db)

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
            bundle = self.bundle_path(h) / "Contents/Thumbnails"
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

    def bundle_path(self, h):
        return self.media / h[0] / (h[1:] + ".bundle")

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

    def test_hidden_titles_leave_the_search_index(self):
        """The README claims hidden titles stop being searchable; prove it.

        Plex's FTS triggers fire on the DELETE from metadata_items, so the
        titles leave fts4_metadata_titles and come back on restore. This is
        what separates moving the rows from merely re-flagging a library."""
        ids = ",".join(r[0] for r in rows(
            self.db, "SELECT mi.id FROM metadata_items mi JOIN library_sections ls "
                     f"ON ls.id = mi.library_section_id WHERE ls.name IN ('{ALPHA}', '{BETA}');"))
        indexed = f"SELECT count(*) FROM fts4_metadata_titles WHERE docid IN ({ids});"
        before = one(self.db, indexed)
        self.assertNotEqual(before, "0", "fixture titles must start out indexed")
        self.run_tool("hide")
        self.assertEqual(one(self.db, indexed), "0",
                         "a hidden title must not stay in the search index")
        self.run_tool("restore")
        self.assertEqual(one(self.db, indexed), before,
                         "restore must put the titles back in the index")

    def test_hide_moves_only_target_sections(self):
        # a real Plex DB has legit section-less items (playlists, collections);
        # what must not change is the number of *orphans* — items pointing at a
        # section id that no longer exists
        orphans = ("SELECT count(*) FROM metadata_items mi WHERE mi.library_section_id IS NOT NULL "
                   "AND NOT EXISTS (SELECT 1 FROM library_sections ls WHERE ls.id = mi.library_section_id)")
        fake_items = (f"SELECT count(*) FROM metadata_items WHERE library_section_id IN "
                      f"(SELECT id FROM library_sections WHERE name IN ('{ALPHA}', '{BETA}'))")
        # the section-less extra owned only by ALPHA travels with it by design;
        # every other item outside the two fake sections must be untouched
        claimed = f"SELECT count(*) FROM metadata_items WHERE id = {self.extras['owned_id']}"
        orphans_before = one(self.db, orphans)
        others_before = (int(one(self.db, "SELECT count(*) FROM metadata_items"))
                         - int(one(self.db, fake_items))
                         - int(one(self.db, claimed)))
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
        bundle = self.bundle_path(h)
        attic_bundle = self.data / "bundles/Media/localhost" / h[0] / (h[1:] + ".bundle")
        self.assertTrue(bundle.is_dir())
        self.run_tool("hide")
        self.assertFalse(bundle.exists(), "bundle must leave the Plex tree while hidden")
        self.assertTrue(attic_bundle.is_dir())
        self.run_tool("restore")
        self.assertTrue((bundle / "Contents/Thumbnails/thumb1.jpg").is_file())
        self.assertFalse((self.data / "bundles").exists())

    # ------------------------------------------------------------ extras

    def item_exists(self, item_id):
        return one(self.db, f"SELECT count(*) FROM metadata_items WHERE id = {item_id};") == "1"

    def test_owned_extra_is_hidden_and_restored(self):
        owned = self.extras["owned_id"]
        self.assertTrue(self.item_exists(owned))
        self.run_tool("hide")
        self.assertFalse(self.item_exists(owned),
                         "an extra reachable only from a hidden library must go with it")
        self.run_tool("restore")
        self.assertTrue(self.item_exists(owned))

    def test_shared_extra_survives_when_a_visible_owner_remains(self):
        shared = self.extras["shared_id"]
        self.run_tool("hide")
        self.assertTrue(self.item_exists(shared),
                        "an extra a visible library still points at must stay put")
        self.run_tool("restore")
        self.assertTrue(self.item_exists(shared))

    def test_extra_media_rows_travel_with_the_extra(self):
        owned = self.extras["owned_id"]
        media_q = f"SELECT count(*) FROM media_items WHERE metadata_item_id = {owned};"
        self.assertEqual(one(self.db, media_q), "1")
        self.run_tool("hide")
        self.assertEqual(one(self.db, media_q), "0",
                         "section-less media rows must not be left behind")
        self.run_tool("restore")
        self.assertEqual(one(self.db, media_q), "1")

    def relations_to(self, item_id):
        return one(self.db, "SELECT count(*) FROM metadata_relations "
                            f"WHERE related_metadata_item_id = {item_id};")

    def test_extra_relations_round_trip(self):
        owned, shared = self.extras["owned_id"], self.extras["shared_id"]
        self.assertEqual(self.relations_to(owned), "1")
        self.assertEqual(self.relations_to(shared), "2", "one hidden owner, one visible")
        self.run_tool("hide")
        self.assertEqual(self.relations_to(owned), "0",
                         "a claimed extra's relation must travel with it")
        self.assertEqual(self.relations_to(shared), "1",
                         "only the hidden owner's link goes; the visible one stays")
        self.run_tool("restore")
        self.assertEqual(self.relations_to(owned), "1")
        self.assertEqual(self.relations_to(shared), "2",
                         "the hidden owner's link must come back on restore")

    def test_extra_bundles_follow_ownership(self):
        owned_b = self.bundle_path(self.extras["owned_hash"])
        shared_b = self.bundle_path(self.extras["shared_hash"])
        self.assertTrue(owned_b.is_dir())
        self.run_tool("hide")
        self.assertFalse(owned_b.exists(), "hidden extra's bundle must leave the Plex tree")
        self.assertTrue(shared_b.is_dir(), "visible extra's bundle must stay in place")
        self.run_tool("restore")
        self.assertTrue((owned_b / "Contents/Thumbnails/thumb1.jpg").is_file())

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


class SessionCountTests(unittest.TestCase):
    """count_playing() decides which sessions block a toggle (no server needed)."""

    def container(self, *states):
        videos = "".join(f'<Video><Player state="{s}"/></Video>' for s in states)
        return f'<MediaContainer size="{len(states)}">{videos}</MediaContainer>'

    def test_empty_container_counts_zero(self):
        self.assertEqual(plexcurtain.count_playing(self.container()), 0)

    def test_paused_sessions_do_not_count(self):
        self.assertEqual(plexcurtain.count_playing(self.container("paused", "paused")), 0)

    def test_stopped_and_unknown_states_do_not_count(self):
        self.assertEqual(plexcurtain.count_playing(self.container("stopped", "weird")), 0)

    def test_stateless_player_does_not_count(self):
        xml = '<MediaContainer size="1"><Video><Player/></Video></MediaContainer>'
        self.assertEqual(plexcurtain.count_playing(xml), 0)

    def test_playing_and_buffering_count(self):
        self.assertEqual(
            plexcurtain.count_playing(self.container("playing", "buffering", "paused")), 2
        )

    def test_malformed_xml_raises_parse_error_for_caller_to_swallow(self):
        with self.assertRaises(ET.ParseError):
            plexcurtain.count_playing("not xml")


if __name__ == "__main__":
    unittest.main()
