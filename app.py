"""
Warhammer 40K 10e — local visualization platform.

Three modules:
  1. /query    — multi-criteria datasheet search + detail
  2. /army     — pick a faction (catalogue) and freely combine its units
  3. /models   — personal model registry with image uploads

Knowledge base (read-only):   ../kb/wh40k.db
User data (read/write):       app.db   (auto-created on first run)
Uploads:                       static/uploads/
"""

from __future__ import annotations

import os
import re
import secrets
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

from functools import wraps

from flask import (
    Flask, abort, flash, g, redirect, render_template, request, send_from_directory,
    session, url_for,
)
from PIL import Image
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

ROOT = Path(__file__).resolve().parent
# DATA_DIR holds user data (app.db, uploads/) — kept outside the repo on the VPS
# so `git pull` from the webhook never touches user-generated content.
# Local default: project root (legacy paths still work).
DATA_DIR = Path(os.environ.get("WH40K_DATA_DIR", ROOT)).resolve()
# KB_SEED_DB is imported into app.db on first run, then never read again.
# After import app.db is the single source of truth — admins can edit every
# datasheet / weapon / model row directly via /admin.
KB_SEED_DB = ROOT / "kb" / "wh40k.db"
USER_DB = DATA_DIR / "app.db"
UPLOAD_DIR = Path(os.environ.get("WH40K_UPLOAD_DIR", DATA_DIR / "uploads"))
MAX_IMAGE_BYTES = 8 * 1024 * 1024
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

# Legacy migration: if a project-root app.db / static/uploads exists from before
# DATA_DIR was introduced, keep using it so existing deployments don't lose data.
_legacy_db = ROOT / "app.db"
if "WH40K_DATA_DIR" not in os.environ and _legacy_db.exists() and not USER_DB.exists():
    USER_DB = _legacy_db
_legacy_uploads = ROOT / "static" / "uploads"
if ("WH40K_UPLOAD_DIR" not in os.environ
        and _legacy_uploads.exists()
        and any(_legacy_uploads.iterdir())
        and not UPLOAD_DIR.exists()):
    UPLOAD_DIR = _legacy_uploads

app = Flask(__name__)
# SECRET_KEY must be stable across restarts so flash messages and any future
# session usage survive a redeploy. Fall back to an ephemeral key for dev.
app.config["SECRET_KEY"] = os.environ.get("WH40K_SECRET_KEY") or secrets.token_hex(16)
app.config["MAX_CONTENT_LENGTH"] = MAX_IMAGE_BYTES * 10  # multi-upload allowance


# ---------------------------------------------------------------------------- #
# DB helpers
# ---------------------------------------------------------------------------- #

def user_db() -> sqlite3.Connection:
    if "user" not in g:
        # timeout=10s = busy_timeout 10000ms — concurrent writers wait up to
        # 10 seconds for the lock before raising sqlite3.OperationalError.
        conn = sqlite3.connect(str(USER_DB), timeout=10.0)
        conn.row_factory = sqlite3.Row
        # foreign_keys, synchronous are per-connection (not persisted).
        # journal_mode=WAL is persisted in the DB file by init_user_db().
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = NORMAL")
        init_user_db(conn)
        g.user = conn
    return g.user


KB_TABLES = (
    # Order matters: dependents last (so legacy override merging can resolve FKs).
    "catalogues", "categories", "detachments", "enhancements", "rules",
    "datasheets", "datasheet_keywords",
    "unit_models", "weapons", "abilities", "transport", "pricing_tiers",
    "loadout_slots", "loadout_options", "loadout_option_weapons",
)

# Hard-coded Space Marine chapter → base SM catalogue inheritance. Every chapter
# can field every base-SM datasheet (Intercessors, Captains, etc.) so the army
# builder must include both. Other library-style inheritance is filled in by a
# name heuristic in _populate_catalogue_inherits_defaults below.
_SM_BASE_ID = "e0af-67df-9d63-8fb7"  # Imperium - Space Marines
_SM_CHAPTER_IDS = (
    "36d3-36bc-68dd-40ac",  # Black Templars
    "4ef9-15ce-e3e6-36de",  # Blood Angels
    "470a-6daa-9014-12df",  # Dark Angels
    "f89b-84e0-6e3b-f1e2",  # Deathwatch
    "5d6e-fd3-330a-11dd",   # Imperial Fists
    "f27e-18c0-b73e-748e",  # Iron Hands
    "6e59-e1ee-47ad-6ce5",  # Raven Guard
    "2261-79a5-19d9-1668",  # Salamanders
    "94bb-3284-ee14-57a1",  # Space Wolves
    "4029-9237-e8db-af55",  # Ultramarines
    "67c1-fc13-f9a1-cbbf",  # White Scars
)


# Factions whose playable catalogue is empty and whose units live in a Library
# catalogue. Matched by exact name on first import — admin can adjust later.
_FACTION_LIBRARY_LINKS = (
    ("Imperium - Astra Militarum",     "Imperium - Astra Militarum - Library"),
    ("Xenos - Aeldari",                "Aeldari - Aeldari Library"),
    ("Chaos - Chaos Daemons",          "Chaos - Daemons Library"),
    ("Imperium - Imperial Knights",    "Imperium - Imperial Knights - Library"),
    ("Chaos - Chaos Knights",          "Chaos - Chaos Knights Library"),
)


def _populate_catalogue_inherits_defaults(conn: sqlite3.Connection) -> None:
    """One-time seed: link every catalogue that needs a Library or base book.

    Runs inside _import_kb_seed. Admins can edit afterwards via /admin/catalogues.
    """
    # 1. SM chapters → base Space Marines (10e rules: any chapter fields base units).
    have_base = conn.execute(
        "SELECT 1 FROM catalogues WHERE id = ?", (_SM_BASE_ID,),
    ).fetchone()
    if have_base:
        for child_id in _SM_CHAPTER_IDS:
            exists = conn.execute(
                "SELECT 1 FROM catalogues WHERE id = ?", (child_id,),
            ).fetchone()
            if exists:
                conn.execute(
                    "INSERT OR IGNORE INTO catalogue_inherits (child_id, parent_id) "
                    "VALUES (?, ?)", (child_id, _SM_BASE_ID),
                )

    # 2. Faction → Library mapping by exact name. Skipped silently if either
    # side doesn't exist in this KB build.
    for child_name, parent_name in _FACTION_LIBRARY_LINKS:
        child = conn.execute(
            "SELECT id FROM catalogues WHERE name = ?", (child_name,),
        ).fetchone()
        parent = conn.execute(
            "SELECT id FROM catalogues WHERE name = ?", (parent_name,),
        ).fetchone()
        if child and parent:
            conn.execute(
                "INSERT OR IGNORE INTO catalogue_inherits (child_id, parent_id) "
                "VALUES (?, ?)", (child["id"], parent["id"]),
            )


def _import_kb_seed(conn: sqlite3.Connection) -> None:
    """One-time import of kb/wh40k.db rows into app.db.

    Triggers when app.db has no 'datasheets' table yet. After this runs the
    KB seed file is never read again — app.db is the single source of truth
    and admins edit rows directly via /admin.
    """
    if not KB_SEED_DB.exists():
        # No seed available — probably running in a test or before kb build.
        # Leave the tables absent; admin will see an empty CMS.
        return
    conn.execute("ATTACH DATABASE ? AS kbseed", (str(KB_SEED_DB),))
    try:
        # CREATE TABLE foo AS SELECT * FROM kbseed.foo carries column types but
        # not constraints or indices — we add them back below where needed.
        for tbl in KB_TABLES:
            exists = conn.execute(
                "SELECT 1 FROM kbseed.sqlite_master WHERE type='table' AND name=?",
                (tbl,),
            ).fetchone()
            if not exists:
                continue
            conn.execute(f"CREATE TABLE {tbl} AS SELECT * FROM kbseed.{tbl}")
        # Apply any legacy point overrides (from before this migration) into the
        # merged rows, then drop the override tables — admin edits go straight
        # into datasheets.points / pricing_tiers.points now.
        has_ds_ov = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='datasheet_overrides'"
        ).fetchone()
        if has_ds_ov:
            conn.execute("""
                UPDATE datasheets SET points = (
                    SELECT points FROM datasheet_overrides
                    WHERE datasheet_overrides.datasheet_id = datasheets.id
                )
                WHERE id IN (SELECT datasheet_id FROM datasheet_overrides)
            """)
            conn.execute("DROP TABLE datasheet_overrides")
        has_tier_ov = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pricing_tier_overrides'"
        ).fetchone()
        if has_tier_ov:
            conn.execute("""
                UPDATE pricing_tiers SET points = (
                    SELECT pricing_tier_overrides.points FROM pricing_tier_overrides
                    WHERE pricing_tier_overrides.datasheet_id = pricing_tiers.datasheet_id
                    AND pricing_tier_overrides.condition_text = pricing_tiers.condition_text
                )
                WHERE EXISTS (
                    SELECT 1 FROM pricing_tier_overrides
                    WHERE pricing_tier_overrides.datasheet_id = pricing_tiers.datasheet_id
                    AND pricing_tier_overrides.condition_text = pricing_tiers.condition_text
                )
            """)
            conn.execute("DROP TABLE pricing_tier_overrides")
        # Indices that the runtime queries rely on.
        conn.executescript("""
            CREATE INDEX IF NOT EXISTS idx_datasheets_cat        ON datasheets(catalogue_id);
            CREATE INDEX IF NOT EXISTS idx_unit_models_ds        ON unit_models(datasheet_id);
            CREATE INDEX IF NOT EXISTS idx_weapons_ds            ON weapons(datasheet_id);
            CREATE INDEX IF NOT EXISTS idx_abilities_ds          ON abilities(datasheet_id);
            CREATE INDEX IF NOT EXISTS idx_ds_keywords_ds        ON datasheet_keywords(datasheet_id);
            CREATE INDEX IF NOT EXISTS idx_loadout_slots_model   ON loadout_slots(model_id);
            CREATE INDEX IF NOT EXISTS idx_loadout_options_slot  ON loadout_options(slot_id);
            CREATE INDEX IF NOT EXISTS idx_low_option            ON loadout_option_weapons(option_id);
            CREATE INDEX IF NOT EXISTS idx_pricing_tiers_ds      ON pricing_tiers(datasheet_id);
        """)
        # catalogue_inherits is OUR table (not in KB), but it's tightly coupled to
        # the just-imported catalogues so we create + seed it here in the same tx.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS catalogue_inherits (
                child_id  TEXT NOT NULL,
                parent_id TEXT NOT NULL,
                PRIMARY KEY (child_id, parent_id)
            )
        """)
        _populate_catalogue_inherits_defaults(conn)
    finally:
        conn.commit()
        conn.execute("DETACH DATABASE kbseed")


def init_user_db(conn: sqlite3.Connection) -> None:
    """Idempotent: safe to call on existing DBs (uses IF NOT EXISTS).

    Also applies concurrency-friendly PRAGMAs:
      - journal_mode=WAL    readers don't block writers, persisted on disk
      - synchronous=NORMAL  pairs with WAL; ~2× write speed, still durable
    Both are persisted in the DB file, so they apply to every later connection.
    """
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    has_datasheets = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='datasheets'"
    ).fetchone()
    if not has_datasheets:
        _import_kb_seed(conn)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id            INTEGER PRIMARY KEY,
        username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
        password_hash TEXT NOT NULL,
        role          TEXT NOT NULL DEFAULT 'user',  -- 'user' or 'admin'
        is_public     INTEGER NOT NULL DEFAULT 0,
        created_at    TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_users_public ON users(is_public);

    CREATE TABLE IF NOT EXISTS armies (
        id          INTEGER PRIMARY KEY,
        name        TEXT NOT NULL,
        faction_id  TEXT NOT NULL,
        faction_name TEXT NOT NULL,
        notes       TEXT,
        created_at  TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS army_units (
        id           INTEGER PRIMARY KEY,
        army_id      INTEGER NOT NULL REFERENCES armies(id) ON DELETE CASCADE,
        datasheet_id TEXT NOT NULL,
        datasheet_name TEXT NOT NULL,
        points       INTEGER,
        count        INTEGER NOT NULL DEFAULT 1,
        notes        TEXT,
        added_at     TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS models (
        id            INTEGER PRIMARY KEY,
        custom_name   TEXT,
        datasheet_id  TEXT NOT NULL,
        datasheet_name TEXT NOT NULL,
        faction_name TEXT,
        status        TEXT NOT NULL DEFAULT 'unpainted',
        notes         TEXT,
        created_at    TEXT NOT NULL,
        updated_at    TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS model_images (
        id           INTEGER PRIMARY KEY,
        model_id     INTEGER NOT NULL REFERENCES models(id) ON DELETE CASCADE,
        filename     TEXT NOT NULL,
        uploaded_at  TEXT NOT NULL
    );

    -- Per-army-unit weapon loadout choices.
    CREATE TABLE IF NOT EXISTS army_unit_loadout (
        id           INTEGER PRIMARY KEY,
        army_unit_id INTEGER NOT NULL REFERENCES army_units(id) ON DELETE CASCADE,
        model_id     TEXT NOT NULL,    -- KB unit_models.id
        slot_id      TEXT NOT NULL,    -- KB loadout_slots.id (choice slots only)
        option_id    TEXT NOT NULL,    -- KB loadout_options.id (chosen option)
        UNIQUE (army_unit_id, slot_id)
    );

    -- Per personal-model weapon loadout choices.
    CREATE TABLE IF NOT EXISTS model_loadout (
        id        INTEGER PRIMARY KEY,
        model_id  INTEGER NOT NULL REFERENCES models(id) ON DELETE CASCADE,
        slot_id   TEXT NOT NULL,
        option_id TEXT NOT NULL,
        UNIQUE (model_id, slot_id)
    );

    -- Faction inheritance. A child catalogue can field every datasheet of any
    -- catalogue it lists here (transitively). E.g. Blood Angels inherits from
    -- "Imperium - Space Marines" so a BA army sees all base SM units too.
    CREATE TABLE IF NOT EXISTS catalogue_inherits (
        child_id  TEXT NOT NULL,
        parent_id TEXT NOT NULL,
        PRIMARY KEY (child_id, parent_id)
    );

    -- User-defined collections of personal models. Models belong to exactly
    -- one list; visibility is set per-list, independent of users.is_public.
    CREATE TABLE IF NOT EXISTS model_lists (
        id              INTEGER PRIMARY KEY,
        user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        name            TEXT NOT NULL,
        description     TEXT,
        is_public       INTEGER NOT NULL DEFAULT 0,
        cover_filename  TEXT,                       -- NULL → random model image
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_model_lists_user   ON model_lists(user_id);
    CREATE INDEX IF NOT EXISTS idx_model_lists_public ON model_lists(is_public);

    CREATE INDEX IF NOT EXISTS idx_army_units_army    ON army_units(army_id);
    CREATE INDEX IF NOT EXISTS idx_model_images_model ON model_images(model_id);
    CREATE INDEX IF NOT EXISTS idx_aul_unit            ON army_unit_loadout(army_unit_id);
    CREATE INDEX IF NOT EXISTS idx_mll_model           ON model_loadout(model_id);
    """)
    # models.model_type_id — added separately because ALTER TABLE has no IF NOT EXISTS in 3.34
    cols = [r[1] for r in conn.execute("PRAGMA table_info(models)").fetchall()]
    if "model_type_id" not in cols:
        conn.execute("ALTER TABLE models ADD COLUMN model_type_id TEXT")
    # army_units.tier_label + tier_points — chosen pricing tier (扩编 attribute).
    # NULL means base size.
    au_cols = [r[1] for r in conn.execute("PRAGMA table_info(army_units)").fetchall()]
    if "tier_label" not in au_cols:
        conn.execute("ALTER TABLE army_units ADD COLUMN tier_label TEXT")
    if "tier_points" not in au_cols:
        conn.execute("ALTER TABLE army_units ADD COLUMN tier_points INTEGER")
    # armies.user_id / models.user_id — nullable; first registered user claims
    # all NULL rows and becomes admin (see register route).
    army_cols = [r[1] for r in conn.execute("PRAGMA table_info(armies)").fetchall()]
    if "user_id" not in army_cols:
        conn.execute("ALTER TABLE armies ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE CASCADE")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_armies_user ON armies(user_id)")
    model_cols = [r[1] for r in conn.execute("PRAGMA table_info(models)").fetchall()]
    if "user_id" not in model_cols:
        conn.execute("ALTER TABLE models ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE CASCADE")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_models_user ON models(user_id)")
    # models.list_id — every model belongs to exactly one model_lists row.
    # Nullable on disk for the migration window; auto-claim runs below.
    if "list_id" not in model_cols:
        conn.execute("ALTER TABLE models ADD COLUMN list_id INTEGER REFERENCES model_lists(id) ON DELETE CASCADE")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_models_list ON models(list_id)")
    # Auto-create a "My Models" default list for any user who has unassigned
    # models. is_public copies their account flag so a public user's models
    # stay visible after the upgrade.
    orphan_owners = conn.execute("""
        SELECT DISTINCT m.user_id FROM models m
        WHERE m.user_id IS NOT NULL AND m.list_id IS NULL
    """).fetchall()
    now = datetime.utcnow().isoformat()
    for row in orphan_owners:
        uid = row["user_id"]
        user_row = conn.execute(
            "SELECT is_public FROM users WHERE id = ?", (uid,),
        ).fetchone()
        is_public = user_row["is_public"] if user_row else 0
        cur = conn.execute(
            "INSERT INTO model_lists (user_id, name, description, is_public, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (uid, "My Models",
             "Auto-created during upgrade. Holds every model you registered "
             "before the list system existed.",
             is_public, now, now),
        )
        conn.execute(
            "UPDATE models SET list_id = ? WHERE user_id = ? AND list_id IS NULL",
            (cur.lastrowid, uid),
        )
    # If KB tables already exist but catalogue_inherits is empty, seed the
    # default chapter/library relationships now (post-upgrade path).
    has_datasheets_now = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='datasheets'"
    ).fetchone()
    inherits_empty = conn.execute(
        "SELECT COUNT(*) AS n FROM catalogue_inherits"
    ).fetchone()["n"] == 0
    if has_datasheets_now and inherits_empty:
        _populate_catalogue_inherits_defaults(conn)
    conn.commit()


