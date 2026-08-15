# 05 — MVP-roadmap, backtesting, teststrategie

---

## 1. Roadmap

Elke fase is **op zichzelf bruikbaar** en eindigt met een meetbaar resultaat. Als een fase je
niets oplevert, is dat het signaal om te stoppen of om te draaien — niet om door te bouwen naar
de volgende. De duurste fout in dit soort projecten is tien fasen bouwen en pas dan ontdekken
dat fase 2 al geen edge had.

### Fase 1 — Data verzamelen *(je hebt nog geen bot, je hebt een dataset)*
**Functionaliteit:** on-chain transacties van een handmatige watchlist van 20–50 wallets
opslaan. Marktdata per token per minuut. Verder niets.
**Bestanden:** `adapters/chain.py`, `ingest/wallets.py`, `ingest/market.py`, `db/models.py`,
`cli.py::ingest`.
**API's:** blockchain-provider + één marktdata-bron.
**Database:** `wallets`, `tokens`, `transactions`, `token_metrics`.
**Tests:** parser-tests op opgeslagen echte transacties (fixtures), idempotentie-test
(dezelfde webhook 3× → 1 rij).
**Verwachte moeilijkheden:** transacties parsen is rommeliger dan het lijkt — multi-hop routes,
gebundelde transacties, wrapped SOL, meerdere swaps in één transactie. Reken op meer werk hier
dan verwacht. Gemiste webhooks vereisen reconciliatie-polling vanaf dag één.
**Klaar wanneer:** je 2 weken data hebt en het aantal transacties dat de parser weigert < 1% is.

### Fase 2 — Wallet monitoring + trader scoring
**Functionaliteit:** de tx-classifier (`01 §2`), wallet-clustering, trader-statistieken en
`TraderScore`.
**Bestanden:** `ingest/classifier.py`, `scoring/trader.py`, `scoring/clustering.py`.
**Tests:** classifier-tests per categorie, Bayesiaanse-shrinkage-tests, `N_eff`-tests op bekende
clusters.
**Moeilijkheden:** clustering is een graafprobleem met veel valse positieven. Begin met alleen
funding-links (hoge precisie), voeg timing en co-occurrence pas toe als je ze kunt valideren.
**Klaar wanneer — en dit is de belangrijkste poort van het hele project:** je kunt aantonen dat
tokens gekocht door je top-quartiel traders het meetbaar beter doen dan tokens gekocht door je
onderste quartiel. **Als dat verschil er niet is, heeft de rest van het systeem geen fundament.**
Ga dan niet door naar fase 3 — verander eerst je watchlist.

### Fase 3 — Twitter monitoring
**Functionaliteit:** watchlist-gestuurde tweet-collectie, account-profilering, `social_metrics`.
**Bestanden:** `adapters/social.py`, `ingest/social.py`, `scoring/social.py`.
**Moeilijkheden:** ticker-disambiguatie (`04 §3.4`) en API-volumelimieten. Begin met het volgen
van een vaste lijst van 200–500 kwaliteitsaccounts; dat is goedkoop en levert het meeste signaal.
**Klaar wanneer:** je van een historisch token de mention-curve kunt reconstrueren en de
`velocity_z` correleert met wat je met eigen ogen op de chart ziet.

### Fase 4 — Coin scoring
**Functionaliteit:** SMART / SOCIAL / MARKET / LIQ, `N_eff`, timing-multiplier.
**Bestanden:** heel `scoring/`.
**Tests:** property-tests (zie §3), gouden-vector-tests.
**Klaar wanneer:** de scores op historische tokens overeenkomen met je eigen oordeel achteraf.

### Fase 5 — AI-analyse
**Functionaliteit:** tweet-classificatie via LLM met schema-output, caching, kostenplafond,
deterministische fallback.
**Bestanden:** `ai/classifier.py`, `ai/fallback.py`, `ai/prompts.py`.
**Moeilijkheden:** kosten lopen op met volume; cache-hitrate is je belangrijkste metriek.
**Klaar wanneer:** ≥ 85% overeenstemming met 200 handmatig gelabelde tweets, én het systeem
draait volledig door als de LLM uitvalt.

