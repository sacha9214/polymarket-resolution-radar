"""
Resolution Radar — alertes Discord sur la fin de vie des marchés Polymarket.

Toute la détection vit dans `resolution.py`, testable sans jeton Discord
(`python3 resolution.py`). Ce fichier n'est que la couche de présentation.

Jeton : token.txt à côté de ce script (une ligne), ou DISCORD_BOT_TOKEN.
"""

# Surtout PAS de `from __future__ import annotations` : py-cord lit les
# annotations pour typer les options du menu Discord. En mode PEP 563 elles
# deviennent des chaînes, py-cord ne reconnaît plus `int`/`float` et affiche des
# champs texte à la place des champs numériques, sans la moindre erreur.

import asyncio
import fcntl
import re
import os
import sqlite3
import sys
import time
from pathlib import Path

import discord
from discord.ext import tasks

import history as H
import resolution as R

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
if not TOKEN:
    _f = Path(__file__).with_name("token.txt")
    if _f.exists():
        TOKEN = _f.read_text(encoding="utf-8").strip()

GUILD_ID = int(os.environ.get("DISCORD_GUILD_ID", "0") or 0)
GUILDS = [GUILD_ID] if GUILD_ID else None

# Une résolution ne bouge pas à la seconde : inutile de marteler l'API.
POLL_MINUTES = int(os.environ.get("RADAR_POLL_MINUTES", "10") or 10)

# Voir/écrire/embeds + créer les salons (/setup) + épingler.
INVITE_PERMS = 1024 | 2048 | 16384 | 16 | 8192

# Au-delà, on plafonne : un pic de résolutions ne doit pas transformer le salon
# en mur de texte.
MAX_ALERTS_PER_CYCLE = 6

# Les clés vues sont purgées après ce délai — sinon la table enfle indéfiniment.
SEEN_TTL_DAYS = 30

_LOCK_PATH = Path(__file__).with_name("bot.lock")
_lock_file = None


def acquire_single_instance_lock():
    """Deux instances se battraient pour enregistrer les commandes (chacune
    efface celles de l'autre) et doubleraient les alertes."""
    global _lock_file
    _lock_file = open(_LOCK_PATH, "w")
    try:
        fcntl.flock(_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit(
            "Another instance is already running (bot.lock held). "
            "Stop it first:  pkill -f bot.py"
        )
    _lock_file.write(str(os.getpid()))
    _lock_file.flush()


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------
db = sqlite3.connect(Path(__file__).with_name("radar.db"))
db.execute(
    """CREATE TABLE IF NOT EXISTS feeds(
  channel_id INTEGER, kind TEXT, guild_id INTEGER, created INTEGER,
  PRIMARY KEY(channel_id, kind))"""
)
db.execute(
    """CREATE TABLE IF NOT EXISTS seen(
  channel_id INTEGER, key TEXT, first_seen INTEGER,
  PRIMARY KEY(channel_id, key))"""
)
db.execute(
    """CREATE TABLE IF NOT EXISTS board(
  channel_id INTEGER PRIMARY KEY, message_id INTEGER, updated INTEGER)"""
)
db.execute(
    """CREATE TABLE IF NOT EXISTS guides(
  channel_id INTEGER PRIMARY KEY, message_id INTEGER, updated INTEGER)"""
)
# Même schéma que `board` pour réutiliser `upsert_pinned` sans le modifier.
db.execute(
    """CREATE TABLE IF NOT EXISTS databoard(
  channel_id INTEGER PRIMARY KEY, message_id INTEGER, updated INTEGER)"""
)
db.execute("""CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)""")

# Enregistreur : base SÉPARÉE de radar.db, pour que la base opérationnelle reste
# petite et rapide, et que le jeu de données se copie indépendamment. Polymarket
# purge l'historique de prix après résolution (mesuré : 0 point sur 500 marchés
# clos), donc l'enregistrer nous-mêmes est le seul moyen d'en disposer.
recorder = H.Recorder(Path(__file__).with_name("history.db"))
# Salons retenus par IDENTIFIANT, pas par nom : un identifiant survit aux
# renommages, un nom non. Sans ça, ajouter un emoji au nom d'un salon fait
# que `/setup` ne le reconnaît plus et en recrée un doublon à côté.
db.execute(
    """CREATE TABLE IF NOT EXISTS channels(
  guild_id INTEGER, key TEXT, channel_id INTEGER,
  PRIMARY KEY(guild_id, key))"""
)
db.commit()


def _norm(name: str) -> str:
    """Nom comparable : emojis, accents et ponctuation retirés."""
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")


async def ensure_channel(guild, cat, key, display, topic, overwrites):
    """Retrouve un salon par ID mémorisé, puis par nom normalisé, sinon le crée.

    Trois niveaux de repli, du plus robuste au plus fragile, pour que l'on
    puisse renommer les salons librement sans que `/setup` fasse des doublons.
    """
    row = db.execute(
        "SELECT channel_id FROM channels WHERE guild_id=? AND key=?", (guild.id, key)
    ).fetchone()
    if row:
        ch = guild.get_channel(row[0])
        if ch is not None:
            return ch, False

    target = _norm(key)
    for ch in cat.text_channels:
        if _norm(ch.name) == target:
            db.execute(
                "INSERT OR REPLACE INTO channels VALUES(?,?,?)", (guild.id, key, ch.id)
            )
            db.commit()
            return ch, False

    ch = await guild.create_text_channel(
        display, category=cat, topic=topic, overwrites=overwrites
    )
    db.execute(
        "INSERT OR REPLACE INTO channels VALUES(?,?,?)", (guild.id, key, ch.id)
    )
    db.commit()
    return ch, True


def meta_get(k, default=None):
    r = db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r[0] if r else default


def meta_set(k, v):
    db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, str(v)))
    db.commit()