def get_loadout_schema(datasheet_id: str) -> list[dict]:
    """Return composition + choice slots + options for a datasheet.

    Returns: [{model: row, choice_slots: [{slot: row, options: [{row, weapons}]}]}]
    Only includes slots with kind='choice' since 'fixed' has no user choice.
    """
    db = user_db()
    model_rows = db.execute(
        "SELECT * FROM unit_models WHERE datasheet_id = ? ORDER BY sort_order",
        (datasheet_id,),
    ).fetchall()
    out = []
    for m in model_rows:
        slot_rows = db.execute(
            "SELECT * FROM loadout_slots WHERE model_id = ? AND kind = 'choice' "
            "ORDER BY sort_order",
            (m["id"],),
        ).fetchall()
        slots = []
        for s in slot_rows:
            opts = db.execute(
                "SELECT * FROM loadout_options WHERE slot_id = ? "
                "ORDER BY is_default DESC, sort_order",
                (s["id"],),
            ).fetchall()
            opts_with_weapons = []
            for o in opts:
                weapons = db.execute(
                    "SELECT w.* FROM loadout_option_weapons l "
                    "JOIN weapons w ON w.profile_id = l.weapon_profile_id "
                    "WHERE l.option_id = ? ORDER BY l.sort_order",
                    (o["id"],),
                ).fetchall()
                opts_with_weapons.append({"row": o, "weapons": weapons})
            slots.append({"slot": s, "options": opts_with_weapons})
        out.append({"model": m, "choice_slots": slots})
    return out


def get_default_loadout(datasheet_id: str) -> list[tuple[str, str, str]]:
    """Return list of (model_id, slot_id, default_option_id) for every choice slot."""
    db = user_db()
    rows = db.execute("""
        SELECT s.model_id, s.id AS slot_id,
               (SELECT id FROM loadout_options WHERE slot_id = s.id
                ORDER BY is_default DESC, sort_order LIMIT 1) AS option_id
        FROM loadout_slots s
        JOIN unit_models um ON um.id = s.model_id
        WHERE um.datasheet_id = ? AND s.kind = 'choice'
    """, (datasheet_id,)).fetchall()
    return [(r["model_id"], r["slot_id"], r["option_id"]) for r in rows
            if r["option_id"]]


def fetch_army_unit_loadout(army_unit_id: int) -> dict[str, str]:
    """Return {slot_id: option_id} for an army unit."""
    rows = user_db().execute(
        "SELECT slot_id, option_id FROM army_unit_loadout WHERE army_unit_id = ?",
        (army_unit_id,),
    ).fetchall()
    return {r["slot_id"]: r["option_id"] for r in rows}


def fetch_model_loadout(model_id: int) -> dict[str, str]:
    rows = user_db().execute(
        "SELECT slot_id, option_id FROM model_loadout WHERE model_id = ?",
        (model_id,),
    ).fetchall()
    return {r["slot_id"]: r["option_id"] for r in rows}


def option_summary(option_id: str) -> dict | None:
    """Return option row + weapons for display."""
    db = user_db()
    o = db.execute("SELECT * FROM loadout_options WHERE id = ?", (option_id,)).fetchone()
    if not o:
        return None
    weapons = db.execute(
        "SELECT w.* FROM loadout_option_weapons l "
        "JOIN weapons w ON w.profile_id = l.weapon_profile_id "
        "WHERE l.option_id = ? ORDER BY l.sort_order",
        (option_id,),
    ).fetchall()
    return {"row": o, "weapons": weapons}


@app.teardown_appcontext
def close_dbs(exception=None):
    conn = g.pop("user", None)
    if conn is not None:
        conn.close()


# ---------------------------------------------------------------------------- #
# Auth
# ---------------------------------------------------------------------------- #

def current_user() -> sqlite3.Row | None:
    """Cached on the request via g — safe to call from anywhere."""
    if "current_user" not in g:
        uid = session.get("user_id")
        g.current_user = None
        if uid is not None:
            row = user_db().execute(
                "SELECT * FROM users WHERE id = ?", (uid,),
            ).fetchone()
            g.current_user = row
            if row is None:
                session.pop("user_id", None)
    return g.current_user


def require_user_id() -> int:
    """Return current user's id or abort. Use inside @login_required routes."""
    u = current_user()
    if u is None:
        abort(401)
    return u["id"]


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_user() is None:
            flash("Please log in first", "error")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        u = current_user()
        if u is None:
            flash("Please log in first", "error")
            return redirect(url_for("login", next=request.path))
        if u["role"] != "admin":
            abort(403)
        return view(*args, **kwargs)
    return wrapped


@app.context_processor
def inject_user():
    """Expose current_user to every template as `me`."""
    return {"me": current_user()}


# ---------------------------------------------------------------------------- #
# Shared lookups
# ---------------------------------------------------------------------------- #

def get_catalogue_chain(catalogue_id: str) -> list[str]:
    """Return [catalogue_id] + all transitive parents from catalogue_inherits.

    Used by every "what units are in this faction" query so that e.g. a Blood
    Angels army sees both BA-specific and base Space Marine datasheets.
    """
    seen = {catalogue_id}
    stack = [catalogue_id]
    while stack:
        cur = stack.pop()
        for r in user_db().execute(
            "SELECT parent_id FROM catalogue_inherits WHERE child_id = ?", (cur,),
        ).fetchall():
            if r["parent_id"] not in seen:
                seen.add(r["parent_id"])
                stack.append(r["parent_id"])
    return list(seen)


def list_factions() -> list[sqlite3.Row]:
    """Playable catalogues, sorted by name.

    A catalogue is shown if it has datasheets of its own OR inherits from a
    catalogue that does (e.g. "Xenos - Aeldari" is empty but inherits the
    Aeldari Library). Libraries themselves are filtered out.
    """
    rows = user_db().execute("""
        WITH counts AS (
            SELECT c.id, c.name, c.is_library,
                   (SELECT COUNT(*) FROM datasheets d WHERE d.catalogue_id = c.id) AS own,
                   (SELECT COUNT(*) FROM datasheets d
                    JOIN catalogue_inherits ci ON ci.parent_id = d.catalogue_id
                    WHERE ci.child_id = c.id) AS inherited
            FROM catalogues c WHERE c.is_library = 0
        )
        SELECT id, name, is_library, (own + inherited) AS n_datasheets
        FROM counts WHERE (own + inherited) > 0
        ORDER BY name
    """).fetchall()
    return rows


def list_keywords() -> list[str]:
    rows = user_db().execute("""
        SELECT category_name, COUNT(*) AS n
        FROM datasheet_keywords
        GROUP BY category_name ORDER BY category_name
    """).fetchall()
    return [r["category_name"] for r in rows]


def get_datasheet_full(datasheet_id: str) -> dict | None:
    db = user_db()
    ds = db.execute("""
        SELECT d.*, c.name AS catalogue_name
        FROM datasheets d JOIN catalogues c ON c.id = d.catalogue_id
        WHERE d.id = ?
    """, (datasheet_id,)).fetchone()
    if not ds:
        return None

    # Composition: 1 row per model type
    model_rows = db.execute(
        "SELECT * FROM unit_models WHERE datasheet_id = ? ORDER BY sort_order",
        (datasheet_id,),
    ).fetchall()
    models: list[dict] = []
    for m in model_rows:
        slot_rows = db.execute(
            "SELECT * FROM loadout_slots WHERE model_id = ? ORDER BY sort_order",
            (m["id"],),
        ).fetchall()
        slots = []
        for s in slot_rows:
            opt_rows = db.execute(
                "SELECT * FROM loadout_options WHERE slot_id = ? "
                "ORDER BY is_default DESC, sort_order",
                (s["id"],),
            ).fetchall()
            opts = []
            for o in opt_rows:
                weapons = db.execute(
                    "SELECT w.* FROM loadout_option_weapons l "
                    "JOIN weapons w ON w.profile_id = l.weapon_profile_id "
                    "WHERE l.option_id = ? ORDER BY l.sort_order",
                    (o["id"],),
                ).fetchall()
                opts.append({"row": o, "weapons": weapons})
            slots.append({"row": s, "options": opts})
        models.append({"row": m, "slots": slots})

    abilities = db.execute(
        "SELECT * FROM abilities WHERE datasheet_id = ? ORDER BY ability_type, name",
        (datasheet_id,),
    ).fetchall()
    keywords = db.execute(
        "SELECT category_name, is_primary FROM datasheet_keywords "
        "WHERE datasheet_id = ? ORDER BY is_primary DESC, category_name",
        (datasheet_id,),
    ).fetchall()
    transport = db.execute(
        "SELECT * FROM transport WHERE datasheet_id = ?", (datasheet_id,)
    ).fetchall()
    pricing_tiers = db.execute(
        "SELECT * FROM pricing_tiers WHERE datasheet_id = ? "
        "ORDER BY condition_value, points",
        (datasheet_id,),
    ).fetchall()
    return {
        "ds": ds,
        "models": models,
        "abilities": abilities,
        "keywords": keywords,
        "transport": transport,
        "pricing_tiers": pricing_tiers,
    }


# ---------------------------------------------------------------------------- #
# Routes — home
# ---------------------------------------------------------------------------- #

@app.route("/uploads/<path:filename>")
def uploaded_file(filename: str):
    """Serve user-uploaded images from UPLOAD_DIR (which may live outside the repo)."""
    return send_from_directory(UPLOAD_DIR, filename)


def _replace_uploaded_image(filename: str, file_storage) -> bool:
    """Overwrite UPLOAD_DIR/filename with file_storage (an edited blob)."""
    target = UPLOAD_DIR / filename
    if not target.exists():
        return False
    try:
        # Re-validate the incoming blob: must be a real image, capped at the
        # same thumbnail size as save_uploaded_image so a malicious client
        # can't push a 200 MB PNG.
        tmp = target.with_suffix(target.suffix + ".tmp")
        file_storage.save(tmp)
        with Image.open(tmp) as img:
            img.verify()
        with Image.open(tmp) as img:
            img.thumbnail((1600, 1600))
            img.save(target)
        tmp.unlink(missing_ok=True)
        return True
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return False


@app.route("/lists/<int:list_id>/cover/replace", methods=["POST"])
@login_required
def list_cover_replace(list_id: int):
    """Save/overwrite a list's custom cover from an edited blob.

    If the list already has a cover, overwrite that file in place. Otherwise
    save the blob as a fresh image and link it. Caller is the in-browser
    editor's Apply handler.
    """
    list_row = _ensure_owned_list(list_id)
    fs = request.files.get("image")
    if not fs or not fs.filename:
        abort(400)
    if list_row["cover_filename"]:
        # Overwrite existing file.
        if not _replace_uploaded_image(list_row["cover_filename"], fs):
            abort(400)
    else:
        # Save fresh and link it. save_uploaded_image validates + thumbnails.
        fn = save_uploaded_image(fs)
        if not fn:
            abort(400)
        user_db().execute(
            "UPDATE model_lists SET cover_filename = ?, updated_at = ? WHERE id = ?",
            (fn, datetime.utcnow().isoformat(), list_id),
        )
        user_db().commit()
    return {"ok": True}


