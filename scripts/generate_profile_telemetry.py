#!/usr/bin/env python3
"""Generate Alok's profile telemetry SVGs from GitHub data.

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
import time
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

WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# Recognizable OSS repos worth naming by their common name (vs. owner/repo).
KNOWN_OSS_LABELS = {
    "mem0ai/mem0": "mem0",
    "run-llama/llama_index": "LlamaIndex",
    "qdrant/qdrant-client": "Qdrant",
    "qdrant/qdrant": "Qdrant",
    "supermemoryai/supermemory": "Supermemory",
    "langchain-ai/langchain": "LangChain",
    "langchain-ai/langchainjs": "LangChain",
}


# ---------------------------------------------------------------------------
# GitHub fetchers
# ---------------------------------------------------------------------------

def gh_json(args):
    for attempt in range(1, 5):
        proc = subprocess.run(["gh", "api", *args], cwd=ROOT, text=True, capture_output=True)
        if proc.returncode == 0:
            return json.loads(proc.stdout)
        if attempt < 4:
            wait = attempt * 3
            print(f"retrying gh api after failure ({attempt}/4), waiting {wait}s", file=sys.stderr)
            time.sleep(wait)
            continue
        print("failed: gh api " + " ".join(args), file=sys.stderr)
        print(proc.stderr, file=sys.stderr)
        raise SystemExit(proc.returncode)


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


def fetch_contribution_days():
    query = """
    query($login: String!, $from: DateTime!, $to: DateTime!) {
      user(login: $login) {
        contributionsCollection(from: $from, to: $to) {
          contributionCalendar {
            weeks {
              contributionDays {
                date
                contributionCount
              }
            }
          }
        }
      }
    }
    """
    data = gh_graphql(
        query,
        login=USER,
        **{
            "from": SINCE.isoformat() + "T00:00:00Z",
            "to": (TODAY + dt.timedelta(days=1)).isoformat() + "T00:00:00Z",
        },
    )
    days = Counter()
    weeks = data["data"]["user"]["contributionsCollection"]["contributionCalendar"]["weeks"]
    for week in weeks:
        for day in week["contributionDays"]:
            days[dt.date.fromisoformat(day["date"])] = day["contributionCount"]
    return days


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
    contribution_daily = fetch_contribution_days()
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
    contribution_values = [contribution_daily[day] for day in days]
    active_days_set = {d for d, c in daily.items() if c > 0}
    contribution_active_days = {d for d, c in contribution_daily.items() if c > 0}
    active_values = [v for v in daily_values if v > 0]
    contribution_active_values = [v for v in contribution_values if v > 0]
    current_streak, longest_streak = streaks(contribution_active_days or active_days_set)

    # Weekday pulse: mean GitHub contributions per occurrence of each weekday.
    weekday_total = [0] * 7
    weekday_count = [0] * 7
    weekday_active = [0] * 7
    for day in days:
        wd = day.weekday()
        weekday_count[wd] += 1
        weekday_total[wd] += contribution_daily[day]
        if contribution_daily[day] > 0:
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
        total = sum(c for d, c in contribution_daily.items() if start <= d < next_month)
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

    # Longest-running repo: widest first-to-last-commit span, not just a single burst.
    longest_running = None
    best_span = -1
    for name, counts in repo_daily.items():
        active = sorted(d for d, c in counts.items() if c > 0)
        if len(active) < 2:
            continue
        span = (active[-1] - active[0]).days + 1
        if span > best_span:
            best_span = span
            longest_running = {"name": name, "spanDays": span, "activeDays": len(active)}

    # Velocity trend: GitHub contribution calendar, last 30d versus the prior 30d.
    last_30 = sum(c for d, c in contribution_daily.items() if d > TODAY - dt.timedelta(days=30))
    prev_30 = sum(c for d, c in contribution_daily.items() if TODAY - dt.timedelta(days=60) < d <= TODAY - dt.timedelta(days=30))
    velocity_trend = ((last_30 - prev_30) / prev_30 * 100) if prev_30 else 0.0
    momentum_7d = sum(c for d, c in contribution_daily.items() if d > TODAY - dt.timedelta(days=7))

    # Largest single day, burst days, longest quiet stretch from contribution calendar.
    biggest_day = max(contribution_daily.items(), key=lambda x: x[1], default=(TODAY, 0))
    burst_threshold = percentile(contribution_active_values, 90) if contribution_active_values else 0
    burst_days = sum(1 for d, c in contribution_daily.items() if c >= max(1, burst_threshold))
    longest_gap = 0
    gap = 0
    for d in days:
        if contribution_daily[d] == 0:
            gap += 1
            longest_gap = max(longest_gap, gap)
        else:
            gap = 0
    # Mean gap between consecutive active contribution days.
    sorted_active = sorted(contribution_active_days or active_days_set)
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
        len(contribution_active_values) / len(days) * 100 * 0.40
        + min(sum(contribution_values) / 18, 35)
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
        "contributionsDaily": {d.isoformat(): contribution_daily[d] for d in days},
        "activeDaysPct": len(contribution_active_values) / len(days) * 100,
        "contributionTotal": sum(contribution_values),
        "commitTotal": sum(daily_values),
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
        "longestRunningRepo": longest_running,
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
</defs>"""


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
    parts.append(f'<text x="{x}" y="{y + big + 3}" fill="{accent}" font-size="{big}" font-weight="800" letter-spacing="-1">{esc(value)}</text>')
    if sub:
        parts.append(f'<text x="{x}" y="{y + big + 21}" fill="{TEXT_DIM}" font-size="11">{esc(sub)}</text>')
    return "\n".join(parts)


