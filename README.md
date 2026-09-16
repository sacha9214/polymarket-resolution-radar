# Resolution Radar — Polymarket

Surveille la **fin de vie** des marchés Polymarket : la phase entre l'événement et
le versement, que presque personne ne regarde.

Un marché de prédiction ne meurt pas proprement. Quelqu'un propose un résultat à
l'oracle UMA, une fenêtre de contestation s'ouvre, parfois quelqu'un conteste.
Pendant tout ce temps le marché continue de se traiter et le capital reste bloqué.

C'est le 3<sup>e</sup> bot de la série, et il répond à une question que les deux
autres ne posent pas :

| Bot | Question |
|---|---|
| Overlap | Qui parie, et est-ce quelqu'un de bon ? |
| Coherence | Est-ce que les prix se contredisent entre eux ? |
| **Resolution Radar** | **La résolution se passe-t-elle mal ?** |

---

## Ce qu'il détecte

### 🔴 Dispute — le signal qui compte
Une résolution proposée a été contestée, caution engagée. Mesuré sur l'historique :
**0,4 % des résolutions**. C'est rare, donc informatif. Le litige part au vote UMA :
des jours de délai, et une issue qui peut encore basculer.

Cas réels trouvés au premier scan : *« Will Hamas agree to disarm by December 31? »*
(101 k$ de liquidité, contesté 2×, à 139 jours de l'échéance) et *« Will Venezuela
recognize Israel by December 31? »* (contesté 2×, prix effondré de 64 points).

### 🟠 Proposal gap — proposé mais pas intégré
Une résolution est sur la table mais le prix n'a pas convergé vers la certitude.
Soit personne n'a vu, soit le marché n'est pas d'accord — ce qui précède souvent
une contestation.

Attention : **24 propositions sur 25 sont déjà à 0,9995**. L'idée de « voir la
vérité avant le marché » ne marche pas ; ce signal est rare par nature.

### 🔵 Shock — mouvement adossé à du volume réel
Un gros mouvement de prix, **loin de l'échéance**, avec une rotation élevée.

Le filtre décisif est le **taux de rotation** (volume 24 h ÷ liquidité). Le seul
choc informatif du premier scan tournait à 5,1× son carnet ; les matchs de
baseball dont la cote dérive étaient tous sous 0,6×. Sans ce filtre : 26 fausses
alertes par scan, toutes des matchs esport qui venaient simplement de finir.

### 🟡 Stuck — capital immobilisé
Échu depuis longtemps, toujours ouvert, sans même une proposition. Des cas à
**287 jours de retard** existent. Au dernier scan : **3,3 M$ de liquidité** répartis
sur **191 marchés** sans date de règlement.

---

## Ce que le bot ne peut PAS dire

L'API Polymarket expose le **statut** de la résolution (`umaResolutionStatuses`)
mais **jamais le résultat proposé**. Le bot décrit donc la situation, jamais une
direction. Il ne dira pas « la proposition dit NON » — cette information n'existe
pas dans les données, et l'inventer serait le plus sûr moyen de faire perdre de
l'argent à quelqu'un.

---

## Installation

```bash
cd polymarket-resolution-radar
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
```

Jeton du bot Discord dans `token.txt` (une ligne) ou dans `DISCORD_BOT_TOKEN`.
Optionnel : `DISCORD_GUILD_ID` pour un enregistrement instantané des commandes.

```bash
./start_mac_linux.sh
```

## Sans Discord

Le moteur est autonome — c'est ce qui permet de vérifier les chiffres sans lancer
le bot :

```bash
./venv/bin/python resolution.py                  # tous les signaux
./venv/bin/python resolution.py --only=dispute   # un seul type
./venv/bin/python selftest.py                    # 24 tests, marchés synthétiques
```

Un scan couvre **~4 000 marchés ouverts en 1 seconde**, via deux passes de tri
complémentaires (voir plus bas).

## Commandes Discord

| Commande | Effet |
|---|---|
| `/setup` | **Crée toute la structure de salons** et branche tout (admin) |
| `/radar [limit]` | Situations en cours, les plus graves d'abord |
| `/stuck [limit]` | Marchés échus, le plus de capital bloqué d'abord |
| `/watch-live` | Abonne le salon aux litiges, écarts et chocs |
| `/watch-stuck` | Abonne le salon aux marchés coincés |
| `/unwatch` | Coupe les alertes du salon |
| `/board` | Installe le tableau vivant, réécrit en place |
| `/guide` | Poste la note « comment lire ce salon » |
| `/preview` | Poste une alerte d'exemple (vérifie rendu et permissions) |
| `/status` | Réglages, seuils et dernier scan |

`/setup` crée la catégorie **POLYMARKET RESOLUTION** : `radar-guide`,
`radar-board`, `dispute-alerts`, `stuck-markets`, `radar-discussion`. Les noms
sont préfixés et la recherche de salons existants est limitée à cette catégorie —
sinon ce bot irait écrire dans les salons des deux autres.

**Une photo de référence est prise à l'abonnement.** Il existe en permanence
~35 situations ouvertes : sans ça, installer le bot déclencherait 35 alertes d'un
coup dont aucune ne serait une nouvelle.

---

## Détails d'implémentation qui comptent

**Deux passes de tri, pas une.** L'API plafonne la pagination vers 2300 marchés
par tri. On balaie donc par **échéance croissante** (les marchés échus, que le tri
par volume ne remonte jamais puisqu'ils ne s'échangent plus) *et* par **volume**
(les gros marchés contestés alors que l'échéance est encore lointaine — le cas
Venezuela, échu en décembre et déjà contesté deux fois). Chacune seule rate la
moitié du signal.

**`limit` est plafonné à 100** par l'API quoi qu'on demande. Coder « je demande
500 et je m'arrête quand la page en renvoie moins » fait quitter la pagination
dès la première page : 100 marchés au lieu de 4 000. Piège rencontré.

**Deux paliers.** Tout ce qui est vrai n'est pas une nouvelle. Un marché échu de
plus ne mérite pas d'alerte ; 3,3 M$ immobilisés au total, si. Les signaux
marqués `digest` n'apparaissent que dans le tableau et `/radar`.

## Réglages

Tout est en haut de `resolution.py` :

| Réglage | Défaut | Rôle |
|---|---|---|
| `OVERDUE_DAYS` | 3 | En deçà, c'est la fenêtre UMA normale |
| `OVERDUE_ALERT_LIQUIDITY` | 25 k$ | Au-dessus : alerte ; en dessous : tableau |
| `SHOCK_MIN` | 25 pts | Amplitude minimale du mouvement |
| `SHOCK_MIN_TURNOVER` | 1,0× | **Le filtre anti-bruit décisif** |
| `PROPOSAL_GAP_MIN` | 2 % | Écart à la certitude |
| `SOON_MIN_LIQUIDITY` | 100 k$ | Seuil des résolutions imminentes |

Les litiges alertent **toujours**, quelle que soit la taille : ils sont trop rares
pour être filtrés.

---

## Limites

- **Le bot ne trade rien** et ne recommande rien. Il décrit.
- **Le résultat proposé n'est pas public** (voir plus haut). Vérifie toujours le
  marché avant d'agir.
- **Un litige peut se résoudre dans les deux sens.** Une contestation n'est pas
  un signal directionnel, c'est un signal d'incertitude et de délai.
- **Le silence est l'état normal.** 0,4 % de contestations : si ce salon parle
  tous les jours, c'est que les seuils sont mal réglés.

## Licence

[MIT](LICENSE)
