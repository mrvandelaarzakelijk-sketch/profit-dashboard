# FOMO — smart money + social momentum signaalengine voor memecoins

Detecteert memecoins waar **meerdere onafhankelijke, bewezen traders instappen**, terwijl
**social momentum versnelt**, **marktdata dat bevestigt**, het **risico acceptabel** is en de
**entry nog niet te laat** is — en zegt dat in een score die je kunt uitleggen.

> ⚠️ **Paper trading only.** Er zit geen enkel codepad in dit project dat een transactie kan
> ondertekenen of versturen. Live execution is bewust een apart proces op een aparte host
> (zie `docs/01-architecture.md §7`). Dat is geen TODO — het is het ontwerp.

---

## Wat dit systeem eigenlijk is

**Een AVOID-machine.** Bij memecoins gaat het overgrote deel van alle tokens naar nul. De waarde
zit niet in het vinden van de winnaar, maar in het wegfilteren van de tokens die er óók goed
uitzien op Twitter maar structureel dood zijn. Daarom is risk in dit model geen optelterm maar
een **poort**:

```
MASTER = 100 · Veto · R_soft · Timing · σ(Evidence)
         └─┬─┘  └┬─┘   └──┬──┘  └──┬──┘  └────┬────┘
           │     │        │        │          └── bewijs: smart money, social, markt, liquiditeit
           │     │        │        └───────────── anti-FOMO: hoeveel is er al gebeurd?
           │     │        └────────────────────── risicokorting  [0.30 … 1.00]
           │     └─────────────────────────────── harde veto     {0, 1}
           └───────────────────────────────────── schaal
```

Geen hoeveelheid hype maakt een 0 groter dan 0. En omdat de vorm een logistische regressie is,
vervang je in fase 10 alleen de coëfficiënten door gefitte waarden — geen herbouw.

---

## Snel starten (geen API-keys nodig)

```bash
cd bot
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Alles hieronder draait volledig offline tegen deterministische scenario's:

```bash
python -m fomo.cli config       # configuratie laden en valideren
python -m fomo.cli scenarios    # elk scenario door de engine
python -m fomo.cli explain happy_path
python -m fomo.cli explain late_entry     # zelfde bewijs, 4x te laat
python -m fomo.cli traders      # trader-scoring uitgewerkt
python -m fomo.cli alert happy_path       # de Telegram-alert
python -m fomo.cli backtest     # replay + performance
python -m fomo.cli sweep        # parameter-sweep + walk-forward
```

### Verwachte output van `scenarios`

```
scenario               signal            master  flags
------------------------------------------------------------------------------
happy_path             POTENTIAL_ENTRY    74.62  -
late_entry             AVOID              21.39  -
cluster_illusion       AVOID              60.32  CLUSTER_ILLUSION
bot_swarm              AVOID              12.39  SOCIAL_WITHOUT_BUYERS, SINGLE_WEAK_TRADER, …
honeypot               AVOID               0.00  -
mint_authority_live    AVOID               0.00  -
rug_setup              AVOID               0.00  -
thin_liquidity         AVOID               0.00  VOLUME_WITHOUT_LIQUIDITY
data_disagreement      AVOID               0.00  -
insider_distribution   AVOID              70.36  INSIDER_DISTRIBUTION
```

Drie dingen om op te letten:

- `late_entry` heeft **exact dezelfde** deelscores als `happy_path`. Alleen de timing-multiplier
  verschilt (0,872 → 0,250). Dat is de anti-FOMO-laag die zijn werk doet.
- `cluster_illusion` scoort 60 maar wordt afgewezen: 5 wallets collapsen naar 1,04 onafhankelijke
  traders.
- De vier `0.00`-scores zijn harde veto's. Die zijn niet "heel laag" — ze zijn absorberend.

## Tests

```bash
pytest                       # 170 tests, ~3 s
pytest -m "not contract"     # CI-standaard
pytest tests/property -q     # alleen de invarianten
pytest --cov=fomo --cov-report=term-missing
```

De belangrijkste test in de suite:

```python
def test_master_score_never_rises_with_price_run_up(lower, step):
    """Holding all evidence fixed, a token that has already run further must never
    score higher. If this ever fails, the system has started chasing pumps."""
