"""FIFA 20 seasonal Objectives catalogue and native SCMP response builders.

Only match results that the local server has actually settled count. The
catalogue is intentionally made of goals the server can prove, rather than
inventing goals/assists from match reports that may not contain those stats.
"""

from __future__ import annotations

from typing import Any


CAMPAIGN_ID = 2020
SEASON_SECONDS = 365 * 24 * 60 * 60

# (category ID, name, category type, groups)
# Objective IDs are stable: they are also the persistent claim identifiers.
CATEGORIES: tuple[dict[str, Any], ...] = (
    {
        "categoryId": 1, "name": "Season Objectives", "type": 0, "priority": 0,
        "groups": (
            {
                "groupId": 2001, "title": "Getting Started", "description": "Play FUT matches and earn Season XP.",
                "reward": ("pack", 1005, 1),
                "objectives": (
                    (2101, "First Match", "Play 1 FUT match", "fut.matches", 1, 250),
                    (2102, "First Win", "Win 1 FUT match", "fut.wins", 1, 350),
                    (2103, "Keep Playing", "Play 3 FUT matches", "fut.matches", 3, 500),
                ),
            },
            {
                "groupId": 2002, "title": "Squad Battles", "description": "Build your club through Squad Battles.",
                "reward": ("pack", 1017, 1),
                "objectives": (
                    (2201, "Take the Field", "Play 1 Squad Battles match", "squad_battles.matches", 1, 300),
                    (2202, "Battle Winner", "Win 1 Squad Battles match", "squad_battles.wins", 1, 500),
                    (2203, "Regular Contender", "Play 4 Squad Battles matches", "squad_battles.matches", 4, 750),
                ),
            },
            {
                "groupId": 2003, "title": "Offline Draft", "description": "Progress in Single Player Draft.",
                "reward": ("pack", 1008, 1),
                "objectives": (
                    (2301, "Draft Debut", "Play 1 Offline Draft match", "draft.matches", 1, 300),
                    (2302, "Draft Victory", "Win 1 Offline Draft match", "draft.wins", 1, 500),
                    (2303, "Draft Run", "Play 4 Offline Draft matches", "draft.matches", 4, 750),
                ),
            },
        ),
    },
)

# Six pages of five levels, matching FIFA 20's 30-tier Season Objectives UI.
# Rewards use pack IDs already defined in this project's pack catalogue.
LEVEL_REWARDS: tuple[tuple[str, int, int], ...] = (
    ("coin", 5000, 1), ("pack", 1005, 1), ("coin", 7500, 1),
    ("pack", 1010, 1), ("pack", 1013, 1), ("coin", 10000, 1),
    ("pack", 1017, 1), ("pack", 1008, 1), ("coin", 12500, 1),
    ("pack", 1006, 1), ("pack", 1017, 1), ("coin", 15000, 1),
    ("pack", 1008, 1), ("pack", 1013, 1), ("coin", 17500, 1),
    ("pack", 1009, 1), ("pack", 1006, 1), ("coin", 20000, 1),
    ("pack", 1008, 1), ("pack", 1009, 1), ("coin", 25000, 1),
    ("pack", 1017, 2), ("pack", 1009, 1), ("coin", 30000, 1),
    ("pack", 1008, 2), ("pack", 1009, 1), ("coin", 40000, 1),
    ("pack", 1009, 2), ("coin", 50000, 1), ("pack", 1009, 3),
)


def metric_value(record: dict[str, Any], metric: str) -> int:
    mode, field = metric.split(".", 1)
    values = record.get(mode, {})
    if not isinstance(values, dict):
        return 0
    return max(0, int(values.get(field, 0) or 0))


def reward_wire(reward: tuple[str, int, int]) -> dict[str, Any]:
    kind, value, count = reward
    return {
        "awardType": kind,
        "type": kind,
        "value": int(value),
        "awardValue": int(value),
        "count": int(count),
        "awardCount": int(count),
        "halId": 4 if kind == "pack" else 0,
        "isUntradeable": kind == "pack",
        "untradeable": kind == "pack",
        "loan": 0,
    }


def objective_definition(objective_id: int) -> tuple[dict[str, Any], tuple[Any, ...]] | None:
    for category in CATEGORIES:
        for group in category["groups"]:
            for objective in group["objectives"]:
                if objective[0] == objective_id:
                    return group, objective
    return None


def group_definition(group_id: int) -> tuple[dict[str, Any], dict[str, Any]] | None:
    for category in CATEGORIES:
        for group in category["groups"]:
            if group["groupId"] == group_id:
                return category, group
    return None


