"""
Enregistreur — constitue l'historique de prix que Polymarket ne fournit pas.

Raison d'être, mesurée le 2026-08-15 : l'API `prices-history` renvoie **zéro
point** sur les marchés clos. Vérifié sur 500 marchés résolus à fort volume,
tous paramètres confondus. Polymarket purge l'historique après résolution.

Conséquence : aucune stratégie statistique n'est backtestable sur Polymarket.
Calibration, biais favori/outsider, valeur du temps — tout exige un historique
qui n'existe pas rétroactivement et ne s'achète pas. Le seul moyen de l'avoir
est de l'enregistrer soi-même, et chaque jour d'attente est perdu définitivement.

Le Radar interroge déjà 4 000 marchés toutes les dix minutes et jette tout. Ce
module garde ces observations, et surtout **les rattache à leur résolution** —
c'est le couple (prix observé, issue réelle) qui rend le jeu de données utile.

Base séparée de `radar.db` : la base opérationnelle du bot reste petite et
rapide, et le jeu de données se copie ou se sauvegarde indépendamment.

Testable seul :  python3 history.py --stats
"""

from __future__ import annotations

import datetime
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

# Un tick Polymarket vaut 0.001. On enregistre à partir de 2 ticks pour ne pas
# stocker le clignotement permanent du meilleur prix sur les marchés liquides.
PRICE_EPS = 0.002
# Même immobile, un marché doit laisser une trace régulière : sans ça on ne peut
# pas distinguer « le prix n'a pas bougé » de « le bot ne tournait pas ».
HEARTBEAT_SECONDS = 6 * 3600
# Au-delà, un marché disparu des scans est considéré comme à régler.
SETTLE_AFTER_SECONDS = 2 * 3600


@dataclass
class RecordStats:
    seen: int = 0
    inserted: int = 0
    unchanged: int = 0
    new_markets: int = 0

    def __str__(self) -> str:
        return (
            f"{self.seen} vus · {self.inserted} points écrits · "
            f"{self.unchanged} inchangés · {self.new_markets} nouveaux marchés"
        )


SCHEMA = """
CREATE TABLE IF NOT EXISTS markets(
  market_id   TEXT PRIMARY KEY,
  question    TEXT,
  slug        TEXT,
  event_slug  TEXT,
  label       TEXT,
  end_date    TEXT,
  first_seen  INTEGER,
  last_seen   INTEGER,
  resolved    INTEGER DEFAULT 0,
  outcome     INTEGER,            -- 1 = YES a gagné, 0 = NO
  resolved_at INTEGER
);

CREATE TABLE IF NOT EXISTS ticks(
  market_id  TEXT,
  ts         INTEGER,
  price      REAL,
  bid        REAL,
  ask        REAL,
  volume24h  REAL,
  liquidity  REAL,
  uma        TEXT,
  PRIMARY KEY(market_id, ts)
) WITHOUT ROWID;

-- Requête reine du jeu de données : « le prix N jours avant la résolution, et
-- ce qui s'est réellement passé ». Sans cet index elle balaie toute la table.
CREATE INDEX IF NOT EXISTS idx_ticks_ts ON ticks(ts);
CREATE INDEX IF NOT EXISTS idx_markets_resolved ON markets(resolved, last_seen);
"""


