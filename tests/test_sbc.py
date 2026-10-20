from __future__ import annotations

import json
import io
import logging
import os
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "localfut20"))
_TEMP_DIR = tempfile.TemporaryDirectory(prefix="localfut20-sbc-test-")
TEST_ROOT = Path(_TEMP_DIR.name)
CONFIG_PATH = TEST_ROOT / "settings.json"
CONFIG_PATH.write_text(
    json.dumps({
        "runtime_root": str(TEST_ROOT / "runtime"),
        "database_path": str(TEST_ROOT / "runtime" / "data" / "test.sqlite3"),
        "players_path": str(PROJECT_ROOT / "localfut20" / "players.json"),
        "starter_players": [],
        "reserve_players": [],
        "starter_xi_only": True,
        "credits": 10000,
        "logging_level": "WARNING",
        "prefetch_pack_player_images": False,
    }),
    encoding="utf-8",
)
os.environ["LOCALFUT20_CONFIG"] = str(CONFIG_PATH)

import server  # noqa: E402  (configuration must be installed before import)
from sbc_catalog import CHALLENGE_BY_ID, SBC_CATALOG  # noqa: E402


class SbcTests(unittest.TestCase):
    def test_general_leaderboards_are_disabled_by_retail_feature_gate(self) -> None:
        status, settings = self._response("GET", "/ut/game/fifa20/settings")
        self.assertEqual(200, status)
        self.assertIn(
            {"type": "usLeaderboardAvailable", "value": "0"},
            settings["configs"],
        )

        status, hub = self._response("GET", "/ut/game/fifa20/hub")
        self.assertEqual(200, status)
        self.assertIs(False, hub["usLeaderboardAvailable"])

    def test_general_leaderboards_remain_disabled_so_club_can_open(self) -> None:
        status, error = self._response(
            "GET", "/ut/game/fifa20/leaderboards/options"
        )
        self.assertEqual({
            "reason": "FEATURE_DISABLED", "code": "LEADERBOARDS_UNAVAILABLE",
        }, error)

    @classmethod
    def tearDownClass(cls) -> None:
        server.STATE.conn.close()
        logging.shutdown()
        _TEMP_DIR.cleanup()

    def setUp(self) -> None:
        with server.STATE.lock:
            server.STATE.conn.execute("DELETE FROM items")
            server.STATE.conn.execute("DELETE FROM squads")
            server.STATE.conn.execute("DELETE FROM unopened_packs")
            server.STATE.conn.execute("DELETE FROM sbc_squads")
            server.STATE.conn.execute("DELETE FROM sbc_completions")
            server.STATE.conn.execute("DELETE FROM sbc_set_completions")
            server.STATE.conn.execute("DELETE FROM draft_runs")
            server.STATE.conn.execute("DELETE FROM auctions")
            server.STATE.conn.execute("DELETE FROM player_pick_candidates")
            server.STATE.conn.execute("DELETE FROM player_pick_receipts")
            server.STATE.conn.execute("DELETE FROM player_pick_sessions")
            server.STATE.conn.execute("DELETE FROM player_pick_tokens")
            server.STATE.conn.execute("DELETE FROM kv WHERE key=?", (server._SB_STATE_KEY,))
            server.STATE.conn.execute("DELETE FROM kv WHERE key='clientdata_squad_building_sets'")
            server.STATE.conn.execute(
                "DELETE FROM kv WHERE key IN "
                "('pending_player_picks','player_pick_sequence','global_match_record',"
                "'global_match_record_keys','pack_recent_special_assets',"
                "'objectives_mode_record','scmp_campaign_v1','scmp_revision')"
            )
            server.STATE.conn.execute(
                "DELETE FROM kv WHERE key IN "
                "('rivals_skill_points','rivals_weekly_points','rivals_division',"
                "'rivals_games_played','weekend_league_points','champions_registered',"
                "'rivals_progress','champion_progress','seasonal_mode')"
            )
            server.STATE.conn.execute(
                "INSERT INTO kv(key,value) VALUES('credits',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps(10000),),
            )
            server.STATE.conn.commit()
        # SCMP active-campaign refresh tracking is process-local because the
        # retail client has one active FUT session. Tests reset SQLite between
        # cases, so reset the corresponding client-seen revision too.
        with server._scmp_campaign_refresh_lock:
            server._scmp_stale_revision = 0
            server._scmp_last_active_fetch_revision = 0
        with server._scmp_push_lock:
            server._scmp_push_revision = 0
            server._scmp_push_remaining = 0
            server._scmp_push_next_at = 0.0

    @staticmethod
    def _response(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        status, _, body = server.route_fut(
            method,
            path,
            {"Content-Type": "application/json"},
            json.dumps(payload or {}).encode("utf-8"),
        )
        return status, json.loads(body.decode("utf-8"))

    def _seed_bronze_players(self, count: int = 11) -> list[int]:
        item_ids = []
        with server.STATE.lock:
            for offset in range(count):
                item_id = 990000000000 + offset
                item_ids.append(item_id)
                item = {
                    "id": item_id,
                    "resourceId": 500000 + offset,
                    "assetId": 500000 + offset,
                    "itemType": "player",
                    "pile": "club",
                    "rating": 64,
                    "rareflag": 0,
                    "nation": 1,
                    "leagueId": 1,
                    "teamid": 1,
                    "preferredPosition": "CM",
                }
                server.STATE.conn.execute(
                    "INSERT INTO items(id,resource_id,pile,data) VALUES(?,?,?,?)",
                    (item_id, item["resourceId"], "club", json.dumps(item)),
                )
            server.STATE.conn.commit()
        return item_ids

    def test_catalog_exposes_fifa20_foundation_advanced_and_upgrades(self) -> None:
        self.assertEqual(20, len(SBC_CATALOG))
        self.assertEqual(45, len(CHALLENGE_BY_ID))
        status, response = self._response("GET", "/ut/game/fifa20/sbs/sets")
        self.assertEqual(200, status)
        self.assertEqual(["LIVE", "BASIC", "ADVANCED", "UPGRADES", "PLAYERS"],
                         [x["name"] for x in response["categories"]])
        all_sets = [entry for category in response["categories"] for entry in category["sets"]]
        self.assertIn("Hybrid Leagues", [entry["name"] for entry in all_sets])
        self.assertIn("Flashback Ricardo Quaresma", [entry["name"] for entry in all_sets])
        restored_stable = {
            "Icons of the Era", "Icon Moments Showcase", "Festival Roulette",
            "Festival Foundations", "Shapeshifters Laboratory", "FUT Birthday Encore",
            "Icon Moments Player Pick", "FUT Birthday Player Pick",
        }
        self.assertTrue(restored_stable <= {entry["name"] for entry in all_sets})
        self.assertFalse(any(
            challenge["name"] == "National Pride"
            for set_entry in SBC_CATALOG for challenge in set_entry["challenges"]
        ))
        self.assertTrue(all(entry["tutorial"] is False for entry in all_sets))
        first_pack = next(
            award for entry in all_sets for award in entry["awards"]
            if award["type"] == "pack"
        )
        self.assertEqual("pack", first_pack["type"])
        self.assertEqual("pack", first_pack["awardType"])
        self.assertTrue(first_pack["displayName"])
        self.assertGreater(first_pack["packId"], 0)

        # Earned SBC packs must use hidden My Packs definitions, never the
        # visible coin-store articles (IDs 1-7).
        all_awards = []
        for set_entry in SBC_CATALOG:
            all_awards.extend(set_entry.get("awards", []))
            for challenge in set_entry.get("challenges", []):
                all_awards.extend(challenge.get("awards", []))
        pack_ids = [
            int(award["pack_id"])
            for award in all_awards
            if str(award.get("type", "")).upper() == "PACK"
        ]
        self.assertTrue(pack_ids)
        self.assertTrue(all(
            server.get_pack_cfg(pack_id).get("reward_only") is True
            for pack_id in pack_ids
        ))

    def test_store_uses_balanced_fifa_style_upgrade_packs(self) -> None:
        expected = {
            4: ("Breakthrough Pack", 6, 81, 0, 0.14, 15000),
            5: ("Birthday Shift Pack", 8, 83, 2, 0.26, 40000),
            6: ("Promo Constellation Pack", 12, 85, 5, 0.42, 90000),
            7: ("Endgame Pantheon Pack", 15, 88, 11, 0.72, 200000),
        }
        for pack_id, values in expected.items():
            cfg = server.get_pack_cfg(pack_id)
            actual = (
                cfg["name"], cfg["size"], cfg["min_rating"],
                cfg["guaranteed_specials"], cfg["special_chance"], cfg["price"],
            )
            self.assertEqual(values, actual)

        store = server._store_purchasegroups()["purchase"]
        visible_names = {entry["name"] for entry in store if not entry["isMyPack"]}
        self.assertTrue(all(name in visible_names for name, *_ in expected.values()))
        top_article = next(entry for entry in store if entry["id"] == 7)
        self.assertEqual(4, top_article["packAssetId"])
        self.assertEqual(4, top_article["assetId"])
        self.assertFalse(any(
            token in name
            for name in visible_names
            for token in ("Inde Nav", "Gattouz", "Mia Khalifa", "Jaw-Dropper")
        ))

        top_pack = server.STATE.create_pack(7)
        self.assertEqual(15, top_pack["numberItems"])
        self.assertTrue(all(item["rating"] >= 88 for item in top_pack["itemData"]))
        self.assertGreaterEqual(sum(
            int(item.get("rareflag", 0) or 0) > 1 for item in top_pack["itemData"]
        ), 11)

        self.assertFalse(server.get_pack_cfg(21))
        self.assertFalse(any(entry["id"] == 21 for entry in store))
        with self.assertRaises(RuntimeError):
            server.STATE.create_pack(21)

    def test_sbc_rewards_support_exact_packs_and_specific_player_cards(self) -> None:
        for set_entry in SBC_CATALOG:
            for award in [*set_entry["awards"], *[
                reward for challenge in set_entry["challenges"] for reward in challenge["awards"]
            ]]:
                if award["type"] == "PACK":
                    self.assertTrue(server.get_pack_cfg(award["pack_id"]))
                    self.assertEqual("pack", server._sbc_awards_payload([award])[0]["type"])
                elif award["type"] == "PLAYER_PICK":
                    preview = server._sbc_awards_payload([award])[0]
                    self.assertEqual("misc", preview["type"])
                    self.assertEqual("playerPick", preview["awardType"])

        set_entry, challenge = CHALLENGE_BY_ID[100006]
        self.assertFalse(set_entry["repeatable"])
        self.assertEqual([], challenge["awards"])
        self.assertEqual(67129639, set_entry["awards"][0]["resource_id"])
        preview_award = server._sbc_challenge_payload(set_entry, challenge, 0)["awards"][0]
        self.assertEqual(67129639, preview_award["resourceId"])
        self.assertEqual("Quaresma", preview_award["name"])
        self.assertEqual(89, preview_award["rating"])
        self.assertEqual(67129639, preview_award["itemData"]["resourceId"])

        server.STATE.complete_sbc(
            100006, 532, [], challenge["awards"], [100006], set_entry["awards"], False,
        )
        unassigned = server.STATE.list_items("unassigned")
        self.assertEqual(1, len(unassigned))
        self.assertEqual(67129639, unassigned[0]["resourceId"])
        self.assertTrue(unassigned[0]["untradeable"])
        self.assertEqual(0, unassigned[0]["discardValue"])
        with self.assertRaises(ValueError):
            server.STATE.complete_sbc(
                100006, 532, [], challenge["awards"], [100006],
                set_entry["awards"], False,
            )

    def test_two_curated_sbc_player_picks_grant_named_native_tokens(self) -> None:
        expected = {
            42: (4201, "icon_moments", "1 of 3 Icon Moments Player Pick"),
            43: (4301, "fut_birthday", "1 of 3 FUT Birthday Player Pick"),
        }
        for set_id, (challenge_id, pool, title) in expected.items():
            set_entry, challenge = CHALLENGE_BY_ID[challenge_id]
            preview = server._sbc_awards_payload(set_entry["awards"])[0]
            self.assertEqual("misc", preview["type"])
            self.assertEqual("playerPick", preview["awardType"])
            self.assertEqual(title, preview["displayName"])
            self.assertEqual(pool, preview["playerPickPool"])
            self.assertEqual(server.PICK_STATE.REAL_PICK_RESOURCE_ID, preview["resourceId"])

            server.STATE.complete_sbc(
                challenge_id, set_id, [], challenge["awards"],
                [challenge_id], set_entry["awards"], True,
            )
            token = max(
                (
                    item for item in server.STATE.list_items("unassigned")
                    if item.get("resourceId") == server.PICK_STATE.REAL_PICK_RESOURCE_ID
                ),
                key=lambda item: int(item["id"]),
            )
            self.assertEqual(title, token["displayName"])
            registered = server.PICK_STATE.token(server.STATE, token["id"])
            self.assertIsNotNone(registered)
            self.assertEqual(pool, registered["pool_key"])

    def test_sbc_group_rewards_match_verified_fifa20_sources(self) -> None:
        expected = {
            1: ("PACK", 1010), 2: ("PACK", 1004), 10: ("PACK", 1019),
            11: ("PACK", 1023), 20: ("PACK", 1009), 21: ("PACK", 1009),
            22: ("COINS", 12000), 30: ("PACK", 1012), 31: ("PACK", 1011),
            32: ("PACK", 1010), 532: ("PLAYER", 67129639),
            542: ("PACK", 1009),
        }
        self.assertTrue(expected.keys() <= {entry["set_id"] for entry in SBC_CATALOG})
        for set_entry in (entry for entry in SBC_CATALOG if entry["set_id"] in expected):
            with self.subTest(set_id=set_entry["set_id"]):
                award_type, value = expected[set_entry["set_id"]]
                award = set_entry["awards"][0]
                self.assertEqual(award_type, award["type"])
                actual = award.get("pack_id", award.get("resource_id", award.get("value")))
                self.assertEqual(value, actual)
                if award_type == "PACK":
                    self.assertTrue(server.get_pack_cfg(value))
                group_wire = server._sbc_set_summary(set_entry, {}, {})["awards"]
                expected_wire_type = {
                    "PACK": "pack", "COINS": "coin", "PLAYER": "player",
                }[award_type]
                self.assertEqual([(value, 1, expected_wire_type)], [
                    (wire_award["value"], wire_award["count"], wire_award["type"])
                    for wire_award in group_wire
                ])
                if len(set_entry["challenges"]) == 1:
                    challenge = set_entry["challenges"][0]
                    self.assertEqual([], challenge["awards"])
                    challenge_wire = server._sbc_challenge_payload(
                        set_entry, challenge, 0,
                    )["awards"]
                    self.assertEqual(group_wire, challenge_wire)

    def test_four_leaf_clover_has_all_real_mini_challenges_and_rewards(self) -> None:
        set_entry = next(entry for entry in SBC_CATALOG if entry["set_id"] == 542)
        self.assertEqual("LIVE", set_entry["category"])
        self.assertFalse(set_entry["repeatable"])
        self.assertEqual(1009, set_entry["awards"][0]["pack_id"])
        challenges = {entry["name"]: entry for entry in set_entry["challenges"]}
        self.assertEqual(
            {"The Shamrock", "The Green Nations", "Celtic Knot", "Rainbow's End"},
            set(challenges),
        )
        self.assertEqual(
            [1004, 1041, 1042, 1006],
            [entry["awards"][0]["pack_id"] for entry in set_entry["challenges"]],
        )
        shamrock_rules = {
            (entry["kind"], entry["scope"], entry["value"], entry["count"])
            for entry in challenges["The Shamrock"]["rules"]
        }
        self.assertIn(("specific_club", "min", 306, 1), shamrock_rules)
        green_rules = {
            (entry["kind"], entry["scope"], entry["value"], entry["count"])
            for entry in challenges["The Green Nations"]["rules"]
        }
        self.assertIn(("specific_nation", "min", 25, 1), green_rules)
        self.assertIn(("specific_nation", "min", 35, 1), green_rules)
        self.assertIn(("nation_count", "exact", 2, -1), green_rules)

    def test_all_ansu_fati_versions_are_excluded_from_new_packs(self) -> None:
        fati_resource_ids = {
            rid for rid, meta in server.PLAYER_DB.items()
            if int(meta.get("assetId", 0) or 0) == 253004
        }
        self.assertEqual({253004, 50584652, 67361868}, fati_resource_ids)
        self.assertTrue(fati_resource_ids <= server.PACK_EXCLUDED_RESOURCE_IDS)
        self.assertIn(253004, server.PACK_EXCLUDED_ASSET_IDS)
        self.assertTrue(all(
            server._player_is_excluded_from_packs(rid, server.PLAYER_DB[rid])
            for rid in fati_resource_ids
        ))
        for _ in range(5):
            pack = server.STATE.create_pack(7)
            self.assertFalse(fati_resource_ids & {
                item["resourceId"] for item in pack["itemData"]
            })

    def test_wez_and_miz_are_distinct_live_promo_packs(self) -> None:
        wez = server.get_pack_cfg(23)
        miz = server.get_pack_cfg(24)
        self.assertEqual("Wez Pack", wez["name"])
        self.assertEqual("Miz Special Pack", miz["name"])
        self.assertTrue(wez["enabled"] and miz["enabled"])
        self.assertEqual(4, wez["pack_asset_id"])
        self.assertEqual(4, miz["pack_asset_id"])
        self.assertLess(wez["price"], miz["price"])
        self.assertLess(wez["min_rating"], miz["min_rating"])
        self.assertLess(wez["guaranteed_specials"], miz["guaranteed_specials"])
        store = server._store_purchasegroups()["purchase"]
        self.assertEqual({23, 24}, {
            entry["id"] for entry in store if entry["id"] in (23, 24)
        })
        for pack_id, minimum, specials in ((23, 86, 7), (24, 89, 12)):
            pack = server.STATE.create_pack(pack_id)
            self.assertTrue(all(item["rating"] >= minimum for item in pack["itemData"]))
            self.assertGreaterEqual(sum(
                int(item.get("rareflag", 0) or 0) > 1 for item in pack["itemData"]
            ), specials)

    def test_recent_record_breaker_aguero_cannot_repeat_in_top_packs(self) -> None:
        aguero = 134370807
        self.assertEqual(6, server.player_meta(aguero)["rareflag"])
        asset_id = server._native_player_asset_id(server.player_meta(aguero), aguero)
        server.STATE.set("pack_recent_special_assets", [asset_id])
        for _ in range(3):
            pack = server.STATE.create_pack(7)
            self.assertNotIn(aguero, [item["resourceId"] for item in pack["itemData"]])

    def test_all_ribery_versions_are_excluded_from_new_packs(self) -> None:
        ribery_resource_ids = {
            rid for rid, meta in server.PLAYER_DB.items()
            if server._native_player_asset_id(meta, rid) == 156616
        }
        self.assertEqual({156616, 50488264, 84042696}, ribery_resource_ids)
        self.assertTrue(ribery_resource_ids <= server.PACK_EXCLUDED_RESOURCE_IDS)
        self.assertIn(156616, server.PACK_EXCLUDED_ASSET_IDS)
        self.assertTrue(all(
            server._player_is_excluded_from_packs(rid, server.PLAYER_DB[rid])
            for rid in ribery_resource_ids
        ))
        for _ in range(5):
            pack = server.STATE.create_pack(7)
            self.assertFalse(ribery_resource_ids & {
                item["resourceId"] for item in pack["itemData"]
            })

    def test_excluded_player_families_cannot_enter_any_player_pack_or_pick(self) -> None:
        forbidden_assets = server.PACK_EXCLUDED_ASSET_IDS
        for pack_id in (3, 7, 23, 24):
            pack = server.STATE.create_pack(pack_id)
            self.assertFalse(forbidden_assets & {
                int(server.player_meta(item["resourceId"]).get("assetId", 0))
                for item in pack["itemData"]
            })
        for pool in ("promo", "fut_birthday", "shapeshifters", "icon_moments"):
            picks = server._generate_player_pick_resources(pool, 3)
            self.assertFalse(forbidden_assets & {
                int(server.player_meta(rid).get("assetId", 0)) for rid in picks
            })
        self.assertTrue(server._player_is_excluded_from_packs(
            999_999_999, {"assetId": 253004, "name": "Unknown"}))
        self.assertTrue(server._player_is_excluded_from_packs(
            999_999_998, {"assetId": 0, "name": "Franck Ribéry"}))

    def test_global_record_is_persistent_and_idempotent(self) -> None:
        self.assertTrue(server.STATE.record_global_match("WIN", "draft:1"))
        self.assertFalse(server.STATE.record_global_match("WIN", "draft:1"))
        self.assertTrue(server.STATE.record_global_match("DRAW", "squad_battles:1"))
        self.assertTrue(server.STATE.record_global_match("LOSS", "rivals:1"))
        self.assertEqual(
            {"wins": 1, "draws": 1, "losses": 1, "matchesPlayed": 3},
            server.STATE.global_match_record(),
        )
        self.assertEqual((1, 1, 1), (
            server._native_user()["won"],
            server._native_user()["draw"],
            server._native_user()["loss"],
        ))

    def test_objective_modes_are_isolated_between_fut_draft_and_squad_battles(self) -> None:
        self._response("GET", "/ut/game/fifa20/scmp/campaign/active")
        self.assertTrue(server.STATE.record_global_match("WIN", "draft:mode-draft"))
        metrics = server._scmp_metrics(server._scmp_state())
        self.assertEqual({"matches": 1, "wins": 1}, metrics["draft"])
        self.assertEqual({"matches": 0, "wins": 0}, metrics["squad_battles"])
        self.assertEqual({"matches": 0, "wins": 0}, metrics["fut"])

        self.assertTrue(server.STATE.record_global_match("WIN", "squad_battles:mode-sb"))
        metrics = server._scmp_metrics(server._scmp_state())
        self.assertEqual({"matches": 1, "wins": 1}, metrics["squad_battles"])
        self.assertEqual({"matches": 0, "wins": 0}, metrics["fut"])

        self.assertTrue(server.STATE.record_global_match("WIN", "generic:mode-fut"))
        metrics = server._scmp_metrics(server._scmp_state())
        self.assertEqual({"matches": 1, "wins": 1}, metrics["fut"])

    def test_draft_objectives_map_debut_victory_and_run_to_draft_only(self) -> None:
        self._response("GET", "/ut/game/fifa20/scmp/campaign/active")
        # Loss: counts as a played Draft match, never as Draft Victory.
        self.assertTrue(server.STATE.record_global_match(
            "LOSS", "draft:mapped-1", xp_eligible=False
        ))
        campaign = server._scmp_campaign()
        draft = campaign["categoriesList"][0]["groupsList"][2]
        self.assertEqual([1, 0, 1], [
            objective["currentProgress"] for objective in draft["objectives"]
        ])
        self.assertEqual("COMPLETED", draft["objectives"][0]["state"])
        self.assertEqual("IN_PROGRESS", draft["objectives"][1]["state"])
        self.assertEqual("IN_PROGRESS", draft["objectives"][2]["state"])

        # A real Draft win completes Draft Victory and also increments matches.
        self.assertTrue(server.STATE.record_global_match(
            "WIN", "draft:mapped-2", xp_eligible=True
        ))
        campaign = server._scmp_campaign()
        draft = campaign["categoriesList"][0]["groupsList"][2]
        self.assertEqual([1, 1, 2], [
            objective["currentProgress"] for objective in draft["objectives"]
        ])
        self.assertEqual("COMPLETED", draft["objectives"][1]["state"])

        # Four settled Draft matches complete Draft Run.
        self.assertTrue(server.STATE.record_global_match(
            "LOSS", "draft:mapped-3", xp_eligible=False
        ))
        self.assertTrue(server.STATE.record_global_match(
            "LOSS", "draft:mapped-4", xp_eligible=False
        ))
        campaign = server._scmp_campaign()
        draft = campaign["categoriesList"][0]["groupsList"][2]
        self.assertEqual([1, 1, 4], [
            objective["currentProgress"] for objective in draft["objectives"]
        ])
        self.assertEqual("COMPLETED", draft["objectives"][2]["state"])

        # Draft/SQBT must not advance ordinary FUT Getting Started counters.
        getting_started = campaign["categoriesList"][0]["groupsList"][0]
        self.assertEqual([0, 0, 0], [
            objective["currentProgress"] for objective in getting_started["objectives"]
        ])

    def test_objective_revision_queues_login_session_soft_refresh(self) -> None:
        self._response("GET", "/ut/game/fifa20/scmp/campaign/active")
        self.assertTrue(server.STATE.record_global_match(
            "LOSS", "draft:soft-refresh", xp_eligible=False
        ))
        self.assertEqual(1, server._scmp_push_revision)
        self.assertEqual(3, server._scmp_push_remaining)

        class Sink:
            def __init__(self) -> None:
                self.frames = []
            def sendall(self, data: bytes) -> None:
                self.frames.append(bytes(data))

        sink = Sink()
        with server._scmp_push_lock:
            server._scmp_push_next_at = 0.0
        server._maybe_send_scmp_live_refresh(sink)
        self.assertEqual(3, len(sink.frames))
        # Fire2 header: component bytes 6:8, command bytes 8:10.
        commands = [
            (int.from_bytes(frame[6:8], "big"), int.from_bytes(frame[8:10], "big"))
            for frame in sink.frames
        ]
        self.assertEqual([(30722, 8), (30722, 1), (30722, 5)], commands)

    def test_two_seasonal_matches_without_gids_count_separately(self) -> None:
        self._response("GET", "/ut/game/fifa20/rivals/user/hub")
        for _ in range(2):
            self.assertEqual(200, self._response("POST", "/ut/game/fifa20/match", {})[0])
            self.assertEqual(200, self._response(
                "POST", "/ut/game/fifa20/match/end", {"endReason": "WIN"}
            )[0])
        self.assertEqual(2, server.STATE.global_match_record()["wins"])

    def test_mixed_concept_card_keeps_regular_squad_and_blocks_play(self) -> None:
        owned_id = self._seed_bronze_players()[0]
        concept_resource = max(
            server.PLAYER_DB,
            key=lambda rid: int(server.PLAYER_DB[rid].get("rating", 0) or 0),
        )
        squad = server.STATE.save_squad({
            "squadName": "My Planning XI",
            "active": True,
            "players": [
                {"index": 0, "itemData": {"id": owned_id}},
                {"index": 1, "itemData": {
                    "id": 800_000_000_000 + concept_resource,
                    "resourceId": concept_resource,
                    "dream": True,
                }},
            ],
        }, create=True)
        self.assertEqual("REGULAR_SQUAD", squad["squadType"])
        self.assertEqual(owned_id, squad["players"][0]["itemId"])
        self.assertEqual(concept_resource, squad["players"][1]["conceptResourceId"])
        self.assertEqual(11, self._response(
            "GET", "/ut/game/fifa20/club?type=player"
        )[1]["itemCount"])
        status, blocked = self._response("POST", "/ut/game/fifa20/match", {})
        self.assertEqual(409, status)
        self.assertEqual("CONCEPT_SQUAD_NOT_PLAYABLE", blocked["reason"])

    def test_store_exposes_curated_standard_range_without_three_removed_gold_packs(self) -> None:
        expected = {
            1: ("Bronze Pack", "bronze", 400, 12, 1, 1),
            8: ("Premium Bronze Pack", "bronze", 750, 12, 3, 1),
            9: ("Jumbo Premium Bronze Pack", "bronze", 1500, 24, 7, 1),
            2: ("Silver Pack", "silver", 2500, 12, 1, 2),
            10: ("Premium Silver Pack", "silver", 3750, 12, 3, 2),
            11: ("Jumbo Premium Silver Pack", "silver", 7500, 24, 7, 2),
            3: ("Gold Pack", "gold", 5000, 12, 1, 3),
            12: ("Premium Gold Pack", "gold", 7500, 12, 3, 3),
            13: ("Jumbo Premium Gold Pack", "gold", 15000, 24, 7, 3),
            17: ("Rare Players Pack", "gold", 50000, 12, 12, 3),
            18: ("Jumbo Rare Players Pack", "gold", 100000, 24, 24, 3),
        }
        rows = {
            row["id"]: row for row in server._store_purchasegroups()["purchase"]
            if not row["isMyPack"]
        }
        for pack_id, (name, group, price, size, rares, asset_id) in expected.items():
            cfg = server.get_pack_cfg(pack_id)
            self.assertEqual((name, price, size, rares), (
                cfg["name"], cfg["price"], cfg["size"], cfg["rare_slots"],
            ))
            self.assertEqual(group, rows[pack_id]["displayGroup"]["value"])
            self.assertEqual(asset_id, rows[pack_id]["packAssetId"])
        self.assertTrue({14, 15, 16}.isdisjoint(rows))

        # Hiding the coin-store articles must not suppress a legitimately
        # earned copy from My Packs.
        server.STATE.grant_unopened_pack(14, json.dumps({"source": "test reward"}))
        earned = [
            row for row in server._store_purchasegroups()["purchase"]
            if row["isMyPack"] and row["id"] == 14
        ]
        self.assertEqual(1, len(earned))
        self.assertEqual("Gold Players Pack", earned[0]["name"])

    def test_legacy_novelty_pack_settings_are_migrated_without_overwriting_custom_packs(self) -> None:
        legacy = server._merge_pack_settings({"packs": {
            "5": {"name": "Inde Nav's Uncharted Pack", "size": 12, "price": 0},
            "6": {"name": "My Custom Pack", "size": 4, "price": 12345},
            "7": {"name": "85+ x10 Rare Gold Players Pack", "size": 10, "price": 100000},
            "21": {"name": "OtaGoat Special Pack", "size": 25, "price": 69},
        }})

        self.assertEqual("Birthday Shift Pack", legacy["packs"]["5"]["name"])
        self.assertEqual(8, legacy["packs"]["5"]["size"])
        self.assertEqual("My Custom Pack", legacy["packs"]["6"]["name"])
        self.assertEqual(4, legacy["packs"]["6"]["size"])
        self.assertEqual(12345, legacy["packs"]["6"]["price"])
        self.assertEqual(4, legacy["packs"]["7"]["pack_asset_id"])
        self.assertEqual("Endgame Pantheon Pack", legacy["packs"]["7"]["name"])
        self.assertNotIn("21", legacy["packs"])
        self.assertEqual("Gold Position Modifiers Pack", legacy["packs"]["22"]["name"])

        graded = server._merge_pack_settings({"packs": {
            "4": {"name": "GRADE I - Breakthrough Pack", "price": 15000},
            "7": {"name": "GRADE IV - Endgame Pantheon Pack", "price": 200000},
        }})
        self.assertEqual("Breakthrough Pack", graded["packs"]["4"]["name"])
        self.assertEqual("Endgame Pantheon Pack", graded["packs"]["7"]["name"])
        self.assertEqual(15000, graded["packs"]["4"]["price"])

        custom = server._merge_pack_settings({"packs": {
            "7": {"name": "My Pack Seven", "pack_asset_id": 3},
        }})
        self.assertEqual(3, custom["packs"]["7"]["pack_asset_id"])

    def test_native_fifa20_challenge_and_squad_wire_shapes(self) -> None:
        status, response = self._response(
            "GET", "/ut/game/fifa20/sbs/setId/1/challenges"
        )
        self.assertEqual(200, status)
        self.assertEqual({"challenges"}, set(response))
        challenge = response["challenges"][0]
        self.assertNotIn("playerRequirements", challenge)
        self.assertEqual("BRICK_CHALLENGE", challenge["type"])
        self.assertEqual("AND", challenge["elgOperation"])
        self.assertTrue(challenge["elgReq"])
        self.assertTrue(all(
            set(rule) == {"type", "eligibilitySlot", "eligibilityKey", "eligibilityValue"}
            for rule in challenge["elgReq"]
        ))
        # FIFA derives the exact occupied-player count from playerRequirements.
        # A second raw PLAYER_COUNT (key 2) is the broken red duplicate rule.
        self.assertEqual([3, 13], [rule["eligibilityKey"] for rule in challenge["elgReq"]])
        self.assertNotIn(2, [rule["eligibilityKey"] for rule in challenge["elgReq"]])

        status, squad_response = self._response(
            "GET", "/ut/game/fifa20/sbs/challenge/101/squad"
        )
        self.assertEqual(200, status)
        self.assertEqual({"challengeId", "playerRequirements", "squad"}, set(squad_response))
        self.assertEqual(11, len(squad_response["playerRequirements"]))
        self.assertEqual(1, sum(
            slot["playerType"] == "DEFAULT"
            for slot in squad_response["playerRequirements"]
        ))
        self.assertEqual(23, len(squad_response["squad"]["players"]))
        self.assertEqual(1, len(squad_response["squad"]["manager"]))
        empty_item = squad_response["squad"]["players"][0]["itemData"]
        self.assertEqual("invalid", empty_item["itemState"])
        self.assertEqual(2020, empty_item["resourceGameYear"])

    def test_empty_post_opens_an_isolated_empty_workspace(self) -> None:
        active_item_ids = self._seed_bronze_players()
        with server.STATE.lock:
            server.STATE.conn.execute(
                "INSERT INTO squads(id,name,active,data) VALUES(?,?,?,?)",
                (1, "Active XI", 1, json.dumps({"players": active_item_ids})),
            )
            server.STATE.conn.commit()

        status, response = self._response(
            "POST", "/ut/game/fifa20/sbs/challenge/101", {}
        )
        self.assertEqual(200, status)
        self.assertEqual({"challengeId", "playerRequirements", "squad"}, set(response))
        self.assertTrue(all(
            slot["itemData"]["id"] == 0 for slot in response["squad"]["players"]
        ))
        self.assertEqual(({}, {}), server.STATE.sbc_completion_counts())
        self.assertEqual({}, server.STATE.unopened_pack_counts())
        self.assertTrue(all(server.STATE.get_item(item_id) is not None for item_id in active_item_ids))

    def test_empty_post_never_submits_a_valid_saved_workspace(self) -> None:
        item_id = self._seed_bronze_players()[0]
        self._response(
            "PUT", "/ut/game/fifa20/sbs/challenge/101/squad", {
                "chemistry": 4,
                "formation": "f442",
                "players": [{"index": 0, "itemData": {"id": item_id, "dream": False}}],
            },
        )

        status, opened = self._response(
            "POST", "/ut/game/fifa20/sbs/challenge/101", {}
        )
        self.assertEqual(200, status)
        self.assertEqual({"challengeId", "playerRequirements", "squad"}, set(opened))
        self.assertIsNotNone(server.STATE.get_item(item_id))
        self.assertEqual(({}, {}), server.STATE.sbc_completion_counts())
        self.assertEqual({}, server.STATE.unopened_pack_counts())

    def test_native_put_submit_completes_mini_challenge_and_persists_marker(self) -> None:
        item_id = self._seed_bronze_players()[0]
        status, saved = self._response(
            "PUT", "/ut/game/fifa20/sbs/challenge/101/squad", {
                "chemistry": 4,
                "formation": "f442",
                "players": [{"index": 0, "itemData": {"id": item_id, "dream": False}}],
            },
        )
        self.assertEqual(200, status)
        self.assertEqual(1, saved["savedPlayerCount"])

        # This is the exact submit request emitted by FIFA 20 PC.  It carries
        # an empty JSON object because the squad was saved by the prior PUT.
        status, completed = self._response(
            "PUT",
            "/ut/game/fifa20/sbs/challenge/101?skipUserSquadValidation=false",
            {},
        )
        self.assertEqual(200, status)
        self.assertTrue(completed["success"])
        self.assertTrue(completed["challengeComplete"])
        self.assertEqual(1, completed["timesCompleted"])
        self.assertIsNone(server.STATE.get_item(item_id))
        self.assertEqual({1024: 1}, server.STATE.unopened_pack_counts())

        status, marker = self._response(
            "PUT", "/ut/game/fifa20/clientdata/squadBuildingSets",
            {"entries": [{"key": 1, "value": 1}]},
        )
        self.assertEqual(200, status)
        self.assertEqual({"entries": [{"key": 1, "value": 1}]}, marker)
        self.assertEqual(marker, self._response(
            "GET", "/ut/game/fifa20/clientdata/squadBuildingSets"
        )[1])

        _, challenges = self._response(
            "GET", "/ut/game/fifa20/sbs/setId/1/challenges"
        )
        challenge = next(entry for entry in challenges["challenges"] if entry["challengeId"] == 101)
        self.assertEqual("COMPLETED", challenge["status"])
        self.assertEqual(1, challenge["timesCompleted"])

        # Native retries must remain idempotent and never issue the reward twice.
        _, retried = self._response(
            "PUT", "/ut/game/fifa20/sbs/challenge/101?skipUserSquadValidation=false", {}
        )
        self.assertTrue(retried["alreadyCompleted"])
        self.assertEqual({1024: 1}, server.STATE.unopened_pack_counts())

    def test_sbc_art_paths_return_a_cacheable_png(self) -> None:
        status, headers, body = server.route_fut(
            "GET",
            "/fut/sbc/gen4/challenges/images/sbc_challenge_image_100101-localfut20-sbc.png",
            {},
            b"",
        )
        self.assertEqual(200, status)
        self.assertEqual("image/png", headers["Content-Type"])
        self.assertTrue(body.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_club_picker_returns_native_items_and_filters_exact_positions(self) -> None:
        item_ids = self._seed_bronze_players()
        with server.STATE.lock:
            for offset, position in enumerate(("LW", "LW", "CM", "LB")):
                item_id = item_ids[offset]
                row = server.STATE.conn.execute(
                    "SELECT data FROM items WHERE id=?", (item_id,)
                ).fetchone()
                item = json.loads(row["data"])
                item["preferredPosition"] = position
                item["rating"] = 60 + offset
                server.STATE.conn.execute(
                    "UPDATE items SET data=? WHERE id=?", (json.dumps(item), item_id)
                )
            server.STATE.conn.commit()

        status, response = self._response(
            "GET",
            "/ut/game/fifa20/club?year=2020&type=player&count=11&position=LW&level=bronze&sortBy=ovr&sort=desc",
        )
        self.assertEqual(200, status)
        self.assertEqual({"itemData", "itemCount"}, set(response))
        self.assertEqual(2, response["itemCount"])
        self.assertEqual(["LW", "LW"], [item["preferredPosition"] for item in response["itemData"]])
        self.assertGreaterEqual(response["itemData"][0]["rating"], response["itemData"][1]["rating"])
        self.assertTrue(all(item["resourceGameYear"] == 2020 for item in response["itemData"]))

        # FIFA sends full definition/resource IDs in excldef. Special cards
        # have a resourceId that differs from their base-player assetId.
        special_id = item_ids[0]
        with server.STATE.lock:
            row = server.STATE.conn.execute(
                "SELECT data FROM items WHERE id=?", (special_id,)
            ).fetchone()
            special = json.loads(row["data"])
            special["resourceId"] = 117461313
            special["assetId"] = 20801
            server.STATE.conn.execute(
                "UPDATE items SET resource_id=?,data=? WHERE id=?",
                (117461313, json.dumps(special), special_id),
            )
            server.STATE.conn.commit()
        status, excluded_special = self._response(
            "GET",
            "/ut/game/fifa20/club?type=player&position=LW&excldef=117461313",
        )
        self.assertEqual(200, status)
        self.assertEqual(1, excluded_special["itemCount"])
        self.assertNotIn(special_id, [item["id"] for item in excluded_special["itemData"]])

        excluded_asset = response["itemData"][0]["assetId"]
        status, excluded = self._response(
            "GET",
            f"/ut/game/fifa20/club?type=player&position=LW&excldef={excluded_asset}",
        )
        self.assertEqual(200, status)
        self.assertEqual(1, excluded["itemCount"])

    def test_club_picker_filters_native_role_types_and_card_quality(self) -> None:
        item_ids = self._seed_bronze_players()
        positions = ("GK", "LB", "CB", "RB", "CDM", "CM", "CAM", "LW", "ST", "RW", "LM")
        ratings = (80, 81, 95, 74, 64, 70, 72, 73, 62, 63, 64)
        rarities = (0, 1, 5, 5, 5, 0, 0, 0, 0, 0, 0)
        with server.STATE.lock:
            for item_id, position, rating, rarity in zip(
                item_ids, positions, ratings, rarities
            ):
                row = server.STATE.conn.execute(
                    "SELECT data FROM items WHERE id=?", (item_id,)
                ).fetchone()
                item = json.loads(row["data"])
                item.update({
                    "preferredPosition": position,
                    "rating": rating,
                    "rareflag": rarity,
                    "rareFlag": rarity,
                })
                server.STATE.conn.execute(
                    "UPDATE items SET data=? WHERE id=?", (json.dumps(item), item_id)
                )
            server.STATE.conn.commit()

        role_expectations = {
            "playerdefender": {"LB", "LWB", "CB", "RB", "RWB"},
            "playermidfielder": {"LM", "CDM", "CM", "CAM", "RM"},
            "playerforward": {"LW", "LF", "CF", "ST", "RF", "RW"},
        }
        for requested_type, accepted_positions in role_expectations.items():
            status, response = self._response(
                "GET",
                f"/ut/game/fifa20/club?year=2020&type={requested_type}&count=50&level=any",
            )
            self.assertEqual(200, status)
            self.assertGreater(response["itemCount"], 0)
            self.assertTrue(all(
                item["preferredPosition"] in accepted_positions
                for item in response["itemData"]
            ))

        status, gold = self._response(
            "GET", "/ut/game/fifa20/club?type=player&count=50&level=gold"
        )
        self.assertEqual(200, status)
        self.assertEqual({item_ids[0], item_ids[1]}, {item["id"] for item in gold["itemData"]})
        self.assertTrue(all(item["rareflag"] <= 1 for item in gold["itemData"]))

        status, special = self._response(
            "GET", "/ut/game/fifa20/club?type=player&count=50&level=any&rare=SP"
        )
        self.assertEqual(200, status)
        self.assertEqual(
            {item_ids[2], item_ids[3], item_ids[4]},
            {item["id"] for item in special["itemData"]},
        )
        self.assertTrue(all(item["rareflag"] > 1 for item in special["itemData"]))

    def test_native_squad_list_create_rename_activate_and_delete(self) -> None:
        item_ids = self._seed_bronze_players()
        status, created = self._response("POST", "/ut/game/fifa20/squad", {
            "squadName": "Second XI",
            "formation": "f442",
            "squadType": "REGULAR_SQUAD",
            "players": [
                {"index": index, "itemData": {"id": item_id, "dream": False}}
                for index, item_id in enumerate(item_ids)
            ],
        })
        self.assertEqual(200, status)
        squad_id = created["id"]
        self.assertGreater(squad_id, 0)
        self.assertEqual("Second XI", created["squadName"])
        self.assertEqual("REGULAR_SQUAD", created["squadType"])
        self.assertEqual(23, len(created["players"]))
        self.assertIn("manager", created)

        status, squad_list = self._response("GET", "/ut/game/fifa20/squad/list")
        self.assertEqual(200, status)
        self.assertEqual({"activeSquadId", "squad"}, set(squad_list))
        self.assertEqual(squad_id, squad_list["activeSquadId"])
        self.assertEqual([squad_id], [entry["id"] for entry in squad_list["squad"]])
        self.assertEqual("Second XI", squad_list["squad"][0]["squadName"])
        self.assertEqual(23, len(squad_list["squad"][0]["players"]))
        self.assertIn("personaId", squad_list["squad"][0])
        self.assertIn("valid", squad_list["squad"][0])

        status, renamed = self._response(
            "PUT", f"/ut/game/fifa20/squad/{squad_id}", {"squadName": "Renamed XI"}
        )
        self.assertEqual(200, status)
        self.assertEqual(squad_id, renamed["id"])
        self.assertEqual("Renamed XI", renamed["squadName"])
        self.assertEqual("f442", renamed["formation"])
        self.assertEqual("REGULAR_SQUAD", renamed["squadType"])
        self.assertEqual(23, len(renamed["players"]))
        self.assertIn("manager", renamed)
        self.assertIn("valid", renamed)
        saved = server.STATE.get_squad(squad_id)
        self.assertEqual("Renamed XI", saved["squadName"])
        self.assertEqual(11, len(saved["players"]))

        status, cloned = self._response(
            "POST", f"/ut/game/fifa20/squad/{squad_id}/clone",
            {"squadName": "Cloned XI", "active": False},
        )
        self.assertEqual(200, status)
        clone_id = cloned["id"]
        self.assertNotEqual(squad_id, clone_id)
        clone = server.STATE.get_squad(clone_id)
        self.assertEqual("Cloned XI", clone["squadName"])
        self.assertEqual(11, len(clone["players"]))

        status, active = self._response(
            "PUT", "/ut/game/fifa20/squad/active", {"squadId": squad_id}
        )
        self.assertEqual(200, status)
        self.assertEqual(squad_id, active["id"])
        self.assertTrue(active["valid"])
        self.assertEqual(23, len(active["players"]))

        status, deleted = self._response(
            "DELETE", f"/ut/delete/game/fifa20/squad/{squad_id}"
        )
        self.assertEqual(200, status)
        self.assertTrue(deleted["success"])
        self.assertEqual(
            [clone_id],
            [entry["id"] for entry in self._response(
                "GET", "/ut/game/fifa20/squad/list"
            )[1]["squad"]],
        )

    def test_name_only_regular_create_returns_a_directly_loadable_empty_squad(self) -> None:
        status, created = self._response("POST", "/ut/game/fifa20/squad", {
            "squadName": "Fresh Squad",
            "formation": "f442",
        })
        self.assertEqual(200, status)
        self.assertGreater(created["id"], 0)
        self.assertEqual("Fresh Squad", created["squadName"])
        self.assertEqual("f442", created["formation"])
        self.assertEqual("REGULAR_SQUAD", created["squadType"])
        self.assertFalse(created["dreamSquad"])
        self.assertEqual(23, len(created["players"]))
        self.assertTrue(all(slot["index"] == index for index, slot in enumerate(created["players"])))
        self.assertTrue(all(
            slot["itemData"] == {"id": 0, "dream": False}
            for slot in created["players"]
        ))
        self.assertEqual(
            [{"index": index, "id": 0, "dream": False} for index in range(5)],
            created["kicktakers"],
        )
        self.assertFalse(created["valid"])
        self.assertTrue(created["manager"])

        status, squad_list = self._response("GET", "/ut/game/fifa20/squad/list")
        self.assertEqual(200, status)
        self.assertEqual(created["id"], squad_list["activeSquadId"])
        self.assertEqual(23, len(squad_list["squad"][0]["players"]))
        self.assertTrue(all(
            isinstance(slot.get("itemData"), dict)
            for slot in squad_list["squad"][0]["players"]
        ))

    def test_repeated_new_squad_saves_return_complete_transition_objects(self) -> None:
        created_squads = []
        for name in ("Second XI", "Third XI"):
            status, created = self._response("POST", "/ut/game/fifa20/squad", {
                # FIFA copies the selected id into each create template.
                "id": 1,
                "squadName": name,
                "formation": "f442",
                "players": [],
            })
            self.assertEqual(200, status)

            status, saved = self._response(
                "PUT", f"/ut/game/fifa20/squad/{created['id']}", created
            )
            self.assertEqual(200, status)
            self.assertEqual(created["id"], saved["id"])
            self.assertEqual(name, saved["squadName"])
            self.assertEqual("REGULAR_SQUAD", saved["squadType"])
            self.assertEqual(23, len(saved["players"]))
            self.assertTrue(all(
                slot["itemData"] == {"id": 0, "dream": False}
                for slot in saved["players"]
            ))
            self.assertEqual(5, len(saved["kicktakers"]))
            self.assertTrue(saved["manager"])
            self.assertIn("personaId", saved)
            self.assertIn("valid", saved)
            created_squads.append(saved)

        self.assertNotEqual(created_squads[0]["id"], created_squads[1]["id"])
        status, squad_list = self._response("GET", "/ut/game/fifa20/squad/list")
        self.assertEqual(200, status)
        self.assertEqual(
            {entry["id"] for entry in created_squads},
            {entry["id"] for entry in squad_list["squad"]},
        )
        self.assertTrue(all(len(entry["players"]) == 23 for entry in squad_list["squad"]))
        self.assertTrue(all(
            all(isinstance(slot.get("itemData"), dict) for slot in entry["players"])
            for entry in squad_list["squad"]
        ))

    def test_removing_a_starter_preserves_all_native_slot_objects(self) -> None:
        item_ids = self._seed_bronze_players()
        status, created = self._response("POST", "/ut/game/fifa20/squad", {
            "squadName": "Removal Test",
            "formation": "f442",
            "players": [
                {"index": index, "itemData": {"id": item_id, "dream": False}}
                for index, item_id in enumerate(item_ids)
            ],
        })
        self.assertEqual(200, status)
        self.assertTrue(created["valid"])

        edited_players = list(created["players"])
        edited_players[0] = {
            "index": 0,
            "kitNumber": 0,
            "itemData": {"id": 0, "dream": False},
        }
        status, saved = self._response(
            "PUT", f"/ut/game/fifa20/squad/{created['id']}",
            {**created, "players": edited_players},
        )

        self.assertEqual(200, status)
        self.assertFalse(saved["valid"])
        self.assertEqual({"id": 0, "dream": False}, saved["players"][0]["itemData"])
        self.assertEqual(item_ids[1], saved["players"][1]["itemData"]["id"])
        self.assertEqual(23, len(saved["players"]))
        self.assertTrue(all(
            isinstance(slot.get("itemData"), dict) for slot in saved["players"]
        ))

    def test_loading_squad_by_id_also_selects_it_for_the_following_hub(self) -> None:
        item_ids = self._seed_bronze_players()
        complete = server.STATE.save_squad({
            "id": 1,
            "squadName": "Playable XI",
            "formation": "f442",
            "active": False,
            "players": [
                {"index": index, "itemData": {"id": item_id}}
                for index, item_id in enumerate(item_ids)
            ],
        })
        empty = server.STATE.save_squad({
            "id": 2,
            "squadName": "New Empty Squad",
            "formation": "f442",
            "active": True,
            "players": [],
        })
        self.assertFalse(complete["active"])
        self.assertTrue(empty["active"])

        status, loaded = self._response("GET", "/ut/game/fifa20/squad/1")

        self.assertEqual(200, status)
        self.assertTrue(loaded["active"])
        self.assertTrue(server.STATE.get_squad(1)["active"])
        self.assertFalse(server.STATE.get_squad(2)["active"])
        status, squad_list = self._response("GET", "/ut/game/fifa20/squad/list")
        self.assertEqual(200, status)
        self.assertEqual(1, squad_list["activeSquadId"])

    def test_hub_uses_complete_formation_when_saved_active_squad_is_empty(self) -> None:
        item_ids = self._seed_bronze_players()
        server.STATE.save_squad({
            "id": 1,
            "squadName": "Playable XI",
            "formation": "f442",
            "active": False,
            "players": [
                {"index": index, "itemData": {"id": item_id}}
                for index, item_id in enumerate(item_ids)
            ],
        })
        server.STATE.save_squad({
            "id": 2,
            "squadName": "New Empty Squad",
            "formation": "f442",
            "active": True,
            "players": [],
        })

        status, hub = self._response("GET", "/ut/game/fifa20/hub")

        self.assertEqual(200, status)
        self.assertEqual(1, hub["squad"]["id"])
        self.assertTrue(hub["squad"]["active"])
        self.assertTrue(hub["squad"]["valid"])
        self.assertTrue(all(
            slot["itemData"]["id"] > 0 for slot in hub["squad"]["players"][:11]
        ))
        # The guard protects CardsDLL without deleting the user's in-progress
        # squad or silently changing their saved selection.
        self.assertTrue(server.STATE.get_squad(2)["active"])

    def test_hub_presentation_fills_active_squad_depleted_by_sbc(self) -> None:
        item_ids = self._seed_bronze_players(15)
        server.STATE.save_squad({
            "id": 1,
            "squadName": "SBC Depleted XI",
            "formation": "f442",
            "active": True,
            "players": [
                {"index": index, "itemData": {"id": item_id}}
                for index, item_id in enumerate(item_ids[:11])
            ],
        })

        server.STATE.complete_sbc(
            challenge_id=103,
            set_id=1,
            item_ids=item_ids[:4],
            challenge_awards=[],
            set_challenge_ids=[103],
            set_awards=[],
            repeatable=False,
        )
        persisted = server.STATE.get_squad(1)
        self.assertEqual(7, len(persisted["players"]))

        status, hub = self._response("GET", "/ut/game/fifa20/hub")

        self.assertEqual(200, status)
        self.assertTrue(hub["squad"]["valid"])
        self.assertTrue(all(
            slot["itemData"]["id"] > 0 for slot in hub["squad"]["players"][:11]
        ))
        self.assertEqual(set(item_ids[4:]), {
            slot["itemData"]["id"] for slot in hub["squad"]["players"][:11]
        })
        # The Hub safety lineup is transient: SBC cards stay consumed and the
        # user's saved squad keeps the four genuinely empty positions.
        self.assertEqual(7, len(server.STATE.get_squad(1)["players"]))
        self.assertTrue(all(server.STATE.get_item(item_id) is None for item_id in item_ids[:4]))

    def test_hub_remains_native_safe_when_club_has_fewer_than_eleven_players(self) -> None:
        server.STATE.save_squad({
            "id": 1,
            "squadName": "Empty Club XI",
            "formation": "f442",
            "active": True,
            "players": [],
        })

        status, hub = self._response("GET", "/ut/game/fifa20/hub")

        self.assertEqual(200, status)
        self.assertTrue(hub["squad"]["valid"])
        self.assertTrue(all(
            slot["itemData"]["id"] > 0 for slot in hub["squad"]["players"][:11]
        ))
        self.assertEqual([], [
            item for item in server.STATE.list_items()
            if str(item.get("itemType", "player")).lower() == "player"
        ])
        self.assertEqual([], server.STATE.get_squad(1)["players"])

    def test_collection_post_ignores_template_id_and_preserves_existing_squad(self) -> None:
        original = server.STATE.save_squad({
            "id": 1,
            "squadName": "Original XI",
            "formation": "f442",
            "active": True,
            "players": [],
        })
        self.assertEqual(1, original["id"])

        status, created = self._response("POST", "/ut/game/fifa20/squad", {
            # FIFA sends the selected squad id in a new-squad template.
            "id": 1,
            "squadName": "Second XI",
            "formation": "f433",
            "squadType": "REGULAR_SQUAD",
            "players": [],
        })

        self.assertEqual(200, status)
        self.assertEqual(2, created["id"])
        self.assertEqual("Original XI", server.STATE.get_squad(1)["squadName"])
        self.assertEqual("Second XI", server.STATE.get_squad(2)["squadName"])
        status, squad_list = self._response("GET", "/ut/game/fifa20/squad/list")
        self.assertEqual(200, status)
        self.assertEqual(2, squad_list["activeSquadId"])
        self.assertEqual({1, 2}, {entry["id"] for entry in squad_list["squad"]})

    def test_native_squad_name_profanity_rpc_returns_passed_text(self) -> None:
        # Exact c9/k20 payload captured when FIFA 20 submitted the name "D3S".
        request = bytes.fromhex(
            "d2ccf4040301929cb40002d74e3401044433530000"
        )
        packet = server.Fire2Packet(
            len(request), 0, 9, 20, 1843, 0, b"", request, b""
        )

        response, label = server._fire2_local_response(packet)

        self.assertEqual("Util.FilterForProfanity", label)
        self.assertEqual(
            bytes.fromhex("d2ccf4040301929cb40000d74e3401044433530000"),
            response,
        )
        self.assertEqual(["D3S"], server._tdf_get_strings(response, "UTXT"))
        self.assertEqual(0, server._tdf_get_int(response, "DIRT"))

    def test_offline_game_report_is_acknowledged_with_final_result(self) -> None:
        request = (
            server._tdf_field_int("FNSH", 1)
            + server._tdf_field_group(
                "RPRT",
                server._tdf_field_int("GRID", 987654321)
                + server._tdf_field_str("GTYP", "gameType84"),
            )
        )
        packet = server.Fire2Packet(
            len(request), 0, 28, 2, 15380, 0, b"", request, b""
        )

        response, label = server._fire2_local_response(packet)
        result = server._blaze_game_report_result_payload(packet.payload)

        self.assertEqual(b"", response)
        self.assertEqual("GameReporting.SubmitOfflineGameReport", label)
        self.assertEqual(0, server._tdf_get_int(result, "EROR"))
        self.assertEqual(1, server._tdf_get_int(result, "FNL"))
        self.assertEqual(987654321, server._tdf_get_int(result, "GHID"))
        self.assertEqual(987654321, server._tdf_get_int(result, "GRID"))

    def test_profanity_rpc_preserves_every_name_in_a_batch(self) -> None:
        request = server._tdf_field_list_groups("TLST", [
            server._tdf_field_int("DIRT", 2)
            + server._tdf_field_str("UTXT", "Regular XI"),
            server._tdf_field_int("DIRT", 2)
            + server._tdf_field_str("UTXT", "Concept XI"),
        ])

        response = server._blaze_filter_for_profanity_payload(request)

        self.assertEqual(
            ["Regular XI", "Concept XI"],
            server._tdf_get_strings(response, "UTXT"),
        )
        self.assertEqual(2, response.count(server._tdf_tag("DIRT", server._TDF_VARINT) + b"\x00"))

    def test_squad_builder_any_ids_do_not_filter_out_the_club(self) -> None:
        item_ids = self._seed_bronze_players()
        status, response = self._response(
            "GET",
            "/ut/game/fifa20/club?year=2020&type=player&count=150&level=any"
            "&nation=-1&league=-1&loan=0&team=-1&sortBy=ovr&sort=desc",
        )
        self.assertEqual(200, status)
        self.assertEqual(len(item_ids), response["itemCount"])
        self.assertEqual(set(item_ids), {item["id"] for item in response["itemData"]})

    def test_concept_search_and_dream_squad_persist_without_owned_duplicates(self) -> None:
        resource_id = max(
            server.PLAYER_DB,
            key=lambda rid: int(server.PLAYER_DB[rid].get("rating", 0) or 0),
        )
        status, concepts = self._response(
            "GET", f"/ut/game/fifa20/defid?definitionId={resource_id}&count=10"
        )
        self.assertEqual(200, status)
        self.assertEqual({"itemData"}, set(concepts))
        self.assertTrue(concepts["itemData"])
        self.assertTrue(all(item["dream"] for item in concepts["itemData"]))
        self.assertTrue(all(item["id"] > 0 for item in concepts["itemData"]))
        self.assertTrue(all(item["concept"] for item in concepts["itemData"]))
        self.assertTrue(all(
            item["conceptResourceId"] == item["resourceId"]
            for item in concepts["itemData"]
        ))

        status, created = self._response("POST", "/ut/game/fifa20/squad", {
            "squadName": "Concept Stars",
            "formation": "f433",
            "squadType": "DREAM_SQUAD",
            "players": [{
                "index": 0,
                "itemData": {"id": resource_id, "resourceId": resource_id, "dream": True},
            }],
        })
        self.assertEqual(200, status)
        squad_id = created["id"]
        saved = server.STATE.get_squad(squad_id)
        self.assertEqual("DREAM_SQUAD", saved["squadType"])
        self.assertEqual(resource_id, saved["players"][0]["conceptResourceId"])
        self.assertEqual([], saved["manager"])

        status, concept_squad = self._response(
            "GET", f"/ut/game/fifa20/squad/{squad_id}"
        )
        self.assertEqual(200, status)
        concept_item = concept_squad["players"][0]["itemData"]
        self.assertEqual(0, concept_item["id"])
        self.assertEqual(resource_id, concept_item["resourceId"])
        self.assertTrue(concept_item["dream"])
        self.assertTrue(concept_squad["dreamSquad"])
        self.assertFalse(any(
            int(item.get("resourceId", 0) or 0) == resource_id
            for item in server.STATE.list_items()
        ))

    def test_empty_native_concept_create_is_classified_before_first_player(self) -> None:
        item_ids = self._seed_bronze_players()
        server.STATE.save_squad({
            "squadName": "Playable XI",
            "squadType": "REGULAR_SQUAD",
            "players": [
                {"index": index, "itemData": {"id": item_id}}
                for index, item_id in enumerate(item_ids)
            ],
        }, create=True)
        regular = server.STATE.save_squad({
            "squadName": "Empty regular template",
            "squadType": "REGULAR_SQUAD",
            "players": [],
        }, create=True)

        status, created = self._response("POST", "/ut/game/fifa20/squad", {
            "id": regular["id"],
            "squadName": "Planning XI",
            "formation": "f442",
            # This is the incorrect value sent by FIFA 20 from the Concept tab.
            "squadType": "REGULAR_SQUAD",
            "manager": [{"id": 0, "dream": False}],
            "players": [],
        })

        self.assertEqual(200, status)
        self.assertEqual("DREAM_SQUAD", created["squadType"])
        self.assertTrue(created["dreamSquad"])
        self.assertEqual([], created["manager"])
        self.assertEqual(
            "DREAM_SQUAD", server.STATE.get_squad(created["id"])["squadType"]
        )

        status, automatic_search = self._response(
            "GET",
            "/ut/game/fifa20/club?year=2020&type=player&count=11"
            "&level=any&sortBy=ovr&sort=desc",
        )
        self.assertEqual(200, status)
        self.assertEqual(11, len(automatic_search["itemData"]))
        self.assertTrue(all(
            item.get("dream") is True and item.get("concept") is True
            for item in automatic_search["itemData"]
        ))

        owned_id = item_ids[0]
        owned_resource = server.STATE.get_item(owned_id)["resourceId"]
        status, exact_owned = self._response(
            "GET",
            f"/ut/game/fifa20/club?type=player&count=1&filter=exact"
            f"&defId={owned_resource}",
        )
        self.assertEqual(200, status)
        self.assertEqual([owned_id], [item["id"] for item in exact_owned["itemData"]])

    def test_concept_search_pages_use_distinct_transient_ids_and_players(self) -> None:
        _, first_page = self._response(
            "GET", "/ut/game/fifa20/defid?position=ATTACK&start=0&count=10"
        )
        _, second_page = self._response(
            "GET", "/ut/game/fifa20/defid?position=ATTACK&start=10&count=10"
        )
        first = first_page["itemData"]
        second = second_page["itemData"]
        self.assertEqual(10, len(first))
        self.assertEqual(10, len(second))
        self.assertEqual(10, len({item["id"] for item in first}))
        self.assertEqual(10, len({item["resourceId"] for item in first}))
        self.assertTrue({item["id"] for item in first}.isdisjoint(
            {item["id"] for item in second}
        ))

    def test_concept_squad_cannot_start_a_match(self) -> None:
        resource_id = next(iter(server.PLAYER_DB))
        saved = server.STATE.save_squad({
            "squadName": "Planning Only",
            "squadType": "DREAM_SQUAD",
            "active": True,
            "players": [{
                "index": 0,
                "itemData": {
                    "id": 800_000_000_000 + resource_id,
                    "resourceId": resource_id,
                    "dream": True,
                },
            }],
        }, create=True)
        self.assertEqual("DREAM_SQUAD", saved["squadType"])
        status, response = self._response("POST", "/ut/game/fifa20/match", {})
        self.assertEqual(409, status)
        self.assertEqual("CONCEPT_SQUAD_NOT_PLAYABLE", response["reason"])

    def test_concept_flags_are_inferred_and_collection_clone_returns_full_squad(self) -> None:
        resource_id = max(
            server.PLAYER_DB,
            key=lambda rid: int(server.PLAYER_DB[rid].get("rating", 0) or 0),
        )
        status, concept = self._response("POST", "/ut/game/fifa20/squad", {
            "squadName": "Inferred Concept",
            "formation": "f442",
            "players": [{
                "index": 0,
                "itemData": {"id": resource_id, "dream": True},
            }],
        })
        self.assertEqual(200, status)
        self.assertEqual("DREAM_SQUAD", concept["squadType"])
        self.assertTrue(concept["dreamSquad"])
        self.assertEqual(23, len(concept["players"]))

        status, clone = self._response("POST", "/ut/game/fifa20/squad/clone", {
            "srcId": concept["id"],
            "dstSquadName": "Concept Copy",
        })
        self.assertEqual(200, status)
        self.assertNotEqual(concept["id"], clone["id"])
        self.assertEqual("Concept Copy", clone["squadName"])
        self.assertEqual("DREAM_SQUAD", clone["squadType"])
        self.assertEqual(23, len(clone["players"]))

    def test_regular_squad_conversion_keeps_owned_cards_as_concept_references(self) -> None:
        owned_id = self._seed_bronze_players()[0]
        owned = server.STATE.get_item(owned_id)
        resource_id = owned["resourceId"]
        regular = server.STATE.save_squad({
            "squadName": "Before Conversion",
            "squadType": "REGULAR_SQUAD",
            "players": [{"index": 0, "itemData": {"id": owned_id}}],
        }, create=True)

        converted = server.STATE.save_squad({
            "id": regular["id"],
            "squadName": "After Conversion",
            "squadType": "DREAM_SQUAD",
            "players": [{"index": 0, "itemData": {"id": owned_id}}],
        })
        self.assertEqual("DREAM_SQUAD", converted["squadType"])
        self.assertEqual(0, converted["players"][0]["itemId"])
        self.assertEqual(resource_id, converted["players"][0]["conceptResourceId"])
        self.assertIsNotNone(server.STATE.get_item(owned_id))

        native = self._response(
            "GET", f"/ut/game/fifa20/squad/{regular['id']}"
        )[1]
        self.assertEqual(0, native["players"][0]["itemData"]["id"])
        self.assertTrue(native["players"][0]["itemData"]["dream"])

    def test_club_gets_manager_test_pool_and_match_squad_contains_full_manager(self) -> None:
        manager = server.STATE._ensure_initial_club_manager()
        self.assertIsNotNone(manager)
        manager_id = manager["id"]
        self.assertEqual("manager", manager["itemType"])
        self.assertEqual(1237389, manager["resourceId"])
        self.assertEqual(237389, manager["assetId"])
        self.assertEqual(manager_id, server.STATE._ensure_initial_club_manager()["id"])

        # Migrate the exact malformed manager identity emitted by 0.8.1.
        managers = [item for item in server.STATE.list_items()
                    if item.get("itemType") == "manager"]
        emery = next(item for item in managers if item["resourceId"] == 1237389)
        server.STATE.update_item_fields(emery["id"], {
            "assetId": 1237389, "leagueId": 0, "teamid": 0,
            "contract": 0, "contracts": 0,
        })
        server.STATE._enrich_existing_manager_items()
        migrated_manager = server.STATE.get_item(emery["id"])
        self.assertEqual(237389, migrated_manager["assetId"])
        self.assertEqual(13, migrated_manager["leagueId"])
        self.assertEqual(1, migrated_manager["teamid"])
        self.assertGreaterEqual(migrated_manager["contract"], 1)
        self.assertGreaterEqual(migrated_manager["contracts"], 1)

        status, manager_search = self._response(
            "GET", "/ut/game/fifa20/club?type=manager"
        )
        self.assertEqual(200, status)
        self.assertEqual(len(server._STARTER_MANAGER_RESOURCE_IDS), manager_search["itemCount"])
        manager_by_resource = {
            entry["resourceId"]: entry for entry in manager_search["itemData"]
        }
        self.assertEqual(emery["id"], manager_by_resource[1237389]["id"])
        self.assertEqual(237389, manager_by_resource[1237389]["assetId"])
        self.assertEqual(237389, manager_by_resource[1237389]["managerId"])
        self.assertGreaterEqual(manager_by_resource[1237389]["contracts"], 1)
        self.assertEqual(237389, manager_by_resource[1237389]["assetId"])

        player_ids = self._seed_bronze_players()
        saved = server.STATE.save_squad({
            "id": 1, "name": "Manager Test XI", "active": True,
            "formation": "f442",
            "players": [{"index": index, "itemData": {"id": item_id}}
                        for index, item_id in enumerate(player_ids)],
        })
        self.assertEqual([{"id": manager_id, "dream": False}], saved["manager"])

        native = server._native_squad()
        self.assertTrue(native["valid"])
        self.assertEqual(1, len(native["manager"]))
        self.assertEqual("manager", native["manager"][0]["itemType"])
        self.assertEqual(manager_id, native["manager"][0]["id"])
        self.assertEqual(1237389, native["manager"][0]["resourceId"])
        self.assertEqual(237389, native["manager"][0]["assetId"])
        self.assertEqual(237389, native["manager"][0]["managerId"])
        self.assertGreaterEqual(native["manager"][0]["contracts"], 1)

        status, match = self._response("POST", "/ut/game/fifa20/match", {
            "sqbtEventId": 1, "sqbtOpponentSquadId": 0,
            "sqbtMatchDifficulty": 3,
        })
        self.assertEqual(200, status)
        self.assertEqual(manager_id, match["homeSquad"]["manager"][0]["id"])

    def test_squad_battle_match_bypasses_stale_draft_for_exact_native_request(self) -> None:
        server.STATE._ensure_initial_club_manager()
        server.STATE._seed_squad()
        stale_draft = server._draft_new_run("SINGLE_PLAYER")
        self.assertEqual("PICK_DIFFICULTY", stale_draft["state"])
        server.STATE.save_draft_run(stale_draft)

        status, match = self._response("POST", "/ut/game/fifa20/match", {
            "squadId": 1,
            "type": "OFFLINE",
            "sqbtEventId": 2957,
            "sqbtOpponentSquadId": 0,
            "sqbtMatchDifficulty": 6,
            "customData1": 1092304324,
        })
        self.assertEqual(200, status)
        self.assertNotIn("reason", match)
        self.assertEqual(2957, match["sqbtEventId"])
        self.assertEqual(0, match["sqbtOpponentSquadId"])
        self.assertEqual(6, match["matchDifficulty"])
        self.assertEqual(11, sum(
            isinstance(slot.get("itemData"), dict)
            for slot in match["opponentSquad"]["players"][:11]
        ))

        # FIFA may resolve the opponent through this endpoint after /match.
        # It must receive the active Squad Battles opponent, not Draft round 1.
        status, opponent = self._response(
            "GET", "/ut/game/fifa20/offlinesquad/0"
        )
        self.assertEqual(200, status)
        self.assertNotEqual("DRAFT_SQUAD", opponent["squadType"])
        self.assertEqual(11, sum(
            isinstance(slot.get("itemData"), dict)
            for slot in opponent["players"][:11]
        ))

    def test_squad_battle_native_put_handshake_is_not_captured_by_stale_draft(self) -> None:
        server.STATE._ensure_initial_club_manager()
        server.STATE._seed_squad()
        stale_draft = server._draft_new_run("SINGLE_PLAYER")
        stale_draft.update({"state": "READY_FOR_REWARDS", "currentMatchGid": 111})
        server.STATE.save_draft_run(stale_draft)

        status, started = self._response("POST", "/ut/game/fifa20/match", {
            "squadId": 2,
            "type": "OFFLINE",
            "sqbtEventId": 2958,
            "sqbtOpponentSquadId": 0,
            "sqbtMatchDifficulty": 6,
            "customData1": 1092304324,
        })
        self.assertEqual(200, status)
        self.assertEqual(2958, started["sqbtEventId"])

        gid = 217106407699
        status, handshake = self._response("PUT", "/ut/game/fifa20/match", {
            "gid": gid,
            "items": [{"id": item_id} for item_id in range(1, 13)],
        })
        self.assertEqual(200, status)
        self.assertEqual(gid, handshake["gid"])
        self.assertIn("opponentSquad", handshake)
        self.assertTrue(server._sb_state()["matchInProgress"])
        self.assertEqual(gid, server._sb_state()["currentMatchGid"])
        self.assertEqual("READY_FOR_REWARDS", server.STATE.draft_run()["state"])

        status, settled = self._response("PUT", "/ut/game/fifa20/match/end", {
            "gid": gid, "endReason": "WIN", "goalsFor": 2, "goalsAgainst": 0,
        })
        self.assertEqual(200, status)
        self.assertEqual("WIN", settled["endReason"])
        self.assertIn("squadBattlePoints", settled)
        self.assertEqual(0, server.STATE.draft_run()["wins"])

    def test_completed_squad_battle_loss_earns_coins_but_early_quit_does_not(self) -> None:
        server.STATE._ensure_initial_club_manager()
        server.STATE._seed_squad()
        before = server.STATE.credits()
        for outcome, should_pay in (("LOSS", True), ("QUIT", False)):
            status, started = self._response("POST", "/ut/game/fifa20/match", {
                "squadId": 1, "type": "OFFLINE", "sqbtEventId": 2957,
                "sqbtOpponentSquadId": 0, "sqbtMatchDifficulty": 3,
            })
            self.assertEqual(200, status)
            self.assertIn("sqbtEventId", started)
            status, settled = self._response("PUT", "/ut/game/fifa20/match/end", {
                "gid": 8000 if should_pay else 8001,
                "endReason": outcome, "secondsPlayed": 720 if should_pay else 38,
                "goalsFor": 0, "goalsAgainst": 2,
            })
            self.assertEqual(200, status)
            if should_pay:
                self.assertGreater(settled["completionAward"], 0)
                self.assertEqual(before + settled["totalCoins"], server.STATE.credits())
                before = server.STATE.credits()
            else:
                self.assertEqual(0, settled["totalCoins"])
                self.assertEqual(0, settled["squadBattlePoints"])
                self.assertEqual(before, server.STATE.credits())

    def test_draft_wins_override_cached_squad_battle_result_and_reach_champion_reward(self) -> None:
        # Reproduce the supplied runtime: a completed Squad Battles result is
        # cached before the user starts a valid single-player Draft match.
        sb_state = server._sb_state()
        sb_state["matchInProgress"] = False
        sb_state["lastMatchResponse"] = {"endReason": "LOSS", "squadBattlePoints": 0}
        server._sb_save(sb_state)

        run = server._draft_new_run("SINGLE_PLAYER")
        run["state"] = "READY_FOR_MATCH"
        server.STATE.save_draft_run(run)

        for expected_wins in range(1, 5):
            gid = 202094993970 + expected_wins
            status, match = self._response("POST", "/ut/game/fifa20/match", {
                "squadId": 900001, "type": "OFFLINE", "customData1": 1092304324,
            })
            self.assertEqual(200, status)
            self.assertIn("opponentSquad", match)
            status, _ = self._response(
                "PUT", "/ut/game/fifa20/match", {"gid": gid}
            )
            self.assertEqual(200, status)

            status, result = self._response("PUT", "/ut/game/fifa20/match/end", {
                "gid": gid,
                "matchReportId": 0,
                "endReason": "WIN",
                "secondsPlayed": 5528,
                "matchDifficulty": 6,
            })
            self.assertEqual(200, status)
            self.assertEqual("WIN", result["endReason"])
            self.assertEqual(expected_wins, result["draftWins"])
            self.assertEqual(expected_wins, result["gamesWonCurrentMatch"])
            self.assertEqual(gid, result["gid"])

            saved = server.STATE.draft_run()
            self.assertEqual(expected_wins, saved["wins"])
            expected_state = "READY_FOR_REWARDS" if expected_wins == 4 else "READY_FOR_MATCH"
            self.assertEqual(expected_state, saved["state"])

            if expected_wins == 1:
                _, duplicate = self._response("PUT", "/ut/game/fifa20/match/end", {
                    "gid": gid, "matchReportId": 0, "endReason": "WIN",
                })
                self.assertEqual(1, duplicate["draftWins"])
                self.assertEqual(1, server.STATE.draft_run()["wins"])

        credits_before = server.STATE.credits()
        packs_before = server.STATE.unopened_pack_counts()
        completed_run = server.STATE.draft_run()
        expected_coins = int(completed_run["pendingCoinReward"])
        expected_packs = {
            pack_id: completed_run["pendingPackRewards"].count(pack_id)
            for pack_id in set(completed_run["pendingPackRewards"])
        }
        status, awards = self._response(
            "POST", "/ut/game/fifa20/draft/grant/award", {}
        )
        self.assertEqual(200, status)
        if expected_coins:
            self.assertIn({
                "halId": 0, "type": 2, "value": expected_coins,
            }, awards)
        self.assertEqual(credits_before + expected_coins, server.STATE.credits())
        packs_after = server.STATE.unopened_pack_counts()
        for pack_id, quantity in expected_packs.items():
            self.assertEqual(
                packs_before.get(pack_id, 0) + quantity,
                packs_after.get(pack_id, 0),
            )
        self.assertIsNone(server.STATE.draft_run())

    def test_legacy_zero_coin_draft_reward_is_repaired_before_claim(self) -> None:
        run = server._draft_new_run("SINGLE_PLAYER")
        run.update({
            "state": "READY_FOR_REWARDS",
            "wins": 0,
            "pendingCoinReward": 0,
            "pendingPackRewards": [1024],
        })
        server.STATE.save_draft_run(run)

        before = server.STATE.credits()
        status, awards = self._response(
            "POST", "/ut/game/fifa20/draft/grant/award", {}
        )
        self.assertEqual(200, status)
        self.assertEqual({
            "halId": 0, "type": 2, "value": 550,
        }, awards[0])
        self.assertEqual({
            "halId": 1024, "type": 0, "value": 1,
        }, awards[1])
        self.assertEqual(before + 550, server.STATE.credits())

    def test_quit_after_draft_kickoff_grants_real_finish_coins_and_pack(self) -> None:
        run = server._draft_new_run("SINGLE_PLAYER")
        run.update({"state": "READY_FOR_MATCH", "difficulty": 1, "seed": 312})
        server.STATE.save_draft_run(run)
        status, match = self._response("POST", "/ut/game/fifa20/match", {
            "squadId": 900001, "type": "OFFLINE", "customData1": 1092304324,
        })
        self.assertEqual(200, status)
        self.assertEqual(2, match["matchDifficulty"])
        self.assertEqual("SEMIPRO", match["difficultyName"])
        gid = 18379094341
        status, _ = self._response("PUT", "/ut/game/fifa20/match", {"gid": gid})
        self.assertEqual(200, status)
        status, result = self._response("PUT", "/ut/game/fifa20/match/end", {
            "gid": gid, "endReason": "QUIT", "secondsPlayed": 52,
            "items": [{"id": 100849449, "fitness": 99}],
        })
        self.assertEqual(200, status)
        self.assertEqual("LOSS", result["endReason"])
        self.assertEqual(750, result["completionAward"])
        self.assertEqual(750, server.STATE.draft_run()["pendingCoinReward"])
        before = server.STATE.credits()
        status, awards = self._response("POST", "/ut/game/fifa20/draft/grant/award", {})
        self.assertEqual(200, status)
        self.assertEqual({
            "halId": 0, "type": 2, "value": 750,
        }, awards[0])
        self.assertEqual(before + 750, server.STATE.credits())
        self.assertIn(1024, server.STATE.unopened_pack_counts())

    def test_draft_autocomplete_returns_native_object_with_complete_squad(self) -> None:
        run = server._draft_new_run("SINGLE_PLAYER")
        server.STATE.save_draft_run(run)

        status, headers, body = server.route_fut(
            "PUT", "/ut/game/fifa20/squad/mode/1/draft/autocomplete",
            {"Content-Type": "application/json"}, b"{}",
        )
        response = json.loads(body.decode("utf-8"))
        self.assertEqual(200, status)
        self.assertIsInstance(response, dict)
        self.assertTrue(response["success"])
        self.assertEqual("SINGLE_PLAYER", response["mode"])
        self.assertNotIn("squadState", response)
        self.assertEqual("READY_FOR_MATCH", response["draftState"]["squadState"])
        self.assertEqual("23", response["draftState"]["stateParam2"])
        self.assertEqual(23, len(response["squad"]["players"]))
        self.assertEqual(response["squad"]["players"], response["players"])
        self.assertEqual(1, len(response["squad"]["manager"]))
        self.assertTrue(all("itemData" in slot for slot in response["squad"]["players"]))

        saved = server.STATE.draft_run()
        self.assertEqual("READY_FOR_MATCH", saved["state"])
        self.assertEqual(23, len(saved["picked"]))
        self.assertIsInstance(saved["manager"], dict)

        status, _, state_body = server.route_fut(
            "GET", "/ut/game/fifa20/squad/mode/draft/state?mode=SINGLE_PLAYER",
            {}, b"",
        )
        full_state = json.loads(state_body.decode("utf-8"))
        self.assertEqual(200, status)
        self.assertEqual(23, len(full_state[0]["squad"]["players"]))
        self.assertEqual(1, len(full_state[0]["squad"]["manager"]))

    def test_draft_formation_choices_exclude_missing_native_localization(self) -> None:
        self.assertNotIn("f541", server._DRAFT_FORMATIONS)
        stale_run = server._draft_new_run("SINGLE_PLAYER")
        stale_run["formationChoices"] = ["f352", "f541", "f343", "f4141", "f433"]
        repaired = server._draft_choices(stale_run, "formation")
        self.assertNotIn("f541", [choice["formation"] for choice in repaired["choices"]])
        for seed in range(100):
            run = server._draft_new_run("SINGLE_PLAYER")
            run["seed"] = seed + 1
            response = server._draft_choices(run, "formation")
            formations = [choice["formation"] for choice in response["choices"]]
            self.assertEqual(5, len(formations))
            self.assertEqual(5, len(set(formations)))
            self.assertNotIn("f541", formations)

    def test_single_player_draft_difficulty_reaches_both_match_handshakes(self) -> None:
        run = server._draft_new_run("SINGLE_PLAYER")
        server.STATE.save_draft_run(run)

        # Before the picker is confirmed FIFA must not receive a fabricated
        # round.  This keeps the native Draft state machine on its difficulty
        # selection step.
        initial_state = server._draft_state_payload(run)[0]
        self.assertEqual("PICK_DIFFICULTY", initial_state["squadState"])
        self.assertEqual([], initial_state["roundsInfo"])

        status, difficulty_choices = self._response(
            "GET", "/ut/game/fifa20/squad/mode/1/draft/choices/difficulty",
        )
        self.assertEqual(200, status)
        self.assertEqual(
            [1, 2, 3, 4, 5, 6],
            [choice["difficulty"] for choice in difficulty_choices["choices"]],
        )

        status, response = self._response(
            "PUT", "/ut/game/fifa20/squad/mode/draft/choose/difficulty",
            {"difficultyName": "WORLDCLASS"},
        )
        self.assertEqual(200, status)
        self.assertEqual({}, response)
        self.assertEqual(3, server.STATE.draft_run()["difficulty"])
        self.assertEqual(4, server._draft_game_difficulty(server.STATE.draft_run()))
        state = server._draft_state_payload(server.STATE.draft_run())[0]
        self.assertNotIn("difficulty", state)
        self.assertNotIn("draftDifficulty", state)
        self.assertNotIn("matchDifficulty", state)
        self.assertNotIn("sqbtMatchDifficulty", state)
        self.assertEqual([{
            "difficulty": "WORLDCLASS",
            "opponentId": 243,
            "opponentPenaltyScore": 0,
            "opponentRating": 80,
            "penaltyScore": 0,
            "round": 1,
            "score": 0,
        }], state["roundsInfo"])

        # The native row follows the active Draft round, not a hard-coded
        # first opponent.  Round three uses the third preset/opponent while
        # retaining the one selected difficulty for the whole run.
        progressed = server.STATE.draft_run()
        progressed["wins"] = 2
        progressed_state = server._draft_state_payload(progressed)[0]
        self.assertEqual([{
            "difficulty": "WORLDCLASS",
            "opponentId": 21,
            "opponentPenaltyScore": 0,
            "opponentRating": 86,
            "penaltyScore": 0,
            "round": 3,
            "score": 0,
        }], progressed_state["roundsInfo"])

        saved = server.STATE.draft_run()
        saved["state"] = "READY_FOR_MATCH"
        server.STATE.save_draft_run(saved)
        for method, payload in (
            ("POST", {"squadId": 900001, "type": "OFFLINE"}),
            ("PUT", {"gid": 202094993999}),
        ):
            status, match = self._response(method, "/ut/game/fifa20/match", payload)
            self.assertEqual(200, status)
            self.assertEqual(4, match["matchDifficulty"])
            self.assertEqual(4, match["difficulty"])
            self.assertEqual(4, match["difficultyId"])
            self.assertEqual(4, match["gameDifficulty"])
            self.assertEqual(4, match["sqbtMatchDifficulty"])
            self.assertEqual(3, match["draftDifficulty"])
            self.assertEqual("WORLDCLASS", match["difficultyName"])
            for key in ("squad", "opponentSquad", "awaySquad", "opponent"):
                self.assertEqual(4, match[key]["difficulty"])
                self.assertEqual(4, match[key]["sqbtMatchDifficulty"])
                self.assertEqual("WORLDCLASS", match[key]["difficultyName"])

        status, offline = self._response("GET", "/ut/game/fifa20/offlinesquad/1")
        self.assertEqual(200, status)
        self.assertEqual(4, offline["matchDifficulty"])
        self.assertEqual(4, offline["sqbtMatchDifficulty"])
        self.assertEqual("WORLDCLASS", offline["difficultyName"])

    def test_all_draft_finishes_award_real_coins_scaled_by_chosen_difficulty(self) -> None:
        for mode in ("SINGLE_PLAYER", "ONLINE"):
            for wins in range(5):
                run = server._draft_new_run(mode)
                run["seed"] = 19420
                amounts = []
                for choice in range(6):
                    run["difficulty"] = choice
                    self.assertEqual(choice + 1, server._draft_game_difficulty(run))
                    state = server._draft_state_payload(run)[0]
                    self.assertNotIn("difficultyName", state["roundsInfo"][0])
                    self.assertEqual(
                        server._DRAFT_ROUND_DIFFICULTIES[choice],
                        state["roundsInfo"][0]["difficulty"],
                    )
                    coins, packs = server._draft_reward_for_run(run, wins)
                    self.assertGreaterEqual(coins, 500)
                    self.assertTrue(packs)
                    amounts.append(coins)
                if mode == "SINGLE_PLAYER":
                    self.assertEqual(sorted(amounts), amounts)
                    self.assertGreater(amounts[-1], amounts[0])

    def test_draft_controller_backout_is_not_a_loss_and_advance_retries(self) -> None:
        run = server._draft_new_run("SINGLE_PLAYER")
        run["state"] = "READY_FOR_MATCH"
        server.STATE.save_draft_run(run)

        status, first_match = self._response("POST", "/ut/game/fifa20/match", {
            "squadId": 900001, "type": "OFFLINE", "customData1": 1092304324,
        })
        self.assertEqual(200, status)
        self.assertEqual(243, first_match["opponentTeamId"])

        status, cancelled = self._response("PUT", "/ut/game/fifa20/match/end", {
            "gid": 0, "matchReportId": 0, "endReason": "DNF", "items": [],
            "isHomeTeam": True, "isFormationChanged": True,
        })
        self.assertEqual(200, status)
        self.assertEqual(0, cancelled["loss"])
        self.assertFalse(cancelled["draftEliminated"])
        saved = server.STATE.draft_run()
        self.assertEqual("READY_FOR_MATCH", saved["state"])
        self.assertEqual(0, saved["wins"])
        self.assertEqual(0, saved["losses"])
        self.assertEqual([], saved["pendingPackRewards"])

        status, retried = self._response("POST", "/ut/game/fifa20/match", {
            "squadId": 900001, "type": "OFFLINE", "customData1": 1092304324,
        })
        self.assertEqual(200, status)
        self.assertIn("opponentSquad", retried)
        self.assertEqual(243, retried["opponentTeamId"])

    def test_draft_rounds_use_four_distinct_club_identities(self) -> None:
        seen = []
        for wins in range(4):
            run = server._draft_new_run("SINGLE_PLAYER")
            run.update({"state": "READY_FOR_MATCH", "wins": wins})
            response = server._native_draft_match_response(run)
            seen.append(response["opponentTeamId"])
            self.assertEqual(response["opponentTeamId"], response["opponentSquad"]["teamId"])
            self.assertNotEqual("Arsenal", response["opponentSquad"]["clubName"])
        self.assertEqual([243, 9, 21, 241], seen)
        self.assertEqual(4, len(set(seen)))

    def test_draft_opponents_use_their_own_roster_badge_and_kits(self) -> None:
        expected_states = {"activeHomeKit", "activeAwayKit", "activeBadge"}
        for round_index, (team_id, club_name, _abbr) in enumerate(
            server._DRAFT_OPPONENT_CLUBS
        ):
            squad = server._draft_opponent_squad(round_index)
            self.assertEqual(team_id, squad["teamId"])
            self.assertEqual(club_name, squad["clubName"])
            self.assertEqual(23, len(squad["players"]))
            self.assertTrue(all(
                slot["itemData"]["teamid"] == team_id
                for slot in squad["players"]
            ))
            self.assertEqual(23, len({
                slot["itemData"]["assetId"] for slot in squad["players"]
            }))

            club_cosmetics = {
                item["itemState"]: item for item in squad["actives"]
                if item["itemState"] in expected_states
            }
            self.assertEqual(expected_states, set(club_cosmetics))
            self.assertTrue(all(
                item["teamid"] == team_id for item in club_cosmetics.values()
            ))
            self.assertEqual(
                club_cosmetics,
                {
                    item["itemState"]: item for item in squad["club"]
                    if item["itemState"] in expected_states
                },
            )

    def test_draft_reward_pools_use_fifa20_scale_and_valid_reward_packs(self) -> None:
        for _mode, finishes in server._DRAFT_REWARD_POOLS.items():
            self.assertEqual({0, 1, 2, 3, 4}, set(finishes))
            for _wins, choices in finishes.items():
                self.assertTrue(choices)
                for coins, packs in choices:
                    self.assertLessEqual(coins, 45_000)
                    self.assertTrue(packs)
                    self.assertTrue(all(
                        server.get_pack_cfg(pack_id).get("reward_only") is True
                        for pack_id in packs
                    ))

    def test_rivals_hub_uses_native_schema_and_active_week(self) -> None:
        status, settings = self._response("GET", "/ut/game/fifa20/settings")
        self.assertEqual(200, status)
        self.assertIn(
            {"type": "enableRivals", "value": "1"}, settings["configs"]
        )
        self.assertIn(
            {"type": "enableDivisionRivals", "value": "1"}, settings["configs"]
        )
        self.assertIn(
            {"type": "enableFUTChampions", "value": "1"}, settings["configs"]
        )

        status, seasonal = self._response(
            "GET", "/ut/game/fifa20/seasonal/seasonalconfig.json"
        )
        self.assertEqual(200, status)
        self.assertTrue(seasonal["divisionRivals"]["enabled"])
        self.assertTrue(seasonal["futChampions"]["enabled"])

        status, hub = self._response("GET", "/ut/game/fifa20/rivals")
        self.assertEqual(200, status)
        self.assertEqual(10, hub["crtRivalDivision"])
        self.assertEqual(1000, hub["skillPoints"])
        self.assertLess(hub["startTime"], hub["currentTime"] + 1)
        self.assertGreater(hub["endTime"], hub["currentTime"])
        self.assertEqual(2000, hub["weekendLeagueThreshold"])
        self.assertEqual([], hub["divisionsList"])
        self.assertEqual([], hub["prizeSet"])
        self.assertEqual("rivals", server.STATE.get("seasonal_mode"))

    def test_champions_native_hub_stats_and_leaderboard(self) -> None:
        status, hub = self._response("GET", "/ut/game/fifa20/champion")
        self.assertEqual(200, status)
        self.assertTrue(hub["active"])
        self.assertEqual(30, hub["gamesRemaining"])
        self.assertEqual(1, len(hub["activeChampionLeagues"]))
        self.assertEqual(10, len(hub["prizeTiers"]))
        self.assertEqual("champion", server.STATE.get("seasonal_mode"))

        status, stats = self._response(
            "GET", "/ut/game/fifa20/champion/user/stats?save=false&period=curr&type=base"
        )
        self.assertEqual(200, status)
        self.assertEqual(0, stats["gamesPlayed"])
        self.assertEqual(30, stats["gamesRemaining"])

        status, leaderboard = self._response(
            "GET", "/ut/game/fifa20/champion/leaderboard?championEventId=1"
        )
        self.assertEqual(200, status)
        self.assertEqual(1, leaderboard["totalCount"])
        self.assertEqual(1, len(leaderboard["entries"]))

    def test_rivals_and_champions_matches_update_progress_and_rewards(self) -> None:
        self._response("GET", "/ut/game/fifa20/rivals/user/hub")
        status, match = self._response("POST", "/ut/game/fifa20/match", {})
        self.assertEqual(200, status)
        self.assertEqual(23, len(match["opponentSquad"]["players"]))
        status, result = self._response(
            "POST", "/ut/game/fifa20/match/end", {"endReason": "WIN"}
        )
        self.assertEqual(200, status)
        self.assertEqual("rivals", result["mode"])
        self.assertEqual(1, result["weeklyWins"])
        self.assertEqual(200, result["weekendLeaguePoints"])
        credits_before = server.STATE.credits()
        _, claimed = self._response("POST", "/ut/game/fifa20/rivals/user/rewards")
        self.assertFalse(claimed["alreadyClaimed"])
        self.assertGreater(server.STATE.credits(), credits_before)
        _, duplicate = self._response("POST", "/ut/game/fifa20/rivals/user/rewards")
        self.assertTrue(duplicate["alreadyClaimed"])

        self._response("GET", "/ut/game/fifa20/champion/user/hub")
        _, champions_match = self._response("POST", "/ut/game/fifa20/match", {})
        self.assertEqual(23, len(champions_match["opponentSquad"]["players"]))
        _, champions_result = self._response(
            "POST", "/ut/game/fifa20/match/end", {"endReason": "LOSS"}
        )
        self.assertEqual("champion", champions_result["mode"])
        self.assertEqual(1, champions_result["gamesPlayed"])

    def test_hidden_reward_packs_survive_custom_store_settings(self) -> None:
        merged = server._merge_pack_settings({"packs": {
            "99": {"name": "Custom Test Pack", "size": 1, "price": 0}
        }})
        self.assertIn("99", merged["packs"])
        self.assertTrue(merged["packs"]["8"]["built_in_store"])
        self.assertTrue(merged["packs"]["18"]["built_in_store"])
        self.assertEqual(100, merged["next_pack_id"])
        self.assertTrue(merged["packs"]["1012"]["reward_only"])
        self.assertTrue(merged["packs"]["1023"]["reward_only"])

    def test_earned_reward_is_exposed_as_my_pack_not_zero_coin_store_offer(self) -> None:
        server.STATE.grant_unopened_pack(1012)
        status, response = self._response(
            "GET", "/ut/game/fifa20/store/purchasegroup/mypacks"
        )
        self.assertEqual(200, status)
        earned = next(pack for pack in response["purchase"] if pack["id"] == 1012)
        self.assertTrue(earned["isMyPack"])
        self.assertTrue(earned["isReward"])
        self.assertEqual("mypacks", earned["displayGroup"]["value"])
        self.assertEqual("mypacks", earned["purchaseGroup"])
        self.assertEqual(1, earned["unopenedQuantity"])
        self.assertTrue(earned["unopened"])
        self.assertEqual(1012, earned["packId"])
        self.assertEqual(2, earned["packAssetId"])
        self.assertEqual(2, earned["assetId"])
        self.assertEqual(0, earned["coins"])
        self.assertEqual(0, earned["points"])
        self.assertFalse(earned["isCoinPurchasable"])
        self.assertFalse(earned["isPurchaseable"])
        normal = next(pack for pack in response["purchase"] if pack["id"] == 3)
        self.assertFalse(normal["isMyPack"])
        self.assertFalse(normal["unopened"])
        self.assertEqual(3, normal["assetId"])
        self.assertEqual(3, normal["packAssetId"])
        self.assertEqual(normal["finalPrice"], normal["coins"])
        self.assertTrue(normal["isCoinPurchasable"])

    def test_legacy_sbc_store_pack_rows_migrate_to_reward_only_ids(self) -> None:
        with server.STATE.lock:
            server.STATE.conn.executemany(
                "INSERT INTO unopened_packs(pack_id,data) VALUES(?,?)",
                [(1, '{"source":"SBC challenge 102"}'),
                 (3, '{"source":"SBC challenge 101"}')],
            )
            server.STATE.conn.commit()

        self.assertEqual({1001: 1, 1005: 1}, server.STATE.unopened_pack_counts())
        _, store = self._response("GET", "/ut/game/fifa20/store/purchasegroup/mypacks")
        rewards = {row["id"]: row for row in store["purchase"] if row["isMyPack"]}
        self.assertEqual({1001, 1005}, set(rewards))
        self.assertTrue(all(row["finalPrice"] == 0 for row in rewards.values()))

    def test_draft_choose_slot_order_uses_latest_client_layout(self) -> None:
        first = {"id": 910000000001, "resourceId": 111, "rating": 90}
        second = {"id": 910000000002, "resourceId": 222, "rating": 91}
        incoming = {"id": 910000000003, "resourceId": 333, "rating": 92}
        run = server._draft_new_run("SINGLE_PLAYER")
        run.update({
            "state": "PLAYER_DRAFT",
            "picked": {"0": first, "1": second},
            "currentChoices": [incoming],
            "currentChoiceKind": "player",
            "currentPositionId": 2,
        })
        server.STATE.save_draft_run(run)

        status, _ = self._response(
            "PUT", "/ut/game/fifa20/squad/mode/1/draft/choose", {
                "choiceIndex": 0,
                "positionId": 2,
                "swapPlayerDefIds": [222, 111, 0] + [0] * 20,
            },
        )
        self.assertEqual(200, status)
        saved = server.STATE.draft_run()
        self.assertEqual(222, saved["picked"]["0"]["resourceId"])
        self.assertEqual(111, saved["picked"]["1"]["resourceId"])
        self.assertEqual(333, saved["picked"]["2"]["resourceId"])
        squad = server._draft_squad(saved)
        self.assertEqual(
            [222, 111, 333],
            [slot["itemData"]["resourceId"] for slot in squad["players"][:3]],
        )

    def test_squad_battles_week_opponents_match_points_and_top100(self) -> None:
        item_ids = self._seed_bronze_players()
        server.STATE.save_squad({
            "id": 1,
            "name": "Test XI",
            "active": True,
            "formation": "f442",
            "players": [
                {"index": index, "itemData": {"id": item_id}}
                for index, item_id in enumerate(item_ids)
            ],
        })

        status, hub = self._response("GET", "/ut/game/fifa20/sqbt/user/hub")
        self.assertEqual(200, status)
        self.assertGreater(hub["endTime"], hub["currentTime"])
        self.assertEqual(40, hub["maxRegularGames"])
        self.assertIsInstance(hub["sqbtOppSquadList"], dict)
        initial_opponents = hub["sqbtOppSquadList"]
        self.assertEqual(
            {"nextSquadRefreshTimeStamp", "sqbtOppSquads"}, set(initial_opponents)
        )
        self.assertEqual(4, len(initial_opponents["sqbtOppSquads"]))
        self.assertEqual(
            ["1", "10", "22", "243"],
            [entry["badgeAssetId"] for entry in initial_opponents["sqbtOppSquads"]],
        )
        self.assertEqual(
            [1, 10, 22, 243],
            [squad["teamId"] for squad in hub["squads"]],
        )
        self.assertEqual(4, len(hub["squads"]))
        self.assertTrue(all(len(squad["players"]) == 23 for squad in hub["squads"]))
        self.assertEqual([0, 1, 2, 3], [
            entry["id"] for entry in initial_opponents["sqbtOppSquads"]
        ])
        self.assertTrue(all("squad" not in entry
                            for entry in initial_opponents["sqbtOppSquads"]))
        initial_names = [entry["clubName"] for entry in initial_opponents["sqbtOppSquads"]]
        initial_rosters = [
            [slot["itemData"]["resourceId"] for slot in squad["players"][:11]]
            for squad in hub["squads"]
        ]

        _, refreshed = self._response(
            "POST", "/ut/game/fifa20/sqbt/v2/user/opponents/refresh"
        )
        self.assertEqual(4, len(refreshed["sqbtOppSquadList"]["sqbtOppSquads"]))
        opponents = refreshed["sqbtOppSquadList"]["sqbtOppSquads"]
        self.assertNotEqual(initial_names, [entry["clubName"] for entry in opponents])
        self.assertNotEqual(
            [entry["badgeAssetId"] for entry in initial_opponents["sqbtOppSquads"]],
            [entry["badgeAssetId"] for entry in opponents],
        )
        _, refreshed_hub_before_match = self._response(
            "GET", "/ut/game/fifa20/sqbt/user/hub"
        )
        refreshed_rosters = [
            [slot["itemData"]["resourceId"] for slot in squad["players"][:11]]
            for squad in refreshed_hub_before_match["squads"]
        ]
        self.assertNotEqual(initial_rosters, refreshed_rosters)
        self.assertNotEqual(
            [1, 10, 22, 243],
            [squad["teamId"] for squad in refreshed_hub_before_match["squads"]],
        )
        self.assertGreater(sum(
            int(slot["itemData"].get("rareflag", 0)) > 1
            for squad in refreshed_hub_before_match["squads"]
            for slot in squad["players"][:11]
        ), 0)

        _, history = self._response(
            "GET", "/ut/game/fifa20/sqbt/history?featureConsumerId=sqbt"
        )
        self.assertEqual(server._SB_FEATURED_CUSTOM_ID,
                         history["entries"][0]["featuredSquadId"])
        self.assertFalse(history["entries"][0]["played"])

        opponent_id = opponents[0]["id"]
        status, cpu_squad = self._response(
            "GET", f"/ut/game/fifa20/sqbt/user/opponentsquad/{opponent_id}"
        )
        self.assertEqual(200, status)
        self.assertEqual(23, len(cpu_squad["players"]))
        self.assertEqual(11, sum(
            isinstance(slot.get("itemData"), dict)
            for slot in cpu_squad["players"][:11]
        ))
        status, offline_squad = self._response(
            "GET", f"/ut/game/fifa20/offlinesquad/{opponent_id}"
        )
        self.assertEqual(200, status)
        self.assertEqual(opponent_id, offline_squad["squadId"])
        self.assertEqual(23, len(offline_squad["players"]))

        status, match = self._response("POST", "/ut/game/fifa20/match", {
            "sqbtEventId": hub["sqbtEventId"],
            "sqbtOpponentSquadId": opponent_id,
            "sqbtMatchDifficulty": 4,
        })
        self.assertEqual(200, status)
        self.assertEqual(opponent_id, match["sqbtOpponentSquadId"])
        self.assertEqual(4, match["matchDifficulty"])
        self.assertEqual(11, sum(
            isinstance(slot.get("itemData"), dict)
            for slot in match["opponentSquad"]["players"][:11]
        ))

        credits_before = server.STATE.credits()
        status, settled = self._response("POST", "/ut/game/fifa20/match/end", {
            "endReason": "WIN", "goalsFor": 3, "goalsAgainst": 1,
            "secondsPlayed": 720,
        })
        self.assertEqual(200, status)
        self.assertEqual("WIN", settled["endReason"])
        self.assertGreater(settled["squadBattlePoints"], 0)
        self.assertGreater(server.STATE.credits(), credits_before)

        # A client retry must not award the same match twice.
        points_after = settled["battlePoints"]
        credits_after = server.STATE.credits()
        _, retried = self._response("POST", "/ut/game/fifa20/match/end", {
            "endReason": "WIN", "goalsFor": 3, "goalsAgainst": 1,
        })
        self.assertEqual(points_after, retried["battlePoints"])
        self.assertEqual(credits_after, server.STATE.credits())

        _, refreshed_hub = self._response("GET", "/ut/game/fifa20/sqbt/user/hub")
        self.assertEqual(1, refreshed_hub["gamesPlayed"])
        _, leaderboard = self._response("GET", "/ut/game/fifa20/sqbt/user/leaderboard")
        self.assertEqual(100, len(leaderboard["entries"]))
        self.assertTrue(all(
            entry["persona"] != str(server.CFG["display_name"])
            for entry in leaderboard["entries"]
        ))
        self.assertEqual(list(range(1, 101)), [entry["rank"] for entry in leaderboard["entries"]])
        self.assertIsInstance(leaderboard["entries"][0]["score"], dict)
        self.assertIn("value", leaderboard["entries"][0]["score"])

    def test_squad_battles_weekly_reward_can_only_be_claimed_once(self) -> None:
        state = server._sb_new_state()
        state.update({
            "competitionEnd": server.now_s() - 1,
            "battlePoints": 7000,
            "matchesPlayed": 5,
            "pointMatchesPlayed": 5,
        })
        server._sb_save(state)

        _, hub = self._response("GET", "/ut/game/fifa20/sqbt/user/hub")
        self.assertTrue(hub["isPrizeAvailable"])
        _, preview = self._response("GET", "/ut/game/fifa20/sqbt/user/prize")
        self.assertTrue(preview["isPrizeAvailable"])
        self.assertEqual("GOLD 2", preview["tierName"])

        credits_before = server.STATE.credits()
        _, claimed = self._response("POST", "/ut/game/fifa20/sqbt/user/prize")
        self.assertTrue(claimed["claimed"])
        self.assertEqual(credits_before + 11000, server.STATE.credits())
        self.assertEqual({1025: 1, 1013: 1}, server.STATE.unopened_pack_counts())

        _, duplicate = self._response("POST", "/ut/game/fifa20/sqbt/user/prize")
        self.assertFalse(duplicate["isPrizeAvailable"])
        self.assertEqual(credits_before + 11000, server.STATE.credits())
        self.assertEqual({1025: 1, 1013: 1}, server.STATE.unopened_pack_counts())

    def test_featured_squad_launches_totw_and_does_not_consume_regular_limit(self) -> None:
        item_ids = self._seed_bronze_players()
        server.STATE.save_squad({
            "id": 1, "name": "Test XI", "active": True, "formation": "f442",
            "players": [
                {"index": index, "itemData": {"id": item_id}}
                for index, item_id in enumerate(item_ids)
            ],
        })
        state = server._sb_state()
        status, match = self._response("POST", "/ut/game/fifa20/match", {
            "sqbtEventId": state["competitionId"],
            "sqbtOpponentSquadId": server._TOTW_LATEST_ID,
            "featureConsumerId": "sqbttotw",
            "sqbtMatchDifficulty": 4,
        })
        self.assertEqual(200, status)
        self.assertEqual(server._TOTW_LATEST_ID, match["featuredSquadId"])
        self.assertEqual("sqbttotw", match["featureConsumerId"])
        self.assertEqual(server._TOTW_LATEST_ID, match["opponentSquad"]["id"])

        status, settled = self._response("POST", "/ut/game/fifa20/match/end", {
            "endReason": "WIN", "goalsFor": 2, "goalsAgainst": 0,
        })
        self.assertEqual(200, status)
        self.assertGreater(settled["squadBattlePoints"], 0)
        saved = server._sb_state()
        self.assertTrue(saved["featuredPlayed"])
        self.assertEqual("WIN", saved["featuredResult"])
        self.assertEqual(0, saved["pointMatchesPlayed"])
        self.assertEqual(1, saved["matchesPlayed"])

        _, stats = self._response(
            "GET", "/ut/game/fifa20/featuredsquad/user/stats?featureConsumerId=sqbttotw"
        )
        self.assertFalse(stats["featuredSquadAvailable"])
        self.assertEqual(1, stats["wins"])
        self.assertEqual(saved["featuredPoints"], stats["points"])

    def test_custom_featured_squad_keeps_identity_during_match_launch(self) -> None:
        item_ids = self._seed_bronze_players()
        server.STATE.save_squad({
            "id": 1, "name": "Test XI", "active": True, "formation": "f442",
            "players": [
                {"index": index, "itemData": {"id": item_id}}
                for index, item_id in enumerate(item_ids)
            ],
        })
        state = server._sb_state()
        status, match = self._response("POST", "/ut/game/fifa20/match", {
            "sqbtEventId": state["competitionId"],
            "sqbtOpponentSquadId": server._SB_FEATURED_CUSTOM_ID,
            "featureConsumerId": "sqbt",
            "sqbtMatchDifficulty": 4,
        })
        self.assertEqual(200, status)
        self.assertEqual(server._SB_FEATURED_CUSTOM_ID, match["featuredSquadId"])
        self.assertEqual("sqbt", match["featureConsumerId"])
        self.assertEqual(server._SB_FEATURED_CUSTOM_ID, match["opponentSquad"]["id"])
        self.assertGreater(sum(
            int(slot["itemData"].get("rareflag", 0)) > 1
            for slot in match["opponentSquad"]["players"][:11]
        ), 0)

    def test_squad_battles_rewards_match_fifa20_bronze_to_elite_table(self) -> None:
        self.assertEqual({
            0: (0, [1019]),
            1: (0, [1024]),
            2: (800, [1024, 1024]),
            3: (1500, [1024, 1005]),
            4: (4000, [1024, 1005, 1005]),
            5: (7000, [1016, 1016]),
            6: (10000, [1016, 1025]),
            7: (11000, [1025, 1013]),
            8: (11000, [1013, 1016]),
            9: (12000, [1013, 1013, 1008]),
            10: (15000, [1008, 1008, 1013]),
            11: (30000, [1009, 1009]),
        }, server._SB_REWARDS)

    def test_retired_player_pick_rewards_generate_matching_positional_cards(self) -> None:
        expected = {1020: "CB", 1021: "CM", 1022: "ST", 1023: "GK"}
        for pack_id, position in expected.items():
            result = server.STATE.create_pack(pack_id)
            self.assertEqual(1, result["numberItems"])
            item = result["itemData"][0]
            self.assertEqual(position, item["preferredPosition"])
            self.assertGreaterEqual(item["rating"], 75)
            self.assertLessEqual(item["rating"], 80)

    def test_store_exposes_three_native_player_pick_test_articles(self) -> None:
        store = server._store_purchasegroups()["purchase"]
        picks = {entry["id"]: entry for entry in store if entry.get("isPlayerPick")}
        self.assertEqual({25, 26, 27}, set(picks))
        self.assertEqual(("promo", 3, 84), (
            picks[25]["playerPickPool"], picks[25]["playerPickOptions"],
            picks[25]["playerPickMinRating"],
        ))
        self.assertEqual(("tots", 3, 88), (
            picks[26]["playerPickPool"], picks[26]["playerPickOptions"],
            picks[26]["playerPickMinRating"],
        ))
        self.assertEqual(("icon_moments", 3, 90), (
            picks[27]["playerPickPool"], picks[27]["playerPickOptions"],
            picks[27]["playerPickMinRating"],
        ))

    def test_store_player_pick_purchase_grants_real_misc_token_and_opens(self) -> None:
        with server.STATE.lock:
            server.STATE.conn.execute(
                "INSERT INTO kv(key,value) VALUES('credits',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps(50000),),
            )
            server.STATE.conn.commit()
        status, bought = self._response(
            "POST", "/ut/game/fifa20/purchased/items", {"packId": 25, "useCredits": 1}
        )
        self.assertEqual(200, status)
        self.assertTrue(bought["isPlayerPick"])
        self.assertEqual(1, bought["numberItems"])
        token = bought["itemList"][0]
        self.assertEqual(server.PICK_STATE.REAL_PICK_RESOURCE_ID, token["resourceId"])
        self.assertEqual("miscPlayerPick", token["sourceMember"])
        stored_token = server.STATE.get_item(token["id"])
        self.assertIsNotNone(stored_token)
        self.assertEqual("unassigned", stored_token["pile"])
        self.assertEqual(45000, server.STATE.credits())
        registered = server.PICK_STATE.token(server.STATE, token["id"])
        self.assertIsNotNone(registered)
        self.assertEqual("promo", registered["pool_key"])
        self.assertEqual(3, registered["option_count"])
        self.assertTrue(all(
            int(server.player_meta(rid).get("rating", 0)) >= 84
            for rid in registered["pool"]
        ))

        status, opened = self._response(
            "POST", f"/ut/game/fifa20/item/nontargeted?itemId={token['id']}"
        )
        self.assertEqual(200, status)
        self.assertEqual(3, opened["numberItems"])
        self.assertTrue(server.PICK_STATE.pending_flag(server.STATE))

    def test_native_player_pick_token_open_select_and_retry_is_durable(self) -> None:
        pool = server._generate_player_pick_resources("promo", 3)
        self.assertGreaterEqual(len(pool), 3)
        token_id = server.STATE.next_item_id()
        token = server.PICK_STATE.token_item(token_id, "1 of 3 Promo Players")
        with server.STATE.lock:
            server.STATE.conn.execute(
                "INSERT INTO items(id,resource_id,pile,data) VALUES(?,?,?,?)",
                (token_id, token["resourceId"], "unassigned", json.dumps(token)),
            )
            server.PICK_STATE.register_cursor(
                server.STATE.conn, token, "promo", "1 of 3 Promo Players", 3, pool,
            )
            server.STATE.conn.commit()

        status, opened = self._response(
            "POST", f"/ut/game/fifa20/item/nontargeted?itemId={token_id}"
        )
        self.assertEqual(200, status)
        self.assertEqual(3, opened["numberItems"])
        self.assertIsNone(server.STATE.get_item(token_id))
        self.assertTrue(server.PICK_STATE.pending_flag(server.STATE))
        candidate = opened["items"][0]

        status, selected = self._response(
            "POST", f"/ut/game/fifa20/playerpicks/item/{candidate['resourceId']}/select"
        )
        self.assertEqual(200, status)
        self.assertEqual(candidate["resourceId"], selected["resourceId"])
        self.assertEqual("unassigned", selected["pile"])
        self.assertIsNotNone(server.STATE.get_item(selected["id"]))
        self.assertFalse(server.PICK_STATE.pending_flag(server.STATE))

        status, retried = self._response(
            "POST", f"/ut/game/fifa20/playerpicks/item/{candidate['resourceId']}/select"
        )
        self.assertEqual(200, status)
        self.assertEqual(selected["id"], retried["id"])

    def test_valid_repeatable_upgrade_consumes_cards_and_grants_one_pack(self) -> None:
        item_ids = self._seed_bronze_players()
        squad = {"players": [
            {"index": index, "itemData": {"id": item_id, "dream": False}}
            for index, item_id in enumerate(item_ids)
        ]}
        status, saved = self._response(
            "PUT", "/ut/game/fifa20/sbs/challenge/3001/squad", squad
        )
        self.assertEqual(200, status)
        self.assertEqual(11, saved["savedPlayerCount"])

        status, completed = self._response(
            "PUT", "/ut/game/fifa20/sbs/challenge/3001?skipUserSquadValidation=false", {}
        )
        self.assertEqual(200, status)
        self.assertTrue(completed["success"])
        self.assertEqual(1, completed["timesCompleted"])
        self.assertEqual({1012: 1}, server.STATE.unopened_pack_counts())
        self.assertTrue(all(server.STATE.get_item(item_id) is None for item_id in item_ids))

        # A completed repeatable tile must remain selectable. FIFA closes a set
        # when challengesCompletedCount equals challengesCount, even when the
        # repeatable bit is true, so each new cycle is advertised as open.
        _, sets_payload = self._response("GET", "/ut/game/fifa20/sbs/sets")
        upgrade_sets = {
            entry["setId"]: entry
            for category in sets_payload["categories"]
            for entry in category["sets"]
        }
        self.assertTrue(upgrade_sets[30]["repeatable"])
        self.assertEqual(1, upgrade_sets[30]["timesCompleted"])
        self.assertEqual(0, upgrade_sets[30]["challengesCompletedCount"])
        _, challenges = self._response("GET", "/ut/game/fifa20/sbs/setid/30/challenges")
        self.assertEqual("NOT_STARTED", challenges["challenges"][0]["status"])

        status, reopened = self._response(
            "POST", "/ut/game/fifa20/sbs/challenge/3001", {}
        )
        self.assertEqual(200, status)
        self.assertEqual(3001, reopened["challengeId"])
        self.assertEqual(0, reopened["savedPlayerCount"] if "savedPlayerCount" in reopened else 0)

        status, opened = self._response(
            "POST", "/ut/game/fifa20/purchased/items", {"packId": 1012, "useCredits": 0}
        )
        self.assertEqual(200, status)
        self.assertEqual(2, opened["numberItems"])
        self.assertEqual({}, server.STATE.unopened_pack_counts())

        status, blocked = self._response(
            "POST", "/ut/game/fifa20/purchased/items", {"packId": 1012, "useCredits": 0}
        )
        self.assertEqual(200, status)
        self.assertFalse(blocked["success"])
        self.assertEqual("PACK_NOT_OWNED", blocked["reason"])

    def test_invalid_squad_is_not_consumed_or_rewarded(self) -> None:
        item_ids = self._seed_bronze_players()[:10]
        squad = {"players": [
            {"index": index, "itemData": {"id": item_id}}
            for index, item_id in enumerate(item_ids)
        ]}
        status, result = self._response(
            "POST", "/ut/game/fifa20/sbs/challenge/3001", squad
        )
        self.assertEqual(200, status)
        self.assertFalse(result["success"])
        self.assertTrue(all(server.STATE.get_item(item_id) is not None for item_id in item_ids))
        self.assertEqual({}, server.STATE.unopened_pack_counts())

    def test_transfer_market_search_has_gold_specials_and_exact_filters(self) -> None:
        status, result = self._response(
            "GET", "/ut/game/fifa20/transfermarket?type=player&start=0&num=50"
        )
        self.assertEqual(200, status)
        self.assertEqual(50, result["count"])
        self.assertEqual(185, result["total"])
        status, deep_page = self._response(
            "GET", "/ut/game/fifa20/transfermarket?type=player&start=150&num=50"
        )
        self.assertEqual(200, status)
        self.assertEqual(35, deep_page["count"])
        self.assertEqual(185, deep_page["total"])
        all_trade_ids = []
        for start in range(0, 185, 50):
            _, page = self._response(
                "GET", f"/ut/game/fifa20/transfermarket?type=player&start={start}&num=50"
            )
            all_trade_ids.extend(row["tradeId"] for row in page["auctionInfo"])
        self.assertEqual(185, len(all_trade_ids))
        self.assertEqual(185, len(set(all_trade_ids)))
        all_items = [row["item_data"] for row in server.STATE.list_auctions("cpu")]
        self.assertTrue(any(int(item.get("rareflag", 0)) <= 1 for item in all_items))
        self.assertTrue(any(int(item.get("rareflag", 0)) > 1 for item in all_items))
        self.assertTrue(all(row["buyNowPrice"] > row["startingBid"] for row in result["auctionInfo"]))

        status, filtered = self._response(
            "GET", "/ut/game/fifa20/transfermarket?type=player&pos=LW&lev=gold&micr=150&macr=5000000&num=50"
        )
        self.assertEqual(200, status)
        for auction in filtered["auctionInfo"]:
            item = auction["itemData"]
            self.assertEqual("LW", item["preferredPosition"])
            self.assertGreaterEqual(item["rating"], 75)
            self.assertLessEqual(item["rareflag"], 1)
            self.assertLessEqual(auction["buyNowPrice"], 5_000_000)

        status, specials = self._response(
            "GET", "/ut/game/fifa20/transfermarket?type=player&rare=SP&num=50"
        )
        self.assertEqual(200, status)
        self.assertGreater(specials["count"], 0)
        self.assertTrue(all(row["itemData"]["rareflag"] > 1 for row in specials["auctionInfo"]))

        for quality, low, high in (("silver", 65, 74), ("bronze", 0, 64)):
            status, tier = self._response(
                "GET", f"/ut/game/fifa20/transfermarket?type=player&lev={quality}&num=50"
            )
            self.assertEqual(200, status)
            self.assertGreater(tier["count"], 0)
            self.assertTrue(all(low <= row["itemData"]["rating"] <= high
                                for row in tier["auctionInfo"]))

        status, managers = self._response(
            "GET", "/ut/game/fifa20/transfermarket?type=staff&start=0&num=12"
        )
        self.assertEqual(200, status)
        self.assertGreater(managers["count"], 0)
        self.assertTrue(all(row["itemData"]["itemType"] == "manager"
                            for row in managers["auctionInfo"]))

    def test_transfer_market_buy_now_delivers_unassigned_then_stores_in_club(self) -> None:
        _, search = self._response("GET", "/ut/game/fifa20/transfermarket?type=player&num=1")
        listing = search["auctionInfo"][0]
        before = server.STATE.credits()
        status, bought = self._response(
            "POST", f"/ut/game/fifa20/trade/{listing['tradeId']}/offer",
            {"bid": listing["buyNowPrice"]},
        )
        self.assertEqual(200, status)
        self.assertEqual(before - listing["buyNowPrice"], bought["credits"])
        self.assertEqual("closed", bought["auctionInfo"][0]["tradeState"])
        unassigned = server.STATE.list_items("unassigned")
        self.assertEqual(1, len(unassigned))
        self.assertEqual(listing["itemData"]["resourceId"], unassigned[0]["resourceId"])
        _, purchased = self._response("GET", "/ut/game/fifa20/purchased/items")
        self.assertEqual(1, purchased["numberItems"])
        self.assertEqual(unassigned[0]["id"], purchased["itemData"][0]["id"])
        item_id = unassigned[0]["id"]
        status, moved = self._response("PUT", "/ut/game/fifa20/item", {
            "itemData": [{"id": item_id, "pile": "club"}],
        })
        self.assertEqual(200, status)
        self.assertTrue(moved["itemData"][0]["success"])
        self.assertEqual("club", server.STATE.get_item(item_id)["pile"])

    def test_inline_buy_now_preserves_auction_item_id_for_post_buy_actions(self) -> None:
        gold, _ = server._market_pools()
        resource_id = gold[0]
        auction_item_id = server.STATE.next_item_id()
        market_item = server.STATE.build_player_item(resource_id, auction_item_id, "trade")
        auction = server.STATE.create_auction(
            resource_id=resource_id,
            item_data=market_item,
            seller_type="cpu",
            seller_name="Local Trader",
            starting_bid=650,
            buy_now_price=700,
            duration=3600,
        )
        server.STATE.add_credits(10_000)

        status, bought = self._response(
            "POST", f"/ut/game/fifa20/trade/{auction['trade_id']}/offer", {"bid": 700}
        )

        self.assertEqual(200, status)
        self.assertEqual(auction_item_id, bought["auctionInfo"][0]["itemData"]["id"])
        delivered = server.STATE.get_item(auction_item_id)
        self.assertIsNotNone(delivered)
        self.assertEqual("unassigned", delivered["pile"])

        status, moved = self._response("PUT", "/ut/game/fifa20/item", {
            "itemData": [{"id": auction_item_id, "pile": "club", "tradeId": 0}],
        })
        self.assertEqual(200, status)
        self.assertTrue(moved["itemData"][0]["success"])
        self.assertEqual("club", server.STATE.get_item(auction_item_id)["pile"])

    def test_transfer_market_bid_escrow_and_expiry_delivery(self) -> None:
        _, search = self._response("GET", "/ut/game/fifa20/transfermarket?type=player&num=1")
        listing = search["auctionInfo"][0]
        before = server.STATE.credits()
        status, result = self._response(
            "POST", f"/ut/game/fifa20/trade/{listing['tradeId']}/offer",
            {"bid": listing["startingBid"]},
        )
        self.assertEqual(200, status)
        self.assertEqual(before - listing["startingBid"], result["credits"])
        server.STATE.update_auction(listing["tradeId"], expires_at=server.now_s() - 1)
        _, watched = self._response("GET", "/ut/game/fifa20/watchlist")
        won = next(row for row in watched["auctionInfo"] if row["tradeId"] == listing["tradeId"])
        self.assertEqual("closed", won["tradeState"])
        self.assertEqual("highest", won["bidState"])
        self.assertEqual(1, len(server.STATE.list_items("unassigned")))

    def test_transfer_market_owned_squad_player_listing_sale_and_claim(self) -> None:
        gold, _ = server._market_pools()
        item = server.STATE.make_player_item(gold[0], "club")
        server.STATE.save_squad({
            "id": 1, "name": "Market XI", "active": True,
            "players": [{"index": 0, "itemId": item["id"]}],
        })
        buy_now = server._market_round_price(server._market_recommended_price(item["resourceId"]))
        starting = server._market_round_price(int(buy_now * 0.9))
        status, listed = self._response("POST", "/ut/game/fifa20/auctionhouse", {
            "itemData": {"id": item["id"]}, "startingBid": starting,
            "buyNowPrice": buy_now, "duration": 3600,
        })
        self.assertEqual(200, status)
        trade_id = listed["auctionInfo"][0]["tradeId"]
        self.assertEqual("trade", server.STATE.get_item(item["id"])["pile"])
        self.assertNotIn(item["id"], [card["id"] for card in server.STATE.list_items("club")])
        self.assertNotIn(item["id"], server.STATE.active_squad_item_ids())
        _, search = self._response(
            "GET", f"/ut/game/fifa20/transfermarket?type=player&maskedDefId={item['assetId']}&num=50"
        )
        self.assertNotIn(trade_id, [row["tradeId"] for row in search["auctionInfo"]])

        auction = server.STATE.get_auction(trade_id)
        auction_data = dict(auction["item_data"])
        auction_data["_cpuBuyAt"] = server.now_s() - 1
        server.STATE.update_auction(trade_id, item_data=auction_data)
        before_sale = server.STATE.credits()
        _, tradepile = self._response("GET", "/ut/game/fifa20/tradepile")
        sold = next(row for row in tradepile["auctionInfo"] if row["tradeId"] == trade_id)
        self.assertEqual("closed", sold["tradeState"])
        expected_net = buy_now - (buy_now * 5 // 100)
        self.assertEqual(before_sale + expected_net, server.STATE.credits())
        before_claim = server.STATE.credits()
        status, cleared = self._response("DELETE", f"/ut/game/fifa20/trade/{trade_id}")
        self.assertEqual(200, status)
        self.assertEqual(before_claim, cleared["credits"])
        self.assertIsNone(server.STATE.get_item(item["id"]))

    def test_cleared_sold_trades_do_not_reappear_after_a_new_listing(self) -> None:
        gold, _ = server._market_pools()
        old_item = server.STATE.make_player_item(gold[0], "club")
        _, listed = self._response("POST", "/ut/game/fifa20/auctionhouse", {
            "itemData": {"id": old_item["id"]}, "startingBid": 1000,
            "buyNowPrice": 1100, "duration": 3600,
        })
        old_trade_id = listed["auctionInfo"][0]["tradeId"]
        server.STATE.update_auction(
            old_trade_id, status="sold", highest_bidder="cpu", current_bid=1100,
        )
        server._market_credit_user_sale(server.STATE.get_auction(old_trade_id))

        # Exact retail-client request captured in the tester's log. It has no
        # request body and uses GET under /ut/delete, despite deleting rows.
        status, cleared = self._response(
            "GET", "/ut/ut/delete/game/fifa20/trade/sold",
        )
        self.assertEqual(200, status)
        self.assertTrue(cleared["success"] if "success" in cleared else True)
        self.assertIsNone(server.STATE.get_auction(old_trade_id))
        self.assertIsNone(server.STATE.get_item(old_item["id"]))

        # Pack opening must not reconstruct previously cleared auctions.
        server.STATE.create_pack(3)
        _, after_pack = self._response("GET", "/ut/game/fifa20/tradepile")
        self.assertNotIn(old_trade_id, {
            row["tradeId"] for row in after_pack["auctionInfo"]
        })

        new_item = server.STATE.make_player_item(gold[1], "club")
        _, new_listing = self._response("POST", "/ut/game/fifa20/auctionhouse", {
            "itemData": {"id": new_item["id"]}, "startingBid": 1200,
            "buyNowPrice": 1300, "duration": 3600,
        })
        new_trade_id = new_listing["auctionInfo"][0]["tradeId"]
        _, tradepile = self._response("GET", "/ut/game/fifa20/tradepile")
        ids = {row["tradeId"] for row in tradepile["auctionInfo"]}
        self.assertIn(new_trade_id, ids)
        self.assertNotIn(old_trade_id, ids)

    def test_transfer_market_price_limits_and_pending_trade_item(self) -> None:
        gold, _ = server._market_pools()
        item = server.STATE.make_player_item(gold[0], "club")
        status, moved = self._response("PUT", "/ut/game/fifa20/item", {
            "itemData": [{"id": item["id"], "pile": "trade", "swap": 0, "tradeId": 0}],
        })
        self.assertEqual(200, status)
        self.assertTrue(moved["itemData"][0]["success"])

        status, tradepile = self._response("GET", "/ut/game/fifa20/tradePile")
        self.assertEqual(200, status)
        pending = next(
            row for row in tradepile["auctionInfo"]
            if row["itemData"]["id"] == item["id"]
        )
        self.assertEqual("inactive", pending["tradeState"])
        self.assertEqual(0, pending["tradeId"])
        self.assertTrue(pending["tradeOwner"])
        self.assertEqual([], tradepile["itemData"])
        self.assertEqual("trade", pending["itemData"]["pile"])
        self.assertGreater(pending["itemData"]["marketDataMaxPrice"],
                           pending["itemData"]["marketDataMinPrice"])

        status, limits = self._response(
            "GET", f"/ut/game/fifa20/marketdata/pricelimits?defId={item['resourceId']}"
        )
        self.assertEqual(200, status)
        self.assertIsInstance(limits, list)
        self.assertEqual(item["resourceId"], limits[0]["itemId"])
        self.assertGreaterEqual(limits[0]["minPrice"], 150)
        self.assertGreater(limits[0]["maxPrice"], limits[0]["minPrice"])

        status, item_limits = self._response(
            "GET", f"/ut/game/fifa20/marketdata/item/pricelimits?itemIdList={item['id']}"
        )
        self.assertEqual(200, status)
        self.assertIsInstance(item_limits, list)
        self.assertEqual(item["id"], item_limits[0]["itemId"])

    def test_transfer_market_bid_status_lite_refreshes_selected_target(self) -> None:
        _, search = self._response("GET", "/ut/game/fifa20/transfermarket?type=player&num=1")
        listing = search["auctionInfo"][0]
        status, _ = self._response(
            "POST", f"/ut/game/fifa20/trade/{listing['tradeId']}/offer",
            {"bid": listing["startingBid"]},
        )
        self.assertEqual(200, status)

        status, refreshed = self._response(
            "GET",
            f"/ut/game/fifa20/trade/status/lite?tradeIds={listing['tradeId']}&includeBankInfo=true",
        )
        self.assertEqual(200, status)
        self.assertEqual(1, len(refreshed["auctionInfo"]))
        target = refreshed["auctionInfo"][0]
        self.assertEqual(listing["tradeId"], target["tradeId"])
        self.assertEqual("active", target["tradeState"])
        self.assertEqual("highest", target["bidState"])
        self.assertGreater(target["expires"], 0)
        self.assertIn("currencies", refreshed)

    def test_transfer_market_purchase_count_waits_before_bot_restock(self) -> None:
        _, search = self._response("GET", "/ut/game/fifa20/transfermarket?type=player&num=1")
        listing = search["auctionInfo"][0]
        active_before = sum(
            1 for row in server.STATE.list_auctions()
            if row["status"] == "active" and int(row["expires_at"]) > server.now_s()
        )
        status, _ = self._response(
            "POST", f"/ut/game/fifa20/trade/{listing['tradeId']}/offer",
            {"bid": listing["buyNowPrice"]},
        )
        self.assertEqual(200, status)
        self._response("GET", "/ut/game/fifa20/transfermarket?type=player&num=1")
        active_after = sum(
            1 for row in server.STATE.list_auctions()
            if row["status"] == "active" and int(row["expires_at"]) > server.now_s()
        )
        self.assertEqual(active_before - 1, active_after)

    def test_transfer_market_compare_price_is_exact_and_read_only(self) -> None:
        _, special = server._market_pools()
        resource_id = next(
            rid for rid in special
            if int(server.player_meta(rid).get("assetId", 0)) in server.PLAYER_DB
            and int(server.player_meta(rid).get("assetId", 0)) != rid
        )
        base_resource_id = int(server.player_meta(resource_id)["assetId"])
        item = server.STATE.build_player_item(
            resource_id, server.STATE.next_item_id(), "trade"
        )
        listing = server.STATE.create_auction(
            resource_id=resource_id,
            item_data=item,
            seller_type="cpu",
            seller_name="Existing Trader",
            starting_bid=10000,
            buy_now_price=12000,
            duration=3600,
        )
        base_item = server.STATE.build_player_item(
            base_resource_id, server.STATE.next_item_id(), "trade"
        )
        server.STATE.create_auction(
            resource_id=base_resource_id,
            item_data=base_item,
            seller_type="cpu",
            seller_name="Base Version Trader",
            starting_bid=9000,
            buy_now_price=11000,
            duration=3600,
        )
        auction_count_before = len(server.STATE.list_auctions())
        with patch.object(server, "_market_seed", return_value=None):
            status, comparison = self._response(
                "GET",
                f"/ut/game/fifa20/transfermarket?type=player&start=0&num=8&definitionId={resource_id}",
            )
        self.assertEqual(200, status)
        self.assertEqual(1, comparison["count"])
        self.assertEqual(listing["trade_id"], comparison["auctionInfo"][0]["tradeId"])
        self.assertEqual(resource_id, comparison["auctionInfo"][0]["itemData"]["resourceId"])
        self.assertEqual(resource_id, comparison["auctionInfo"][0]["itemData"]["definitionId"])
        self.assertEqual(auction_count_before, len(server.STATE.list_auctions()))

    def test_transfer_market_upgrade_removes_legacy_compare_duplicates(self) -> None:
        _, special = server._market_pools()
        resource_id = special[0]
        created: list[dict] = []
        for index in range(6):
            item = server.STATE.build_player_item(
                resource_id, server.STATE.next_item_id(), "trade"
            )
            created.append(server.STATE.create_auction(
                resource_id=resource_id,
                item_data=item,
                seller_type="cpu",
                seller_name=server._MARKET_SELLER_NAMES[
                    (resource_id + index) % len(server._MARKET_SELLER_NAMES)
                ],
                starting_bid=10000,
                buy_now_price=12000,
                duration=3600,
            ))
        with server.STATE.lock:
            server.STATE.conn.execute(
                "UPDATE auctions SET created_at=?", (server.now_s() - 10,)
            )
            server.STATE.conn.execute(
                "DELETE FROM kv WHERE key='market_compare_seed_cleanup_v094'"
            )
            server.STATE.conn.commit()
        server.STATE.update_auction(created[-1]["trade_id"], watched=1)

        removed = server._market_cleanup_compare_seed_artifacts()
        self.assertEqual(5, removed)
        remaining = server.STATE.list_auctions()
        self.assertEqual(1, len(remaining))
        self.assertEqual(created[-1]["trade_id"], remaining[0]["trade_id"])

    def test_transfer_market_upgrade_shrinks_oversized_cpu_population(self) -> None:
        server._market_seed()
        gold, _ = server._market_pools()
        resource_id = gold[0]
        created = []
        for _ in range(25):
            item = server.STATE.build_player_item(
                resource_id, server.STATE.next_item_id(), "trade"
            )
            created.append(server.STATE.create_auction(
                resource_id=resource_id,
                item_data=item,
                seller_type="cpu",
                seller_name="Population Migration",
                starting_bid=1000,
                buy_now_price=1200,
                duration=3600,
            ))
        server.STATE.update_auction(created[-1]["trade_id"], watched=1)
        with server.STATE.lock:
            server.STATE.conn.execute(
                "DELETE FROM kv WHERE key='market_proven_population_restore_v1031'"
            )
            server.STATE.conn.commit()
        active = [
            row for row in server.STATE.list_auctions("cpu")
            if row["status"] == "active" and row["expires_at"] > server.now_s()
        ]
        restored = server._market_restore_proven_population(active, {
            "gold": 70, "special": 45, "silver": 40, "bronze": 30, "manager": 15,
        })
        self.assertEqual(200, len(restored))
        self.assertTrue(any(
            row["trade_id"] == created[-1]["trade_id"] for row in restored
        ))

    def test_consumable_picker_returns_native_stacks_and_stats(self) -> None:
        contract = server.STATE.make_consumable_item(5_001_003, "club", 20)
        fitness = server.STATE.make_consumable_item(5_002_003, "club", 10)

        status, picker = self._response(
            "GET", "/ut/game/fifa20/club/consumables/development"
        )
        self.assertEqual(200, status)
        self.assertEqual(2, picker["itemCount"])
        contract_row = next(
            row for row in picker["itemData"] if row["resourceId"] == 5_001_003
        )
        self.assertEqual(20, contract_row["count"])
        self.assertEqual(contract["id"], contract_row["item"]["id"])
        self.assertEqual("development", contract_row["item"]["itemType"])
        self.assertEqual(7, contract_row["item"]["cardassetid"])

        status, stats = self._response(
            "GET", "/ut/game/fifa20/club/stats/consumables"
        )
        self.assertEqual(200, status)
        self.assertEqual({"category", "stat"}, set(stats))
        self.assertEqual("consumables", stats["category"])
        self.assertTrue(stats["stat"])
        self.assertTrue(all(
            set(row) == {"contextId", "contextValue", "type", "typeValue"}
            and row["contextId"] == 5
            and row["contextValue"] == 0
            for row in stats["stat"]
        ))
        totals = {row["type"]: row["typeValue"] for row in stats["stat"]}
        self.assertEqual(20, totals["consumablesContract"])
        self.assertEqual(20, totals["consumablesContractPlayer"])
        self.assertEqual(10, totals["consumablesFitness"])
        self.assertEqual(10, totals["consumablesFitnessPlayer"])
        self.assertEqual(0, totals["consumablesFitnessTeam"])
        self.assertEqual(fitness["resourceId"], 5_002_003)

    def test_native_club_stat_routes_do_not_emit_unknown_members(self) -> None:
        status, year = self._response("GET", "/ut/game/fifa20/club/stats/year")
        self.assertEqual(200, status)
        self.assertEqual({"category", "stat"}, set(year))
        self.assertEqual("year", year["category"])
        self.assertTrue(year["stat"])
        self.assertTrue(all(
            set(row) == {"contextId", "contextValue", "type", "typeValue"}
            and row["contextId"] == 1
            and row["contextValue"] == 0
            for row in year["stat"]
        ))

        status, staff = self._response("GET", "/ut/game/fifa20/club/stats/staff")
        self.assertEqual(200, status)
        self.assertEqual({"bonus": []}, staff)

    def test_consumable_application_decrements_stack(self) -> None:
        gold, _ = server._market_pools()
        player = server.STATE.make_player_item(gold[0], "club")
        server.STATE.update_item_fields(player["id"], {"contract": 0, "contracts": 0})
        consumable = server.STATE.make_consumable_item(5_001_003, "club", 2)

        updated = server._apply_consumable_resource(5_001_003, [player["id"]])
        self.assertEqual(1, len(updated))
        self.assertGreater(updated[0]["contract"], 0)
        remaining = server.STATE.get_item(consumable["id"])
        self.assertIsNotNone(remaining)
        self.assertEqual(1, remaining["stackCount"])

    def test_consumable_search_stacks_before_the_native_100_row_limit(self) -> None:
        resource_ids = list(server.CONSUMABLE_DB)
        for index in range(330):
            server.STATE.make_consumable_item(
                resource_ids[index % len(resource_ids)], "club", 1
            )

        status, response = self._response(
            "GET", "/ut/game/fifa20/club/consumables?start=0&count=100"
        )
        self.assertEqual(200, status)
        self.assertEqual(len(resource_ids), response["total"])
        self.assertEqual(len(resource_ids), response["itemCount"])
        self.assertEqual(330, sum(row["count"] for row in response["itemData"]))
        self.assertEqual(len(resource_ids), len({
            row["resourceId"] for row in response["itemData"]
        }))
        self.assertTrue(all(
            row["count"] == row["item"]["stackCount"]
            for row in response["itemData"]
        ))

    def test_position_modifier_changes_player_and_survives_startup_enrichment(self) -> None:
        striker_resource = next(
            rid for rid, meta in server.PLAYER_DB.items()
            if str(meta.get("preferredPosition", "")).upper() == "ST"
        )
        player = server.STATE.make_player_item(striker_resource, "club")
        modifier = server.STATE.make_consumable_item(5_003_078, "club", 2)

        updated = server._apply_consumable_resource(5_003_078, [player["id"]])
        self.assertEqual("CF", updated[0]["preferredPosition"])
        self.assertEqual(1, server.STATE.get_item(modifier["id"])["stackCount"])

        server.STATE._enrich_existing_player_items()
        self.assertEqual("CF", server.STATE.get_item(player["id"])["preferredPosition"])
        native = server._native_player_item(server.STATE.get_item(player["id"]))
        self.assertEqual("CF", native["preferredPosition"])

    def test_consumable_route_returns_updated_native_player_document(self) -> None:
        striker_resource = next(
            rid for rid, meta in server.PLAYER_DB.items()
            if str(meta.get("preferredPosition", "")).upper() == "ST"
        )
        player = server.STATE.make_player_item(striker_resource, "club")
        server.STATE.make_consumable_item(5_003_078, "club", 1)

        status, response = self._response(
            "POST", "/ut/game/fifa20/item/resource/5003078",
            {"apply": [{"id": player["id"]}]},
        )
        self.assertEqual(200, status)
        self.assertTrue(response["consumableConsumed"])
        self.assertEqual(1, len(response["itemData"]))
        self.assertEqual(player["id"], response["itemData"][0]["id"])
        self.assertEqual("CF", response["itemData"][0]["preferredPosition"])
        self.assertEqual(response["itemData"], response["items"])

    def test_chemistry_style_uses_native_subtype_and_returns_player(self) -> None:
        striker_resource = next(
            rid for rid, meta in server.PLAYER_DB.items()
            if str(meta.get("preferredPosition", "")).upper() == "ST"
        )
        player = server.STATE.make_player_item(striker_resource, "club")
        server.STATE.make_consumable_item(5_003_111, "club", 1)

        status, response = self._response(
            "POST", "/ut/game/fifa20/item/resource/5003111",
            {"apply": [{"id": player["id"]}]},
        )
        self.assertEqual(200, status)
        self.assertTrue(response["consumableConsumed"])
        self.assertEqual(266, response["itemData"][0]["playStyle"])
        self.assertEqual(266, server.STATE.get_item(player["id"])["playStyle"])

    def test_position_modifier_rejects_wrong_source_without_consuming(self) -> None:
        midfielder_resource = next(
            rid for rid, meta in server.PLAYER_DB.items()
            if str(meta.get("preferredPosition", "")).upper() == "CM"
        )
        player = server.STATE.make_player_item(midfielder_resource, "club")
        modifier = server.STATE.make_consumable_item(5_003_078, "club", 1)
        self.assertEqual([], server._apply_consumable_resource(5_003_078, [player["id"]]))
        self.assertEqual(1, server.STATE.get_item(modifier["id"])["stackCount"])
        self.assertEqual("CM", server.STATE.get_item(player["id"])["preferredPosition"])

    def test_gold_item_packs_emit_native_modifier_and_club_items(self) -> None:
        consumables_pack = server.STATE.create_pack(19)
        self.assertEqual(10, consumables_pack["numberItems"])
        self.assertTrue(all(
            item["resourceId"] in server.CONSUMABLE_DB
            and item["itemType"] == "development"
            for item in consumables_pack["itemData"]
        ))
        source_members = {
            server.CONSUMABLE_DB[item["resourceId"]]["sourceMember"]
            for item in consumables_pack["itemData"]
        }
        self.assertNotIn("consumablesPosition", source_members)
        self.assertTrue(any("PlayStyle" in member for member in source_members))
        self.assertTrue(any("Contract" in member for member in source_members))
        self.assertTrue(any("Fitness" in member for member in source_members))
        position_pack = server.STATE.create_pack(22)
        self.assertEqual(12, position_pack["numberItems"])
        self.assertTrue(all(
            server.CONSUMABLE_DB[item["resourceId"]]["sourceMember"] == "consumablesPosition"
            for item in position_pack["itemData"]
        ))
        # Pack reveal remains Unassigned-shaped, but every development item is
        # already persisted as an owned club stack so it cannot disappear.
        for revealed in consumables_pack["itemData"]:
            stored = server.STATE.get_item(revealed["id"])
            self.assertEqual("unassigned", revealed["pile"])
            self.assertEqual("club", stored["pile"])

        club_pack = server.STATE.create_pack(20)
        self.assertEqual(5, club_pack["numberItems"])
        self.assertEqual(
            {"kit", "custom", "stadium", "ball"},
            {item["itemType"] for item in club_pack["itemData"]},
        )
        self.assertGreater(len(server._NATIVE_STADIUMS), 10)
        self.assertGreater(len(server._NATIVE_BALLS), 10)
        stadium = next(item for item in club_pack["itemData"] if item["itemType"] == "stadium")
        ball = next(item for item in club_pack["itemData"] if item["itemType"] == "ball")
        self.assertIn(stadium["resourceId"], server._NATIVE_STADIUMS)
        self.assertIn(ball["resourceId"], server._NATIVE_BALLS)
        self.assertNotEqual(191, ball["ballid"])
        self.assertEqual(
            ["custom", "stadium", "kit", "kit", "ball"],
            [item["itemType"] for item in club_pack["itemData"]],
        )

        with patch.dict(server.CFG, {"prefetch_pack_player_images": True}), patch.object(
            server, "_cached_catalog_player_head"
        ) as portrait_fetch:
            server._prime_pack_player_heads(club_pack["itemData"])
        portrait_fetch.assert_not_called()

    def test_club_item_activation_and_inventory_are_persistent(self) -> None:
        actives = server._active_club_items()
        self.assertEqual(set(server._CLUB_ACTIVE_STATES), {item["itemState"] for item in actives})
        home = next(item for item in server.STATE.list_items("club") if item.get("itemState") == "activeHomeKit")
        duplicate = server.STATE.make_club_item(home["resourceId"], "club", "free")
        status, _, body = server.route_fut(
            "PUT", f"/ut/game/fifa20/item/{duplicate['id']}",
            {"Content-Type": "application/json"},
            json.dumps({"itemState": "active", "activateSlotNumber": 0}).encode("utf-8"),
        )
        self.assertEqual(200, status)
        self.assertEqual(b"", body)
        self.assertEqual("activeHomeKit", server.STATE.get_item(duplicate["id"])["itemState"])
        self.assertEqual("free", server.STATE.get_item(home["id"])["itemState"])
        self.assertEqual(duplicate["id"], next(
            item["id"] for item in server._active_club_items()
            if item["itemState"] == "activeHomeKit"
        ))

        _, kit_search = self._response("GET", "/ut/game/fifa20/club?type=kit&count=50")
        self.assertGreaterEqual(kit_search["itemCount"], 3)
        self.assertIn(duplicate["id"], {item["id"] for item in kit_search["itemData"]})

    def test_gold_store_has_unique_names_and_two_item_packs(self) -> None:
        rows = [
            row for row in server._store_purchasegroups()["purchase"]
            if row["displayGroup"]["value"] == "gold"
        ]
        names = [row["name"].strip().casefold() for row in rows]
        self.assertEqual(len(names), len(set(names)))
        self.assertIn("Gold Consumables Pack", {row["name"] for row in rows})
        self.assertIn("Gold Club Items Pack", {row["name"] for row in rows})

    def test_transfer_market_manager_buy_now_delivers_manager(self) -> None:
        _, search = self._response("GET", "/ut/game/fifa20/transfermarket?type=staff&num=1")
        listing = search["auctionInfo"][0]
        status, _ = self._response(
            "POST", f"/ut/game/fifa20/trade/{listing['tradeId']}/offer",
            {"bid": listing["buyNowPrice"]},
        )
        self.assertEqual(200, status)
        unassigned = server.STATE.list_items("unassigned")
        self.assertEqual(1, len(unassigned))
        self.assertEqual("manager", unassigned[0]["itemType"])

    def test_transfer_market_bots_bid_on_reasonable_user_listings(self) -> None:
        gold, _ = server._market_pools()
        item = server.STATE.make_player_item(gold[1], "club")
        recommended = server._market_recommended_price(item["resourceId"])
        starting = server._market_round_price(int(recommended * 0.80))
        buy_now = server._market_round_price(int(recommended * 1.30))
        status, listed = self._response("POST", "/ut/game/fifa20/auctionhouse", {
            "itemData": {"id": item["id"]}, "startingBid": starting,
            "buyNowPrice": buy_now, "duration": 3600,
        })
        self.assertEqual(200, status)
        trade_id = listed["auctionInfo"][0]["tradeId"]
        auction = server.STATE.get_auction(trade_id)
        data = dict(auction["item_data"])
        data["_cpuActionAt"] = server.now_s() - 1
        data["_cpuMaxBid"] = recommended
        server.STATE.update_auction(trade_id, item_data=data)

        _, tradepile = self._response("GET", "/ut/game/fifa20/tradepile")
        active = next(row for row in tradepile["auctionInfo"] if row["tradeId"] == trade_id)
        self.assertEqual(starting, active["currentBid"])
        self.assertEqual(1, active["offers"])
        self.assertEqual("active", active["tradeState"])

        server.STATE.update_auction(trade_id, expires_at=server.now_s() - 1)
        _, tradepile = self._response("GET", "/ut/game/fifa20/tradepile")
        sold = next(row for row in tradepile["auctionInfo"] if row["tradeId"] == trade_id)
        self.assertEqual("closed", sold["tradeState"])

    def test_all_icon_moments_use_native_player_identity_ids(self) -> None:
        moments = [
            (resource_id, meta) for resource_id, meta in server.PLAYER_DB.items()
            if int(meta.get("rareflag", 0) or 0) == 84
        ]
        self.assertEqual(89, len(moments))
        self.assertTrue(all(
            (int(resource_id) & 0x00FFFFFF) == int(meta["assetId"])
            for resource_id, meta in moments
        ))
        self.assertTrue(all(
            int(resource_id) >= 4 * 0x01000000
            and meta.get("name")
            and len(meta.get("face", [])) == 6
            and isinstance(meta.get("attributes"), dict)
            for resource_id, meta in moments
        ))
        self.assertEqual(89, len(server.PLAYER_RESOURCE_ALIASES))

    def test_legacy_icon_moments_item_is_migrated_to_native_id(self) -> None:
        new_id, meta = next(
            (resource_id, definition)
            for resource_id, definition in server.PLAYER_DB.items()
            if int(definition.get("rareflag", 0) or 0) == 84
        )
        old_id = int(meta["legacyResourceIds"][0])
        item_id = 990000900001
        with server.STATE.lock:
            server.STATE.conn.execute(
                "INSERT INTO items(id,resource_id,pile,data) VALUES(?,?,?,?)",
                (item_id, old_id, "club", json.dumps({
                    "id": item_id, "resourceId": old_id, "assetId": old_id,
                    "itemType": "player", "pile": "club",
                })),
            )
            server.STATE.conn.commit()

        server.STATE._enrich_existing_player_items()
        migrated = server.STATE.get_item(item_id)
        self.assertEqual(new_id, migrated["resourceId"])
        self.assertEqual(meta["assetId"], migrated["assetId"])
        self.assertEqual(meta["rating"], migrated["rating"])
        with server.STATE.lock:
            row = server.STATE.conn.execute(
                "SELECT resource_id FROM items WHERE id=?", (item_id,)
            ).fetchone()
        self.assertEqual(new_id, int(row["resource_id"]))

    def test_pack_portrait_prefetch_is_non_blocking_and_resolves_every_card_resource(self) -> None:
        resource_ids = [117637401, 117670070, 67347259]
        requested = []
        finished = threading.Event()

        def fake_cached(resource_id: int):
            time.sleep(0.1)
            requested.append(resource_id)
            if len(requested) == len(resource_ids):
                finished.set()
            return b"DDS " + b"x" * 124

        with patch.dict(server.CFG, {"prefetch_pack_player_images": True}), patch.object(
            server, "_cached_catalog_player_head", side_effect=fake_cached
        ):
            started = time.monotonic()
            server._prime_pack_player_heads([
                {"resourceId": resource_id} for resource_id in resource_ids
            ])
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 0.08)
            self.assertTrue(finished.wait(2.0))

        self.assertEqual(set(resource_ids), set(requested))

    def test_icon_moments_portrait_uses_full_catalog_canvas(self) -> None:
        resource_id = 67351386  # Cruyff Moments -> explicit image key 246516.
        cache_file = server.CONTENT_CACHE / "catalog-playerheads-v12" / f"p{resource_id}.dds"
        cache_file.unlink(missing_ok=True)
        source = server.Image.new("RGBA", (485, 567), (0, 0, 0, 0))
        for x in range(181, 485):
            for y in range(23, 487):
                source.putpixel((x, y), (230, 220, 190, 255))
        png = io.BytesIO()
        source.save(png, format="PNG")
        requested = []

        def fake_download(url: str, **_kwargs):
            requested.append(url)
            return png.getvalue()

        with patch.dict(server.CFG, {"prefer_catalog_player_images": True}), patch.object(
            server, "ROOT", TEST_ROOT / "no-bundled-playerheads"
        ), patch.object(server, "_download_bytes", side_effect=fake_download):
            body = server._cached_catalog_player_head(resource_id)

        self.assertIsNotNone(body)
        self.assertEqual(b"DXT5", body[84:88])
        self.assertEqual(56456, len(body))
        self.assertEqual(56320, int.from_bytes(body[20:24], "little"))
        self.assertEqual(0, int.from_bytes(body[88:92], "little"))
        self.assertEqual(0x1002, int.from_bytes(body[108:112], "little"))
        self.assertEqual(b"\0" * 8, body[-8:])
        with server.Image.open(io.BytesIO(body)) as generated:
            self.assertEqual((220, 256), generated.size)
            self.assertGreaterEqual(generated.convert("RGBA").getchannel("A").getbbox()[0], 80)
        self.assertEqual(1, len(requested))
        self.assertIn("/custom/246516.png", requested[0])

    def test_bundled_portraits_match_native_fifa_dds_contract(self) -> None:
        heads = list((server.ROOT / "playerheads").glob("p*.dds"))
        self.assertEqual(93, len(heads))
        for head in heads:
            body = head.read_bytes()
            self.assertEqual(body, server._native_player_head_dds(body), head.name)
            self.assertEqual(56456, len(body), head.name)

    def test_local_revision_resolves_exact_card_page_portrait(self) -> None:
        resource_id = 33742809
        cache_file = server.CONTENT_CACHE / "catalog-playerheads-v12" / f"p{resource_id}.dds"
        cache_file.unlink(missing_ok=True)
        source = server.Image.new("RGBA", (485, 567), (0, 0, 0, 0))
        source.paste((20, 90, 180, 255), (150, 10, 450, 470))
        png = io.BytesIO()
        source.save(png, format="PNG")
        requested = []

        def fake_download(url: str, **_kwargs):
            requested.append(url)
            if "wefut.com/player/" in url:
                return b'<img src="//static.wefut.com/assets/images/fut20/playeravatars/custom/184737753.png">'
            return png.getvalue()

        with patch.dict(server.CFG, {"prefer_catalog_player_images": True}), patch.object(
            server, "_download_bytes", side_effect=fake_download
        ):
            body = server._cached_catalog_player_head(resource_id)

        self.assertIsNotNone(body)
        self.assertEqual(2, len(requested))
        self.assertIn("/player/20/22628/kyle-walker", requested[0])
        self.assertTrue(requested[1].endswith("/custom/184737753.png"))

    def test_native_square_avatar_is_rejected_for_fifa_native_fallback(self) -> None:
        resource_id = 17015865  # Frick Hero; representative ordinary avatar.
        cache_file = server.CONTENT_CACHE / "catalog-playerheads-v12" / f"p{resource_id}.dds"
        cache_file.unlink(missing_ok=True)
        source = server.Image.new("RGBA", (160, 160), (0, 0, 0, 0))
        source.paste((210, 80, 40, 255), (20, 5, 150, 160))
        png = io.BytesIO()
        source.save(png, format="PNG")

        with patch.dict(server.CFG, {"prefer_catalog_player_images": True}), patch.object(
            server, "_download_bytes", return_value=png.getvalue()
        ), patch.object(
            server, "_catalog_portrait_png_url",
            return_value="https://static.wefut.com/assets/images/fut20/playeravatars/238649.png",
        ):
            body = server._cached_catalog_player_head(resource_id)

        self.assertIsNone(body)
        self.assertFalse(cache_file.exists())

    def test_custom_square_avatar_is_not_used_as_dynamic_replacement(self) -> None:
        source = server.Image.new("RGBA", (512, 512), (25, 120, 210, 255))
        portrait = server._compose_catalog_portrait(source)
        self.assertIsNone(portrait)

    def test_missing_native_player_identity_is_carried_by_item_dto(self) -> None:
        ribery = server.STATE.build_player_item(84042696, 990000910001)
        self.assertEqual("Ribéry", ribery["name"])
        self.assertEqual("Ribéry", ribery["commonname"])
        self.assertEqual("Franck", ribery["firstName"])
        self.assertEqual("1983-04-07", ribery["dateOfBirth"])
        self.assertEqual(5, ribery["skillMoves"])
        native = server._native_player_item(ribery)
        self.assertEqual("Ribéry", native["name"])
        self.assertEqual("Ribéry", native["knownAs"])

        ansu = server.STATE.build_player_item(67361868, 990000910002)
        self.assertEqual("Ansu Fati", ansu["commonname"])
        self.assertEqual("Anssumane", ansu["firstName"])
        self.assertEqual(240654, ansu["assetId"])
        self.assertEqual("RW", ansu["preferredPosition"])

    def test_player_packs_never_repeat_the_same_underlying_player(self) -> None:
        for pack_id in (2, 7, 11, 18):
            pack = server.STATE.create_pack(pack_id)
            asset_ids = [
                server.player_meta(item["resourceId"])["assetId"]
                for item in pack["itemData"]
            ]
            self.assertEqual(len(asset_ids), len(set(asset_ids)), pack_id)
            self.assertTrue(all(
                str(item.get("name") or item.get("commonname") or "").strip()
                for item in pack["itemData"]
            ), pack_id)

    def test_all_adidas_base_cards_use_their_fifa20_clubs(self) -> None:
        expected = {
            158023: (241, 53),  # Messi, Barcelona
            168651: (241, 53),  # Rakitic, Barcelona
            176676: (243, 53),  # Marcelo, Real Madrid
            201942: (9, 13),    # Firmino, Liverpool
            212218: (10, 13),   # Laporte, Manchester City
            230666: (10, 13),   # Gabriel Jesus, Manchester City
        }
        for resource_id, (team_id, league_id) in expected.items():
            meta = server.player_meta(resource_id)
            self.assertEqual(team_id, meta["teamid"], resource_id)
            self.assertEqual(league_id, meta["leagueId"], resource_id)
            item = server.STATE.build_player_item(resource_id, 990000000000 + resource_id)
            self.assertEqual(team_id, item["teamid"], resource_id)
            self.assertEqual(league_id, item["leagueId"], resource_id)

        # Frenkie de Jong was suspected, but his source record is already correct.
        de_jong = server.player_meta(228702)
        self.assertEqual(241, de_jong["teamid"])
        self.assertEqual(53, de_jong["leagueId"])

    def test_zlatan_base_card_uses_la_galaxy(self) -> None:
        meta = server.player_meta(41236)
        self.assertEqual(697, meta["teamid"])
        self.assertEqual(39, meta["leagueId"])
        item = server.STATE.build_player_item(41236, 990000910010)
        self.assertEqual(697, item["teamid"])
        self.assertEqual(39, item["leagueId"])

    def test_mls_all_stars_base_cards_use_their_real_clubs(self) -> None:
        expected = {
            16097: 111065, 41236: 697, 54050: 688, 121944: 693,
            139068: 112606, 164505: 112885, 164559: 111928,
            169416: 112996, 183895: 112828, 190557: 696,
            197225: 111144, 199669: 112885, 199781: 111138,
            203067: 111140, 203895: 111651, 206662: 695,
            216939: 112134, 220209: 689, 226377: 112885,
            236007: 112885, 238049: 695, 238922: 112996,
            241907: 112996,
        }
        for resource_id, team_id in expected.items():
            meta = server.player_meta(resource_id)
            self.assertEqual(team_id, meta["teamid"], resource_id)
            self.assertEqual(39, meta["leagueId"], resource_id)
            self.assertNotEqual("MLS All Stars", meta.get("teamName"), resource_id)

    def test_ansu_fati_carries_name_aliases_that_override_stale_native_db(self) -> None:
        item = server.STATE.build_player_item(67361868, 990000910012)
        for field in (
            "name", "displayName", "playerName", "shortName", "commonName",
            "commonname", "playerCommonName", "knownAs", "knownas",
        ):
            self.assertEqual("Ansu Fati", item[field], field)
        self.assertEqual("Anssumane", item["firstname"])
        self.assertEqual("Fati", item["surname"])

    def test_squad_battle_opponents_have_fut_club_presentation_items(self) -> None:
        expected = {
            1: ("North London XI", 156, "ENGLAND"),
            10: ("Sky Blues United", 246, "ENGLAND"),
            22: ("Yellow Wall FC", 9, "GERMANY"),
            243: ("Royal Champions", 2, "SPAIN"),
        }
        hub = server._native_squad_battle_hub()
        for opponent in hub["squads"]:
            team_id = opponent["teamId"]
            name, stadium_id, location = expected[team_id]
            self.assertEqual(name, opponent["clubName"])
            self.assertEqual(stadium_id, opponent["stadiumId"])
            self.assertEqual(2019, opponent["foundationYear"])
            self.assertGreater(opponent["established"], 1_000_000_000)
            self.assertEqual(location, opponent["location"])
            states = {item["itemState"] for item in opponent["actives"]}
            self.assertEqual(set(server._CLUB_ACTIVE_STATES), states)
            self.assertEqual(team_id, opponent["homeKit"]["teamid"])
            self.assertEqual(team_id, opponent["awayKit"]["teamid"])
            self.assertEqual(team_id, opponent["homeKitAssetId"])
            self.assertEqual(team_id, opponent["awayKitAssetId"])

        summaries = hub["sqbtOppSquadList"]["sqbtOppSquads"]
        for summary in summaries:
            detail = summary["sqbt2data"]
            self.assertEqual(summary["clubName"], detail["clubName"])
            self.assertEqual(summary["location"], detail["location"])
            self.assertEqual(summary["established"], detail["established"])
            self.assertIsInstance(detail["homeKit"], int)
            self.assertIsInstance(detail["awayKit"], int)
            self.assertEqual(summary["homeKitId"], detail["homeKit"])
            self.assertEqual(summary["awayKitId"], detail["awayKit"])
            self.assertEqual(summary["teamId"], detail["homeKitTeamId"])
            self.assertEqual(summary["teamId"], detail["awayKitTeamId"])

    def test_ribery_base_card_carries_all_native_name_aliases(self) -> None:
        item = server.STATE.build_player_item(156616, 990000910011)
        for field in ("name", "displayName", "playerName", "shortName",
                      "commonName", "commonname", "knownAs"):
            self.assertEqual("Ribéry", item[field], field)

    def test_native_fifa20_manager_catalog_has_real_clubs(self) -> None:
        self.assertNotIn(1001067, server.MANAGER_DB)  # Conte is absent in FIFA 20 DB.
        self.assertNotIn(1232425, server.MANAGER_DB)  # Mourinho is absent in FIFA 20 DB.
        team_ids = {int(meta.get("teamid", 0)) for meta in server.MANAGER_DB.values()}
        self.assertGreater(len(team_ids - {0}), 20)
        self.assertEqual(1, server.MANAGER_DB[1237389]["teamid"])

    def test_squad_star_rating_uses_overall_scale(self) -> None:
        draft_opponent = server._draft_opponent_squad(0)
        self.assertEqual(draft_opponent["rating"], draft_opponent["starRating"])
        self.assertGreaterEqual(draft_opponent["starRating"], 80)
        sb_opponent = server._native_squad_battle_opponent(0)
        self.assertEqual(sb_opponent["rating"], sb_opponent["starRating"])
        self.assertGreaterEqual(sb_opponent["starRating"], 60)

    def test_dynamic_head_prefers_exact_catalog_portrait(self) -> None:
        portrait = b"DDS " + b"p" * 124
        with patch.object(server, "_cached_catalog_player_head", return_value=portrait), patch.object(
            server, "_download_fut20_dds"
        ) as fallback:
            response = server._dynamic_fut20_content(
                "/fut/playerheads/g4/single/p184737753.dds"
            )
        self.assertEqual((200, portrait, "application/octet-stream"), response)
        fallback.assert_not_called()

    def test_rejected_catalog_photo_falls_through_to_native_ea_head(self) -> None:
        native = b"DDS " + b"n" * 124
        requested = []

        def fake_ea(_path: str, cache_key: str):
            requested.append(cache_key)
            return native if cache_key.endswith("/p200260.dds") else None

        with patch.object(server, "_cached_catalog_player_head", return_value=None), patch.object(
            server, "_download_fut20_dds", side_effect=fake_ea
        ):
            response = server._dynamic_fut20_content(
                "/fut/playerheads/g4/single/p33754692.dds"
            )

        self.assertEqual((200, native, "application/octet-stream"), response)
        self.assertEqual([
            "/fut/playerheads/g4/single/p33754692.dds",
            "/fut/playerheads/g4/single/p200260.dds",
        ], requested)

    def test_custom_dynamic_head_never_guesses_local_resource_on_ea_cdn(self) -> None:
        native = b"DDS " + b"a" * 124
        requested = []

        def fake_ea(_path: str, cache_key: str):
            requested.append(cache_key)
            return native if cache_key.endswith("/p190717.dds") else None

        with patch.object(server, "_cached_catalog_player_head", return_value=None), patch.object(
            server, "_download_fut20_dds", side_effect=fake_ea
        ):
            response = server._dynamic_fut20_content(
                "/fut/playerheads/g4/single/p67299581.dds"
            )

        self.assertEqual((200, native, "application/octet-stream"), response)
        self.assertEqual(["/fut/playerheads/g4/single/p190717.dds"], requested)

    def test_unique_dynamic_image_id_maps_to_exact_card_portrait(self) -> None:
        portrait = b"DDS " + b"r" * 124
        with patch.object(server, "_cached_catalog_player_head", return_value=portrait) as exact:
            response = server._dynamic_fut20_content(
                "/fut/playerheads/g4/single/p246477.dds"
            )
        self.assertEqual((200, portrait, "application/octet-stream"), response)
        exact.assert_called_once_with(67351814)

    def test_player_head_route_disables_stale_texture_caching(self) -> None:
        portrait = b"DDS " + b"h" * 124
        with patch.object(server, "_dynamic_fut20_content",
                          return_value=(200, portrait, "application/octet-stream")):
            status, headers, body = server.route_fut(
                "GET", "/fut/playerheads/g4/single/p67299581.dds", {}, b""
            )
        self.assertEqual(200, status)
        self.assertEqual(portrait, body)
        self.assertIn("no-store", headers["Cache-Control"])
        self.assertEqual("close", headers["Connection"])

    def test_objectives_campaign_uses_fifa20_native_hierarchy(self) -> None:
        status, campaign = self._response("GET", "/ut/game/fifa20/scmp/campaign/active")
        self.assertEqual(200, status)
        self.assertEqual(30, len(campaign["levels"]))
        self.assertEqual([0, 500, 1000, 1500, 2000], [
            level["xpThreshold"] for level in campaign["levels"][:5]
        ])
        self.assertEqual(14500, campaign["levels"][-1]["xpThreshold"])
        self.assertEqual(0, campaign["userXp"])
        self.assertEqual(1, campaign["remainingRewards"])
        self.assertEqual(1005, campaign["levels"][1]["rewardOptionList"][0]
                         ["awards"][0]["awardValue"])
        categories = campaign["categoriesList"]
        self.assertEqual(1, len(categories))
        groups = categories[0]["groupsList"]
        self.assertEqual(3, len(groups))
        self.assertEqual("IN_PROGRESS", groups[0]["objectives"][0]["state"])
        self.assertEqual("xp", groups[0]["objectives"][0]["awards"][0]["awardType"])
        self.assertEqual("pack", groups[0]["awardsList"][0]["awardType"])
        self.assertEqual(campaign["userXp"], campaign["campaignScore"])
        self.assertEqual(campaign["userXp"], campaign["userXpList"][0]["xp"])
        self.assertEqual(campaign["categoriesList"], campaign["categories"])
        self.assertEqual(1, campaign["scmpCategoryProgressList"][0]["categoryId"])
        self.assertEqual(3, campaign["scmpCategoryProgressList"][0]["groupsCount"])
        self.assertEqual(0, campaign["scmpCategoryProgressList"][0]["groupsCompleted"])
        self.assertEqual(campaign["levels"], campaign["levelsList"])
        self.assertIn("needsGroupsRefresh", campaign)

    def test_objectives_progress_claims_and_rewards_are_idempotent(self) -> None:
        self._response("GET", "/ut/game/fifa20/scmp/campaign/active")
        self.assertTrue(server.STATE.record_global_match("WIN", "generic:objective-test-1"))
        self.assertFalse(server.STATE.record_global_match("WIN", "generic:objective-test-1"))
        status, detail = self._response(
            "GET", "/ut/game/fifa20/scmp/campaign/active/category/1/details?groupIdList=2001"
        )
        self.assertEqual(200, status)
        self.assertEqual(1, detail["groupsList"][0]["objectives"][0]["currentProgress"])

        status, result = self._response(
            "POST", "/ut/game/fifa20/scmp/campaign/group/2001/objective/2101/rewards"
        )
        self.assertEqual(200, status)
        self.assertFalse(result["alreadyClaimed"])
        self.assertEqual(400, result["userXp"])
        status, repeated = self._response(
            "POST", "/ut/game/fifa20/scmp/campaign/group/2001/objective/2101/rewards"
        )
        self.assertEqual(200, status)
        self.assertTrue(repeated["alreadyClaimed"])
        self.assertEqual(400, self._response("GET", "/ut/game/fifa20/scmp/campaign")[1]["userXp"])

        status, locked = self._response(
            "POST", "/ut/game/fifa20/scmp/campaign/level/4/reward/0"
        )
        self.assertEqual(409, status)
        self.assertEqual("LEVEL_LOCKED", locked["reason"])
        status, level = self._response(
            "POST", "/ut/game/fifa20/scmp/campaign/level/1/reward/0"
        )
        self.assertEqual(200, status)
        self.assertEqual(15000, level["credits"])
        self.assertEqual(15000, server.STATE.credits())
        self._response("POST", "/ut/game/fifa20/scmp/campaign/level/1/reward/0")
        self.assertEqual(15000, server.STATE.credits())

        self.assertTrue(server.STATE.record_global_match("LOSS", "draft:objective-test-2"))
        self.assertTrue(server.STATE.record_global_match("DRAW", "generic:objective-test-3"))
        # Draft is intentionally isolated from ordinary FUT Objectives.
        detail = self._response(
            "GET", "/ut/game/fifa20/scmp/campaign/active/category/1/details?groupIdList=2001"
        )[1]
        self.assertEqual(2, detail["groupsList"][0]["objectives"][2]["currentProgress"])
        self.assertTrue(server.STATE.record_global_match("LOSS", "generic:objective-test-4"))
        for objective_id in (2102, 2103):
            status, _ = self._response(
                "POST", f"/ut/game/fifa20/scmp/campaign/group/2001/objective/{objective_id}/rewards"
            )
            self.assertEqual(200, status)
        before_packs = sum(server.STATE.unopened_pack_counts().values())
        status, group = self._response(
            "POST", "/ut/game/fifa20/scmp/campaign/group/2001/rewards"
        )
        self.assertEqual(200, status)
        self.assertEqual("pack", group["awards"][0]["awardType"])
        self.assertEqual(before_packs + 1, sum(server.STATE.unopened_pack_counts().values()))
        self._response("POST", "/ut/game/fifa20/scmp/campaign/group/2001/rewards")
        self.assertEqual(before_packs + 1, sum(server.STATE.unopened_pack_counts().values()))

    def test_every_objectives_level_is_reachable_and_uses_real_pack_ids(self) -> None:
        self._response("GET", "/ut/game/fifa20/scmp/campaign/active")
        for mode in ("squad_battles", "draft", "generic"):
            for index in range(4):
                server.STATE.record_global_match(
                    "WIN" if index == 0 else "LOSS", f"{mode}:season-test-{index}"
                )
        for category in server.SCMP.CATEGORIES:
            for group in category["groups"]:
                for objective in group["objectives"]:
                    status, response = self._response(
                        "POST", f"/ut/game/fifa20/scmp/campaign/group/{group['groupId']}/"
                        f"objective/{objective[0]}/rewards"
                    )
                    self.assertEqual(200, status)
                    self.assertEqual(objective[0], response["objectiveId"])
        campaign = self._response("GET", "/ut/game/fifa20/scmp/campaign")[1]
        self.assertEqual(5550, campaign["userXp"])
        for index in range(63):
            self.assertTrue(server.STATE.record_global_match(
                "WIN", f"squad_battles:season-grind-{index}"))
        campaign = self._response("GET", "/ut/game/fifa20/scmp/campaign")[1]
        self.assertEqual(15000, campaign["userXp"])
        self.assertGreaterEqual(campaign["userXp"], campaign["levels"][-1]["xpThreshold"])
        before_packs = sum(server.STATE.unopened_pack_counts().values())
        status, response = self._response(
            "POST", "/ut/game/fifa20/scmp/campaign/level/30/reward/0"
        )
        self.assertEqual(200, status)
        self.assertEqual("pack", response["awardedPrizes"][0]["awardType"])
        self.assertEqual(3, response["awardedPrizes"][0]["awardCount"])
        self.assertEqual(before_packs + 3, sum(server.STATE.unopened_pack_counts().values()))
        for reward in server.SCMP.LEVEL_REWARDS:
            if reward[0] == "pack":
                self.assertTrue(server.get_pack_cfg(reward[1]))

    def test_settled_match_xp_is_deduplicated_and_quits_do_not_earn_xp(self) -> None:
        self._response("GET", "/ut/game/fifa20/scmp/campaign/active")
        self.assertTrue(server.STATE.record_global_match(
            "WIN", "squad_battles:xp-1"))
        self.assertFalse(server.STATE.record_global_match(
            "WIN", "squad_battles:xp-1"))
        self.assertTrue(server.STATE.record_global_match(
            "LOSS", "draft:quit-1", xp_eligible=False))
        self.assertTrue(server.STATE.record_global_match(
            "DRAW", "rivals:xp-2"))
        campaign = self._response("GET", "/ut/game/fifa20/scmp/campaign/active")[1]
        self.assertEqual(300, campaign["userXp"])

    def test_match_end_and_user_payload_refresh_objectives_without_restarting(self) -> None:
        self._response("GET", "/ut/game/fifa20/scmp/campaign/active")
        self.assertTrue(server.STATE.record_global_match("WIN", "generic:live-objectives"))
        campaign = server._scmp_campaign()
        live = server._with_scmp_live({"success": True})
        self.assertEqual(150, live["userXp"])
        self.assertEqual(1, live["objectivesRevision"])
        self.assertEqual(campaign, live["scmpCampaign"])
        self.assertEqual(1, campaign["categoriesList"][0]["groupsList"][0]
                         ["objectives"][0]["currentProgress"])
        user = server._native_user()
        self.assertEqual(150, user["userXp"])
        self.assertEqual(1, user["objectivesRevision"])
        self.assertEqual(campaign, user["seasonalCampaign"])

        # FIFA's observed post-match sequence refreshes /user and then /hub,
        # not /scmp/campaign/active.  Both responses must therefore expose the
        # new revision directly so the native Objectives model has a live
        # invalidation path without leaving Ultimate Team.
        status, headers, body = server.route_fut(
            "GET", "/ut/game/fifa20/user", {}, b""
        )
        refreshed_user = json.loads(body.decode("utf-8"))
        self.assertEqual(200, status)
        self.assertIn("no-store", headers["Cache-Control"])
        self.assertEqual(1, refreshed_user["objectivesRevision"])

        status, headers, body = server.route_fut(
            "GET", "/ut/game/fifa20/hub?screen=futlivemsggen4", {}, b""
        )
        hub = json.loads(body.decode("utf-8"))
        self.assertEqual(200, status)
        self.assertIn("no-store", headers["Cache-Control"])
        self.assertEqual(1, hub["objectivesRevision"])
        self.assertEqual(1, hub["scmpCampaign"]["categoriesList"][0]
                         ["groupsList"][0]["objectives"][0]["currentProgress"])

        mass = server.user_mass_info()
        self.assertEqual(1, mass["objectivesRevision"])
        self.assertEqual(campaign["campaignId"], mass["scmpCampaign"]["campaignId"])

    def test_post_match_details_can_replace_stale_active_campaign_model(self) -> None:
        # Authentication loads revision 0 as the native active campaign.
        status, initial = self._response("GET", "/ut/game/fifa20/scmp/campaign/active")
        self.assertEqual(200, status)
        self.assertEqual(0, initial["revision"])
        self.assertEqual(0, initial["categoriesList"][0]["groupsList"][2]
                         ["objectives"][0]["currentProgress"])

        # A settled Draft result advances progress and marks that active model
        # stale without logging the user out of FUT.
        self.assertTrue(server.STATE.record_global_match(
            "LOSS", "draft:live-full-campaign-test", xp_eligible=False
        ))
        status, headers, body = server.route_fut(
            "GET",
            "/ut/game/fifa20/scmp/learning/category/1/details?groupIdList=2003,2002,2001",
            {}, b"",
        )
        self.assertEqual(200, status)
        self.assertIn("no-store", headers["Cache-Control"])
        self.assertEqual('"localfut20-scmp-r1"', headers["ETag"])
        detail = json.loads(body.decode("utf-8"))
        self.assertEqual(1, detail["revision"])
        self.assertIn("categoriesList", detail)
        self.assertIn("levels", detail)
        self.assertIn("activeCampaign", detail)
        draft = next(group for group in detail["groupsList"] if group["groupId"] == 2003)
        self.assertEqual(1, draft["objectives"][0]["currentProgress"])
        self.assertEqual("COMPLETED", draft["objectives"][0]["state"])
        campaign_draft = detail["categoriesList"][0]["groupsList"][2]
        self.assertEqual(1, campaign_draft["objectives"][0]["currentProgress"])

        # Once FIFA explicitly fetches /campaign/active at this revision, the
        # normal compact category-details envelope is enough again.
        self._response("GET", "/ut/game/fifa20/scmp/campaign/active")
        status, _, body = server.route_fut(
            "GET",
            "/ut/game/fifa20/scmp/learning/category/1/details?groupIdList=2003",
            {}, b"",
        )
        detail_after_active = json.loads(body.decode("utf-8"))
        self.assertEqual(200, status)
        self.assertNotIn("categoriesList", detail_after_active)
        self.assertEqual(1, detail_after_active["groupsList"][0]
                         ["objectives"][0]["currentProgress"])

        status, teardown = self._response("POST", "/ut/delete/auth")
        self.assertEqual(200, status)
        self.assertTrue(teardown["success"])

    def test_manual_objective_claim_invalidates_loaded_campaign_immediately(self) -> None:
        # Entering Objectives establishes the campaign revision held by FIFA's
        # long-lived SCMP frontend model.
        _, initial = self._response("GET", "/ut/game/fifa20/scmp/campaign/active")
        self.assertEqual(0, initial["revision"])
        self.assertTrue(server.STATE.record_global_match(
            "WIN", "generic:claim-live-refresh"
        ))
        # Simulate the post-match active fetch that made progress visible before
        # the user presses Claim.  The claim itself must create a second stale
        # revision, not rely on leaving and re-entering Objectives.
        self._response("GET", "/ut/game/fifa20/scmp/campaign/active")
        status, headers, body = server.route_fut(
            "POST",
            "/ut/game/fifa20/scmp/campaign/group/2001/objective/2101/rewards",
            {}, b"{}",
        )
        self.assertEqual(200, status)
        self.assertIn("no-store", headers["Cache-Control"])
        self.assertNotIn("ETag", headers)
        claimed = json.loads(body.decode("utf-8"))
        self.assertEqual(2, claimed["objectivesRevision"])
        self.assertNotIn("categoriesList", claimed)
        objective = claimed["scmpCampaign"]["categoriesList"][0]["groupsList"][0]["objectives"][0]
        self.assertEqual("REDEEMED", objective["state"])

        status, headers, body = server.route_fut(
            "GET",
            "/ut/game/fifa20/scmp/learning/category/1/details?groupIdList=2001",
            {}, b"",
        )
        self.assertEqual(200, status)
        self.assertEqual('"localfut20-scmp-r2"', headers["ETag"])
        detail = json.loads(body.decode("utf-8"))
        self.assertEqual("REDEEMED", detail["groupsList"][0]["objectives"][0]["state"])

    def test_claimed_season_reward_survives_state_reload_and_cannot_repeat(self) -> None:
        self._response("GET", "/ut/game/fifa20/scmp/campaign/active")
        status, claimed = self._response(
            "POST", "/ut/game/fifa20/scmp/campaign/level/1/reward/0"
        )
        self.assertEqual(200, status)
        self.assertFalse(claimed["alreadyClaimed"])
        self.assertEqual(15000, server.STATE.credits())

        reloaded = server.State(server.DB_PATH)
        try:
            with patch.object(server, "STATE", reloaded):
                campaign = server._scmp_campaign()
                level_one = next(level for level in campaign["levels"] if level["levelId"] == 1)
                self.assertEqual(0, level_one["chosenOption"])
                status, duplicate = server._scmp_claim("levels", 1, 0)
                self.assertEqual(200, status)
                self.assertTrue(duplicate["alreadyClaimed"])
                self.assertEqual(15000, reloaded.credits())
        finally:
            reloaded.conn.close()

    def test_sbc_set_badges_are_portrait_pngs_with_a_fresh_cache_key(self) -> None:
        sets = self._response("GET", "/ut/game/fifa20/sbs/sets")[1]
        summaries = sets.get("sets") or [
            summary for category in sets["categories"] for summary in category["sets"]
        ]
        self.assertEqual(20, len(summaries))
        dimensions = {}
        for summary in summaries:
            image_id = summary["setImageId"]
            self.assertTrue(image_id.endswith("-localfut20-sbc-v9"))
            status, headers, body = server.route_fut(
                "GET", f"/fut/sbc/gen4/sets/images/sbc_set_image_{image_id}.png",
                {}, b"",
            )
            self.assertEqual(200, status)
            self.assertEqual("image/png", headers["Content-Type"])
            self.assertGreater(len(body), 10000)
            self.assertEqual(b"\x89PNG\r\n\x1a\n", body[:8])
            width, height = struct.unpack(">II", body[16:24])
            self.assertEqual(375, width)
            self.assertEqual(520, height)
            alpha_bbox = Image.open(io.BytesIO(body)).convert("RGBA").getchannel("A").getbbox()
            self.assertIsNotNone(alpha_bbox)
            # v9 trims every source PNG to its visible alpha before scaling.
            # That removes inconsistent source transparency which made player
            # and upgrade SBC badges tiny.  The visible art is now a consistent
            # 149..220px wide / 209..225px tall and bottom-aligned on the same
            # taller canvas which lowers the crest to the retail shadow line.
            visible_width = alpha_bbox[2] - alpha_bbox[0]
            visible_height = alpha_bbox[3] - alpha_bbox[1]
            self.assertGreaterEqual(visible_width, 149)
            self.assertLessEqual(visible_width, 220)
            self.assertGreaterEqual(visible_height, 209)
            self.assertLessEqual(visible_height, 225)
            self.assertGreaterEqual(alpha_bbox[0], 151)
            self.assertLessEqual(alpha_bbox[0], 222)
            self.assertGreaterEqual(alpha_bbox[1], 287)
            self.assertLessEqual(alpha_bbox[1], 303)
            self.assertLessEqual(alpha_bbox[2], 371)
            self.assertLessEqual(alpha_bbox[3], 512)
            dimensions[int(summary["setId"])] = (width, height)
        self.assertEqual((375, 520), dimensions[1])
        self.assertEqual((375, 520), dimensions[20])
        self.assertEqual((375, 520), dimensions[532])
        self.assertEqual((375, 520), dimensions[542])

    def test_totw_history_is_native_id_wire_shape_not_nested_squads(self) -> None:
        status, history = self._response(
            "GET", "/ut/game/fifa20/featuredsquad/history?featureConsumerId=sqbttotw"
        )
        self.assertEqual(200, status)
        self.assertEqual({"squads"}, set(history))
        # FIFA appends two native live/current cards after this history.
        # Returning only 1..43 here produces the real visible 45-week selector
        # instead of duplicate WEEK 46/47 cards for the latest squad.
        self.assertEqual(43, len(history["squads"]))
        self.assertEqual(43, history["squads"][0])
        self.assertEqual(1, history["squads"][-1])
        self.assertEqual(list(range(43, 0, -1)), history["squads"])
        self.assertTrue(set(range(33, 38)) <= set(history["squads"]))
        self.assertTrue(all(isinstance(squad_id, int) for squad_id in history["squads"]))

    def test_totw_gap_weeks_preserve_selector_numbering_without_fake_squads(self) -> None:
        for week in range(33, 38):
            status, gap = self._response(
                "GET", f"/ut/game/fifa20/featuredsquad/{week}?featureConsumerId=sqbttotw"
            )
            self.assertEqual(200, status)
            self.assertEqual(week, gap["featuredSquadId"])
            self.assertEqual(week, gap["actualWeek"])
            self.assertEqual(week, gap["archiveIndex"])
            self.assertEqual("no_release", gap["releaseType"])
            self.assertEqual([], gap["players"])
            self.assertFalse(gap["active"])
            self.assertFalse(gap["valid"])
            self.assertFalse(gap["available"])
            self.assertTrue(gap["selectorPlaceholder"])
            self.assertTrue(gap["noTeamOfTheWeekRelease"])

    def test_resumed_totw_selector_slot_matches_real_week(self) -> None:
        _, week_40 = self._response(
            "GET", "/ut/game/fifa20/featuredsquad/40?featureConsumerId=sqbttotw"
        )
        self.assertEqual(40, week_40["archiveIndex"])
        self.assertEqual(35, week_40["publishedIndex"])
        self.assertEqual(40, week_40["actualWeek"])
        self.assertEqual(40, week_40["realTotwNumber"])
        self.assertEqual("WEEK 40", week_40["displayLabel"])
        self.assertEqual("FIFA 20 Team of the Week 40", week_40["name"])

    def test_all_published_totw_squads_have_real_players_and_native_details(self) -> None:
        self.assertEqual(40, len(server._TOTW_SQUADS))
        self.assertEqual(904, sum(len(entry["resourceIds"]) for entry in server._TOTW_SQUADS))
        for definition in server._TOTW_SQUADS:
            squad_id = int(definition["id"])
            status, squad = self._response(
                "GET", f"/ut/game/fifa20/featuredsquad/{squad_id}?featureConsumerId=sqbttotw"
            )
            self.assertEqual(200, status)
            self.assertEqual(squad_id, squad["featuredSquadId"])
            self.assertEqual("sqbttotw", squad["featureConsumerId"])
            players = squad["players"]
            self.assertGreaterEqual(len(players), 18)
            self.assertEqual(len(players), len({entry["itemData"]["resourceId"] for entry in players}))
            self.assertEqual(100, squad["chemistry"])
            self.assertEqual(definition["formation"], squad["formation"])
            self.assertEqual(definition["startingResourceIds"], [
                entry["itemData"]["resourceId"] for entry in players[:11]
            ])
            self.assertEqual(definition["resourceIds"], [
                entry["itemData"]["resourceId"] for entry in players
            ])

    def test_totw_known_week_cards_and_zero_alias(self) -> None:
        status, week_one = self._response(
            "GET", "/ut/game/fifa20/featuredsquad/1?featureConsumerId=sqbttotw"
        )
        self.assertEqual(200, status)
        players = week_one["players"]
        self.assertEqual(23, len(players))
        self.assertEqual(23, len({entry["itemData"]["resourceId"] for entry in players}))
        self.assertIn(67276359, {entry["itemData"]["resourceId"] for entry in players})

        _, week_twenty = self._response(
            "GET", "/ut/game/fifa20/featuredsquad/20?featureConsumerId=sqbttotw"
        )
        self.assertTrue(any(
            entry["itemData"].get("rating") == 87
            and "donnarumma" in str(entry["itemData"].get("lastName", "")).lower()
            for entry in week_twenty["players"]
        ))
        _, week_43 = self._response(
            "GET", "/ut/game/fifa20/featuredsquad/43?featureConsumerId=sqbttotw"
        )
        cards = {(entry["itemData"].get("assetId"), entry["itemData"].get("rating"))
                 for entry in week_43["players"]}
        self.assertIn((231281, 95), cards)  # Alexander-Arnold
        self.assertIn((192985, 98), cards)  # De Bruyne
        _, week_44 = self._response(
            "GET", "/ut/game/fifa20/featuredsquad/44?featureConsumerId=sqbttotw"
        )
        self.assertTrue({33763763, 100843699, 16966906} <= {
            entry["itemData"]["resourceId"] for entry in week_44["players"]
        })
        _, week_45 = self._response(
            "GET", "/ut/game/fifa20/featuredsquad/45?featureConsumerId=sqbttotw"
        )
        self.assertTrue({67310990, 33754536, 50537761} <= {
            entry["itemData"]["resourceId"] for entry in week_45["players"]
        })
        _, alias = self._response(
            "GET", "/ut/game/fifa20/featuredsquad/0?featureConsumerId=sqbttotw"
        )
        self.assertEqual(44, alias["featuredSquadId"])

        # Stale 46/47 requests must not alias the latest Week 45 squad.
        for stale_week in (46, 47):
            _, stale = self._response(
                "GET", f"/ut/game/fifa20/featuredsquad/{stale_week}?featureConsumerId=sqbttotw"
            )
            self.assertEqual(stale_week, stale["featuredSquadId"])
            self.assertEqual("no_release", stale["releaseType"])
            self.assertEqual([], stale["players"])
            self.assertFalse(stale["available"])

        status, stats = self._response(
            "GET", "/ut/game/fifa20/featuredsquad/user/stats?featureConsumerId=sqbttotw"
        )
        self.assertEqual(200, status)
        self.assertTrue(stats["featuredSquadAvailable"])
        self.assertEqual(45, stats["featuredSquadId"])


if __name__ == "__main__":
    unittest.main()
