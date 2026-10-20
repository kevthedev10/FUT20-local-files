"""Source-verified FIFA 20 Squad Building Challenge catalogue.

Only challenges whose original requirements and rewards are available in the
bundled reference are exposed.  In particular, this module must never fill an
archive gap with a made-up promo SBC or reward.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def pack(pack_id: int, count: int = 1) -> dict[str, Any]:
    return {"type": "PACK", "pack_id": int(pack_id), "count": int(count)}


def coins(amount: int) -> dict[str, Any]:
    return {"type": "COINS", "value": int(amount), "count": 1}


def player(resource_id: int, count: int = 1, loan_games: int = 0) -> dict[str, Any]:
    """A specific FIFA 20 player-card reward, optionally as a timed loan."""
    return {
        "type": "PLAYER",
        "resource_id": int(resource_id),
        "count": max(1, int(count)),
        "loan_games": max(0, int(loan_games)),
    }


def player_pick(pool: str, options: int = 3, min_rating: int = 0,
                count: int = 1, title: str = "") -> dict[str, Any]:
    """An untradeable choice reward generated when the SBC is completed."""
    award = {
        "type": "PLAYER_PICK",
        "pool": str(pool),
        "options": max(2, min(5, int(options))),
        "min_rating": max(0, int(min_rating)),
        "count": max(1, int(count)),
    }
    if title:
        award["title"] = str(title)
        award["displayName"] = str(title)
    return award


def rule(kind: str, value: int, scope: str = "min", count: int = -1, text: str = "") -> dict[str, Any]:
    return {
        "kind": str(kind),
        "value": int(value),
        "scope": str(scope),
        "count": int(count),
        "text": str(text),
    }


def challenge(
    challenge_id: int,
    name: str,
    description: str,
    players: int,
    rules: list[dict[str, Any]],
    awards: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "challenge_id": int(challenge_id),
        "name": str(name),
        "description": str(description),
        "players": int(players),
        "formation": "f442",
        "rules": [rule("player_count", players, "exact", text=f"Players in the Squad: {players}"), *rules],
        "awards": awards,
    }


SBC_CATALOG: list[dict[str, Any]] = [
    {
        "set_id": 1, "category_id": 1, "category": "BASIC", "priority": 1,
        "name": "Let's Get Started", "description": "Learn the basics of exchanging players for rewards.",
        "repeatable": False, "awards": [pack(1010)],
        "challenges": [
            challenge(101, "First Exchange", "Exchange one Bronze player from your Club.", 1, [
                rule("quality", 1, "exact", -1, "Player Quality: Exactly Bronze"),
            ], [pack(1024)]),
            challenge(102, "The Second Step", "Exchange three Common players.", 3, [
                rule("rare_count", 0, "exact", -1, "Common Players: Exactly 3"),
                rule("chemistry", 12, "min", -1, "Team Chemistry: Min. 12"),
            ], [pack(1001)]),
            challenge(103, "The Third Step", "Exchange four players from three different nations.", 4, [
                rule("nation_count", 3, "exact", -1, "Nationalities: Exactly 3"),
                rule("chemistry", 17, "min", -1, "Team Chemistry: Min. 17"),
            ], [pack(1002)]),
        ],
    },
    {
        "set_id": 2, "category_id": 1, "category": "BASIC", "priority": 2,
        "name": "League and Nation Basics", "description": "Build squads with basic League and Nation requirements.",
        "repeatable": False, "awards": [pack(1004)],
        "challenges": [
            challenge(201, "One League Attack", "Use players from one league and several nations.", 7, [
                rule("league_count", 1, "exact", -1, "Leagues: Exactly 1"),
                rule("nation_count", 5, "exact", -1, "Nationalities: Exactly 5"),
                rule("chemistry", 45, "min", -1, "Team Chemistry: Min. 45"),
            ], [pack(1014)]),
            challenge(202, "One Nation Midfield", "Use players from one nation and several leagues.", 5, [
                rule("quality", 2, "min", 3, "Silver Players: Min. 3"),
                rule("league_count", 5, "exact", -1, "Leagues: Exactly 5"),
                rule("nation_count", 1, "exact", -1, "Nationalities: Exactly 1"),
                rule("chemistry", 45, "min", -1, "Team Chemistry: Min. 45"),
            ], [pack(1005)]),
            challenge(203, "Multi League & Nation", "Use chemistry links across clubs, leagues and nations.", 6, [
                rule("club_count", 6, "exact", -1, "Clubs: Exactly 6"),
                rule("same_league", 2, "max", -1, "Players from the same League: Max. 2"),
                rule("same_nation", 2, "max", -1, "Players from the same Nation: Max. 2"),
                rule("team_rating", 55, "min", -1, "Team Rating: Min. 55"),
                rule("chemistry", 45, "min", -1, "Team Chemistry: Min. 45"),
            ], [pack(1005)]),
        ],
    },
    {
        "set_id": 10, "category_id": 1, "category": "BASIC", "priority": 3,
        "name": "Let's Keep Going", "description": "Learn more Squad Building techniques for a Premium Gold Players Pack.",
        "repeatable": False, "awards": [pack(1019)],
        "challenges": [
            challenge(1001, "Brick Slots", "Use the open slots to build chemistry.", 2, [
                rule("league_count", 2, "exact", -1, "Leagues: Exactly 2"),
                rule("nation_count", 2, "exact", -1, "Nationalities: Exactly 2"),
                rule("chemistry", 43, "min", -1, "Team Chemistry: Min. 43"),
            ], [pack(1001)]),
            challenge(1002, "The Correct Position", "Place players in their correct positions.", 3, [
                rule("league_count", 3, "exact", -1, "Leagues: Exactly 3"),
                rule("nation_count", 3, "exact", -1, "Nationalities: Exactly 3"),
                rule("chemistry", 12, "min", -1, "Team Chemistry: Min. 12"),
            ], [pack(1018)]),
            challenge(1003, "Perfect Link", "Use strong club, league and nation links.", 5, [
                rule("quality", 2, "min", 2, "Silver Players: Min. 2"),
                rule("same_club", 2, "max", -1, "Players from the same Club: Max. 2"),
                rule("same_nation", 2, "max", -1, "Players from the same Nation: Max. 2"),
                rule("chemistry", 40, "min", -1, "Team Chemistry: Min. 40"),
            ], [coins(500), pack(1002)]),
            challenge(1004, "Loyal Lads", "Use loyalty to reach the chemistry target.", 3, [
                rule("league_count", 3, "exact", -1, "Leagues: Exactly 3"),
                rule("nation_count", 1, "exact", -1, "Nationalities: Exactly 1"),
                rule("chemistry", 30, "min", -1, "Team Chemistry: Min. 30"),
            ], [pack(1002, 2)]),
        ],
    },
    {
        "set_id": 11, "category_id": 1, "category": "BASIC", "priority": 4,
        "name": "Let's Move Forward", "description": "Exchange Bronze players to improve your starter squad.",
        "repeatable": False, "awards": [pack(1023)],
        "challenges": [
            challenge(1101, "Defensive Development", "Exchange Bronze players for a Gold defender reward.", 2, [
                rule("quality", 1, "exact", -1, "Player Quality: Exactly Bronze"),
                rule("chemistry", 8, "min", -1, "Team Chemistry: Min. 8"),
            ], [pack(1020)]),
            challenge(1102, "A Better Buildup", "Exchange Bronze players for a Gold midfielder reward.", 3, [
                rule("quality", 1, "exact", -1, "Player Quality: Exactly Bronze"),
                rule("chemistry", 8, "min", -1, "Team Chemistry: Min. 8"),
            ], [pack(1021)]),
            challenge(1103, "Advancing Attack", "Exchange Bronze players for a Gold attacker reward.", 3, [
                rule("quality", 1, "exact", -1, "Player Quality: Exactly Bronze"),
                rule("chemistry", 9, "min", -1, "Team Chemistry: Min. 9"),
            ], [pack(1022)]),
        ],
    },
    {
        "set_id": 20, "category_id": 2, "category": "ADVANCED", "priority": 1,
        "name": "Hybrid Leagues", "description": "Solve four escalating league puzzles for a Rare Mega Pack.",
        "repeatable": False, "awards": [pack(1009)],
        "challenges": [
            challenge(2001, "Rare Fives", "Exchange a squad featuring exactly five leagues.", 11, [
                rule("rare_count", 5, "min", -1, "Rare Players: Min. 5"),
                rule("league_count", 5, "exact", -1, "Leagues: Exactly 5"),
                rule("same_league", 3, "max", -1, "Players from the same League: Max. 3"),
                rule("team_rating", 68, "min", -1, "Team Rating: Min. 68"),
                rule("chemistry", 80, "min", -1, "Team Chemistry: Min. 80"),
            ], [pack(1015)]),
            challenge(2002, "Seven Suspects", "Exchange a squad featuring exactly seven leagues.", 11, [
                rule("league_count", 7, "exact", -1, "Leagues: Exactly 7"),
                rule("same_league", 3, "max", -1, "Players from the same League: Max. 3"),
                rule("team_rating", 78, "min", -1, "Team Rating: Min. 78"),
                rule("chemistry", 85, "min", -1, "Team Chemistry: Min. 85"),
            ], [pack(1016)]),
            challenge(2003, "Prime Nine", "Exchange a squad featuring exactly nine leagues.", 11, [
                rule("rare_count", 5, "min", -1, "Rare Players: Min. 5"),
                rule("league_count", 9, "exact", -1, "Leagues: Exactly 9"),
                rule("same_league", 2, "max", -1, "Players from the same League: Max. 2"),
                rule("team_rating", 79, "min", -1, "Team Rating: Min. 79"),
                rule("chemistry", 99, "min", -1, "Team Chemistry: Min. 99"),
            ], [pack(1006)]),
            challenge(2004, "Squad Goals", "Exchange an all-Gold squad featuring eleven leagues.", 11, [
                rule("rare_count", 6, "min", -1, "Rare Players: Min. 6"),
                rule("quality", 3, "exact", -1, "Player Quality: Exactly Gold"),
                rule("league_count", 11, "exact", -1, "Leagues: Exactly 11"),
                rule("chemistry", 100, "exact", -1, "Team Chemistry: Exactly 100"),
            ], [pack(1008)]),
        ],
    },
    {
        "set_id": 21, "category_id": 2, "category": "ADVANCED", "priority": 2,
        "name": "Hybrid Nations", "description": "Solve four escalating nation puzzles for a Rare Mega Pack.",
        "repeatable": False, "awards": [pack(1009)],
        "challenges": [
            challenge(2101, "Quads", "Exchange a squad featuring exactly four nations.", 11, [
                rule("rare_count", 4, "min", -1, "Rare Players: Min. 4"),
                rule("nation_count", 4, "exact", -1, "Nationalities: Exactly 4"),
                rule("same_nation", 3, "max", -1, "Players from the same Nation: Max. 3"),
                rule("team_rating", 75, "min", -1, "Team Rating: Min. 75"),
                rule("chemistry", 80, "min", -1, "Team Chemistry: Min. 80"),
            ], [pack(1013)]),
            challenge(2102, "The Six", "Exchange a squad featuring exactly six nations.", 11, [
                rule("nation_count", 6, "exact", -1, "Nationalities: Exactly 6"),
                rule("same_nation", 2, "max", -1, "Players from the same Nation: Max. 2"),
                rule("team_rating", 70, "min", -1, "Team Rating: Min. 70"),
                rule("chemistry", 85, "min", -1, "Team Chemistry: Min. 85"),
            ], [pack(1005)]),
            challenge(2103, "It Takes Eight", "Exchange an all-Gold squad featuring eight nations.", 11, [
                rule("quality", 3, "exact", -1, "Player Quality: Exactly Gold"),
                rule("rare_count", 5, "min", -1, "Rare Players: Min. 5"),
                rule("nation_count", 8, "exact", -1, "Nationalities: Exactly 8"),
                rule("same_nation", 2, "max", -1, "Players from the same Nation: Max. 2"),
                rule("chemistry", 88, "min", -1, "Team Chemistry: Min. 88"),
            ], [pack(1006)]),
            challenge(2104, "National Pride", "Exchange a squad featuring ten nations.", 11, [
                rule("rare_count", 7, "min", -1, "Rare Players: Min. 7"),
                rule("nation_count", 10, "exact", -1, "Nationalities: Exactly 10"),
                rule("team_rating", 80, "min", -1, "Team Rating: Min. 80"),
                rule("chemistry", 100, "exact", -1, "Team Chemistry: Exactly 100"),
            ], [pack(1008)]),
        ],
    },
    {
        "set_id": 22, "category_id": 2, "category": "ADVANCED", "priority": 3,
        "name": "League and Nation Hybrid", "description": "Master mixed league-and-nation squads for 12,000 coins.",
        "repeatable": False, "awards": [coins(12000)],
        "challenges": [
            challenge(2201, "The Puzzler", "Build a squad with three nations and two leagues.", 11, [
                rule("league_count", 2, "exact", -1, "Leagues: Exactly 2"),
                rule("same_league", 6, "max", -1, "Players from the same League: Max. 6"),
                rule("nation_count", 3, "exact", -1, "Nationalities: Exactly 3"),
                rule("same_nation", 4, "max", -1, "Players from the same Nation: Max. 4"),
                rule("team_rating", 77, "min", -1, "Team Rating: Min. 77"),
                rule("chemistry", 100, "min", -1, "Team Chemistry: Min. 100"),
            ], [pack(1013)]),
            challenge(2202, "Intermediate", "Build a squad with five nations and three leagues.", 11, [
                rule("league_count", 3, "exact", -1, "Leagues: Exactly 3"),
                rule("same_league", 4, "max", -1, "Players from the same League: Max. 4"),
                rule("nation_count", 5, "exact", -1, "Nationalities: Exactly 5"),
                rule("same_nation", 3, "max", -1, "Players from the same Nation: Max. 3"),
                rule("team_rating", 78, "min", -1, "Team Rating: Min. 78"),
                rule("chemistry", 100, "min", -1, "Team Chemistry: Min. 100"),
            ], [coins(1500), pack(1017)]),
            challenge(2203, "Tough", "Build a squad with six nations and six leagues.", 11, [
                rule("league_count", 6, "exact", -1, "Leagues: Exactly 6"),
                rule("same_league", 2, "max", -1, "Players from the same League: Max. 2"),
                rule("nation_count", 6, "exact", -1, "Nationalities: Exactly 6"),
                rule("same_nation", 2, "max", -1, "Players from the same Nation: Max. 2"),
                rule("team_rating", 80, "min", -1, "Team Rating: Min. 80"),
                rule("chemistry", 89, "min", -1, "Team Chemistry: Min. 89"),
            ], [pack(1008)]),
            challenge(2204, "Hybrid Master", "Build a squad with nine nations and seven leagues.", 11, [
                rule("league_count", 7, "exact", -1, "Leagues: Exactly 7"),
                rule("nation_count", 9, "exact", -1, "Nationalities: Exactly 9"),
                rule("team_rating", 82, "min", -1, "Team Rating: Min. 82"),
                rule("chemistry", 86, "min", -1, "Team Chemistry: Min. 86"),
            ], [pack(1007)]),
        ],
    },
    {
        "set_id": 30, "category_id": 3, "category": "UPGRADES", "priority": 1,
        "name": "Bronze Upgrade", "description": "Exchange Bronze players for Two Silver Players.",
        "repeatable": True, "awards": [pack(1012)],
        "challenges": [challenge(3001, "Bronze Upgrade", "Exchange eleven Bronze players.", 11, [
            rule("quality", 1, "exact", -1, "Player Quality: Exactly Bronze"),
        ], [])],
    },
    {
        "set_id": 31, "category_id": 3, "category": "UPGRADES", "priority": 2,
        "name": "Silver Upgrade", "description": "Exchange Silver players for Three Common Gold Players.",
        "repeatable": True, "awards": [pack(1011)],
        "challenges": [challenge(3101, "Silver Upgrade", "Exchange eleven Silver players.", 11, [
            rule("quality", 2, "exact", -1, "Player Quality: Exactly Silver"),
        ], [])],
    },
    {
        "set_id": 32, "category_id": 3, "category": "UPGRADES", "priority": 3,
        "name": "Gold Upgrade", "description": "Exchange Gold players for Two Rare Gold Players.",
        "repeatable": True, "awards": [pack(1010)],
        "challenges": [challenge(3201, "Gold Upgrade", "Exchange eleven Gold players.", 11, [
            rule("quality", 3, "exact", -1, "Player Quality: Exactly Gold"),
        ], [])],
    },
    {
        "set_id": 532, "category_id": 4, "category": "PLAYERS", "priority": 1,
        "name": "Flashback Ricardo Quaresma",
        "description": "Earn a Flashback Ricardo Quaresma celebrating his 2007/2008 Season.",
        "repeatable": False, "awards": [player(67129639)],
        "challenges": [challenge(
            100006, "Flashback Ricardo Quaresma",
            "Exchange an 85-rated squad featuring Portugal and two TOTW players.", 11, [
                rule("specific_nation", 38, "min", 1, "Players from Portugal: Min. 1"),
                rule("card_type_count", 3, "min", 2, "TOTW Players: Min. 2"),
                rule("team_rating", 85, "min", -1, "Squad Rating: Min. 85"),
                rule("chemistry", 75, "min", -1, "Team Chemistry: Min. 75"),
            ], []
        )],
    },
    {
        "set_id": 542, "category_id": 5, "category": "LIVE", "priority": 1,
        "name": "Four Leaf Clover",
        "description": "Solve these St. Patrick's Day themed puzzles and earn great rewards.",
        "repeatable": False, "awards": [pack(1009)],
        "challenges": [
            challenge(54201, "The Shamrock", "Build around a Shamrock Rovers player.", 11, [
                rule("specific_club", 306, "min", 1, "Players from Shamrock Rovers: Min. 1"),
                rule("club_count", 11, "min", -1, "Clubs: Min. 11"),
                rule("rare_count", 3, "min", -1, "Rare Players: Min. 3"),
                rule("same_nation", 3, "max", -1, "Players from the same Nation: Max. 3"),
                rule("same_league", 3, "max", -1, "Players from the same League: Max. 3"),
                rule("chemistry", 80, "min", -1, "Team Chemistry: Min. 80"),
            ], [pack(1004)]),
            challenge(54202, "The Green Nations", "Use players from the Republic of Ireland and Northern Ireland.", 11, [
                # Both nations must appear and there must be exactly two
                # nations overall; together those rules make the entire XI an
                # exact match for the original two-nation player pool.
                rule("specific_nation", 25, "min", 1, "Players from Republic of Ireland: Min. 1"),
                rule("specific_nation", 35, "min", 1, "Players from Northern Ireland: Min. 1"),
                rule("nation_count", 2, "exact", -1, "Nationalities: Exactly 2"),
                rule("quality", 2, "min", 5, "Silver Players: Min. 5"),
                rule("rare_count", 5, "min", -1, "Rare Players: Min. 5"),
                rule("chemistry", 85, "min", -1, "Team Chemistry: Min. 85"),
            ], [pack(1041)]),
            challenge(54203, "Celtic Knot", "Build a Gold squad using eleven nations.", 11, [
                rule("nation_count", 11, "exact", -1, "Nationalities: Exactly 11"),
                rule("league_count", 5, "min", -1, "Leagues: Min. 5"),
                rule("quality", 3, "exact", -1, "Player Quality: Exactly Gold"),
                rule("rare_count", 3, "min", -1, "Rare Players: Min. 3"),
                rule("chemistry", 75, "min", -1, "Team Chemistry: Min. 75"),
            ], [pack(1042)]),
            challenge(54204, "Rainbow's End", "Build a high-chemistry squad from two nations and six leagues.", 11, [
                rule("nation_count", 2, "exact", -1, "Nationalities: Exactly 2"),
                rule("same_nation", 6, "max", -1, "Players from the same Nation: Max. 6"),
                rule("league_count", 6, "min", -1, "Leagues: Min. 6"),
                rule("rare_count", 3, "min", -1, "Rare Players: Min. 3"),
                rule("team_rating", 80, "min", -1, "Squad Rating: Min. 80"),
                rule("chemistry", 90, "min", -1, "Team Chemistry: Min. 90"),
            ], [pack(1006)]),
        ],
    },
]

# Keep the small, hand-tested catalogue that preceded the recovered 598-group
# archive.  Those archive entries contain many incomplete reward labels and
# unrepresentable rules; exposing them produced blank or misleading native
# tiles.  These six sets are copied from the last stable pre-archive release.
SBC_CATALOG.extend([
    {
        "set_id": 33, "category_id": 3, "category": "UPGRADES", "priority": 4,
        "name": "FUT Birthday Encore", "description": "A repeatable route to a guaranteed FUT Birthday star.",
        "repeatable": True, "awards": [pack(1033)],
        "challenges": [challenge(3301, "Birthday XI", "Exchange an 84-rated squad for a FUT Birthday Star Pack.", 11, [
            rule("team_rating", 84, "min", -1, "Team Rating: Min. 84"),
            rule("rare_count", 4, "min", -1, "Rare Players: Min. 4"),
            rule("chemistry", 45, "min", -1, "Team Chemistry: Min. 45"),
        ], [])],
    },
    {
        "set_id": 34, "category_id": 3, "category": "UPGRADES", "priority": 5,
        "name": "Shapeshifters Laboratory", "description": "A repeatable guaranteed Shapeshifters reward.",
        "repeatable": True, "awards": [pack(1034)],
        "challenges": [challenge(3401, "Shift the Shape", "Exchange an 85-rated squad for a Shapeshifters Star Pack.", 11, [
            rule("team_rating", 85, "min", -1, "Team Rating: Min. 85"),
            rule("rare_count", 5, "min", -1, "Rare Players: Min. 5"),
            rule("chemistry", 45, "min", -1, "Team Chemistry: Min. 45"),
        ], [])],
    },
    {
        "set_id": 35, "category_id": 3, "category": "UPGRADES", "priority": 6,
        "name": "Icon Moments Showcase", "description": "A demanding repeatable SBC with a guaranteed Icon Moments reward.",
        "repeatable": True, "awards": [pack(1035)],
        "challenges": [challenge(3501, "Moments of Immortality", "Exchange an elite squad for an Icon Moments Star Pack.", 11, [
            rule("team_rating", 88, "min", -1, "Team Rating: Min. 88"),
            rule("rare_count", 6, "min", -1, "Rare Players: Min. 6"),
            rule("chemistry", 50, "min", -1, "Team Chemistry: Min. 50"),
        ], [])],
    },
    {
        "set_id": 36, "category_id": 3, "category": "UPGRADES", "priority": 7,
        "name": "Festival Roulette", "description": "A repeatable guaranteed elite promo reward.",
        "repeatable": True, "awards": [pack(1037)],
        "challenges": [challenge(3601, "Festival XI", "Exchange an 86-rated squad for an Elite Promo Star Pack.", 11, [
            rule("team_rating", 86, "min", -1, "Team Rating: Min. 86"),
            rule("rare_count", 5, "min", -1, "Rare Players: Min. 5"),
            rule("chemistry", 50, "min", -1, "Team Chemistry: Min. 50"),
        ], [])],
    },
    {
        "set_id": 40, "category_id": 2, "category": "ADVANCED", "priority": 4,
        "name": "Festival Foundations", "description": "Four escalating squads celebrating the biggest FIFA 20 promos.",
        "repeatable": False, "awards": [pack(1031)],
        "challenges": [
            challenge(4001, "Birthday Blueprint", "Build a strong mixed squad to unlock the FUT Birthday Vault.", 11, [
                rule("team_rating", 84, "min", -1, "Team Rating: Min. 84"),
                rule("rare_count", 4, "min", -1, "Rare Players: Min. 4"),
                rule("league_count", 3, "min", -1, "Leagues: Min. 3"),
                rule("chemistry", 65, "min", -1, "Team Chemistry: Min. 65"),
            ], [pack(1028)]),
            challenge(4002, "Shape Theory", "Trade an 85-rated hybrid for the Shapeshifters Vault.", 11, [
                rule("team_rating", 85, "min", -1, "Team Rating: Min. 85"),
                rule("nation_count", 4, "min", -1, "Nationalities: Min. 4"),
                rule("chemistry", 60, "min", -1, "Team Chemistry: Min. 60"),
            ], [pack(1029)]),
            challenge(4003, "Team of the Season", "Submit an elite rare squad for a TOTS Vanguard Pack.", 11, [
                rule("team_rating", 87, "min", -1, "Team Rating: Min. 87"),
                rule("rare_count", 6, "min", -1, "Rare Players: Min. 6"),
                rule("chemistry", 55, "min", -1, "Team Chemistry: Min. 55"),
            ], [pack(1030)]),
            challenge(4004, "Final Festival", "Complete the final 88-rated test for an Endgame Crown Pack.", 11, [
                rule("team_rating", 88, "min", -1, "Team Rating: Min. 88"),
                rule("rare_count", 7, "min", -1, "Rare Players: Min. 7"),
                rule("chemistry", 50, "min", -1, "Team Chemistry: Min. 50"),
            ], [pack(1032)]),
        ],
    },
    {
        "set_id": 41, "category_id": 2, "category": "ADVANCED", "priority": 5,
        "name": "Icons of the Era", "description": "Three elite squads culminating in an Icon Moments Trinity Pack.",
        "repeatable": False, "awards": [pack(1040)],
        "challenges": [
            challenge(4101, "World Class", "Submit an 86-rated squad with strong chemistry.", 11, [
                rule("team_rating", 86, "min", -1, "Team Rating: Min. 86"),
                rule("rare_count", 5, "min", -1, "Rare Players: Min. 5"),
                rule("chemistry", 65, "min", -1, "Team Chemistry: Min. 65"),
            ], [pack(1029)]),
            challenge(4102, "Legendary", "Submit an 88-rated rare squad.", 11, [
                rule("team_rating", 88, "min", -1, "Team Rating: Min. 88"),
                rule("rare_count", 7, "min", -1, "Rare Players: Min. 7"),
                rule("chemistry", 55, "min", -1, "Team Chemistry: Min. 55"),
            ], [pack(1030)]),
            challenge(4103, "Immortal", "The final 89-rated test of the Icon journey.", 11, [
                rule("team_rating", 89, "min", -1, "Team Rating: Min. 89"),
                rule("rare_count", 8, "min", -1, "Rare Players: Min. 8"),
                rule("chemistry", 50, "min", -1, "Team Chemistry: Min. 50"),
            ], [pack(1039)]),
        ],
    },
    {
        "set_id": 42, "category_id": 3, "category": "UPGRADES", "priority": 8,
        "name": "Icon Moments Player Pick",
        "description": "Choose one of three Icon Moments players rated 90 or higher.",
        "repeatable": True,
        "awards": [player_pick("icon_moments", 3, 90, title="1 of 3 Icon Moments Player Pick")],
        "challenges": [challenge(4201, "Moments Selection", "Exchange an elite squad for an Icon Moments Player Pick.", 11, [
            rule("team_rating", 88, "min", -1, "Team Rating: Min. 88"),
            rule("rare_count", 6, "min", -1, "Rare Players: Min. 6"),
            rule("chemistry", 50, "min", -1, "Team Chemistry: Min. 50"),
        ], [])],
    },
    {
        "set_id": 43, "category_id": 3, "category": "UPGRADES", "priority": 9,
        "name": "FUT Birthday Player Pick",
        "description": "Choose one of three FUT Birthday players rated 84 or higher.",
        "repeatable": True,
        "awards": [player_pick("fut_birthday", 3, 84, title="1 of 3 FUT Birthday Player Pick")],
        "challenges": [challenge(4301, "Birthday Selection", "Exchange a strong squad for a FUT Birthday Player Pick.", 11, [
            rule("team_rating", 86, "min", -1, "Team Rating: Min. 86"),
            rule("rare_count", 5, "min", -1, "Rare Players: Min. 5"),
            rule("chemistry", 55, "min", -1, "Team Chemistry: Min. 55"),
        ], [])],
    },
])

# National Pride's exact retail chemistry constraint was the one Advanced
# mini-challenge the tester asked not to retain.
for _set in SBC_CATALOG:
    if int(_set.get("set_id", 0)) == 21:
        _set["challenges"] = [
            entry for entry in _set.get("challenges", [])
            if str(entry.get("name", "")).casefold() != "national pride"
        ]


def _complete_catalog() -> list[dict[str, Any]]:
    """Load all playable archive groups and retain audited hand-tuned entries."""
    data_path = Path(__file__).with_name("sbc_catalog_data.json")
    if not data_path.exists():
        return SBC_CATALOG
    imported = json.loads(data_path.read_text(encoding="utf-8"))
    verified_by_name = {entry["name"]: entry for entry in SBC_CATALOG}
    complete: list[dict[str, Any]] = []
    for source in imported:
        verified = verified_by_name.get(source.get("name"))
        if not verified:
            complete.append(source)
            continue
        # The earlier LocalFUT releases already persisted completions against
        # these twelve ids. Keep those ids stable across this full-catalogue
        # upgrade while attaching archive provenance to the verified record.
        merged = dict(verified)
        merged.update({key: source.get(key) for key in (
            "released", "expires", "event", "source_url", "archive_index_id"
        )})
        complete.append(merged)
    return complete


# Keep the full recovered archive beside the source for auditing, but do not
# publish its incomplete entries to FIFA's native SBC carousel.


SET_BY_ID = {int(entry["set_id"]): entry for entry in SBC_CATALOG}
CHALLENGE_BY_ID: dict[int, tuple[dict[str, Any], dict[str, Any]]] = {
    int(challenge_entry["challenge_id"]): (set_entry, challenge_entry)
    for set_entry in SBC_CATALOG
    for challenge_entry in set_entry["challenges"]
}
