"""
refresh_kb.py — Re-import the knowledge base (kb/wh40k.db) into the live app.db.

The app only auto-imports the KB seed on first run (when app.db has no
'datasheets' table). After a KB rebuild (see build_kb.py) the seed changes but
the existing app.db keeps the old datasheets/weapons/loadouts. This script
refreshes the 15 KB tables (+ catalogue_inherits) from the current
kb/wh40k.db while preserving every user table (users, armies, army_units,
army_unit_loadout, models, model_loadout, model_images, model_lists, lists).

It honours WH40K_DATA_DIR, so on the server run it as the app user, e.g.:

    sudo -u wh40k WH40K_DATA_DIR=/var/lib/warhammer-manager \
        /opt/warhammer/venv/bin/python refresh_kb.py

A timestamped backup of app.db is written next to it before any change.

Caveat: datasheet / loadout-option ids can change between editions, so a few
saved army or model loadout selections may need re-picking afterwards — this is
inherent to updating the source data, not a bug in the refresh.
"""
import importlib.util
import shutil
import sqlite3
import sys
import time
from pathlib import Path

# Load the Flask app module without running a server, to reuse its exact import
# logic (USER_DB / KB_SEED_DB resolution, _import_kb_seed, catalogue_inherits).
_spec = importlib.util.spec_from_file_location(
    "whapp", str(Path(__file__).resolve().parent / "app.py"))
app = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(app)

USER_DB = Path(app.USER_DB)
SEED_DB = Path(app.KB_SEED_DB)
KB_TABLES = list(app.KB_TABLES) + ["catalogue_inherits"]


def main():
    if not SEED_DB.exists():
        sys.exit(f"KB seed not found: {SEED_DB} (build it first with build_kb.py)")
    if not USER_DB.exists():
        sys.exit(f"app.db not found: {USER_DB} (nothing to refresh; app will "
                 f"import the seed on first run)")

    backup = USER_DB.with_name(f"{USER_DB.name}.bak-kbrefresh-"
                               f"{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(USER_DB, backup)
    print(f"Backed up app.db -> {backup}")
    print(f"Seed: {SEED_DB}")

    conn = sqlite3.connect(str(USER_DB))
    conn.row_factory = sqlite3.Row          # _populate_catalogue_inherits needs Row
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.OperationalError:
        pass

    before = _count(conn, "datasheets")
    for tbl in KB_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {tbl}")
    conn.commit()

    # Re-create + repopulate all KB tables from the seed (also rebuilds indices
    # and catalogue_inherits). _import_kb_seed commits in its finally block.
    app._import_kb_seed(conn)
    conn.commit()

    after = _count(conn, "datasheets")
    print(f"\nKB refreshed: datasheets {before} -> {after}")
    for t in ("catalogues", "datasheets", "weapons", "abilities",
              "loadout_slots", "loadout_options", "loadout_option_weapons",
              "pricing_tiers", "catalogue_inherits"):
        print(f"    {t}: {_count(conn, t)}")
    print("Preserved user data:")
    for t in ("users", "armies", "army_units", "army_unit_loadout",
              "models", "model_loadout"):
        print(f"    {t}: {_count(conn, t)}")
    conn.close()
    print("\nDone. Restart the app service so workers reconnect cleanly:")
    print("    sudo systemctl restart warhammer-manager")


def _count(conn, table):
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.OperationalError:
        return "-"


if __name__ == "__main__":
    main()