# auto_sync_commands=False : sinon py-cord enregistre AUSSI les commandes en
# global, elles cohabitent avec les copies par serveur et Discord les affiche
# en double.
bot = discord.Bot(intents=discord.Intents.default(), auto_sync_commands=False)

_lock = asyncio.Lock()
_cache = {"ts": 0.0, "result": None}
CACHE_TTL = 300


async def get_radar(force: bool = False):
    """Un seul scan à la fois, partagé par la boucle et les commandes."""
    async with _lock:
        now = time.time()
        if not force and _cache["result"] and now - _cache["ts"] < CACHE_TTL:
            return _cache["result"]
        result = await R.scan()
        _cache.update(ts=now, result=result)
        meta_set("last_scan", int(now))
        meta_set("last_alerts", len(result.alerts))
        return result


# Quels signaux vont dans quel flux. Séparer par urgence : une contestation se
# lit tout de suite, un marché coincé depuis 120 jours se lit quand on a le temps.
FEED_KINDS = {
    "live": {"dispute", "proposal_gap", "shock"},
    "stuck": {"overdue"},
}


# ---------------------------------------------------------------------------
# Présentation
# ---------------------------------------------------------------------------

STYLE = {
    "dispute": ("🔴", 0xE74C3C, "Resolution disputed"),
    "proposal_gap": ("🟠", 0xE67E22, "Proposed but not priced in"),
    "shock": ("🔵", 0x3498DB, "Resolution shock"),
    "overdue": ("🟡", 0xF1C40F, "Stuck past its end date"),
    "resolving_soon": ("⚪", 0x95A5A6, "Resolving soon"),
}


def signal_embed(s) -> discord.Embed:
    icon, color, label = STYLE.get(s.kind, ("⚪", 0x95A5A6, s.kind))
    m = s.market

    e = discord.Embed(
        title=f"{icon} {m.title[:230]}",
        url=m.url,
        description=f"**{label}** — {s.headline}\n{s.detail}",
        color=color,
    )
    if m.statuses:
        e.add_field(
            name="UMA status", value="`" + " → ".join(m.statuses) + "`", inline=False
        )
    e.add_field(name="Price (YES)", value=f"{m.yes_price:.3f}", inline=True)
    e.add_field(name="Liquidity", value=R.fmt_usd(m.liquidity), inline=True)
    e.add_field(name="Timing", value=R.fmt_delay(m.days_late), inline=True)
    e.add_field(name="24h volume", value=R.fmt_usd(m.volume24h), inline=True)
    e.add_field(name="Turnover", value=f"{m.turnover:.1f}× book", inline=True)
    e.add_field(name="Spread", value=f"{m.spread*100:.1f}¢", inline=True)
    if s.action:
        e.add_field(name="👉 What this means for you", value=s.action[:1024], inline=False)

    e.set_footer(
        text="Polymarket does not publish which outcome was proposed — "
        "check the market itself before acting."
    )
    return e


CALIB_TARGET = 100   # marchés résolus nécessaires pour une première étude