@app.route("/lists/<int:list_id>/cover/clear", methods=["POST"])
@login_required
def list_cover_clear(list_id: int):
    """Drop the custom cover so the list falls back to a random model image."""
    list_row = _ensure_owned_list(list_id)
    if list_row["cover_filename"]:
        try:
            (UPLOAD_DIR / list_row["cover_filename"]).unlink(missing_ok=True)
        except Exception:
            pass
    user_db().execute(
        "UPDATE model_lists SET cover_filename = NULL, updated_at = ? WHERE id = ?",
        (datetime.utcnow().isoformat(), list_id),
    )
    user_db().commit()
    flash("Custom cover removed", "ok")
    return redirect(url_for("list_edit", list_id=list_id))


@app.route("/")
def index():
    db_u = user_db()
    me = current_user()
    if me is not None:
        n_armies = db_u.execute(
            "SELECT COUNT(*) AS n FROM armies WHERE user_id = ?", (me["id"],),
        ).fetchone()["n"]
        n_my_lists = db_u.execute(
            "SELECT COUNT(*) AS n FROM model_lists WHERE user_id = ?", (me["id"],),
        ).fetchone()["n"]
    else:
        n_armies = n_my_lists = 0
    n_units = user_db().execute("SELECT COUNT(*) AS n FROM datasheets").fetchone()["n"]
    n_factions = len(list_factions())
    n_public_users = db_u.execute(
        "SELECT COUNT(*) AS n FROM users WHERE is_public = 1"
    ).fetchone()["n"]
    # Public list gallery — guests included. Anyone (logged in or not) sees
    # every list that was flagged is_public, regardless of the owner's account
    # visibility (list visibility is intentionally independent).
    list_rows = db_u.execute("""
        SELECT l.id, l.name, l.description, l.cover_filename, l.created_at,
               u.username AS owner,
               (SELECT COUNT(*) FROM models m WHERE m.list_id = l.id) AS n_models
        FROM model_lists l
        JOIN users u ON u.id = l.user_id
        WHERE l.is_public = 1
        ORDER BY l.updated_at DESC LIMIT 60
    """).fetchall()
    public_lists = [
        {**dict(r), "cover_url": _list_cover_url(r, db_u)} for r in list_rows
    ]
    n_public_lists = db_u.execute(
        "SELECT COUNT(*) AS n FROM model_lists WHERE is_public = 1"
    ).fetchone()["n"]
    return render_template(
        "index.html",
        n_armies=n_armies, n_my_lists=n_my_lists, n_units=n_units, n_factions=n_factions,
        n_public_users=n_public_users, n_public_lists=n_public_lists,
        public_lists=public_lists,
    )


# ---------------------------------------------------------------------------- #
# Routes — module 1: query
# ---------------------------------------------------------------------------- #

@app.route("/query")
def query_search():
    args = request.args
    name = (args.get("name") or "").strip()
    catalogue_id = args.get("catalogue") or ""
    keyword = args.get("keyword") or ""
    t_min = args.get("t_min", type=int)
    t_max = args.get("t_max", type=int)
    w_min = args.get("w_min", type=int)
    pts_min = args.get("pts_min", type=int)
    pts_max = args.get("pts_max", type=int)
    has_weapon = (args.get("weapon") or "").strip()

    where = []
    params: list = []
    # Use the first model's stats as the "headline" stats for the datasheet
    sql = """
        SELECT DISTINCT d.id, d.name, d.points, c.name AS catalogue_name,
               um.t AS t, um.w AS w, um.sv AS sv, um.m AS m
        FROM datasheets d
        JOIN catalogues c ON c.id = d.catalogue_id
        LEFT JOIN unit_models um ON um.datasheet_id = d.id AND um.sort_order = 0
    """
    if keyword:
        sql += "\n JOIN datasheet_keywords k ON k.datasheet_id = d.id"
        where.append("k.category_name = ?")
        params.append(keyword)
    if has_weapon:
        sql += "\n JOIN weapons w ON w.datasheet_id = d.id"
        where.append("w.name LIKE ?")
        params.append(f"%{has_weapon}%")
    if name:
        where.append("d.name LIKE ?")
        params.append(f"%{name}%")
    if catalogue_id:
        chain = get_catalogue_chain(catalogue_id)
        ph = ",".join("?" * len(chain))
        where.append(f"d.catalogue_id IN ({ph})")
        params.extend(chain)
    if t_min is not None:
        where.append("CAST(um.t AS INTEGER) >= ?")
        params.append(t_min)
    if t_max is not None:
        where.append("CAST(um.t AS INTEGER) <= ?")
        params.append(t_max)
    if w_min is not None:
        where.append("CAST(um.w AS INTEGER) >= ?")
        params.append(w_min)
    if pts_min is not None:
        where.append("d.points >= ?")
        params.append(pts_min)
    if pts_max is not None:
        where.append("d.points <= ?")
        params.append(pts_max)

    if where:
        sql += "\n WHERE " + " AND ".join(where)
    sql += "\n ORDER BY c.name, d.name LIMIT 500"

    has_filter = bool(where or name or catalogue_id or keyword or has_weapon)
    results = user_db().execute(sql, params).fetchall() if has_filter else []

    return render_template(
        "query_search.html",
        factions=list_factions(),
        keywords=list_keywords(),
        results=results,
        has_filter=has_filter,
        f={"name": name, "catalogue": catalogue_id, "keyword": keyword,
           "t_min": t_min, "t_max": t_max, "w_min": w_min,
           "pts_min": pts_min, "pts_max": pts_max, "weapon": has_weapon},
    )


def _resolve_back(from_param: str) -> tuple[str, str]:
    """Parse ?from=<kind>:<id> → (label, href) for the back link.

    Ownership-aware: only resolves to an army/model the current user owns,
    otherwise falls back to the search page.
    """
    me = current_user()
    uid = me["id"] if me else None
    if from_param.startswith("army:") and uid is not None:
        try:
            aid = int(from_param.split(":", 1)[1])
        except (ValueError, IndexError):
            return ("← Back to search", url_for("query_search"))
        row = user_db().execute(
            "SELECT name FROM armies WHERE id = ? AND user_id = ?", (aid, uid),
        ).fetchone()
        if row:
            return (f"← Back to army “{row['name']}”", url_for("army_view", army_id=aid))
    if from_param.startswith("model:") and uid is not None:
        try:
            mid = int(from_param.split(":", 1)[1])
        except (ValueError, IndexError):
            return ("← Back to search", url_for("query_search"))
        row = user_db().execute(
            "SELECT custom_name, datasheet_name FROM models WHERE id = ? AND user_id = ?",
            (mid, uid),
        ).fetchone()
        if row:
            label = row["custom_name"] or row["datasheet_name"]
            return (f"← Back to model “{label}”", url_for("model_detail", model_id=mid))
    if from_param == "models":
        return ("← Back to My Models", url_for("models_list"))
    return ("← Back to search", url_for("query_search"))


@app.route("/query/unit/<datasheet_id>")
def query_unit(datasheet_id: str):
    data = get_datasheet_full(datasheet_id)
    if not data:
        abort(404)
    from_param = request.args.get("from") or ""
    back_label, back_href = _resolve_back(from_param)
    return render_template("query_unit.html",
                           back_label=back_label, back_href=back_href, **data)


# ---------------------------------------------------------------------------- #
# Routes — module 2: army builder
# ---------------------------------------------------------------------------- #

# Tabs for the "Available units" panel in army_view. Order matches what the
# user sees; classification priority is top-down (Epic Hero wins over Character
# which wins over Vehicle/Monster, etc.) so a unit lands in exactly one bucket.
UNIT_TABS = (
    ("epic_hero",       "Epic Hero"),
    ("character",       "Character"),
    ("battleline",      "Battleline"),
    ("infantry",        "Infantry"),
    ("mounted",         "Mounted"),
    ("beast",           "Beast"),
    ("vehicle_monster", "Vehicle / Monster"),
    ("others",          "Others"),
)


def _classify_unit_keywords(kws: set[str]) -> str:
    """Return the tab key a unit belongs to based on its keywords.

    Priority order:
      1. Epic Hero / Character — named or generic characters
      2. Battleline — battleline units of any base type
      3. Mounted / Beast — distinct unit-type keywords (no overlap with
         Infantry / Vehicle / Monster in the KB)
      4. Vehicle / Monster
      5. Infantry
      6. Others
    """
    if "Epic Hero" in kws:
        return "epic_hero"
    if "Character" in kws:
        return "character"
    if "Battleline" in kws:
        return "battleline"
    if "Mounted" in kws:
        return "mounted"
    if "Beast" in kws:
        return "beast"
    if "Vehicle" in kws or "Monster" in kws:
        return "vehicle_monster"
    if "Infantry" in kws:
        return "infantry"
    return "others"


def _classify_units_into_tabs(rows: list[sqlite3.Row]) -> dict[str, list[dict]]:
    """Bucket query rows (with a 'kw_str' column) into UNIT_TABS keys."""
    buckets: dict[str, list[dict]] = {k: [] for k, _ in UNIT_TABS}
    for r in rows:
        kws = set((r["kw_str"] or "").split("|"))
        buckets[_classify_unit_keywords(kws)].append(dict(r))
    return buckets


@app.route("/army")
@login_required
def army_list():
    uid = require_user_id()
    # Live pricing: read datasheets.points / pricing_tiers.points fresh on every
    # render so admin edits in /admin/units retroactively update army totals.
    rows = user_db().execute("""
        SELECT a.*,
               COALESCE(SUM(COALESCE(pt.points, d.points) * u.count), 0) AS total_pts,
               COUNT(u.id) AS n_units
        FROM armies a
          LEFT JOIN army_units u ON u.army_id = a.id
          LEFT JOIN datasheets d ON d.id = u.datasheet_id
          LEFT JOIN pricing_tiers pt ON pt.datasheet_id = u.datasheet_id
                                    AND pt.condition_text = u.tier_label
        WHERE a.user_id = ?
        GROUP BY a.id ORDER BY a.created_at DESC
    """, (uid,)).fetchall()
    return render_template("army_list.html", armies=rows)


@app.route("/army/new", methods=["GET", "POST"])
@login_required
def army_new():
    uid = require_user_id()
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        faction_id = request.form.get("faction") or ""
        notes = (request.form.get("notes") or "").strip()
        if not name or not faction_id:
            flash("Name and faction are required", "error")
            return redirect(url_for("army_new"))
        faction = user_db().execute(
            "SELECT name FROM catalogues WHERE id = ?", (faction_id,)
        ).fetchone()
        if not faction:
            flash("Invalid faction", "error")
            return redirect(url_for("army_new"))
        cur = user_db().execute(
            "INSERT INTO armies (name, faction_id, faction_name, notes, created_at, user_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, faction_id, faction["name"], notes,
             datetime.utcnow().isoformat(), uid),
        )
        user_db().commit()
        return redirect(url_for("army_view", army_id=cur.lastrowid))
    return render_template("army_new.html", factions=list_factions())


@app.route("/army/<int:army_id>")
@login_required
def army_view(army_id: int):
    uid = require_user_id()
    army = user_db().execute(
        "SELECT * FROM armies WHERE id = ? AND user_id = ?", (army_id, uid),
    ).fetchone()
    if not army:
        abort(404)
    unit_rows = user_db().execute("""
        SELECT u.*,
               d.points  AS live_base_points,
               pt.points AS live_tier_points
        FROM army_units u
          LEFT JOIN datasheets d ON d.id = u.datasheet_id
          LEFT JOIN pricing_tiers pt ON pt.datasheet_id = u.datasheet_id
                                    AND pt.condition_text = u.tier_label
        WHERE u.army_id = ? ORDER BY u.added_at
    """, (army_id,)).fetchall()
    units = []
    total = 0
    for r in unit_rows:
        d = dict(r)
        d["is_expanded"] = d["tier_label"] is not None
        d["effective_points"] = (d["live_tier_points"] if d["is_expanded"]
                                 else d["live_base_points"])
        # Loadout summary: list of "model: option_name"
        chosen = fetch_army_unit_loadout(d["id"])
        d["loadout_summary"] = []
        if chosen:
            db = user_db()
            for slot_id, option_id in chosen.items():
                row = db.execute("""
                    SELECT o.name AS option_name, s.slot_name, um.name AS model_name
                    FROM loadout_options o
                    JOIN loadout_slots s ON s.id = o.slot_id
                    JOIN unit_models um ON um.id = s.model_id
                    WHERE o.id = ?
                """, (option_id,)).fetchone()
                if row:
                    d["loadout_summary"].append({
                        "model_name": row["model_name"],
                        "slot_name": row["slot_name"],
                        "option_name": row["option_name"],
                    })
        total += (d["effective_points"] or 0) * d["count"]
        units.append(d)

    keyword = (request.args.get("keyword") or "").strip()
    show_legends = request.args.get("show_legends") == "1"
    # Expand the army's catalogue with its inherited parents (chapter → base SM
    # etc.) so the unit picker shows everything the army can legally field.
    cat_chain = get_catalogue_chain(army["faction_id"])
    placeholders = ",".join("?" * len(cat_chain))
    # Pull keywords inline via GROUP_CONCAT so we can classify into tabs in
    # Python without a second per-row query.
    avail_rows = user_db().execute(f"""
        SELECT d.id, d.name, d.points,
               GROUP_CONCAT(k.category_name, '|') AS kw_str
        FROM datasheets d
        LEFT JOIN datasheet_keywords k ON k.datasheet_id = d.id
        WHERE d.catalogue_id IN ({placeholders})
        GROUP BY d.id ORDER BY d.name
    """, cat_chain).fetchall()
    if keyword:
        avail_rows = [r for r in avail_rows
                      if keyword in (r["kw_str"] or "").split("|")]
    # Legends units are flagged by a literal "[Legends]" suffix in the name —
    # there is no separate keyword tag in BSData for it.
    if not show_legends:
        avail_rows = [r for r in avail_rows if "[Legends]" not in (r["name"] or "")]
    available_by_tab = _classify_units_into_tabs(avail_rows)
    # Keywords available across the full chain
    kw_rows = user_db().execute(
        f"SELECT DISTINCT k.category_name FROM datasheet_keywords k "
        f"JOIN datasheets d ON d.id = k.datasheet_id "
        f"WHERE d.catalogue_id IN ({placeholders}) ORDER BY k.category_name",
        cat_chain,
    ).fetchall()
    return render_template(
        "army_view.html",
        army=army, units=units, total=total,
        available_by_tab=available_by_tab, tab_defs=UNIT_TABS,
        faction_keywords=[r["category_name"] for r in kw_rows],
        filter_keyword=keyword, show_legends=show_legends,
    )


