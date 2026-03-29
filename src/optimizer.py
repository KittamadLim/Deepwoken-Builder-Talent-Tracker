import logging

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# TALENT_DB — placeholder for future expansion.
# Format when populated:
#   {
#     "TalentName": [
#       {"stat": "Strength",  "threshold": 40},
#       {"stat": "Willpower", "threshold": 30},
#     ],
#     ...
#   }
# An entry with ≥2 stat requirements triggers Priority-1 treatment.
# ---------------------------------------------------------------------------
TALENT_DB: dict[str, list[dict]] = {}


def compute_priority(
    pre_shrine: dict,
    post_shrine: dict | None = None,
    talent_db: dict | None = None,
) -> tuple[list[dict], list[dict]]:
    """
    Compute stat leveling order for a Deepwoken build.

    Priority 1 (pre-shrine): Stats required by multi-stat talents (TALENT_DB entries ≥2 reqs).
    Priority 2 (pre-shrine): Remaining stats from pre_shrine target values.
    Priority 3 (pre-shrine): Within each tier, higher target value comes first.
    Post-shrine:             Stats still needed after Shrine of Order, sorted by value desc.

    Returns:
        (pre_shrine_order, post_shrine_order)
        Each is a list of {"stat": str, "target": int}.
    """
    if talent_db is None:
        talent_db = TALENT_DB

    # ------------------------------------------------------------------ pre-shrine
    targets: dict[str, int] = {}

    # Priority 1: multi-stat talents
    priority1_stats: set[str] = set()
    for talent_name, reqs in talent_db.items():
        if len(reqs) < 2:
            continue
        log.debug("P1 talent '%s' requires: %s", talent_name, reqs)
        for req in reqs:
            stat = req["stat"]
            threshold = int(req["threshold"])
            targets[stat] = max(targets.get(stat, 0), threshold)
            priority1_stats.add(stat)

    # Priority 2 + 3: all pre-shrine stat values (base + weapon + attunement)
    for stat, value in pre_shrine.items():
        try:
            val = int(value)
        except (TypeError, ValueError):
            continue
        if val <= 0:
            continue
        targets[stat] = max(targets.get(stat, 0), val)

    if not targets:
        log.warning(
            "compute_priority: no pre-shrine stat targets found — "
            "pre_shrine may be empty and TALENT_DB is unpopulated."
        )

    def _sort_key(item: tuple[str, int]) -> tuple[bool, int]:
        stat, target = item
        return (stat not in priority1_stats, -target)

    pre_order = [
        {"stat": stat, "target": target}
        for stat, target in sorted(targets.items(), key=_sort_key)
    ]
    log.info("Pre-shrine order (%d entries): %s", len(pre_order), pre_order)

    # ------------------------------------------------------------------ post-shrine
    post_order: list[dict] = []
    if post_shrine:
        post_order = [
            {"stat": stat, "target": val}
            for stat, val in sorted(post_shrine.items(), key=lambda x: -x[1])
            if val > 0
        ]
        log.info("Post-shrine order (%d entries): %s", len(post_order), post_order)

    return pre_order, post_order