def dataset_embed() -> discord.Embed:
    """Tableau vivant du jeu de données.

    Le compteur seul serait décourageant : « 0 résolu » pendant des semaines.
    On affiche donc la PROGRESSION vers le seuil d'exploitabilité et la date
    estimée — ce qui transforme une attente opaque en compte à rebours lisible.
    """
    st = recorder.stats()
    done = st["resolved"]
    pct = min(done / CALIB_TARGET, 1.0)
    filled = int(pct * 20)
    bar = "█" * filled + "░" * (20 - filled)
    ready = done >= CALIB_TARGET

    e = discord.Embed(
        title="🗄️ Price-history dataset — live",
        description=(
            "Polymarket **deletes price history once a market resolves** — "
            "verified on 500 resolved markets, zero points returned. No strategy "
            "here can be backtested from public data.\n"
            "This radar keeps what it already reads every cycle and pairs it with "
            "the real outcome. It cannot be bought or copied, only accumulated."
        ),
        color=0x1ABC9C if ready else 0x34495E,
    )

    eta = recorder.eta_days(CALIB_TARGET)
    if ready:
        line = f"`{bar}` **{done}/{CALIB_TARGET}**\n**Ready** — the first calibration study can run."
    else:
        rate = recorder.resolution_rate()
        when = (
            f"about **{eta:.0f} days** to go" if eta is not None and eta < 400
            else "pace not measurable yet — needs a full day of recording"
        )
        line = (
            f"`{bar}` **{done}/{CALIB_TARGET}**\n"
            + (f"{rate:.1f} markets settling per day · {when}" if rate > 0 else when)
        )
    e.add_field(name="Progress to a usable dataset", value=line, inline=False)

    e.add_field(name="Markets tracked", value=f"{st['markets']:,}", inline=True)
    e.add_field(name="Price points", value=f"{st['ticks']:,}", inline=True)
    e.add_field(name="Depth", value=f"{st['days']:.1f} days", inline=True)

    size = f"{st['mb']:.1f} MB"
    if st["mb_per_day"]:
        size += f" (+{st['mb_per_day']:.1f}/day)"
    e.add_field(name="Storage", value=size, inline=True)
    e.add_field(name="Resolved", value=f"{done:,}", inline=True)
    e.add_field(name="Written", value="changes only", inline=True)

    e.add_field(
        name="What it will answer",
        value=(
            "Does a contract priced at 5% actually happen 5% of the time? If not, "
            "selling long shots is a measurable edge. Nobody can answer that on "
            "Polymarket today — the data to check it does not exist publicly."
        ),
        inline=False,
    )
    e.set_footer(text=f"Rewritten every {POLL_MINUTES} min · recording since it was switched on")
    e.timestamp = discord.utils.utcnow()
    return e


def board_embed(res) -> discord.Embed:
    disputes = res.by_kind("dispute")
    e = discord.Embed(
        title="🛰️ Resolution radar — live",
        description=(
            f"**{len(res.alerts)} live situations** across {res.markets_seen:,} "
            f"open markets, scanned in {res.duration:.1f}s\n"
            f"**{R.fmt_usd(res.locked_capital)}** of liquidity is sitting in "
            f"**{res.overdue_total}** markets that are past their end date."
        ),
        color=0xE74C3C if disputes else 0x34495E,
    )

    if disputes:
        lines = []
        for s in disputes[:5]:
            m = s.market
            rounds = f" ×{m.dispute_rounds}" if m.dispute_rounds > 1 else ""
            lines.append(
                f"🔴 `{R.fmt_usd(m.liquidity):>7}{rounds}` [{m.title[:52]}]({m.url})"
            )
        e.add_field(name="Disputed resolutions", value="\n".join(lines)[:1024], inline=False)

    stuck = sorted(res.by_kind("overdue"), key=lambda s: -s.market.liquidity)[:5]
    if stuck:
        lines = [
            f"🟡 `{R.fmt_usd(s.market.liquidity):>7}` `{s.market.days_late:>4.0f}d` "
            f"{s.market.title[:46]}"
            for s in stuck
        ]
        e.add_field(name="Most capital stuck", value="\n".join(lines)[:1024], inline=False)

    shocks = res.by_kind("shock")[:4]
    if shocks:
        lines = [
            f"🔵 `{s.market.day_change*100:+4.0f}pts` `{s.market.turnover:>4.1f}×` "
            f"{s.market.title[:46]}"
            for s in shocks
        ]
        e.add_field(name="Price shocks", value="\n".join(lines)[:1024], inline=False)

    if not disputes and not shocks:
        e.add_field(
            name="No live resolution drama",
            value=(
                "Disputes are rare — about **0.4%** of resolutions. Long quiet "
                "stretches here are the normal state, not a broken scanner."
            ),
            inline=False,
        )

    e.set_footer(text=f"Rewritten every {POLL_MINUTES} min")
    e.timestamp = discord.utils.utcnow()
    return e


