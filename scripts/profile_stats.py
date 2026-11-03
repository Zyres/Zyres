#!/usr/bin/env python3
# Builds the profile stat cards (dark/light SVG) from the GitHub GraphQL API. Stdlib only.
import argparse, datetime as dt, json, os, re, sys, urllib.request
from collections import defaultdict

API = "https://api.github.com/graphql"


def gql(token, query, variables):
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(API, data=body, headers={"Authorization": "bearer " + token, "User-Agent": "profile-stats"})
    with urllib.request.urlopen(req, timeout=90) as r:
        res = json.load(r)
    if res.get("errors"):
        sys.exit("GraphQL error: " + json.dumps(res["errors"])[:2000])
    return res["data"]


Q_COUNTS = """query($qPr: String!, $qMerged: String!, $qIssues: String!) {
  prs: search(query: $qPr, type: ISSUE) { issueCount }
  merged: search(query: $qMerged, type: ISSUE) { issueCount }
  issues: search(query: $qIssues, type: ISSUE) { issueCount }
}"""

Q_YEAR = """query($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $from, to: $to) {
      totalCommitContributions totalIssueContributions totalPullRequestContributions totalPullRequestReviewContributions
      contributionCalendar { totalContributions weeks { contributionDays { date contributionCount } } }
      commitContributionsByRepository(maxRepositories: 100) {
        contributions { totalCount }
        repository {
          nameWithOwner isPrivate isFork stargazerCount
          primaryLanguage { name color }
          languages(first: 12, orderBy: {field: SIZE, direction: DESC}) { totalSize edges { size node { name color } } }
        }
      }
    }
  }
}"""

Q_CLOSED = """query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    issues(states: CLOSED, first: 100, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes {
        author { login }
        timelineItems(itemTypes: [CLOSED_EVENT], last: 1) {
          nodes { ... on ClosedEvent { actor { login } closer { __typename ... on PullRequest { author { login } } ... on Commit { author { user { login } } } } } }
        }
      }
    }
  }
}"""


def collect(token, login):
    user = gql(token, "query($login: String!) { user(login: $login) { name createdAt } }", {"login": login})["user"]
    counts = gql(token, Q_COUNTS, {"qPr": f"author:{login} is:pr", "qMerged": f"author:{login} is:pr is:merged", "qIssues": f"author:{login} is:issue"})
    created = dt.datetime.fromisoformat(user["createdAt"].replace("Z", "+00:00"))
    now = dt.datetime.now(dt.timezone.utc)

    years, calendar = [], {}
    totals = defaultdict(int)
    repo_commits, repo_info = defaultdict(int), {}
    for year in range(created.year, now.year + 1):
        start = max(created, dt.datetime(year, 1, 1, tzinfo=dt.timezone.utc))
        end = min(now, dt.datetime(year, 12, 31, 23, 59, 59, tzinfo=dt.timezone.utc))
        cc = gql(token, Q_YEAR, {"login": login, "from": start.isoformat(), "to": end.isoformat()})["user"]["contributionsCollection"]
        years.append({"year": year, "commits": cc["totalCommitContributions"], "contributions": cc["contributionCalendar"]["totalContributions"]})
        totals["commits"] += cc["totalCommitContributions"]
        totals["reviews"] += cc["totalPullRequestReviewContributions"]
        totals["contributions"] += cc["contributionCalendar"]["totalContributions"]
        for w in cc["contributionCalendar"]["weeks"]:
            for d in w["contributionDays"]:
                calendar[d["date"]] = d["contributionCount"]
        for c in cc["commitContributionsByRepository"]:
            r = c["repository"]
            if r["isPrivate"]:
                continue
            repo_commits[r["nameWithOwner"]] += c["contributions"]["totalCount"]
            repo_info[r["nameWithOwner"]] = r

    # issues closed by the user, by one of the user's pull requests or commits
    solved = 0
    for full in repo_info:
        owner, name = full.split("/")
        after = None
        while True:
            repo = gql(token, Q_CLOSED, {"owner": owner, "name": name, "after": after})["repository"]
            if not repo:
                break
            page = repo["issues"]
            for issue in page["nodes"]:
                for ev in issue["timelineItems"]["nodes"]:
                    who = {(ev.get("actor") or {}).get("login")}
                    closer = ev.get("closer") or {}
                    if closer.get("__typename") == "PullRequest":
                        who.add((closer.get("author") or {}).get("login"))
                    elif closer.get("__typename") == "Commit":
                        who.add(((closer.get("author") or {}).get("user") or {}).get("login"))
                    if login.lower() in {w.lower() for w in who if w}:
                        solved += 1
            if not page["pageInfo"]["hasNextPage"]:
                break
            after = page["pageInfo"]["endCursor"]

    # languages weighted by the user's commits per repository
    lang_weight, lang_color = defaultdict(float), {}
    for full, commits in repo_commits.items():
        langs = repo_info[full]["languages"]
        if not langs["totalSize"]:
            continue
        for e in langs["edges"]:
            lang_weight[e["node"]["name"]] += commits * e["size"] / langs["totalSize"]
            lang_color[e["node"]["name"]] = e["node"]["color"] or "#888888"
    total_w = sum(lang_weight.values()) or 1
    languages = [{"name": n, "color": lang_color[n], "share": w / total_w} for n, w in sorted(lang_weight.items(), key=lambda x: -x[1])]

    repos = []
    for full, commits in sorted(repo_commits.items(), key=lambda x: -x[1])[:6]:
        r = repo_info[full]
        pl = r["primaryLanguage"] or {}
        repos.append({"name": full, "commits": commits, "stars": r["stargazerCount"], "lang": pl.get("name", ""), "color": pl.get("color") or "#888888"})

    return {
        "login": login, "name": user["name"] or login, "created": user["createdAt"], "updated": now.isoformat(),
        "totals": {"commits": totals["commits"], "contributions": totals["contributions"], "reviews": totals["reviews"],
                   "prs": counts["prs"]["issueCount"], "prs_merged": counts["merged"]["issueCount"],
                   "issues_opened": counts["issues"]["issueCount"], "issues_solved": solved, "repos": len(repo_commits)},
        "years": years,
        "calendar": [{"date": d, "count": calendar[d]} for d in sorted(calendar)],
        "languages": languages,
        "repos": repos,
    }


