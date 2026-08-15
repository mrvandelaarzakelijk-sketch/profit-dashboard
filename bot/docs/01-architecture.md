# 01 — Systeemarchitectuur

> **Scope-aanname:** memecoins, primair Solana (pump.fun / bonk.fun launchpads → Raydium/Meteora
> migratie). Tokens zijn **minuten tot dagen** oud, niet maanden. Dit is de belangrijkste
> ontwerpbeperking en hij raakt élk onderdeel: er is geen prijshistorie om op te backtesten
> vóór minuut 0, "holders" verandert per seconde, en de base rate van succes is extreem laag.

---

## 0. Het eerlijke uitgangspunt

Voordat we architectuur tekenen, moeten twee getallen vaststaan, want ze bepalen het hele ontwerp:

1. **De base rate is brutaal.** Van de tokens die op een launchpad live gaan haalt een zeer klein
   deel ooit betekenisvolle liquiditeit. Van wat migreert, gaat het overgrote deel alsnog naar
   ~nul. Elke strategie die "koop als score hoog" luidt zonder harde filters verliest geld.
2. **Daarom is dit systeem primair een AVOID-machine.** De waarde zit niet in het vinden van de
   winnaar; die vind je toch pas met een handvol kandidaten per dag. De waarde zit in het
   wegfilteren van de 95%+ die er *ook* goed uitziet op Twitter maar structureel dood is.

Concreet ontwerpgevolg: **risk is geen optelterm, risk is een poort.** Zie §3.

---

## 1. Componenten

Elk onderdeel hieronder: wat het doet · welke data · welke technologie · hoe het praat met de rest.

### A. Data Ingestion (`fomo/adapters/`)
**Doet:** één uniforme laag boven alle externe bronnen. Elke provider krijgt een adapter die
een provider-specifiek antwoord vertaalt naar ons interne domeinmodel.
**Data in:** HTTP/WebSocket/gRPC van blockchain-, markt- en social-providers.
**Data uit:** genormaliseerde events op de interne bus.
**Tech:** `httpx.AsyncClient` (async, HTTP/2, moderne opvolger van `requests`), `websockets`,
`tenacity` voor retry/backoff. Elke adapter implementeert een `Protocol` uit `adapters/base.py`.
**Waarom een adapterlaag:** providers in deze markt gaan failliet, veranderen prijzen of
deprecaten endpoints. De adapterlaag is de enige plek die dat mag voelen. Alles daarboven werkt
op ons eigen domeinmodel.

> Regel: **geen enkel ander component importeert ooit een provider-SDK direct.**

### B. Wallet / Trader Monitoring (`fomo/ingest/wallets.py`)
**Doet:** near-realtime detecteren wat gevolgde wallets doen, en die ruwe transacties
classificeren naar betekenisvolle acties.
**Data:** wallet addresses (watchlist), transactiestromen, token balances, SOL/USD prijs.
**Tech:** webhook-push (provider stuurt naar onze endpoint) als primair pad, WebSocket/gRPC-stream
als lage-latency pad, REST polling als fallback/reconciliatie.
**Kern:** de classifier in §2 hieronder. Een ruwe swap is géén signaal.

### C. Twitter/X Monitoring (`fomo/ingest/social.py`)
**Doet:** mentions verzamelen per token (ticker, contract address, projectnaam) en per account.
**Data:** tweets, auteur-metadata, engagement, timestamps.
**Tech:** X API v2 (filtered stream voor push, recent search voor backfill). Zie `03-apis.md` —
dit is het duurste en meest gelimiteerde onderdeel van het hele systeem, en dat stuurt het ontwerp:
we monitoren **niet alles**, we monitoren een *watchlist die door on-chain events wordt gevuld*.

### D. Market Data (`fomo/ingest/market.py`)
**Doet:** prijs, volume, liquiditeit, market cap, FDV, pool-samenstelling, OHLCV.
**Data:** per token per pool, op 1m-resolutie voor jonge tokens.
**Tech:** DEX-aggregator API's + directe pool-reads als kruiscontrole.

### E. AI / NLP Analysis (`fomo/ai/`)
**Doet:** ongestructureerde tekst → gestructureerde velden. Tweet-classificatie, narratief-
detectie, account-kwaliteit, anomalie-uitleg.
**Tech:** Claude via de Anthropic API met **tool-use / structured output**, zodat de output
schema-gevalideerd is en niet geparsed hoeft te worden uit vrije tekst. Batched, gecached op
tweet-hash. Zie `04-scoring-and-risk.md §7` voor waarom AI hier wél en elders níet.
**Harde regel:** de AI produceert uitsluitend `TweetAnalysis`-objecten. De AI heeft geen toegang
tot de trading-engine, geen tools die orders plaatsen, en zijn output gaat door dezelfde
numerieke scoring als alle andere features.