class Recorder:
    """Journalise les observations de marché, en n'écrivant que les changements.

    Enregistrer 4 000 marchés toutes les 10 minutes ferait 576 000 lignes par
    jour, dont l'écrasante majorité identiques à la précédente. On n'écrit que
    lorsqu'un prix bouge d'au moins deux ticks, qu'un statut UMA change, ou que
    le battement de cœur est dû. La série reste fidèle pour une fraction du coût.
    """

    def __init__(self, path: str | Path = "history.db"):
        self.path = str(path)
        self.db = sqlite3.connect(self.path)
        # WAL : les écritures ne bloquent pas les lectures, ce qui permet
        # d'analyser le jeu de données pendant que le bot continue d'écrire.
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self.db.commit()
        self._last: dict[str, tuple[int, float, str]] = {}
        self._load_last()

    def _load_last(self):
        """Dernier point connu par marché, gardé en mémoire.

        Sans ce cache il faudrait 4 000 requêtes par cycle pour savoir si le prix
        a bougé. Le cache tient en quelques centaines de Ko.
        """
        cur = self.db.execute(
            """SELECT t.market_id, t.ts, t.price, t.uma FROM ticks t
               JOIN (SELECT market_id, MAX(ts) AS m FROM ticks GROUP BY market_id) x
                 ON x.market_id = t.market_id AND x.m = t.ts"""
        )
        for mid, ts, price, uma in cur:
            self._last[mid] = (ts, price, uma or "")

    # -- écriture ---------------------------------------------------------

    def _should_write(self, mid: str, ts: int, price: float, uma: str) -> bool:
        prev = self._last.get(mid)
        if prev is None:
            return True
        pts, pprice, puma = prev
        if abs(price - pprice) >= PRICE_EPS:
            return True
        if uma != puma:  # un changement de statut UMA est toujours un événement
            return True
        return ts - pts >= HEARTBEAT_SECONDS

    def record(self, markets, ts: int | None = None) -> RecordStats:
        ts = int(ts if ts is not None else datetime.datetime.now(datetime.timezone.utc).timestamp())
        st = RecordStats(seen=len(markets))
        meta, ticks = [], []

        for m in markets:
            uma = ",".join(getattr(m, "statuses", []) or [])
            price = float(m.yes_price)
            meta.append(
                (
                    m.id, m.question, m.slug, getattr(m, "event_slug", ""),
                    getattr(m, "label", ""),
                    m.end.isoformat() if getattr(m, "end", None) else None,
                    ts, ts,
                )
            )
            if self._should_write(m.id, ts, price, uma):
                ticks.append(
                    (m.id, ts, price, float(m.best_bid), float(m.best_ask),
                     float(m.volume24h), float(m.liquidity), uma)
                )
                self._last[m.id] = (ts, price, uma)
            else:
                st.unchanged += 1

        before = self.db.execute("SELECT COUNT(*) FROM markets").fetchone()[0]
        with self.db:
            self.db.executemany(
                """INSERT INTO markets(market_id,question,slug,event_slug,label,
                                       end_date,first_seen,last_seen)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(market_id) DO UPDATE SET
                     last_seen=excluded.last_seen,
                     question=excluded.question,
                     end_date=excluded.end_date""",
                meta,
            )
            # OR IGNORE : deux cycles dans la même seconde ne doivent pas planter.
            self.db.executemany(
                "INSERT OR IGNORE INTO ticks VALUES(?,?,?,?,?,?,?,?)", ticks
            )
        after = self.db.execute("SELECT COUNT(*) FROM markets").fetchone()[0]

        st.inserted = len(ticks)
        st.new_markets = after - before
        return st

    # -- résolution -------------------------------------------------------

    def pending_settlement(self, now: int | None = None) -> list[str]:
        """Marchés vus autrefois, non résolus, absents des scans récents.

        Un marché qui disparaît des scans est presque toujours un marché qui
        vient de se résoudre : c'est le moment d'aller chercher son issue.
        """
        now = int(now if now is not None else datetime.datetime.now(datetime.timezone.utc).timestamp())
        cur = self.db.execute(
            "SELECT market_id FROM markets WHERE resolved=0 AND last_seen < ?",
            (now - SETTLE_AFTER_SECONDS,),
        )
        return [r[0] for r in cur]

    def settle(self, results: dict[str, int], now: int | None = None) -> int:
        """Inscrit l'issue réelle. `results` : {market_id: 1 si YES a gagné}.

        C'est cette table qui donne sa valeur à tout le reste : une série de prix
        sans son dénouement ne permet de mesurer aucune calibration.
        """
        now = int(now if now is not None else datetime.datetime.now(datetime.timezone.utc).timestamp())
        rows = [(int(v), now, k) for k, v in results.items() if v in (0, 1)]
        with self.db:
            self.db.executemany(
                "UPDATE markets SET resolved=1, outcome=?, resolved_at=? WHERE market_id=?",
                rows,
            )
        return len(rows)

    # -- lecture ----------------------------------------------------------

    def series(self, market_id: str) -> list[tuple]:
        return self.db.execute(
            "SELECT ts, price, bid, ask, volume24h, liquidity, uma "
            "FROM ticks WHERE market_id=? ORDER BY ts",
            (market_id,),
        ).fetchall()

    def price_before_resolution(self, days: float) -> list[tuple[float, int]]:
        """Couples (prix N jours avant la résolution, issue réelle).

        La brique exacte d'une étude de calibration : on pourra enfin répondre
        « un contrat à 5 % se réalise-t-il vraiment 5 % du temps ? », question
        aujourd'hui impossible faute d'historique public.
        """
        out = []
        cur = self.db.execute(
            "SELECT market_id, outcome, resolved_at FROM markets "
            "WHERE resolved=1 AND outcome IS NOT NULL AND resolved_at IS NOT NULL"
        )
        for mid, outcome, res_at in cur:
            row = self.db.execute(
                "SELECT price FROM ticks WHERE market_id=? AND ts<=? "
                "ORDER BY ts DESC LIMIT 1",
                (mid, res_at - int(days * 86400)),
            ).fetchone()
            if row:
                out.append((float(row[0]), int(outcome)))
        return out

    def stats(self) -> dict:
        q = lambda s: self.db.execute(s).fetchone()[0]
        size = os.path.getsize(self.path) if os.path.exists(self.path) else 0
        for suffix in ("-wal", "-shm"):
            p = self.path + suffix
            if os.path.exists(p):
                size += os.path.getsize(p)
        span = self.db.execute("SELECT MIN(ts), MAX(ts) FROM ticks").fetchone()
        days = ((span[1] - span[0]) / 86400) if span[0] and span[1] else 0.0
        return {
            "markets": q("SELECT COUNT(*) FROM markets"),
            "resolved": q("SELECT COUNT(*) FROM markets WHERE resolved=1"),
            "ticks": q("SELECT COUNT(*) FROM ticks"),
            "days": days,
            "mb": size / 1e6,
            "mb_per_day": (size / 1e6 / days) if days > 0.5 else 0.0,
        }

    def close(self):
        self.db.close()


