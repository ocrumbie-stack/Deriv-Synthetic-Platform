# Platform Fix Checklist

Regression guide for the Deriv Synthetic Trading Platform: every fix and feature
shipped since the move to Deriv, with how to test it and what you should see.
Re-check the items for any area a change touches before calling it done.

Tick results off on the shared page, which saves Pass/Fail and notes:
https://claude.ai/artifact/3wDXMfsGPzdKZmk3u2JMkd

This file and that page list the same items - when adding a fix, update both.

## Check these first

- **Multiplier safety cap is off while trading live.** `max_multiplier_floor` defaults to 10000 (raised for demo testing in e472ef5). Set `MAX_MULTIPLIER_FLOOR` on Railway (e.g. 20-50). See C7.
- **Open issue:** `UNIQUE constraint failed: symbol_leverage.symbol` for frxXAUUSD (27 Sep 19:27 UTC). See F3.
- **Open issue:** `database is locked` on the Open Positions refresh (self-heals). See I10.
- **Live v25 entries rejected as "An open position already exists" (28 Sep).** A demo position left open was blocking live signals. After deploying the fix, switch to demo and close any leftover v25 position there. See H6.
- **step200 alert has the wrong webhook secret** - its signals are rejected as "Invalid webhook secret". Update the alert message in TradingView.

## A. Signals from TradingView

| ID | Check | How to test | Expect | Commit |
|---|---|---|---|---|
| A1 | Webhook answers instantly | Fire any alert and look at TradingView's alert log and Railway logs. | Railway shows POST /webhook 202 Accepted. TradingView shows no timeout. | cf563e6 (22 Jun) |
| A2 | Synthetic index names resolve to Deriv codes | Send an alert with a TradingView-style name such as VOLATILITY_50_1S_INDEX. | Journal row is executed. No InvalidInputAsset / unknown symbol error. | 5bb63a0 (13 Sep) |
| A3 | STEP_INDEX maps to Step Index 100 | Send an alert with symbol STEP_INDEX. | Trade opens on Step Index 100 (stpRNG). | 6ddd156 (26 Sep) |
| A4 | FX, metal and crypto tickers accepted | Send EURUSD, OANDA:XAUUSD or BTCUSD. | Resolved to frxEURUSD / frxXAUUSD / cryBTCUSD and executed. | e1b46b8 (27 Sep) |
| A5 | Perpetual suffixes stripped | Send a symbol ending in .P or .PERP. | Suffix removed; symbol resolves normally. | e991183 (21 Jun) |
| A6 | Entries need a registered Signal Bot | Send an entry for a strategy name with no bot. | Journal shows it rejected with a reason. Nothing is opened. | 9197ca4 (25 Sep) |
| A7 | Stake and leverage come from the platform, not the alert | Send an alert with a different size/leverage from the bot's settings. | Trade uses the bot's stake and the bot/Leverage-page multiplier. | 9197ca4 (25 Sep) |
| A8 | Back-to-back alerts no longer lock the database | Trigger a reversal (exit + entry fired together). | Both rows appear in the journal, in order. No "database is locked" in Railway logs. | ae0ba11 (27 Sep) |
| A9 | Reversals handled by the platform | With a long open, send a short entry. Then send a second short entry. | Long closes (journal: reversal) and short opens. The second same-direction entry is rejected. | 44d4a1c (25 Sep) |
| A10 | Standalone exit webhook works | POST to /webhook/exit for an open position, including while its bot is paused. | Position closes; journal reason is webhook_exit. | 2e3b7f6 (25 Sep) |
| A11 | Webhook secret is not exposed | Open /api/config and the alert templates on the Signal Bots page. Open /api/webhook-secret in a private window. | No secret in /api/config. Templates on screen show YOUR_SECRET. /api/webhook-secret needs sign-in ("Not signed in."). | b7c685c (26 Sep) |
| A16 | Copied alerts work as pasted | Create a bot, click Copy on its TradingView Alert (and on the webhook payloads and Exit → Get exit webhook), paste into a text editor. | The pasted "secret" is the real one, not YOUR_SECRET. An alert pasted unedited into TradingView executes. | 69d2baa (28 Sep) |
| A12 | Webhook secret kept out of the logs | Send an alert that fails (e.g. an unknown symbol) and read the Railway log line. | The logged payload has no secret field. | 7644347 (27 Sep) |
| A13 | Reversals fit within max account exposure | Set max account exposure to one bot stake. With a position open, send an opposite entry. | Old position closes and the new one opens. No "Signal would exceed max account exposure." | 4582910 (28 Sep) |
| A14 | A blocked reversal still closes the old position | With a position open, pause its bot (or hit a daily loss limit), then send an opposite entry. Repeat with a wrong secret. | Entry is rejected with "…The opposite position was still closed." and a reversal row follows. With the wrong secret nothing closes. | 4582910 (28 Sep) |
| A15 | Symbol case and spaces never break an exit | With a position open, send an exit (and separately a /webhook/exit) with the same symbol in mixed case or with a trailing space, e.g. "Volatility_25_Index ". | Position closes. No "No open position exists for this strategy and symbol." | e2199ce (28 Sep) |

