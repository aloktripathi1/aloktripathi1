#!/usr/bin/env python3
"""Generate Snehit's profile telemetry SVGs from GitHub data.

Pulls repos (public + private, with archived/forks excluded), default-branch
commits, and authored PRs via the GitHub GraphQL/REST API (through `gh`),
derives a battery of stats, and renders two static SVG dashboards plus a
JSON snapshot.
"""
import datetime as dt
import json
import math
import os
import statistics
import subprocess
import sys
import xml.sax.saxutils as xml
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
USER = "aloktripathi1"
LOCAL_TZ = dt.timezone(dt.timedelta(hours=5, minutes=30))  # IST for hour-of-day.
TODAY = dt.datetime.now(dt.timezone.utc).date()
SINCE = TODAY - dt.timedelta(days=365)

CATEGORY_KEYWORDS = {
    "AI / Agents": ("agent", "rag", "sachcheck", "sentinel", "pagerzero", "mentor", "reed", "ai"),
    "OSS / Core": ("mem0", "qdrant", "llama", "torchmetrics", "mastra", "letta"),
    "Competitive ML": ("kaggle", "mashup", "leaves", "mcq", "gridlock", "classifier"),
    "Web Apps": ("web", "app", "next", "vue", "react", "sangam", "learnsy", "hub"),
    "Tooling": ("cli", "tui", "tracker", "activitywatch", "dashboard"),
}
IGNORED_LANGUAGES = {"Jupyter Notebook"}
VERBOSE = os.environ.get("VERBOSE") == "1"

# --- Design tokens --------------------------------------------------------
BG = "#0a0a0a"
BG2 = "#050505"
DOT = "#1f1f1f"
HAIRLINE = "#242424"
HAIRLINE_BRIGHT = "#333333"
TEXT_HI = "#f5f5f0"
TEXT = "#c9c9c0"
TEXT_MUTED = "#7a7a72"
TEXT_DIM = "#4a4a44"
# Editorial accent set: signal yellow as primary, warm supports.
LIME = "#F7EE00"
AMBER = "#ff8a3d"
CYAN = "#5fd4d0"
MAGENTA = "#ff5f8f"
VIOLET = "#9d8cff"
RED = "#ff5f5f"
PALETTE = [LIME, CYAN, AMBER, VIOLET, MAGENTA, "#5eead4", "#fde68a", "#94a3b8"]

# GitHub-aligned language colors.
LANG_COLORS = {
    "TypeScript": "#3178c6",
    "JavaScript": "#f1e05a",
    "Python": "#3572A5",
    "Vue": "#41b883",
    "CSS": "#a855f7",
    "HTML": "#e34c26",
    "Kotlin": "#A97BFF",
    "Go": "#00ADD8",
    "Shell": "#89e051",
    "Dockerfile": "#384d54",
    "Mako": "#7e858d",
    "Other": "#475569",
}

WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# ---------------------------------------------------------------------------
# GitHub fetchers
# ---------------------------------------------------------------------------

def gh_json(args):
    proc = subprocess.run(["gh", "api", *args], cwd=ROOT, text=True, capture_output=True)
    if proc.returncode != 0:
        print("failed: gh api " + " ".join(args), file=sys.stderr)
        print(proc.stderr, file=sys.stderr)
        raise SystemExit(proc.returncode)
    return json.loads(proc.stdout)


def gh_graphql(query, **fields):
    args = ["graphql", "-f", f"query={query}"]
    for key, value in fields.items():
        if value is None:
            continue
        args.extend(["-F", f"{key}={value}"])
    return gh_json(args)


def fetch_repos():
    query = """
    query($login: String!, $cursor: String) {
      user(login: $login) {
        repositories(first: 100, after: $cursor, ownerAffiliations: OWNER, isFork: false, orderBy: {field: PUSHED_AT, direction: DESC}) {
          pageInfo { hasNextPage endCursor }
          nodes {
            name
            description
            isPrivate
            isArchived
            pushedAt
            url
            primaryLanguage { name }
            defaultBranchRef { name }
            languages(first: 10, orderBy: {field: SIZE, direction: DESC}) {
              edges { size node { name color } }
            }
          }
        }
      }
    }
    """
    repos = []
    cursor = None
    while True:
        data = gh_graphql(query, login=USER, cursor=cursor)
        conn = data["data"]["user"]["repositories"]
        for node in conn["nodes"]:
            if node["isArchived"] or not node["defaultBranchRef"]:
                continue
            repos.append({
                "name": node["name"],
                "description": node["description"] or "",
                "url": node["url"],
                "pushedAt": node["pushedAt"],
                "isPrivate": node["isPrivate"],
                "primaryLanguage": (node["primaryLanguage"] or {}).get("name"),
                "defaultBranch": node["defaultBranchRef"]["name"],
                "languages": node["languages"]["edges"],
            })
        if not conn["pageInfo"]["hasNextPage"]:
            return repos
        cursor = conn["pageInfo"]["endCursor"]


