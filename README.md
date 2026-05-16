# THE EDGE — Scott Bot's Daily Picks Terminal

> Three picks daily. One ladder pick carries the streak. Paper-traded. Tracked publicly.

Live: **https://the-edge-terminal.netlify.app/**

Scott Bot (Claude Opus 4.7) reads every morning's MLB / NBA / NHL / NFL / CFB slate, calls in real odds + pitcher splits + park factors + line movement + late news, and writes up to three picks with full analytical breakdowns. One pick per day is designated the **ladder pick** — get 10 ladder wins in a row and you complete a climb.

## How it runs

- **6:00 AM ET** — GitHub Actions runs the morning pipeline: harvest intel → call Claude → validate → publish.
- **1:00 PM + 5:00 PM ET** — Actions re-harvests odds + regenerates the site data.
- **11:30 PM ET** — Actions grades the day's picks via ESPN finals, updates the ladder, runs autopsy on losses.

Every job commits the updated `site/data.json` + `site/analytics.json` back to `main`. Netlify auto-deploys on every push. The static site never needs a manual deploy — the boys just refresh the page.

## Layout

```
site/                # static front-end (deployed to Netlify)
engine/              # Python backend
  intel/             # per-game data gathering (MLB, NBA, NHL, football, market, schedule)
  handicapper.py     # calls Claude with web_search tool
  validator.py       # 10 deterministic rules
  publisher.py       # writes picks_history.json + site/data.json
  ladder.py          # ladder streak math
  grader.py          # ESPN finals → WIN/LOSS/PUSH + units P/L + CLV
  analytics.py       # rollups across all scopes + ladder stats
  autopsy.py         # Claude post-loss classification
  main.py            # CLI entry points (morning|midday|grader|refresh)
prompts/             # Scott Bot system prompt
data/                # source-of-truth JSON ledgers
.github/workflows/   # the three crons
```

## Secrets required (GitHub repo → Settings → Secrets and variables → Actions)

- `ANTHROPIC_API_KEY` — Claude API key
- `ODDS_API_KEY` — the-odds-api.com key
- `OPENWEATHERMAP_API_KEY` — optional, only used for outdoor MLB weather context

## Local dev

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # fill in keys

# Generate today's picks
python -m engine.main morning

# Grade yesterday's
python -m engine.main grader

# Refresh site JSON without re-harvesting
python -m engine.main refresh

# Serve the site locally
cd site && python -m http.server 8000
```

## Operating principles

1. **The brain is Claude. The discipline is code.**
2. **No invented data** — if it isn't in the intel pack, it isn't in the pick.
3. **CLV is the truth, not W/L.**
4. **Max 3 picks/day. Forever.** Zero is allowed.
5. **No real money.** Paper-trade only.
6. **Better to publish nothing than garbage.**