## B. Opening trades on Deriv

| ID | Check | How to test | Expect | Commit |
|---|---|---|---|---|
| B1 | Orders authenticate | Place a demo and a live entry. | No authorization errors (credentials are trimmed, OTP flow used). | 3e8008c (12 Sep) |
| B2 | Proposal and buy on one connection | Place any entry. | No "Unknown contract proposal" / InvalidContractProposal. | 691e38f (13 Sep) |
| B3 | Trades are Multiplier contracts | Open a trade and look at it in Deriv. | Contract type is MULTUP or MULTDOWN and stays open until sold. | 883a18d (13 Sep) |
| B4 | Leverage snaps to a value Deriv accepts | Set a leverage that isn't valid for the symbol. | Order goes through at the nearest valid multiplier. No MultiplierOutOfRange. | 63b3772 (14 Sep) |
| B5 | Failed orders don't leave stuck positions | Force a failure (e.g. invalid symbol) on an entry. | Trade marked failed and closed; the next entry isn't blocked. | 63e8213 (12 Sep) |
| B6 | Bot TP/SL is set on each trade | Open a bot trade with TP/SL % set, then check the contract on Deriv. | Contract has a take profit / stop loss matching the % of stake. | e4e8d84 (26 Sep) |
| B7 | TP/SL that rounds to $0 doesn't fail the order | Use a tiny stake with a small TP/SL %. | Order opens; the $0 side is just left off. | 27d2b4c (27 Sep) |
| B8 | Deriv calls share one login (no rate limit) | Keep the dashboard open in two tabs with several positions open, and let a reversal fire. | No "OTP request failed (429)" / RateLimit in Railway logs or the journal. Reversals close the old position every time. | 7644347 (27 Sep) |
| B9 | Leverage column shows the real multiplier | Open a trade from a bot with leverage "Auto" and no Leverage-page setting for the symbol. Compare Open Positions with the contract on Deriv. | Leverage matches Deriv's multiplier (e.g. 50x on Volatility 25), not 1x. | 237b0a6 (28 Sep) |

## C. Closing trades and P&L accuracy

| ID | Check | How to test | Expect | Commit |
|---|---|---|---|---|
| C1 | Closes sell the exact contract | Close a position, then check Deriv's open positions. | That contract is sold on Deriv. No orphaned open contract. | 9179651 (16 Sep) |
| C2 | P&L uses Deriv's own figure | Compare a few closed trades to Deriv's profit table. | Net result matches Deriv's sell minus buy. No large price-difference numbers. | f89f5e0 (26 Sep) |
| C3 | Impossible losses are blocked | Scan Trade History for losses far bigger than the stake. | None. Any clamped figure is logged on Railway. | d2fb0f2 (26 Sep) |
| C4 | Deriv-side closes are picked up | Let a trade hit its TP/SL or stop out on Deriv. | Trade shows closed with Deriv's profit; journal adds a deriv_close row. | b7c685c (26 Sep) |
| C5 | Still-open contracts are never marked closed | Compare Open Positions to Deriv's open contracts. | Same list on both. Nothing open on Deriv is missing from the dashboard. | b573823 (16 Sep) |
| C6 | Platform closes don't crash | Close a bot trade from the dashboard. | Success, not "Close failed". Journal reason is manual_close, not deriv_close. | 049199a (27 Sep) |
| C7 | Multiplier safety cap is set for live | Check Railway variables for MAX_MULTIPLIER_FLOOR. | Set to a sensible value (e.g. 20–50). Currently defaults to 10000, which means no cap. | e472ef5 (16 Sep) |
| C8 | Daily P&L audit runs clean | Open /api/trade-audit or search Railway logs for "Trade audit". | Recent run with 0 flagged. Any corrections explained. | 313369a (26 Sep) |

## D. Open Positions exit menu