# ---------------------------------------------------------------------------


def _main():
    import sys

    rec = Recorder(Path(__file__).with_name("history.db"))
    s = rec.stats()
    print("Jeu de données Polymarket")
    print(f"  marchés suivis   : {s['markets']:,}")
    print(f"  dont résolus     : {s['resolved']:,}  ← la partie exploitable")
    print(f"  points de prix   : {s['ticks']:,}")
    print(f"  profondeur       : {s['days']:.1f} jours")
    print(f"  taille           : {s['mb']:.1f} Mo", end="")
    print(f"  ({s['mb_per_day']:.1f} Mo/jour)" if s["mb_per_day"] else "")

    if "--calib" in sys.argv:
        import statistics

        for d in (1, 7):
            rows = rec.price_before_resolution(d)
            if len(rows) < 30:
                print(f"\n{d}j avant résolution : {len(rows)} obs — trop peu, patience")
                continue
            print(f"\n═══ {d} jour(s) avant résolution — {len(rows)} marchés ═══")
            for lo, hi in [(0,.05),(.05,.15),(.15,.30),(.30,.50),
                           (.50,.70),(.70,.85),(.85,.95),(.95,1.01)]:
                b = [r for r in rows if lo <= r[0] < hi]
                if len(b) < 10:
                    continue
                mp = statistics.mean(r[0] for r in b)
                fr = sum(r[1] for r in b) / len(b)
                print(f"  {lo:.2f}-{hi:<5.2f} n={len(b):>4} "
                      f"prix {mp:.3f} → réalisé {fr:.3f}  écart {fr-mp:+.3f}")
    rec.close()


if __name__ == "__main__":
    _main()