def level_reward(level_id: int, option_id: int) -> tuple[str, int, int] | None:
    if option_id != 0 or not 1 <= level_id <= len(LEVEL_REWARDS):
        return None
    return LEVEL_REWARDS[level_id - 1]


def level_threshold(level_id: int) -> int:
    """Level 1 is the starting tier; subsequent tiers require earned XP."""
    return max(0, level_id - 1) * 500


def settled_match_xp(mode: str, result: str) -> int:
    """Repeatable local Season XP for a completed, settled FUT match."""
    if mode not in ("squad_battles", "draft", "rivals", "champion", "generic"):
        return 0
    base = 100 if mode in ("squad_battles", "draft", "generic") else 125
    return base + (50 if result == "WIN" else 25 if result == "DRAW" else 0)


def objective_payload(objective: tuple[Any, ...], record: dict[str, Any],
                      claims: dict[str, Any], end_time: int) -> dict[str, Any]:
    objective_id, name, description, metric, target, xp = objective
    progress = min(target, metric_value(record, metric))
    claimed = str(objective_id) in claims.get("objectives", {})
    state = "REDEEMED" if claimed else "COMPLETED" if progress >= target else "IN_PROGRESS"
    return {
        "objectiveId": objective_id,
        "name": name,
        "description": description,
        "shortDescription": description,
        "currentProgress": progress,
        "progressCount": target,
        "state": state,
        "expiryTime": end_time,
        "awards": [reward_wire(("xp", xp, 1))],
    }


def group_payload(group: dict[str, Any], record: dict[str, Any],
                  claims: dict[str, Any], start_time: int) -> dict[str, Any]:
    objectives = [objective_payload(o, record, claims, start_time + SEASON_SECONDS)
                  for o in group["objectives"]]
    completed = all(o["state"] == "REDEEMED" for o in objectives)
    redeemed = str(group["groupId"]) in claims.get("groups", {})
    return {
        "groupId": group["groupId"],
        "title": group["title"],
        "subTitle": group["description"],
        "description": group["description"],
        "groupType": 0,
        "groupState": 2 if redeemed else 1 if completed else 0,
        "state": 2 if redeemed else 1 if completed else 0,
        "priority": group["groupId"],
        "startTime": start_time,
        "endTime": start_time + SEASON_SECONDS,
        "groupIdsToUnlock": [],
        "objectives": objectives,
        "awardsList": [reward_wire(group["reward"])],
    }


def campaign_payload(record: dict[str, Any], claims: dict[str, Any],
                     xp: int, start_time: int, now: int) -> dict[str, Any]:
    categories = []
    for category in CATEGORIES:
        categories.append({
            "categoryId": category["categoryId"],
            "name": category["name"],
            "type": category["type"],
            "priority": category["priority"],
            "groupsList": [group_payload(g, record, claims, start_time)
                           for g in category["groups"]],
        })
    levels = []
    for index, reward in enumerate(LEVEL_REWARDS, start=1):
        option = {
            "optionId": 0,
            "awards": [reward_wire(reward)],
            "hiddenReward": False,
        }
        levels.append({
            "levelId": index,
            "levelName": f"Level {index}",
            "xpThreshold": level_threshold(index),
            "chosenOption": 0 if str(index) in claims.get("levels", {}) else -1,
            "rewardOptionList": [option],
        })
    category_progress = []
    for category in categories:
        groups = category.get("groupsList", [])
        completed_groups = sum(
            1 for group in groups
            if int(group.get("groupState", group.get("state", 0)) or 0) >= 1
        )
        category_progress.append({
            "categoryId": int(category["categoryId"]),
            "groupsCompleted": completed_groups,
            "groupsCount": len(groups),
            "completed": bool(groups) and completed_groups == len(groups),
        })
    current_xp = max(0, int(xp))
    return {
        "campaignId": CAMPAIGN_ID,
        "campaignName": "Local FUT Season",
        "campaignScore": current_xp,
        "title": "Local FUT Season",
        "subtitle": "Complete Objectives to earn rewards",
        "startTime": start_time,
        "endTime": start_time + SEASON_SECONDS,
        "serverCrtTime": now,
        "userXp": current_xp,
        "userXpList": [{"campaignId": CAMPAIGN_ID, "xp": current_xp}],
        "scmpCategoryProgressList": category_progress,
        "categoriesList": categories,
        "categories": categories,
        "levelsList": levels,
        "levels": levels,
        "hasPreviousCampaign": False,
        "needsAutoClaim": False,
        "needsWorldAutoclaim": False,
        "needsGroupsRefresh": False,
        "remainingRewards": sum(1 for level in levels
                                if xp >= level["xpThreshold"] and level["chosenOption"] < 0),
    }