### F. Signal Engine (`fomo/signals/`)
**Doet:** alle deelscores combineren tot één master score + één signaaltoestand, met een
volledig traceerbare uitleg.
**Data:** alle subscores + de token-snapshot.
**Tech:** puur deterministische Python, geen I/O. Dit is bewust een **pure functie**:
`(TokenSnapshot, Weights) -> SignalDecision`. Daardoor is hij triviaal te testen, te backtesten
en te reproduceren.

### G. Risk Engine (`fomo/risk/`)
**Doet:** hard vetorecht. Draait vóór en ná de scoring.
**Tech:** regelgebaseerd, ook puur. Elke regel is een los object met een id, een drempel uit
config, en een uitleg-string.

### H. Paper Trading Engine (`fomo/trading/paper.py`)
**Doet:** signalen omzetten in virtuele posities, met realistische fees, slippage en
prijsimpact. Beheert stops, targets, trailing exits.
**Tech:** deterministische state machine, event-driven. Dezelfde code draait live-paper én in
de backtest — dat is essentieel, anders backtest je iets anders dan je draait.

### I. Database (`fomo/db/`)
**Doet:** append-only feiten (transacties, tweets, prijzen) + afgeleide state (scores, posities).
**Tech:** PostgreSQL 16 + TimescaleDB voor de tijdreeksen. SQLAlchemy 2.0 (async) + Alembic.
SQLite in tests. Zie `02-data-model.md`.

### J. Alert System (`fomo/alerts/`)
**Doet:** signaal → Telegram/Discord bericht, met dedupe en rate limiting.
**Tech:** Telegram Bot API (`sendMessage`, HTML parse mode) en Discord webhooks. Beide zijn
simpele HTTP-POSTs; geen zware SDK nodig.

### K. Performance Analytics (`fomo/trading/analytics.py`)
**Doet:** win rate, expectancy, profit factor, Sharpe/Sortino, max drawdown, holding time —
en, belangrijker, **attributie**: welke signaalcategorie verdient geld en welke niet.
**Tech:** pure functies over de trade-log.

### L. Dashboard (`web/`)
**Doet:** live signalen, trader-ranking, token-detailpagina's, paper-portfolio, risk alerts.
**Tech:** FastAPI levert JSON + Server-Sent Events; frontend is Next.js (React) of — voor de
MVP — één statische pagina in de stijl van de bestaande `index.html`. SSE boven WebSockets
omdat de datastroom eenrichtingsverkeer is en SSE door elke proxy heen komt.

---

## 2. De transactie-classifier (het belangrijkste stuk van B)

Dit is waar de meeste "smart money bots" stuk gaan: ze behandelen elke swap als een koopsignaal.
Wij classificeren elke wallet-transactie eerst in precies één categorie:

| Categorie | Detectie | Signaalwaarde |
|---|---|---|
| `OPEN` — nieuwe positie | wallet had 0 balance in dit token vóór tx | **hoog** |
| `ADD` — positie vergroot | balance > 0, neemt toe | hoog, iets minder dan OPEN |
| `TRIM` / `CLOSE` — verkoop | balance neemt af / naar ~0 | exit-signaal |
| `SELF_TRANSFER` — eigen wallets | tegenpartij zit in dezelfde wallet-cluster, geen DEX-programma | **nul** — negeren |
| `MM_BOT` — market maker / arbitrage | hoge tx-frequentie, symmetrische buy/sell binnen seconden, vaste bedragen, interactie met bekende bot-programma's | **nul** — negeren |
| `AIRDROP_DUST` | ontvangen zonder swap, waarde onder drempel | **nul** — negeren |

De `SELF_TRANSFER`-detectie leunt op de wallet-cluster-graaf uit `04-scoring-and-risk.md §4`.
De `MM_BOT`-detectie is een lopend profiel per wallet, geen per-transactie-beslissing.

**Waarom dit zo vroeg in de pipeline zit:** een genegeerde transactie kost 0 verdere API-calls.
Alles wat we hier weggooien, hoeven we niet te verrijken met markt- en socialdata — en die
verrijking is het dure deel. De classifier is dus tegelijk het correctheidsfilter én het
kostenfilter.

