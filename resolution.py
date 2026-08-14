"""
Moteur Resolution Radar — surveille la fin de vie des marchés Polymarket.

Un marché de prédiction ne meurt pas proprement. Entre le moment où l'événement
a lieu et celui où l'argent est versé, il se passe une phase que presque personne
ne regarde : quelqu'un propose un résultat à l'oracle UMA, une fenêtre de
contestation s'ouvre, et parfois quelqu'un conteste. Pendant tout ce temps le
marché continue de se traiter, et le capital reste bloqué.

Ce module détecte cinq situations, mesurées sur données réelles :

  1. DISPUTE          Une résolution a été contestée (caution engagée). 0,4 % des
                      résolutions — rare, donc informatif. Le litige part au vote
                      UMA : des jours de délai et une issue qui peut basculer.

  2. PROPOSAL GAP     Une résolution est proposée mais le prix n'a pas convergé
                      vers la certitude. Soit le marché n'a pas vu, soit il n'est
                      pas d'accord — les deux méritent d'être sus.

  3. OVERDUE          Marché échu depuis longtemps, toujours ouvert, sans même une
                      proposition. Du capital immobilisé sans horizon (des cas à
                      287 jours de retard existent).

  4. SHOCK            Effondrement ou envolée brutale du prix, signe qu'une
                      information de résolution vient de tomber.

  5. RESOLVING SOON   Échéance imminente sur un marché liquide : de quoi préparer
                      sa sortie avant la phase illiquide.

HONNÊTETÉ SUR LES DONNÉES : l'API Polymarket expose le STATUT de la résolution
(`umaResolutionStatuses`) mais JAMAIS le résultat proposé. Le moteur ne peut donc
pas dire « la proposition dit NON » — il décrit la situation, pas une direction.
Prétendre le contraire serait inventer une information qui n'existe pas.

Testable seul, sans Discord :  python3 resolution.py
"""

from __future__ import annotations

import asyncio
import datetime
import json
from dataclasses import dataclass, field

import aiohttp

GAMMA = "https://gamma-api.polymarket.com"
UA = {"User-Agent": "polymarket-resolution-radar/1.0"}

# ---------------------------------------------------------------------------
# Réglages
# ---------------------------------------------------------------------------

# L'API refuse les offsets au-delà d'environ 2300 par tri (422). On fait donc
# deux passes de tris différents et on fusionne : par échéance croissante pour
# attraper les marchés échus, par volume pour attraper les gros marchés dont la
# résolution peut être contestée alors que l'échéance est encore lointaine —
# c'est exactement le cas du marché Venezuela, échu en décembre et déjà contesté.
# `limit` est PLAFONNÉ À 100 par l'API, quelle que soit la valeur demandée.
# Demander 500 et s'arrêter quand la page en renvoie moins faisait quitter la
# pagination dès la première page : 100 marchés au lieu de 2000.
PAGE = 100
MAX_OFFSET = 2200
SWEEP_CONCURRENCY = 6

# En dessous, un marché « en retard » n'immobilise rien qui vaille une alerte.
MIN_LIQUIDITY = 1_000.0

# Un marché échu depuis moins que ça est simplement en cours de résolution
# normale : la fenêtre UMA dure des heures, ce n'est pas une anomalie.
OVERDUE_DAYS = 3.0
# 191 marchés échus existent en permanence : les pousser tous en alerte noierait
# le salon. Au-dessus de ce seuil le capital bloqué justifie une alerte
# individuelle ; en dessous, le marché ne compte que dans l'agrégat du tableau.
OVERDUE_ALERT_LIQUIDITY = 25_000.0

# Écart à la certitude au-delà duquel une proposition en cours devient notable.
# Sous 1 %, c'est le tick du carnet, pas un désaccord.
PROPOSAL_GAP_MIN = 0.02
# Ce signal ne vaut que si l'on peut ENCORE trader contre la résolution connue :
# un marché mort à 1 k$ et 0 $ de volume n'offre rien, quel que soit son écart.
PROPOSAL_GAP_MIN_LIQUIDITY = 10_000.0

