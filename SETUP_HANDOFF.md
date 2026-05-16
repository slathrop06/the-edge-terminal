# Setup handoff — make THE EDGE go live

The full system is built locally and verified end-to-end. Today's smoke run produced 2 real picks (Under 7.5 BOS@ATL as ladder, Under 8.5 TOR@DET) from Claude Opus 4.7 with full analytical breakdowns. Your job from here is roughly 15 minutes of clicking.

---

## 1) Push the new code to your GitHub repo

The old repo content (`.gitignore`, `README.md`, `update_picks.py`) gets replaced. The repo is already private, so no one else sees the in-flight history.

```bash
cd /Users/Agent/Documents/EdgePicks

# Replace the old repo state entirely
git add .
git commit -m "Rebuild: deep-intel handicapper + Ladder Challenge + light ESPN UI"

# Force-push to wipe the old commit history (it's a small repo, this is fine)
git push -u origin main --force
```

> If `git push` asks for credentials and you don't have a GitHub PAT/credential helper set up, run `gh auth login` first (assumes you have the `gh` CLI; install with `brew install gh` if not).

---

## 2) Connect Netlify to the new repo layout

The Netlify site (`64f398e6-2d60-4dd7-9064-2b203a6c4a76`) is currently publishing the repo root. We need to point it at `site/`.

- Go to https://app.netlify.com → your `the-edge-terminal` site → **Site configuration → Build & deploy → Build settings → Edit**.
- Set **Publish directory** to `site`.
- Leave **Build command** empty.
- Save.

Then trigger a deploy: **Deploys → Trigger deploy → Deploy site**.

(Or `netlify.toml` should pick this up automatically on next push — it declares `publish = "site"`. The dashboard setting is just belt-and-suspenders.)

---

## 3) Add GitHub Secrets

In the repo settings:

https://github.com/slathrop06/the-edge-terminal/settings/secrets/actions

Add two repository secrets — use the same values from your local `.env`:

- `ANTHROPIC_API_KEY` — the one currently in `.env`
- `ODDS_API_KEY` — the one currently in `.env` (`ba70bab48d599e1f66b8f55fb9840da0`)
- `OPENWEATHERMAP_API_KEY` — optional, skip for now (only adds outdoor MLB weather detail)

---

## 4) Verify the GitHub Actions workflows

After the push, go to https://github.com/slathrop06/the-edge-terminal/actions — you'll see three workflows registered:

- **Morning Picks (6:00 AM ET)** — runs daily at 10:00 UTC
- **Midday Refresh (1 PM + 5 PM ET)** — runs at 17:00 + 21:00 UTC
- **Grader (11:30 PM ET previous day)** — runs at 03:30 UTC

Test the morning run manually:

- Click **Morning Picks → Run workflow → main → Run workflow**.
- Watch it execute (~2-3 minutes). The job will:
  1. Install Python deps
  2. Run `python -m engine.main morning`
  3. Commit `site/data.json`, `site/analytics.json`, `data/*.json` back to `main`

When it pushes, Netlify will see the commit and redeploy automatically (~30 seconds). Refresh https://the-edge-terminal.netlify.app/ and today's picks should appear.

---

## 5) That's it

From here forward, the system is autonomous:

- **6 AM ET daily** — Scott Bot reads the slate, makes picks, ships to the site.
- **1 PM + 5 PM ET** — odds refresh, line-history snapshots taken.
- **11:30 PM ET** — yesterday's picks graded, ladder streak updates, losses get an autopsy.

You don't push code unless you're changing the site design or the engine logic. The boys just refresh the page.

---

## What was built (quick map)

```
engine/
├── intel/
│   ├── schedule.py      # ESPN scoreboards across MLB/NBA/NHL/NFL/CFB
│   ├── market.py        # The Odds API — 9-book line shopping, RLM detection
│   ├── mlb.py           # Probable pitchers + season/L3 stats + park factors + weather
│   ├── nba.py           # team advanced ratings (Net/Off/Def/eFG%/pace)
│   ├── nhl.py           # standings basics
│   ├── football.py      # NFL + CFB scaffold (uses web_search when in season)
│   └── orchestrator.py  # assembles + computes pre-game signals
├── handicapper.py       # Claude Opus 4.7 + web_search tool, rich JSON output
├── validator.py         # 10 deterministic rules
├── ladder.py            # Ladder pick designation + 10-rung streak math
├── publisher.py         # Writes picks_history.json + site/data.json
├── grader.py            # ESPN finals → WIN/LOSS/PUSH + ladder update
├── analytics.py         # Daily/weekly/monthly/yearly/all-time rollups
├── autopsy.py           # Claude post-loss classification
└── main.py              # CLI entry points

site/                    # New light, ESPN-style front-end (no black backgrounds)
.github/workflows/       # Three cron jobs
```

**Today's smoke run produced 2 picks:**
- Under 7.5 BOS @ ATL (+100) — confidence 4, 1.5u, **🪜 LADDER PICK**
- Under 8.5 TOR @ DET (-114) — confidence 3, 1.0u

Both with full thesis paragraphs, data tables, market analysis, case-against, and scott_bot_quip. They live in `data/picks_history.json` and `site/data.json` right now.

---

## Known things to improve later (not blockers)

- **pybaseball is getting 403'd by FanGraphs.** We're still getting plenty from MLB Stats API (ERA, K%, BB%, HR9, L3 ERA) — what we're missing is xFIP/SIERA/Stuff+ and team wRC+. Workarounds: switch to Baseball Savant Statcast endpoints (different host, not blocked), or pin pybaseball to a known-working version.
- **NHL intel is light** — only standings basics. For deeper xGF%/Corsi/PDO we'd need to scrape naturalstattrick or use MoneyPuck. Not urgent.
- **NFL + CFB intel is minimal** — out of season anyway, kick that can to August.
- **Web search tool fallback path** — currently if Anthropic deprecates the tool we'd need to re-wire. Stable for now.
