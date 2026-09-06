# Plexcurtain

Draw the curtain on selected Plex libraries, making them fully invisible to
**every** account, including the server admin, via a menubar control or the
command line.

Instead of deleting libraries (which loses watch states and custom metadata),
`plexcurtain.py` briefly stops Plex Media Server and surgically moves every
database row belonging to the selected libraries (plus their artwork
bundles on disk) into an "attic" SQLite file at
`~/Library/Application Support/Plexcurtain/attic.db`. Restoring moves everything
back byte-for-byte. Nothing is ever rescanned or re-matched, so watch states,
custom posters, manual edits, added-dates, and collections all survive.
Media files are never touched.

## What hiding actually does

Hiding is not unpinning, unsharing, or re-flagging. The rows leave the
database entirely, which is what makes the rest of this true:

- **Gone for every account**, including the server admin. There is no view,
  no client, and no login that still lists the library.
- **Gone from search.** The rows are deleted from `metadata_items` (into the
  attic), so Plex's full-text triggers fire and every title leaves the search
  index. Searching a hidden title returns nothing. Approaches that only
  unpin or re-flag a library leave every title indexed and findable, which
  is the difference between tidying a sidebar and actually hiding something.
- **Gone from the API.** Nothing answers for that section id, so anything
  built on the HTTP API sees a server that simply doesn't have the library.
- **Artwork leaves the Plex tree too.** The `.bundle` directories under
  `Metadata/` and `Media/localhost/` move into the attic alongside the rows,
  and all of it happens while the server is stopped. Butler therefore never
  observes a bundle whose rows are missing, so its cleanup jobs have nothing
  to collect. This is the part people expect to go wrong; it is handled by
  moving the bundles out, not by racing Butler.

What deliberately stays: your media files, which are never touched, and
watch states, which Plex keys by GUID in `metadata_item_settings`. Those
render nowhere while the library is hidden and relink on restore.

## Why not unshare it, or use a managed user?

All reasonable, and for many setups they are the better answer. They solve a
different problem:

- **Unsharing** removes a library from *other* accounts. It stays on the
  admin account, which is usually the account you are actually using.
- **A managed user who doesn't pin the library.** Pinning controls sidebar
  ordering, not access: an unpinned library still appears in search, is
  reachable by URL, and sits one tap away in the full library list.
  Restricting libraries from a managed user *is* real access control, but it
  only applies while you are that user — the admin account still shows
  everything, watch history splits across two accounts, and you cannot
  administer the server from the account you are watching on.
- **Changing `library_sections.section_type` to an undefined value** (say 1
  to 1001) is faster and moves no data. But every row stays in the database,
  so every title stays in the full-text index and remains searchable; the
  web client still renders the library as an unreadable entry, which
  advertises that something is being withheld; and an out-of-range type is
  undefined behavior for Butler, the scanners, and the agents, to be
  re-verified on every server update.
- **Deleting and re-adding** loses watch states, posters, and manual edits,
  and costs a full rescan in each direction.

## Requirements