# Variation de prix sur 24 h à partir de laquelle on parle de choc.
SHOCK_MIN = 0.25
# Un prix qui bondit à 1.00 le jour de l'échéance n'est pas une anomalie : c'est
# l'événement qui a lieu. Sans ce garde-fou, chaque match esport terminé produit
# une alerte et le signal utile se noie (26 chocs par scan, tous du bruit).
# Un choc n'est informatif que LOIN de l'échéance, ou sur un marché en litige.
SHOCK_MIN_DAYS_BEFORE_END = 2.0
# Un prix qui bouge sans volume est un artefact de cotation. Le discriminant le
# plus net trouvé sur données réelles est le TAUX DE ROTATION (volume 24 h ÷
# liquidité) : le marché Venezuela, seul vrai choc informatif du lot, tourne à
# 5,1× son carnet, quand les matchs de baseball dont les cotes dérivent sont
# tous sous 0,6×. Un marché qui échange plusieurs fois sa profondeur en un jour
# a reçu une information ; un marché dont le mid glisse, non.
SHOCK_MIN_TURNOVER = 1.0
SHOCK_MIN_VOLUME = 10_000.0

# Fenêtre « résolution imminente » et liquidité minimale pour la signaler.
SOON_HOURS = 24.0
SOON_MIN_LIQUIDITY = 100_000.0


# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------


def fmt_usd(v: float) -> str:
    v = v or 0
    if abs(v) >= 1e6:
        return f"${v/1e6:.2f}M"
    if abs(v) >= 1e3:
        return f"${v/1e3:.1f}K"
    return f"${v:.0f}"


def fmt_delay(days: float) -> str:
    if days < 0:
        hours = -days * 24
        return f"in {hours:.0f}h" if hours < 48 else f"in {-days:.0f}d"
    if days < 1:
        return f"{days * 24:.0f}h late"
    return f"{days:.0f}d late"


def _jloads(value, default):
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def _num(value, default=0.0) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return f if f == f else default  # écarte les NaN


def _parse_dt(value) -> datetime.datetime | None:
    if not value or str(value) in ("None", "null"):
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)


# ---------------------------------------------------------------------------
# Modèle
# ---------------------------------------------------------------------------


@dataclass
class Market:
    id: str
    question: str
    slug: str
    event_slug: str
    label: str
    end: datetime.datetime | None
    days_late: float  # positif = échu, négatif = à venir
    statuses: list[str]
    yes_price: float
    best_bid: float
    best_ask: float
    spread: float
    liquidity: float
    volume24h: float
    day_change: float
    hour_change: float
    resolution_source: str
    bond: float
    accepting_orders: bool

    @property
    def winner_price(self) -> float:
        """Prix du camp favori : c'est lui qui doit tendre vers 1 à la résolution."""
        return max(self.yes_price, 1.0 - self.yes_price)

    @property
    def gap(self) -> float:
        """Distance du favori à la certitude."""
        return 1.0 - self.winner_price

    @property
    def turnover(self) -> float:
        """Volume 24 h rapporté à la profondeur du carnet.

        Au-dessus de 1, le marché a échangé plus que son propre carnet en une
        journée : c'est la signature d'une information, pas d'une dérive de cote.
        """
        return self.volume24h / self.liquidity if self.liquidity > 0 else 0.0

    @property
    def dispute_rounds(self) -> int:
        return sum(1 for s in self.statuses if s == "disputed")

    @property
    def proposed(self) -> bool:
        return bool(self.statuses)

    @property
    def disputed(self) -> bool:
        return "disputed" in self.statuses

    @property
    def url(self) -> str:
        if self.event_slug:
            return f"https://polymarket.com/event/{self.event_slug}"
        return f"https://polymarket.com/market/{self.slug}"

    @property
    def title(self) -> str:
        """Le libellé de groupe porte souvent l'info utile (« Venezuela ») que la
        question seule répète pour chaque issue d'un même événement."""
        if self.label and self.label.lower() not in self.question.lower():
            return f"{self.question} — {self.label}"
        return self.question


