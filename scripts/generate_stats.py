"""
Builds profile stat cards (stats, languages, streak) from your own GitHub data,
including private and organisation work, using a personal access token.

Runs inside a GitHub Action. Uses only the Python standard library, so no
third-party code ever handles the token.

Env vars:
  GH_STATS_TOKEN     classic PAT with scopes: repo, read:user, read:org   (required)
  EXCLUDE_LANGUAGES  comma-separated languages to leave out of the languages card
                     (default: "Jupyter Notebook", whose output cells inflate byte counts)
  OUTPUT_DIR         where to write the SVGs (default: assets)
"""

import json
import os
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from html import escape

API = "https://api.github.com/graphql"
TOKEN = os.environ.get("GH_STATS_TOKEN", "").strip()
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "assets")
EXCLUDE = {
    s.strip().lower()
    for s in os.environ.get("EXCLUDE_LANGUAGES", "Jupyter Notebook").split(",")
    if s.strip()
}

# Theme (matches the README)
BG, BORDER = "#0d1117", "#30363d"
ACCENT, TEXT, MUTED, WHITE = "#38BDAC", "#c9d1d9", "#8b949e", "#ffffff"
FONT = "'Segoe UI', Ubuntu, 'Helvetica Neue', Helvetica, Arial, sans-serif"