---

## 3. Waarom risk een poort is en geen optelterm

De naïeve aanpak:

```
MASTER = 0.3·smart + 0.25·social + 0.2·market + 0.15·liq + 0.1·(100−risk)
```

Deze formule is **kapot** voor memecoins, om één reden: hij is compenserend. Een token met een
actieve mint authority (de dev kan oneindig bijdrukken) krijgt `risk = 100`, verliest 10 punten,
en kan dat ruimschoots goedmaken met Twitter-hype. Het model zegt dan "koop" over een token dat
per constructie naar nul gaat.

Wij gebruiken daarom een **gepoorte, multiplicatieve** structuur:

```
MASTER = 100 · Veto · R_soft · Timing · σ(evidence)
```

- `Veto ∈ {0, 1}` — harde ja/nee. Honeypot, mint authority, LP niet gelocked → **0**, klaar.
- `R_soft ∈ [0.30, 1.00]` — graduele risicokorting voor wat wel erg maar niet fataal is.
- `Timing ∈ [0.15, 1.00]` — anti-FOMO. Hoeveel van de beweging is al gebeurd?
- `σ(evidence)` — logistische combinatie van het bewijs (smart money, social, markt).

Twee eigenschappen die dit correct maakt:
1. **Niets compenseert een veto.** Geen enkele hoeveelheid hype maakt een 0 groter dan 0.
2. **De vorm is een logistische regressie.** De handmatige gewichten van vandaag zijn de *prior*;
   in fase 10 vervangen we ze door coëfficiënten die we op echte uitkomsten fitten — zónder de
   architectuur te veranderen. Het scoremodel en het latere ML-model zijn dezelfde functie.

Volledige uitwerking van elke term in `04-scoring-and-risk.md`.

---

## 4. Realtime pipeline & latency-budget

```
                    ┌──────────────── WATCHLIST-GESTUURD ────────────────┐
                    │                                                    │
  [Solana chain]    │   [X / Twitter]        [DEX / markt]               │
        │           │         │                    │                     │
        ▼           │         ▼                    ▼                     │
  webhook/gRPC      │   filtered stream       REST + pool reads          │
        │           │         │                    │                     │
        ▼           │         ▼                    ▼                     │
 ┌──────────────────┴─────────────────────────────────────────┐
 │  ADAPTERS  → normalisatie → Redis Streams (de interne bus)  │
 └──────────────────┬─────────────────────────────────────────┘
                    ▼
        ┌───────────────────────┐
        │ tx-classifier (§2)    │  ← 90% van het volume sterft hier
        └───────────┬───────────┘
                    ▼  (alleen OPEN / ADD / CLOSE)
        ┌───────────────────────┐
        │ token identification  │  → is dit token al bekend? watchlist-add
        └───────────┬───────────┘
                    ▼
        ┌───────────────────────┐
        │ RISK ENGINE — vetofase│  ← goedkoopste killer, dus zo vroeg mogelijk
        └───────────┬───────────┘
                    ▼  (overleefden de veto's)
   ┌────────────────┴────────────────┐
   ▼                ▼                ▼        (parallel, asyncio.gather)
 trader-score   market-data      social-fetch
   │                │                │
   │                │                ▼
   │                │          AI-classificatie (batched)
   └────────────────┼────────────────┘
                    ▼
        ┌───────────────────────┐
        │ confirmation (N_eff)  │  cluster-gecorrigeerd aantal traders
        └───────────┬───────────┘
                    ▼
        ┌───────────────────────┐
        │ MASTER SCORE + TIMING │
        └───────────┬───────────┘
                    ▼
        ┌───────────────────────┐
        │ SIGNAL ENGINE         │ → WATCH / EARLY / POTENTIAL / STRONG / AVOID
        └───────────┬───────────┘
                    ├──────────► paper trading engine
                    ├──────────► Telegram / Discord
                    └──────────► PostgreSQL (feiten + beslissing + uitleg)
```

### Waar de vertraging ontstaat — met budget per stap

| Stap | Realistische orde | Opmerking |
|---|---|---|
| Blok → provider heeft het | ~0,5–2 s | Buiten onze controle. Bepaalt de vloer. |
| Provider → onze webhook | ~50–300 ms | Push. Polling maakt dit 10–30× erger. |
| tx-classificatie | < 5 ms | Puur in-memory, wallet-profielen gecached. |
| Risk veto (on-chain metadata) | 100–400 ms | **Cachen per mint.** Mint/freeze authority verandert zelden. |
| Market data fetch | 100–500 ms | Parallel met social. |
| **Social fetch + AI-classificatie** | **1–10 s** | **De grootste bron van latency.** |
| Scoring + signaal | < 5 ms | Pure functies. |
| Alert versturen | 100–500 ms | Fire-and-forget, buiten het kritieke pad. |