def fetch_commits(repo):
    query = """
    query($owner: String!, $name: String!, $branch: String!, $since: GitTimestamp!, $cursor: String) {
      repository(owner: $owner, name: $name) {
        ref(qualifiedName: $branch) {
          target {
            ... on Commit {
              history(first: 100, since: $since, after: $cursor) {
                pageInfo { hasNextPage endCursor }
                nodes {
                  committedDate
                  additions
                  deletions
                  author { user { login } }
                }
              }
            }
          }
        }
      }
    }
    """
    commits = []
    cursor = None
    while True:
        if VERBOSE:
            print(f"commits: {repo['name']}", file=sys.stderr)
        data = gh_graphql(
            query,
            owner=USER,
            name=repo["name"],
            branch=repo["defaultBranch"],
            since=SINCE.isoformat() + "T00:00:00Z",
            cursor=cursor,
        )
        ref = data["data"]["repository"]["ref"]
        if not ref:
            return commits
        history = ref["target"]["history"]
        for node in history["nodes"]:
            author = ((node.get("author") or {}).get("user") or {}).get("login")
            if author == USER:
                commits.append(node)
        if not history["pageInfo"]["hasNextPage"]:
            return commits
        cursor = history["pageInfo"]["endCursor"]


def fetch_contribution_total():
    query = """
    query($login: String!, $from: DateTime!, $to: DateTime!) {
      user(login: $login) {
        contributionsCollection(from: $from, to: $to) {
          totalCommitContributions
        }
      }
    }
    """
    data = gh_graphql(
        query,
        login=USER,
        **{"from": SINCE.isoformat() + "T00:00:00Z", "to": TODAY.isoformat() + "T23:59:59Z"},
    )
    return data["data"]["user"]["contributionsCollection"]["totalCommitContributions"]


def fetch_prs():
    query = f"author:{USER} type:pr created:>={SINCE.isoformat()}"
    items = []
    page = 1
    while True:
        data = gh_json(["search/issues", "-X", "GET", "-f", f"q={query}", "-f", "per_page=100", "-f", f"page={page}"])
        items.extend(data.get("items", []))
        if len(items) >= data.get("total_count", 0) or not data.get("items"):
            return items
        page += 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def esc(value):
    return xml.escape(str(value), {'"': "&quot;"})


def pct(value, digits=0):
    return f"{value:.{digits}f}%"


def fmt_num(value):
    return f"{int(value):,}"


def percentile(values, p):
    if not values:
        return 0
    vals = sorted(values)
    k = (len(vals) - 1) * (p / 100)
    lower = math.floor(k)
    upper = math.ceil(k)
    if lower == upper:
        return vals[int(k)]
    return vals[lower] * (upper - k) + vals[upper] * (k - lower)


def iso_week_start(day):
    return day - dt.timedelta(days=day.weekday())


def repo_category(repo):
    text = " ".join([repo["name"], repo.get("description") or "", repo.get("primaryLanguage") or ""]).lower()
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            return category
    return "Other"


def streaks(active_days):
    current = 0
    cursor = TODAY
    # Grace period: a streak stays alive until the end of the next day, so an
    # early-morning run that finds no commit *yet today* counts from yesterday
    # instead of resetting to 0.
    if cursor not in active_days:
        cursor -= dt.timedelta(days=1)
    while cursor in active_days:
        current += 1
        cursor -= dt.timedelta(days=1)
    longest = 0
    run = 0
    for offset in range((TODAY - SINCE).days + 1):
        day = SINCE + dt.timedelta(days=offset)
        if day in active_days:
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    return current, longest


# ---------------------------------------------------------------------------
# Collection + derivation
# ---------------------------------------------------------------------------

