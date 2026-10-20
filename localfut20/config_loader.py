from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

LOCAL_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = LOCAL_ROOT.parent

DEFAULT_STARTERS = [24630, 210665, 200677, 198176, 172316, 205257, 184703, 184271, 234913, 235410, 223816]
DEFAULT_RESERVES: list[int] = []

DEFAULT_CONFIG: dict[str, Any] = {
    "fifa_exe": "",
    "gameReadyDelaySeconds": 5,
    "display_name": "Local Test Player",
    "test_account_id": "local-test-account-001",
    "date_of_birth": "2001-08-20",
    "minimum_age": 18,
    "nucleus_id": 1000000001,
    "persona_id": 1000000002,
    "club_id": 1,
    "club_name": "Local Test FC",
    "club_abbr": "LTF",
    "squad_id": 1,
    "squad_name": "Local Test XI",
    "credits": 70000000,
    "fut_game": "fifa20",
    "game_sku": "FFA20PCC",
    "fut_sku": "FUT20PCC",
    "bind_host": "127.0.0.1",
    "local_host": "127.0.0.1",
    "redirector_bind_host": "0.0.0.0",
    "redirector_host": "spring18.gosredirector.ea.com",
    "redirector_port": 42230,
    "tls_redirector_port": 42230,
    "redirect_port": 42231,
    "redirect_debug_port": 42231,
    "blaze_port": 44321,
    "qos_port": 17502,
    "easw_port": 42232,
    "fut_port": 8080,
    # Hosts-pinned accounts.ea.com and utas.* requests always use HTTPS.
    # Keep this explicit so launch-time checks cannot silently omit port 443.
    "fut_tls_port": 443,
    "legacy_fut_port": 8081,
    "lsx_port": 3216,
    "control_port": 47220,
    "session_lifetime_seconds": 900,
    "runtime_root": "",
    "database_path": "",
    "players_path": "localfut20/players.json",
    "certificate_path": "runtime-source/certs/spring18-cert.pem",
    "certificate_key_path": "runtime-source/certs/spring18-key.pem",
    "logging_level": "INFO",
    "pack_size": 12,
    "pack_cost": 7500,
    "special_pack_chance": 0.10,
    "rare_flag": 1,
    "debug_unknown_http": True,
    "starter_xi_only": True,
    "prefetch_pack_player_images": True,
    "prefer_catalog_player_images": True,
    "starter_players": DEFAULT_STARTERS,
    "reserve_players": DEFAULT_RESERVES,
}


def default_runtime_root() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "FIFA20LocalFUT"
    return Path.home() / "AppData" / "Local" / "FIFA20LocalFUT"


def resolve_path(value: str | os.PathLike[str] | None, base: Path | None = None) -> Path | None:
    if value is None or str(value).strip() == "":
        return None
    path = Path(value)
    if not path.is_absolute():
        path = (base or PROJECT_ROOT) / path
    return path


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    return raw if isinstance(raw, dict) else None


def _flatten(raw: dict[str, Any]) -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for key, value in raw.items():
        if not isinstance(value, dict):
            flat[key] = value
    for section in ("game", "launcher", "identity", "services", "paths", "certificates", "logging"):
        value = raw.get(section)
        if isinstance(value, dict):
            flat.update(value)
    return flat


def _int_list(value: Any, fallback: list[int]) -> list[int]:
    if not isinstance(value, list):
        return list(fallback)
    out: list[int] = []
    for item in value:
        try:
            out.append(int(item))
        except Exception:
            continue
    # An explicit empty list is meaningful: it requests an empty FUT club.
    # Only a missing/non-list setting should fall back to the demo roster.
    return out


def _coerce_ints(cfg: dict[str, Any]) -> None:
    for key in (
        "gameReadyDelaySeconds", "minimum_age", "nucleus_id", "persona_id",
        "club_id", "squad_id", "credits", "redirector_port",
        "tls_redirector_port", "redirect_debug_port", "redirect_port",
        "blaze_port", "qos_port", "easw_port", "fut_port",
        "legacy_fut_port", "fut_tls_port", "lsx_port", "control_port", "session_lifetime_seconds", "pack_size", "pack_cost",
        "rare_flag",
    ):
        try:
            cfg[key] = int(cfg[key])
        except Exception:
            cfg[key] = int(DEFAULT_CONFIG[key])


def _candidate_paths(project_root: Path) -> list[Path]:
    candidates: list[Path] = []
    env_config = os.environ.get("LOCALFUT20_CONFIG")
    if env_config:
        candidates.append(Path(env_config))
    candidates.extend([
        project_root / "launcher-settings.json",
        PROJECT_ROOT / "launcher-settings.json",
        LOCAL_ROOT / "config.json",
    ])
    seen: set[Path] = set()
    out: list[Path] = []
    for path in candidates:
        key = path.resolve() if path.exists() else path.absolute()
        if key not in seen:
            seen.add(key)
            out.append(path)
    return out


def load_config(project_root: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    base = Path(project_root).resolve() if project_root else PROJECT_ROOT
    cfg = dict(DEFAULT_CONFIG)
    config_path: Path | None = None
    for path in _candidate_paths(base):
        if not path.exists():
            continue
        raw = _read_json(path)
        if raw is None:
            continue
        cfg.update(_flatten(raw))
        config_path = path
        break

    if "gamePath" in cfg and not cfg.get("fifa_exe"):
        cfg["fifa_exe"] = cfg["gamePath"]
    if "game_path" in cfg and not cfg.get("fifa_exe"):
        cfg["fifa_exe"] = cfg["game_path"]

    if "redirector_port" in cfg:
        cfg["tls_redirector_port"] = cfg["redirector_port"]
    if "redirect_port" in cfg and "redirect_debug_port" not in cfg:
        cfg["redirect_debug_port"] = cfg["redirect_port"]

    _coerce_ints(cfg)
    cfg["redirector_port"] = int(cfg["tls_redirector_port"])
    cfg["redirect_port"] = int(cfg["redirect_debug_port"])

    cfg["starter_players"] = _int_list(cfg.get("starter_players"), DEFAULT_STARTERS)
    cfg["reserve_players"] = _int_list(cfg.get("reserve_players"), DEFAULT_RESERVES)

    runtime_root = resolve_path(str(cfg.get("runtime_root", "")), base) or default_runtime_root()
    cfg["runtime_root"] = str(runtime_root)
    cfg["database_path"] = str(
        resolve_path(str(cfg.get("database_path", "")), base)
        or (runtime_root / "data" / "localfut20.sqlite3")
    )
    cfg["players_path"] = str(resolve_path(str(cfg.get("players_path", "")), base) or (LOCAL_ROOT / "players.json"))
    cfg["certificate_path"] = str(resolve_path(str(cfg.get("certificate_path", "")), base) or (base / "runtime-source" / "certs" / "spring18-cert.pem"))
    cfg["certificate_key_path"] = str(resolve_path(str(cfg.get("certificate_key_path", "")), base) or (base / "runtime-source" / "certs" / "spring18-key.pem"))
    cfg["_config_path"] = str(config_path) if config_path else ""
    cfg["_project_root"] = str(base)
    return cfg