**De ontwerpbeslissing die hieruit volgt:** social mag het smart-money-signaal niet ophouden.
Daarom draait de pipeline **tweefasig**:

- **Fase 1 (< 1 s):** smart money + risk + markt → dit kan al `EARLY_WATCH` opleveren.
- **Fase 2 (+ 2–10 s):** social arriveert → score wordt *herzien*, kan opwaarderen naar
  `POTENTIAL_ENTRY` / `STRONG_ENTRY`.

Een signaal is dus geen eenmalige gebeurtenis maar een **stateful object dat evolueert**. Dat is
ook precies hoe een mens dit doet: je ziet de whale-buy, je kijkt dán pas naar Twitter.

**Verdere latency-optimalisaties**
- Alle on-chain metadata die onveranderlijk is (decimals, creator, launch-timestamp) permanent cachen.
- Mint/freeze authority cachen met korte TTL — ze kunnen revoked worden, maar niet terug.
- Social baselines per token vooraf berekenen (rolling EWMA in Redis), niet on-demand.
- AI-calls batchen per token, niet per tweet, en cachen op tekst-hash (retweets zijn identiek).
- Redis Streams met consumer groups → horizontaal schaalbaar zonder de code te veranderen.

---

## 5. Technologiekeuze en de motivering

| Keuze | Waarom | Waarom niet het alternatief |
|---|---|---|
| **Python 3.11+** | Scoring, backtesting en later ML zijn het hart. `numpy`/`pandas`/`scikit-learn`/`statsmodels` hebben geen serieuze tegenhanger in JS. `asyncio` is ruim voldoende voor I/O-bound streaming. | Node/TS is sneller voor pure event-throughput, maar dan schrijf je het kwantitatieve deel alsnog in Python — twee talen voor één systeem is niet gratis. |
| **FastAPI** | Async, pydantic-native (hetzelfde model valideert je API én je domein), automatische OpenAPI. | Flask is sync; Django is te zwaar voor een JSON-API zonder ORM-admin-behoefte. |
| **pydantic v2** | Runtime-validatie op de systeemgrens. Bij externe API's die stilletjes van vorm veranderen wil je een luide fout, geen `None` die drie lagen dieper explodeert. | Dataclasses valideren niet. |
| **PostgreSQL + TimescaleDB** | Eén database voor relationeel (traders, tokens) én tijdreeksen (prijzen, mentions). Hypertables + compressie halen tijdreeksen weg waar een gewone tabel omvalt. | Een aparte tijdreeksdatabase erbij = tweede operationeel systeem voor een MVP. Doe dat pas als Postgres écht knelt. |
| **Redis Streams** | Interne bus met consumer groups, replay en backpressure. Ook de cache- en rate-limit-laag. Eén dependency, drie functies. | Kafka is het juiste antwoord bij >100k events/s en meerdere teams. Hier is het operationele overhead zonder opbrengst. |
| **SSE (geen WebSockets)** | Dashboard-updates zijn eenrichting. SSE herverbindt automatisch en gaat door elke proxy. | WebSockets zijn bidirectioneel — functionaliteit die we niet gebruiken maar wel moeten onderhouden. |
| **httpx** | Async, HTTP/2, connection pooling, moderne API. | `requests` is sync-only en blokkeert de event loop. |
| **Next.js/React** | Alleen als het dashboard interactief moet worden. Voor de MVP: één statische pagina. | Een SPA-build-pipeline vóór er data is, is werk zonder opbrengst. |

**Wat we bewust NIET bouwen in de MVP:** Kubernetes, microservices, message-broker-clusters,
een eigen indexer, GraphQL. Één Python-proces met `asyncio` + Postgres + Redis draait dit
prima tot ver voorbij het punt waarop je weet of de strategie überhaupt werkt. Als hij niet
werkt, heb je die infra voor niets gebouwd.

---

## 6. Projectstructuur

