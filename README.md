# Resolution Radar — Polymarket

Monitors the **end of life** of Polymarket markets: the stretch between the event and
the payout, which almost nobody watches.

A prediction market doesn't die cleanly. Someone proposes an outcome to the UMA
oracle, a challenge window opens, and sometimes someone disputes it. All the while
the market keeps trading and capital stays locked.

This is the 3<sup>rd</sup> bot in the series, and it answers a question the other
two don't ask:

| Bot | Question |
|---|---|
| Overlap | Who is betting, and are they any good? |
| Coherence | Do the prices contradict each other? |
| **Resolution Radar** | **Is the resolution going wrong?** |

---

## What it detects

### 🔴 Dispute — the signal that matters
A proposed resolution has been challenged, with a bond posted. Measured on history:
**0.4% of resolutions**. It is rare, and therefore informative. The dispute goes to an
UMA vote: days of delay, and an outcome that can still flip.

Real cases found on the first scan: *"Will Hamas agree to disarm by December 31?"*
($101k in liquidity, disputed twice, 139 days before its end date) and *"Will Venezuela
recognize Israel by December 31?"* (disputed twice, price down 64 points).

### 🟠 Proposal gap — proposed but not priced in
A resolution is on the table, but the price hasn't converged to certainty.
Either nobody noticed, or the market disagrees — which often comes before a dispute.

Careful: **24 proposals out of 25 are already at 0.9995**. The idea of "seeing the
truth before the market" doesn't work; this signal is rare by nature.

### 🔵 Shock — a move backed by real volume
A large price move, **far from the end date**, with high turnover.

The decisive filter is the **turnover rate** (24h volume ÷ liquidity). The only
informative shock on the first scan turned over 5.1× its book; the baseball games
whose odds drifted were all below 0.6×. Without this filter: 26 false alerts per
scan, all esports matches that had simply ended.

### 🟡 Stuck — capital tied up
Past its end date for a long time, still open, not even a proposal. Cases
**287 days overdue** exist. On the last scan: **$3.3M in liquidity** spread across
**191 markets** with no settlement date.

---

## What the bot CANNOT tell you

The Polymarket API exposes the resolution **status** (`umaResolutionStatuses`)
but **never the proposed outcome**. The bot therefore describes the situation, never a
direction. It will not say "the proposal says NO" — that information does not exist
in the data, and making it up would be the surest way to make someone lose
money.

---

## Installation

```bash
cd polymarket-resolution-radar
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
```

Discord bot token in `token.txt` (one line) or in `DISCORD_BOT_TOKEN`.
Optional: `DISCORD_GUILD_ID` to register commands instantly.

```bash
./start_mac_linux.sh
```

## Without Discord

The engine is standalone — that's what makes it possible to check the numbers
without starting the bot:

```bash
./venv/bin/python resolution.py                  # all signals
./venv/bin/python resolution.py --only=dispute   # a single type
./venv/bin/python selftest.py                    # 24 tests, synthetic markets
```

A scan covers **~4,000 open markets in 1 second**, using two complementary sort
passes (see below).

## Discord commands

| Command | Effect |
|---|---|
| `/setup` | **Creates the full channel structure** and wires everything up (admin) |
| `/radar [limit]` | Current situations, most serious first |
| `/stuck [limit]` | Overdue markets, most capital locked first |
| `/watch-live` | Subscribes the channel to disputes, proposal gaps and shocks |
| `/watch-stuck` | Subscribes the channel to stuck markets |
| `/unwatch` | Stops alerts in the channel |
| `/board` | Installs the live board, rewritten in place |
| `/stuck-board` | Installs the live stuck-markets board |
| `/dataset` · `/dataset-board` | Growth of the price-history dataset being recorded |
| `/guide` | Posts the "how to read this channel" note |
| `/preview` | Posts a sample alert (checks formatting and permissions) |
| `/status` | Settings, thresholds and last scan |

`/setup` creates the **POLYMARKET RESOLUTION** category: `radar-guide`,
`radar-board`, `dispute-alerts`, `stuck-markets`, `radar-discussion`. Names
are prefixed and the search for existing channels is limited to this category —
otherwise this bot would start writing in the other two bots' channels.

**A baseline snapshot is taken on subscription.** About 35 open situations exist at
any time: without it, installing the bot would fire 35 alerts at once, none of
which would be news.

---

## Implementation details that matter

**Two sort passes, not one.** The API caps pagination at around 2,300 markets
per sort order. So the bot sweeps by **ascending end date** (overdue markets, which
a volume sort never surfaces since they no longer trade) *and* by **volume**
(large markets disputed while their end date is still far away — the Venezuela case,
ending in December and already disputed twice). Either pass alone misses half
the signal.

**`limit` is capped at 100** by the API whatever you ask for. Writing "request
500 and stop when a page returns fewer" exits pagination after the first page:
100 markets instead of 4,000. A trap hit during development.

**Two tiers.** Not everything that is true is news. One more overdue market doesn't
deserve an alert; $3.3M locked up in total does. Signals marked `digest` only
appear on the board and in `/radar`.

## Settings

Everything is at the top of `resolution.py`:

| Setting | Default | Role |
|---|---|---|
| `OVERDUE_DAYS` | 3 | Below this, it's the normal UMA window |
| `OVERDUE_ALERT_LIQUIDITY` | $25k | Above: alert; below: board only |
| `SHOCK_MIN` | 25 pts | Minimum size of the move |
| `SHOCK_MIN_TURNOVER` | 1.0× | **The decisive noise filter** |
| `PROPOSAL_GAP_MIN` | 2% | Distance from certainty |
| `SOON_MIN_LIQUIDITY` | $100k | Threshold for imminent resolutions |

Disputes **always** alert, whatever their size: they are too rare to be
filtered.

---

## Limitations

- **The bot trades nothing** and recommends nothing. It describes.
- **The proposed outcome is not public** (see above). Always check the market
  before acting.
- **A dispute can resolve either way.** A challenge is not a directional signal;
  it is a signal of uncertainty and delay.
- **Silence is the normal state.** With 0.4% of resolutions disputed, if this channel
  talks every day, the thresholds are set wrong.

## License

[MIT](LICENSE)