def collect():
    repos = fetch_repos()
    daily = Counter()
    repo_daily = defaultdict(Counter)
    hour_dist = Counter()
    additions = 0
    deletions = 0
    language_bytes = Counter()
    category_commits = Counter()

    for repo in repos:
        for edge in repo["languages"]:
            lang = edge["node"]["name"]
            if lang not in IGNORED_LANGUAGES:
                language_bytes[lang] += edge["size"]
        commits = fetch_commits(repo)
        category = repo_category(repo)
        for commit in commits:
            ts = dt.datetime.fromisoformat(commit["committedDate"].replace("Z", "+00:00"))
            local_ts = ts.astimezone(LOCAL_TZ)
            day = local_ts.date()
            daily[day] += 1
            hour_dist[local_ts.hour] += 1
            repo_daily[repo["name"]][day] += 1
            category_commits[category] += 1
            additions += commit.get("additions") or 0
            deletions += commit.get("deletions") or 0

    prs = fetch_prs()
    pr_weekly = Counter()
    merged = 0
    external_repo_counts = Counter()
    external_repo_merged = Counter()
    for pr in prs:
        created = dt.datetime.fromisoformat(pr["created_at"].replace("Z", "+00:00")).date()
        pr_weekly[iso_week_start(created)] += 1
        is_merged = bool(pr.get("pull_request", {}).get("merged_at"))
        if is_merged:
            merged += 1
        repo_url = pr.get("repository_url", "")
        full_name = "/".join(repo_url.rstrip("/").split("/")[-2:]) if repo_url else ""
        owner = full_name.split("/")[0] if full_name else ""
        if owner and owner.lower() != USER.lower():
            external_repo_counts[full_name] += 1
            if is_merged:
                external_repo_merged[full_name] += 1
    pr_active_weeks = sum(1 for v in pr_weekly.values() if v > 0)
    external_prs_total = sum(external_repo_counts.values())
    external_prs_merged = sum(external_repo_merged.values())
    external_repos_touched = sorted(
        external_repo_counts.keys(),
        key=lambda r: (-external_repo_merged[r], -external_repo_counts[r]),
    )
    external_top = [(r, external_repo_merged[r], external_repo_counts[r]) for r in external_repos_touched[:6]]

    days = [SINCE + dt.timedelta(days=i) for i in range((TODAY - SINCE).days + 1)]
    daily_values = [daily[day] for day in days]
    active_days_set = {d for d, c in daily.items() if c > 0}
    active_values = [v for v in daily_values if v > 0]
    current_streak, longest_streak = streaks(active_days_set)

    # Weekday pulse: mean commits per occurrence of each weekday.
    weekday_total = [0] * 7
    weekday_count = [0] * 7
    weekday_active = [0] * 7
    for day in days:
        wd = day.weekday()
        weekday_count[wd] += 1
        weekday_total[wd] += daily[day]
        if daily[day] > 0:
            weekday_active[wd] += 1
    weekday_mean = [t / c if c else 0 for t, c in zip(weekday_total, weekday_count)]
    best_weekday = max(range(7), key=lambda i: weekday_mean[i])
    quiet_weekday = min(range(7), key=lambda i: weekday_mean[i])

    # Monthly commit totals over the trailing 12 calendar months.
    monthly = []
    cursor_month = dt.date(TODAY.year, TODAY.month, 1)
    for _ in range(12):
        start = cursor_month
        if start.month == 12:
            next_month = dt.date(start.year + 1, 1, 1)
        else:
            next_month = dt.date(start.year, start.month + 1, 1)
        total = sum(c for d, c in daily.items() if start <= d < next_month)
        monthly.append((start.isoformat(), total))
        if start.month == 1:
            cursor_month = dt.date(start.year - 1, 12, 1)
        else:
            cursor_month = dt.date(start.year, start.month - 1, 1)
    monthly.reverse()

    def repo_sum(name, days_back):
        start = TODAY - dt.timedelta(days=days_back - 1)
        return sum(c for d, c in repo_daily[name].items() if d >= start)

    top_week = [(n, c) for n, c in sorted(((n, repo_sum(n, 7)) for n in repo_daily), key=lambda x: x[1], reverse=True) if c > 0][:5]
    top_month = [(n, c) for n, c in sorted(((n, repo_sum(n, 30)) for n in repo_daily), key=lambda x: x[1], reverse=True) if c > 0][:5]
    top_year = sorted(((n, sum(repo_daily[n].values())) for n in repo_daily), key=lambda x: x[1], reverse=True)
    top_year = [(n, c) for n, c in top_year if c > 0]

    # Velocity trend: last 30d versus the prior 30d.
    last_30 = sum(c for d, c in daily.items() if d > TODAY - dt.timedelta(days=30))
    prev_30 = sum(c for d, c in daily.items() if TODAY - dt.timedelta(days=60) < d <= TODAY - dt.timedelta(days=30))
    velocity_trend = ((last_30 - prev_30) / prev_30 * 100) if prev_30 else 0.0
    momentum_7d = sum(c for d, c in daily.items() if d > TODAY - dt.timedelta(days=7))

    # Largest single day, burst days, longest quiet stretch.
    biggest_day = max(daily.items(), key=lambda x: x[1], default=(TODAY, 0))
    burst_threshold = percentile(active_values, 90) if active_values else 0
    burst_days = sum(1 for d, c in daily.items() if c >= max(1, burst_threshold))
    longest_gap = 0
    gap = 0
    for d in days:
        if daily[d] == 0:
            gap += 1
            longest_gap = max(longest_gap, gap)
        else:
            gap = 0
    # Mean gap between consecutive active days.
    sorted_active = sorted(active_days_set)
    if len(sorted_active) >= 2:
        gaps = [(sorted_active[i] - sorted_active[i - 1]).days for i in range(1, len(sorted_active))]
        mean_gap = statistics.mean(gaps)
    else:
        mean_gap = 0.0

    # Hour-of-day analysis (24 buckets, IST).
    hour_values = [hour_dist.get(h, 0) for h in range(24)]
    peak_hour = max(range(24), key=lambda h: hour_values[h]) if sum(hour_values) else 0
    # Group hours into night/morning/afternoon/evening buckets.
    hour_buckets = {
        "night (00–06)": sum(hour_values[0:6]),
        "morning (06–12)": sum(hour_values[6:12]),
        "afternoon (12–18)": sum(hour_values[12:18]),
        "evening (18–24)": sum(hour_values[18:24]),
    }

    # Shipping pulse: distinct repos touched.
    shipping_7d = sum(1 for n in repo_daily if repo_sum(n, 7) > 0)
    shipping_30d = sum(1 for n in repo_daily if repo_sum(n, 30) > 0)

    # Repo concentration & entropy across the 365d commit pool.
    year_totals = [c for _, c in top_year]
    year_sum = sum(year_totals) or 1
    focus_top3 = sum(year_totals[:3]) / year_sum * 100
    hhi = sum((c / year_sum) ** 2 for c in year_totals)
    if len(year_totals) > 1:
        entropy = -sum((c / year_sum) * math.log2(c / year_sum) for c in year_totals if c > 0)
        entropy_norm = entropy / math.log2(len(year_totals))
    else:
        entropy = 0.0
        entropy_norm = 0.0

    pr_week_values = list(pr_weekly.values()) or [0]
    # Trailing 26-week PR series for the bar chart.
    week_cursor = iso_week_start(TODAY)
    pr_recent = []
    for i in range(26):
        wk = week_cursor - dt.timedelta(weeks=25 - i)
        pr_recent.append((wk.isoformat(), pr_weekly.get(wk, 0)))

    momentum_score = min(100, round(
        len(active_values) / len(days) * 100 * 0.40
        + min(sum(daily_values) / 18, 35)
        + min(len(prs) / 4, 15)
        + min(max(velocity_trend, 0) / 4, 10)
    ))

    private_count = sum(1 for r in repos if r.get("isPrivate"))
    public_count = len(repos) - private_count

    # Code growth: additions vs deletions ratio (capped formatting elsewhere).
    code_total = additions + deletions
    growth_ratio = (additions / code_total * 100) if code_total else 0
    avg_commit_size = (code_total / sum(daily_values)) if sum(daily_values) else 0

    # PR momentum trend (last 4 weeks vs prior 4 weeks).
    this_4w = sum(c for d, c in daily.items() if d > TODAY - dt.timedelta(days=28))
    prev_4w = sum(c for d, c in daily.items() if TODAY - dt.timedelta(days=56) < d <= TODAY - dt.timedelta(days=28))
    pr_this_4w = sum(v for w, v in pr_weekly.items() if w > TODAY - dt.timedelta(days=28))
    pr_prev_4w = sum(v for w, v in pr_weekly.items() if TODAY - dt.timedelta(days=56) < w <= TODAY - dt.timedelta(days=28))
    pr_trend = ((pr_this_4w - pr_prev_4w) / pr_prev_4w * 100) if pr_prev_4w else 0

    return {
        "generated": TODAY.isoformat(),
        "windowDays": len(days),
        "repos": repos,
        "reposAnalyzed": len(repo_daily),
        "reposPublic": public_count,
        "reposPrivate": private_count,
        "biggestDay": {"date": biggest_day[0].isoformat(), "commits": biggest_day[1]},
        "burstDays": burst_days,
        "burstThreshold": burst_threshold,
        "longestGap": longest_gap,
        "meanGapDays": mean_gap,
        "hourDistribution": hour_values,
        "peakHour": peak_hour,
        "hourBuckets": hour_buckets,
        "additionsPct": growth_ratio,
        "avgCommitSize": avg_commit_size,
        "prActiveWeeks": pr_active_weeks,
        "prThis4w": pr_this_4w,
        "prPrev4w": pr_prev_4w,
        "prTrendPct": pr_trend,
        "daily": {d.isoformat(): daily[d] for d in days},
        "activeDaysPct": len(active_values) / len(days) * 100,
        "commitTotal": fetch_contribution_total(),
        "commitMean": statistics.mean(daily_values),
        "commitMedian": statistics.median(daily_values),
        "commitP95": percentile(daily_values, 95),
        "commitActiveMean": statistics.mean(active_values) if active_values else 0,
        "commitMax": max(daily_values) if daily_values else 0,
        "currentStreak": current_streak,
        "longestStreak": longest_streak,
        "additions": additions,
        "deletions": deletions,
        "prs": len(prs),
        "mergedPrs": merged,
        "externalPrsTotal": external_prs_total,
        "externalPrsMerged": external_prs_merged,
        "externalReposCount": len(external_repo_counts),
        "externalTop": external_top,
        "prMergeRatio": (merged / len(prs) * 100) if prs else 0,
        "prWeeklyMean": statistics.mean(pr_week_values),
        "prWeeklyMedian": statistics.median(pr_week_values),
        "prWeeklyP95": percentile(pr_week_values, 95),
        "prWeeklyMax": max(pr_week_values) if pr_week_values else 0,
        "prRecent": pr_recent,
        "topWeek": top_week,
        "topMonth": top_month,
        "topYear": top_year,
        "languages": language_bytes.most_common(),
        "categories": category_commits.most_common(),
        "weekdayMean": weekday_mean,
        "weekdayActive": weekday_active,
        "bestWeekday": best_weekday,
        "quietWeekday": quiet_weekday,
        "monthly": monthly,
        "velocity30d": last_30,
        "velocityPrev30d": prev_30,
        "velocityTrendPct": velocity_trend,
        "momentum7d": momentum_7d,
        "momentumScore": momentum_score,
        "shipping7d": shipping_7d,
        "shipping30d": shipping_30d,
        "focusTop3Pct": focus_top3,
        "hhi": hhi,
        "entropy": entropy,
        "entropyNormalized": entropy_norm,
    }