- macOS with Plex Media Server installed at
  `/Applications/Plex Media Server.app` (the tool uses the bundled
  `Plex SQLite` binary, which is required because Plex's database uses a custom
  full-text tokenizer that stock sqlite can't open safely).
- Python 3.9+ (stdlib only, no packages). Note: on a Mac where the Xcode
  license hasn't been accepted, `/usr/bin/python3` won't run; use a
  Homebrew Python (`/opt/homebrew/bin/python3`) instead.
- [SwiftBar](https://github.com/swiftbar/SwiftBar), needed only for the
  menubar control. The CLI works without it.

## Install

1. **Clone the repo** anywhere and make the scripts executable:

   ```sh
   git clone https://github.com/pjdoland/plexcurtain.git
   cd plexcurtain
   chmod +x plexcurtain.py plexcurtain.5m.sh
   ```

2. **Check the CLI works** (this also creates the config file):

   ```sh
   ./plexcurtain.py status          # should print: all libraries visible
   ./plexcurtain.py list-sections   # should list your libraries
   ```

3. **Install SwiftBar** (skip for CLI-only use):

   ```sh
   brew install --cask swiftbar
   # or download the zip from https://github.com/swiftbar/SwiftBar/releases
   # and drag SwiftBar.app into /Applications
   ```

4. **Point the plugin at your paths.** Edit the two variables at the top of
   `plexcurtain.5m.sh`: `PLEXCURTAIN` (absolute path to `plexcurtain.py` in
   your clone) and `PY` (your working `python3`).

5. **Link the plugin into SwiftBar's plugin folder** (choose the folder on
   SwiftBar's first launch, or pre-set it as below), then launch SwiftBar:

   ```sh
   PLUGDIR="$HOME/Library/Application Support/SwiftBar/Plugins"
   mkdir -p "$PLUGDIR"
   defaults write com.ameba.SwiftBar PluginDirectory -string "$PLUGDIR"
   ln -s "$PWD/plexcurtain.5m.sh" "$PLUGDIR/plexcurtain.5m.sh"
   open -a SwiftBar
   ```

   An eye icon appears in the menubar. Use its checkboxes to select
   libraries, then "Hide extra libraries".

6. **First-toggle permission prompt:** the first hide from the menubar may
   trigger a macOS prompt asking to let SwiftBar control Plex Media Server
   (it quits/relaunches Plex via AppleScript). Approve it once.

Nothing else is installed: no launch agents, no login items beyond SwiftBar
itself, no changes to Plex or your plex.tv account.

## Uninstall

1. **Restore first. Do not uninstall while libraries are hidden:**

   ```sh
   ./plexcurtain.py status    # if anything is hidden:
   ./plexcurtain.py restore
   ```

   (If you already deleted the tool while things were hidden, nothing is
   lost: re-clone it and run `restore`, or copy the newest backup from
   `~/Library/Application Support/Plexcurtain/backups/` back into
   `~/Library/Application Support/Plex Media Server/Plug-in Support/Databases/`
   with Plex stopped. The attic and backups hold everything.)

2. **Remove the menubar plugin:**

   ```sh
   rm "$HOME/Library/Application Support/SwiftBar/Plugins/plexcurtain.5m.sh"
   ```

3. **Delete the data directory** (config, attic, backups):

   ```sh
   rm -rf "$HOME/Library/Application Support/Plexcurtain"
   ```

4. **Delete the repo clone.**

5. **Optionally remove SwiftBar** if nothing else uses it:

   ```sh
   brew uninstall --cask swiftbar        # or just delete /Applications/SwiftBar.app
   rm -rf "$HOME/Library/Application Support/SwiftBar"
   defaults delete com.ameba.SwiftBar
   ```

Plex itself is left exactly as it was; the tool never modifies anything
outside its own data directory, the SwiftBar plugin link, and (while hiding)
the Plex library database it's designed to manage.

## Menubar

The SwiftBar plugin (`plexcurtain.5m.sh`, symlinked into
`~/Library/Application Support/SwiftBar/Plugins/`) shows an eye icon:
open eye = everything visible, slashed eye = hidden.

- **While visible:** "Select libraries to hide" lists every library with a
  checkbox; check/uncheck freely, then "Hide extra libraries".
- **While hidden:** the menu never lists library names. It offers
  "Show extra libraries", and, only if you changed the selection since
  hiding, "Apply selection changes", which restores everything and
  re-hides the new set in one step.

## CLI

```sh
./plexcurtain.py status                 # what's hidden right now
./plexcurtain.py list-sections          # every library: checked? visible/hidden?
./plexcurtain.py check "Some Library"   # add to the hide set
./plexcurtain.py uncheck "Some Library" # remove from the hide set
./plexcurtain.py hide                   # stop Plex, attic the set, restart (~5s)
./plexcurtain.py restore                # bring everything back (~10s)
./plexcurtain.py toggle                 # whichever applies
./plexcurtain.py apply                  # reconcile a selection changed while hidden
```

## Selection semantics (why this can't get wedged)

- The selection (config `sections`) is consulted **only by hide**.
- **Restore always brings back everything in the attic**, no matter what the
  selection says. Editing the selection while hidden can never orphan
  records or half-restore a library.
- Changing the selection while hidden simply marks intent; `apply` (or the
  menubar item) reconciles it via a full restore followed by a fresh hide.
- **Extras travel with their library.** Featurettes, interviews, and fetched
  trailers are stored with no `library_section_id` of their own, linked to
  their movie through `metadata_relations`, so a section-only sweep would
  strand their rows and artwork. Hide claims the extras reachable *only*
  from items it is moving. An extra that a library still on screen also
  points at is left alone, so hiding one library can never strip a trailer
  from another; an extra whose owner link is missing entirely cannot be
  attributed to any library and stays put by design.

Config lives outside the repo at
`~/Library/Application Support/Plexcurtain/config.json`:

```json
{
  "sections": ["Example Library A", "Example Library B"],
  "keep_backups": 3
}
```

## Tests

```sh
python3 tests.py -v
```

The suite builds a synthetic fixture by copying the local Plex database and
cloning rows into fake libraries ("Curtain Test Alpha/Beta"), then exercises
hide/restore round trips (asserting byte-identical content), hidden titles
leaving and re-entering the full-text search index, bundle moves,
watch-state survival, extras travelling with (or staying behind from)
their owners, ID-collision safety for rows added while hidden,
tag resurrection after garbage collection, selection drift + apply,
guard rails (empty selection, double hide, missing library), simulated
schema migration, and failure injection (a failed hide must leave the
database untouched). Everything runs against temp copies with
`PLEXCURTAIN_NO_SERVER=1`; the real server and database are never touched.

## Safety

- A full copy of `com.plexapp.plugins.library.db` is made before every
  hide/restore, kept in `~/Library/Application Support/Plexcurtain/backups/`
  (last N per `keep_backups`). Worst case: copy one back into
  `~/Library/Application Support/Plex Media Server/Plug-in Support/Databases/`.
- Any SQL failure rolls back the transaction and restores the backup
  automatically.
- All SQL runs through Plex's own bundled `Plex SQLite` binary, so the
  full-text search index triggers fire correctly and hidden titles genuinely
  leave the search index rather than lingering in it.
- Every affected table uses AUTOINCREMENT, so rows restored after days of
  hiding can never collide with IDs created in the meantime.

## Caveats

- Plex TV apps cache the sidebar, and all row changes happen while the
  server is stopped, so clients never receive change events for them. The
  tool compensates after every toggle: on restore it triggers a scan on
  each restored library (cheap: nothing on disk changed), and after both
  hide and restore it renames a surviving library and immediately renames
  it back, which fires the section-change events that make connected
  clients re-fetch the library list. A client that is fully asleep during
  the toggle picks up the change when it next connects.
- Each toggle restarts Plex Media Server (a few seconds). The tool refuses
  to run while anyone is actively streaming; override with `--force`.
  Only sessions that are actually playing or buffering block; paused or
  stopped ones do not, since a device someone walked away from mid-video
  would otherwise wedge the toggle indefinitely, and Plex keeps the resume
  point across the restart.
- Play queues referencing hidden items are dropped (Plex regenerates them).
  Playlist *membership* is preserved.
- Avoid letting Plex upgrade itself while libraries are hidden: if a server
  update migrates the database schema, restore inserts by column
  intersection and warns; a removed/renamed column would need manual
  attention (the pre-hide backup always exists). Best practice: restore,
  update Plex, hide again.
- Watch states live in `metadata_item_settings`, keyed by GUID; they are
  intentionally left in place while hidden (they render nowhere) and relink
  automatically on restore.

## License

MIT. See [LICENSE](LICENSE).
