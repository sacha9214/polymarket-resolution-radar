"""
Tests du moteur Resolution Radar — sans réseau, sur marchés synthétiques.

Un radar qui ne signale rien est indiscernable d'un radar cassé. Ces tests
fabriquent des situations dont la réponse attendue est évidente, et vérifient
surtout les FILTRES : c'est là que se joue la différence entre un salon utile et
un salon noyé sous le bruit.

    python3 selftest.py
"""

from __future__ import annotations

import datetime
import sys

import resolution as R

NOW = datetime.datetime(2026, 8, 14, 12, 0, tzinfo=datetime.timezone.utc)


def mk(
    *,
    days_late=-30.0,
    statuses=(),
    yes=0.50,
    liquidity=50_000.0,
    volume24h=50_000.0,
    day_change=0.0,
    hour_change=0.0,
    question="Test market",
    label="",
    accepting=True,
) -> R.Market:
    return R.Market(
        id="m1",
        question=question,
        slug="slug",
        event_slug="ev",
        label=label,
        end=NOW - datetime.timedelta(days=days_late),
        days_late=days_late,
        statuses=list(statuses),
        yes_price=yes,
        best_bid=max(yes - 0.01, 0.0),
        best_ask=min(yes + 0.01, 1.0),
        spread=0.02,
        liquidity=liquidity,
        volume24h=volume24h,
        day_change=day_change,
        hour_change=hour_change,
        resolution_source="",
        bond=500.0,
        accepting_orders=accepting,
    )


def kinds(m: R.Market) -> set[str]:
    return {s.kind for s in R.classify(m)}


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


# --- Contestation ----------------------------------------------------------


@case
def test_dispute_detecte():
    m = mk(statuses=["proposed", "disputed"])
    assert "dispute" in kinds(m)


@case
def test_dispute_compte_les_tours():
    m = mk(statuses=["proposed", "disputed", "proposed", "disputed"])
    assert m.dispute_rounds == 2
    sig = [s for s in R.classify(m) if s.kind == "dispute"][0]
    assert "2×" in sig.headline, sig.headline
    simple = [s for s in R.classify(mk(statuses=["proposed", "disputed"])) if s.kind == "dispute"][0]
    # Un deuxième litige est une information plus forte qu'un premier.
    assert sig.severity > simple.severity


@case
def test_proposition_seule_nest_pas_un_litige():
    assert "dispute" not in kinds(mk(statuses=["proposed"], yes=0.999))


@case
def test_dispute_prime_meme_sans_liquidite():
    """Le litige est trop rare pour être filtré par la liquidité."""
    m = mk(statuses=["proposed", "disputed"], liquidity=200.0, volume24h=0.0)
    assert "dispute" in kinds(m)


# --- Écart de proposition --------------------------------------------------


@case
def test_proposal_gap_detecte():
    m = mk(statuses=["proposed"], yes=0.90, liquidity=50_000.0)
    assert "proposal_gap" in kinds(m)


@case
def test_proposal_gap_ignore_prix_converge():
    """24 propositions sur 25 sont déjà à 0.9995 : c'est le cas normal."""
    assert "proposal_gap" not in kinds(mk(statuses=["proposed"], yes=0.9995))


@case
def test_proposal_gap_ignore_marche_mort():
    """Écart énorme mais 1 k$ de liquidité : rien à y faire."""
    m = mk(statuses=["proposed"], yes=0.80, liquidity=1_000.0, days_late=79)
    assert "proposal_gap" not in kinds(m)


@case
def test_proposal_gap_exige_carnet_ouvert():
    m = mk(statuses=["proposed"], yes=0.85, liquidity=50_000.0, accepting=False)
    assert "proposal_gap" not in kinds(m)


# --- Échéance dépassée -----------------------------------------------------


@case
def test_overdue_detecte():
    assert "overdue" in kinds(mk(days_late=120, liquidity=50_000.0))


@case
def test_overdue_ignore_resolution_en_cours():
    """Quelques heures de retard = fenêtre UMA normale, pas une anomalie."""
    assert "overdue" not in kinds(mk(days_late=0.5, liquidity=50_000.0))


@case
def test_overdue_ignore_si_proposition_deposee():
    m = mk(days_late=30, statuses=["proposed"], liquidity=50_000.0, yes=0.999)
    assert "overdue" not in kinds(m)


@case
def test_overdue_palier_selon_capital():
    gros = [s for s in R.classify(mk(days_late=100, liquidity=60_000.0)) if s.kind == "overdue"][0]
    petit = [s for s in R.classify(mk(days_late=100, liquidity=5_000.0)) if s.kind == "overdue"][0]
    assert gros.tier == "alert"
    assert petit.tier == "digest", "191 marchés échus en alerte noieraient le salon"


