"""Seed and validate the local FUT data layer without starting any service.

This module intentionally uses only JSON and SQLite. It does not import the
server, open a socket, touch routing/certificates, or inspect FIFA20.exe.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from config_loader import PROJECT_ROOT, load_config

ROOT = Path(__file__).resolve().parent
CFG = load_config(PROJECT_ROOT)
CONFIG_PATH = Path(CFG.get("_config_path") or (PROJECT_ROOT / "launcher-settings.json"))
PLAYERS_PATH = Path(str(CFG["players_path"]))
RUNTIME_ROOT = Path(str(CFG["runtime_root"]))
DB_PATH = Path(str(CFG["database_path"]))

PLAYER_FIELDS = {"name", "rating", "preferredPosition", "leagueId", "teamid", "nation", "face"}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_inputs() -> tuple[dict[str, Any], dict[int, dict[str, Any]]]:
    config = dict(CFG)
    raw_players = load_json(PLAYERS_PATH)
    if not isinstance(config, dict) or not isinstance(raw_players, dict):
        raise ValueError("config.json and players.json must contain JSON objects")
    players: dict[int, dict[str, Any]] = {}
    for raw_id, meta in raw_players.items():
        rid = int(raw_id)
        if not isinstance(meta, dict):
            raise ValueError(f"player {rid} is not an object")
        missing = PLAYER_FIELDS - set(meta)
        if missing:
            raise ValueError(f"player {rid} missing fields: {sorted(missing)}")
        if not 1 <= int(meta["rating"]) <= 99:
            raise ValueError(f"player {rid} has an invalid rating")
        if len(meta["face"]) != 6 or any(not 0 <= int(v) <= 99 for v in meta["face"]):
            raise ValueError(f"player {rid} must have six face values in [0,99]")
        players[rid] = meta
    starters = [int(x) for x in config.get("starter_players", [])]
    reserves = [int(x) for x in config.get("reserve_players", [])]
    if len(starters) not in (0, 11) or len(set(starters + reserves)) != len(starters + reserves):
        raise ValueError("config must contain either zero or 11 unique starters and unique reserves")
    if not starters and reserves:
        raise ValueError("an empty starter squad cannot contain reserve players")
    for rid in starters + reserves:
        if rid not in players:
            raise ValueError(f"configured roster player {rid} is not defined")
    return config, players


def _item(rid: int, meta: dict[str, Any], item_id: int, timestamp: int, pile: str = "club") -> dict[str, Any]:
    return {
        "id": item_id, "timestamp": timestamp, "resourceId": rid,
        "assetId": int(meta.get("assetId", rid)), "rating": int(meta["rating"]),
        "itemState": "free", "rareflag": int(meta.get("rareflag", 1)),
        "formation": "f442", "leagueId": int(meta["leagueId"]),
        "injuryType": "none", "injuryGames": 0, "lastSalePrice": 0,
        "fitness": 99, "training": 0, "suspension": 0, "contract": 7,
        "contracts": 7, "preferredPosition": str(meta["preferredPosition"]),
        "playStyle": 0, "discardValue": 700, "itemType": "player",
        "cardsubtypeid": int(meta.get("cardsubtypeid", 0) or 0), "owners": 1, "untradeable": True,
        "morale": 50, "statsList": [], "lifetimeStats": [],
        "attributeList": [{"index": i, "value": int(v)} for i, v in enumerate(meta["face"])],
        "teamid": int(meta["teamid"]), "assists": 0, "lifetimeAssists": 0,
        "loyaltyBonus": 1, "pile": pile, "nation": int(meta["nation"]),
        "resourceGameYear": 2020, "marketDataMinPrice": 150,
        "marketDataMaxPrice": 15000000,
    }


def seed() -> dict[str, Any]:
    config, players = load_inputs()
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    now = int(time.time())
    starters = [int(x) for x in config["starter_players"]]
    reserves = [int(x) for x in config.get("reserve_players", [])]
    starter_only = bool(config.get("starter_xi_only", False))
    roster = list(starters if starter_only else starters + reserves)
    starter_only_marker = "starter_xi_only_v1"

    conn.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS items(id INTEGER PRIMARY KEY, resource_id INTEGER NOT NULL,
            pile TEXT NOT NULL DEFAULT 'club', data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS squads(id INTEGER PRIMARY KEY, name TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 0, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS unopened_packs(id INTEGER PRIMARY KEY AUTOINCREMENT,
            pack_id INTEGER NOT NULL, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sbc_squads(challenge_id INTEGER PRIMARY KEY,
            data TEXT NOT NULL, updated_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS sbc_completions(challenge_id INTEGER PRIMARY KEY,
            times_completed INTEGER NOT NULL DEFAULT 0, completed_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS sbc_set_completions(set_id INTEGER PRIMARY KEY,
            times_completed INTEGER NOT NULL DEFAULT 0, completed_at INTEGER NOT NULL);
    """)

    def set_kv(key: str, value: Any) -> None:
        conn.execute(
            "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    def set_kv_default(key: str, value: Any) -> None:
        conn.execute("INSERT OR IGNORE INTO kv(key,value) VALUES(?,?)", (key, json.dumps(value)))

    def get_kv(key: str, default: Any = None) -> Any:
        row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row[0])
        except Exception:
            return default

    def seed_identity_defaults() -> None:
        set_kv_default("credits", int(config.get("credits", 0)))
        set_kv_default("account_id", str(config.get("test_account_id", "local-test-account-001")))
        set_kv_default("nucleus_id", int(config.get("nucleus_id", 1000000001)))
        set_kv_default("persona_id", int(config.get("persona_id", 1000000002)))
        set_kv_default("club_id", int(config.get("club_id", 1)))
        set_kv_default("club_name", str(config.get("club_name", "Local Test FC")))
        set_kv_default("club_abbr", str(config.get("club_abbr", "LTF")))
        set_kv_default("entitlements", [
            {"id": 1, "grantName": str(config.get("game_sku", "FFA20PCC")), "tag": "ONLINE_ACCESS", "status": "ACTIVE"},
            {"id": 2, "grantName": str(config.get("fut_sku", "FUT20PCC")), "tag": "FUT_ACCESS", "status": "ACTIVE"},
        ])

    def rebuild_roster(reset_credits: bool) -> tuple[int, int]:
        conn.execute("DELETE FROM items")
        conn.execute("DELETE FROM squads")
        conn.execute("DELETE FROM unopened_packs")
        conn.execute("DELETE FROM sbc_squads")
        conn.execute("DELETE FROM sbc_completions")
        conn.execute("DELETE FROM sbc_set_completions")
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='draft_runs'").fetchone():
            conn.execute("DELETE FROM draft_runs")

        item_ids: dict[int, int] = {}
        next_id = 900000000000
        for rid in roster:
            next_id += 1
            item_ids[rid] = next_id
            item = _item(rid, players[rid], next_id, now)
            conn.execute(
                "INSERT INTO items(id,resource_id,pile,data) VALUES(?,?,?,?)",
                (next_id, rid, "club", json.dumps(item, separators=(",", ":"))),
            )

        squad_id = int(config.get("squad_id", 1))
        squad_name = str(config.get("squad_name", "Local Test XI"))
        lineup = [
            {"index": i, "itemId": item_ids[rid], "loyaltyBonus": 1}
            for i, rid in enumerate(starters)
        ]
        squad = {
            "id": squad_id,
            "squadId": squad_id,
            "name": squad_name,
            "squadName": squad_name,
            "formation": "f442",
            "rating": round(sum(players[r]["rating"] for r in starters) / len(starters)) if starters else 0,
            "chemistry": 100 if starters else 0,
            "active": True,
            "players": lineup,
            "kicktakers": [],
        }
        conn.execute(
            "INSERT INTO squads(id,name,active,data) VALUES(?,?,1,?)",
            (squad_id, squad_name, json.dumps(squad, separators=(",", ":"))),
        )

        if reset_credits:
            set_kv("credits", int(config.get("credits", 0)))
        else:
            set_kv_default("credits", int(config.get("credits", 0)))
        set_kv("next_item_id", next_id)
        set_kv("squad_id", squad_id)
        set_kv("squad_name", squad_name)
        set_kv("owned_player_resource_ids", roster)
        seed_identity_defaults()
        return next_id, squad_id

    existing_player_count = 0
    for row in conn.execute("SELECT data FROM items").fetchall():
        try:
            if json.loads(row["data"]).get("itemType") == "player":
                existing_player_count += 1
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    has_credits = conn.execute("SELECT 1 FROM kv WHERE key='credits'").fetchone() is not None

    if starter_only and not bool(get_kv(starter_only_marker, False)):
        with conn:
            _, squad_id = rebuild_roster(reset_credits=not has_credits)
            set_kv(starter_only_marker, True)
        credits_now = get_kv("credits")
        conn.close()
        return {
            "database": str(DB_PATH),
            "players": len(players),
            "status": "starter XI only migration applied",
            "owned": len(roster),
            "starters": len(starters),
            "reserves": 0,
            "squad_id": squad_id,
            "credits": credits_now,
        }

    if existing_player_count and has_credits:
        with conn:
            seed_identity_defaults()
        credits_now = get_kv("credits")
        conn.close()
        return {
            "database": str(DB_PATH),
            "players": len(players),
            "status": "preserved existing club",
            "owned_players": existing_player_count,
            "credits": credits_now,
        }

    with conn:
        _, squad_id = rebuild_roster(reset_credits=True)
        if starter_only:
            set_kv(starter_only_marker, True)

    conn.close()
    return {
        "database": str(DB_PATH),
        "players": len(players),
        "owned": len(roster),
        "starters": len(starters),
        "reserves": 0 if starter_only else len(reserves),
        "squad_id": squad_id,
    }


if __name__ == "__main__":
    print(json.dumps(seed(), indent=2))