### Fase 6 — Signal engine
**Functionaliteit:** risk engine (veto's + soft), false-positive-flags, master score,
signaaltoestanden, alerts.
**Bestanden:** `risk/`, `signals/`, `alerts/`.
**Klaar wanneer:** je een week aan alerts krijgt en ze *handmatig* beoordeelt als redelijk.
Dit is subjectief en dat mag: je bouwt hier vertrouwen op vóór je er geld aan koppelt.

### Fase 7 — Paper trading
**Functionaliteit:** virtuele posities, fees, slippage, stops, targets, trailing exits, trade-log.
**Bestanden:** `trading/paper.py`, `trading/analytics.py`.
**Moeilijkheden:** realistische slippage simuleren. Wees hier **pessimistisch**; te optimistische
aannames zijn de meest voorkomende reden dat een paper-strategie live geld verliest.
**Klaar wanneer:** 100+ paper-trades met volledige metrics.

### Fase 8 — Backtesting
**Functionaliteit:** historische replay, parameter-sweeps, walk-forward.
**Bestanden:** `backtest/`.
**Moeilijkheden:** look-ahead bias sluipt overal in. Zie §2.
**Klaar wanneer:** dezelfde configuratie in backtest en live-paper vergelijkbare statistieken
geeft. Wijken ze sterk af, dan is je backtest fout — niet je live-run.

### Fase 9 — Dashboard
**Functionaliteit:** live signalen, trader-ranking, token-detail, paper-portfolio, performance,
risk alerts.
**Bestanden:** `api/`, `web/`.
**Klaar wanneer:** je 's ochtends niet meer in de database hoeft te kijken.

### Fase 10 — Live trading *(alleen bij bewezen edge)*
**Toegangseisen — alle vier, geen uitzonderingen:**
1. ≥ 300 paper-trades over ≥ 60 dagen
2. positieve expectancy ná realistische kosten
3. backtest en live-paper komen overeen
4. de execution service draait apart, met eigen key-isolatie en kill switch (`01 §7`)

**Begin met bedragen die je zonder gevoel kunt verliezen.** De sprong van paper naar live brengt
altijd verrassingen: gefaalde transacties, front-running, priority fees, MEV. Reken op een
meetbaar slechter resultaat dan paper, ook als alles klopt.

---

## 2. Backtesting zonder jezelf voor de gek te houden

### Verplichte constructie: point-in-time replay

De backtest mag **nooit** de database bevragen met "wat weten we nu". Hij speelt events af in
chronologische volgorde en de engine ziet alleen wat een `observed_at ≤ t` had:

```python
for event in stream.replay(start, end):        # gesorteerd op observed_at
    world.apply(event)                          # muteert alleen state ≤ t
    decision = signal_engine.evaluate(world.snapshot(event.token_id, at=event.observed_at))
    portfolio.on_decision(decision, at=event.observed_at)
```

`world.snapshot()` heeft geen toegang tot toekomstige rijen. Dat is een structurele garantie,
geen discipline-kwestie — en dat onderscheid is precies waarom dit werkt.

### De acht manieren waarop een crypto-backtest liegt

1. **`observed_at` vs `event_at`.** Data die je pas 8 s later had, mag je op t=0 niet gebruiken.
2. **Survivorship bias.** Als je backtest alleen tokens bevat die vandaag nog bestaan, test je
   een strategie op uitsluitend overlevenden. Bij memecoins is dat een fout van meerdere ordes
   van grootte — de meeste tokens verdwijnen.
3. **Optimistische fills.** Aannemen dat je op de laatste prijs koopt, is fictie. Simuleer
   prijsimpact op de werkelijke poolgrootte op dat moment (`04 §5.3`).
4. **Vergeten kosten.** DEX-fee, priority fee, gefaalde transacties (die kosten óók geld), en
   het bied-laat-verschil.
5. **Herzien social data.** Likes en retweets van een tweet groeien ná het posten. Gebruik de
   telling zoals die op het beslismoment was, niet de eindstand — anders geef je jezelf
   informatie uit de toekomst.
6. **Parameter-overfitting.** 200 combinaties proberen en de beste kiezen levert gegarandeerd een
   mooie curve en geen edge. Gebruik walk-forward en rapporteer de *out-of-sample* resultaten.
7. **Retroactieve watchlist.** Traders selecteren op basis van hun performance ín de testperiode
   is de subtielste en dodelijkste vorm van look-ahead. De watchlist op tijdstip t mag alleen
   gebaseerd zijn op data vóór t.
8. **Ontbrekende liquiditeitshistorie.** Als je de poolgrootte van toen niet hebt, kun je
   slippage niet simuleren en is elk resultaat fantasie.

### Walk-forward

```
|--- train 60d ---|- test 14d -|
        |--- train 60d ---|- test 14d -|
                |--- train 60d ---|- test 14d -|
```

Optimaliseer op train, rapporteer uitsluitend op test, plak de testperiodes aan elkaar tot één
out-of-sample curve. Alleen die curve telt.

### Parameter-sweep — te rapporteren

Voor elke combinatie: `n_trades` · win rate · expectancy · profit factor · max drawdown ·
Sharpe · **en de spreiding over de walk-forward-vensters**.

```
                                    n     win%   exp%   PF    maxDD   Sharpe
Trader>80 Social>70 Master>85     412    31.2   +4.1   1.38   −22%    1.11
Trader>90 Social>80 Master>90      87    38.6   +6.9   1.71   −18%    1.34
```

Kies **niet** klakkeloos de tweede. 87 trades is weinig; het verschil kan ruis zijn. De juiste
vraag is of het verschil overleeft in elk walk-forward-venster afzonderlijk. Consistentie over
vensters is een betrouwbaarder signaal dan de hoogste geaggregeerde metriek — die laatste is
precies wat overfitting produceert.

---

## 3. Teststrategie

### Laag 1 — Unit (snel, deterministisch, geen I/O)
De hele beslissingskern is puur, dus volledig unit-testbaar:
- `math_utils`: saturatie, logistiek, logschaal — randgevallen (0, negatief, ∞)
- `scoring/trader`: shrinkage-gedrag, straffen
- `scoring/confirmation`: `N_eff` bij ρ=0, ρ=1, gemengd
- `scoring/social`: entropie/diversiteit met de tabel uit `04 §3.3` als fixture
- `risk`: elke regel met een waarde net onder en net boven de drempel
- `signals`: elke toestandsovergang
- `trading/paper`: fees, slippage, stops, gedeeltelijke exits
- `trading/analytics`: metrics tegen handmatig doorgerekende voorbeelden

### Laag 2 — Property-based (`hypothesis`)
Invarianten die voor *elke* input moeten gelden. Deze vangen wat voorbeeld-tests missen:

| Invariant |
|---|
| Alle scores liggen in `[0, 100]` bij elke input |
| Een actief veto ⇒ `MASTER == 0` **en** `signal == AVOID` — voor elke andere input |
| `MASTER` is monotoon niet-dalend in `SMART`, ceteris paribus |
| `MASTER` is monotoon niet-**stijgend** in prijsstijging sinds smart entry (anti-FOMO-garantie) |
| `N_eff ≤ n_traders` altijd, en `N_eff ≥ 1` bij ≥ 1 trader |
| Paper-portfolio: som van posities + cash == equity, tot op afrondingstolerantie |
| Geen positie overschrijdt `max_position_pct` |
| Backtest is deterministisch: dezelfde seed + data ⇒ identieke trades |

De monotoniteitstests zijn de belangrijkste. Ze zijn de machine-controleerbare formulering van
"het systeem mag geen FOMO traden".

### Laag 3 — Integratie (met fakes, geen netwerk)
`adapters/fakes.py` levert deterministische nep-providers met scriptbare scenario's:
`happy_path`, `rug_pull`, `honeypot`, `bot_swarm`, `cluster_illusion`, `late_entry`,
`data_disagreement`. Elk scenario is een test die de verwachte eindtoestand afdwingt.

### Laag 4 — Contract-tests tegen de echte API's (`@pytest.mark.contract`)
Draaien apart en niet in CI-op-elke-commit. Ze controleren één ding: **is de vorm van de respons
nog wat we denken?** Providers veranderen velden zonder aankondiging; dit is de test die je
waarschuwt vóórdat productie stil kapotgaat.

### Laag 5 — Replay-regressie
Een vaste opgenomen dataset van 48 uur. Elke wijziging draait hem opnieuw. Verandert het aantal
signalen of de PnL, dan moet dat een *bewuste* verandering zijn. Dit is je vangnet tegen
onbedoelde gedragsdrift bij refactors.

### Wat we bewust **niet** testen
- Exacte score-getallen als "gouden waarden" — die veranderen zodra je gewichten tunet, en dan
  test je je config in plaats van je code. Test *gedrag en relaties*, niet decimalen.
- Externe API's in CI. Traag, flaky, en het test hun uptime in plaats van jouw code.

### Commando's

```bash
pytest                              # unit + property + integratie
pytest -m "not contract"            # standaard in CI
pytest -m contract                  # handmatig, vereist echte API-keys
pytest --cov=fomo --cov-report=term-missing
pytest tests/property -q            # alleen de invarianten
```

**Dekkingsdoel:** ≥ 90% op `scoring/`, `risk/`, `signals/`, `trading/`. Voor `adapters/` en
`ingest/` is een lager percentage prima — daar test je met contract-tests, niet met mocks van
je eigen aannames.
