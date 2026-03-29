import logging
import urllib.parse
import requests

log = logging.getLogger(__name__)

BASE_PROXY = "https://deepwoken.co/api/proxy?url="
BUILD_API = "https://api.deepwoken.co/build"
ALL_API = "https://api.deepwoken.co/get?type=all"

# ---------------------------------------------------------------------------
# Stat name normalisation — maps every known API abbreviation to display name.
# Keys that already appear as proper names pass through unchanged via .get(k, k).
# ---------------------------------------------------------------------------
STAT_DISPLAY: dict[str, str] = {
    # Base stats
    "str": "Strength",       "STR": "Strength",
    "for": "Fortitude",      "FOR": "Fortitude",
    "agi": "Agility",        "AGI": "Agility",
    "int": "Intelligence",   "INT": "Intelligence",
    "wil": "Willpower",      "WIL": "Willpower",
    "cha": "Charisma",       "CHA": "Charisma",
    # Weapon stats
    "LHT": "Light Weapon",   "lht": "Light Weapon",
    "MED": "Medium Weapon",  "med": "Medium Weapon",
    "HVY": "Heavy Weapon",   "hvy": "Heavy Weapon",
    # Attunements (usually already full names; add common shorthand as well)
    "Flamecharm": "Flamecharm",   "flame": "Flamecharm",
    "Frostdraw": "Frostdraw",     "frost": "Frostdraw",
    "Thundercall": "Thundercall", "thunder": "Thundercall",
    "Galebreathe": "Galebreathe", "gale": "Galebreathe",
    "Shadowcast": "Shadowcast",   "shadow": "Shadowcast",
    "Ironsing": "Ironsing",       "iron": "Ironsing",
    "Bloodrend": "Bloodrend",     "blood": "Bloodrend",
}


def _flatten_attributes(block: dict) -> dict[str, int]:
    """
    Recursively flatten an attributes / preShrine API block into
    {display_name: int_value}.  Works for both flat and nested structures
    (e.g. {"base": {...}, "weapon": {...}, "attunement": {...}}).
    Only keeps stats with a positive integer value.
    """
    flat: dict[str, int] = {}

    def _absorb(d: dict) -> None:
        for k, v in d.items():
            if isinstance(v, dict):
                _absorb(v)
            else:
                try:
                    name = STAT_DISPLAY.get(k, k)
                    val = int(v)
                    if val > 0:
                        flat[name] = max(flat.get(name, 0), val)
                except (TypeError, ValueError):
                    pass

    _absorb(block)
    return flat


def _extract_id(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)
    if "id" not in params:
        raise ValueError(f"No 'id' parameter found in URL: {url}")
    return params["id"][0]


def _proxy_get(api_url: str) -> dict:
    # Build the proxied URL: the inner URL is NOT double-encoded per the site's pattern
    proxy_url = BASE_PROXY + api_url
    log.info("GET %s", proxy_url)
    resp = requests.get(proxy_url, timeout=15, headers={"User-Agent": "DeepwokenOverlay/1.0"})
    resp.raise_for_status()
    return resp.json()


def fetch_build(url: str) -> dict:
    """Fetch and normalize a Deepwoken build from a builder URL."""
    build_id = _extract_id(url)
    log.info("Extracted build ID: %s", build_id)

    # Fetch the specific build
    build_data = _proxy_get(f"{BUILD_API}?id={build_id}&options={{}}")
    log.info("Build response top-level keys: %s", list(build_data.keys()))

    # Fetch all talent definitions (used for future TALENT_DB expansion)
    all_data = _proxy_get(ALL_API)
    log.info("All-talents response top-level keys: %s", list(all_data.keys()))

    return _normalize(build_data, all_data)


def _normalize(build_data: dict, all_data: dict) -> dict:
    """
    Flatten the raw API response into:
      {
        "stats":       {...},  # all final target stats (base + weapon + attunement)
        "pre_shrine":  {...},  # stats committed BEFORE Shrine of Order
        "post_shrine": {...},  # additional points invested AFTER Shrine (final - pre)
        "talents":     [...],  # list of talent name strings
        "all_talents": {...},  # raw all-talent data for future TALENT_DB expansion
      }
    """
    build = build_data.get("build", build_data)
    log.debug("Raw build keys: %s", list(build.keys()))

    # Merge base + weapon + attunement (and any other sub-categories) into flat dicts
    stats = _flatten_attributes(build.get("attributes", {}))
    pre_shrine = _flatten_attributes(build.get("preShrine", {}))

    # post_shrine = stats that must be invested *after* the shrine
    post_shrine: dict[str, int] = {
        stat: final_val - pre_shrine.get(stat, 0)
        for stat, final_val in stats.items()
        if final_val - pre_shrine.get(stat, 0) > 0
    }

    # Talents: list of dicts with a "name" key, or plain strings
    raw_talents = build.get("talents", [])
    if raw_talents and isinstance(raw_talents[0], dict):
        talents = [t.get("name") or t.get("id", "") for t in raw_talents]
    else:
        talents = [str(t) for t in raw_talents]
    talents = [t for t in talents if t]

    log.info(
        "Normalized: %d total stats (%d pre-shrine, %d post-shrine), %d talents",
        len(stats), len(pre_shrine), len(post_shrine), len(talents),
    )
    log.debug("Stats: %s", stats)
    log.debug("Pre-shrine: %s", pre_shrine)
    log.debug("Post-shrine: %s", post_shrine)

    return {
        "stats": stats,
        "pre_shrine": pre_shrine,
        "post_shrine": post_shrine,
        "talents": talents,
        "all_talents": all_data,
    }