| ID | Check | How to test | Expect | Commit |
|---|---|---|---|---|
| D1 | Close now | Exit → Close on an open position. | Closes at market; journal reason manual_close. | 4e49bd1 (25 Sep) |
| D2 | Close at price waits for the market | Exit → Close at price, with a level away from the current price. | Position stays open showing the armed price; closes when spot crosses it (price_exit). Change and cancel both work. | f5da257 (27 Sep) |
| D3 | Set TP/SL in dollars | Exit → Set TP/SL ($). Enter values, then clear one. | Shows current values from Deriv; new values appear on the Deriv contract; a blank side is removed. | c1f53f9 (27 Sep) |
| D4 | Exit webhook payload | Exit → Get exit webhook. | Modal with URL and payload, each with its own Copy button. No "Copy & close". | 8234b05 (25 Sep) |
| D5 | Close all | Open Positions with two or more trades → Close all → confirm. Then look with no positions open. | Dialog lists every position and names the mode. All close at market (journal: manual_close); only the current mode's positions are touched. If one fails, the rest still close and a dialog names the ones still open. Button hidden when nothing is open, with no jump in the header. | be059a8 (28 Sep) |
| D5 | In-app dialogs replace browser pop-ups | Trigger a close, delete a bot, switch to live. | Styled dialog with position details; errors show inline. No browser confirm/alert boxes. | fe93881 (27 Sep) |
| D6 | Trailing stop | Run a bot with trail start/distance set and let a trade go into profit then pull back. | Closes once profit drops the trail distance below its peak; journal reason trailing_stop. Peak survives a redeploy. | 161cb0c (26 Sep) |

## E. Signal Bots

| ID | Check | How to test | Expect | Commit |
|---|---|---|---|---|
| E1 | Pause/Resume is a real button | Pause a bot, then send it an entry and an exit. | Entry rejected; exit still closes the open position. | 338f426 (26 Sep) |
| E2 | Session P&L survives pause/resume | Note a bot's session P&L, pause, resume. | Same P&L and cycle count. Only a coin stopped by its cycle cap starts fresh. | 1cd9619 (27 Sep) |
| E3 | Reset session button | Click Reset session on a bot. | Bot and all its coins go back to $0 and 0 cycles. | 1cd9619 (27 Sep) |
| E4 | Bot defaults reach coins that already traded | Change a bot's default TP/SL % or max cycles. | Coins on the old default update (and their open contract's TP/SL); customised coins keep theirs. | 27d2b4c (27 Sep) |
| E5 | Save errors show on the input | Enter an invalid bot/pair value and save. | Error shown next to the field, not silently ignored. | 27d2b4c (27 Sep) |
| E6 | Blank bot leverage means Auto | Leave bot leverage blank; set the symbol on the Leverage page. | Trade uses the Leverage page's value. | 89c7705 (15 Sep) |
| E7 | Hedge checkbox layout | Open the bot form. | Hedge checkbox lines up with its label. | 41f52f8 (9 Sep) |

## F. Leverage page

| ID | Check | How to test | Expect | Commit |
|---|---|---|---|---|
| F1 | Only valid multipliers can be picked | Open the Leverage page and a symbol's dropdown. | Options match Deriv's allowed list; the server rejects anything else. | 943d4ac (15 Sep) |
| F2 | Lists synthetics, FX, metals and crypto | Scroll the Leverage page. | FX, metals and crypto appear alongside synthetic indices. | e1b46b8 (27 Sep) |
| F3 | No duplicate-symbol error on save | Change leverage on frxXAUUSD (and another new symbol) and save. | Saves cleanly. On 27 Sep this raised UNIQUE constraint failed: symbol_leverage.symbol — still to fix. | open issue |
| F4 | Page loads without holding up the dashboard | Open the dashboard right after a deploy. | Everything loads within seconds; the Leverage table doesn't block other panels. | 23f40ba (15 Sep) |

## G. Signal Journal

| ID | Check | How to test | Expect | Commit |
|---|---|---|---|---|
| G1 | Every row has a reason | Scroll the journal. | Each row shows one of: strategy_entry, strategy_exit, reversal, webhook_exit, manual_close, price_exit, trailing_stop, deriv_close — or the error for failed/rejected rows. | b7c685c (26 Sep) |
| G2 | Reversal and dashboard closes are logged | Do a reversal and a dashboard close. | Both appear in the journal with their reason. | 8dd0dac (25 Sep) |

## H. Demo and live