# ---------- rendering ----------

THEMES = {
    "whitemango": {
        "dark":  dict(bg="#161616", panel="#1f1f1f", line="#2e2e2e", fg="#ffffff", dim="#d0d0d0", faint="#8a8a8a",
                      accent="#ff2bea", hot=("#fc00e3", "#fc0000"),
                      heat=("#262626", "#5a1452", "#a3008f", "#e6007a", "#fc1c1c")),
        "light": dict(bg="#ffffff", panel="#f4f4f4", line="#e4e4e4", fg="#161616", dim="#383838", faint="#777777",
                      accent="#d400bd", hot=("#ed00e1", "#fc0000"),
                      heat=("#eeeeee", "#fbc4f2", "#f46be0", "#ef0f8f", "#fc0000")),
    },
    "ascemu": {
        "dark":  dict(bg="#0f0c0a", panel="#17120e", line="#2a2119", fg="#f3ebe2", dim="#b9ab9c", faint="#7d6f62",
                      accent="#ff8a1f", hot=("#ffc173", "#ff8a1f"), grid="#ff8a1f", gridop=".06",
                      heat=("#1f1914", "#4a2a0c", "#8a4a10", "#c96a14", "#ff8a1f")),
        "light": dict(bg="#faf6f1", panel="#ffffff", line="#e6dccf", fg="#231a12", dim="#5d4f42", faint="#8a7b6c",
                      accent="#aa5e18", hot=("#c4600d", "#7a3504"), grid="#aa5e18", gridop=".07",
                      heat=("#efe7dc", "#f3c99a", "#e59a4c", "#c4600d", "#7a3504")),
    },
    "stutzer": {
        "dark":  dict(bg="#0f1d33", panel="#162848", line="#243a60", fg="#fdf9ea", dim="#c9d3e6", faint="#8392ad",
                      accent="#a6bcdf", hot=("#fdf9ea", "#a6bcdf"),
                      heat=("#1a2b4a", "#24447a", "#2e55a5", "#6f93d6", "#fdf9ea")),
        "light": dict(bg="#ffffff", panel="#fdf9ea", line="#ebe3c7", fg="#1b2b4b", dim="#3c4f72", faint="#8a93a3",
                      accent="#2e55a5", hot=("#2e55a5", "#12589a"),
                      heat=("#f0f0f0", "#c9d6ee", "#8aa6dc", "#4f74c0", "#12589a")),
    },
}
FONT = "Roboto, 'Segoe UI', -apple-system, 'Helvetica Neue', Arial, sans-serif"
MONO = "'Cascadia Mono', Consolas, 'SFMono-Regular', Menlo, monospace"
STYLE = """<style>
  .in { opacity: 0; animation: in .7s ease-out forwards; }
  @keyframes in { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: translateY(0); } }
  .grow { transform-box: fill-box; animation: grow 1.1s cubic-bezier(.2,.7,.2,1) both; }
  .growx { transform-origin: left center; } .growy { transform-origin: center bottom; }
  @keyframes grow { from { transform: scale(0, 1); } to { transform: scale(1, 1); } }
  .growy { animation-name: growy; } @keyframes growy { from { transform: scale(1, 0); } to { transform: scale(1, 1); } }
  @media (prefers-reduced-motion: reduce) { .in, .grow { animation: none; opacity: 1; } }
</style>"""


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def num(n):
    return f"{n:,}"