@app.route("/army/<int:army_id>/add", methods=["POST"])
@login_required
def army_add_unit(army_id: int):
    uid = require_user_id()
    army = user_db().execute(
        "SELECT * FROM armies WHERE id = ? AND user_id = ?", (army_id, uid),
    ).fetchone()
    if not army:
        abort(404)
    datasheet_id = request.form.get("datasheet_id") or ""
    count = request.form.get("count", type=int) or 1
    ds = user_db().execute(
        "SELECT id, name, points FROM datasheets WHERE id = ?", (datasheet_id,),
    ).fetchone()
    if not ds:
        flash("Invalid unit", "error")
        return redirect(url_for("army_view", army_id=army_id))
    cur = user_db().execute(
        "INSERT INTO army_units (army_id, datasheet_id, datasheet_name, points, count, added_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (army_id, ds["id"], ds["name"], ds["points"], max(1, count),
         datetime.utcnow().isoformat()),
    )
    new_unit_id = cur.lastrowid
    # Auto-fill default loadout choices
    for model_id, slot_id, option_id in get_default_loadout(ds["id"]):
        user_db().execute(
            "INSERT OR IGNORE INTO army_unit_loadout "
            "(army_unit_id, model_id, slot_id, option_id) VALUES (?, ?, ?, ?)",
            (new_unit_id, model_id, slot_id, option_id),
        )
    user_db().commit()
    flash(f"Added {ds['name']} × {count} (defaults applied)", "ok")
    return redirect(url_for("army_view", army_id=army_id))


@app.route("/army/<int:army_id>/unit/<int:unit_id>/loadout", methods=["GET", "POST"])
@login_required
def army_unit_loadout(army_id: int, unit_id: int):
    uid = require_user_id()
    army = user_db().execute(
        "SELECT * FROM armies WHERE id = ? AND user_id = ?", (army_id, uid),
    ).fetchone()
    if not army:
        abort(404)
    unit = user_db().execute(
        "SELECT * FROM army_units WHERE id = ? AND army_id = ?",
        (unit_id, army_id),
    ).fetchone()
    if not unit:
        abort(404)

    # Available pricing tiers for this datasheet (the base is row 0; alternatives follow)
    tiers = user_db().execute(
        "SELECT * FROM pricing_tiers WHERE datasheet_id = ? "
        "ORDER BY condition_value, points",
        (unit["datasheet_id"],),
    ).fetchall()

    if request.method == "POST":
        # 1) Save tier (扩编 size)
        tier_choice = (request.form.get("tier") or "base").strip()
        if tier_choice == "base":
            new_tier_label, new_tier_points = None, None
        else:
            chosen = next((t for t in tiers if str(t["id"]) == tier_choice), None)
            if chosen is None:
                new_tier_label, new_tier_points = None, None
            else:
                new_tier_label = chosen["condition_text"]
                new_tier_points = chosen["points"]
        user_db().execute(
            "UPDATE army_units SET tier_label = ?, tier_points = ? WHERE id = ?",
            (new_tier_label, new_tier_points, unit_id),
        )

        # 2) Save loadout slot choices
        user_db().execute(
            "DELETE FROM army_unit_loadout WHERE army_unit_id = ?", (unit_id,),
        )
        schema = get_loadout_schema(unit["datasheet_id"])
        for m_entry in schema:
            m_id = m_entry["model"]["id"]
            for s_entry in m_entry["choice_slots"]:
                s_id = s_entry["slot"]["id"]
                chosen_opt = request.form.get(f"slot_{s_id}") or ""
                if chosen_opt and any(o["row"]["id"] == chosen_opt for o in s_entry["options"]):
                    user_db().execute(
                        "INSERT INTO army_unit_loadout "
                        "(army_unit_id, model_id, slot_id, option_id) VALUES (?, ?, ?, ?)",
                        (unit_id, m_id, s_id, chosen_opt),
                    )
        user_db().commit()
        flash("Saved", "ok")
        return redirect(url_for("army_view", army_id=army_id))

    schema = get_loadout_schema(unit["datasheet_id"])
    current = fetch_army_unit_loadout(unit_id)
    ds_row = user_db().execute(
        "SELECT name, points FROM datasheets WHERE id = ?", (unit["datasheet_id"],),
    ).fetchone()
    base_points = ds_row["points"] if ds_row else None
    return render_template(
        "army_unit_loadout.html",
        army=army, unit=unit, schema=schema, current=current,
        tiers=tiers, base_points=base_points,
    )


@app.route("/army/<int:army_id>/remove/<int:unit_id>", methods=["POST"])
@login_required
def army_remove_unit(army_id: int, unit_id: int):
    uid = require_user_id()
    army = user_db().execute(
        "SELECT 1 FROM armies WHERE id = ? AND user_id = ?", (army_id, uid),
    ).fetchone()
    if not army:
        abort(404)
    user_db().execute(
        "DELETE FROM army_units WHERE id = ? AND army_id = ?", (unit_id, army_id),
    )
    user_db().commit()
    return redirect(url_for("army_view", army_id=army_id))


@app.route("/army/<int:army_id>/delete", methods=["POST"])
@login_required
def army_delete(army_id: int):
    uid = require_user_id()
    cur = user_db().execute(
        "DELETE FROM armies WHERE id = ? AND user_id = ?", (army_id, uid),
    )
    user_db().commit()
    if cur.rowcount == 0:
        abort(404)
    flash("Army deleted", "ok")
    return redirect(url_for("army_list"))


# ---------------------------------------------------------------------------- #
# Routes — module 3: personal models
# ---------------------------------------------------------------------------- #

STATUS_OPTIONS = [
    ("unpainted", "Unpainted"),
    ("primed", "Primed"),
    ("wip", "In progress"),
    ("done", "Painted"),
    ("based", "Based / Finished"),
]
STATUS_MAP = dict(STATUS_OPTIONS)


def save_uploaded_image(file_storage) -> str | None:
    """Save an uploaded image, normalize extension via Pillow, return filename."""
    if not file_storage or not file_storage.filename:
        return None
    name = secure_filename(file_storage.filename)
    ext = Path(name).suffix.lower()
    if ext not in ALLOWED_EXT:
        return None
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"{uuid.uuid4().hex}{ext}"
    target = UPLOAD_DIR / fname
    file_storage.save(target)
    # Optional: verify it's a real image, re-save to strip metadata, limit size
    try:
        with Image.open(target) as img:
            img.verify()
        with Image.open(target) as img:
            img.thumbnail((1600, 1600))
            img.save(target)
    except Exception:
        target.unlink(missing_ok=True)
        return None
    return fname


# ---------------------------------------------------------------------------- #
# Routes — model lists (collections of personal models)
# ---------------------------------------------------------------------------- #

def _list_cover_url(list_row: sqlite3.Row, db: sqlite3.Connection) -> str | None:
    """Return the URL for a list's cover — explicit cover_filename if set,
    otherwise a random image from one of the list's models. None if empty.
    """
    if list_row["cover_filename"]:
        return url_for("uploaded_file", filename=list_row["cover_filename"])
    img = db.execute("""
        SELECT mi.filename FROM model_images mi
        JOIN models m ON m.id = mi.model_id
        WHERE m.list_id = ?
        ORDER BY RANDOM() LIMIT 1
    """, (list_row["id"],)).fetchone()
    if img:
        return url_for("uploaded_file", filename=img["filename"])
    return None


def _user_can_view_list(list_row: sqlite3.Row) -> bool:
    if list_row["is_public"]:
        return True
    me = current_user()
    return me is not None and me["id"] == list_row["user_id"]


def _ensure_owned_list(list_id: int) -> sqlite3.Row:
    """Fetch a list, abort 404 if it doesn't exist or current user doesn't own it."""
    uid = require_user_id()
    row = user_db().execute(
        "SELECT * FROM model_lists WHERE id = ? AND user_id = ?",
        (list_id, uid),
    ).fetchone()
    if not row:
        abort(404)
    return row


@app.route("/lists")
@login_required
def lists_index():
    uid = require_user_id()
    rows = user_db().execute("""
        SELECT l.*,
               (SELECT COUNT(*) FROM models m WHERE m.list_id = l.id) AS n_models
        FROM model_lists l
        WHERE l.user_id = ? ORDER BY l.updated_at DESC, l.created_at DESC
    """, (uid,)).fetchall()
    db = user_db()
    enriched = [
        {**dict(r), "cover_url": _list_cover_url(r, db)} for r in rows
    ]
    return render_template("lists.html", lists=enriched)


@app.route("/lists/new", methods=["GET", "POST"])
@login_required
def list_new():
    uid = require_user_id()
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        description = (request.form.get("description") or "").strip() or None
        is_public = 1 if request.form.get("is_public") == "1" else 0
        if not name:
            flash("List name is required", "error")
            return redirect(url_for("list_new"))
        now = datetime.utcnow().isoformat()
        cur = user_db().execute(
            "INSERT INTO model_lists (user_id, name, description, is_public, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (uid, name, description, is_public, now, now),
        )
        user_db().commit()
        flash(f"Created list “{name}”", "ok")
        return redirect(url_for("list_view", list_id=cur.lastrowid))
    return render_template("list_new.html")


@app.route("/lists/<int:list_id>")
def list_view(list_id: int):
    list_row = user_db().execute(
        "SELECT l.*, u.username AS owner FROM model_lists l "
        "JOIN users u ON u.id = l.user_id WHERE l.id = ?",
        (list_id,),
    ).fetchone()
    if not list_row:
        abort(404)
    if not _user_can_view_list(list_row):
        abort(404)
    me = current_user()
    is_owner = me is not None and me["id"] == list_row["user_id"]
    models = user_db().execute("""
        SELECT m.*,
               (SELECT filename FROM model_images WHERE model_id = m.id
                ORDER BY id LIMIT 1) AS cover,
               (SELECT COUNT(*) FROM model_images WHERE model_id = m.id) AS n_images
        FROM models m WHERE m.list_id = ? ORDER BY m.created_at DESC
    """, (list_id,)).fetchall()
    return render_template(
        "list_view.html",
        list=list_row, models=models, is_owner=is_owner,
        status_map=STATUS_MAP,
    )


@app.route("/lists/<int:list_id>/edit", methods=["GET", "POST"])
@login_required
def list_edit(list_id: int):
    list_row = _ensure_owned_list(list_id)
    db = user_db()
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        description = (request.form.get("description") or "").strip() or None
        is_public = 1 if request.form.get("is_public") == "1" else 0
        if not name:
            flash("Name is required", "error")
            return redirect(url_for("list_edit", list_id=list_id))
        db.execute(
            "UPDATE model_lists SET name = ?, description = ?, is_public = ?, "
            "updated_at = ? WHERE id = ?",
            (name, description, is_public, datetime.utcnow().isoformat(), list_id),
        )
        db.commit()
        flash("List updated", "ok")
        return redirect(url_for("list_edit", list_id=list_id))
    # Cover is shown + edited via the JS image editor, hitting /cover/replace
    # and /cover/clear directly — no cover_action radio buttons anymore.
    cover_url = _list_cover_url(list_row, db)
    return render_template(
        "list_edit.html", list=list_row, cover_url=cover_url,
    )


@app.route("/lists/<int:list_id>/delete", methods=["POST"])
@login_required
def list_delete(list_id: int):
    list_row = _ensure_owned_list(list_id)
    db = user_db()
    # Cascade: collect every uploaded filename so we can unlink them on disk.
    img_rows = db.execute("""
        SELECT mi.filename FROM model_images mi
        JOIN models m ON m.id = mi.model_id WHERE m.list_id = ?
    """, (list_id,)).fetchall()
    db.execute("DELETE FROM model_lists WHERE id = ?", (list_id,))
    db.commit()
    for r in img_rows:
        try:
            (UPLOAD_DIR / r["filename"]).unlink(missing_ok=True)
        except Exception:
            pass
    if list_row["cover_filename"]:
        try:
            (UPLOAD_DIR / list_row["cover_filename"]).unlink(missing_ok=True)
        except Exception:
            pass
    flash(f"Deleted list “{list_row['name']}” and its {len(img_rows)} image(s)", "ok")
    return redirect(url_for("lists_index"))


@app.route("/models")
@login_required
def models_list():
    """Backward-compat: model index now lives at /lists."""
    return redirect(url_for("lists_index"))


def _save_model_loadout(model_id: int, datasheet_id: str, form) -> None:
    """Validate and persist model loadout choices for the model's picked model_type."""
    schema = get_loadout_schema(datasheet_id)
    picked_type = form.get("model_type_id") or ""
    # Clear existing
    user_db().execute("DELETE FROM model_loadout WHERE model_id = ?", (model_id,))
    for m_entry in schema:
        if m_entry["model"]["id"] != picked_type:
            continue
        for s_entry in m_entry["choice_slots"]:
            s_id = s_entry["slot"]["id"]
            chosen = form.get(f"slot_{s_id}") or ""
            if not chosen:
                continue
            if any(o["row"]["id"] == chosen for o in s_entry["options"]):
                user_db().execute(
                    "INSERT INTO model_loadout (model_id, slot_id, option_id) "
                    "VALUES (?, ?, ?)",
                    (model_id, s_id, chosen),
                )


