"""
Tests de l'enregistreur — sans réseau, sur base temporaire.

Enjeu particulier : une erreur ici ne se voit pas. Un enregistreur qui perd
silencieusement des points produit un jeu de données d'apparence normale, et le
défaut n'apparaît que des mois plus tard, au moment de l'exploiter — quand les
données perdues le sont définitivement.

    python3 selftest_history.py
"""

from __future__ import annotations

import os
import sys
import tempfile

import history as H


class FakeMarket:
    """Minimum vital d'un Market du Radar."""

    def __init__(self, mid="m1", price=0.50, bid=None, ask=None,
                 statuses=None, vol=1000.0, liq=5000.0, question="Q?"):
        self.id = mid
        self.question = question
        self.slug = f"slug-{mid}"
        self.event_slug = f"ev-{mid}"
        self.label = ""
        self.end = None
        self.yes_price = price
        self.best_bid = bid if bid is not None else price - 0.01
        self.best_ask = ask if ask is not None else price + 0.01
        self.volume24h = vol
        self.liquidity = liq
        self.statuses = statuses or []


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def fresh():
    path = tempfile.mktemp(suffix=".db")
    return H.Recorder(path), path


def cleanup(rec, path):
    rec.close()
    for s in ("", "-wal", "-shm"):
        try:
            os.remove(path + s)
        except OSError:
            pass


# --- écriture de base -------------------------------------------------------


@case
def test_premier_passage_ecrit_tout():
    rec, p = fresh()
    try:
        st = rec.record([FakeMarket("a", 0.5), FakeMarket("b", 0.2)], ts=1000)
        assert st.inserted == 2, st
        assert st.new_markets == 2, st
        assert rec.stats()["ticks"] == 2
    finally:
        cleanup(rec, p)


@case
def test_prix_identique_nest_pas_reecrit():
    """Le cœur de l'économie de place : 4 000 marchés × 144 cycles/jour dont
    presque aucun ne bouge."""
    rec, p = fresh()
    try:
        rec.record([FakeMarket("a", 0.5)], ts=1000)
        st = rec.record([FakeMarket("a", 0.5)], ts=1600)
        assert st.inserted == 0 and st.unchanged == 1, st
        assert rec.stats()["ticks"] == 1
    finally:
        cleanup(rec, p)


@case
def test_micro_variation_ignoree():
    """Un seul tick de clignotement ne mérite pas une ligne."""
    rec, p = fresh()
    try:
        rec.record([FakeMarket("a", 0.500)], ts=1000)
        st = rec.record([FakeMarket("a", 0.5005)], ts=1600)
        assert st.inserted == 0, st
    finally:
        cleanup(rec, p)


@case
def test_vraie_variation_ecrite():
    rec, p = fresh()
    try:
        rec.record([FakeMarket("a", 0.500)], ts=1000)
        st = rec.record([FakeMarket("a", 0.503)], ts=1600)
        assert st.inserted == 1, st
        assert rec.stats()["ticks"] == 2
    finally:
        cleanup(rec, p)


@case
def test_seuil_exact():
    """PRICE_EPS doit être inclusif, sinon on perd les mouvements pile au seuil."""
    rec, p = fresh()
    try:
        rec.record([FakeMarket("a", 0.500)], ts=1000)
        st = rec.record([FakeMarket("a", 0.500 + H.PRICE_EPS)], ts=1600)
        assert st.inserted == 1, "un mouvement égal au seuil doit être gardé"
    finally:
        cleanup(rec, p)


@case
def test_battement_de_coeur():
    """Sans point périodique, impossible de distinguer « rien n'a bougé » de
    « le bot était éteint »."""
    rec, p = fresh()
    try:
        rec.record([FakeMarket("a", 0.5)], ts=1000)
        st = rec.record([FakeMarket("a", 0.5)], ts=1000 + H.HEARTBEAT_SECONDS)
        assert st.inserted == 1, st
    finally:
        cleanup(rec, p)


