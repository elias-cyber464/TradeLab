# TradeLab

A fantasy football trade calculator that updates itself. Every 3 hours, GitHub pulls fresh data, re-runs the model, and republishes the site. It costs nothing to run.

**What goes into each player's value**

| Input | Source (all free) |
|---|---|
| Real trades | [FantasyCalc](https://www.fantasycalc.com) redraft values, built from real league trades |
| Expert consensus | FantasyPros rest-of-season rankings, via [DynastyProcess](https://github.com/dynastyprocess/data) |
| Production | Points per game from [nflverse](https://github.com/nflverse) box scores |
| Team situation | Team scoring + starting QB health |
| Strength of schedule | Fantasy points each remaining opponent allows by position, rest of season and Weeks 15–17 |
| Injury risk | [Sleeper](https://docs.sleeper.com) injury status + two-season durability + bye weeks |
| News | RSS headlines scored by AI (GitHub Models, free) or keyword rules, plus Sleeper waiver trends |

## Setup (about 10 minutes)

1. **Create a GitHub account** at github.com if you don't have one.
2. **Create a new repository.** Click **+ → New repository**, name it `tradelab`, and set it to **Public** (free GitHub Pages needs a public repo). Don't add a README.
3. **Upload the files.** On the empty repo page, click **uploading an existing file**. Drag in everything from this folder, including the hidden `.github` folder. On a Mac, press `Cmd+Shift+.` in Finder to show hidden folders. Then click **Commit changes**.
   - If the `.github` folder won't upload, create the file by hand: **Add file → Create new file**, name it `.github/workflows/update.yml`, and paste in the contents of that file.
4. **Turn on Pages.** Go to **Settings → Pages**, and under **Build and deployment → Source**, choose **GitHub Actions**.
5. **Run it once.** Go to the **Actions** tab. If asked, click **I understand my workflows, go ahead and enable them**. Then pick **Update data and deploy → Run workflow**. It takes 2–3 minutes.
6. **Open the site:** `https://YOUR-USERNAME.github.io/tradelab/`

After that it runs on its own every 3 hours.

## Changing things

- **How often it updates:** edit the `cron` line in `.github/workflows/update.yml`. For example, `"17 */1 * * *"` runs every hour. Public repos get unlimited free Actions minutes.
- **News sources:** edit `data/feeds.json`. Any RSS or Atom feed works.
- **AI news scoring:** it's on by default and free through GitHub Models. To use keyword rules only, add `TRADELAB_AI: "off"` under `env:` in the workflow.
- **Model weights:** use the sliders on the site. Each visitor's settings are saved in their own browser.

## Troubleshooting

- **The workflow fails on the `models: read` line:** delete that line. News scoring falls back to keyword rules.
- **A source shows "failed"** in How it works → Data status: that source was down or changed its format. Everything else still updates, and the next run tries again.
- **Updates stop after about 60 days:** GitHub pauses scheduled workflows on repos with no activity. The bot's news commits usually count, but if it pauses, click **Enable workflow** in the Actions tab.

## Running locally

```bash
pip install -r requirements.txt
python -m pipeline.build          # writes site/data.json
python -m http.server -d site     # open http://localhost:8000
```

Offline test with sample data (no network needed beyond one git clone):

```bash
git clone --depth 1 https://github.com/dynastyprocess/data /tmp/dp
python tests/make_fixtures.py /tmp/dp/files
TRADELAB_FIXTURES=tests/fixtures TRADELAB_AI=off python -m pipeline.build
```

## Project layout

```
pipeline/sources.py   fetchers for each data source
pipeline/news.py      RSS → player matching → AI or rule scoring, with decay
pipeline/build.py     joins everything and computes team, schedule, and durability inputs → site/data.json
site/index.html       the app (reads data.json; all value math runs in the browser)
data/feeds.json       news feeds to read
data/news_history.json  rolling 21-day news log (maintained by the bot)
.github/workflows/update.yml  the schedule
```