@app.route("/models/new", methods=["GET", "POST"])
@login_required
def model_new():
    uid = require_user_id()
    # list_id must be present and owned by the current user.
    list_id_raw = request.values.get("list") or request.values.get("list_id") or ""
    try:
        list_id = int(list_id_raw)
    except ValueError:
        flash("Pick a list to add this model to", "error")
        return redirect(url_for("lists_index"))
    target_list = user_db().execute(
        "SELECT id, name FROM model_lists WHERE id = ? AND user_id = ?",
        (list_id, uid),
    ).fetchone()
    if not target_list:
        flash("List not found", "error")
        return redirect(url_for("lists_index"))
    if request.method == "POST":
        datasheet_id = request.form.get("datasheet_id") or ""
        model_type_id = request.form.get("model_type_id") or ""
        custom_name = (request.form.get("custom_name") or "").strip()
        status = request.form.get("status") or "unpainted"
        notes = (request.form.get("notes") or "").strip()
        ds = user_db().execute("""
            SELECT d.id, d.name, c.name AS catalogue_name
            FROM datasheets d JOIN catalogues c ON c.id = d.catalogue_id
            WHERE d.id = ?
        """, (datasheet_id,)).fetchone()
        if not ds:
            flash("Please pick a valid datasheet", "error")
            return redirect(url_for("model_new", list=list_id))
        # If no model_type picked, default to the first model type of this datasheet
        if not model_type_id:
            first_m = user_db().execute(
                "SELECT id FROM unit_models WHERE datasheet_id = ? "
                "ORDER BY sort_order LIMIT 1",
                (ds["id"],),
            ).fetchone()
            if first_m:
                model_type_id = first_m["id"]
        now = datetime.utcnow().isoformat()
        cur = user_db().execute(
            "INSERT INTO models (custom_name, datasheet_id, datasheet_name, faction_name, "
            "status, notes, model_type_id, created_at, updated_at, user_id, list_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (custom_name, ds["id"], ds["name"], ds["catalogue_name"],
             status, notes, model_type_id, now, now, uid, list_id),
        )
        model_id = cur.lastrowid
        _save_model_loadout(model_id, ds["id"], request.form)
        # Single image per model. Client crops/rotates in the modal before
        # submission; we just persist what arrives.
        fs = request.files.get("image")
        if fs and fs.filename:
            fn = save_uploaded_image(fs)
            if fn:
                user_db().execute(
                    "INSERT INTO model_images (model_id, filename, uploaded_at) "
                    "VALUES (?, ?, ?)",
                    (model_id, fn, now),
                )
        # Bump the list's updated_at so it sorts to the top of /lists.
        user_db().execute(
            "UPDATE model_lists SET updated_at = ? WHERE id = ?", (now, list_id),
        )
        user_db().commit()
        flash("Registered", "ok")
        # Back to the list view after creating — list-centric workflow.
        return redirect(url_for("list_view", list_id=list_id))
    return render_template(
        "model_new.html",
        factions=list_factions(),
        status_options=STATUS_OPTIONS,
        preselect_ds=request.args.get("ds") or "",
        target_list=target_list,
    )


@app.route("/api/datasheet/<datasheet_id>/loadout")
def api_datasheet_loadout(datasheet_id: str):
    """Return composition + choice slots for a datasheet, used by the model registration form."""
    schema = get_loadout_schema(datasheet_id)
    return {
        "models": [
            {
                "id": m["model"]["id"],
                "name": m["model"]["name"],
                "min_count": m["model"]["min_count"],
                "max_count": m["model"]["max_count"],
                "choice_slots": [
                    {
                        "id": s["slot"]["id"],
                        "name": s["slot"]["slot_name"],
                        "min_select": s["slot"]["min_select"],
                        "max_select": s["slot"]["max_select"],
                        "options": [
                            {
                                "id": o["row"]["id"],
                                "name": o["row"]["name"],
                                "is_default": bool(o["row"]["is_default"]),
                                "weapons": [
                                    {"name": w["name"], "type": w["weapon_type"]}
                                    for w in o["weapons"]
                                ],
                            }
                            for o in s["options"]
                        ],
                    }
                    for s in m["choice_slots"]
                ],
            }
            for m in schema
        ]
    }


@app.route("/api/datasheets")
def api_datasheets():
    """Used by model_new and army to populate the datasheet picker."""
    catalogue_id = request.args.get("catalogue") or ""
    q = (request.args.get("q") or "").strip()
    sql = "SELECT d.id, d.name, d.points, c.name AS catalogue_name FROM datasheets d JOIN catalogues c ON c.id = d.catalogue_id WHERE 1=1"
    params: list = []
    if catalogue_id:
        sql += " AND d.catalogue_id = ?"
        params.append(catalogue_id)
    if q:
        sql += " AND d.name LIKE ?"
        params.append(f"%{q}%")
    sql += " ORDER BY c.name, d.name LIMIT 200"
    rows = user_db().execute(sql, params).fetchall()
    return {"items": [dict(r) for r in rows]}


@app.route("/models/<int:model_id>")
@login_required
def model_detail(model_id: int):
    uid = require_user_id()
    m = user_db().execute(
        "SELECT * FROM models WHERE id = ? AND user_id = ?", (model_id, uid),
    ).fetchone()
    if not m:
        abort(404)
    imgs = user_db().execute(
        "SELECT * FROM model_images WHERE model_id = ? ORDER BY id", (model_id,),
    ).fetchall()
    # Loadout schema (for the edit form) + current choices
    schema = get_loadout_schema(m["datasheet_id"])
    current = fetch_model_loadout(model_id)
    # Resolve model_type → row for display
    model_type_row = None
    if m["model_type_id"]:
        model_type_row = user_db().execute(
            "SELECT * FROM unit_models WHERE id = ?", (m["model_type_id"],),
        ).fetchone()
    # Compose loadout summary for display
    loadout_summary = []
    if current:
        db = user_db()
        for slot_id, option_id in current.items():
            row = db.execute("""
                SELECT o.name AS option_name, s.slot_name
                FROM loadout_options o JOIN loadout_slots s ON s.id = o.slot_id
                WHERE o.id = ?
            """, (option_id,)).fetchone()
            if row:
                loadout_summary.append({
                    "slot_name": row["slot_name"],
                    "option_name": row["option_name"],
                })
    parent_list = None
    if m["list_id"] is not None:
        parent_list = user_db().execute(
            "SELECT id, name FROM model_lists WHERE id = ?", (m["list_id"],),
        ).fetchone()
    return render_template(
        "model_detail.html", m=m, imgs=imgs,
        status_options=STATUS_OPTIONS, status_map=STATUS_MAP,
        schema=schema, current=current, model_type_row=model_type_row,
        loadout_summary=loadout_summary, parent_list=parent_list,
    )


@app.route("/models/<int:model_id>/edit", methods=["POST"])
@login_required
def model_edit(model_id: int):
    uid = require_user_id()
    m = user_db().execute(
        "SELECT * FROM models WHERE id = ? AND user_id = ?", (model_id, uid),
    ).fetchone()
    if not m:
        abort(404)
    custom_name = (request.form.get("custom_name") or "").strip()
    status = request.form.get("status") or m["status"]
    notes = (request.form.get("notes") or "").strip()
    model_type_id = request.form.get("model_type_id") or m["model_type_id"]
    now = datetime.utcnow().isoformat()
    user_db().execute(
        "UPDATE models SET custom_name = ?, status = ?, notes = ?, "
        "model_type_id = ?, updated_at = ? WHERE id = ?",
        (custom_name, status, notes, model_type_id, now, model_id),
    )
    _save_model_loadout(model_id, m["datasheet_id"], request.form)
    # Single image per model: a new upload wipes any previous images first.
    fs = request.files.get("image")
    if fs and fs.filename:
        fn = save_uploaded_image(fs)
        if fn:
            old = user_db().execute(
                "SELECT filename FROM model_images WHERE model_id = ?", (model_id,),
            ).fetchall()
            user_db().execute(
                "DELETE FROM model_images WHERE model_id = ?", (model_id,),
            )
            for r in old:
                try:
                    (UPLOAD_DIR / r["filename"]).unlink(missing_ok=True)
                except Exception:
                    pass
            user_db().execute(
                "INSERT INTO model_images (model_id, filename, uploaded_at) "
                "VALUES (?, ?, ?)",
                (model_id, fn, now),
            )
    user_db().commit()
    flash("Saved", "ok")
    # Land on the list view after Save (list-centric workflow). If the model
    # is somehow unlisted, fall back to the lists index.
    if m["list_id"]:
        return redirect(url_for("list_view", list_id=m["list_id"]))
    return redirect(url_for("lists_index"))


@app.route("/models/<int:model_id>/delete", methods=["POST"])
@login_required
def model_delete(model_id: int):
    uid = require_user_id()
    m = user_db().execute(
        "SELECT 1 FROM models WHERE id = ? AND user_id = ?", (model_id, uid),
    ).fetchone()
    if not m:
        abort(404)
    imgs = user_db().execute(
        "SELECT filename FROM model_images WHERE model_id = ?", (model_id,),
    ).fetchall()
    user_db().execute("DELETE FROM models WHERE id = ?", (model_id,))
    user_db().commit()
    for r in imgs:
        try:
            (UPLOAD_DIR / r["filename"]).unlink(missing_ok=True)
        except Exception:
            pass
    flash("Deleted", "ok")
    return redirect(url_for("models_list"))


@app.route("/models/<int:model_id>/image/clear", methods=["POST"])
@login_required
def model_image_clear(model_id: int):
    """Remove every image attached to this model (one-image-per-model UX)."""
    uid = require_user_id()
    owner = user_db().execute(
        "SELECT 1 FROM models WHERE id = ? AND user_id = ?", (model_id, uid),
    ).fetchone()
    if not owner:
        abort(404)
    rows = user_db().execute(
        "SELECT filename FROM model_images WHERE model_id = ?", (model_id,),
    ).fetchall()
    user_db().execute("DELETE FROM model_images WHERE model_id = ?", (model_id,))
    user_db().commit()
    for r in rows:
        try:
            (UPLOAD_DIR / r["filename"]).unlink(missing_ok=True)
        except Exception:
            pass
    flash("Image cleared", "ok")
    return redirect(url_for("model_detail", model_id=model_id))


# ---------------------------------------------------------------------------- #
# Routes — auth (register / login / logout / profile)
# ---------------------------------------------------------------------------- #

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")


def _claim_legacy_data(conn: sqlite3.Connection, user_id: int) -> int:
    """Bind every armies/models row with NULL user_id to user_id.

    Only ever runs for the first registered user (the bootstrap admin) because
    every subsequent insert sets user_id explicitly.
    """
    cur = conn.execute(
        "UPDATE armies SET user_id = ? WHERE user_id IS NULL", (user_id,),
    )
    n = cur.rowcount or 0
    cur = conn.execute(
        "UPDATE models SET user_id = ? WHERE user_id IS NULL", (user_id,),
    )
    n += cur.rowcount or 0
    return n


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user() is not None:
        return redirect(url_for("index"))
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        confirm  = request.form.get("confirm") or ""
        if not USERNAME_RE.match(username):
            flash("Username must be 3–32 chars: letters, digits, _ - .", "error")
            return redirect(url_for("register"))
        if len(password) < 8:
            flash("Password must be at least 8 characters", "error")
            return redirect(url_for("register"))
        if password != confirm:
            flash("Passwords do not match", "error")
            return redirect(url_for("register"))
        db = user_db()
        if db.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
            flash("Username is already taken", "error")
            return redirect(url_for("register"))
        # First-ever registration → admin, and takes ownership of legacy data.
        is_first = db.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] == 0
        role = "admin" if is_first else "user"
        cur = db.execute(
            "INSERT INTO users (username, password_hash, role, is_public, created_at) "
            "VALUES (?, ?, ?, 0, ?)",
            (username, generate_password_hash(password), role,
             datetime.utcnow().isoformat()),
        )
        uid = cur.lastrowid
        claimed = _claim_legacy_data(db, uid) if is_first else 0
        db.commit()
        session["user_id"] = uid
        if is_first:
            flash(f"Welcome, {username}! As the first registered user you are now "
                  f"an admin, and have inherited {claimed} legacy record(s).", "ok")
        else:
            flash(f"Registered. Welcome, {username}.", "ok")
        return redirect(url_for("index"))
    # Show whether this will be the bootstrap admin to give the UI a hint.
    is_bootstrap = user_db().execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] == 0
    return render_template("register.html", is_bootstrap=is_bootstrap)


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user() is not None:
        return redirect(url_for("index"))
    next_url = request.args.get("next") or request.form.get("next") or ""
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        row = user_db().execute(
            "SELECT * FROM users WHERE username = ?", (username,),
        ).fetchone()
        if row is None or not check_password_hash(row["password_hash"], password):
            flash("Invalid username or password", "error")
            return redirect(url_for("login", next=next_url))
        session["user_id"] = row["id"]
        flash(f"Welcome back, {row['username']}", "ok")
        # next must be a local path to avoid open-redirects.
        if next_url.startswith("/") and not next_url.startswith("//"):
            return redirect(next_url)
        return redirect(url_for("index"))
    return render_template("login.html", next=next_url)


@app.route("/logout", methods=["POST"])
def logout():
    session.pop("user_id", None)
    flash("Logged out", "ok")
    return redirect(url_for("index"))


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    me = current_user()
    if request.method == "POST":
        action = request.form.get("action") or ""
        db = user_db()
        if action == "visibility":
            new_val = 1 if request.form.get("is_public") == "1" else 0
            db.execute("UPDATE users SET is_public = ? WHERE id = ?", (new_val, me["id"]))
            db.commit()
            flash("Profile is now " + ("public" if new_val else "private"), "ok")
        elif action == "password":
            old_pw = request.form.get("old_password") or ""
            new_pw = request.form.get("new_password") or ""
            confirm = request.form.get("confirm_password") or ""
            if not check_password_hash(me["password_hash"], old_pw):
                flash("Current password is incorrect", "error")
            elif len(new_pw) < 8:
                flash("New password must be at least 8 characters", "error")
            elif new_pw != confirm:
                flash("New passwords do not match", "error")
            else:
                db.execute(
                    "UPDATE users SET password_hash = ? WHERE id = ?",
                    (generate_password_hash(new_pw), me["id"]),
                )
                db.commit()
                flash("Password updated", "ok")
        return redirect(url_for("profile"))
    n_armies = user_db().execute(
        "SELECT COUNT(*) AS n FROM armies WHERE user_id = ?", (me["id"],),
    ).fetchone()["n"]
    n_models = user_db().execute(
        "SELECT COUNT(*) AS n FROM models WHERE user_id = ?", (me["id"],),
    ).fetchone()["n"]
    return render_template("profile.html", n_armies=n_armies, n_models=n_models)


# ---------------------------------------------------------------------------- #
# Routes — public browse (/users + /u/<username>)
# ---------------------------------------------------------------------------- #

def _get_public_user(username: str) -> sqlite3.Row:
    row = user_db().execute(
        "SELECT * FROM users WHERE username = ? AND is_public = 1", (username,),
    ).fetchone()
    if row is None:
        abort(404)
    return row