def frame(t, w, h, title, sub, body, label):
    grid = ""
    if t.get("grid"):
        grid = (f'<pattern id="grid" width="32" height="32" patternUnits="userSpaceOnUse"><path d="M32 0H0V32" fill="none" '
                f'stroke="{t["grid"]}" stroke-opacity="{t["gridop"]}"/></pattern><rect width="{w}" height="{h}" fill="url(#grid)"/>')
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" aria-label="{esc(label)}">
<defs><linearGradient id="mg" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="{t['hot'][0]}"/><stop offset="1" stop-color="{t['hot'][1]}"/></linearGradient>
<linearGradient id="mgv" x1="0" y1="1" x2="0" y2="0"><stop offset="0" stop-color="{t['hot'][0]}"/><stop offset="1" stop-color="{t['hot'][1]}"/></linearGradient>
<clipPath id="frame"><rect width="{w}" height="{h}" rx="16"/></clipPath></defs>
{STYLE}
<g clip-path="url(#frame)"><rect width="{w}" height="{h}" fill="{t['bg']}"/>{grid}</g>
<rect x=".5" y=".5" width="{w-1}" height="{h-1}" rx="15.5" fill="none" stroke="{t['line']}"/>
<g font-family="{FONT}">
  <rect x="28" y="28" width="34" height="4" rx="2" fill="url(#mg)"/>
  <text x="28" y="58" font-size="20" font-weight="600" fill="{t['fg']}">{esc(title)}</text>
  <text x="{w-28}" y="58" font-size="12.5" fill="{t['faint']}" font-family="{MONO}" text-anchor="end">{esc(sub)}</text>
  {body}
</g>
</svg>
'''


def streaks(calendar, today):
    days = [c for c in calendar if c["date"] <= today]
    longest = run = 0
    for c in days:
        run = run + 1 if c["count"] else 0
        longest = max(longest, run)
    current = 0
    for i, c in enumerate(reversed(days)):
        if c["count"]:
            current += 1
        elif i == 0:
            continue  # today without contributions does not break the streak yet
        else:
            break
    return current, longest


def card_overview(d, t):
    tot = d["totals"]
    today = d["updated"][:10]
    year = int(today[:4])
    this_year = next((y for y in d["years"] if y["year"] == year), {"commits": 0, "contributions": 0})
    cur, longest = streaks(d["calendar"], today)
    since = d["created"][:4]
    tiles = [
        ("COMMITS", num(tot["commits"]), f"{num(this_year['commits'])} in {year}", True),
        ("ISSUES SOLVED", num(tot["issues_solved"]), "closed by me or my commits", True),
        ("PULL REQUESTS", num(tot["prs"]), f"{num(tot['prs_merged'])} merged", False),
        ("CODE REVIEWS", num(tot["reviews"]), "pull request reviews", False),
        ("CONTRIBUTIONS", num(tot["contributions"]), f"{num(this_year['contributions'])} in {year}", False),
        ("ISSUES OPENED", num(tot["issues_opened"]), "reported and tracked", False),
        ("CURRENT STREAK", f"{cur} d", "days in a row", False),
        ("LONGEST STREAK", f"{longest} d", f"{len(d['years'])} years of history", False),
    ]
    W, tw, th, gap, x0, y0 = 1200, 276, 100, 12, 28, 80
    out = []
    for i, (lab, val, sub, hot) in enumerate(tiles):
        x, y = x0 + (i % 4) * (tw + gap), y0 + (i // 4) * (th + gap)
        out.append(f'''<g class="in" style="animation-delay:{i*0.06:.2f}s">
  <rect x="{x}" y="{y}" width="{tw}" height="{th}" rx="12" fill="{t['panel']}" stroke="{t['line']}"/>
  <text x="{x+18}" y="{y+26}" font-size="11" font-weight="600" fill="{t['faint']}" letter-spacing="1.2">{lab}</text>
  <text x="{x+18}" y="{y+60}" font-size="32" font-weight="700" fill="{'url(#mg)' if hot else t['fg']}">{esc(val)}</text>
  <text x="{x+18}" y="{y+84}" font-size="12" fill="{t['dim']}" font-family="{MONO}">{esc(sub)}</text>