@dataclass
class Signal:
    kind: str
    severity: float
    market: Market
    headline: str
    detail: str
    # Ce que la situation implique CONCRÈTEMENT pour quelqu'un qui détient ou
    # regarde ce marché. Décrire sans dire « et donc ? » laisse le lecteur
    # deviner, et c'est là qu'il se trompe.
    action: str = ""
    # "alert" = poussé dans le salon ; "digest" = visible seulement dans le
    # tableau et /radar. Un marché échu de plus n'est pas une nouvelle, mais
    # 3,3 M$ immobilisés au total en est une.
    tier: str = "alert"

    @property
    def key(self) -> str:
        """Identité d'une alerte.

        Le nombre de statuts UMA en fait partie à dessein : une DEUXIÈME
        contestation sur le même marché est une information neuve et doit
        re-déclencher, alors que la même contestation vue au cycle suivant ne
        doit pas.
        """
        return f"{self.kind}:{self.market.id}:{len(self.market.statuses)}"


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def parse_market(raw: dict, now: datetime.datetime) -> Market | None:
    prices = _jloads(raw.get("outcomePrices"), [])
    if len(prices) != 2:
        return None
    yes = _num(prices[0], -1)
    if not 0.0 <= yes <= 1.0:
        return None

    end = _parse_dt(raw.get("endDate")) or _parse_dt(raw.get("endDateIso"))
    days_late = (now - end).total_seconds() / 86400 if end else 0.0

    events = raw.get("events") or []
    event_slug = events[0].get("slug", "") if events else ""

    return Market(
        id=str(raw.get("id")),
        question=raw.get("question") or "",
        slug=raw.get("slug") or "",
        event_slug=event_slug,
        label=(raw.get("groupItemTitle") or "").strip(),
        end=end,
        days_late=days_late,
        statuses=[str(s) for s in _jloads(raw.get("umaResolutionStatuses"), [])],
        yes_price=yes,
        best_bid=_num(raw.get("bestBid")),
        best_ask=_num(raw.get("bestAsk"), 1.0),
        spread=_num(raw.get("spread")),
        liquidity=_num(raw.get("liquidityNum")),
        volume24h=_num(raw.get("volume24hr")),
        day_change=_num(raw.get("oneDayPriceChange")),
        hour_change=_num(raw.get("oneHourPriceChange")),
        resolution_source=(raw.get("resolutionSource") or "").strip(),
        bond=_num(raw.get("umaBond")),
        accepting_orders=bool(raw.get("acceptingOrders")),
    )


# ---------------------------------------------------------------------------
# Détection
# ---------------------------------------------------------------------------


def _liquidity_weight(liq: float) -> float:
    """Pondération douce par la liquidité, plafonnée.

    Sans plafond, un marché à 500 k$ écraserait tout le classement quelle que
    soit la gravité réelle ; sans pondération, une anomalie sur un marché à 200 $
    remonterait au même rang qu'un litige à 300 k$.
    """
    return min(liq / 50_000.0, 1.0)