| ID | Check | How to test | Expect | Commit |
|---|---|---|---|---|
| H1 | Mode switch works and asks first | Switch demo → live → demo from the dashboard. | Confirmation dialog appears; live is refused if Deriv live credentials are missing. | 8950bd9 (24 Sep) |
| H2 | Demo is a real Deriv virtual account | Switch to demo and compare the balance to your Deriv demo account. | Balances match; demo trades appear on the Deriv virtual account. | d7d3d27 (24 Sep) |
| H3 | DEMO/LIVE badge under the logo | Look at the sidebar. | Badge under the logo matches the current mode. | a951fa8 (24 Sep) |
| H4 | Demo and live data kept apart | Switch modes and look at Trade History, Journal, Positions and Overview. | Each mode shows only its own rows and stats. | d7937b2 (25 Sep) |
| H5 | Risk limits count only the current mode | Compare the daily loss/exposure figures in each mode. | Demo losses don't count toward live limits, and the reverse. | 8baf7c6 (24 Sep) |
| H6 | A demo position never blocks or gets reversed by live signals | Leave a demo position open, switch to live, then send an entry for the same strategy and symbol, first in the same direction and then in the opposite direction. | Both live entries execute. No "An open position already exists" rejection. The demo position is untouched and still open when you switch back to demo. | 4582910 (28 Sep) |

## I. Dashboard reliability

| ID | Check | How to test | Expect | Commit |
|---|---|---|---|---|
| I1 | Shows real data on first load | Open the dashboard in a fresh tab. | Real balance and mode, not the $12,450 / PAPER placeholder. | 7541139 (15 Sep) |
| I2 | Doesn't freeze when Deriv is slow | Watch the dashboard over a few minutes of refreshes. | Panels keep updating; a slow balance call doesn't stall the rest. | 4f84eec (24 Sep) |
| I3 | No "Application failed to respond" | Use the dashboard with several open positions. | Pages keep responding; no Railway error screen. | 80c9f5c (25 Sep) |
| I4 | Open tabs reload onto a new deploy | Leave a tab open across a deploy. | Tab reloads by itself once no dialog or field is in use. | 5e4732e (27 Sep) |
| I5 | Opening a page link directly works | Refresh the browser while on #trading-bots. | Page loads normally, not blank. | 338f426 (26 Sep) |
| I6 | Live unrealized P&L moves | Watch Open Positions with a trade open. | P&L changes with the market and matches Deriv. | 54b378e (16 Sep) |
| I7 | No Deriv rate-limit errors | Search Railway logs for 429. | None during normal use. | 7f4f366 (16 Sep) |
| I8 | Account balance is correct | Compare the dashboard balance with Deriv for the current mode. | Matches. | 1acfe68 (12 Sep) |
| I9 | Works on a phone | Open the dashboard on a phone. | No sideways scrolling; headers and controls wrap. | 1e0ce52 (16 Sep) |
| I10 | Open Positions refresh doesn't lock the database | Search Railway logs for "database is locked" coming from /api/open-positions. | None. On 27 Sep it failed twice while recording a Deriv-side close (self-heals on the next refresh) — still to fix. | open issue |
| I11 | Signal bot position badges colour by P&L | Open Signal Bots with a winning and a losing position (try a losing long and a winning short). | Badge is green when the position is up, yellow when down, plain when flat or not yet priced — regardless of direction. | 5b52adc (28 Sep) |
| I12 | Summary cards on Open Positions | Open Open Positions with two or more trades, then with none. | Four cards above the table: Account Balance (with available), Open Positions (long/short split), Account Exposure (% of limit, yellow above 80%), Running P/L (sum of the Unrealized P/L column, green if up, yellow if down). Running P/L doesn't flash — between refreshes; with no positions it shows $0.00. | fd49df2 (28 Sep) |

## J. Trade History and charts

| ID | Check | How to test | Expect | Commit |
|---|---|---|---|---|
| J1 | Symbol filter and stats | Pick a symbol in Trade History and wait for a refresh. | Filter stays set; count, win rate and P&L cover just that symbol; up to 1000 trades load. | 4a5df41 (16 Sep) |
| J2 | Net P&L by Symbol chart | Look at the chart, especially the largest negative bar. | Bars sorted best to worst; value labels never overlap symbol names. | 177a93e (17 Sep) |
| J3 | Overview charts use real data | Open Overview. | Charts reflect your actual trades. | 511d682 (3 Jul) |

## K. Access and data safety

| ID | Check | How to test | Expect | Commit |
|---|---|---|---|---|
| K1 | Google sign-in protects the dashboard | Open the dashboard in a private window. | Google sign-in required; only allowed emails get in. Webhooks still work without sign-in. | eea0300 (26 Sep) |
| K2 | Data survives deploys | Check bots and history after a redeploy. | Nothing lost. | f79d4ac (13 Sep) |
