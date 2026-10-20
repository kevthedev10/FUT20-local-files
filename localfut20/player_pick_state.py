"""Durable FIFA 20 native Player Pick lifecycle.

FIFA 20 does not open a pick from an arbitrary JSON ``options`` array.  It
expects a real miscPlayerPick item, redeems that item through
``/item/nontargeted``, stores the temporary candidates, and finally promotes
the chosen candidate's exact item instance to the unassigned pile.
"""

from __future__ import annotations

import json
import time
from typing import Any


REAL_PICK_RESOURCE_ID = 5_004_241
REAL_PICK_CARD_ASSET_ID = 44
REAL_PICK_STATIC = {
    "cardassetid": REAL_PICK_CARD_ASSET_ID,
    "cardsubtypeid": 237,
    "rating": 99,
    "rareflag": 1,
    "amount": 4012,
    "bronze": 0,
    "silver": 0,
    "gold": 0,
    "discardValue": 0,
    "sourceMember": "miscPlayerPick",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS player_pick_tokens(
    token_id INTEGER PRIMARY KEY,
    pool_key TEXT NOT NULL,
    title TEXT NOT NULL,
    option_count INTEGER NOT NULL,
    token_json TEXT NOT NULL,
    pool_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'available',
    created_at INTEGER NOT NULL,
    redeemed_at INTEGER,
    resolved_at INTEGER
);
CREATE TABLE IF NOT EXISTS player_pick_sessions(
    token_id INTEGER PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at INTEGER NOT NULL,
    resolved_at INTEGER,
    selected_candidate_id INTEGER,
    selected_resource_id INTEGER
);
CREATE TABLE IF NOT EXISTS player_pick_candidates(
    candidate_id INTEGER PRIMARY KEY,
    token_id INTEGER NOT NULL,
    ordinal INTEGER NOT NULL,
    resource_id INTEGER NOT NULL,
    owned INTEGER NOT NULL DEFAULT 0,
    item_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS player_pick_candidates_token
    ON player_pick_candidates(token_id, ordinal);
CREATE TABLE IF NOT EXISTS player_pick_receipts(
    token_id INTEGER PRIMARY KEY,
    candidate_id INTEGER NOT NULL,
    resource_id INTEGER NOT NULL,
    item_json TEXT NOT NULL,
    selected_at INTEGER NOT NULL
);
"""


def _dump(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _load(value: str, default: Any = None) -> Any:
    try:
        return json.loads(value)
    except Exception:
        return default


def ensure(state: Any) -> None:
    with state.lock:
        state.conn.executescript(SCHEMA)
        state.conn.commit()


def token_item(token_id: int, title: str = "1 of 3 FUT Champions Players") -> dict[str, Any]:
    """Build the static-catalog misc item the retail client recognizes."""
    return {
        "id": int(token_id),
        "timestamp": int(time.time()),
        "resourceId": REAL_PICK_RESOURCE_ID,
        "definitionId": REAL_PICK_RESOURCE_ID,
        "_definitionId": REAL_PICK_RESOURCE_ID,
        "assetId": REAL_PICK_RESOURCE_ID,
        "cardassetid": REAL_PICK_CARD_ASSET_ID,
        "cardsubtypeid": 237,
        "rating": 99,
        "rareflag": 1,
        "rareFlag": 1,
        "amount": 4012,
        "itemType": "misc",
        "itemState": "free",
        "pile": "unassigned",
        "owners": 1,
        "untradeable": True,
        "discardValue": 0,
        "resourceGameYear": 2020,
        "sourceMember": "miscPlayerPick",
        "name": "PlayerPickItemName113",
        "displayName": str(title),
        "shortName": str(title),
        "itemName": str(title),
        "detaildescription": "PlayerPickItemDetailDesc113",
        "description": str(title),
        "title": str(title),
    }


def register_cursor(
    cursor: Any,
    token: dict[str, Any],
    pool_key: str,
    title: str,
    option_count: int,
    pool: list[int],
) -> None:
    """Register a token inside its caller's existing SQLite transaction."""
    cursor.execute(
        "INSERT OR REPLACE INTO player_pick_tokens "
        "(token_id,pool_key,title,option_count,token_json,pool_json,status,created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (
            int(token["id"]), str(pool_key), str(title), int(option_count),
            _dump(token), _dump([int(value) for value in pool]), "available", int(time.time()),
        ),
    )


def available_tokens(state: Any) -> list[dict[str, Any]]:
    ensure(state)
    with state.lock:
        rows = state.conn.execute(
            "SELECT t.token_id,t.pool_key,t.title,t.option_count,t.token_json,t.pool_json "
            "FROM player_pick_tokens t JOIN items i ON i.id=t.token_id "
            "WHERE t.status='available' ORDER BY t.created_at,t.token_id"
        ).fetchall()
    return [
        {
            "token_id": int(row[0]), "pool_key": str(row[1]),
            "title": str(row[2]), "option_count": int(row[3]),
            "token": _load(row[4], {}), "pool": _load(row[5], []),
        }
        for row in rows
    ]


def token(state: Any, token_id: int) -> dict[str, Any] | None:
    wanted = int(token_id)
    return next((entry for entry in available_tokens(state) if entry["token_id"] == wanted), None)


def _active_locked(conn: Any) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT token_id FROM player_pick_sessions WHERE status='pending' "
        "ORDER BY created_at,token_id LIMIT 1"
    ).fetchone()
    if not row:
        return None
    token_id = int(row[0])
    rows = conn.execute(
        "SELECT resource_id,owned,item_json FROM player_pick_candidates "
        "WHERE token_id=? ORDER BY ordinal", (token_id,),
    ).fetchall()
    return {
        "token_id": token_id,
        "items": [_load(entry[2], {}) for entry in rows],
        "resource_ids": [int(entry[0]) for entry in rows],
        "ownership": [bool(entry[1]) for entry in rows],
    }


