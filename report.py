#!/usr/bin/env python3
"""
Sleeper dynasty league metrics (12-team, 1QB, PPR).

Outputs three tables:
  1. Overall metrics          : PWR, LONG, OVERALL
  2. Current-season metrics   : VALUE, PF AVG, PF VAR, COACH, EXP WR, LUCK, PWR
  3. Future-season metrics    : AGE AVG, DYN, PICKS, LONG

The script refuses to run mid-week: it only ever reports on the most recently
*completed* regular-season week (see week_state()/ESPN check below), and never
reports on postseason weeks.

Requires: requests    ( pip install requests )
Recommended: curl_cffi ( pip install curl_cffi )
"""

import csv
import datetime
import html
import json
import math
import os
import re
import statistics
import time

import requests

try:
    from curl_cffi import requests as curl_requests
    _HAS_CURL_CFFI = True
except ImportError:
    _HAS_CURL_CFFI = False

# ----------------------------------------------------------------------------
# CONFIG  -- everything tunable lives here
# ----------------------------------------------------------------------------

LEAGUE_ID = "1375130525345275904"

# FantasyCalc league shape
FC_PARAMS = {"numQbs": 1, "numTeams": 12, "ppr": 1}

TOP_N_FOR_VALUE = 22       # full starting lineup + bench
SIGMA = 15.0               # std dev of margin, used in the win-probability model
REGULAR_SEASON_WEEKS = 14  # fallback; overridden by league playoff_week_start

# Never generate a report for this week or later -- it's the postseason.
# Also acts as a hard safety cap independent of whatever the league's own
# playoff_week_start setting says.
POSTSEASON_START_WEEK = 15

CACHE_DIR = ".cache"
CACHE_TTL = 60 * 60 * 12   # 12h -- player map is ~5MB, don't refetch constantly

# Which future rookie-draft seasons to count toward PICKS.
# None = auto (the 3 seasons after the current one).
FUTURE_PICK_SEASONS = None

# Rounds in a FUTURE ROOKIE draft. This cannot be read from the API: the only
# draft this league has is the 25-round startup, and the 2027 rookie draft
# doesn't exist yet. Set it to match your league. FantasyCalc only publishes
# values through the 4th, so anything beyond 4 contributes nothing anyway.
ROOKIE_DRAFT_ROUNDS = 4

# Power rating weights. Interpolated from EARLY -> LATE as the season plays out,
# so preseason leans on roster value/projection and December leans on results.
PWR_WEIGHTS_EARLY = {
    "VALUE": 0.50, "EXP_WR": 0.50, "PF_AVG": 0.00,
    "COACH": 0.00, "PF_VAR": 0.00, "LUCK": 0.00,
}
PWR_WEIGHTS_LATE = {
    "VALUE": 0.00, "EXP_WR": 0.50, "PF_AVG": 0.50,
    "COACH": 0.20, "PF_VAR": -0.10, "LUCK": -0.10,
}
# Note: PF_VAR and LUCK carry negative weight late -- a high-variance team is
# less reliable, and a lucky team (wins > all-play wins) is due to regress.

LONG_WEIGHTS = {
    "AGE_AVG": -0.75,   # younger is better
    "DYN": 0.50,        # dynasty value above redraft value = long-term assets
    "PICKS": 0.75,
}

# --- running joke -----------------------------------------------------------
# When True, this Sleeper username is always forced to the bottom row of the
# OVERALL table, no matter what their actual score is. Ranks stay correct and
# sequential (1..N) and the metric values themselves are never touched -- only
# the sort order changes so they land last. Flip to False to turn it off.
JOKE_LOSER_ENABLED = True
JOKE_LOSER_USERNAME = "jamesminrow"

# Which bench positions can cover which starting slot.
FLEX_ELIGIBILITY = {
    "FLEX": {"RB", "WR", "TE"},
}
NON_STARTING_SLOTS = {"BN", "TAXI", "IR"}

# Where CSV + HTML land. Prior weeks' CSVs are read back to compute rank moves.
OUTPUT_DIR = "reports"

# (label, better_direction, legend blurb). Drives the table headers, the
# up/down arrows, the legend, the CSV columns, and which cells get bolded as
# the column's best value -- edit here only.
CURRENT_COLS = [
    ("VALUE",  "up",   "Team Value: Mean FantasyCalc dynasty trade value of the 22 most "
                       "valuable assets on the roster."),
    ("PF AVG", "up",   "Average Points-for: Average points scored per completed week."),
    ("PF VAR", "down", "Scoring Consistency: Standard deviation of weekly score. Lower means a "
                       "more predictable team."),
    ("COACH",  "up",   "Lineup Efficiency: Points actually scored divided by "
                       "the maximum the roster could have scored."),
    ("EXP WR", "up",   "Expected Win Rate: Wins so far plus projected win probability for every "
                       "remaining matchup, divided by the number of weeks in the season."),
    ("LUCK",   "down", "Luck Factor: Win rate divided by all-play win rate. Above 1.00 "
                       "indicates an easier schedule."),
    ("PWR",    "up",   "Power Rating: Weighted blend of the z-scores above, scaled 0-100. "
                       "Weighting shifts from roster value toward results as "
                       "the season progresses."),
]
FUTURE_COLS = [
    ("AGE AVG", "down", "Team Age: Average age across the whole roster."),
    ("DYN",     "up",   "Dynasty Differential: FantasyCalc dynasty value minus redraft value. Higher means more "
                        "of the team's worth sits in future seasons."),
    ("PICKS",   "up",   "Draft Capital: Combined FantasyCalc value of every future rookie "
                        "pick the team owns."),
    ("LONG",    "up",   "Longevity Score: Weighted blend of age, dynasty differential and draft "
                        "capital, scaled 0-100."),
]
OVERALL_COLS = [
    ("PWR",     "up", "Power Rating: This season's performance/value score. "
                      "See the Current Season table for its components."),
    ("LONG",    "up", "Longevity Score: Long-term outlook score. See the Future "
                      "Seasons table for its components."),
    ("OVERALL", "up", "Overall Score: Simple average of Power Rating and Longevity "
                      "Score; a high score indicates a current and future contender."),
]