</g>''')
    h = y0 + 2 * th + gap + 28
    return frame(t, W, h, "GitHub at a glance", f"since {since} · updated {today}", "\n".join(out), "GitHub statistics")


def card_activity(d, t):
    today = dt.date.fromisoformat(d["updated"][:10])
    counts = {c["date"]: c["count"] for c in d["calendar"]}
    start = today - dt.timedelta(days=today.weekday() + 1 + 52 * 7)  # sunday, 53 weeks
    cell, gap, x0, y0 = 14, 3, 64, 96
    vals = [counts.get((start + dt.timedelta(days=i)).isoformat(), 0) for i in range((today - start).days + 1)]
    peak = max(vals) or 1
    out, months = [], []
    for i, v in enumerate(vals):
        day = start + dt.timedelta(days=i)
        wk, wd = i // 7, i % 7
        lvl = 0 if v == 0 else min(4, 1 + int(3.999 * v / peak))
        x, y = x0 + wk * (cell + gap), y0 + wd * (cell + gap)
        out.append(f'<rect x="{x}" y="{y}" width="{cell}" height="{cell}" rx="3" fill="{t["heat"][lvl]}"><title>{v} on {day}</title></rect>')
        if day.day == 1 or i == 0:
            months.append(f'<text x="{x}" y="{y0-12}" font-size="11.5" fill="{t["faint"]}">{day.strftime("%b")}</text>')
    for wd, name in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        months.append(f'<text x="{x0-12}" y="{y0 + wd*(cell+gap) + 12}" font-size="11" fill="{t["faint"]}" text-anchor="end">{name}</text>')
    year_total = sum(vals)
    best_i = max(range(len(vals)), key=lambda i: vals[i])
    best_day = start + dt.timedelta(days=best_i)
    weeks = [sum(vals[i:i+7]) for i in range(0, len(vals), 7)]
    active = sum(1 for v in vals if v)
    px = x0 + 53 * (cell + gap) + 40
    side = [("LAST 12 MONTHS", num(year_total), "contributions"), ("BUSIEST DAY", num(vals[best_i]), best_day.strftime("%d %b %Y")),
            ("BEST WEEK", num(max(weeks)), "contributions"), ("ACTIVE DAYS", num(active), f"of {len(vals)}")]
    for i, (lab, val, sub) in enumerate(side):
        y = y0 - 14 + i * 38
        out.append(f'<text x="{px}" y="{y+8}" font-size="10.5" font-weight="600" fill="{t["faint"]}" letter-spacing="1">{lab}</text>'
                   f'<text x="{px}" y="{y+28}" font-size="19" font-weight="700" fill="{t["fg"]}">{val} <tspan font-size="11.5" font-weight="400" fill="{t["dim"]}" font-family="{MONO}">{esc(sub)}</tspan></text>')
    legend_x = x0 + 53 * (cell + gap) - 5 * (cell + gap) - 80
    ly = y0 + 7 * (cell + gap) + 14
    out.append(f'<text x="{legend_x}" y="{ly+11}" font-size="11" fill="{t["faint"]}">less</text>')
    for i in range(5):
        out.append(f'<rect x="{legend_x + 32 + i*(cell+gap)}" y="{ly}" width="{cell}" height="{cell}" rx="3" fill="{t["heat"][i]}"/>')
    out.append(f'<text x="{legend_x + 36 + 5*(cell+gap)}" y="{ly+11}" font-size="11" fill="{t["faint"]}">more</text>')
    body = f'<g class="in">{"".join(months)}{"".join(out)}</g>'
    return frame(t, 1200, ly + 44, "Activity", "contributions per day", body, "Contribution activity of the last year")


def card_languages_years(d, t):
    out = []
    langs = d["languages"][:8]
    rest = 1 - sum(l["share"] for l in langs)
    if rest > 0.005:
        langs = langs + [{"name": "Other", "color": t["faint"], "share": rest}]
    x, bx, by, bw = 28, 28, 92, 520
    out.append(f'<rect x="{bx}" y="{by}" width="{bw}" height="14" rx="7" fill="{t["panel"]}"/>')
    out.append(f'<clipPath id="lbar"><rect x="{bx}" y="{by}" width="{bw}" height="14" rx="7"/></clipPath><g clip-path="url(#lbar)">')
    cx = bx
    for i, l in enumerate(langs):
        w = bw * l["share"]
        out.append(f'<rect class="grow growx" style="animation-delay:{i*0.08:.2f}s" x="{cx:.1f}" y="{by}" width="{w+0.5:.1f}" height="14" fill="{l["color"]}"/>')
        cx += w
    out.append("</g>")
    for i, l in enumerate(langs):
        lx, ly = bx + (i % 2) * 270, by + 46 + (i // 2) * 30
        out.append(f'<g class="in" style="animation-delay:{0.3 + i*0.05:.2f}s"><circle cx="{lx+6}" cy="{ly-5}" r="6" fill="{l["color"]}"/>'
                   f'<text x="{lx+20}" y="{ly}" font-size="15" fill="{t["fg"]}">{esc(l["name"])}</text>'
                   f'<text x="{lx+240}" y="{ly}" font-size="13" fill="{t["dim"]}" font-family="{MONO}" text-anchor="end">{l["share"]*100:.1f}%</text></g>')
    # commits per year
    years = d["years"]
    gx, gy, gw, gh = 640, 112, 532, 156
    peak = max((y["commits"] for y in years), default=1) or 1
    slot = gw / max(len(years), 1)
    bwid = min(30, slot * 0.62)
    out.append(f'<text x="{gx}" y="{gy-28}" font-size="11" font-weight="600" fill="{t["faint"]}" letter-spacing="1.2">COMMITS PER YEAR</text>')
    out.append(f'<line x1="{gx}" y1="{gy+gh}" x2="{gx+gw}" y2="{gy+gh}" stroke="{t["line"]}"/>')
    for i, y in enumerate(years):
        h = max(2, gh * y["commits"] / peak) if y["commits"] else 0
        x0 = gx + i * slot + (slot - bwid) / 2
        hot = y["commits"] == peak
        out.append(f'<rect class="grow growy" style="animation-delay:{i*0.05:.2f}s" x="{x0:.1f}" y="{gy+gh-h:.1f}" width="{bwid:.1f}" height="{h:.1f}" rx="4" fill="url(#mgv)" fill-opacity="{1 if hot else .45}"><title>{y["commits"]} commits in {y["year"]}</title></rect>')
        if y["commits"]:
            out.append(f'<text x="{x0+bwid/2:.1f}" y="{gy+gh-h-6:.1f}" font-size="10.5" fill="{t["dim"]}" text-anchor="middle" font-family="{MONO}">{num(y["commits"])}</text>')
        out.append(f'<text x="{x0+bwid/2:.1f}" y="{gy+gh+17}" font-size="11" fill="{t["faint"]}" text-anchor="middle">{"’" + str(y["year"])[2:]}</text>')
    h = max(by + 46 + ((len(langs) + 1) // 2) * 30, gy + gh + 30) + 10
    return frame(t, 1200, h, "Languages and years", "weighted by my commits", "\n".join(out), "Languages and commits per year")


def card_repos(d, t):
    out = []
    repos = d["repos"]
    peak = max((r["commits"] for r in repos), default=1) or 1
    y0, row = 92, 38
    for i, r in enumerate(repos):
        y = y0 + i * row
        w = 380 * r["commits"] / peak
        out.append(f'''<g class="in" style="animation-delay:{i*0.07:.2f}s">
  <text x="28" y="{y}" font-size="15.5" font-weight="600" fill="{t['fg']}">{esc(r['name'])}</text>
  <rect class="grow growx" style="animation-delay:{0.2 + i*0.07:.2f}s" x="420" y="{y-11}" width="{w:.1f}" height="12" rx="6" fill="url(#mg)" fill-opacity="{1 if i == 0 else .55}"/>
  <text x="{430 + w:.1f}" y="{y}" font-size="13" fill="{t['dim']}" font-family="{MONO}">{num(r['commits'])} commits</text>
  <circle cx="1010" cy="{y-5}" r="6" fill="{r['color']}"/><text x="1022" y="{y}" font-size="13" fill="{t['dim']}">{esc(r['lang'] or '-')}</text>
  <text x="1172" y="{y}" font-size="13" fill="{t['dim']}" text-anchor="end">★ {num(r['stars'])}</text>