def active(state: Any) -> dict[str, Any] | None:
    ensure(state)
    with state.lock:
        return _active_locked(state.conn)


def begin(
    state: Any,
    token_id: int,
    items: list[dict[str, Any]],
    ownership: list[bool],
) -> tuple[dict[str, Any] | None, bool]:
    ensure(state)
    wanted = int(token_id)
    now = int(time.time())
    with state.lock:
        existing = _active_locked(state.conn)
        if existing:
            return existing, False
        valid = state.conn.execute(
            "SELECT 1 FROM player_pick_tokens t JOIN items i ON i.id=t.token_id "
            "WHERE t.token_id=? AND t.status='available'", (wanted,),
        ).fetchone()
        if not valid:
            return None, False
        try:
            state.conn.execute("BEGIN IMMEDIATE")
            state.conn.execute("DELETE FROM items WHERE id=?", (wanted,))
            state.conn.execute(
                "UPDATE player_pick_tokens SET status='redeemed',redeemed_at=? WHERE token_id=?",
                (now, wanted),
            )
            state.conn.execute(
                "INSERT INTO player_pick_sessions(token_id,status,created_at) VALUES(?,?,?)",
                (wanted, "pending", now),
            )
            for ordinal, item in enumerate(items):
                state.conn.execute(
                    "INSERT INTO player_pick_candidates"
                    "(candidate_id,token_id,ordinal,resource_id,owned,item_json) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        int(item["id"]), wanted, ordinal, int(item["resourceId"]),
                        1 if ownership[ordinal] else 0, _dump(item),
                    ),
                )
            state.conn.commit()
        except Exception:
            state.conn.rollback()
            raise
        return _active_locked(state.conn), True


def pending_flag(state: Any) -> bool:
    return active(state) is not None


def select(state: Any, selected_value: int, duplicate: bool = False) -> dict[str, Any] | None:
    ensure(state)
    value = int(selected_value)
    now = int(time.time())
    with state.lock:
        current = _active_locked(state.conn)
        if not current:
            return None
        chosen = next(
            (
                dict(item) for item in current["items"]
                if value in {
                    int(item.get(key) or 0)
                    for key in ("id", "resourceId", "assetId", "definitionId", "_definitionId")
                }
            ),
            None,
        )
        if chosen is None:
            return None
        token_id = int(current["token_id"])
        candidate_id = int(chosen["id"])
        resource_id = int(chosen["resourceId"])
        chosen.update({"pile": "unassigned", "untradeable": True, "discardValue": 0})
        if duplicate:
            chosen.update({"duplicate": True, "isDuplicate": True, "itemState": "duplicate"})
        try:
            state.conn.execute("BEGIN IMMEDIATE")
            state.conn.execute(
                "INSERT INTO items(id,resource_id,pile,data) VALUES(?,?,?,?)",
                (candidate_id, resource_id, "unassigned", _dump(chosen)),
            )
            state.conn.execute(
                "UPDATE player_pick_sessions SET status='resolved',resolved_at=?,"
                "selected_candidate_id=?,selected_resource_id=? WHERE token_id=?",
                (now, candidate_id, resource_id, token_id),
            )
            state.conn.execute(
                "UPDATE player_pick_tokens SET status='resolved',resolved_at=? WHERE token_id=?",
                (now, token_id),
            )
            state.conn.execute(
                "INSERT OR REPLACE INTO player_pick_receipts"
                "(token_id,candidate_id,resource_id,item_json,selected_at) VALUES(?,?,?,?,?)",
                (token_id, candidate_id, resource_id, _dump(chosen), now),
            )
            state.conn.execute("DELETE FROM player_pick_candidates WHERE token_id=?", (token_id,))
            state.conn.commit()
        except Exception:
            state.conn.rollback()
            raise
        return {
            "token_id": token_id, "candidate_id": candidate_id,
            "resource_id": resource_id, "item": chosen,
        }


def receipt(state: Any, selected_value: int) -> dict[str, Any] | None:
    """Return the recent exact selection for idempotent native retries."""
    ensure(state)
    value = int(selected_value)
    with state.lock:
        rows = state.conn.execute(
            "SELECT token_id,candidate_id,resource_id,item_json FROM player_pick_receipts "
            "ORDER BY selected_at DESC LIMIT 32"
        ).fetchall()
    for row in rows:
        if value in (int(row[1]), int(row[2])):
            return {
                "token_id": int(row[0]), "candidate_id": int(row[1]),
                "resource_id": int(row[2]), "item": _load(row[3], {}),
            }
    return None