```

Dat is de machine-controleerbare formulering van "dit systeem mag geen FOMO traden", en
hypothesis controleert hem op honderden gegenereerde gevallen.

---

## Structuur

```
bot/
├── config/            weights.toml + thresholds.toml — élke tunebare waarde
├── docs/              het volledige ontwerp (lees 01 eerst)
├── fomo/
│   ├── domain.py      de gedeelde vocabulaire (frozen pydantic-modellen)
│   ├── config.py      laden + valideren, extra="forbid"
│   ├── scoring/       trader, confirmation (N_eff), smart/social/market/liquidity
│   ├── risk/          veto's + graduele straffen
│   ├── signals/       timing (anti-FOMO), false positives, master score
│   ├── trading/       paper engine + performance analytics
│   ├── backtest/      event-replay, sweep, walk-forward
│   ├── alerts/        Telegram/Discord rendering
│   ├── adapters/      externe wereld + deterministische scenario's
│   └── cli.py
└── tests/             unit · property · integration
```

**De grens die telt is niet "welk onderwerp" maar "raakt dit de buitenwereld?"**
`scoring/`, `risk/`, `signals/` en `trading/` zijn volledig puur — geen netwerk, geen klok,
geen database. Daardoor draaien backtest en live-paper gegarandeerd dezelfde code, en is elke
beslissing reproduceerbaar.

---

## Documentatie

| Document | Inhoud |
|---|---|
| [`docs/01-architecture.md`](docs/01-architecture.md) | Componenten, transactie-classifier, realtime pipeline, latency-budget, stack-keuzes, security |
| [`docs/02-data-model.md`](docs/02-data-model.md) | Databaseschema, indexes, bewaarbeleid, environment variables |
| [`docs/03-apis.md`](docs/03-apis.md) | Providerkeuzes per rol + verificatie-checklist |
| [`docs/04-scoring-and-risk.md`](docs/04-scoring-and-risk.md) | Elke formule met motivering: trader score, social velocity, N_eff, risk, signalen, ML |
| [`docs/05-roadmap-and-testing.md`](docs/05-roadmap-and-testing.md) | MVP-fasen 1–10, backtest-valkuilen, teststrategie |

---

## Status en volgende stap

**Gebouwd en getest (fase 4, 6, 7, 8 van de roadmap):**
trader scoring · cluster-correctie (`N_eff`) · social velocity/diversiteit · market- en
liquiditeitsscore · risk engine · anti-FOMO timing · false-positive-detectie · master score ·
signaaltoestanden · paper trading met fees/slippage/exits · performance analytics ·
backtest-harness met sweep en walk-forward · alert-rendering.

**Nog te bouwen (fase 1, 2, 3, 5, 9):**
de I/O-laag — chain-ingest, social-ingest, marktdata-adapters, LLM-classificatie, database,
dashboard. Die staan bewust achteraan: de beslissingskern is nu bewijsbaar correct, dus alles
wat daarna komt is *plumbing* in plaats van ontwerp.

**De volgende stap is niet meer code — het is data.** Roadmap-fase 1–2 (`docs/05`) eindigt op
één vraag, en die is de poort voor het hele project:

> Doen de tokens die je top-quartiel traders kopen het meetbaar beter dan die van je onderste
> quartiel?

Is dat verschil er niet, dan heeft geen enkel onderdeel hierboven een fundament, en moet je
je watchlist veranderen in plaats van je model. Bouw daarom eerst de wallet-ingest, verzamel
twee weken data, en beantwoord die vraag vóórdat je de dure social-API koopt.