</g>''')
    h = y0 + len(repos) * row + 4
    return frame(t, 1200, h, "Where my commits go", f"{d['totals']['repos']} public repositories", "\n".join(out), "Repositories with the most commits")


# ---------- banner and AscEmu builds ----------

def fetch_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": "profile-stats"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "replace")


def collect_builds(cfg):
    # build numbers from the protocol constants, versions from the profiles, state from the ascemu.org sidebar
    src = cfg["ascemu"]
    try:
        proto = dict(re.findall(r"constexpr uint32_t (\w+_BUILD) = (\d+);", fetch_text(src["protocol"])))
    except Exception as e:
        print("protocol source failed:", e)
        proto = {}
    try:
        text = fetch_text(src["versions"])
        versions = {}
        for chunk in text.split("AE_CLIENT_PROFILE == ")[1:]:
            m = re.search(r"#define VERSION ([\d, ]+)", re.split(r"#elif|#else|#endif", chunk)[0])
            if m:
                versions[chunk.split()[0]] = m.group(1).strip().replace(", ", ".")
    except Exception as e:
        print("version source failed:", e)
        versions = {}
    try:
        site = {xp: (label, chip) for xp, label, chip in re.findall(
            r'data-xp="(\w+)"[^>]*><span>([^<]*)</span><span class="num">\d*</span><span class="ae-chip \w+">([^<]*)<', fetch_text(src["site"]))}
    except Exception as e:
        print("site source failed:", e)
        site = {}
    rows = []
    for b in cfg["builds"]:
        label, state = site.get(b["xp"], ("", "Early"))
        version = label.split(" ")[-1] if re.search(r"\d", label.split(" ")[-1] if label else "") else versions.get(b["profile"], "")
        rows.append({"name": b["name"], "version": version, "build": proto.get(b["const"], ""), "state": state,
                     "dark": b["dark"], "light": b["light"]})
    return rows


BRAND = {
    "dark": dict(ok="#3ddc84", dev="#9a9a9a"),
    "light": dict(ok="#1f8f4e", dev="#777777"),
}


def banner_whitemango(d, t, mode):
    W, H = 1200, 300
    tot = d["totals"]
    years = len(d["years"])
    langs = " · ".join(l["name"] for l in d["languages"][:4])
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" aria-label="{esc(d['name'])}, C++ developer">
<defs>
  <linearGradient id="hero" x1="0" y1="0" x2="1" y2=".55"><stop offset=".05" stop-color="#fc00e3"/><stop offset=".9" stop-color="#fc0000"/></linearGradient>
  <linearGradient id="badge" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#fc00e3"/><stop offset="1" stop-color="#fc0000"/></linearGradient>
  <radialGradient id="light"><stop offset="0" stop-color="#fff" stop-opacity=".28"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></radialGradient>
  <pattern id="dots" width="22" height="22" patternUnits="userSpaceOnUse"><circle cx="2" cy="2" r="1.4" fill="#fff" fill-opacity=".16"/></pattern>
  <clipPath id="frame"><rect width="{W}" height="{H}" rx="18"/></clipPath>
</defs>
<style>
  .drift {{ animation: drift 12s ease-in-out infinite alternate; }}
  @keyframes drift {{ from {{ transform: translate(0px, 0px); }} to {{ transform: translate(520px, 30px); }} }}
  .up {{ opacity: 0; animation: up .9s ease-out forwards; }}
  .d1 {{ animation-delay: .15s; }} .d2 {{ animation-delay: .35s; }} .d3 {{ animation-delay: .55s; }}
  @keyframes up {{ from {{ opacity: 0; transform: translateY(10px); }} to {{ opacity: 1; transform: translateY(0); }} }}
  .blink {{ animation: blink 1.1s steps(1) infinite; }}
  @keyframes blink {{ 50% {{ opacity: 0; }} }}
  @media (prefers-reduced-motion: reduce) {{ .drift, .up, .blink {{ animation: none; opacity: 1; }} }}
</style>
<g clip-path="url(#frame)">
  <rect width="{W}" height="{H}" fill="url(#hero)"/>
  <rect width="{W}" height="{H}" fill="url(#dots)"/>
  <g class="drift"><ellipse cx="300" cy="90" rx="380" ry="200" fill="url(#light)"/></g>
</g>
<g transform="translate(70 75)"><g class="up">
  <rect width="150" height="150" rx="34" fill="#fff"/>
  <text x="75" y="97" font-family="{MONO}" font-size="56" font-weight="700" fill="url(#badge)" text-anchor="middle">&lt;/&gt;</text>
</g></g>
<g font-family="{FONT}" fill="#fff">
  <text class="up d1" x="262" y="140" font-size="80" font-weight="700" letter-spacing="-1">{esc(d['name'])}<tspan class="blink" font-weight="300">_</tspan></text>
  <text class="up d2" x="266" y="184" font-size="25" fill-opacity=".95">C++ developer · <tspan font-weight="700">{num(tot['commits'])}</tspan> commits · <tspan font-weight="700">{num(tot['issues_solved'])}</tspan> issues solved · {years} years</text>
  <text class="up d3" x="266" y="226" font-size="17" fill-opacity=".85" font-family="{MONO}">{esc(langs)} · Zürich, Switzerland</text>
</g>
<rect class="up d3" x="266" y="244" width="140" height="3" rx="1.5" fill="#fff" fill-opacity=".85"/>
</svg>
'''


