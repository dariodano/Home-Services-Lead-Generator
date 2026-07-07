"""SQLite storage for leads.

One database file (leads.db) in the project folder. Each industry (search
term) is its own list; leads are unique per (industry, Google place id), so
re-running a search only adds businesses you haven't seen before and never
resets the "clicked" state on leads you've already worked.
"""

import sqlite3
from contextlib import closing
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "leads.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS industries (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS leads (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    industry_id    INTEGER NOT NULL REFERENCES industries(id) ON DELETE CASCADE,
    place_id       TEXT NOT NULL,
    name           TEXT NOT NULL,
    phone          TEXT NOT NULL DEFAULT '',
    maps_url       TEXT NOT NULL DEFAULT '',
    website_status TEXT NOT NULL,
    address        TEXT NOT NULL DEFAULT '',
    clicked        INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (industry_id, place_id)
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with closing(connect()) as conn, conn:
        conn.executescript(_SCHEMA)


def get_or_create_industry(name: str) -> int:
    with closing(connect()) as conn, conn:
        conn.execute("INSERT OR IGNORE INTO industries (name) VALUES (?)", (name,))
        row = conn.execute(
            "SELECT id FROM industries WHERE name = ? COLLATE NOCASE", (name,)
        ).fetchone()
        return row["id"]


def insert_leads(industry_id: int, rows: list[dict]) -> int:
    """Insert leads, ignoring any (industry, place_id) already stored.
    Returns how many rows were actually new."""
    if not rows:
        return 0
    with closing(connect()) as conn, conn:
        before = conn.total_changes
        conn.executemany(
            """INSERT OR IGNORE INTO leads
               (industry_id, place_id, name, phone, maps_url, website_status, address)
               VALUES (:industry_id, :place_id, :name, :phone, :maps_url,
                       :website_status, :address)""",
            [{**r, "industry_id": industry_id} for r in rows],
        )
        return conn.total_changes - before


def list_industries() -> list[sqlite3.Row]:
    with closing(connect()) as conn:
        return conn.execute(
            """SELECT i.id, i.name, i.created_at,
                      COUNT(l.id) AS total,
                      COALESCE(SUM(l.clicked = 0), 0) AS remaining
               FROM industries i
               LEFT JOIN leads l ON l.industry_id = i.id
               GROUP BY i.id
               ORDER BY i.name COLLATE NOCASE""",
        ).fetchall()


def get_industry(industry_id: int) -> sqlite3.Row | None:
    with closing(connect()) as conn:
        return conn.execute(
            "SELECT * FROM industries WHERE id = ?", (industry_id,)
        ).fetchone()


def get_leads(industry_id: int) -> list[sqlite3.Row]:
    """Leads for one industry: unclicked first, then 'No website' before
    'Social only', then rows with a phone number, then by name."""
    with closing(connect()) as conn:
        return conn.execute(
            """SELECT * FROM leads
               WHERE industry_id = ?
               ORDER BY clicked ASC,
                        (website_status != 'No website') ASC,
                        (phone = '') ASC,
                        name COLLATE NOCASE ASC""",
            (industry_id,),
        ).fetchall()


def set_clicked(lead_id: int, clicked: bool) -> bool:
    with closing(connect()) as conn, conn:
        cur = conn.execute(
            "UPDATE leads SET clicked = ? WHERE id = ?", (int(clicked), lead_id)
        )
        return cur.rowcount > 0


def delete_industry(industry_id: int) -> None:
    with closing(connect()) as conn, conn:
        conn.execute("DELETE FROM industries WHERE id = ?", (industry_id,))


init_db()