GUIDE = (
    "**What this channel is**\n"
    "A prediction market does not die cleanly. Between the event happening and "
    "the money being paid, someone proposes an outcome to the UMA oracle, a "
    "challenge window opens, and occasionally someone disputes it. Meanwhile the "
    "market keeps trading and your capital stays locked. This bot watches that "
    "phase — the part almost nobody looks at.\n\n"
    "**What gets flagged**\n"
    "🔴 **Dispute** — a proposed resolution was challenged. Only about **0.4%** "
    "of resolutions get disputed, so this is the signal that matters most. It "
    "goes to a UMA vote: expect days of delay, and the outcome can still flip.\n"
    "🟠 **Proposal gap** — a resolution is on the table but the price has not "
    "moved to certainty. Either nobody noticed, or the market disagrees.\n"
    "🔵 **Shock** — a big price move backed by real turnover, far from the end "
    "date. A market trading several times its own book in a day had news.\n"
    "🟡 **Stuck** — past its end date with no resolution proposed at all. Cases "
    "over 200 days late exist. Your capital is simply parked.\n\n"
    "**Every alert says what it means for you**\n"
    "Each one ends with a « What this means for you » block: whether there is "
    "anything to do, what to check first, and when the honest answer is « stay "
    "away ». Only **proposal gaps** are directly tradable — and only if you can "
    "verify the outcome yourself against the market's stated source.\n\n"
    "**What the bot cannot tell you**\n"
    "Polymarket publishes the resolution *status* but never **which outcome was "
    "proposed**. So the bot describes the situation, never a direction. Anyone "
    "claiming otherwise is inventing data that does not exist.\n\n"
    "**Why it is quiet**\n"
    "Disputes are rare by design. Silence here means resolutions are running "
    "normally — which is exactly what you want when you hold positions."
)


def build_guide_embed() -> discord.Embed:
    return discord.Embed(
        title="📖 How to read this channel", description=GUIDE, color=0x34495E
    )


async def upsert_pinned(channel, table: str, embed: discord.Embed):
    """Réécrit le message épinglé, ou le crée. Sans ça, relancer `/board` empile
    les copies et le salon finit avec trois tableaux dont deux périmés."""
    row = db.execute(
        f"SELECT message_id FROM {table} WHERE channel_id=?", (channel.id,)
    ).fetchone()
    if row:
        try:
            msg = await channel.fetch_message(row[0])
            await msg.edit(embed=embed)
            db.execute(
                f"UPDATE {table} SET updated=? WHERE channel_id=?",
                (int(time.time()), channel.id),
            )
            db.commit()
            return msg
        except discord.NotFound:
            pass

    msg = await channel.send(embed=embed)
    try:
        await msg.pin()
    except discord.DiscordException:
        pass  # pas la permission d'épingler : le message reste utile
    db.execute(
        f"INSERT OR REPLACE INTO {table} VALUES(?,?,?)",
        (channel.id, msg.id, int(time.time())),
    )
    db.commit()
    return msg


# ---------------------------------------------------------------------------
# Suivi des alertes déjà vues
# ---------------------------------------------------------------------------


def snapshot(channel_id: int, signals) -> int:
    """Marque l'état courant comme « déjà connu », sans rien annoncer.

    Indispensable au premier abonnement : il existe en permanence ~35 situations
    ouvertes (dont 24 marchés échus de longue date). Sans photo de référence, le
    salon recevrait 35 alertes d'un coup à l'installation, dont aucune ne serait
    une nouvelle. Piège déjà rencontré sur le bot overlap.
    """
    now = int(time.time())
    rows = [(channel_id, s.key, now) for s in signals]
    db.executemany("INSERT OR IGNORE INTO seen VALUES(?,?,?)", rows)
    db.commit()
    return len(rows)


def is_new(channel_id: int, key: str) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM seen WHERE channel_id=? AND key=?", (channel_id, key)
        ).fetchone()
        is None
    )


def mark_seen(channel_id: int, key: str):
    db.execute(
        "INSERT OR IGNORE INTO seen VALUES(?,?,?)", (channel_id, key, int(time.time()))
    )
    db.commit()


# ---------------------------------------------------------------------------
# Boucle
# ---------------------------------------------------------------------------