```
bot/
├── pyproject.toml
├── .env.example
├── config/
│   ├── weights.toml            # alle scoring-gewichten, tunebaar zonder code
│   ├── thresholds.toml         # risk-drempels en signaal-cutoffs
│   └── traders.example.toml    # de watchlist
├── docs/                       # deze map
├── fomo/
│   ├── config.py               # laden + valideren van bovenstaande
│   ├── domain.py               # alle pydantic-domeinmodellen
│   ├── adapters/               # A — externe wereld, enige plek met provider-kennis
│   │   ├── base.py             #     Protocols
│   │   └── fakes.py            #     deterministische fakes voor tests/backtest
│   ├── ingest/                 # B, C, D — streams → bus
│   ├── ai/                     # E — LLM-classificatie + deterministische fallback
│   ├── scoring/                # trader, social, market, confirmation, master
│   ├── risk/                   # G — regels + engine
│   ├── signals/                # F — timing, false positives, entry/exit-beslissing
│   ├── trading/                # H, K — paper engine + analytics
│   ├── backtest/               # event-replay harness
│   ├── db/                     # I — models, migraties, repositories
│   ├── alerts/                 # J — Telegram/Discord
│   ├── api/                    # L — FastAPI routes + SSE
│   └── cli.py                  # entrypoints
├── tests/
│   ├── unit/
│   ├── property/               # invarianten (zie 05)
│   └── integration/
└── web/                        # L — frontend
```

**Waarom deze indeling:** de grens die ertoe doet is niet "welk onderwerp" maar **"raakt dit de
buitenwereld?"**. `scoring/`, `risk/`, `signals/`, `trading/` zijn volledig puur — geen netwerk,
geen klok, geen database. Alleen `adapters/`, `ingest/` en `db/` doen I/O. Die scheiding maakt
de hele beslissingslogica deterministisch testbaar en zorgt dat backtest en live-paper
gegarandeerd dezelfde code draaien.

---

## 7. Security

De harde eis: **de analyse-engine mag nooit een private key kunnen aanraken.**

```
┌───────────────────────────────┐        ┌──────────────────────────────┐
│  ANALYSE-ENGINE               │        │  EXECUTION SERVICE           │
│  (dit hele project)           │        │  (fase 10, apart proces)     │
│                               │        │                              │
│  • read-only RPC keys         │ ─────► │  • ontvangt ONDERTEKENDE     │
│  • social API keys            │ intent │    intents via mTLS          │
│  • LLM API key                │        │  • eigen host / container    │
│  • GEEN private keys          │        │  • private key in KMS/HSM    │
│  • GEEN uitgaande tx-rechten  │        │  • eigen kill switch         │
└───────────────────────────────┘        │  • harde daglimieten         │
                                         └──────────────────────────────┘
```

- **Private keys** staan nooit in `.env`, nooit in de repo, nooit in logs. In productie:
  cloud KMS of een hardware signer. Lokaal: een encrypted keystore met een passphrase die
  niet op schijf staat.
- **Intents zijn ondertekend en hebben een TTL.** De execution service accepteert alleen een
  intent die minder dan N seconden oud is, met een bedrag onder de harde cap, voor een token
  dat de risk engine heeft goedgekeurd — die service controleert dat **zelf opnieuw**. Vertrouw
  je eigen upstream niet.
- **Key-isolatie per scope:** de social-key kan geen on-chain calls doen; de RPC-key is read-only;
  de LLM-key heeft een eigen spend-limiet. Compromittering van één sleutel is dan begrensd.
- **Audit trail:** elke beslissing wordt append-only opgeslagen mét de volledige feature-vector
  en de gebruikte config-versie. Zonder dat kun je achteraf niet reconstrueren *waarom* de bot
  iets deed — en dan kun je het model ook niet verbeteren.
- **Rate limiting** op elke uitgaande provider (token bucket per key) én op de inkomende API.
- **Authenticatie** op het dashboard vanaf dag één. Een read-only dashboard dat je posities en
  watchlist lekt is een gratis front-run-kans voor een ander.
- **Logging:** secrets worden geredigeerd door een log-filter, niet door discipline.

---

## 8. Hoe componenten communiceren — samengevat

- **Buiten → binnen:** uitsluitend via `adapters/`, altijd genormaliseerd naar `domain.py`-types.
- **Tussen ingest en verwerking:** Redis Streams (async, met replay).
- **Binnen de beslissingskern:** gewone functieaanroepen, geen bus. Het is één pure pipeline;
  daar een message broker tussen zetten voegt latency en debugging-pijn toe zonder winst.
- **Verwerking → buiten:** fire-and-forget taken (alerts, persistence) buiten het kritieke pad,
  zodat een trage Telegram-API nooit een signaal vertraagt.
