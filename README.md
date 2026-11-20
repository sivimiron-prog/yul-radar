# YUL Error Fare Radar: setup guide

The Radar scans fares from Montréal several times a day: round trips, one-way flights leaving Montréal, and one-way flights back to Montréal from anywhere. It learns what each route normally costs, flags fares far below normal, has Claude judge whether each one is an error fare, a sale or a false alarm, and sends you an alert on Telegram or by email. It also keeps a phone-friendly page of the latest finds that you can add to your home screen like an app.

## What you need

| Service | What it does | Cost |
|---|---|---|
| Travelpayouts | Fare data for every destination from YUL | Free |
| Telegram bot | Alerts on your phone | Free |
| Anthropic API | AI review and alert writing | Pay per use, a few cents a day at this volume |
| SerpApi (optional) | Checks flagged fares live on Google Flights | 250 free checks a month |
| GitHub | Runs the scan every 4 hours without your computer on, and hosts the page you open on your phone | Free |

Without the Anthropic key, the Radar still works and uses simple built-in rules for the verdict.

## Step 1: Install Python

Install Python 3.10 or newer from python.org. On Windows, tick **"Add Python to PATH"** during setup. Nothing else needs installing because the Radar uses only built-in Python.

Try it right away with simulated fares. Open a terminal in this folder and run:

```
python radar.py --sample
```

Then open `site/index.html` in your browser. That's the page you'll see on your phone.

## Step 2: Get your keys

**Travelpayouts (required)**
1. Create a free account at travelpayouts.com.
2. Go to **Profile → API token** and copy the token.

**Telegram bot (for phone alerts)**
1. In Telegram, search for **@BotFather**, press Start, and send `/newbot`.
2. Pick a name, such as "YUL Radar". BotFather replies with a token like `123456:ABC...`. Copy it.
3. Open your new bot, press **Start**, and send it any message.
4. Put the token in `.env` (see Step 3), then run `python radar.py --telegram-id`. It prints your `TELEGRAM_CHAT_ID`.

**Anthropic API (for the AI review)**
1. Sign up at console.anthropic.com, add a small credit balance, and create an API key.

**SerpApi (optional)**
1. Sign up at serpapi.com. The free plan gives 250 searches a month. The Radar uses at most 3 per scan, on the best candidates only.

## Step 3: Put your keys in `.env`

Copy `.env.example` to a new file named `.env` in the same folder, then paste your keys after the `=` signs. This is also where you change the settings:

- `DEAL_THRESHOLD=0.40` flags fares 40% or more below normal.
- `ERROR_THRESHOLD=0.55` treats fares 55% or more below normal as likely errors.
- `MAX_PRICE=600` ignores anything above $600.
- `ALERT_LANGUAGE=fr` writes alerts in French.

Check that alerts arrive:

```
python radar.py --test-alert
```

Then run a real scan:

```
python radar.py
```

## Step 4: Run it automatically

Error fares often last only a few hours, so the Radar is most useful when it runs every few hours. Choose one of these options.

**Option A: On GitHub (recommended, works with your computer off)**
1. Create a free GitHub account, then create a new **public** repository named `yul-radar`. On a free account, only public repositories can host a web page. Your keys stay private because they go in Secrets (step 3), never in the files. What's public is the program and the fare price history.
2. Upload every file from this folder **except `.env`**. Include the `.github` folder, which is hidden on Mac and Linux. (On the repository page: **Add file → Upload files**, then drag the folder's contents in.)
3. In the repository, go to **Settings → Secrets and variables → Actions → New repository secret**. Add each key with the same name as in `.env`: `TRAVELPAYOUTS_TOKEN`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `ANTHROPIC_API_KEY`, and `SERPAPI_KEY` if you have one.
4. Go to **Settings → Pages**. Under **Build and deployment → Source**, choose **GitHub Actions**.
5. Go to the **Actions** tab, open "Scan fares from YUL", and press **Run workflow** to test it. After that it runs every 4 hours on its own. It saves the price history back to the repository so the baselines keep improving, and it updates your phone page.

Options B and C keep everything on your computer. You still get Telegram and email alerts on your phone, but the phone page only works with Option A.

**Option B: On Windows**
1. Open **Task Scheduler**, then choose **Create Basic Task**.
2. Name it "YUL Radar", set the trigger to **Daily**, then open the task's properties and, under **Triggers**, set it to repeat every 4 hours.
3. For the action, choose **Start a program**. Program: `python`. Arguments: `radar.py`. Start in: the full path of this folder.

**Option C: On Mac**
Run `crontab -e` and add this line, using your own folder path:
```
17 */4 * * * cd /Users/YOU/yul-radar && /usr/bin/python3 radar.py >> radar.log 2>&1
```

## Step 5: Put the Radar on your phone

With Option A running, your page lives at:

```
https://YOUR-GITHUB-NAME.github.io/yul-radar/
```

The link also appears on the **Actions** tab, next to each finished run.

- **iPhone:** open the link in Safari, tap **Share**, then **Add to Home Screen**.
- **Android:** open the link in Chrome, tap the **⋮** menu, then **Add to Home screen** (or **Install app**).

The Radar icon then opens the page full screen, like an app. It shows the finds from the last 7 days, with error fares first. Each card has the price, the normal price, the travel dates, the AI's explanation, a price trend, a button to check the fare on Google Flights, and a button to copy the alert to send to a friend. A green dot means the last scan was recent. An orange dot means no scan has run in the last 8 hours, so check the Actions tab.

Telegram alerts still arrive as notifications, and the page is where you go to look at all the finds together.

## How detection works

1. **Scan:** for each of the next 10 months, the Radar fetches the cheapest round trips from YUL to every destination.
2. **Normal price:** the Radar compares each fare to the median price for that destination and month over the last 90 days. In the first days, before enough history exists, it compares against the other prices from the same scan, and alerts say so.
3. **Flag:** it keeps the cheapest fare per destination per month that is far enough below normal.
4. **Live check:** if SerpApi is set up, the top candidates are checked on Google Flights. A fare that no longer appears there is marked as a false alarm.
5. **AI review:** Claude looks at the drop, the season, the routing and the live check, then gives a verdict and writes the alert.
6. **Alert:** you get the alert with a Google Flights link. The same destination and month won't alert again for 3 days unless the price drops another 5% or more.

## Limits to know

- Travelpayouts fares come from searches made by Aviasales users in the last 48 hours, so some are already gone by the time you see them. The Google Flights check and the booking links let you confirm quickly.
- Detection improves over time. After about 2 weeks of scans, each route has a solid price history.
- Airlines sometimes cancel error fares. After booking, wait a few days before paying for anything non-refundable.
- The scan covers economy fares only. Business-class errors would need another data source.
- On the phone page, switch between **Round trip**, **From Montréal** and **Back to Montréal**, and tap **Europe** to see only European cities.