@tasks.loop(minutes=POLL_MINUTES)
async def poll():
    feeds = db.execute("SELECT channel_id, kind FROM feeds").fetchall()
    boards = db.execute("SELECT channel_id FROM board").fetchall()
    # Le scan tourne même sans abonné : l'enregistrement du jeu de données ne
    # dépend pas de Discord, et chaque cycle manqué est une donnée perdue pour
    # toujours — l'historique ne se rattrape pas après coup.
    try:
        res = await get_radar(force=True)
    except Exception as e:  # noqa: BLE001
        print(f"[poll] scan failed: {type(e).__name__}: {e}", flush=True)
        return

    # Isolé dans son propre try : une panne de l'enregistreur ne doit jamais
    # empêcher les alertes de partir. Le bot rend un service aujourd'hui, le jeu
    # de données n'en rendra un que dans plusieurs mois.
    try:
        st = recorder.record(res.markets)
        pending = recorder.pending_settlement()
        settled = 0
        if pending:
            settled = recorder.settle(await R.fetch_outcomes(pending))
        if st.inserted or settled:
            print(f"[data] {st} · {settled} résolution(s) inscrite(s)", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[data] recorder failed: {type(e).__name__}: {e}", flush=True)

    if not feeds and not boards:
        return

    for (channel_id,) in db.execute("SELECT channel_id FROM databoard").fetchall():
        ch = bot.get_channel(channel_id)
        if ch is None:
            continue
        try:
            await upsert_pinned(ch, "databoard", dataset_embed())
        except discord.DiscordException as e:
            print(f"[poll] databoard failed on {channel_id}: {e}", flush=True)

    for (channel_id,) in boards:
        ch = bot.get_channel(channel_id)
        if ch is None:
            continue
        try:
            await upsert_pinned(ch, "board", board_embed(res))
        except discord.DiscordException as e:
            print(f"[poll] board failed on {channel_id}: {e}", flush=True)

    for channel_id, kind in feeds:
        ch = bot.get_channel(channel_id)
        if ch is None:
            continue
        wanted = FEED_KINDS.get(kind, set())
        sent = 0
        for s in res.alerts:
            if s.kind not in wanted or not is_new(channel_id, s.key):
                continue
            try:
                await ch.send(embed=signal_embed(s))
            except discord.DiscordException as e:
                print(f"[poll] send failed on {channel_id}: {e}", flush=True)
                break
            mark_seen(channel_id, s.key)
            sent += 1
            if sent >= MAX_ALERTS_PER_CYCLE:
                # Le reste sera repris au cycle suivant : rien n'est perdu,
                # les clés non marquées restent « nouvelles ».
                break

    db.execute(
        "DELETE FROM seen WHERE first_seen < ?",
        (int(time.time()) - SEEN_TTL_DAYS * 86400,),
    )
    db.commit()
    print(
        f"[poll] {res.markets_seen} markets · {len(res.alerts)} alertable · "
        f"{res.counts}",
        flush=True,
    )


@poll.before_loop
async def before_poll():
    await bot.wait_until_ready()


# ---------------------------------------------------------------------------
# Commandes
# ---------------------------------------------------------------------------


async def _subscribe(ctx, kind: str, label: str):
    await ctx.defer(ephemeral=True)
    res = await get_radar()
    db.execute(
        "INSERT OR REPLACE INTO feeds VALUES(?,?,?,?)",
        (ctx.channel.id, kind, ctx.guild.id if ctx.guild else 0, int(time.time())),
    )
    db.commit()
    n = snapshot(ctx.channel.id, res.signals)
    live = len([s for s in res.alerts if s.kind in FEED_KINDS[kind]])
    await ctx.respond(
        f"✅ Watching **{label}** in this channel.\n"
        f"Baseline taken on {n} current situations ({live} in this feed) — you "
        f"will only be told about **changes from now on**, not the backlog.\n"
        f"Checked every {POLL_MINUTES} min · `/guide` posts the how-to-read note.",
        ephemeral=True,
    )


@bot.slash_command(
    name="watch-live",
    description="Send disputes, proposal gaps and price shocks to this channel",
    guild_ids=GUILDS,
)
async def watch_live(ctx):
    await _subscribe(ctx, "live", "disputes, proposal gaps and shocks")


@bot.slash_command(
    name="watch-stuck",
    description="Send stuck / overdue market alerts to this channel",
    guild_ids=GUILDS,
)
async def watch_stuck(ctx):
    await _subscribe(ctx, "stuck", "markets stuck past their end date")


@bot.slash_command(
    name="unwatch", description="Stop all alerts in this channel", guild_ids=GUILDS
)
async def unwatch(ctx):
    await ctx.defer(ephemeral=True)
    db.execute("DELETE FROM feeds WHERE channel_id=?", (ctx.channel.id,))
    db.commit()
    await ctx.respond("🔕 Stopped watching this channel.", ephemeral=True)


@bot.slash_command(
    name="radar", description="Show the current resolution situations", guild_ids=GUILDS
)
async def radar(ctx, limit: int = 5):
    await ctx.defer()
    res = await get_radar()
    top = res.alerts[: max(1, min(limit, 8))]
    if not top:
        return await ctx.respond(
            embed=discord.Embed(
                title="Nothing live",
                description=(
                    f"Scanned {res.markets_seen:,} open markets in "
                    f"{res.duration:.1f}s. No disputes, no shocks, nothing stuck "
                    f"above the alert threshold."
                ),
                color=0x95A5A6,
            )
        )
    await ctx.respond(
        f"**{len(res.alerts)}** live situations across {res.markets_seen:,} markets "
        f"— showing top {len(top)} by severity:",
        embeds=[signal_embed(s) for s in top],
    )


@bot.slash_command(
    name="stuck", description="Markets past their end date, most capital first",
    guild_ids=GUILDS,
)
async def stuck(ctx, limit: int = 10):
    await ctx.defer()
    res = await get_radar()
    rows = sorted(res.by_kind("overdue"), key=lambda s: -s.market.liquidity)
    rows = rows[: max(1, min(limit, 20))]
    if not rows:
        return await ctx.respond("Nothing is stuck right now.")

    e = discord.Embed(
        title="🟡 Markets stuck past their end date",
        description=(
            f"**{R.fmt_usd(res.locked_capital)}** of liquidity across "
            f"**{res.overdue_total}** markets with no resolution proposed."
        ),
        color=0xF1C40F,
    )
    e.add_field(
        name="Most capital stuck",
        value="\n".join(
            f"`{R.fmt_usd(s.market.liquidity):>7}` `{s.market.days_late:>4.0f}d` "
            f"[{s.market.title[:46]}]({s.market.url})"
            for s in rows
        )[:1024],
        inline=False,
    )
    await ctx.respond(embed=e)


@bot.slash_command(name="status", description="Radar settings and last run", guild_ids=GUILDS)
async def status(ctx):
    await ctx.defer(ephemeral=True)
    last = int(meta_get("last_scan", 0) or 0)
    subs = db.execute(
        "SELECT kind FROM feeds WHERE channel_id=?", (ctx.channel.id,)
    ).fetchall()

    e = discord.Embed(title="Resolution radar", color=0x34495E)
    e.add_field(
        name="This channel",
        value=(
            ", ".join(k for (k,) in subs) if subs else "not watching · `/watch-live`"
        ),
        inline=False,
    )
    e.add_field(
        name="Last scan",
        value=f"<t:{last}:R> · {meta_get('last_alerts', 0)} alertable" if last else "never",
        inline=True,
    )
    e.add_field(name="Interval", value=f"{POLL_MINUTES} min", inline=True)
    e.add_field(
        name="Detection thresholds",
        value=(
            f"overdue ≥ {R.OVERDUE_DAYS:g}d, alert above {R.fmt_usd(R.OVERDUE_ALERT_LIQUIDITY)}\n"
            f"shock ≥ {R.SHOCK_MIN*100:g}pts with ≥ {R.SHOCK_MIN_TURNOVER:g}× turnover\n"
            f"proposal gap ≥ {R.PROPOSAL_GAP_MIN*100:g}% above {R.fmt_usd(R.PROPOSAL_GAP_MIN_LIQUIDITY)}\n"
            f"disputes always alert, whatever the size"
        ),
        inline=False,
    )
    await ctx.respond(embed=e, ephemeral=True)


@bot.slash_command(
    name="board", description="Install the live radar board in this channel",
    guild_ids=GUILDS,
)
async def board(ctx):
    await ctx.defer(ephemeral=True)
    res = await get_radar()
    await upsert_pinned(ctx.channel, "board", board_embed(res))
    await ctx.respond(
        f"🛰️ Board installed and pinned, rewritten every {POLL_MINUTES} min.",
        ephemeral=True,
    )


@bot.slash_command(
    name="guide", description="Post the how-to-read note here (pin it)", guild_ids=GUILDS
)
async def guide(ctx):
    await ctx.defer(ephemeral=True)
    await upsert_pinned(ctx.channel, "guides", build_guide_embed())
    await ctx.respond(
        "📖 Guide posted and pinned. Running `/guide` again updates that same "
        "message instead of adding another one.",
        ephemeral=True,
    )


@bot.slash_command(
    name="preview", description="Post a sample alert to check formatting", guild_ids=GUILDS
)
async def preview(ctx):
    await ctx.defer(ephemeral=True)
    import datetime

    m = R.Market(
        id="0", question="Will Venezuela recognize Israel by December 31?  (SAMPLE)",
        slug="", event_slug="", label="", end=datetime.datetime.now(datetime.timezone.utc),
        days_late=-139.0, statuses=["proposed", "disputed", "proposed", "disputed"],
        yes_price=0.14, best_bid=0.13, best_ask=0.15, spread=0.02,
        liquidity=32_000.0, volume24h=162_000.0, day_change=-0.64, hour_change=0.015,
        resolution_source="", bond=500.0, accepting_orders=True,
    )
    sig = R.classify(m)[0]
    e = signal_embed(sig)
    e.title = "🧪 EXAMPLE ALERT — not a live situation"
    e.url = None
    e.color = 0x95A5A6
    e.set_footer(text="Sample posted by /preview. Numbers are from a past event.")

    try:
        await ctx.channel.send(embed=e)
    except discord.Forbidden:
        return await ctx.respond(
            "I can't post here. Give my role **Send Messages** and **Embed Links**, "
            "then run `/preview` again.",
            ephemeral=True,
        )
    await ctx.respond("🧪 Sample posted — this is how a real alert looks.", ephemeral=True)


@bot.slash_command(
    name="dataset",
    description="Growth of the price-history dataset being recorded",
    guild_ids=GUILDS,
)
async def dataset_cmd(ctx):
    await ctx.defer(ephemeral=True)
    st = recorder.stats()

    e = discord.Embed(
        title="🗄️ Price-history dataset",
        description=(
            "Polymarket **deletes price history once a market resolves** — verified "
            "on 500 resolved markets, zero data points returned. So no strategy on "
            "this platform can be backtested from public data.\n"
            "This radar keeps what it already reads every cycle, and pairs it with "
            "the real outcome. The data cannot be bought or copied, only accumulated."
        ),
        color=0x1ABC9C if st["resolved"] else 0x34495E,
    )
    e.add_field(name="Markets tracked", value=f"{st['markets']:,}", inline=True)
    e.add_field(name="Price points", value=f"{st['ticks']:,}", inline=True)
    e.add_field(name="Depth", value=f"{st['days']:.1f} days", inline=True)
    e.add_field(
        name="✅ Resolved — the usable part",
        value=(
            f"**{st['resolved']:,}** markets with a known outcome.\n"
            + ("A calibration study needs about 100. "
               f"{'Not there yet — this grows on its own as markets settle.' if st['resolved'] < 100 else 'Enough to run the first study.'}")
        ),
        inline=False,
    )
    size = f"{st['mb']:.1f} MB"
    if st["mb_per_day"]:
        size += f" · {st['mb_per_day']:.1f} MB/day"
    e.add_field(name="Storage", value=size, inline=True)
    e.set_footer(text="Recording every cycle · only changes are written")
    await ctx.respond(embed=e, ephemeral=True)


@bot.slash_command(
    name="dataset-board",
    description="Install the live dataset board in this channel",
    guild_ids=GUILDS,
)
async def dataset_board_cmd(ctx):
    await ctx.defer(ephemeral=True)
    await upsert_pinned(ctx.channel, "databoard", dataset_embed())
    await ctx.respond(
        f"🗄️ Dataset board installed and pinned. It is **rewritten in place "
        f"every {POLL_MINUTES} min**, so this channel always shows the current "
        "state.\nExpect it to look idle at first — the counter that matters "
        "(**resolved markets**) only moves as markets settle.",
        ephemeral=True,
    )


@bot.slash_command(
    name="setup", description="Create the full channel structure and wire everything up",
    guild_ids=GUILDS,
)
@discord.default_permissions(manage_guild=True)
async def setup(ctx):
    await ctx.defer(ephemeral=True)
    g = ctx.guild
    if g is None:
        return await ctx.respond("Run this in a server, not in a DM.", ephemeral=True)
    if not g.me.guild_permissions.manage_channels:
        return await ctx.respond(
            "I need the **Manage Channels** permission.\n"
            f"https://discord.com/oauth2/authorize?client_id={bot.user.id}"
            f"&permissions={INVITE_PERMS}&scope=bot%20applications.commands",
            ephemeral=True,
        )

    # py-cord REFUSE overwrites=None et exige un dict : passer {} pour un salon
    # ouvert, sinon la commande entière échoue sur le premier salon non verrouillé.
    read_only = {
        g.default_role: discord.PermissionOverwrite(send_messages=False, add_reactions=True),
        g.me: discord.PermissionOverwrite(send_messages=True, manage_messages=True),
    }

    # (clé logique, nom affiché à la création, sujet, lecture seule)
    # La clé ne change jamais : c'est elle qui identifie le salon en base.
    # Le nom affiché, lui, est libre — tu peux le renommer sans rien casser.
    plan = [
        ("radar-guide", "📖radar-guide", "Read this first — what the resolution phase is", True),
        ("radar-board", "🛰️radar-board", "Live state of resolutions, rewritten automatically", True),
        ("dispute-alerts", "🔴dispute-alerts", "Disputes, proposal gaps and resolution shocks", True),
        ("stuck-markets", "🟡stuck-markets", "Markets past their end date with capital locked", True),
        ("radar-discussion", "💬radar-discussion", "Talk about it here — open to everyone", False),
    ]

    cat = discord.utils.get(g.categories, name="POLYMARKET RESOLUTION")
    if cat is None:
        cat = await g.create_category("POLYMARKET RESOLUTION")

    made, reused, chans = [], [], {}
    for key, display, topic, locked in plan:
        # La recherche reste confinée à NOTRE catégorie : balayer tout le serveur
        # retrouverait les salons des deux autres bots Polymarket.
        try:
            ch, created = await ensure_channel(
                g, cat, key, display, topic, read_only if locked else {}
            )
        except discord.DiscordException as e:
            return await ctx.respond(
                f"Failed while creating **{display}**: {e}\n"
                "Fix the permission and run `/setup` again — existing channels "
                "are reused, not duplicated.",
                ephemeral=True,
            )
        (made if created else reused).append(ch)
        chans[key] = ch

    await upsert_pinned(chans["radar-guide"], "guides", build_guide_embed())

    res = await get_radar()
    await upsert_pinned(chans["radar-board"], "board", board_embed(res))

    now = int(time.time())
    for name, kind in (("dispute-alerts", "live"), ("stuck-markets", "stuck")):
        db.execute(
            "INSERT OR REPLACE INTO feeds VALUES(?,?,?,?)",
            (chans[name].id, kind, g.id, now),
        )
        snapshot(chans[name].id, res.signals)
    db.commit()

    lines = [
        "**Setup complete.**",
        f"📖 {chans['radar-guide'].mention} — guide posted and pinned",
        f"🛰️ {chans['radar-board'].mention} — live board, rewritten every {POLL_MINUTES} min",
        f"🔴 {chans['dispute-alerts'].mention} — disputes, proposal gaps, shocks",
        f"🟡 {chans['stuck-markets'].mention} — markets stuck past their end date",
        f"💬 {chans['radar-discussion'].mention} — open to everyone",
    ]
    if made:
        lines.append(f"\nCreated: {', '.join(c.mention for c in made)}")
    if reused:
        lines.append(f"Reused: {', '.join(c.mention for c in reused)}")
    lines.append(
        f"\nA baseline was taken on the **{len(res.signals)}** situations that "
        f"already exist, so the alert channels start quiet — you will only be "
        f"told about changes from now on.\n"
        f"Disputes are about **0.4%** of resolutions: expect long silences."
    )
    await ctx.respond("\n".join(lines), ephemeral=True)


# ---------------------------------------------------------------------------


@bot.event
async def on_ready():
    print(f"Connected as {bot.user}", flush=True)
    try:
        # Purger les globales AVANT le sync par serveur, sinon les deux jeux
        # cohabitent et Discord affiche chaque commande en double.
        await bot.http.bulk_upsert_global_commands(bot.application_id, [])
        await bot.sync_commands(guild_ids=[g.id for g in bot.guilds], force=True)
        print(f"Commands synced on {len(bot.guilds)} guild(s)", flush=True)
    except discord.DiscordException as e:
        print(f"Command sync failed: {e}", flush=True)

    if not poll.is_running():
        poll.start()


@bot.event
async def on_guild_join(guild):
    """Synchroniser à l'arrivée sur un serveur, et pas seulement au démarrage.

    `on_ready` ne voit que les serveurs déjà rejoints. Un bot invité APRÈS son
    lancement se retrouve donc sans aucune commande dans le menu, sans erreur ni
    trace : il faut le redémarrer à la main pour que les commandes apparaissent.
    """
    try:
        await bot.sync_commands(guild_ids=[guild.id], force=True)
        print(f"Commands synced on join: {guild.name} ({guild.id})", flush=True)
    except discord.DiscordException as e:
        print(f"Sync on join failed for {guild.id}: {e}", flush=True)


def main():
    if not TOKEN:
        sys.exit(
            "No Discord token. Put it in token.txt next to this script, "
            "or set DISCORD_BOT_TOKEN."
        )
    acquire_single_instance_lock()
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