def classify(m: Market) -> list[Signal]:
    signals: list[Signal] = []

    # --- 1. Contestation : le signal le plus rare et le plus fort -----------
    if m.disputed:
        rounds = m.dispute_rounds
        sev = 80 + 8 * (rounds - 1) + 12 * _liquidity_weight(m.liquidity)
        signals.append(
            Signal(
                kind="dispute",
                severity=sev,
                market=m,
                headline=(
                    f"Resolution disputed ({rounds}×)"
                    if rounds > 1
                    else "Resolution disputed"
                ),
                detail=(
                    f"UMA status chain: {' → '.join(m.statuses)}. "
                    f"A disputed resolution goes to a UMA vote — expect days of "
                    f"delay, and the outcome can still flip. Capital in this "
                    f"market is locked until it settles."
                ),
                action=(
                    "**If you hold this market:** your money is locked until the "
                    "vote ends, and the side that pays out can still change. Do "
                    "not count on this settling soon.\n"
                    "**If you don't:** this is uncertainty, not an opportunity. "
                    "The price can swing hard in either direction, and nobody "
                    "outside the dispute knows which way. Only trade it if you "
                    "have independently checked the resolution criteria and "
                    "believe you know the answer better than the disputer does."
                ),
            )
        )

    # --- 2. Proposition sans convergence du prix ---------------------------
    # Un marché dont la résolution est proposée devrait coter ~1.00 du bon côté.
    # S'il ne le fait pas, soit personne n'a vu la proposition, soit le marché
    # la conteste implicitement. On ne tranche pas : on signale l'écart.
    elif (
        m.proposed
        and m.gap >= PROPOSAL_GAP_MIN
        and m.liquidity >= PROPOSAL_GAP_MIN_LIQUIDITY
        and m.accepting_orders
    ):
        sev = 40 + 100 * min(m.gap, 0.3) + 15 * _liquidity_weight(m.liquidity)
        signals.append(
            Signal(
                kind="proposal_gap",
                severity=sev,
                market=m,
                headline=f"Proposed, but price is {m.gap*100:.0f}% from certainty",
                detail=(
                    f"A resolution is already proposed, yet the favourite trades "
                    f"at {m.winner_price:.3f} instead of ~1.00. Either the market "
                    f"has not noticed, or it disagrees — which often precedes a "
                    f"dispute. Polymarket does not publish WHICH outcome was "
                    f"proposed, so check the market before acting."
                ),
                action=(
                    "**This is the one case here that can be traded.** Open the "
                    "market, read its resolution criteria, and check the outcome "
                    "yourself against the stated source. If you can confirm the "
                    "favourite is right, buying it below 1.00 pays the difference "
                    "at settlement.\n"
                    "**If you cannot confirm it yourself, skip it** — the gap "
                    "usually means the market disagrees, and a dispute follows."
                ),
            )
        )

    # --- 3. Échu depuis longtemps, sans résolution -------------------------
    if (
        m.days_late >= OVERDUE_DAYS
        and not m.proposed
        and m.liquidity >= MIN_LIQUIDITY
    ):
        # Le capital immobilisé est le vrai coût : jours × liquidité.
        sev = 20 + 25 * min(m.days_late / 60.0, 1.0) + 25 * _liquidity_weight(m.liquidity)
        signals.append(
            Signal(
                kind="overdue",
                tier="alert" if m.liquidity >= OVERDUE_ALERT_LIQUIDITY else "digest",
                severity=sev,
                market=m,
                headline=f"Overdue {m.days_late:.0f} days, no proposal yet",
                detail=(
                    f"Ended {m.end.date() if m.end else '?'} but still open with no "
                    f"resolution proposed. {fmt_usd(m.liquidity)} of liquidity is "
                    f"sitting here with no settlement date. Usually means the "
                    f"resolution criteria turned out to be ambiguous."
                ),
                action=(
                    "**Nothing to buy here — this is a warning.** Money committed "
                    "to this market has no settlement date. Treat it as the cost "
                    "of markets whose wording was never precise enough, and "
                    "factor that risk in before entering similar ones."
                ),
            )
        )

    # --- 4. Choc de prix ---------------------------------------------------
    # Le cas Venezuela : −66 points en 24 h, quatre mois avant l'échéance, sur un
    # marché en cours de contestation. Le choc est souvent la trace visible d'une
    # information de résolution.
    far_from_end = m.days_late < -SHOCK_MIN_DAYS_BEFORE_END
    if (
        abs(m.day_change) >= SHOCK_MIN
        and m.liquidity >= MIN_LIQUIDITY
        and (far_from_end or m.disputed)
        and m.turnover >= SHOCK_MIN_TURNOVER
        and m.volume24h >= SHOCK_MIN_VOLUME
    ):
        direction = "collapsed" if m.day_change < 0 else "spiked"
        sev = 30 + 60 * min(abs(m.day_change), 0.8) + 10 * _liquidity_weight(m.liquidity)
        signals.append(
            Signal(
                kind="shock",
                severity=sev,
                market=m,
                headline=f"Price {direction} {abs(m.day_change)*100:.0f} pts in 24h",
                detail=(
                    f"YES moved to {m.yes_price:.3f} ({m.day_change*+100:+.0f} pts "
                    f"in a day, {m.hour_change*100:+.1f} in the last hour) on "
                    f"{fmt_usd(m.volume24h)} of volume — {m.turnover:.1f}× the "
                    f"book depth in a day, which is what an information event "
                    f"looks like rather than a drifting quote."
                ),
                action=(
                    "**Find the news before doing anything.** A move this size "
                    "with this much turnover means informed money already acted. "
                    "Buying the direction of the move after the fact is chasing; "
                    "the tradable question is whether it overshot, and you can "
                    "only answer that by knowing what happened."
                ),
            )
        )

    # --- 5. Résolution imminente sur marché liquide ------------------------
    if (
        -SOON_HOURS / 24.0 <= m.days_late < 0
        and m.liquidity >= SOON_MIN_LIQUIDITY
        and not m.proposed
    ):
        sev = 10 + 20 * _liquidity_weight(m.liquidity)
        signals.append(
            Signal(
                kind="resolving_soon",
                tier="digest",
                severity=sev,
                market=m,
                headline=f"Resolves {fmt_delay(m.days_late)}",
                detail=(
                    f"{fmt_usd(m.liquidity)} liquidity, spread {m.spread*100:.1f}¢. "
                    f"Liquidity usually thins out right after the end date and "
                    f"before settlement — plan the exit now if you hold this."
                ),
                action=(
                    "**Only matters if you hold it.** Getting out is cheapest "
                    "now, while the book is still deep."
                ),
            )
        )

    return signals