# ---------------------------------------------------------------- GitHub API
def gql(query, variables=None):
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        API,
        data=body,
        headers={
            "Authorization": f"bearer {TOKEN}",
            "Content-Type": "application/json",
            "User-Agent": "profile-stats-action",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.load(resp)
    if data.get("errors"):
        raise RuntimeError(json.dumps(data["errors"], indent=2))
    return data["data"]


def fetch_profile():
    return gql(
        """
        query {
          viewer {
            login
            name
            createdAt
            contributionsCollection {
              totalCommitContributions
              totalPullRequestContributions
              totalPullRequestReviewContributions
              totalIssueContributions
              totalRepositoriesWithContributedCommits
              contributionCalendar { totalContributions }
            }
          }
        }
        """
    )["viewer"]


def fetch_calendar(created_at):
    """All contribution days since the account was created (one query per year)."""
    days = {}
    start = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    cursor = start
    while cursor < now:
        end = min(cursor + timedelta(days=365), now)
        data = gql(
            """
            query($from: DateTime!, $to: DateTime!) {
              viewer {
                contributionsCollection(from: $from, to: $to) {
                  contributionCalendar {
                    weeks { contributionDays { date contributionCount } }
                  }
                }
              }
            }
            """,
            {"from": cursor.isoformat(), "to": end.isoformat()},
        )
        weeks = data["viewer"]["contributionsCollection"]["contributionCalendar"]["weeks"]
        for w in weeks:
            for d in w["contributionDays"]:
                days[d["date"]] = d["contributionCount"]
        cursor = end
    return dict(sorted(days.items()))


LANG_FRAGMENT = """
  nameWithOwner
  isFork
  languages(first: 10, orderBy: {field: SIZE, direction: DESC}) {
    edges { size node { name color } }
  }
"""


def fetch_languages():
    """Languages across repos you own plus repos you have contributed to (private included)."""
    repos = {}

    def collect(field, args):
        after = None
        while True:
            data = gql(
                f"""
                query($after: String) {{
                  viewer {{
                    {field}(first: 100, after: $after, {args}) {{
                      pageInfo {{ hasNextPage endCursor }}
                      nodes {{ {LANG_FRAGMENT} }}
                    }}
                  }}
                }}
                """,
                {"after": after},
            )
            conn = data["viewer"][field]
            for r in conn["nodes"]:
                if r and not r["isFork"]:
                    repos[r["nameWithOwner"]] = r
            if not conn["pageInfo"]["hasNextPage"]:
                break
            after = conn["pageInfo"]["endCursor"]

    collect("repositories", "ownerAffiliations: [OWNER]")
    collect(
        "repositoriesContributedTo",
        "includeUserRepositories: true, "
        "contributionTypes: [COMMIT, PULL_REQUEST, REPOSITORY]",
    )

    totals, colors = {}, {}
    for r in repos.values():
        for e in r["languages"]["edges"]:
            name = e["node"]["name"]
            if name.lower() in EXCLUDE:
                continue
            totals[name] = totals.get(name, 0) + e["size"]
            colors[name] = e["node"]["color"] or "#8b949e"
    return totals, colors, len(repos)


# ---------------------------------------------------------------- streaks
def streaks(days):
    today = datetime.now(timezone.utc).date()
    dates = [date.fromisoformat(d) for d in days]
    counts = list(days.values())

    longest = (0, None, None)
    run, run_start = 0, None
    for d, c in zip(dates, counts):
        if d > today:
            break
        if c > 0:
            if run == 0:
                run_start = d
            run += 1
            if run > longest[0]:
                longest = (run, run_start, d)
        else:
            run = 0

    # Current streak: today counts if it has contributions; otherwise start from yesterday.
    lookup = dict(zip(dates, counts))
    cur_end = today if lookup.get(today, 0) > 0 else today - timedelta(days=1)
    cur, d = 0, cur_end
    while lookup.get(d, 0) > 0:
        cur += 1
        d -= timedelta(days=1)
    current = (cur, d + timedelta(days=1) if cur else None, cur_end if cur else None)
    return current, longest


def fmt_range(a, b):
    if not a:
        return "No active streak"
    f = lambda x: x.strftime("%b %-d")
    return f(a) if a == b else f"{f(a)} - {f(b)}"


# ---------------------------------------------------------------- SVG cards
def card(width, height, body):
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img">
  <style>
    text {{ font-family: {FONT}; }}
    .title {{ font-size: 20px; font-weight: 600; fill: {ACCENT}; }}
    .label {{ font-size: 14px; font-weight: 600; fill: {TEXT}; }}
    .value {{ font-size: 14px; font-weight: 700; fill: {WHITE}; }}
    .small {{ font-size: 12px; fill: {TEXT}; }}
    .muted {{ font-size: 12px; fill: {MUTED}; }}
    .big   {{ font-size: 30px; font-weight: 700; fill: {WHITE}; }}
    .fade  {{ opacity: 0; animation: fade 0.6s ease forwards; }}
    @keyframes fade {{ to {{ opacity: 1; }} }}
  </style>
  <rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="10" fill="{BG}" stroke="{BORDER}"/>
{body}
</svg>
"""


def stats_card(name, cc):
    rows = [
        ("Commits (last year)", cc["totalCommitContributions"]),
        ("Pull requests (last year)", cc["totalPullRequestContributions"]),
        ("Code reviews (last year)", cc["totalPullRequestReviewContributions"]),
        ("Repos contributed to (last year)", cc["totalRepositoriesWithContributedCommits"]),
        ("Contributions (last year)", cc["contributionCalendar"]["totalContributions"]),
    ]
    body = [f'  <text x="25" y="38" class="title">{escape(name)}\'s GitHub Stats</text>']
    for i, (label, val) in enumerate(rows):
        y = 75 + i * 26
        delay = 0.1 + i * 0.12
        body.append(
            f'  <g class="fade" style="animation-delay:{delay:.2f}s">'
            f'<circle cx="31" cy="{y - 5}" r="4" fill="{ACCENT}"/>'
            f'<text x="45" y="{y}" class="label">{escape(label)}:</text>'
            f'<text x="455" y="{y}" class="value" text-anchor="end">{val:,}</text></g>'
        )
    return card(480, 210, "\n".join(body))


def languages_card(totals, colors, top=8):
    items = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:top]
    total = sum(v for _, v in items) or 1
    body = ['  <text x="25" y="38" class="title">Most Used Languages</text>']
    x, bar_w = 25.0, 430.0
    body.append(f'  <clipPath id="bar"><rect x="25" y="55" width="{bar_w}" height="10" rx="5"/></clipPath>')
    body.append('  <g clip-path="url(#bar)">')
    for name, size in items:
        w = bar_w * size / total
        body.append(f'    <rect x="{x:.2f}" y="55" width="{w + 0.5:.2f}" height="10" fill="{colors[name]}"/>')
        x += w
    body.append("  </g>")
    for i, (name, size) in enumerate(items):
        col, row = i % 2, i // 2
        cx, cy = 30 + col * 215, 92 + row * 26
        pct = 100 * size / total
        body.append(
            f'  <g class="fade" style="animation-delay:{0.1 + i * 0.08:.2f}s">'
            f'<circle cx="{cx}" cy="{cy - 4}" r="5" fill="{colors[name]}"/>'
            f'<text x="{cx + 12}" y="{cy}" class="small">{escape(name)} {pct:.1f}%</text></g>'
        )
    return card(480, 210, "\n".join(body))


def streak_card(total, since, current, longest):
    col = [160, 490, 820]
    body = [
        f'  <line x1="325" y1="35" x2="325" y2="175" stroke="{BORDER}"/>',
        f'  <line x1="655" y1="35" x2="655" y2="175" stroke="{BORDER}"/>',
        # total
        f'  <g class="fade"><text x="{col[0]}" y="95" class="big" text-anchor="middle">{total:,}</text>'
        f'<text x="{col[0]}" y="128" class="label" text-anchor="middle">Total Contributions</text>'
        f'<text x="{col[0]}" y="152" class="muted" text-anchor="middle">{escape(since)} - Present</text></g>',
        # current (ring)
        f'  <g class="fade" style="animation-delay:0.15s">'
        f'<circle cx="{col[1]}" cy="80" r="40" fill="none" stroke="{ACCENT}" stroke-width="5"/>'
        f'<text x="{col[1]}" y="91" class="big" text-anchor="middle">{current[0]}</text>'
        f'<text x="{col[1]}" y="145" class="label" text-anchor="middle" style="fill:{ACCENT}">Current Streak</text>'
        f'<text x="{col[1]}" y="168" class="muted" text-anchor="middle">{escape(fmt_range(current[1], current[2]))}</text></g>',
        # longest
        f'  <g class="fade" style="animation-delay:0.3s"><text x="{col[2]}" y="95" class="big" text-anchor="middle">{longest[0]}</text>'
        f'<text x="{col[2]}" y="128" class="label" text-anchor="middle">Longest Streak</text>'
        f'<text x="{col[2]}" y="152" class="muted" text-anchor="middle">{escape(fmt_range(longest[1], longest[2]))}</text></g>',
    ]
    return card(980, 200, "\n".join(body))


# ---------------------------------------------------------------- main
def write(name, svg):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    path = os.path.join(OUTPUT_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(svg)
    print("wrote", path)


def main():
    if not TOKEN:
        sys.exit("GH_STATS_TOKEN is not set. Add it under Settings > Secrets and variables > Actions.")

    profile = fetch_profile()
    display = (profile["name"] or profile["login"]).split()[0]

    days = fetch_calendar(profile["createdAt"])
    total = sum(days.values())
    first_day = next((d for d, c in days.items() if c > 0), profile["createdAt"][:10])
    since = date.fromisoformat(first_day).strftime("%b %-d, %Y")
    current, longest = streaks(days)

    totals, colors, repo_count = fetch_languages()
    print(f"{len(days)} days, {total} contributions, {repo_count} repos scanned")

    write("stats.svg", stats_card(display, profile["contributionsCollection"]))
    write("languages.svg", languages_card(totals, colors))
    write("streak.svg", streak_card(total, since, current, longest))


if __name__ == "__main__":
    main()