ARROW = {"up": "&#9650;", "down": "&#9660;"}


# ----------------------------------------------------------------------------
# HTTP helpers
# ----------------------------------------------------------------------------

def get_json(url, params=None, cache_key=None, headers=None):
    """GET with optional on-disk cache. Raises loudly so failures are obvious."""
    if cache_key:
        os.makedirs(CACHE_DIR, exist_ok=True)
        path = os.path.join(CACHE_DIR, cache_key + ".json")
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < CACHE_TTL:
            with open(path) as f:
                return json.load(f)

    r = requests.get(url, params=params, timeout=30,
                     headers=headers or {"User-Agent": "league-metrics/1.0"})
    if r.status_code != 200:
        raise RuntimeError(f"{r.status_code} from {r.url}")
    data = r.json()

    if cache_key:
        with open(path, "w") as f:
            json.dump(data, f)
    return data


def sleeper(path):
    return get_json(f"https://api.sleeper.app/v1{path}")


def fantasycalc(is_dynasty):
    """FantasyCalc trade values. Returns the raw list of value entries."""
    params = dict(FC_PARAMS, isDynasty=str(bool(is_dynasty)).lower())
    return get_json("https://api.fantasycalc.com/values/current", params=params,
                    cache_key=f"fc_{'dyn' if is_dynasty else 'redraft'}")


def week_projections(season, week):
    """All PPR projections for one week, keyed by sleeper player_id."""
    url = f"https://api.sleeper.com/projections/nfl/{season}/{week}"
    params = [("season_type", "regular"), ("order_by", "ppr")]
    for pos in ("QB", "RB", "WR", "TE", "K", "DEF"):
        params.append(("position[]", pos))
    rows = get_json(url, params=params, cache_key=f"proj_{season}_{week}")
    out = {}
    for row in rows:
        pts = (row.get("stats") or {}).get("pts_ppr")
        if pts is not None:
            out[str(row["player_id"])] = float(pts)
    return out


def _espn_get(url, params):
    """
    ESPN's site API isn't a documented public API -- it sits behind bot
    detection (Akamai-style) that fingerprints the TLS handshake itself, not
    just headers. That's why a browser hitting this exact URL sails through
    while `requests` gets a 403 even with a convincing User-Agent: the
    handshake alone gives it away as a script. curl_cffi reproduces a real
    Chrome TLS fingerprint and gets past it; if it isn't installed we fall
    back to plain requests + browser headers, which may or may not work.
    """
    if _HAS_CURL_CFFI:
        r = curl_requests.get(url, params=params, timeout=30,
                              impersonate="chrome124")
    else:
        r = requests.get(url, params=params, timeout=30, headers={
            "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0.0.0 Safari/537.36"),
            "Accept": "application/json",
            "Referer": "https://www.espn.com/",
        })
    if r.status_code != 200:
        raise RuntimeError(f"{r.status_code} from {r.url}")
    return r.json()


def espn_game_statuses(season, week, seasontype=2):
    """
    Status strings ('STATUS_SCHEDULED' / 'STATUS_IN_PROGRESS' / 'STATUS_FINAL' /
    etc.) for every NFL game in a given week, via ESPN's public scoreboard.

    Sleeper's own /state/nfl only exposes a single "current week" counter with
    no notion of whether that week's games have actually kicked off or wrapped
    up, so this is what we lean on to tell "week in progress" apart from
    "week is over". Never cached -- the whole point is a live read at run time.
    """
    url = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
    params = {"seasontype": seasontype, "week": week, "year": season}
    try:
        data = _espn_get(url, params)
    except Exception as e:
        # Network hiccup or ESPN blocking us -- don't crash the whole run,
        # just report the week as unknown so main() refuses gracefully.
        print(f"  [debug] ESPN scoreboard request failed for week {week} "
              f"({e}); treating status as unknown")
        return []
    out = []
    for event in data.get("events", []):
        comp = (event.get("competitions") or [{}])[0]
        name = ((comp.get("status") or {}).get("type") or {}).get("name")
        if name:
            out.append(name)
    return out


def week_state(season, week):
    """
    'not_started' -- no games for this week have kicked off yet
    'in_progress' -- at least one game has started and at least one isn't final
    'complete'    -- every game for this week is final
    'unknown'     -- ESPN returned nothing for this week; can't tell, don't guess
    """
    statuses = espn_game_statuses(season, week)
    if not statuses:
        return "unknown"
    if all(s == "STATUS_FINAL" for s in statuses):
        return "complete"
    if all(s == "STATUS_SCHEDULED" for s in statuses):
        return "not_started"
    return "in_progress"