@case
def test_changement_uma_toujours_ecrit():
    """Un statut UMA qui change est un événement, même à prix constant."""
    rec, p = fresh()
    try:
        rec.record([FakeMarket("a", 0.5, statuses=[])], ts=1000)
        st = rec.record([FakeMarket("a", 0.5, statuses=["proposed"])], ts=1600)
        assert st.inserted == 1, st
        assert rec.series("a")[-1][6] == "proposed"
    finally:
        cleanup(rec, p)


@case
def test_meme_seconde_ne_plante_pas():
    rec, p = fresh()
    try:
        rec.record([FakeMarket("a", 0.5)], ts=1000)
        rec.record([FakeMarket("a", 0.9)], ts=1000)   # même ts, clé primaire
        assert rec.stats()["ticks"] == 1
    finally:
        cleanup(rec, p)


# --- persistance ------------------------------------------------------------


@case
def test_cache_reconstruit_a_la_reouverture():
    """Après un redémarrage du bot, le dédoublonnage doit continuer de marcher —
    sinon chaque redémarrage réécrit inutilement les 4 000 marchés."""
    rec, p = fresh()
    try:
        rec.record([FakeMarket("a", 0.5)], ts=1000)
        rec.close()
        rec = H.Recorder(p)
        st = rec.record([FakeMarket("a", 0.5)], ts=1600)
        assert st.inserted == 0, "le cache n'a pas été rechargé"
    finally:
        cleanup(rec, p)


@case
def test_schema_idempotent():
    rec, p = fresh()
    try:
        rec.close()
        rec = H.Recorder(p)   # rouvrir ne doit rien casser
        rec.record([FakeMarket("a")], ts=1000)
        assert rec.stats()["markets"] == 1
    finally:
        cleanup(rec, p)


@case
def test_metadonnees_mises_a_jour():
    rec, p = fresh()
    try:
        rec.record([FakeMarket("a", question="ancienne")], ts=1000)
        rec.record([FakeMarket("a", 0.9, question="nouvelle")], ts=2000)
        row = rec.db.execute(
            "SELECT question, first_seen, last_seen FROM markets WHERE market_id='a'"
        ).fetchone()
        assert row[0] == "nouvelle", row
        assert row[1] == 1000 and row[2] == 2000, "first_seen doit rester le premier"
    finally:
        cleanup(rec, p)


# --- résolution -------------------------------------------------------------


@case
def test_marche_disparu_passe_en_attente():
    rec, p = fresh()
    try:
        rec.record([FakeMarket("a"), FakeMarket("b")], ts=1000)
        now = 1000 + H.SETTLE_AFTER_SECONDS + 1
        assert sorted(rec.pending_settlement(now=now)) == ["a", "b"]
        rec.record([FakeMarket("a")], ts=now)          # « a » revu
        assert rec.pending_settlement(now=now) == ["b"]
    finally:
        cleanup(rec, p)


@case
def test_settle_inscrit_lissue():
    rec, p = fresh()
    try:
        rec.record([FakeMarket("a"), FakeMarket("b")], ts=1000)
        assert rec.settle({"a": 1, "b": 0}, now=5000) == 2
        rows = dict(rec.db.execute("SELECT market_id, outcome FROM markets"))
        assert rows == {"a": 1, "b": 0}, rows
        assert rec.stats()["resolved"] == 2
        assert rec.pending_settlement(now=10**9) == [], "un résolu ne doit plus être en attente"
    finally:
        cleanup(rec, p)


@case
def test_settle_ignore_les_valeurs_invalides():
    rec, p = fresh()
    try:
        rec.record([FakeMarket("a")], ts=1000)
        assert rec.settle({"a": None}) == 0
        assert rec.settle({"a": 7}) == 0
        assert rec.stats()["resolved"] == 0
    finally:
        cleanup(rec, p)


# --- la requête qui justifie tout -------------------------------------------