# ---------------------------------------------------------------------------
# Render helpers
# ---------------------------------------------------------------------------

def shell(W, H, defs_extra=""):
    """Outer container + subtle dotted background; no inner panels."""
    return f"""<rect width="{W}" height="{H}" rx="10" fill="url(#bgGrad)"/>
<rect width="{W}" height="{H}" rx="10" fill="url(#dots)"/>
<rect x="0.5" y="0.5" width="{W - 1}" height="{H - 1}" rx="9.5" fill="none" stroke="{HAIRLINE_BRIGHT}"/>
{defs_extra}"""


def defs():
    return f"""<defs>
  <linearGradient id="bgGrad" x1="0" x2="1" y1="0" y2="1">
    <stop offset="0" stop-color="{BG}"/>
    <stop offset="1" stop-color="{BG2}"/>
  </linearGradient>
  <pattern id="dots" width="20" height="20" patternUnits="userSpaceOnUse">
    <circle cx="1" cy="1" r="0.9" fill="{DOT}" opacity="0.55"/>
  </pattern>
  <linearGradient id="limeFade" x1="0" x2="0" y1="0" y2="1">
    <stop offset="0" stop-color="{LIME}" stop-opacity="0.55"/>
    <stop offset="1" stop-color="{LIME}" stop-opacity="0"/>
  </linearGradient>
  <linearGradient id="cyanFade" x1="0" x2="0" y1="0" y2="1">
    <stop offset="0" stop-color="{CYAN}" stop-opacity="0.45"/>
    <stop offset="1" stop-color="{CYAN}" stop-opacity="0"/>
  </linearGradient>
  <radialGradient id="glowSpot" cx="0.5" cy="0.5" r="0.5">
    <stop offset="0" stop-color="{LIME}" stop-opacity="0.28"/>
    <stop offset="1" stop-color="{LIME}" stop-opacity="0"/>
  </radialGradient>
  <filter id="softGlow" x="-60%" y="-60%" width="220%" height="220%">
    <feGaussianBlur stdDeviation="5" result="blur"/>
    <feMerge>
      <feMergeNode in="blur"/>
      <feMergeNode in="SourceGraphic"/>
    </feMerge>
  </filter>
</defs>"""