def banner_lines(d):
    tot = d["totals"]
    return num(tot["commits"]), num(tot["issues_solved"]), len(d["years"]), " · ".join(l["name"] for l in d["languages"][:4])


def banner_ascemu(d, t, mode, icon_path):
    W, H = 1200, 300
    logo = {"dark": ("#ffc173", "#ff8a1f", "#b64f04"), "light": ("#e08a2c", "#c4600d", "#7a3504")}[mode]
    colors = [r[mode] for r in d.get("builds", [])] or [t["accent"]]
    seg = W / len(colors)
    stripe = "".join(f'<rect x="{i*seg:.2f}" y="{H-6}" width="{seg+0.5:.2f}" height="6" fill="{c}"/>' for i, c in enumerate(colors))
    commits, solved, years, langs = banner_lines(d)
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" aria-label="{esc(d['name'])}, C++ developer">
<defs>
  <pattern id="grid" width="40" height="40" patternUnits="userSpaceOnUse"><path d="M40 0H0V40" fill="none" stroke="{t['grid']}" stroke-opacity="{t['gridop']}"/></pattern>
  <radialGradient id="glow"><stop offset="0" stop-color="{t['accent']}" stop-opacity="{'.35' if mode == 'dark' else '.18'}"/><stop offset="1" stop-color="{t['accent']}" stop-opacity="0"/></radialGradient>
  <linearGradient id="logo" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{logo[0]}"/><stop offset=".55" stop-color="{logo[1]}"/><stop offset="1" stop-color="{logo[2]}"/></linearGradient>
  <clipPath id="frame"><rect width="{W}" height="{H}" rx="18"/></clipPath>