@case
def test_prix_avant_resolution():
    """La brique de l'étude de calibration : prix à J-N, puis issue réelle."""
    rec, p = fresh()
    try:
        res_at = 10_000_000
        j = 86400
        rec.record([FakeMarket("a", 0.30)], ts=res_at - 8 * j)
        rec.record([FakeMarket("a", 0.60)], ts=res_at - 5 * j)
        rec.record([FakeMarket("a", 0.95)], ts=res_at - 1 * j)
        rec.settle({"a": 1}, now=res_at)

        assert rec.price_before_resolution(7) == [(0.30, 1)], rec.price_before_resolution(7)
        assert rec.price_before_resolution(3) == [(0.60, 1)]
        assert rec.price_before_resolution(0.5) == [(0.95, 1)]
        # Antérieur au premier point : rien, plutôt qu'une valeur inventée.
        assert rec.price_before_resolution(30) == []
    finally:
        cleanup(rec, p)


@case
def test_non_resolus_exclus_de_letude():
    rec, p = fresh()
    try:
        rec.record([FakeMarket("a", 0.4)], ts=1000)
        assert rec.price_before_resolution(0) == [], "un marché non résolu n'a pas d'issue"
    finally:
        cleanup(rec, p)


@case
def test_calibration_sur_donnees_synthetiques():
    """Bout en bout : 200 marchés dont l'issue suit exactement le prix affiché.
    Une calibration parfaite doit ressortir parfaite."""
    import random

    random.seed(7)
    rec, p = fresh()
    try:
        res_at, j = 10_000_000, 86400
        for i in range(200):
            price = round(random.uniform(0.05, 0.95), 3)
            win = 1 if random.random() < price else 0
            mid = f"m{i}"
            rec.record([FakeMarket(mid, price)], ts=res_at - 5 * j)
            rec.settle({mid: win}, now=res_at)

        rows = rec.price_before_resolution(3)
        assert len(rows) == 200, len(rows)
        avg_p = sum(r[0] for r in rows) / len(rows)
        avg_w = sum(r[1] for r in rows) / len(rows)
        # 200 tirages : ±0.07 est large mais exclut une erreur systématique.
        assert abs(avg_p - avg_w) < 0.07, f"prix moyen {avg_p:.3f} vs réalisé {avg_w:.3f}"
    finally:
        cleanup(rec, p)


# --- rythme et échéance -----------------------------------------------------


@case
def test_rythme_inconnu_au_demarrage():
    """Avec quelques minutes d'enregistrement, aucun rythme n'est mesurable."""
    rec, p = fresh()
    try:
        rec.record([FakeMarket("a")], ts=1_000_000)
        assert rec.resolution_rate(now=1_000_600) == 0.0
        assert rec.eta_days(100, now=1_000_600) is None, "ne pas inventer une date"
    finally:
        cleanup(rec, p)


@case
def test_rythme_mesure_sur_fenetre():
    rec, p = fresh()
    try:
        j = 86400
        now = 10_000_000
        rec.record([FakeMarket(f"m{i}") for i in range(30)], ts=now - 10 * j)
        # 15 résolutions étalées sur les 3 derniers jours
        for i in range(15):
            rec.settle({f"m{i}": 1}, now=now - int(i * j / 5))
        r = rec.resolution_rate(window_days=3.0, now=now)
        assert 4.0 < r < 6.0, f"attendu ~5/jour, obtenu {r:.2f}"
    finally:
        cleanup(rec, p)


@case
def test_rythme_rapporte_au_temps_vecu():
    """Trois résolutions en 12 h font 6/jour, pas 1/jour : diviser par une
    fenêtre de 3 jours qu'on n'a pas vécue diviserait le rythme par six."""
    rec, p = fresh()
    try:
        now = 10_000_000
        start = now - 43200  # 12 h d'enregistrement
        rec.record([FakeMarket(f"m{i}") for i in range(5)], ts=start)
        for i in range(3):
            rec.settle({f"m{i}": 1}, now=now - i * 1000)
        r = rec.resolution_rate(window_days=3.0, now=now)
        assert 5.0 < r < 7.0, f"attendu ~6/jour, obtenu {r:.2f}"
    finally:
        cleanup(rec, p)


