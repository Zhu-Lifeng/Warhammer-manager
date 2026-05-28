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
KB_DB = ROOT / "kb" / "wh40k.db"
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

def kb_db() -> sqlite3.Connection:
    if "kb" not in g:
        if not KB_DB.exists():
            raise RuntimeError(
                f"Knowledge-base DB not found at {KB_DB}. "
                "Run `py kb/build_kb.py` first."
            )
        # Read-only. timeout=10s tolerates the DB being rebuilt while Flask runs.
        conn = sqlite3.connect(
            f"file:{KB_DB}?mode=ro", uri=True, timeout=10.0,
        )
        conn.row_factory = sqlite3.Row
        g.kb = conn
    return g.kb


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


def init_user_db(conn: sqlite3.Connection) -> None:
    """Idempotent: safe to call on existing DBs (uses IF NOT EXISTS).

    Also applies concurrency-friendly PRAGMAs:
      - journal_mode=WAL    readers don't block writers, persisted on disk
      - synchronous=NORMAL  pairs with WAL; ~2× write speed, still durable
    Both are persisted in the DB file, so they apply to every later connection.
    """
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
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

    -- User-edited point overrides. KB DB stays read-only; these win at display time.
    CREATE TABLE IF NOT EXISTS datasheet_overrides (
        datasheet_id TEXT PRIMARY KEY,
        points       INTEGER,
        notes        TEXT,
        updated_at   TEXT NOT NULL
    );

    -- Per-tier overrides; (datasheet_id, condition_text) is stable across KB rebuilds.
    CREATE TABLE IF NOT EXISTS pricing_tier_overrides (
        datasheet_id   TEXT NOT NULL,
        condition_text TEXT NOT NULL,
        points         INTEGER NOT NULL,
        notes          TEXT,
        updated_at     TEXT NOT NULL,
        PRIMARY KEY (datasheet_id, condition_text)
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
    conn.commit()


def get_points_overrides() -> dict[str, int]:
    rows = user_db().execute(
        "SELECT datasheet_id, points FROM datasheet_overrides"
    ).fetchall()
    return {r["datasheet_id"]: r["points"] for r in rows}


def get_tier_overrides(datasheet_id: str) -> dict[str, sqlite3.Row]:
    """Return {condition_text: row} of tier overrides for a datasheet."""
    rows = user_db().execute(
        "SELECT * FROM pricing_tier_overrides WHERE datasheet_id = ?",
        (datasheet_id,),
    ).fetchall()
    return {r["condition_text"]: r for r in rows}


def get_loadout_schema(datasheet_id: str) -> list[dict]:
    """Return composition + choice slots + options for a datasheet.

    Returns: [{model: row, choice_slots: [{slot: row, options: [{row, weapons}]}]}]
    Only includes slots with kind='choice' since 'fixed' has no user choice.
    """
    db = kb_db()
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
    db = kb_db()
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
    db = kb_db()
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
    for key in ("kb", "user"):
        conn = g.pop(key, None)
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
            flash("请先登录", "error")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        u = current_user()
        if u is None:
            flash("请先登录", "error")
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

def list_factions() -> list[sqlite3.Row]:
    """Catalogues that actually have datasheets, sorted by name."""
    rows = kb_db().execute("""
        SELECT c.id, c.name, c.is_library, COUNT(d.id) AS n_datasheets
        FROM catalogues c LEFT JOIN datasheets d ON d.catalogue_id = c.id
        GROUP BY c.id HAVING n_datasheets > 0
        ORDER BY c.name
    """).fetchall()
    return rows


def list_keywords() -> list[str]:
    rows = kb_db().execute("""
        SELECT category_name, COUNT(*) AS n
        FROM datasheet_keywords
        GROUP BY category_name ORDER BY category_name
    """).fetchall()
    return [r["category_name"] for r in rows]


def get_datasheet_full(datasheet_id: str) -> dict | None:
    db = kb_db()
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
    pricing_tier_rows = db.execute(
        "SELECT * FROM pricing_tiers WHERE datasheet_id = ? "
        "ORDER BY condition_value, points",
        (datasheet_id,),
    ).fetchall()
    # Enrich each tier with effective points (override > KB)
    tier_overrides = get_tier_overrides(datasheet_id)
    pricing_tiers = []
    for t in pricing_tier_rows:
        d = dict(t)
        ov_t = tier_overrides.get(t["condition_text"])
        d["override"] = ov_t  # sqlite3.Row or None
        d["effective_points"] = ov_t["points"] if ov_t else t["points"]
        d["overridden"] = ov_t is not None and ov_t["points"] != t["points"]
        pricing_tiers.append(d)
    # Base point override (user-edited)
    ov = user_db().execute(
        "SELECT points, notes, updated_at FROM datasheet_overrides WHERE datasheet_id = ?",
        (datasheet_id,),
    ).fetchone()
    return {
        "ds": ds,
        "models": models,
        "abilities": abilities,
        "keywords": keywords,
        "transport": transport,
        "pricing_tiers": pricing_tiers,
        "override": ov,
        "effective_points": ov["points"] if ov else ds["points"],
    }


# ---------------------------------------------------------------------------- #
# Routes — home
# ---------------------------------------------------------------------------- #

@app.route("/uploads/<path:filename>")
def uploaded_file(filename: str):
    """Serve user-uploaded images from UPLOAD_DIR (which may live outside the repo)."""
    return send_from_directory(UPLOAD_DIR, filename)


@app.route("/")
def index():
    db_u = user_db()
    me = current_user()
    if me is not None:
        n_armies = db_u.execute(
            "SELECT COUNT(*) AS n FROM armies WHERE user_id = ?", (me["id"],),
        ).fetchone()["n"]
        n_models = db_u.execute(
            "SELECT COUNT(*) AS n FROM models WHERE user_id = ?", (me["id"],),
        ).fetchone()["n"]
    else:
        n_armies = n_models = 0
    n_units = kb_db().execute("SELECT COUNT(*) AS n FROM datasheets").fetchone()["n"]
    n_factions = len(list_factions())
    n_public_users = db_u.execute(
        "SELECT COUNT(*) AS n FROM users WHERE is_public = 1"
    ).fetchone()["n"]
    return render_template(
        "index.html",
        n_armies=n_armies, n_models=n_models, n_units=n_units, n_factions=n_factions,
        n_public_users=n_public_users,
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
        where.append("d.catalogue_id = ?")
        params.append(catalogue_id)
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
    rows = kb_db().execute(sql, params).fetchall() if has_filter else []

    # Enrich with point overrides
    overrides = get_points_overrides()
    results = []
    for r in rows:
        d = dict(r)
        ov = overrides.get(d["id"])
        d["effective_points"] = ov if ov is not None else d["points"]
        d["overridden"] = ov is not None and ov != d["points"]
        results.append(d)

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


@app.route("/query/unit/<datasheet_id>/edit-tier-points", methods=["POST"])
@admin_required
def edit_tier_points(datasheet_id: str):
    """Set or clear a per-tier point override (size-tier price edit)."""
    ds = kb_db().execute(
        "SELECT name FROM datasheets WHERE id = ?", (datasheet_id,),
    ).fetchone()
    if not ds:
        abort(404)
    condition_text = (request.form.get("condition_text") or "").strip()
    if not condition_text:
        abort(400)
    tier_row = kb_db().execute(
        "SELECT points FROM pricing_tiers "
        "WHERE datasheet_id = ? AND condition_text = ?",
        (datasheet_id, condition_text),
    ).fetchone()
    if not tier_row:
        flash("Tier not found", "error")
        return redirect(url_for("query_unit", datasheet_id=datasheet_id))

    raw = (request.form.get("points") or "").strip()
    notes = (request.form.get("notes") or "").strip() or None
    now = datetime.utcnow().isoformat()
    db = user_db()
    if raw == "":
        db.execute(
            "DELETE FROM pricing_tier_overrides "
            "WHERE datasheet_id = ? AND condition_text = ?",
            (datasheet_id, condition_text),
        )
        db.commit()
        flash(f"Cleared override for tier “{condition_text}”", "ok")
    else:
        try:
            new_pts = int(raw)
            if new_pts < 0:
                raise ValueError("negative")
        except ValueError:
            flash("Points must be a non-negative integer", "error")
            return redirect(url_for("query_unit", datasheet_id=datasheet_id))
        db.execute(
            "INSERT INTO pricing_tier_overrides "
            "(datasheet_id, condition_text, points, notes, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(datasheet_id, condition_text) DO UPDATE SET "
            "points=excluded.points, notes=excluded.notes, "
            "updated_at=excluded.updated_at",
            (datasheet_id, condition_text, new_pts, notes, now),
        )
        db.commit()
        flash(f"Tier “{condition_text}” set to {new_pts} pts", "ok")
    return redirect(url_for("query_unit", datasheet_id=datasheet_id))


@app.route("/query/unit/<datasheet_id>/edit-points", methods=["POST"])
@admin_required
def edit_points(datasheet_id: str):
    """Set or clear a user point override for a datasheet."""
    ds = kb_db().execute(
        "SELECT id, name, points FROM datasheets WHERE id = ?", (datasheet_id,),
    ).fetchone()
    if not ds:
        abort(404)
    raw = (request.form.get("points") or "").strip()
    notes = (request.form.get("notes") or "").strip() or None
    now = datetime.utcnow().isoformat()
    db = user_db()
    if raw == "":
        # empty input → clear override
        db.execute("DELETE FROM datasheet_overrides WHERE datasheet_id = ?", (datasheet_id,))
        db.commit()
        flash(f"Cleared override for {ds['name']}", "ok")
    else:
        try:
            new_pts = int(raw)
            if new_pts < 0:
                raise ValueError("negative")
        except ValueError:
            flash("Points must be a non-negative integer", "error")
            return redirect(url_for("query_unit", datasheet_id=datasheet_id))
        db.execute(
            "INSERT INTO datasheet_overrides (datasheet_id, points, notes, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(datasheet_id) DO UPDATE SET "
            "points=excluded.points, notes=excluded.notes, updated_at=excluded.updated_at",
            (datasheet_id, new_pts, notes, now),
        )
        db.commit()
        orig = ds["points"]
        if orig is None:
            flash(f"{ds['name']} set to {new_pts} pts", "ok")
        else:
            flash(f"{ds['name']} updated: {orig} → {new_pts} pts", "ok")
    return redirect(url_for("query_unit", datasheet_id=datasheet_id))


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

@app.route("/army")
@login_required
def army_list():
    uid = require_user_id()
    rows = user_db().execute("""
        SELECT a.*,
               COALESCE(SUM(
                   CASE WHEN u.tier_points IS NOT NULL
                        THEN COALESCE(tov.points, u.tier_points)
                        ELSE COALESCE(o.points,   u.points)
                   END * u.count), 0) AS total_pts,
               COUNT(u.id) AS n_units
        FROM armies a
          LEFT JOIN army_units u ON u.army_id = a.id
          LEFT JOIN datasheet_overrides o ON o.datasheet_id = u.datasheet_id
          LEFT JOIN pricing_tier_overrides tov
                 ON tov.datasheet_id = u.datasheet_id
                AND tov.condition_text = u.tier_label
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
        faction = kb_db().execute(
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
               o.points   AS override_points,
               tov.points AS tier_override_points
        FROM army_units u
          LEFT JOIN datasheet_overrides o ON o.datasheet_id = u.datasheet_id
          LEFT JOIN pricing_tier_overrides tov
                 ON tov.datasheet_id = u.datasheet_id
                AND tov.condition_text = u.tier_label
        WHERE u.army_id = ? ORDER BY u.added_at
    """, (army_id,)).fetchall()
    units = []
    total = 0
    for r in unit_rows:
        d = dict(r)
        d["is_expanded"] = d["tier_points"] is not None
        if d["is_expanded"]:
            d["effective_points"] = (d["tier_override_points"]
                                     if d["tier_override_points"] is not None
                                     else d["tier_points"])
            d["overridden"] = (d["tier_override_points"] is not None
                               and d["tier_override_points"] != d["tier_points"])
        else:
            d["effective_points"] = (d["override_points"]
                                     if d["override_points"] is not None
                                     else d["points"])
            d["overridden"] = (d["override_points"] is not None
                               and d["override_points"] != d["points"])
        # Loadout summary: list of "model: option_name"
        chosen = fetch_army_unit_loadout(d["id"])
        d["loadout_summary"] = []
        if chosen:
            kb = kb_db()
            for slot_id, option_id in chosen.items():
                row = kb.execute("""
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
    sql = """
        SELECT DISTINCT d.id, d.name, d.points
        FROM datasheets d
    """
    params = [army["faction_id"]]
    if keyword:
        sql += " JOIN datasheet_keywords k ON k.datasheet_id = d.id"
        sql += " WHERE d.catalogue_id = ? AND k.category_name = ?"
        params.append(keyword)
    else:
        sql += " WHERE d.catalogue_id = ?"
    sql += " ORDER BY d.name"
    avail_rows = kb_db().execute(sql, params).fetchall()
    overrides = get_points_overrides()
    available = []
    for r in avail_rows:
        d = dict(r)
        ov = overrides.get(d["id"])
        d["effective_points"] = ov if ov is not None else d["points"]
        d["overridden"] = ov is not None and ov != d["points"]
        available.append(d)
    # keywords available within this catalogue
    kw_rows = kb_db().execute("""
        SELECT DISTINCT k.category_name
        FROM datasheet_keywords k JOIN datasheets d ON d.id = k.datasheet_id
        WHERE d.catalogue_id = ? ORDER BY k.category_name
    """, (army["faction_id"],)).fetchall()
    return render_template(
        "army_view.html",
        army=army, units=units, total=total,
        available=available, faction_keywords=[r["category_name"] for r in kw_rows],
        filter_keyword=keyword,
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
    ds = kb_db().execute(
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
    tier_rows = kb_db().execute(
        "SELECT * FROM pricing_tiers WHERE datasheet_id = ? "
        "ORDER BY condition_value, points",
        (unit["datasheet_id"],),
    ).fetchall()
    tier_ov_map = get_tier_overrides(unit["datasheet_id"])
    tiers = []
    for t in tier_rows:
        d = dict(t)
        ov_t = tier_ov_map.get(t["condition_text"])
        d["effective_points"] = ov_t["points"] if ov_t else t["points"]
        d["overridden"] = ov_t is not None and ov_t["points"] != t["points"]
        tiers.append(d)

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
    # Datasheet base/override info for the tier picker
    ds_row = kb_db().execute(
        "SELECT name, points FROM datasheets WHERE id = ?", (unit["datasheet_id"],),
    ).fetchone()
    ov = user_db().execute(
        "SELECT points FROM datasheet_overrides WHERE datasheet_id = ?",
        (unit["datasheet_id"],),
    ).fetchone()
    base_points = ov["points"] if ov else (ds_row["points"] if ds_row else None)
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


@app.route("/models")
@login_required
def models_list():
    uid = require_user_id()
    args = request.args
    faction = (args.get("faction") or "").strip()
    keyword = (args.get("keyword") or "").strip()
    status = (args.get("status") or "").strip()
    q      = (args.get("q") or "").strip()

    # Keyword filter → look up matching datasheet_ids from KB
    ds_filter_ids: set[str] | None = None
    if keyword:
        kb_rows = kb_db().execute(
            "SELECT DISTINCT datasheet_id FROM datasheet_keywords "
            "WHERE category_name = ?",
            (keyword,),
        ).fetchall()
        ds_filter_ids = {r["datasheet_id"] for r in kb_rows}
        if not ds_filter_ids:
            ds_filter_ids = {"__none__"}   # force empty result

    where: list[str] = ["m.user_id = ?"]
    params: list = [uid]
    if faction:
        where.append("m.faction_name = ?")
        params.append(faction)
    if status:
        where.append("m.status = ?")
        params.append(status)
    if q:
        where.append("(m.custom_name LIKE ? OR m.datasheet_name LIKE ?)")
        params.append(f"%{q}%")
        params.append(f"%{q}%")
    if ds_filter_ids is not None:
        placeholders = ",".join("?" * len(ds_filter_ids))
        where.append(f"m.datasheet_id IN ({placeholders})")
        params.extend(ds_filter_ids)

    sql = ("SELECT m.*, "
           "(SELECT filename FROM model_images WHERE model_id = m.id "
           " ORDER BY id LIMIT 1) AS cover, "
           "(SELECT COUNT(*) FROM model_images WHERE model_id = m.id) AS n_images "
           "FROM models m WHERE " + " AND ".join(where) +
           " ORDER BY m.created_at DESC")
    rows = user_db().execute(sql, params).fetchall()
    total = user_db().execute(
        "SELECT COUNT(*) FROM models WHERE user_id = ?", (uid,),
    ).fetchone()[0]

    # Available faction values (from already-registered models, for the dropdown)
    faction_rows = user_db().execute(
        "SELECT DISTINCT faction_name FROM models "
        "WHERE user_id = ? AND faction_name IS NOT NULL AND faction_name != '' "
        "ORDER BY faction_name", (uid,),
    ).fetchall()
    faction_list = [r["faction_name"] for r in faction_rows]

    return render_template(
        "models_list.html",
        models=rows, status_map=STATUS_MAP,
        status_options=STATUS_OPTIONS,
        factions=faction_list,
        keywords=list_keywords(),
        total=total,
        f={"faction": faction, "keyword": keyword, "status": status, "q": q},
    )


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
    if request.method == "POST":
        datasheet_id = request.form.get("datasheet_id") or ""
        model_type_id = request.form.get("model_type_id") or ""
        custom_name = (request.form.get("custom_name") or "").strip()
        status = request.form.get("status") or "unpainted"
        notes = (request.form.get("notes") or "").strip()
        ds = kb_db().execute("""
            SELECT d.id, d.name, c.name AS catalogue_name
            FROM datasheets d JOIN catalogues c ON c.id = d.catalogue_id
            WHERE d.id = ?
        """, (datasheet_id,)).fetchone()
        if not ds:
            flash("Please pick a valid datasheet", "error")
            return redirect(url_for("model_new"))
        # If no model_type picked, default to the first model type of this datasheet
        if not model_type_id:
            first_m = kb_db().execute(
                "SELECT id FROM unit_models WHERE datasheet_id = ? "
                "ORDER BY sort_order LIMIT 1",
                (ds["id"],),
            ).fetchone()
            if first_m:
                model_type_id = first_m["id"]
        now = datetime.utcnow().isoformat()
        cur = user_db().execute(
            "INSERT INTO models (custom_name, datasheet_id, datasheet_name, faction_name, "
            "status, notes, model_type_id, created_at, updated_at, user_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (custom_name, ds["id"], ds["name"], ds["catalogue_name"],
             status, notes, model_type_id, now, now, uid),
        )
        model_id = cur.lastrowid
        _save_model_loadout(model_id, ds["id"], request.form)
        for fs in request.files.getlist("images"):
            fn = save_uploaded_image(fs)
            if fn:
                user_db().execute(
                    "INSERT INTO model_images (model_id, filename, uploaded_at) "
                    "VALUES (?, ?, ?)",
                    (model_id, fn, now),
                )
        user_db().commit()
        flash("Registered", "ok")
        return redirect(url_for("model_detail", model_id=model_id))
    return render_template(
        "model_new.html",
        factions=list_factions(),
        status_options=STATUS_OPTIONS,
        preselect_ds=request.args.get("ds") or "",
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
    rows = kb_db().execute(sql, params).fetchall()
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
        model_type_row = kb_db().execute(
            "SELECT * FROM unit_models WHERE id = ?", (m["model_type_id"],),
        ).fetchone()
    # Compose loadout summary for display
    loadout_summary = []
    if current:
        kb = kb_db()
        for slot_id, option_id in current.items():
            row = kb.execute("""
                SELECT o.name AS option_name, s.slot_name
                FROM loadout_options o JOIN loadout_slots s ON s.id = o.slot_id
                WHERE o.id = ?
            """, (option_id,)).fetchone()
            if row:
                loadout_summary.append({
                    "slot_name": row["slot_name"],
                    "option_name": row["option_name"],
                })
    return render_template(
        "model_detail.html", m=m, imgs=imgs,
        status_options=STATUS_OPTIONS, status_map=STATUS_MAP,
        schema=schema, current=current, model_type_row=model_type_row,
        loadout_summary=loadout_summary,
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
    for fs in request.files.getlist("images"):
        fn = save_uploaded_image(fs)
        if fn:
            user_db().execute(
                "INSERT INTO model_images (model_id, filename, uploaded_at) "
                "VALUES (?, ?, ?)",
                (model_id, fn, now),
            )
    user_db().commit()
    flash("Saved", "ok")
    return redirect(url_for("model_detail", model_id=model_id))


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


@app.route("/models/<int:model_id>/image/<int:image_id>/delete", methods=["POST"])
@login_required
def model_image_delete(model_id: int, image_id: int):
    uid = require_user_id()
    owner = user_db().execute(
        "SELECT 1 FROM models WHERE id = ? AND user_id = ?", (model_id, uid),
    ).fetchone()
    if not owner:
        abort(404)
    row = user_db().execute(
        "SELECT filename FROM model_images WHERE id = ? AND model_id = ?",
        (image_id, model_id),
    ).fetchone()
    if row:
        user_db().execute("DELETE FROM model_images WHERE id = ?", (image_id,))
        user_db().commit()
        try:
            (UPLOAD_DIR / row["filename"]).unlink(missing_ok=True)
        except Exception:
            pass
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
            flash("用户名 3–32 位，只允许字母/数字/_-.", "error")
            return redirect(url_for("register"))
        if len(password) < 8:
            flash("密码至少 8 位", "error")
            return redirect(url_for("register"))
        if password != confirm:
            flash("两次输入的密码不一致", "error")
            return redirect(url_for("register"))
        db = user_db()
        if db.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
            flash("用户名已被占用", "error")
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
            flash(f"欢迎，{username}！你是首位用户，已设为管理员，"
                  f"接管了 {claimed} 条历史数据。", "ok")
        else:
            flash(f"注册成功，欢迎 {username}", "ok")
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
            flash("用户名或密码错误", "error")
            return redirect(url_for("login", next=next_url))
        session["user_id"] = row["id"]
        flash(f"欢迎回来，{row['username']}", "ok")
        # next must be a local path to avoid open-redirects.
        if next_url.startswith("/") and not next_url.startswith("//"):
            return redirect(next_url)
        return redirect(url_for("index"))
    return render_template("login.html", next=next_url)


@app.route("/logout", methods=["POST"])
def logout():
    session.pop("user_id", None)
    flash("已退出", "ok")
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
            flash("已切换为" + ("公开" if new_val else "私人"), "ok")
        elif action == "password":
            old_pw = request.form.get("old_password") or ""
            new_pw = request.form.get("new_password") or ""
            confirm = request.form.get("confirm_password") or ""
            if not check_password_hash(me["password_hash"], old_pw):
                flash("旧密码错误", "error")
            elif len(new_pw) < 8:
                flash("新密码至少 8 位", "error")
            elif new_pw != confirm:
                flash("两次输入的新密码不一致", "error")
            else:
                db.execute(
                    "UPDATE users SET password_hash = ? WHERE id = ?",
                    (generate_password_hash(new_pw), me["id"]),
                )
                db.commit()
                flash("密码已更新", "ok")
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
    armies = user_db().execute("""
        SELECT a.*,
               COALESCE(SUM(
                   CASE WHEN u.tier_points IS NOT NULL
                        THEN COALESCE(tov.points, u.tier_points)
                        ELSE COALESCE(o.points,   u.points)
                   END * u.count), 0) AS total_pts,
               COUNT(u.id) AS n_units
        FROM armies a
          LEFT JOIN army_units u ON u.army_id = a.id
          LEFT JOIN datasheet_overrides o ON o.datasheet_id = u.datasheet_id
          LEFT JOIN pricing_tier_overrides tov
                 ON tov.datasheet_id = u.datasheet_id
                AND tov.condition_text = u.tier_label
        WHERE a.user_id = ?
        GROUP BY a.id ORDER BY a.created_at DESC
    """, (user["id"],)).fetchall()
    models = user_db().execute("""
        SELECT m.*,
               (SELECT filename FROM model_images WHERE model_id = m.id
                ORDER BY id LIMIT 1) AS cover,
               (SELECT COUNT(*) FROM model_images WHERE model_id = m.id) AS n_images
        FROM models m WHERE m.user_id = ? ORDER BY m.created_at DESC
    """, (user["id"],)).fetchall()
    return render_template(
        "u_profile.html", user=user, armies=armies, models=models,
        status_map=STATUS_MAP,
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
               o.points   AS override_points,
               tov.points AS tier_override_points
        FROM army_units u
          LEFT JOIN datasheet_overrides o ON o.datasheet_id = u.datasheet_id
          LEFT JOIN pricing_tier_overrides tov
                 ON tov.datasheet_id = u.datasheet_id
                AND tov.condition_text = u.tier_label
        WHERE u.army_id = ? ORDER BY u.added_at
    """, (army_id,)).fetchall()
    units, total = [], 0
    for r in unit_rows:
        d = dict(r)
        if d["tier_points"] is not None:
            d["effective_points"] = (d["tier_override_points"]
                                     if d["tier_override_points"] is not None
                                     else d["tier_points"])
        else:
            d["effective_points"] = (d["override_points"]
                                     if d["override_points"] is not None
                                     else d["points"])
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
        flash("新密码至少 8 位", "error")
        return redirect(url_for("admin_users"))
    user_db().execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (generate_password_hash(new_pw), user_id),
    )
    user_db().commit()
    flash(f"已重置 {target['username']} 的密码", "ok")
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
            flash("无法降级最后一位管理员", "error")
            return redirect(url_for("admin_users"))
    user_db().execute(
        "UPDATE users SET role = ? WHERE id = ?", (new_role, user_id),
    )
    user_db().commit()
    flash(f"已将 {target['username']} 设为 {new_role}", "ok")
    return redirect(url_for("admin_users"))


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