@app.route("/users")
def users_browse():
    q = (request.args.get("q") or "").strip()
    sql = ("SELECT u.id, u.username, u.created_at, "
           "(SELECT COUNT(*) FROM armies WHERE user_id = u.id) AS n_armies, "
           "(SELECT COUNT(*) FROM models WHERE user_id = u.id) AS n_models "
           "FROM users u WHERE u.is_public = 1")
    params: list = []
    if q:
        sql += " AND u.username LIKE ?"
        params.append(f"%{q}%")
    sql += " ORDER BY u.username COLLATE NOCASE"
    rows = user_db().execute(sql, params).fetchall()
    return render_template("users_browse.html", users=rows, q=q)


@app.route("/u/<username>")
def public_profile(username: str):
    user = _get_public_user(username)
    db = user_db()
    armies = db.execute("""
        SELECT a.*,
               COALESCE(SUM(COALESCE(pt.points, d.points) * u.count), 0) AS total_pts,
               COUNT(u.id) AS n_units
        FROM armies a
          LEFT JOIN army_units u ON u.army_id = a.id
          LEFT JOIN datasheets d ON d.id = u.datasheet_id
          LEFT JOIN pricing_tiers pt ON pt.datasheet_id = u.datasheet_id
                                    AND pt.condition_text = u.tier_label
        WHERE a.user_id = ?
        GROUP BY a.id ORDER BY a.created_at DESC
    """, (user["id"],)).fetchall()
    list_rows = db.execute("""
        SELECT l.*,
               (SELECT COUNT(*) FROM models m WHERE m.list_id = l.id) AS n_models
        FROM model_lists l
        WHERE l.user_id = ? AND l.is_public = 1
        ORDER BY l.updated_at DESC
    """, (user["id"],)).fetchall()
    public_lists = [
        {**dict(r), "cover_url": _list_cover_url(r, db)} for r in list_rows
    ]
    return render_template(
        "u_profile.html", user=user, armies=armies, public_lists=public_lists,
    )


@app.route("/u/<username>/army/<int:army_id>")
def public_army(username: str, army_id: int):
    user = _get_public_user(username)
    army = user_db().execute(
        "SELECT * FROM armies WHERE id = ? AND user_id = ?", (army_id, user["id"]),
    ).fetchone()
    if not army:
        abort(404)
    unit_rows = user_db().execute("""
        SELECT u.*,
               d.points  AS live_base_points,
               pt.points AS live_tier_points
        FROM army_units u
          LEFT JOIN datasheets d ON d.id = u.datasheet_id
          LEFT JOIN pricing_tiers pt ON pt.datasheet_id = u.datasheet_id
                                    AND pt.condition_text = u.tier_label
        WHERE u.army_id = ? ORDER BY u.added_at
    """, (army_id,)).fetchall()
    units, total = [], 0
    for r in unit_rows:
        d = dict(r)
        d["effective_points"] = (d["live_tier_points"] if d["tier_label"] is not None
                                 else d["live_base_points"])
        total += (d["effective_points"] or 0) * d["count"]
        units.append(d)
    return render_template(
        "public_army.html", user=user, army=army, units=units, total=total,
    )


@app.route("/u/<username>/model/<int:model_id>")
def public_model(username: str, model_id: int):
    user = _get_public_user(username)
    m = user_db().execute(
        "SELECT * FROM models WHERE id = ? AND user_id = ?", (model_id, user["id"]),
    ).fetchone()
    if not m:
        abort(404)
    imgs = user_db().execute(
        "SELECT * FROM model_images WHERE model_id = ? ORDER BY id", (model_id,),
    ).fetchall()
    return render_template(
        "public_model.html", user=user, m=m, imgs=imgs, status_map=STATUS_MAP,
    )


# ---------------------------------------------------------------------------- #
# Routes — admin
# ---------------------------------------------------------------------------- #

# Editable fields per entity. Centralised so the edit form and the UPDATE
# statement always stay in lockstep — adding a new editable column means
# adding it here and adding the form input.
UNIT_MODEL_FIELDS = ("name", "m", "t", "sv", "w", "ld", "oc",
                     "min_count", "max_count")
WEAPON_FIELDS = ("name", "weapon_type", "range_", "a", "bs_ws", "s", "ap", "d",
                 "keywords")


def _new_id() -> str:
    """KB-style id: 16 hex chars in 4-4-4-4 grouping. Matches BSData format."""
    h = uuid.uuid4().hex[:16]
    return f"{h[:4]}-{h[4:8]}-{h[8:12]}-{h[12:16]}"


def _cascade_delete_option(conn: sqlite3.Connection, option_id: str) -> None:
    conn.execute("DELETE FROM loadout_option_weapons WHERE option_id = ?", (option_id,))
    conn.execute("DELETE FROM loadout_options WHERE id = ?", (option_id,))


def _cascade_delete_slot(conn: sqlite3.Connection, slot_id: str) -> None:
    for o in conn.execute(
        "SELECT id FROM loadout_options WHERE slot_id = ?", (slot_id,),
    ).fetchall():
        _cascade_delete_option(conn, o["id"])
    conn.execute("DELETE FROM loadout_slots WHERE id = ?", (slot_id,))


def _cascade_delete_model(conn: sqlite3.Connection, model_id: str) -> None:
    for s in conn.execute(
        "SELECT id FROM loadout_slots WHERE model_id = ?", (model_id,),
    ).fetchall():
        _cascade_delete_slot(conn, s["id"])
    conn.execute("DELETE FROM unit_models WHERE id = ?", (model_id,))


def _cascade_delete_datasheet(conn: sqlite3.Connection, ds_id: str) -> None:
    for m in conn.execute(
        "SELECT id FROM unit_models WHERE datasheet_id = ?", (ds_id,),
    ).fetchall():
        _cascade_delete_model(conn, m["id"])
    # Weapons owned by this datasheet are nuked too. Loadouts on OTHER datasheets
    # that referenced these weapons will have dangling option_weapons; we clean
    # those up via _cascade_delete_weapon.
    for w in conn.execute(
        "SELECT profile_id FROM weapons WHERE datasheet_id = ?", (ds_id,),
    ).fetchall():
        _cascade_delete_weapon(conn, w["profile_id"])
    conn.execute("DELETE FROM abilities WHERE datasheet_id = ?", (ds_id,))
    conn.execute("DELETE FROM datasheet_keywords WHERE datasheet_id = ?", (ds_id,))
    conn.execute("DELETE FROM transport WHERE datasheet_id = ?", (ds_id,))
    conn.execute("DELETE FROM pricing_tiers WHERE datasheet_id = ?", (ds_id,))
    conn.execute("DELETE FROM datasheets WHERE id = ?", (ds_id,))


def _cascade_delete_weapon(conn: sqlite3.Connection, profile_id: str) -> None:
    conn.execute(
        "DELETE FROM loadout_option_weapons WHERE weapon_profile_id = ?",
        (profile_id,),
    )
    conn.execute("DELETE FROM weapons WHERE profile_id = ?", (profile_id,))


@app.route("/admin")
@admin_required
def admin_home():
    db = user_db()
    counts = {
        "users": db.execute("SELECT COUNT(*) FROM users").fetchone()[0],
        "datasheets": db.execute("SELECT COUNT(*) FROM datasheets").fetchone()[0],
        "weapons": db.execute("SELECT COUNT(*) FROM weapons").fetchone()[0],
        "unit_models": db.execute("SELECT COUNT(*) FROM unit_models").fetchone()[0],
    }
    return render_template("admin_home.html", counts=counts)


@app.route("/admin/units")
@admin_required
def admin_units():
    q = (request.args.get("q") or "").strip()
    catalogue_id = (request.args.get("catalogue") or "").strip()
    where, params = [], []
    if q:
        where.append("d.name LIKE ?")
        params.append(f"%{q}%")
    if catalogue_id:
        where.append("d.catalogue_id = ?")
        params.append(catalogue_id)
    sql = ("SELECT d.id, d.name, d.points, d.entry_type, c.name AS catalogue_name "
           "FROM datasheets d JOIN catalogues c ON c.id = d.catalogue_id")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY c.name, d.name LIMIT 500"
    rows = user_db().execute(sql, params).fetchall()
    return render_template(
        "admin_units.html",
        rows=rows, q=q, catalogue_id=catalogue_id, factions=list_factions(),
    )


@app.route("/admin/units/<datasheet_id>", methods=["GET", "POST"])
@admin_required
def admin_unit_edit(datasheet_id: str):
    db = user_db()
    ds = db.execute(
        "SELECT d.*, c.name AS catalogue_name FROM datasheets d "
        "JOIN catalogues c ON c.id = d.catalogue_id WHERE d.id = ?",
        (datasheet_id,),
    ).fetchone()
    if not ds:
        abort(404)

    if request.method == "POST":
        # Datasheet-level edits: name + base points.
        new_name = (request.form.get("ds_name") or "").strip()
        raw_pts = (request.form.get("ds_points") or "").strip()
        try:
            new_pts = int(raw_pts) if raw_pts else None
            if new_pts is not None and new_pts < 0:
                raise ValueError
        except ValueError:
            flash("Points must be a non-negative integer", "error")
            return redirect(url_for("admin_unit_edit", datasheet_id=datasheet_id))
        if not new_name:
            flash("Name is required", "error")
            return redirect(url_for("admin_unit_edit", datasheet_id=datasheet_id))
        db.execute(
            "UPDATE datasheets SET name = ?, points = ? WHERE id = ?",
            (new_name, new_pts, datasheet_id),
        )

        # Per-model-type stat edits. Form fields are namespaced m_<model_id>_<field>.
        model_rows = db.execute(
            "SELECT id FROM unit_models WHERE datasheet_id = ?", (datasheet_id,),
        ).fetchall()
        for m in model_rows:
            updates, vals = [], []
            for f in UNIT_MODEL_FIELDS:
                key = f"m_{m['id']}_{f}"
                if key not in request.form:
                    continue
                raw = request.form.get(key) or ""
                if f in ("min_count", "max_count"):
                    val = int(raw) if raw.strip() else None
                else:
                    val = raw.strip() or None
                updates.append(f"{f} = ?")
                vals.append(val)
            if updates:
                vals.append(m["id"])
                db.execute(
                    f"UPDATE unit_models SET {', '.join(updates)} WHERE id = ?",
                    vals,
                )

        # Pricing-tier edits. Form fields: tier_<id>_points.
        tier_rows = db.execute(
            "SELECT id FROM pricing_tiers WHERE datasheet_id = ?", (datasheet_id,),
        ).fetchall()
        for t in tier_rows:
            key = f"tier_{t['id']}_points"
            if key in request.form:
                raw = (request.form.get(key) or "").strip()
                try:
                    pts = int(raw) if raw else None
                except ValueError:
                    flash("Tier points must be an integer", "error")
                    return redirect(url_for("admin_unit_edit", datasheet_id=datasheet_id))
                db.execute(
                    "UPDATE pricing_tiers SET points = ? WHERE id = ?", (pts, t["id"]),
                )
        db.commit()
        flash(f"Saved changes to {new_name}", "ok")
        return redirect(url_for("admin_unit_edit", datasheet_id=datasheet_id))

    models = db.execute(
        "SELECT * FROM unit_models WHERE datasheet_id = ? ORDER BY sort_order",
        (datasheet_id,),
    ).fetchall()
    tiers = db.execute(
        "SELECT * FROM pricing_tiers WHERE datasheet_id = ? "
        "ORDER BY condition_value, points",
        (datasheet_id,),
    ).fetchall()
    abilities = db.execute(
        "SELECT * FROM abilities WHERE datasheet_id = ? ORDER BY ability_type, name",
        (datasheet_id,),
    ).fetchall()
    keywords = db.execute(
        "SELECT * FROM datasheet_keywords WHERE datasheet_id = ? "
        "ORDER BY is_primary DESC, category_name",
        (datasheet_id,),
    ).fetchall()
    transport = db.execute(
        "SELECT * FROM transport WHERE datasheet_id = ?", (datasheet_id,),
    ).fetchall()
    return render_template(
        "admin_unit_edit.html", ds=ds, models=models, tiers=tiers,
        abilities=abilities, keywords=keywords, transport=transport,
        unit_model_fields=UNIT_MODEL_FIELDS,
    )


@app.route("/admin/weapons")
@admin_required
def admin_weapons():
    q = (request.args.get("q") or "").strip()
    datasheet_id = (request.args.get("datasheet") or "").strip()
    where, params = [], []
    if q:
        where.append("w.name LIKE ?")
        params.append(f"%{q}%")
    if datasheet_id:
        where.append("w.datasheet_id = ?")
        params.append(datasheet_id)
    sql = ("SELECT w.profile_id, w.name, w.weapon_type, w.range_, w.a, "
           "w.bs_ws, w.s, w.ap, w.d, w.datasheet_id, "
           "d.name AS ds_name "
           "FROM weapons w LEFT JOIN datasheets d ON d.id = w.datasheet_id")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY d.name, w.name LIMIT 500"
    rows = user_db().execute(sql, params).fetchall()
    return render_template(
        "admin_weapons.html",
        rows=rows, q=q, datasheet_id=datasheet_id,
    )


@app.route("/admin/weapons/<profile_id>", methods=["GET", "POST"])
@admin_required
def admin_weapon_edit(profile_id: str):
    db = user_db()
    w = db.execute("SELECT * FROM weapons WHERE profile_id = ?", (profile_id,)).fetchone()
    if not w:
        abort(404)
    if request.method == "POST":
        updates, vals = [], []
        for f in WEAPON_FIELDS:
            raw = request.form.get(f"w_{f}", "").strip()
            updates.append(f"{f} = ?")
            vals.append(raw or None)
        vals.append(profile_id)
        db.execute(
            f"UPDATE weapons SET {', '.join(updates)} WHERE profile_id = ?", vals,
        )
        db.commit()
        flash(f"Saved {request.form.get('w_name') or w['name']}", "ok")
        return redirect(url_for("admin_weapon_edit", profile_id=profile_id))
    ds = None
    if w["datasheet_id"]:
        ds = db.execute(
            "SELECT id, name FROM datasheets WHERE id = ?", (w["datasheet_id"],),
        ).fetchone()
    return render_template(
        "admin_weapon_edit.html", w=w, ds=ds, weapon_fields=WEAPON_FIELDS,
    )


@app.route("/admin/users")
@admin_required
def admin_users():
    rows = user_db().execute(
        "SELECT u.id, u.username, u.role, u.is_public, u.created_at, "
        "(SELECT COUNT(*) FROM armies WHERE user_id = u.id) AS n_armies, "
        "(SELECT COUNT(*) FROM models WHERE user_id = u.id) AS n_models "
        "FROM users u ORDER BY u.created_at"
    ).fetchall()
    return render_template("admin_users.html", users=rows)