# ----------------------------------------------------------------------------
# Pure helpers  -- these are the bits worth unit-testing / editing
# ----------------------------------------------------------------------------

def phi(x):
    """CDF of the standard normal."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def eligible_positions(slot):
    return FLEX_ELIGIBILITY.get(slot, {slot})


def best_lineup(scores, positions, slots, waiver=None):
    """
    Highest total achievable from `scores` given the league's starting `slots`.

    Fills the most restrictive slot first. Because eligibility here is nested
    (QB subset of SUPER_FLEX, RB/WR/TE subset of FLEX), that greedy order is
    optimal and avoids needing a full assignment solver.

    `waiver`: optional {position: [best_fa_pts, 2nd_best, ...]} descending.
    Used when a slot has no rostered player projected above zero -- i.e. bye
    weeks, or a team that simply doesn't roster a 2nd TE/K. Each free agent is
    consumed once, so a team can't plug the same streamer into two slots.
    """
    remaining = set(scores)
    pool = {k: list(v) for k, v in (waiver or {}).items()}
    total = 0.0

    for slot in sorted(slots, key=lambda s: len(eligible_positions(s))):
        elig = eligible_positions(slot)
        candidates = [p for p in remaining if positions.get(p) in elig]
        best_pts = 0.0
        best_player = None
        if candidates:
            best_player = max(candidates, key=lambda p: scores.get(p, 0.0))
            best_pts = scores.get(best_player, 0.0)

        if pool:
            # best still-available free agent across eligible positions
            fa_pos = max((p for p in elig if pool.get(p)),
                         key=lambda p: pool[p][0], default=None)
            if fa_pos is not None and pool[fa_pos][0] > best_pts:
                total += pool[fa_pos].pop(0)   # stream a replacement instead
                continue

        if best_player is not None:
            total += best_pts
            remaining.discard(best_player)
    return total


def zscores(values):
    """Z-score a list. Returns zeros if there's no spread."""
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return [0.0] * len(values)
    mu = statistics.mean(vals)
    sd = statistics.pstdev(vals)
    if sd == 0:
        return [0.0] * len(values)
    return [0.0 if v is None else (v - mu) / sd for v in values]


def scale_0_100(values):
    """Min-max a composite onto 0-100 for readability."""
    lo, hi = min(values), max(values)
    if hi == lo:
        return [50.0] * len(values)
    return [100.0 * (v - lo) / (hi - lo) for v in values]


PICK_RE = re.compile(
    r"^(20\d\d)\s+(?:(early|mid|late)\s+)?(\d)(?:st|nd|rd|th)", re.IGNORECASE)


def parse_pick_values(fc_rows):
    """
    Pull draft-pick entries out of the FantasyCalc list.

    FantasyCalc mixes picks in with players, named like "2027 1st" or
    "2027 Mid 2nd". We average across early/mid/late since future pick slots
    are unknown. Returns {(season:int, round:int): value:float}.
    """
    buckets = {}
    for row in fc_rows:
        name = (row.get("player") or {}).get("name", "")
        m = PICK_RE.match(name.strip())
        if not m:
            continue
        season, _tier, rnd = int(m.group(1)), m.group(2), int(m.group(3))
        buckets.setdefault((season, rnd), []).append(float(row["value"]))
    return {k: statistics.mean(v) for k, v in buckets.items()}


def owned_picks(rosters, traded_picks, seasons, rounds):
    """
    {roster_id: [(season, round), ...]} of future picks actually owned.

    Every roster starts owning its own picks. A pick can change hands more than
    once and the endpoint isn't guaranteed to be in chronological order, so we
    follow the previous_owner -> owner chain from the original owner rather than
    replaying the rows in list order.
    """
    rids = [r["roster_id"] for r in rosters]
    current = {}
    for rid in rids:
        for season in seasons:
            for rnd in range(1, rounds + 1):
                current[(season, rnd, rid)] = rid   # key = (season, rnd, ORIGINAL)

    hops = {}
    for tp in traded_picks:
        season = int(tp["season"])
        if season not in seasons or tp["round"] > rounds:
            continue
        key = (season, tp["round"], tp["roster_id"])
        prev = tp.get("previous_owner_id", tp["roster_id"])
        hops.setdefault(key, {})[prev] = tp["owner_id"]

    for key, chain in hops.items():
        owner, seen = key[2], set()
        while owner in chain and owner not in seen:
            seen.add(owner)
            owner = chain[owner]
        current[key] = owner

    out = {rid: [] for rid in rids}
    for (season, rnd, _orig), owner in current.items():
        if owner in out:
            out[owner].append((season, rnd))
    return out


def find_roster_id_by_username(rosters, users, username):
    """
    roster_id for the roster owned by a given Sleeper username, or None.

    Matches against both "username" (login handle) and "display_name" (what
    shows in-app), case-insensitively, since people refer to others by either.
    """
    if not username:
        return None
    target = username.strip().lower()
    for r in rosters:
        u = users.get(r["owner_id"], {})
        handle = (u.get("username") or "").strip().lower()
        display = (u.get("display_name") or "").strip().lower()
        if target in (handle, display):
            return r["roster_id"]
    return None