def corner_ticks(W, H, m=20, size=12, color=None):
    color = color or HAIRLINE_BRIGHT
    corners = [
        (m, m, 1, 1), (W - m, m, -1, 1), (m, H - m, 1, -1), (W - m, H - m, -1, -1),
    ]
    out = []
    for x, y, sx, sy in corners:
        out.append(f'<path d="M{x} {y + size * sy} V{y} H{x + size * sx}" fill="none" stroke="{color}" stroke-width="1.2"/>')
    return "\n".join(out)


def donut_ring(cx, cy, r, thickness, segments, glow=False):
    """segments: list of (value, color). Draws a ring starting at 12 o'clock, clockwise."""
    total = sum(v for v, _ in segments) or 1
    circumference = 2 * math.pi * r
    out = [f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{HAIRLINE}" stroke-width="{thickness}"/>']
    cumulative = 0.0
    for value, color in segments:
        frac = value / total
        dash = frac * circumference
        gap = circumference - dash
        offset = -cumulative
        glow_attr = ' filter="url(#softGlow)"' if glow else ""
        out.append(
            f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{color}" stroke-width="{thickness}" '
            f'stroke-dasharray="{dash:.2f} {gap:.2f}" stroke-dashoffset="{offset:.2f}" '
            f'transform="rotate(-90 {cx} {cy})" stroke-linecap="butt"{glow_attr}/>'
        )
        cumulative += dash
    return "\n".join(out)


def ledger_row(x, y, w, label, value, color=None, sub=None):
    color = color or TEXT_HI
    parts = [
        f'<text x="{x}" y="{y}" fill="{TEXT}" font-size="12" font-weight="600">{esc(label)}</text>',
        f'<line x1="{x + 8 + len(label) * 7.3:.1f}" y1="{y - 4}" x2="{x + w - 60}" y2="{y - 4}" stroke="{HAIRLINE}" stroke-width="1" stroke-dasharray="1.5 4"/>',
        f'<text x="{x + w}" y="{y}" fill="{color}" font-size="13" font-weight="700" text-anchor="end">{esc(value)}</text>',
    ]
    if sub:
        parts.append(f'<text x="{x + w}" y="{y + 15}" fill="{TEXT_DIM}" font-size="10" text-anchor="end">{esc(sub)}</text>')
    return "\n".join(parts)


def section_label(x, y, text, tag=None, width=None):
    """Eyebrow heading + optional right-aligned tag, with a hairline."""
    out = [f'<text x="{x}" y="{y}" fill="{TEXT_HI}" font-size="11" font-weight="700" letter-spacing="2">{esc(text.upper())}</text>']
    if tag:
        out.append(f'<text x="{x + (width or 0)}" y="{y}" fill="{TEXT_DIM}" font-size="10.5" letter-spacing="1" text-anchor="end">{esc(tag.upper())}</text>')
    return "\n".join(out)


def hairline(x1, y, x2, color=HAIRLINE):
    return f'<line x1="{x1}" y1="{y}" x2="{x2}" y2="{y}" stroke="{color}"/>'


def number_block(x, y, label, value, sub=None, accent=TEXT_HI, label_color=TEXT_MUTED, big=34):
    parts = [f'<text x="{x}" y="{y}" fill="{label_color}" font-size="10" font-weight="700" letter-spacing="1.5">{esc(label.upper())}</text>']
    parts.append(f'<text x="{x}" y="{y + big - 4}" fill="{accent}" font-size="{big}" font-weight="800" letter-spacing="-1">{esc(value)}</text>')
    if sub:
        parts.append(f'<text x="{x}" y="{y + big + 14}" fill="{TEXT_DIM}" font-size="11">{esc(sub)}</text>')
    return "\n".join(parts)


# --- Squarified treemap ---------------------------------------------------

def squarify(items, x, y, w, h):
    """Squarified treemap.

    items: list of (label, value) sorted descending by value.
    Returns: list of (label, value, x, y, w, h) tuples (one per item).
    """
    sizes = [v for _, v in items]
    total = sum(sizes)
    if total == 0 or not sizes:
        return []
    scale = (w * h) / total
    scaled = [s * scale for s in sizes]
    results = [None] * len(scaled)
    pending = list(range(len(scaled)))  # original indices

    def worst(row_areas, side):
        s = sum(row_areas)
        return max(
            (side * side * max(row_areas)) / (s * s),
            (s * s) / (side * side * min(row_areas)),
        )

    def place(row_idx, x, y, w, h):
        # Lay row along the long edge; fill the short edge with the row.
        row_areas = [scaled[i] for i in row_idx]
        s = sum(row_areas)
        if w >= h:
            row_w = s / h
            cy = y
            for i, area in zip(row_idx, row_areas):
                rh = area / row_w if row_w else 0
                results[i] = (x, cy, row_w, rh)
                cy += rh
            return x + row_w, y, w - row_w, h
        else:
            row_h = s / w
            cx = x
            for i, area in zip(row_idx, row_areas):
                rw = area / row_h if row_h else 0
                results[i] = (cx, y, rw, row_h)
                cx += rw
            return x, y + row_h, w, h - row_h

    def go(remaining, row, x, y, w, h):
        if not remaining:
            if row:
                place(row, x, y, w, h)
            return
        side = min(w, h)
        if not row:
            go(remaining[1:], [remaining[0]], x, y, w, h)
            return
        cur_areas = [scaled[i] for i in row]
        new_areas = cur_areas + [scaled[remaining[0]]]
        if worst(new_areas, side) <= worst(cur_areas, side):
            go(remaining[1:], row + [remaining[0]], x, y, w, h)
        else:
            nx, ny, nw, nh = place(row, x, y, w, h)
            go(remaining, [], nx, ny, nw, nh)

    go(pending, [], x, y, w, h)
    out = []
    for i, (label, value) in enumerate(items):
        rx, ry, rw, rh = results[i] or (x, y, 0, 0)
        out.append((label, value, rx, ry, rw, rh))
    return out

# Telemetry SVG — flat editorial layout, no heatmap
# ---------------------------------------------------------------------------

def render_profile(stats):
    W = 1100
    pad = 56

    monthly = stats["monthly"]

    # ------------------------------------------------------------------
    # Eyebrow header
    # ------------------------------------------------------------------
    eb_y = 56
    header = f"""
    <text x="{pad}" y="{eb_y}" fill="{TEXT_HI}" font-size="26" font-weight="800" letter-spacing="-0.5" class="sans">build telemetry</text>
    <text x="{pad}" y="{eb_y + 20}" fill="{TEXT_DIM}" font-size="11" letter-spacing="1">AI/ML ENGINEERING · AGENTIC SYSTEMS · {esc(stats["generated"]).upper()}</text>
    """
    hairline_y = eb_y + 30
    hero_y = hairline_y + 10

    # ------------------------------------------------------------------
    # Hero triptych: streak / OSS merge donut / trajectory — equal-height,
    # bottom-aligned cards in a single row.
    # ------------------------------------------------------------------
    gap = 32
    avail = W - pad * 2 - gap * 2
    col1_w = avail * 0.30
    col2_w = avail * 0.20
    col3_w = avail * 0.50
    c1_x = pad
    c2_x = c1_x + col1_w + gap
    c3_x = c2_x + col2_w + gap

    inner = 22
    card_top = hairline_y + 20
    card_h = 146
    card_bottom = card_top + card_h
    label_y = card_top + 16
    caption_y = card_top + 124

    cards_bg = "\n".join(
        f'<rect x="{cx:.1f}" y="{card_top}" width="{cw:.1f}" height="{card_h}" rx="12" fill="#ffffff" fill-opacity="0.015" stroke="{HAIRLINE}" stroke-opacity="0.4"/>'
        for cx, cw in [(c1_x, col1_w), (c2_x, col2_w), (c3_x, col3_w)]
    )

    # -- Col 1: streak, glowing display number --------------------------
    streak_val = f'{stats["currentStreak"]}'
    x1 = c1_x + inner
    col1 = f"""
    <text x="{x1}" y="{label_y}" fill="{TEXT_MUTED}" font-size="10" font-weight="700" letter-spacing="2">CURRENT STREAK</text>
    <text x="{x1}" y="{card_top + 98}" fill="{LIME}" font-size="96" font-weight="900" letter-spacing="-4" class="sans">{streak_val}<tspan font-size="34" fill="{TEXT_HI}" font-weight="800">d</tspan></text>
    <text x="{x1}" y="{caption_y}" fill="{TEXT}" font-size="11">longest {stats["longestStreak"]}d · quiet stretch {stats["longestGap"]}d</text>
    """

    # -- Col 2: PR merge-rate donut --------------------------------------
    merge_pct = stats["prMergeRatio"]
    r2 = 38
    x2 = c2_x + inner
    donut_cx = x2 + r2 + 10 / 2
    donut_cy = card_top + 66
    ring = donut_ring(donut_cx, donut_cy, r2, 10, [(merge_pct, LIME), (max(0, 100 - merge_pct), HAIRLINE)], glow=False)
    col2 = f"""
    <text x="{x2}" y="{label_y}" fill="{TEXT_MUTED}" font-size="10" font-weight="700" letter-spacing="2">PR MERGE RATE</text>
    {ring}
    <text x="{donut_cx}" y="{donut_cy + 7}" fill="{TEXT_HI}" font-size="24" font-weight="900" text-anchor="middle" class="sans">{merge_pct:.0f}%</text>
    <text x="{x2}" y="{caption_y}" fill="{TEXT}" font-size="11">{stats["mergedPrs"]}/{stats["prs"]} merged · {stats["prActiveWeeks"]} wks</text>
    """

    # -- Col 3: 12-month trajectory (area chart) -------------------------
    x3 = c3_x + inner
    chart_x = x3
    chart_y = card_top + 34
    chart_w = col3_w - inner * 2
    axis_y = caption_y
    chart_h = (axis_y - 16) - chart_y
    max_m = max((v for _, v in monthly), default=1) or 1
    pts = []
    for i, (_, v) in enumerate(monthly):
        px = chart_x + (i / max(1, len(monthly) - 1)) * chart_w
        py = chart_y + chart_h - (v / max_m) * chart_h
        pts.append((px, py))
    poly = " ".join(f"{x:.2f},{y:.2f}" for x, y in pts)
    area = f"{chart_x},{chart_y + chart_h} {poly} {chart_x + chart_w},{chart_y + chart_h}"
    axis = []
    for i, (mk, _) in enumerate(monthly):
        if i % 2 != 0:
            continue
        px = chart_x + (i / max(1, len(monthly) - 1)) * chart_w
        m = MONTH_NAMES[int(mk.split("-")[1]) - 1]
        axis.append(f'<text x="{px:.1f}" y="{axis_y}" fill="{TEXT_DIM}" font-size="9" text-anchor="middle">{m}</text>')
    peak_idx = max(range(len(monthly)), key=lambda i: monthly[i][1])
    cur_idx = len(monthly) - 1
    annots = []
    for idx, dot_color, text_color, label in [(peak_idx, AMBER, AMBER, f"peak · {monthly[peak_idx][1]}"), (cur_idx, LIME, TEXT_HI, f"now · {monthly[cur_idx][1]}")]:
        if monthly[idx][1] == 0:
            continue
        px, py = pts[idx]
        annots.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="3.5" fill="{dot_color}" stroke="{BG}" stroke-width="1.5"/>')
        if idx == cur_idx:
            # Pin the "now" label outside the chart's right edge so it never
            # collides with the peak annotation regardless of data shape.
            annots.append(f'<text x="{chart_x + chart_w + 10:.1f}" y="{py + 4:.1f}" fill="{text_color}" font-size="9.5" font-weight="700" text-anchor="start">{label}</text>')
        else:
            annots.append(f'<text x="{px + 6:.1f}" y="{py - 8:.1f}" fill="{text_color}" font-size="9.5" font-weight="700" text-anchor="start">{label}</text>')
    grid = [
        f'<line x1="{chart_x}" y1="{chart_y + chart_h}" x2="{chart_x + chart_w}" y2="{chart_y + chart_h}" stroke="{HAIRLINE}"/>',
        f'<line x1="{chart_x}" y1="{chart_y}" x2="{chart_x + chart_w}" y2="{chart_y}" stroke="{HAIRLINE}" stroke-dasharray="2 4"/>',
    ]
    col3 = f"""
    <text x="{x3}" y="{label_y}" fill="{TEXT_MUTED}" font-size="10" font-weight="700" letter-spacing="2">12-MONTH TRAJECTORY</text>
    {chr(10).join(grid)}
    <polygon points="{area}" fill="url(#limeFade)"/>
    <polyline points="{poly}" fill="none" stroke="{LIME}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
    {chr(10).join(annots)}
    {chr(10).join(axis)}
    """

    hero_bottom = card_bottom

    # ------------------------------------------------------------------
    # Vitals ledger strip (single row, dotted leaders)
    # ------------------------------------------------------------------
    led_y = hero_bottom + 56
    trend = stats["velocityTrendPct"]
    trend_color = LIME if trend >= 0 else RED
    trend_sign = "+" if trend >= 0 else ""
    led_items = [
        ("ALL CONTRIBUTIONS", fmt_num(stats["commitTotal"]), "GitHub calendar", LIME),
        ("30D VELOCITY", fmt_num(stats["velocity30d"]), f'{"▲" if trend >= 0 else "▼"} {trend_sign}{trend:.0f}% vs prior 30', trend_color),
        ("ACTIVE DAYS", pct(stats["activeDaysPct"]), f'mean gap {stats["meanGapDays"]:.1f}d', CYAN),
        ("OSS PRS MERGED", fmt_num(stats["externalPrsMerged"]), f'across {stats["externalReposCount"]} external repos', VIOLET),
        ("PULL REQUESTS", fmt_num(stats["prs"]), f'{stats["prMergeRatio"]:.0f}% merged', MAGENTA),
    ]
    seg_w = (W - pad * 2) / len(led_items)
    led_parts = [section_label(pad, led_y - 16, "Vitals", width=W - pad * 2)]
    for i, (label, value, sub, color) in enumerate(led_items):
        lx = pad + i * seg_w
        led_parts.append(f'<text x="{lx:.1f}" y="{led_y}" fill="{TEXT_DIM}" font-size="10" font-weight="700" letter-spacing="1.5">{esc(label)}</text>')
        led_parts.append(f'<text x="{lx:.1f}" y="{led_y + 20}" fill="{color}" font-size="24" font-weight="800" letter-spacing="-1">{esc(value)}</text>')
        led_parts.append(f'<text x="{lx:.1f}" y="{led_y + 38}" fill="{TEXT_DIM}" font-size="11">{esc(sub)}</text>')
        if i > 0:
            led_parts.append(f'<line x1="{lx - 10:.1f}" y1="{led_y - 4}" x2="{lx - 10:.1f}" y2="{led_y + 50}" stroke="{HAIRLINE}"/>')
    led_bottom = led_y + 50

    # ------------------------------------------------------------------
    # Language mix donut (left) + Work categories ledger (right)
    # ------------------------------------------------------------------
    mid_y = led_bottom + 46
    half_w = (W - pad * 2 - 56) / 2
    left_x = pad
    right_x = pad + half_w + 56

    langs = stats["languages"][:6]
    total_lang = sum(v for _, v in langs) or 1
    lang_cx = left_x + 78
    lang_cy = mid_y + 90
    lang_segments = [(v, LANG_COLORS.get(name, PALETTE[i % len(PALETTE)])) for i, (name, v) in enumerate(langs)]
    lang_ring = donut_ring(lang_cx, lang_cy, 62, 18, lang_segments)
    lang_legend = []
    for i, (name, v) in enumerate(langs):
        ly = mid_y + 24 + i * 22
        color = LANG_COLORS.get(name, PALETTE[i % len(PALETTE)])
        share = v / total_lang * 100
        lang_legend.append(f'<rect x="{left_x + 172}" y="{ly - 9}" width="9" height="9" rx="2" fill="{color}"/>')
        lang_legend.append(f'<text x="{left_x + 188}" y="{ly}" fill="{TEXT}" font-size="11.5">{esc(name)}</text>')
        lang_legend.append(f'<text x="{left_x + half_w}" y="{ly}" fill="{TEXT_MUTED}" font-size="11" text-anchor="end">{share:.0f}%</text>')

    left_col = f"""
    {section_label(left_x, mid_y, "Language mix", width=half_w)}
    {lang_ring}
    <text x="{lang_cx}" y="{lang_cy + 9}" fill="{TEXT_HI}" font-size="24" font-weight="900" text-anchor="middle" class="sans">{len(stats["languages"])}</text>
    <text x="{lang_cx}" y="{lang_cy + 24}" fill="{TEXT_DIM}" font-size="8.5" text-anchor="middle" letter-spacing="1">LANGS</text>
    {chr(10).join(lang_legend)}
    """

    cats = stats["categories"][:5]
    total_cat = sum(v for _, v in cats) or 1
    focus_label = "focused" if stats["focusTop3Pct"] >= 60 else ("balanced" if stats["focusTop3Pct"] >= 40 else "scattered")
    cat_rows = []
    for i, (name, v) in enumerate(cats):
        ry = mid_y + 30 + i * 30
        share = v / total_cat
        color = PALETTE[i % len(PALETTE)]
        bw = share * (half_w - 100)
        cat_rows.append(f'<text x="{right_x}" y="{ry}" fill="{TEXT}" font-size="11.5" font-weight="600">{esc(name)}</text>')
        cat_rows.append(f'<rect x="{right_x}" y="{ry + 6}" width="{half_w - 100:.1f}" height="5" rx="2.5" fill="{HAIRLINE}"/>')
        cat_rows.append(f'<rect x="{right_x}" y="{ry + 6}" width="{bw:.1f}" height="5" rx="2.5" fill="{color}"/>')
        cat_rows.append(f'<text x="{right_x + half_w}" y="{ry}" fill="{TEXT_MUTED}" font-size="11" text-anchor="end">{share * 100:.0f}%</text>')

    right_col = f"""
    {section_label(right_x, mid_y, "Work categories", tag=f'{focus_label} · top-3 {stats["focusTop3Pct"]:.0f}%', width=half_w)}
    {chr(10).join(cat_rows)}
    """

    mid_bottom = mid_y + max(24 + len(langs) * 22, 30 + len(cats) * 30)

    # ------------------------------------------------------------------
    # Bottom ledger: top projects + OSS contributions
    # ------------------------------------------------------------------
    bot_y = mid_bottom + 44
    bot_labels = [
        section_label(left_x, bot_y - 18, "Top projects · 7d", width=half_w),
        section_label(right_x, bot_y - 18, "OSS contributions", tag=f'{stats["externalPrsMerged"]}/{stats["externalPrsTotal"]} merged', width=half_w),
    ]
    proj_items = stats["topWeek"][:4] or [("no commits", 0)]
    oss_items = stats["externalTop"][:4] or [("no external PRs yet", 0, 0)]
    bot_rows = []
    for i, (name, value) in enumerate(proj_items):
        ry = bot_y + 8 + i * 24
        bot_rows.append(ledger_row(left_x, ry, half_w, name, str(value), color=LIME))
    for i, item in enumerate(oss_items):
        ry = bot_y + 8 + i * 24
        if len(item) == 3:
            repo, merged_n, total_n = item
            status = "merged" if merged_n else "open"
            color = LIME if merged_n else TEXT_DIM
            bot_rows.append(ledger_row(right_x, ry, half_w, repo, f'{merged_n}/{total_n} {status}', color=color))
        else:
            bot_rows.append(ledger_row(right_x, ry, half_w, item[0], "", color=TEXT_DIM))

    H = int(bot_y + 8 + 4 * 24 + 40)

    return f"""<svg width="{W}" height="{H}" viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" role="img" aria-labelledby="ttl desc">
  <title id="ttl">Alok build telemetry</title>
  <desc id="desc">Streak, PR merge rate, twelve-month trajectory, vitals, language mix, work categories, top projects, and OSS contributions.</desc>
  <style>text {{ font-family: 'JetBrains Mono', 'SF Mono', ui-monospace, Menlo, monospace; }} .sans {{ font-family: 'Inter', ui-sans-serif, system-ui, sans-serif; }}</style>
  {defs()}
  {shell(W, H)}
  {corner_ticks(W, H)}

  <!-- Header -->
  {header}
  {hairline(pad, hairline_y, W - pad, HAIRLINE_BRIGHT)}

  <!-- Hero triptych -->
  {col1}
  {col2}
  {col3}

  <!-- Vitals ledger -->
  {hairline(pad, led_y - 40, W - pad)}
  {chr(10).join(led_parts)}

  <!-- Language mix + work categories -->
  {hairline(pad, mid_y - 16, W - pad)}
  {left_col}
  {right_col}

  <!-- Bottom: top projects + OSS -->
  {hairline(pad, bot_y - 30, W - pad)}
  {chr(10).join(bot_labels)}
  {chr(10).join(bot_rows)}
</svg>
"""

# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main():
    ASSETS.mkdir(exist_ok=True)
    stats = collect()
    (ASSETS / "profile-telemetry.json").write_text(json.dumps(stats, indent=2), encoding="utf-8")
    (ASSETS / "profile-telemetry.svg").write_text(render_profile(stats), encoding="utf-8")
    print(f"Generated {ASSETS / 'profile-telemetry.svg'}")


if __name__ == "__main__":
    main()