</defs>
<style>
  .drift {{ animation: drift 14s ease-in-out infinite alternate; }}
  @keyframes drift {{ from {{ transform: translate(0px, 0px); }} to {{ transform: translate(560px, 40px); }} }}
  .up {{ opacity: 0; animation: up .9s ease-out forwards; }}
  .d1 {{ animation-delay: .15s; }} .d2 {{ animation-delay: .35s; }} .d3 {{ animation-delay: .55s; }}
  @keyframes up {{ from {{ opacity: 0; transform: translateY(10px); }} to {{ opacity: 1; transform: translateY(0); }} }}
  @media (prefers-reduced-motion: reduce) {{ .drift, .up {{ animation: none; opacity: 1; }} }}
</style>
<g clip-path="url(#frame)">
  <rect width="{W}" height="{H}" fill="{t['bg']}"/>
  <rect width="{W}" height="{H}" fill="url(#grid)"/>
  <g class="drift"><ellipse cx="260" cy="130" rx="420" ry="230" fill="url(#glow)"/></g>
  {stripe}
</g>
<rect x=".5" y=".5" width="{W-1}" height="{H-1}" rx="17.5" fill="none" stroke="{t['line']}"/>
<g transform="translate(70 62) scale(.80)"><g class="up"><path fill="url(#logo)" fill-rule="evenodd" d="{icon_path}"/></g></g>
<g font-family="{FONT}">
  <text class="up d1" x="290" y="132" font-size="78" font-weight="700" fill="{t['fg']}" letter-spacing="-1">{esc(d['name'])}</text>
  <text class="up d2" x="294" y="174" font-size="25" fill="{t['dim']}">C++ developer · <tspan fill="{t['accent']}" font-weight="600">{commits}</tspan> commits · <tspan fill="{t['accent']}" font-weight="600">{solved}</tspan> issues solved · {years} years</text>
  <text class="up d3" x="294" y="222" font-size="17" fill="{t['faint']}" font-family="{MONO}">{esc(langs)} · Zürich, Switzerland</text>
</g>
</svg>
'''


def banner_stutzer(d, t, mode):
    W, H = 1200, 300
    blue, deep, sun = "#2e55a5", "#12589a", "#fdf9ea"
    commits, solved, years, langs = banner_lines(d)
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" aria-label="{esc(d['name'])}, C++ developer">
<defs><clipPath id="frame"><rect width="{W}" height="{H}" rx="18"/></clipPath></defs>
<style>
  .up {{ opacity: 0; animation: up .9s ease-out forwards; }}
  .d1 {{ animation-delay: .15s; }} .d2 {{ animation-delay: .35s; }} .d3 {{ animation-delay: .55s; }}
  @keyframes up {{ from {{ opacity: 0; transform: translateY(10px); }} to {{ opacity: 1; transform: translateY(0); }} }}
  .slide {{ animation: slide 1.2s cubic-bezier(.2,.7,.2,1) both; }}
  @keyframes slide {{ from {{ transform: translateX(260px); }} to {{ transform: translateX(0); }} }}
  @media (prefers-reduced-motion: reduce) {{ .up, .slide {{ animation: none; opacity: 1; }} }}
</style>
<g clip-path="url(#frame)">
  <rect width="{W}" height="{H}" fill="{blue}"/>
  <g class="slide"><path d="M820 0H1200V300H700Z" fill="{deep}"/><path d="M1000 0H1200V300H930Z" fill="#fff" fill-opacity=".06"/></g>
</g>
<g transform="translate(70 75)"><g class="up">
  <rect width="150" height="150" rx="34" fill="{sun}"/>
  <text x="75" y="97" font-family="{MONO}" font-size="56" font-weight="700" fill="{blue}" text-anchor="middle">&lt;/&gt;</text>
</g></g>
<g font-family="{FONT}" fill="{sun}">
  <text class="up d1" x="262" y="140" font-size="82" font-weight="700" letter-spacing="-2">{esc(d['name'])}.</text>
  <text class="up d2" x="266" y="184" font-size="25">C++ developer · <tspan font-weight="700">{commits}</tspan> commits · <tspan font-weight="700">{solved}</tspan> issues solved · {years} years</text>
  <text class="up d3" x="266" y="226" font-size="17" fill-opacity=".85" font-family="{MONO}">{esc(langs)} · Zürich, Switzerland</text>
</g>
<rect class="up d3" x="266" y="244" width="140" height="3" rx="1.5" fill="{sun}"/>
</svg>
'''