@app.route("/admin/users/<int:user_id>/reset-password", methods=["POST"])
@admin_required
def admin_reset_password(user_id: int):
    target = user_db().execute(
        "SELECT * FROM users WHERE id = ?", (user_id,),
    ).fetchone()
    if not target:
        abort(404)
    new_pw = request.form.get("new_password") or ""
    if len(new_pw) < 8:
        flash("New password must be at least 8 characters", "error")
        return redirect(url_for("admin_users"))
    user_db().execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (generate_password_hash(new_pw), user_id),
    )
    user_db().commit()
    flash(f"Reset password for {target['username']}", "ok")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/role", methods=["POST"])
@admin_required
def admin_set_role(user_id: int):
    target = user_db().execute(
        "SELECT * FROM users WHERE id = ?", (user_id,),
    ).fetchone()
    if not target:
        abort(404)
    new_role = request.form.get("role") or "user"
    if new_role not in ("user", "admin"):
        abort(400)
    me = current_user()
    if target["id"] == me["id"] and new_role != "admin":
        # Don't let the last admin demote themselves and lock everyone out.
        n_admins = user_db().execute(
            "SELECT COUNT(*) AS n FROM users WHERE role = 'admin'"
        ).fetchone()["n"]
        if n_admins <= 1:
            flash("Cannot demote the last admin", "error")
            return redirect(url_for("admin_users"))
    user_db().execute(
        "UPDATE users SET role = ? WHERE id = ?", (new_role, user_id),
    )
    user_db().commit()
    flash(f"Set {target['username']} to {new_role}", "ok")
    return redirect(url_for("admin_users"))


# ---------------------------------------------------------------------------- #
# Routes — admin: create new + delete datasheets / weapons
# ---------------------------------------------------------------------------- #

# ---------------------------------------------------------------------------- #
# Routes — admin: catalogues + inheritance
# ---------------------------------------------------------------------------- #

@app.route("/admin/catalogues")
@admin_required
def admin_catalogues():
    db = user_db()
    rows = db.execute("""
        SELECT c.id, c.name, c.is_library,
               (SELECT COUNT(*) FROM datasheets d WHERE d.catalogue_id = c.id) AS own,
               (SELECT COUNT(*) FROM datasheets d
                JOIN catalogue_inherits ci ON ci.parent_id = d.catalogue_id
                WHERE ci.child_id = c.id) AS inherited,
               (SELECT GROUP_CONCAT(c2.name, ' | ')
                FROM catalogue_inherits ci
                JOIN catalogues c2 ON c2.id = ci.parent_id
                WHERE ci.child_id = c.id) AS parents
        FROM catalogues c ORDER BY c.is_library, c.name
    """).fetchall()
    return render_template("admin_catalogues.html", rows=rows)


@app.route("/admin/catalogues/<catalogue_id>", methods=["GET", "POST"])
@admin_required
def admin_catalogue_edit(catalogue_id: str):
    db = user_db()
    cat = db.execute("SELECT * FROM catalogues WHERE id = ?", (catalogue_id,)).fetchone()
    if not cat:
        abort(404)
    if request.method == "POST":
        new_name = (request.form.get("name") or "").strip()
        is_library = 1 if request.form.get("is_library") == "1" else 0
        if not new_name:
            flash("Name is required", "error")
            return redirect(url_for("admin_catalogue_edit", catalogue_id=catalogue_id))
        db.execute(
            "UPDATE catalogues SET name = ?, is_library = ? WHERE id = ?",
            (new_name, is_library, catalogue_id),
        )
        db.commit()
        flash("Catalogue updated", "ok")
        return redirect(url_for("admin_catalogue_edit", catalogue_id=catalogue_id))
    parents = db.execute("""
        SELECT c.id, c.name FROM catalogue_inherits ci
        JOIN catalogues c ON c.id = ci.parent_id
        WHERE ci.child_id = ? ORDER BY c.name
    """, (catalogue_id,)).fetchall()
    # Candidates for adding as a parent: anything that isn't self and isn't
    # already a parent. We don't try to prevent cycles in the form — the chain
    # walker has a `seen` set so cycles can't crash queries either way.
    existing_parent_ids = {p["id"] for p in parents}
    all_others = db.execute(
        "SELECT id, name, is_library FROM catalogues WHERE id != ? ORDER BY name",
        (catalogue_id,),
    ).fetchall()
    candidates = [c for c in all_others if c["id"] not in existing_parent_ids]
    own_n = db.execute(
        "SELECT COUNT(*) AS n FROM datasheets WHERE catalogue_id = ?", (catalogue_id,),
    ).fetchone()["n"]
    inherited_n = db.execute("""
        SELECT COUNT(*) AS n FROM datasheets d
        JOIN catalogue_inherits ci ON ci.parent_id = d.catalogue_id
        WHERE ci.child_id = ?
    """, (catalogue_id,)).fetchone()["n"]
    return render_template(
        "admin_catalogue_edit.html",
        cat=cat, parents=parents, candidates=candidates,
        own_n=own_n, inherited_n=inherited_n,
    )


@app.route("/admin/catalogues/<catalogue_id>/inherits/add", methods=["POST"])
@admin_required
def admin_catalogue_inherit_add(catalogue_id: str):
    db = user_db()
    if not db.execute("SELECT 1 FROM catalogues WHERE id = ?", (catalogue_id,)).fetchone():
        abort(404)
    parent_id = (request.form.get("parent_id") or "").strip()
    if not parent_id or parent_id == catalogue_id:
        flash("Pick a different catalogue as the parent", "error")
        return redirect(url_for("admin_catalogue_edit", catalogue_id=catalogue_id))
    if not db.execute(
        "SELECT 1 FROM catalogues WHERE id = ?", (parent_id,),
    ).fetchone():
        flash("Parent catalogue not found", "error")
        return redirect(url_for("admin_catalogue_edit", catalogue_id=catalogue_id))
    db.execute(
        "INSERT OR IGNORE INTO catalogue_inherits (child_id, parent_id) VALUES (?, ?)",
        (catalogue_id, parent_id),
    )
    db.commit()
    flash("Inheritance added", "ok")
    return redirect(url_for("admin_catalogue_edit", catalogue_id=catalogue_id))


@app.route("/admin/catalogues/<catalogue_id>/inherits/<parent_id>/remove",
           methods=["POST"])
@admin_required
def admin_catalogue_inherit_remove(catalogue_id: str, parent_id: str):
    user_db().execute(
        "DELETE FROM catalogue_inherits WHERE child_id = ? AND parent_id = ?",
        (catalogue_id, parent_id),
    )
    user_db().commit()
    flash("Inheritance removed", "ok")
    return redirect(url_for("admin_catalogue_edit", catalogue_id=catalogue_id))


@app.route("/admin/units/new", methods=["GET", "POST"])
@admin_required
def admin_unit_new():
    db = user_db()
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        catalogue_id = (request.form.get("catalogue_id") or "").strip()
        entry_type = (request.form.get("entry_type") or "").strip() or None
        raw_pts = (request.form.get("points") or "").strip()
        if not name or not catalogue_id:
            flash("Name and faction are required", "error")
            return redirect(url_for("admin_unit_new"))
        if not db.execute("SELECT 1 FROM catalogues WHERE id = ?", (catalogue_id,)).fetchone():
            flash("Invalid faction", "error")
            return redirect(url_for("admin_unit_new"))
        try:
            points = int(raw_pts) if raw_pts else None
        except ValueError:
            flash("Points must be an integer", "error")
            return redirect(url_for("admin_unit_new"))
        new_id = _new_id()
        db.execute(
            "INSERT INTO datasheets (id, name, entry_type, points, catalogue_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (new_id, name, entry_type, points, catalogue_id),
        )
        db.commit()
        flash(f"Created datasheet {name}", "ok")
        return redirect(url_for("admin_unit_edit", datasheet_id=new_id))
    return render_template("admin_unit_new.html", factions=list_factions())


@app.route("/admin/units/<datasheet_id>/delete", methods=["POST"])
@admin_required
def admin_unit_delete(datasheet_id: str):
    db = user_db()
    ds = db.execute("SELECT name FROM datasheets WHERE id = ?", (datasheet_id,)).fetchone()
    if not ds:
        abort(404)
    _cascade_delete_datasheet(db, datasheet_id)
    db.commit()
    flash(f"Deleted {ds['name']} and all its composition / loadout / abilities", "ok")
    return redirect(url_for("admin_units"))


@app.route("/admin/weapons/new", methods=["GET", "POST"])
@admin_required
def admin_weapon_new():
    db = user_db()
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        datasheet_id = (request.form.get("datasheet_id") or "").strip() or None
        if not name:
            flash("Weapon name is required", "error")
            return redirect(url_for("admin_weapon_new"))
        if datasheet_id and not db.execute(
            "SELECT 1 FROM datasheets WHERE id = ?", (datasheet_id,),
        ).fetchone():
            flash("Owner datasheet does not exist", "error")
            return redirect(url_for("admin_weapon_new"))
        new_id = _new_id()
        cat_id = None
        if datasheet_id:
            row = db.execute(
                "SELECT catalogue_id FROM datasheets WHERE id = ?", (datasheet_id,),
            ).fetchone()
            cat_id = row["catalogue_id"] if row else None
        db.execute(
            "INSERT INTO weapons (profile_id, datasheet_id, name, weapon_type, "
            "range_, a, bs_ws, s, ap, d, keywords, catalogue_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (new_id, datasheet_id, name,
             request.form.get("weapon_type") or None,
             request.form.get("range_") or None,
             request.form.get("a") or None,
             request.form.get("bs_ws") or None,
             request.form.get("s") or None,
             request.form.get("ap") or None,
             request.form.get("d") or None,
             request.form.get("keywords") or None,
             cat_id),
        )
        db.commit()
        flash(f"Created weapon {name}", "ok")
        return redirect(url_for("admin_weapon_edit", profile_id=new_id))
    datasheets = db.execute(
        "SELECT d.id, d.name, c.name AS cat_name FROM datasheets d "
        "JOIN catalogues c ON c.id = d.catalogue_id "
        "ORDER BY c.name, d.name LIMIT 2000"
    ).fetchall()
    return render_template("admin_weapon_new.html", datasheets=datasheets)


@app.route("/admin/weapons/<profile_id>/delete", methods=["POST"])
@admin_required
def admin_weapon_delete(profile_id: str):
    db = user_db()
    w = db.execute("SELECT name FROM weapons WHERE profile_id = ?", (profile_id,)).fetchone()
    if not w:
        abort(404)
    _cascade_delete_weapon(db, profile_id)
    db.commit()
    flash(f"Deleted weapon {w['name']}", "ok")
    return redirect(url_for("admin_weapons"))


# ---------------------------------------------------------------------------- #
# Routes — admin: composition (unit_models)
# ---------------------------------------------------------------------------- #

@app.route("/admin/units/<datasheet_id>/models/new", methods=["POST"])
@admin_required
def admin_model_new(datasheet_id: str):
    db = user_db()
    if not db.execute("SELECT 1 FROM datasheets WHERE id = ?", (datasheet_id,)).fetchone():
        abort(404)
    name = (request.form.get("name") or "New model").strip()
    next_sort = db.execute(
        "SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM unit_models "
        "WHERE datasheet_id = ?", (datasheet_id,),
    ).fetchone()["n"]
    db.execute(
        "INSERT INTO unit_models (id, datasheet_id, name, min_count, max_count, "
        "sort_order) VALUES (?, ?, ?, 1, 1, ?)",
        (_new_id(), datasheet_id, name, next_sort),
    )
    db.commit()
    flash(f"Added model type “{name}”", "ok")
    return redirect(url_for("admin_unit_edit", datasheet_id=datasheet_id))


@app.route("/admin/units/<datasheet_id>/models/<model_id>/delete", methods=["POST"])
@admin_required
def admin_model_delete(datasheet_id: str, model_id: str):
    db = user_db()
    row = db.execute(
        "SELECT name FROM unit_models WHERE id = ? AND datasheet_id = ?",
        (model_id, datasheet_id),
    ).fetchone()
    if not row:
        abort(404)
    _cascade_delete_model(db, model_id)
    db.commit()
    flash(f"Deleted model type “{row['name']}” and its loadout", "ok")
    return redirect(url_for("admin_unit_edit", datasheet_id=datasheet_id))


# ---------------------------------------------------------------------------- #
# Routes — admin: loadout (slots / options / option_weapons)
# ---------------------------------------------------------------------------- #

def _get_model_for_admin(datasheet_id: str, model_id: str) -> sqlite3.Row:
    row = user_db().execute(
        "SELECT * FROM unit_models WHERE id = ? AND datasheet_id = ?",
        (model_id, datasheet_id),
    ).fetchone()
    if not row:
        abort(404)
    return row


@app.route("/admin/units/<datasheet_id>/models/<model_id>/loadout")
@admin_required
def admin_model_loadout(datasheet_id: str, model_id: str):
    db = user_db()
    model = _get_model_for_admin(datasheet_id, model_id)
    ds = db.execute(
        "SELECT id, name FROM datasheets WHERE id = ?", (datasheet_id,),
    ).fetchone()
    slot_rows = db.execute(
        "SELECT * FROM loadout_slots WHERE model_id = ? ORDER BY sort_order",
        (model_id,),
    ).fetchall()
    slots = []
    for s in slot_rows:
        opt_rows = db.execute(
            "SELECT * FROM loadout_options WHERE slot_id = ? "
            "ORDER BY is_default DESC, sort_order",
            (s["id"],),
        ).fetchall()
        options = []
        for o in opt_rows:
            weapons = db.execute(
                "SELECT w.profile_id, w.name, w.weapon_type "
                "FROM loadout_option_weapons low "
                "JOIN weapons w ON w.profile_id = low.weapon_profile_id "
                "WHERE low.option_id = ? ORDER BY low.sort_order",
                (o["id"],),
            ).fetchall()
            options.append({"row": o, "weapons": weapons})
        slots.append({"row": s, "options": options})
    # Weapon picker datalist: prefer weapons owned by the same datasheet (most
    # likely candidates), then everything else.
    weapon_choices = db.execute(
        "SELECT profile_id, name, datasheet_id FROM weapons "
        "ORDER BY (datasheet_id = ?) DESC, name LIMIT 2000",
        (datasheet_id,),
    ).fetchall()
    return render_template(
        "admin_model_loadout.html",
        ds=ds, model=model, slots=slots, weapon_choices=weapon_choices,
    )