# --- Donut ring -----------------------------------------------------------

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

# Telemetry SVG — flat editorial layout, no heatmap
# ---------------------------------------------------------------------------

def render_profile(stats):
    W = 1100
    pad = 44

    # --- Hero: OSS PRs merged (left) + PR merge-rate donut (right) ---------
    hero_y = 96
    streak_x = pad
    donut_col_x = 620

    merge_pct = stats["prMergeRatio"]
    donut_cx = donut_col_x + 70
    donut_cy = hero_y + 88
    merge_ring = donut_ring(donut_cx, donut_cy, 58, 14, [(merge_pct, LIME), (max(0, 100 - merge_pct), HAIRLINE)])

    # --- Vitals strip --------------------------------------------------------
    velocity_trend = stats["velocityTrendPct"]
    trend_color = LIME if velocity_trend >= 0 else RED
    trend_sign = "+" if velocity_trend >= 0 else ""
    trend_arrow = "▲" if velocity_trend >= 0 else "▼"

    strip_y = 320
    fields = [
        ("all contributions", fmt_num(stats["contributionTotal"]), "GitHub calendar", LIME),
        ("30d velocity", fmt_num(stats["velocity30d"]), f'{trend_arrow} {trend_sign}{velocity_trend:.0f}% vs prior 30', trend_color),
        ("active days", pct(stats["activeDaysPct"]), f'mean gap {stats["meanGapDays"]:.1f}d', CYAN),
        ("oss prs merged", fmt_num(stats["externalPrsMerged"]), f'across {stats["externalReposCount"]} external repos', VIOLET),
        ("pull requests", fmt_num(stats["prs"]), f'{stats["prMergeRatio"]:.0f}% merged', MAGENTA),
    ]
    strip_parts = []
    col_w = (W - pad * 2) / len(fields)
    for i, (label, value, sub, color) in enumerate(fields):
        cx = pad + i * col_w
        strip_parts.append(number_block(cx, strip_y, label, value, sub, accent=color, big=24))
        if i > 0:
            strip_parts.append(f'<line x1="{cx - 10}" y1="{strip_y - 4}" x2="{cx - 10}" y2="{strip_y + 50}" stroke="{HAIRLINE}"/>')

    # --- Longest-running project: follow-through over bursts ----------------
    lrr_y = 412
    lrr = stats["longestRunningRepo"]
    if lrr:
        lrr_line = (
            f'<text x="{pad}" y="{lrr_y + 26}" fill="{TEXT_HI}" font-size="14" font-weight="700">{esc(lrr["name"])}</text>'
            f'<text x="{pad + 12 + len(lrr["name"]) * 8.4:.1f}" y="{lrr_y + 26}" fill="{TEXT_MUTED}" font-size="12">'
            f'{lrr["spanDays"]}d span · {lrr["activeDays"]} active days</text>'
        )
    else:
        lrr_line = f'<text x="{pad}" y="{lrr_y + 26}" fill="{TEXT_DIM}" font-size="12">no repo with sustained activity yet</text>'

    # --- OSS contributions: named repos, full width -------------------------
    oss_y = lrr_y + 66

    def oss_rows(items, x, y, w):
        if not items:
            return f'<text x="{x}" y="{y + 22}" fill="{TEXT_DIM}" font-size="12">no external PRs yet</text>'
        out = []
        for i, (repo, merged_n, total_n) in enumerate(items[:6]):
            ry = y + 26 + i * 24
            status = "merged" if merged_n else "open"
            color = LIME if merged_n else TEXT_DIM
            out.append(f'<text x="{x}" y="{ry}" fill="{TEXT}" font-size="13" font-weight="600">{esc(repo)}</text>')
            out.append(f'<text x="{x + w}" y="{ry}" fill="{color}" font-size="12" font-weight="700" text-anchor="end">{merged_n}/{total_n} {status}</text>')
        return "\n".join(out)

    # Recognized OSS (mem0, LlamaIndex, Qdrant, ...) surfaces first; stable sort
    # preserves the existing merged/total ranking within each group.
    oss_sorted = sorted(stats["externalTop"], key=lambda item: 0 if item[0] in KNOWN_OSS_LABELS else 1)
    oss_items = oss_sorted[:6] or [("no external PRs yet", 0, 0)]
    oss_rows_svg = oss_rows(oss_sorted, pad, oss_y, W - pad * 2)

    H = int(oss_y + len(oss_items) * 24 + 34)

    return f"""<svg width="{W}" height="{H}" viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" role="img" aria-labelledby="ttl desc">
  <title id="ttl">Alok Tripathi — AI/ML Engineer</title>
  <desc id="desc">OSS PRs merged, PR merge rate, vitals, longest-running project, and named open-source contributions.</desc>
  <style>text {{ font-family: 'JetBrains Mono', 'SF Mono', ui-monospace, Menlo, monospace; }} .sans {{ font-family: 'Inter', ui-sans-serif, system-ui, sans-serif; }}</style>
  {defs()}
  {shell(W, H)}

  <!-- Header -->
  <text x="{pad}" y="56" fill="{TEXT_HI}" font-size="26" font-weight="800" letter-spacing="-0.5" class="sans">AI/ML Engineer</text>
  <text x="{pad}" y="76" fill="{TEXT_MUTED}" font-size="11" letter-spacing="1">SHIPS AI PRODUCTS · AGENTIC SYSTEMS · {esc(stats["generated"]).upper()}</text>
  {hairline(pad, 86, W - pad, HAIRLINE_BRIGHT)}

  <!-- Hero: OSS PRs merged + PR merge-rate donut -->
  <g>
    <text x="{streak_x}" y="{hero_y + 12}" fill="{TEXT_MUTED}" font-size="10" font-weight="700" letter-spacing="2">OSS PRS MERGED</text>
    <text x="{streak_x}" y="{hero_y + 105}" fill="{LIME}" font-size="118" font-weight="900" letter-spacing="-6" class="sans">{stats["externalPrsMerged"]}</text>
    <text x="{streak_x}" y="{hero_y + 138}" fill="{TEXT}" font-size="13">across {stats["externalReposCount"]} external repos · {stats["externalPrsTotal"]} opened</text>
  </g>
  <g>
    <text x="{donut_col_x}" y="{hero_y + 12}" fill="{TEXT_MUTED}" font-size="10" font-weight="700" letter-spacing="2">PR MERGE RATE</text>
    {merge_ring}
    <text x="{donut_cx}" y="{donut_cy + 10}" fill="{TEXT_HI}" font-size="28" font-weight="900" text-anchor="middle" class="sans">{merge_pct:.0f}%</text>
    <text x="{donut_cx}" y="{hero_y + 168}" fill="{TEXT}" font-size="13" text-anchor="middle">{stats["prs"]} PRs shipped total</text>
  </g>

  <!-- Vitals -->
  {hairline(pad, 280, W - pad)}
  <text x="{pad}" y="292" fill="{TEXT_MUTED}" font-size="10" font-weight="700" letter-spacing="2">VITALS</text>
  {chr(10).join(strip_parts)}

  <!-- Longest-running project -->
  {hairline(pad, lrr_y - 16, W - pad)}
  {section_label(pad, lrr_y - 4, "Longest-running project", width=W - pad * 2)}
  {lrr_line}

  <!-- OSS contributions -->
  {hairline(pad, oss_y - 16, W - pad)}
  {section_label(pad, oss_y - 4, "OSS contributions", tag=f'{stats["externalPrsMerged"]}/{stats["externalPrsTotal"]} merged', width=W - pad * 2)}
  {oss_rows_svg}
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