def card_builds(d, t, mode):
    rows = d.get("builds", [])
    b = BRAND[mode]
    W, tw, th, gap, top = 1200, 160, 104, 10, 58
    lines = [rows[:7], rows[7:]]
    out, n = [], 0
    for r, line in enumerate(lines):
        x0 = (W - (len(line) * tw + (len(line) - 1) * gap)) / 2
        for i, e in enumerate(line):
            c = e[mode]
            x, y = x0 + i * (tw + gap), top + r * (th + gap)
            sc = {"Stable": b["ok"], "Focus": c}.get(e["state"], b["dev"])
            sw = 9 + len(e["state"]) * 7.2
            sub = " · ".join(v for v in (e["version"], str(e["build"])) if v)
            fs = 17 if len(e["name"]) <= 12 else 15
            out.append(f'''<g class="tile" style="animation-delay:{n*0.07:.2f}s">
  <rect x="{x}" y="{y}" width="{tw}" height="{th}" rx="12" fill="{t['panel']}" stroke="{t['line']}"/>
  <rect x="{x}" y="{y}" width="{tw}" height="{th}" rx="12" fill="{c}" opacity="{'.07' if mode == 'dark' else '.05'}"/>
  <rect class="bar" x="{x+14}" y="{y+16}" width="26" height="4" rx="2" fill="{c}" style="animation-delay:{n*0.45:.2f}s"/>
  <text x="{x+14}" y="{y+46}" font-size="{fs}" font-weight="600" fill="{t['fg']}">{esc(e['name'])}</text>
  <text x="{x+14}" y="{y+68}" font-size="13" fill="{t['dim']}" font-family="{MONO}">{esc(sub)}</text>
  <rect x="{x+14}" y="{y+78}" width="{sw:.1f}" height="17" rx="8.5" fill="none" stroke="{sc}" stroke-opacity=".7"/>
  <text x="{x+14+sw/2:.1f}" y="{y+90.5}" font-size="10.5" font-weight="600" fill="{sc}" text-anchor="middle" letter-spacing=".4">{esc(e['state'].upper())}</text>
</g>''')
            n += 1
    H = top + 2 * th + gap + 24
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" aria-label="Client builds supported by AscEmu">
<style>
  .tile {{ opacity: 0; animation: in .6s ease-out forwards; }}
  @keyframes in {{ from {{ opacity: 0; transform: translateY(8px); }} to {{ opacity: 1; transform: translateY(0); }} }}
  .bar {{ animation: pulse 5.85s ease-in-out infinite; }}
  @keyframes pulse {{ 0%, 100% {{ width: 26px; }} 8% {{ width: 60px; }} 20% {{ width: 26px; }} }}
  @media (prefers-reduced-motion: reduce) {{ .tile, .bar {{ animation: none; opacity: 1; }} }}
</style>
<g font-family="{FONT}">
  <text x="{W/2}" y="30" font-size="20" font-weight="600" fill="{t['fg']}" text-anchor="middle">One codebase, every expansion</text>
  <text x="{W/2}" y="48" font-size="13" fill="{t['faint']}" text-anchor="middle" font-family="{MONO}">version · client build · state, read from the AscEmu source and ascemu.org</text>
  {"".join(out)}
</g>
</svg>
'''


def render(d, out):
    cards = {"stats": card_overview, "activity": card_activity, "languages": card_languages_years, "repos": card_repos}
    here = os.path.dirname(os.path.abspath(__file__))
    icon = open(os.path.join(here, "..", "assets", "ascemu-icon.svg"), encoding="utf-8").read()
    icon_path = re.search(r'fill-rule="evenodd" d="([^"]+)"', icon).group(1)
    banners = {"whitemango": banner_whitemango, "stutzer": banner_stutzer,
               "ascemu": lambda d, t, mode: banner_ascemu(d, t, mode, icon_path)}

    def write(path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)

    for theme, modes in THEMES.items():
        for mode, t in modes.items():
            write(os.path.join(out, theme, f"banner-{mode}.svg"), banners[theme](d, t, mode))
            for name, fn in cards.items():
                write(os.path.join(out, theme, f"{name}-{mode}.svg"), fn(d, t))
    for mode, t in THEMES["ascemu"].items():
        write(os.path.join(out, f"builds-{mode}.svg"), card_builds(d, t, mode))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", required=True)
    ap.add_argument("--out", default="dist")
    ap.add_argument("--data", help="render from a saved data.json instead of the API")
    a = ap.parse_args()
    if a.data:
        with open(a.data, encoding="utf-8") as f:
            data = json.load(f)
    else:
        token = os.environ.get("GH_TOKEN")
        if not token:
            sys.exit("GH_TOKEN is not set")
        data = collect(token, a.login)
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "profile.json"), encoding="utf-8") as f:
            data["builds"] = collect_builds(json.load(f))
        os.makedirs(a.out, exist_ok=True)
        with open(os.path.join(a.out, "data.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
    render(data, a.out)
    print("cards written to", a.out)


if __name__ == "__main__":
    main()