# --- Choc de prix ----------------------------------------------------------


@case
def test_shock_detecte_evenement_reel():
    """Cas Venezuela : −64 pts, 5,1× le carnet, loin de l'échéance."""
    m = mk(days_late=-139, day_change=-0.64, liquidity=32_000.0, volume24h=162_000.0)
    assert "shock" in kinds(m)


@case
def test_shock_ignore_fin_de_match():
    """Un esport qui passe à 1.00 à l'échéance : l'événement a lieu, ce n'est
    pas une anomalie de résolution. C'était 26 fausses alertes par scan."""
    m = mk(days_late=-0.1, day_change=0.71, liquidity=569_000.0, volume24h=398_000.0)
    assert "shock" not in kinds(m)


@case
def test_shock_ignore_derive_sans_volume():
    """Baseball : gros carnet, volume ridicule, rotation 0,04× → cote qui glisse."""
    m = mk(days_late=-7, day_change=0.30, liquidity=176_000.0, volume24h=6_500.0)
    assert "shock" not in kinds(m)
    assert mk(days_late=-7, liquidity=176_000.0, volume24h=6_500.0).turnover < 0.1


@case
def test_shock_garde_le_litige_meme_pres_de_lecheance():
    m = mk(days_late=-0.1, day_change=-0.5, statuses=["proposed", "disputed"],
           liquidity=30_000.0, volume24h=150_000.0)
    assert "shock" in kinds(m)


# --- Résolution imminente --------------------------------------------------


@case
def test_resolving_soon_est_digest():
    m = mk(days_late=-0.2, liquidity=700_000.0)
    sigs = [s for s in R.classify(m) if s.kind == "resolving_soon"]
    assert sigs and sigs[0].tier == "digest", "112 esports par scan ne sont pas des alertes"


@case
def test_resolving_soon_exige_liquidite():
    assert "resolving_soon" not in kinds(mk(days_late=-0.2, liquidity=30_000.0))


# --- Modèle ----------------------------------------------------------------


@case
def test_turnover():
    assert abs(mk(liquidity=32_000.0, volume24h=162_000.0).turnover - 5.0625) < 1e-6
    assert mk(liquidity=0.0, volume24h=10.0).turnover == 0.0  # pas de division par zéro


@case
def test_gap_symetrique():
    """Le favori peut être l'un ou l'autre camp : l'écart se mesure sur lui."""
    assert abs(mk(yes=0.86).gap - 0.14) < 1e-9
    assert abs(mk(yes=0.14).gap - 0.14) < 1e-9


@case
def test_cle_change_a_chaque_nouveau_litige():
    """Un DEUXIÈME litige doit re-déclencher ; le même litige revu, non."""
    k1 = R.classify(mk(statuses=["proposed", "disputed"]))[0].key
    k2 = R.classify(mk(statuses=["proposed", "disputed"]))[0].key
    k3 = R.classify(mk(statuses=["proposed", "disputed", "proposed", "disputed"]))[0].key
    assert k1 == k2
    assert k1 != k3


@case
def test_titre_ajoute_le_libelle_utile():
    m = mk(question="Will a country recognize Israel?", label="Venezuela")
    assert "Venezuela" in m.title
    # ...mais ne le répète pas s'il est déjà dans la question.
    assert mk(question="Will Venezuela recognize Israel?", label="Venezuela").title.count("Venezuela") == 1


@case
def test_parse_market_rejette_donnees_invalides():
    assert R.parse_market({"outcomePrices": '["abc","def"]'}, NOW) is None
    assert R.parse_market({"outcomePrices": "[]"}, NOW) is None
    assert R.parse_market({}, NOW) is None
    ok = R.parse_market(
        {"id": 1, "question": "q", "outcomePrices": '["0.4","0.6"]',
         "endDate": "2026-08-01T00:00:00Z", "umaResolutionStatuses": '["proposed"]',
         "liquidityNum": 1000, "acceptingOrders": True},
        NOW,
    )
    # Du 1er août 00:00 au 14 août 12:00 : 13 jours et demi.
    assert ok and ok.statuses == ["proposed"] and 13.4 < ok.days_late < 13.6, (
        ok.days_late if ok else None
    )


@case
def test_fmt_delay():
    assert R.fmt_delay(-0.5) == "in 12h"
    assert R.fmt_delay(-10) == "in 10d"
    assert R.fmt_delay(0.5) == "12h late"
    assert R.fmt_delay(120) == "120d late"


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