def best_teams_by_column(rows, cols, key_col):
    """
    {label: {team, ...}} -- whichever row(s) hold the best raw value for that
    column, so the caller can bold them. Skips key_col (it's the sort column
    and already rendered bold) and skips columns with no data (all None).
    Ties are all marked best.
    """
    best = {}
    for label, direction, _ in cols:
        if label == key_col:
            continue
        candidates = [(r["raw"][label], r["team"]) for r in rows
                      if r["raw"][label] is not None]
        if not candidates:
            continue
        target = (max if direction == "up" else min)(v for v, _ in candidates)
        best[label] = {team for v, team in candidates if v == target}
    return best


def season_dir(season):
    """OUTPUT_DIR/<season>/ -- everything for one league season lives here."""
    return os.path.join(OUTPUT_DIR, str(season))


def csv_dir(season, week):
    """OUTPUT_DIR/<season>/week_<week>_csv/ -- CSVs live in their own
    per-week subfolder, kept separate from the HTML report."""
    return os.path.join(season_dir(season), f"week_{week}_csv")


def csv_path(season, week, kind):
    return os.path.join(csv_dir(season, week), f"week_{week}_{kind}.csv")


def html_path(season, week):
    """OUTPUT_DIR/<season>/week_<week>_report.html -- the report sits
    directly in the season folder, not nested inside the csv subfolder."""
    return os.path.join(season_dir(season), f"week_{week}_report.html")


def write_csv(season, week, kind, cols, rows):
    os.makedirs(csv_dir(season, week), exist_ok=True)
    path = csv_path(season, week, kind)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["RANK", "TEAM", "ROSTER_ID", "OWNER"] +
                   [label for label, _, _ in cols])
        for r in rows:
            w.writerow([r["rank"], r["team"], r["roster_id"], r["owner"]] +
                       ["" if r["raw"][label] is None else round(r["raw"][label], 4)
                        for label, _, _ in cols])
    return path


def prev_ranks(season, week, kind):
    """
    (prev_week, by_roster_id, by_owner, by_team_name) from the most recent
    earlier week we have on disk.

    Team display names get renamed all the time, so matching week-to-week
    purely by name (the old behavior) silently breaks rank-movement tracking
    every time someone renames their team. roster_id is the stable key --
    it's tied to the roster slot for the whole season regardless of name --
    with the owner's Sleeper username as a second fallback (covers the rare
    case of a league being recreated mid-season with new roster ids but the
    same owners) and team name kept only for CSVs written before this fix,
    which won't have a ROSTER_ID column at all.

    Scans backwards rather than assuming week-1 exists, so skipping a week
    still produces sensible movement instead of silently blanking it.
    """
    for w in range(week - 1, -1, -1):
        path = csv_path(season, w, kind)
        if not os.path.exists(path):
            continue
        with open(path, newline="") as f:
            by_id, by_owner, by_name = {}, {}, {}
            for row in csv.DictReader(f):
                rank = int(row["RANK"])
                by_name[row["TEAM"]] = rank
                rid = (row.get("ROSTER_ID") or "").strip()
                if rid:
                    by_id[int(rid)] = rank
                owner = (row.get("OWNER") or "").strip().lower()
                if owner:
                    by_owner[owner] = rank
            return w, by_id, by_owner, by_name
    return None, {}, {}, {}


def row_tint(delta):
    """Green if the team climbed, red if it slid, stronger with bigger moves."""
    if not delta:
        return "transparent"
    alpha = 0.06 + min(abs(delta), 5) / 5 * 0.20
    rgb = "22,163,74" if delta > 0 else "220,38,38"
    return f"rgba({rgb},{alpha:.3f})"


def delta_badge(delta, has_prev):
    if not has_prev:
        return '<span class="d flat">&middot;</span>'
    if delta > 0:
        return f'<span class="d up">&#9650;{delta}</span>'
    if delta < 0:
        return f'<span class="d down">&#9660;{abs(delta)}</span>'
    return '<span class="d flat">&ndash;</span>'


CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{background:#e9ebef;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',
Roboto,Helvetica,Arial,sans-serif;padding:28px 16px;color:#12141a}
.page{width:940px;margin:0 auto 34px;background:#fff;border-radius:10px;
overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.14),0 8px 26px rgba(0,0,0,.09)}
.bar{height:4px;background:#d50a0a}
.head{padding:20px 26px 16px;border-bottom:1px solid #e3e5ea}
.head h1{font-size:21px;font-weight:800;letter-spacing:-.35px}
.head .sub{margin-top:4px;font-size:12px;color:#6b7280;
text-transform:uppercase;letter-spacing:.7px;font-weight:600}
table{width:100%;border-collapse:collapse}
thead th{background:#12141a;color:#fff;font-size:10.5px;font-weight:700;
text-transform:uppercase;letter-spacing:.8px;padding:11px 10px;text-align:right;
white-space:nowrap}
thead th.l{text-align:left}
thead th .dir{font-size:7.5px;margin-left:4px;opacity:.75;vertical-align:middle}
tbody td{padding:11px 10px;font-size:13.5px;text-align:right;
border-bottom:1px solid #eef0f3;font-variant-numeric:tabular-nums}
tbody tr:last-child td{border-bottom:none}
td.rank{width:38px;text-align:center;font-weight:800;color:#6b7280;font-size:12.5px}
td.mv{width:42px;text-align:left;padding-left:0}
td.team{text-align:left;font-weight:650;letter-spacing:-.2px}
td.key{font-weight:800}
td.best{font-weight:800}
.d{font-size:10.5px;font-weight:700}
.d.up{color:#16a34a}.d.down{color:#dc2626}.d.flat{color:#c2c7d0}
.legend{padding:16px 26px 22px;background:#fafbfc;border-top:1px solid #e3e5ea}
.legend h2{font-size:10.5px;text-transform:uppercase;letter-spacing:.9px;
color:#6b7280;margin-bottom:11px;font-weight:700}
.li{display:flex;gap:10px;margin-bottom:7px;font-size:11.5px;line-height:1.45;align-items:flex-start}
.li b{flex:0 0 78px;font-weight:800;font-size:10.5px;padding-top:1px;white-space:nowrap}
.li span{color:#4b5563;flex:1 1 auto;min-width:0;font-size:11.5px}
.li .a{font-size:7.5px;color:#9aa1ac}
.note{padding:11px 26px 16px;font-size:10.5px;color:#8b919c;line-height:1.5;
background:#fafbfc}
.footer{max-width:940px;margin:0 auto 26px;text-align:center;font-size:11px;
color:#9aa1ac}
@media print{body{background:#fff;padding:0}
.page{box-shadow:none;margin:0;page-break-after:always;border-radius:0}
.footer{display:none}}
"""


def render_page(title, subtitle, cols, rows, prev_week, key_col, note):
    has_prev = prev_week is not None
    best = best_teams_by_column(rows, cols, key_col)

    th = "".join(
        f'<th>{label}<span class="dir">{ARROW[d]}</span></th>'
        for label, d, _ in cols)

    body = ""
    for r in rows:
        cells = ""
        for label, _, _ in cols:
            if label == key_col:
                cls = "key"
            elif r["team"] in best.get(label, ()):
                cls = "best"
            else:
                cls = ""
            cells += f'<td class="{cls}">{r["disp"][label]}</td>'
        body += (f'<tr style="background:{row_tint(r["delta"])}">'
                 f'<td class="rank">{r["rank"]}</td>'
                 f'<td class="mv">{delta_badge(r["delta"], has_prev)}</td>'
                 f'<td class="team">{html.escape(r["team"])}</td>{cells}</tr>')

    legend = "".join(
        f'<div class="li"><b>{label} <span class="a">{ARROW[d]}</span></b>'
        f'<span>{html.escape(desc)}</span></div>' for label, d, desc in cols)

    movement = (f"Row colour shows movement against week {prev_week}: "
                "green climbed, red slid." if has_prev else
                "")

    return f"""<div class="page"><div class="bar"></div>
<div class="head"><h1>{html.escape(title)}</h1><div class="sub">{html.escape(subtitle)}</div></div>
<table><thead><tr><th class="l">#</th><th class="l"></th><th class="l">Team</th>{th}</tr></thead>
<tbody>{body}</tbody></table>
<div class="legend"><h2>Legend &nbsp;&middot;&nbsp; {ARROW['up']} higher is better &nbsp;&middot;&nbsp; {ARROW['down']} lower is better &nbsp;&middot;&nbsp; <b>bold</b> = best in column</h2>{legend}</div>
<div class="note">{movement} {html.escape(note)}</div></div>"""


def write_html(path, league_name, pages, generated_at):
    footer = f'<div class="footer">Last updated {html.escape(generated_at)}</div>'
    with open(path, "w") as f:
        f.write(f"<!doctype html><html><head><meta charset='utf-8'>"
                f"<title>{html.escape(league_name)}</title><style>{CSS}</style>"
                f"</head><body>{''.join(pages)}{footer}</body></html>")
    return path


def print_table(title, headers, rows):
    print(f"\n{title}")
    widths = [max(len(str(h)), max((len(str(r[i])) for r in rows), default=0))
              for i, h in enumerate(headers)]
    line = "  ".join(str(h).rjust(w) for h, w in zip(headers, widths))
    print(line)
    print("-" * len(line))
    for r in rows:
        print("  ".join(str(c).rjust(w) for c, w in zip(r, widths)))


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main():
    # --- league scaffolding -------------------------------------------------
    league = sleeper(f"/league/{LEAGUE_ID}")
    rosters = sleeper(f"/league/{LEAGUE_ID}/rosters")
    users = {u["user_id"]: u for u in sleeper(f"/league/{LEAGUE_ID}/users")}
    state = sleeper("/state/nfl")
    players = get_json("https://api.sleeper.app/v1/players/nfl",
                       cache_key="players_nfl")

    season = int(league["season"])
    start_slots = [s for s in league["roster_positions"]
                   if s not in NON_STARTING_SLOTS]
    reg_weeks = league["settings"].get("playoff_week_start", 0) - 1
    if reg_weeks <= 0:
        reg_weeks = REGULAR_SEASON_WEEKS

    def team_name(roster):
        u = users.get(roster["owner_id"], {})
        return (u.get("metadata") or {}).get("team_name") or \
               u.get("display_name") or f"Roster {roster['roster_id']}"

    def owner_of(roster):
        """Stable-ish handle for whoever owns a roster: Sleeper username, or
        display_name if no username is set. Team display names get renamed
        constantly; this doesn't, which is why week-to-week rank comparisons
        key off roster_id/owner rather than the team name string."""
        u = users.get(roster["owner_id"], {})
        return u.get("username") or u.get("display_name") or ""

    names = {r["roster_id"]: team_name(r) for r in rosters}
    owners = {r["roster_id"]: owner_of(r) for r in rosters}
    rids = [r["roster_id"] for r in rosters]
    pos_of = {pid: (p.get("fantasy_positions") or [p.get("position")])[0]
              for pid, p in players.items() if p.get("position")}

    # --- decide which week (if any) we're allowed to report on -------------
    # We only ever report the most recently *completed* regular-season week:
    # never a week whose games are still being played, and never the
    # postseason. Sleeper's /state/nfl gives a "current week" counter but no
    # per-game timing, so ESPN's public scoreboard (week_state) is used to
    # tell "in progress" apart from "over".
    max_report_week = min(reg_weeks, POSTSEASON_START_WEEK - 1)
    global_season = int(state.get("season", season))

    if global_season < season:
        print(f"League season {season} hasn't started yet per Sleeper "
              f"(current NFL season is {global_season}) -- nothing to report.")
        return
    elif global_season > season:
        # this league's season is entirely in the past -- report it in full.
        report_week = max_report_week
    else:
        season_type = state.get("season_type", "regular")
        if season_type != "regular":
            print(f"NFL isn't in its regular season right now "
                  f"(season_type={season_type!r}) -- nothing to report.")
            return

        nfl_week = int(state.get("week") or 1)
        status = week_state(season, nfl_week)

        if status == "in_progress":
            print(f"Week {nfl_week} games are in progress -- refusing to "
                  f"generate a report mid-week. Run again once the week wraps.")
            return
        if status == "unknown":
            print(f"Couldn't find any game data for week {nfl_week} -- "
                  f"refusing to guess. Exiting.")
            return

        report_week = nfl_week if status == "complete" else nfl_week - 1

    if report_week < 1:
        print("No completed regular-season weeks yet -- nothing to report.")
        return
    if report_week > max_report_week:
        print(f"Week {report_week} is at or past the postseason "
              f"(cap={max_report_week}) -- refusing to generate that report.")
        return

    completed = list(range(1, report_week + 1))
    matchups_by_week = {w: sleeper(f"/league/{LEAGUE_ID}/matchups/{w}")
                        for w in completed}
    future = list(range(report_week + 1, reg_weeks + 1))

    print(f"League: {league['name']}  |  season {season}  |  "
          f"completed weeks: {len(completed)}  |  remaining: {len(future)}")

    # --- results-based metrics ----------------------------------------------
    weekly = {rid: [] for rid in rids}     # actual points
    optimal = {rid: [] for rid in rids}    # max possible points
    wins = {rid: 0.0 for rid in rids}
    allplay_wins = {rid: 0.0 for rid in rids}
    allplay_games = {rid: 0 for rid in rids}

    for wk in completed:
        ms = matchups_by_week[wk]
        pts = {m["roster_id"]: (m.get("points") or 0.0) for m in ms}

        for m in ms:
            rid = m["roster_id"]
            weekly[rid].append(pts[rid])
            pp = m.get("players_points") or {}
            if not pp:
                print(f"  [debug] week {wk} roster {rid}: no players_points, "
                      f"COACH will be understated")
            roster_pts = {p: pp.get(p, 0.0) for p in (m.get("players") or [])}
            optimal[rid].append(best_lineup(roster_pts, pos_of, start_slots))

        # head-to-head
        by_matchup = {}
        for m in ms:
            by_matchup.setdefault(m.get("matchup_id"), []).append(m["roster_id"])
        for pair in by_matchup.values():
            if len(pair) != 2:
                continue
            a, b = pair
            if pts[a] > pts[b]:
                wins[a] += 1
            elif pts[b] > pts[a]:
                wins[b] += 1
            else:
                wins[a] += 0.5
                wins[b] += 0.5

        # all-play
        for rid in pts:
            for other in pts:
                if other == rid:
                    continue
                allplay_games[rid] += 1
                if pts[rid] > pts[other]:
                    allplay_wins[rid] += 1
                elif pts[rid] == pts[other]:
                    allplay_wins[rid] += 0.5

    pf_avg = {r: statistics.mean(weekly[r]) if weekly[r] else 0.0 for r in rids}
    pf_var = {r: statistics.pstdev(weekly[r]) if len(weekly[r]) > 1 else 0.0
              for r in rids}
    coach = {r: (100.0 * sum(weekly[r]) / sum(optimal[r])
                 if sum(optimal[r]) > 0 else 0.0) for r in rids}
    luck = {r: ((wins[r] / len(completed)) /
                (allplay_wins[r] / allplay_games[r])
                if completed and allplay_wins[r] > 0 else 1.0) for r in rids}

    # --- expected win rate --------------------------------------------------
    roster_players = {r["roster_id"]: (r.get("players") or []) for r in rosters}
    rostered = {p for ps in roster_players.values() for p in ps}
    proj_wins = {r: 0.0 for r in rids}

    for wk in future:
        proj = week_projections(season, wk)

        # top free agents at each position, for bye/empty-slot streaming
        waiver = {}
        for pid, pts in proj.items():
            if pid in rostered:
                continue
            p = pos_of.get(pid)
            if p:
                waiver.setdefault(p, []).append(pts)
        waiver = {p: sorted(v, reverse=True)[:3] for p, v in waiver.items()}

        team_proj = {}
        for rid in rids:
            scores = {p: proj.get(p, 0.0) for p in roster_players[rid]}
            team_proj[rid] = best_lineup(scores, pos_of, start_slots, waiver)

        ms = sleeper(f"/league/{LEAGUE_ID}/matchups/{wk}")
        by_matchup = {}
        for m in (ms or []):
            by_matchup.setdefault(m.get("matchup_id"), []).append(m["roster_id"])
        for pair in by_matchup.values():
            if len(pair) != 2:
                continue
            a, b = pair
            pa = phi((team_proj[a] - team_proj[b]) / SIGMA)
            proj_wins[a] += pa
            proj_wins[b] += 1.0 - pa

    exp_wr = {r: (wins[r] + proj_wins[r]) / reg_weeks for r in rids}

    # --- FantasyCalc values -------------------------------------------------
    dyn_rows = fantasycalc(True)
    redraft_rows = fantasycalc(False)

    def value_map(rows):
        out = {}
        for row in rows:
            sid = (row.get("player") or {}).get("sleeperId")
            if sid:
                out[str(sid)] = float(row["value"])
        return out

    dyn_vals, redraft_vals = value_map(dyn_rows), value_map(redraft_rows)

    def top_n_avg(pids, vals):
        v = sorted((vals.get(p, 0.0) for p in pids), reverse=True)
        top = v[:TOP_N_FOR_VALUE]
        return statistics.mean(top) if top else 0.0

    # Full roster incl. taxi/IR, so a stashed stud counts if it's top-22.
    all_owned = {}
    for r in rosters:
        ids = set(r.get("players") or [])
        ids |= set(r.get("reserve") or [])
        ids |= set(r.get("taxi") or [])
        all_owned[r["roster_id"]] = ids

    value = {r: top_n_avg(all_owned[r], dyn_vals) for r in rids}
    redraft_value = {r: top_n_avg(all_owned[r], redraft_vals) for r in rids}
    dyn_diff = {r: value[r] - redraft_value[r] for r in rids}

    # --- age ----------------------------------------------------------------
    age_avg = {}
    for r in rids:
        ages = [players[p]["age"] for p in all_owned[r]
                if p in players and players[p].get("age")]
        age_avg[r] = statistics.mean(ages) if ages else 0.0

    # --- draft capital ------------------------------------------------------
    pick_values = parse_pick_values(dyn_rows)
    rounds = ROOKIE_DRAFT_ROUNDS
    seasons = FUTURE_PICK_SEASONS or [season + 1, season + 2, season + 3]

    valued_rounds = {rnd for (_s, rnd) in pick_values}
    if valued_rounds and rounds > max(valued_rounds):
        print(f"  [debug] ROOKIE_DRAFT_ROUNDS={rounds} but FantasyCalc only "
              f"values through round {max(valued_rounds)}; the rest count as 0")
    traded = sleeper(f"/league/{LEAGUE_ID}/traded_picks")
    holdings = owned_picks(rosters, traded, set(seasons), rounds)

    picks = {}
    missing = set()
    for rid in rids:
        total = 0.0
        for (s, rnd) in holdings.get(rid, []):
            if (s, rnd) in pick_values:
                total += pick_values[(s, rnd)]
            else:
                missing.add((s, rnd))
        picks[rid] = total
    unexpected = {(s, r) for (s, r) in missing
                  if valued_rounds and r <= max(valued_rounds)}
    if unexpected:
        print(f"  [debug] expected but missing FantasyCalc pick values: "
              f"{sorted(unexpected)}")

    # --- composites ---------------------------------------------------------
    def zmap(metric):
        return dict(zip(rids, zscores([metric[r] for r in rids])))

    z = {k: zmap(m) for k, m in {
        "VALUE": value, "PF_AVG": pf_avg, "PF_VAR": pf_var,
        "COACH": coach, "EXP_WR": exp_wr, "LUCK": luck,
        "AGE_AVG": age_avg, "DYN": dyn_diff, "PICKS": picks,
    }.items()}

    # Blend early -> late weights based on how much of the season is in the books.
    t = len(completed) / reg_weeks
    pwr_w = {k: PWR_WEIGHTS_EARLY[k] * (1 - t) + PWR_WEIGHTS_LATE[k] * t
             for k in PWR_WEIGHTS_EARLY}

    pwr_raw = [sum(pwr_w[k] * z[k][r] for k in pwr_w) for r in rids]
    long_raw = [sum(LONG_WEIGHTS[k] * z[k][r] for k in LONG_WEIGHTS) for r in rids]
    pwr = dict(zip(rids, scale_0_100(pwr_raw)))
    long_score = dict(zip(rids, scale_0_100(long_raw)))

    # Overall = simple average of the two 0-100 composites above. Both are
    # already scaled onto the same 0-100 range, so a plain mean is enough --
    # no re-zscoring needed.
    overall_score = {r: (pwr[r] + long_score[r]) / 2 for r in rids}

    # --- output -------------------------------------------------------------
    wk = len(completed)
    have, have_var = bool(completed), len(completed) > 1

    # None = no data yet, rendered as "-" and left blank in the CSV rather than
    # printed as 0.0, which would read like a real measurement.
    cur_raw = {r: {"VALUE": value[r],
                   "PF AVG": pf_avg[r] if have else None,
                   "PF VAR": pf_var[r] if have_var else None,
                   "COACH": coach[r] if have else None,
                   "EXP WR": exp_wr[r],
                   "LUCK": luck[r] if have else None,
                   "PWR": pwr[r]} for r in rids}
    cur_fmt = {"VALUE": ".0f", "PF AVG": ".1f", "PF VAR": ".1f", "COACH": ".1f",
               "EXP WR": ".3f", "LUCK": ".2f", "PWR": ".1f"}

    fut_raw = {r: {"AGE AVG": age_avg[r], "DYN": dyn_diff[r],
                   "PICKS": picks[r], "LONG": long_score[r]} for r in rids}
    fut_fmt = {"AGE AVG": ".1f", "DYN": "+.0f", "PICKS": ".0f", "LONG": ".1f"}

    ovr_raw = {r: {"PWR": pwr[r], "LONG": long_score[r],
                   "OVERALL": overall_score[r]} for r in rids}
    ovr_fmt = {"PWR": ".1f", "LONG": ".1f", "OVERALL": ".1f"}

    def build(order, raws, fmts, cols, kind):
        """Rank the teams (in the given order) and attach movement against the
        last week on disk. `order` is a list of roster_ids already sorted the
        way the caller wants them displayed -- ranks are just 1..N over it."""
        prev_week, by_id, by_owner, by_name = prev_ranks(season, wk, kind)
        rows = []
        for rank, r in enumerate(order, 1):
            disp = {}
            for label, _, _ in cols:
                v = raws[r][label]
                disp[label] = ("-" if v is None else
                               format(v, fmts[label]) +
                               ("%" if label == "COACH" else ""))
            # roster_id is the primary match key -- it doesn't change when a
            # team gets renamed. Owner username is a fallback for the rare
            # case of a recreated league; team name is a last-resort fallback
            # for CSVs written before ROSTER_ID/OWNER were tracked.
            prev = by_id.get(r)
            if prev is None and owners[r]:
                prev = by_owner.get(owners[r].strip().lower())
            if prev is None:
                prev = by_name.get(names[r])
            rows.append({"team": names[r], "roster_id": r, "owner": owners[r],
                         "rank": rank, "raw": raws[r],
                         "disp": disp, "delta": (prev - rank) if prev else 0})
        return prev_week, rows

    # OVERALL ranking, with the running-joke override applied to the sort
    # order (not the scores) before ranks get assigned.
    overall_order = sorted(rids, key=lambda r: -overall_score[r])
    if JOKE_LOSER_ENABLED:
        joke_rid = find_roster_id_by_username(rosters, users, JOKE_LOSER_USERNAME)
        if joke_rid is None:
            print(f"  [debug] JOKE_LOSER_ENABLED but no roster found for "
                  f"username {JOKE_LOSER_USERNAME!r}")
        elif joke_rid in overall_order:
            overall_order = [r for r in overall_order if r != joke_rid] + [joke_rid]

    ovr_prev, ovr_rows = build(overall_order, ovr_raw, ovr_fmt, OVERALL_COLS, "overall")
    pwr_prev, pwr_rows = build(sorted(rids, key=lambda r: -pwr[r]),
                               cur_raw, cur_fmt, CURRENT_COLS, "pwr")
    long_prev, long_rows = build(sorted(rids, key=lambda r: -long_score[r]),
                                 fut_raw, fut_fmt, FUTURE_COLS, "long")

    for title, cols, rows in [("OVERALL", OVERALL_COLS, ovr_rows),
                              ("CURRENT SEASON", CURRENT_COLS, pwr_rows),
                              ("FUTURE SEASONS", FUTURE_COLS, long_rows)]:
        print_table(f"{title}  (through week {wk})",
                    ["TEAM"] + [c[0] for c in cols],
                    [[r["team"][:20]] + [r["disp"][c[0]] for c in cols]
                     for r in rows])

    weights = ", ".join(f"{k}={v:+.2f}" for k, v in pwr_w.items())
    print(f"\nPWR weights (t={t:.2f}): {weights}")

    # --- files --------------------------------------------------------------
    paths = [write_csv(season, wk, "overall", OVERALL_COLS, ovr_rows),
             write_csv(season, wk, "pwr", CURRENT_COLS, pwr_rows),
             write_csv(season, wk, "long", FUTURE_COLS, long_rows)]

    now = datetime.datetime.now()
    stamp = now.strftime("%d %b %Y")
    generated_at = now.strftime("%d %b %Y, %I:%M %p")
    long_weights = ", ".join(f"{k}={v:+.2f}" for k, v in LONG_WEIGHTS.items())
    pages = [
        render_page(league["name"], f"Overall rankings | Week {wk} | {stamp}",
                    OVERALL_COLS, ovr_rows, ovr_prev, "OVERALL",
                    "OVERALL: unweighted average of PWR and LONG."),
        render_page(league["name"], f"Power rankings | Week {wk} | {stamp}",
                    CURRENT_COLS, pwr_rows, pwr_prev, "PWR",
                    f"PWR weighting: {weights}."),
        render_page(league["name"], f"Long-term outlook | Week {wk} | {stamp}",
                    FUTURE_COLS, long_rows, long_prev, "LONG",
                    f"LONG weighting: {long_weights}."),
    ]
    os.makedirs(season_dir(season), exist_ok=True)
    paths.append(write_html(html_path(season, wk), league["name"], pages,
                            generated_at))
    print("\nWrote:\n  " + "\n  ".join(paths))


if __name__ == "__main__":
    main()