# ---------------------------------------------------------------------------
# Réseau
# ---------------------------------------------------------------------------


async def _get(session, url, params=None, retries=2):
    for attempt in range(retries + 1):
        try:
            async with session.get(url, params=params, headers=UA, timeout=30) as r:
                if r.status == 429:
                    await asyncio.sleep(2 + attempt * 3)
                    continue
                if r.status == 422:
                    return None  # offset au-delà de la limite : fin de pagination
                r.raise_for_status()
                return await r.json()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            if attempt == retries:
                return None
            await asyncio.sleep(1 + attempt)
    return None


async def _sweep(session, order: str, ascending: bool) -> list[dict]:
    """Balaie une passe complète, offsets en parallèle.

    Les offsets sont connus d'avance, donc rien n'oblige à les enchaîner : on les
    lance par lots. Au-delà de la limite de l'API, les pages répondent 422 et
    sont simplement vides — quelques requêtes perdues valent mieux qu'une
    pagination séquentielle de 22 allers-retours.
    """
    sem = asyncio.Semaphore(SWEEP_CONCURRENCY)

    async def page(offset: int):
        async with sem:
            return await _get(
                session,
                f"{GAMMA}/markets",
                {
                    "active": "true",
                    "closed": "false",
                    "archived": "false",
                    "limit": PAGE,
                    "offset": offset,
                    "order": order,
                    "ascending": "true" if ascending else "false",
                },
            )

    pages = await asyncio.gather(
        *(page(o) for o in range(0, MAX_OFFSET + 1, PAGE))
    )
    out = []
    for p in pages:
        if p:
            out.extend(p)
    return out