def _ensure_slot(slot_id: str) -> sqlite3.Row:
    row = user_db().execute(
        "SELECT s.*, m.datasheet_id FROM loadout_slots s "
        "JOIN unit_models m ON m.id = s.model_id WHERE s.id = ?",
        (slot_id,),
    ).fetchone()
    if not row:
        abort(404)
    return row


def _ensure_option(option_id: str) -> sqlite3.Row:
    row = user_db().execute(
        "SELECT o.*, s.model_id, m.datasheet_id FROM loadout_options o "
        "JOIN loadout_slots s ON s.id = o.slot_id "
        "JOIN unit_models m ON m.id = s.model_id WHERE o.id = ?",
        (option_id,),
    ).fetchone()
    if not row:
        abort(404)
    return row


def _redirect_to_loadout(datasheet_id: str, model_id: str):
    return redirect(url_for("admin_model_loadout",
                            datasheet_id=datasheet_id, model_id=model_id))


@app.route("/admin/units/<datasheet_id>/models/<model_id>/slots/new",
           methods=["POST"])
@admin_required
def admin_slot_new(datasheet_id: str, model_id: str):
    db = user_db()
    _get_model_for_admin(datasheet_id, model_id)
    slot_name = (request.form.get("slot_name") or "New slot").strip()
    kind = request.form.get("kind") or "choice"
    if kind not in ("choice", "fixed"):
        kind = "choice"
    try:
        min_sel = int(request.form.get("min_select") or "1")
        max_sel = int(request.form.get("max_select") or "1")
    except ValueError:
        flash("min/max select must be integers", "error")
        return _redirect_to_loadout(datasheet_id, model_id)
    next_sort = db.execute(
        "SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM loadout_slots "
        "WHERE model_id = ?", (model_id,),
    ).fetchone()["n"]
    db.execute(
        "INSERT INTO loadout_slots (id, model_id, slot_name, kind, min_select, "
        "max_select, sort_order) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (_new_id(), model_id, slot_name, kind, min_sel, max_sel, next_sort),
    )
    db.commit()
    flash(f"Added slot “{slot_name}”", "ok")
    return _redirect_to_loadout(datasheet_id, model_id)


@app.route("/admin/loadout/slots/<slot_id>/edit", methods=["POST"])
@admin_required
def admin_slot_edit(slot_id: str):
    s = _ensure_slot(slot_id)
    try:
        min_sel = int(request.form.get("min_select") or "1")
        max_sel = int(request.form.get("max_select") or "1")
    except ValueError:
        flash("min/max select must be integers", "error")
        return _redirect_to_loadout(s["datasheet_id"], s["model_id"])
    kind = request.form.get("kind") or s["kind"]
    if kind not in ("choice", "fixed"):
        kind = s["kind"]
    user_db().execute(
        "UPDATE loadout_slots SET slot_name = ?, kind = ?, min_select = ?, "
        "max_select = ? WHERE id = ?",
        ((request.form.get("slot_name") or s["slot_name"]).strip(),
         kind, min_sel, max_sel, slot_id),
    )
    user_db().commit()
    flash("Slot updated", "ok")
    return _redirect_to_loadout(s["datasheet_id"], s["model_id"])


@app.route("/admin/loadout/slots/<slot_id>/delete", methods=["POST"])
@admin_required
def admin_slot_delete(slot_id: str):
    s = _ensure_slot(slot_id)
    _cascade_delete_slot(user_db(), slot_id)
    user_db().commit()
    flash(f"Deleted slot “{s['slot_name']}” and its options", "ok")
    return _redirect_to_loadout(s["datasheet_id"], s["model_id"])


@app.route("/admin/loadout/slots/<slot_id>/options/new", methods=["POST"])
@admin_required
def admin_option_new(slot_id: str):
    s = _ensure_slot(slot_id)
    db = user_db()
    name = (request.form.get("name") or "New option").strip()
    is_default = 1 if request.form.get("is_default") == "1" else 0
    next_sort = db.execute(
        "SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM loadout_options "
        "WHERE slot_id = ?", (slot_id,),
    ).fetchone()["n"]
    db.execute(
        "INSERT INTO loadout_options (id, slot_id, name, is_default, sort_order) "
        "VALUES (?, ?, ?, ?, ?)",
        (_new_id(), slot_id, name, is_default, next_sort),
    )
    db.commit()
    flash(f"Added option “{name}”", "ok")
    return _redirect_to_loadout(s["datasheet_id"], s["model_id"])


@app.route("/admin/loadout/options/<option_id>/edit", methods=["POST"])
@admin_required
def admin_option_edit(option_id: str):
    o = _ensure_option(option_id)
    name = (request.form.get("name") or o["name"]).strip()
    is_default = 1 if request.form.get("is_default") == "1" else 0
    user_db().execute(
        "UPDATE loadout_options SET name = ?, is_default = ? WHERE id = ?",
        (name, is_default, option_id),
    )
    user_db().commit()
    flash("Option updated", "ok")
    return _redirect_to_loadout(o["datasheet_id"], o["model_id"])


@app.route("/admin/loadout/options/<option_id>/delete", methods=["POST"])
@admin_required
def admin_option_delete(option_id: str):
    o = _ensure_option(option_id)
    _cascade_delete_option(user_db(), option_id)
    user_db().commit()
    flash(f"Deleted option “{o['name']}”", "ok")
    return _redirect_to_loadout(o["datasheet_id"], o["model_id"])


@app.route("/admin/loadout/options/<option_id>/weapons/add", methods=["POST"])
@admin_required
def admin_option_weapon_add(option_id: str):
    o = _ensure_option(option_id)
    db = user_db()
    wpid = (request.form.get("weapon_profile_id") or "").strip()
    if not wpid:
        flash("Pick a weapon", "error")
        return _redirect_to_loadout(o["datasheet_id"], o["model_id"])
    if not db.execute(
        "SELECT 1 FROM weapons WHERE profile_id = ?", (wpid,),
    ).fetchone():
        flash("Unknown weapon", "error")
        return _redirect_to_loadout(o["datasheet_id"], o["model_id"])
    if db.execute(
        "SELECT 1 FROM loadout_option_weapons WHERE option_id = ? AND weapon_profile_id = ?",
        (option_id, wpid),
    ).fetchone():
        flash("Already in this option", "error")
        return _redirect_to_loadout(o["datasheet_id"], o["model_id"])
    next_sort = db.execute(
        "SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM loadout_option_weapons "
        "WHERE option_id = ?", (option_id,),
    ).fetchone()["n"]
    db.execute(
        "INSERT INTO loadout_option_weapons (option_id, weapon_profile_id, sort_order) "
        "VALUES (?, ?, ?)",
        (option_id, wpid, next_sort),
    )
    db.commit()
    flash("Weapon added to option", "ok")
    return _redirect_to_loadout(o["datasheet_id"], o["model_id"])


@app.route("/admin/loadout/options/<option_id>/weapons/<weapon_profile_id>/remove",
           methods=["POST"])
@admin_required
def admin_option_weapon_remove(option_id: str, weapon_profile_id: str):
    o = _ensure_option(option_id)
    user_db().execute(
        "DELETE FROM loadout_option_weapons "
        "WHERE option_id = ? AND weapon_profile_id = ?",
        (option_id, weapon_profile_id),
    )
    user_db().commit()
    flash("Weapon removed from option", "ok")
    return _redirect_to_loadout(o["datasheet_id"], o["model_id"])


# ---------------------------------------------------------------------------- #
# Routes — admin: abilities / keywords / transport (inline on unit edit)
# ---------------------------------------------------------------------------- #

@app.route("/admin/units/<datasheet_id>/abilities/new", methods=["POST"])
@admin_required
def admin_ability_new(datasheet_id: str):
    db = user_db()
    if not db.execute("SELECT 1 FROM datasheets WHERE id = ?", (datasheet_id,)).fetchone():
        abort(404)
    name = (request.form.get("name") or "").strip()
    ability_type = (request.form.get("ability_type") or "").strip() or None
    desc = (request.form.get("description") or "").strip() or None
    if not name:
        flash("Ability name is required", "error")
        return redirect(url_for("admin_unit_edit", datasheet_id=datasheet_id))
    db.execute(
        "INSERT INTO abilities (profile_id, datasheet_id, name, ability_type, "
        "description) VALUES (?, ?, ?, ?, ?)",
        (_new_id(), datasheet_id, name, ability_type, desc),
    )
    db.commit()
    flash(f"Added ability “{name}”", "ok")
    return redirect(url_for("admin_unit_edit", datasheet_id=datasheet_id))


@app.route("/admin/abilities/<profile_id>/edit", methods=["POST"])
@admin_required
def admin_ability_edit(profile_id: str):
    db = user_db()
    a = db.execute(
        "SELECT datasheet_id FROM abilities WHERE profile_id = ?", (profile_id,),
    ).fetchone()
    if not a:
        abort(404)
    name = (request.form.get("name") or "").strip()
    ability_type = (request.form.get("ability_type") or "").strip() or None
    desc = (request.form.get("description") or "").strip() or None
    if not name:
        flash("Ability name is required", "error")
        return redirect(url_for("admin_unit_edit", datasheet_id=a["datasheet_id"]))
    db.execute(
        "UPDATE abilities SET name = ?, ability_type = ?, description = ? "
        "WHERE profile_id = ?",
        (name, ability_type, desc, profile_id),
    )
    db.commit()
    flash("Ability updated", "ok")
    return redirect(url_for("admin_unit_edit", datasheet_id=a["datasheet_id"]))


@app.route("/admin/abilities/<profile_id>/delete", methods=["POST"])
@admin_required
def admin_ability_delete(profile_id: str):
    db = user_db()
    a = db.execute(
        "SELECT datasheet_id, name FROM abilities WHERE profile_id = ?",
        (profile_id,),
    ).fetchone()
    if not a:
        abort(404)
    db.execute("DELETE FROM abilities WHERE profile_id = ?", (profile_id,))
    db.commit()
    flash(f"Deleted ability “{a['name']}”", "ok")
    return redirect(url_for("admin_unit_edit", datasheet_id=a["datasheet_id"]))


@app.route("/admin/units/<datasheet_id>/keywords/new", methods=["POST"])
@admin_required
def admin_keyword_new(datasheet_id: str):
    db = user_db()
    if not db.execute("SELECT 1 FROM datasheets WHERE id = ?", (datasheet_id,)).fetchone():
        abort(404)
    name = (request.form.get("category_name") or "").strip()
    is_primary = 1 if request.form.get("is_primary") == "1" else 0
    if not name:
        flash("Keyword is required", "error")
        return redirect(url_for("admin_unit_edit", datasheet_id=datasheet_id))
    # category_id needs to be unique within (datasheet_id) — synthesize one.
    cat_id = _new_id()
    if db.execute(
        "SELECT 1 FROM datasheet_keywords WHERE datasheet_id = ? AND category_name = ?",
        (datasheet_id, name),
    ).fetchone():
        flash(f"Keyword “{name}” already present", "error")
        return redirect(url_for("admin_unit_edit", datasheet_id=datasheet_id))
    db.execute(
        "INSERT INTO datasheet_keywords (datasheet_id, category_id, category_name, "
        "is_primary) VALUES (?, ?, ?, ?)",
        (datasheet_id, cat_id, name, is_primary),
    )
    db.commit()
    flash(f"Added keyword “{name}”", "ok")
    return redirect(url_for("admin_unit_edit", datasheet_id=datasheet_id))


@app.route("/admin/units/<datasheet_id>/keywords/<category_id>/delete",
           methods=["POST"])
@admin_required
def admin_keyword_delete(datasheet_id: str, category_id: str):
    db = user_db()
    cur = db.execute(
        "DELETE FROM datasheet_keywords WHERE datasheet_id = ? AND category_id = ?",
        (datasheet_id, category_id),
    )
    db.commit()
    if cur.rowcount == 0:
        abort(404)
    flash("Keyword removed", "ok")
    return redirect(url_for("admin_unit_edit", datasheet_id=datasheet_id))


@app.route("/admin/units/<datasheet_id>/transport/new", methods=["POST"])
@admin_required
def admin_transport_new(datasheet_id: str):
    db = user_db()
    if not db.execute("SELECT 1 FROM datasheets WHERE id = ?", (datasheet_id,)).fetchone():
        abort(404)
    capacity = (request.form.get("capacity") or "").strip()
    if not capacity:
        flash("Capacity is required", "error")
        return redirect(url_for("admin_unit_edit", datasheet_id=datasheet_id))
    db.execute(
        "INSERT INTO transport (profile_id, datasheet_id, capacity) VALUES (?, ?, ?)",
        (_new_id(), datasheet_id, capacity),
    )
    db.commit()
    flash("Added transport entry", "ok")
    return redirect(url_for("admin_unit_edit", datasheet_id=datasheet_id))


@app.route("/admin/transport/<profile_id>/edit", methods=["POST"])
@admin_required
def admin_transport_edit(profile_id: str):
    db = user_db()
    t = db.execute(
        "SELECT datasheet_id FROM transport WHERE profile_id = ?", (profile_id,),
    ).fetchone()
    if not t:
        abort(404)
    capacity = (request.form.get("capacity") or "").strip()
    if not capacity:
        flash("Capacity is required", "error")
    else:
        db.execute(
            "UPDATE transport SET capacity = ? WHERE profile_id = ?",
            (capacity, profile_id),
        )
        db.commit()
        flash("Transport updated", "ok")
    return redirect(url_for("admin_unit_edit", datasheet_id=t["datasheet_id"]))


@app.route("/admin/transport/<profile_id>/delete", methods=["POST"])
@admin_required
def admin_transport_delete(profile_id: str):
    db = user_db()
    t = db.execute(
        "SELECT datasheet_id FROM transport WHERE profile_id = ?", (profile_id,),
    ).fetchone()
    if not t:
        abort(404)
    db.execute("DELETE FROM transport WHERE profile_id = ?", (profile_id,))
    db.commit()
    flash("Transport entry removed", "ok")
    return redirect(url_for("admin_unit_edit", datasheet_id=t["datasheet_id"]))


# ---------------------------------------------------------------------------- #
# Template filters
# ---------------------------------------------------------------------------- #

@app.template_filter("nl2br")
def nl2br(value):
    if value is None:
        return ""
    from markupsafe import Markup, escape
    return Markup("<br>".join(escape(value).split("\n")))


@app.template_filter("dt")
def dt_filter(value):
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return value


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