@case
def test_eta_coherente():
    rec, p = fresh()
    try:
        j = 86400
        now = 10_000_000
        rec.record([FakeMarket(f"m{i}") for i in range(60)], ts=now - 10 * j)
        for i in range(20):
            rec.settle({f"m{i}": 1}, now=now - int(i * j / 10))
        # Les 20 résolutions tombent dans la fenêtre de 3 jours → 6,7/jour,
        # et non 10/jour : le rythme se lit sur la fenêtre, pas sur l'étalement
        # réel des résolutions.
        eta = rec.eta_days(100, now=now)
        assert eta is not None
        assert 10 < eta < 14, f"80 restants à ~6,7/jour → ~12j, obtenu {eta:.1f}"
    finally:
        cleanup(rec, p)


@case
def test_eta_nulle_si_objectif_atteint():
    rec, p = fresh()
    try:
        rec.record([FakeMarket(f"m{i}") for i in range(5)], ts=1_000_000)
        rec.settle({f"m{i}": 1 for i in range(5)}, now=1_100_000)
        assert rec.eta_days(3, now=1_100_000) == 0.0
    finally:
        cleanup(rec, p)


# --- volumétrie -------------------------------------------------------------


@case
def test_volumetrie_realiste():
    """4 000 marchés, 12 cycles, 5 % qui bougent : on vérifie que le
    delta-encodage tient sa promesse plutôt que de le supposer."""
    import random

    random.seed(1)
    rec, p = fresh()
    try:
        markets = [FakeMarket(f"m{i}", round(random.uniform(0.02, 0.98), 3))
                   for i in range(4000)]
        total = 0
        for c in range(12):
            for m in markets:
                if random.random() < 0.05:
                    m.yes_price = max(0.001, min(0.999, m.yes_price + random.uniform(-0.05, 0.05)))
            total += rec.record(markets, ts=1000 + c * 600).inserted

        naif = 4000 * 12
        print(f"       {total:,} points écrits contre {naif:,} en enregistrement naïf "
              f"({100*total/naif:.0f} %)")
        assert total < naif * 0.35, f"delta-encodage inefficace : {total}"
        assert total > 4000, "au minimum le premier passage complet"
    finally:
        cleanup(rec, p)


@case
def test_stats_coherentes():
    rec, p = fresh()
    try:
        rec.record([FakeMarket("a", 0.5), FakeMarket("b", 0.5)], ts=1000)
        rec.record([FakeMarket("a", 0.9)], ts=1000 + 10 * 86400)
        rec.settle({"b": 1})
        s = rec.stats()
        assert s["markets"] == 2 and s["ticks"] == 3 and s["resolved"] == 1, s
        assert 9.5 < s["days"] < 10.5, s["days"]
        assert s["mb"] > 0
    finally:
        cleanup(rec, p)


@case
def test_series_ordonnee():
    rec, p = fresh()
    try:
        for i, pr in enumerate([0.1, 0.5, 0.9]):
            rec.record([FakeMarket("a", pr)], ts=1000 + i * 700)
        ts = [r[0] for r in rec.series("a")]
        assert ts == sorted(ts) and len(ts) == 3, ts
    finally:
        cleanup(rec, p)


# ---------------------------------------------------------------------------


def main() -> int:
    failed = 0
    for fn in CASES:
        try:
            fn()
        except AssertionError as e:
            failed += 1
            print(f"  ÉCHEC  {fn.__name__}\n         {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERREUR {fn.__name__}\n         {type(e).__name__}: {e}")
        else:
            print(f"  ok     {fn.__name__}")
    print()
    if failed:
        print(f"{failed} test(s) en échec sur {len(CASES)}.")
        return 1
    print(f"{len(CASES)} tests passés.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