@dataclass
class RadarResult:
    signals: list[Signal]
    markets_seen: int
    duration: float
    counts: dict = field(default_factory=dict)
    locked_capital: float = 0.0  # liquidité totale coincée dans les marchés échus
    overdue_total: int = 0

    @property
    def alerts(self) -> list[Signal]:
        """Ce qui mérite d'être poussé dans un salon."""
        return [s for s in self.signals if s.tier == "alert"]

    @property
    def digest(self) -> list[Signal]:
        """Ce qui n'a sa place que dans le tableau : vrai, mais pas une nouvelle."""
        return [s for s in self.signals if s.tier == "digest"]

    def by_kind(self, kind: str) -> list[Signal]:
        return [s for s in self.signals if s.kind == kind]


async def scan() -> RadarResult:
    started = asyncio.get_event_loop().time()

    async with aiohttp.ClientSession() as session:
        # Deux passes complémentaires. Par échéance : les marchés échus, que le
        # tri par volume ne remonterait jamais (ils ne s'échangent plus). Par
        # volume : les gros marchés dont la résolution se conteste alors que
        # l'échéance est encore loin — invisibles au tri par échéance.
        by_end, by_vol = await asyncio.gather(
            _sweep(session, "endDate", ascending=True),
            _sweep(session, "volume24hr", ascending=False),
        )

    now = datetime.datetime.now(datetime.timezone.utc)
    seen: dict[str, Market] = {}
    for raw in by_end + by_vol:
        m = parse_market(raw, now)
        if m and m.id not in seen:
            seen[m.id] = m

    signals: list[Signal] = []
    for m in seen.values():
        signals.extend(classify(m))

    signals.sort(key=lambda s: s.severity, reverse=True)

    counts: dict[str, int] = {}
    for s in signals:
        counts[s.kind] = counts.get(s.kind, 0) + 1

    overdue = [s.market for s in signals if s.kind == "overdue"]

    return RadarResult(
        signals=signals,
        markets_seen=len(seen),
        duration=asyncio.get_event_loop().time() - started,
        counts=counts,
        locked_capital=sum(m.liquidity for m in overdue),
        overdue_total=len(overdue),
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

KIND_LABEL = {
    "dispute": "🔴 DISPUTE",
    "proposal_gap": "🟠 PROPOSAL GAP",
    "overdue": "🟡 OVERDUE",
    "shock": "🔵 SHOCK",
    "resolving_soon": "⚪ SOON",
}


async def _main():
    import sys

    only = None
    for arg in sys.argv[1:]:
        if arg.startswith("--only="):
            only = arg.split("=", 1)[1]

    print("Scan de la fin de vie des marchés Polymarket…\n")
    res = await scan()

    print(f"Marchés uniques analysés : {res.markets_seen}")
    print(f"Durée                    : {res.duration:.1f}s")
    print(f"Signaux                  : {len(res.signals)}  {res.counts}")
    print(f"  dont à pousser en alerte : {len(res.alerts)}")
    print(f"Capital bloqué (échus)   : {fmt_usd(res.locked_capital)} "
          f"sur {res.overdue_total} marchés\n")

    shown = [s for s in res.signals if not only or s.kind == only]
    if not shown:
        print("Aucun signal. Toutes les résolutions suivent leur cours normal.")
        return

    for s in shown[:40]:
        m = s.market
        print("─" * 78)
        print(f"{KIND_LABEL.get(s.kind, s.kind)}  [sev {s.severity:.0f}]  {s.headline}")
        print(f"  {m.title[:72]}")
        print(
            f"  prix {m.yes_price:.3f} · spread {m.spread*100:.1f}¢ · "
            f"liq {fmt_usd(m.liquidity)} · vol24h {fmt_usd(m.volume24h)} · "
            f"{fmt_delay(m.days_late)}"
        )
        if m.statuses:
            print(f"  UMA: {' → '.join(m.statuses)}")
        print(f"  {m.url}")


if __name__ == "__main__":
    asyncio.run(_main())
