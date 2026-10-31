# Launch kit for india-trading-skills

This file sits outside the repo on purpose, so it won't be pushed.

## 1. Before you push

Replace the username placeholder everywhere (README badges, install commands, plugin manifest):

```bash
cd /home/shubham/Code/new/india-trading-skills
grep -rl YOUR_GITHUB_USERNAME . | xargs sed -i 's/YOUR_GITHUB_USERNAME/<your-username>/g'
python3 -m unittest discover -s tests        # should print OK
```

Then create an **empty** public repo named `india-trading-skills` on GitHub (no README or licence,
since the repo already has them) and push:

```bash
git init -b main
git add .
git commit -m "India Trading Skills v1.0.0: 18 Claude skills for NSE/BSE"
git remote add origin https://github.com/<your-username>/india-trading-skills.git
git push -u origin main
```

## 2. Repo settings (10 minutes, big impact)

- **About → Description:** `18 Claude skills that make Claude a Dalal Street analyst: NSE/BSE charts, NIFTY F&O, exact STT & charges, Indian tax, Groww/Zerodha/Dhan MCP. Tested, zero-dependency calculators.`
- **About → Topics** (GitHub allows 20): `claude` `claude-code` `claude-skills` `agent-skills` `anthropic` `mcp` `nse` `bse` `nifty` `sensex` `indian-stock-market` `stock-market` `trading` `options-trading` `zerodha` `groww` `technical-analysis` `income-tax` `fintech` `india`
- **Settings → General → Social preview:** upload `docs/assets/social-preview.png`. This is the card people see on X, LinkedIn and WhatsApp.
- **Settings → General → Features:** enable **Discussions**. The issue template links to it.
- **Settings → Code security:** enable **Private vulnerability reporting**. SECURITY.md points to it.
- **Releases → Draft a new release:** tag `v1.0.0`, title "v1.0.0: 18 skills for Indian markets", and paste `CHANGELOG.md`.
- **Actions:** open the "data freshness" workflow and click *Run workflow* once to confirm it works.
- **Your profile:** pin the repo.

## 3. The biggest lever: a 30–45 second demo video

A screen recording beats any text. Record Claude Code answering one prompt end to end, for example
"Analyze RELIANCE, I hold 100 at ₹1,250" or "Build a NIFTY iron condor for Tuesday". Use OBS, or
`asciinema` + `agg` for a GIF. Put it at the top of the README, under the banner, and attach it to
every post. The terminal screenshots in `docs/assets/` work as a fallback.

## 4. Launch posts (edit freely)

### X / Twitter thread

1/ Most AI stock analysis for India is confident arithmetic with stale numbers.

So I built 18 Claude skills that know the 2026 NSE/BSE rulebook and do the maths with tested
calculators. Open source. 🧵👇 [banner + demo]

2/ Things it gets right that most tools don't:
• A delivery round trip costs ~0.23% to break even, not 0.1%
• After the Apr-2026 STT hike a NIFTY futures lot needs ~14 points just to pay charges
• 2026 had 6 Tuesday holidays, so NIFTY expiries shift. It handles that

3/ Ask it anything:
"How many NIFTY lots with ₹8L and a 150-pt stop?"
"Charges on Groww vs Zerodha for this trade?"
"F&O loss + LTCG + salary: my tax?"
[charges + tax screenshots]

4/ Connects to Groww, Zerodha Kite, Dhan and Upstox MCP for live data. It never places an order
without showing you a full ticket and getting a "yes".

5/ 5 zero-dependency Python calculators, 80 tests, MIT licence. Built on top of
@<original author handle, if they have one>'s nse-trading-skills.
⭐ github.com/<you>/india-trading-skills

### LinkedIn

I built an open-source "Dalal Street analyst" for Claude.

India Trading Skills is a pack of 18 Claude skills for NSE/BSE traders and investors. It knows the
2026 rules (the new F&O STT, NIFTY lot size 65, Tuesday expiries, the ₹1.25 lakh LTCG exemption, F&O
set-off rules), and it uses tested calculators instead of letting the AI guess.

A few things surprised me while building it:
→ Delivery trades cost ~0.23% round trip, more than double the commonly quoted 0.1%
→ A NIFTY futures lot now needs ~14 points just to cover charges
→ Your F&O loss can offset capital gains, but never your salary

It connects to Groww, Zerodha, Dhan and Upstox through their official MCP servers, and it will
never place an order without explicit confirmation.

Free and open source: github.com/<you>/india-trading-skills
(Educational tool, not investment advice.)

### Reddit

Good fits: r/ClaudeAI, r/ClaudeCode, r/developersIndia (check the showcase-day rules),
r/IndianStockMarket, r/IndiaInvestments, r/IndianStreetBets. **Read each subreddit's
self-promotion rules first**, and lead with what it does, not the link.

Title: *I built open-source Claude skills that know India's 2026 market rules (STT, lot sizes,
expiries, F&O tax) and do the maths with tested calculators*

Body: 3–4 lines on the problem (stale AI answers), 3 bullets with the surprising facts above, one
screenshot, the link, and "feedback and rate-update PRs welcome".

### Hacker News

Title: `Show HN: Claude skills for Indian stock markets with tested calculators`
Post on a weekday, roughly 8–10 AM US Eastern (6:30–8:30 PM IST). First comment: why you built
it, how the calculators are verified (indicator cross-check, hand-checked tests), and what's next.

## 5. Directories and lists

Submit a PR or listing (check each one's current contribution rules):
- awesome-claude-skills lists on GitHub (for example travisvn/awesome-claude-skills and
  ComposioHQ/awesome-claude-skills)
- awesome-claude-code (hesreallyhim/awesome-claude-code)
- Claude plugin and skill directories (for example claudemarketplaces.com)
- skills.sh, so `npx skills add` users can find it

## 6. Keep the momentum

- Reply to every issue within a day in the first two weeks.
- Pin an issue: "Rates changed? Report it here" (the data-update template does the rest).
- Post a short update whenever a rule changes (lot-size revision, Budget day, holiday list in
  December). Timely posts get reshared.
- Label a few `good first issue` items from the roadmap (new broker profiles are easy).

## 7. Stay compliant

- Don't give stock tips, target prices or return promises in posts. SEBI's finfluencer rules apply
  to promotion that looks like advice.
- Keep the "educational, not investment advice" line in every post.
- Don't use broker logos in promo images. Naming the brokers you integrate with is fine.
