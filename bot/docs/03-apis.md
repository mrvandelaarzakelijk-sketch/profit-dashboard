# 03 — API's en dataproviders

> ## ⚠️ Lees dit eerst
>
> In deze sessie is web search geblokkeerd, dus ik heb prijzen, rate limits en endpoint-vormen
> **niet live kunnen verifiëren**. Ik ga ze niet verzinnen — verzonnen API-specs zijn erger dan
> geen specs, want je bouwt eromheen en komt er pas achter tijdens integratie.
>
> Wat hieronder staat is: **wat elke provider doet, welke rol hij in dit systeem speelt, en
> waarom je hem wel of niet kiest.** Elk veld gemarkeerd met 🔍 moet je verifiëren op de
> officiële pricing-/docs-pagina vóórdat je een abonnement neemt of code schrijft.
>
> Het `adapters/`-ontwerp uit `01 §1.A` bestaat precies hiervoor: providerkeuzes zijn omkeerbaar,
> zolang je ze achter een interface houdt.

---

## Verificatie-checklist per provider

Loop deze langs voordat je integreert. Dit zijn de vragen die achteraf duur blijken:

1. Prijs per maand en **wat de eenheid is** (calls? credits? compute units? Eén "credit" is
   zelden één call, en zware endpoints kosten vaak 10–100×.)
2. Rate limit: requests/sec én requests/maand. Wat gebeurt er bij overschrijding — 429, of
   stilzwijgend afgeknepen data?
3. Ondersteunt hij **push** (webhook/stream) of alleen polling? Dit is het verschil tussen
   200 ms en 20 s latency, en daarmee tussen bruikbaar en niet.
4. Dekt hij **pre-migratie launchpad-tokens**? Veel "Solana token API's" indexeren pas ná
   migratie naar Raydium — precies te laat voor deze strategie.
5. Historische data: hoe ver terug, welke resolutie? Zonder 1m-historie kun je niet backtesten.
6. Is er een gratis tier om mee te prototypen?
7. SLA / uptime, en wat je plan B is als hij omvalt.

---

## Rol-per-rol

### 1. Blockchain-transactiemonitoring (kritiek pad, laagste latency)

**Wat we nodig hebben:** binnen ~1 s weten dat een gevolgde wallet een swap deed, mét
geparseerde token-, bedrag- en prijsinformatie.

| Optie | Wat het is | Voordelen | Nadelen |
|---|---|---|---|
| **Helius** (Solana) | RPC + verrijkte transactie-API + webhooks per adres | Geparseerde transacties (je hoeft geen Raydium/pump.fun-instructies zelf te decoderen — dat scheelt weken werk), address-webhooks zijn precies onze use case, gespecialiseerd in Solana | Vendor lock-in op hun parse-formaat; kosten schalen met watchlist-grootte 🔍 |
| **QuickNode / Alchemy** | RPC met add-ons/streams | Multi-chain, volwassen infra | Verrijking/parsing is minder Solana-specifiek; je decodeert vaker zelf |
| **Yellowstone gRPC (Geyser)** | Directe validator-stream | Laagste latency die er is | Duur, je parseert álles zelf, hoog operationeel gewicht — dit is een fase-10-optimalisatie, geen MVP-keuze |
| **Publieke RPC** | Gratis endpoints | €0 | Rate-limited en onbetrouwbaar. Prima om te ontwikkelen, kansloos in productie |

**Aanbeveling MVP:** één verrijkte provider met address-webhooks (Helius is de meest voor de
hand liggende voor Solana), met publieke RPC als read-only fallback voor niet-urgente reads.
Bouw `adapters/chain.py` zó dat een latere overstap naar gRPC alleen die file raakt.

**Let op bij implementatie:**
- Verifieer de webhook-HMAC. Een onbeveiligde webhook-endpoint is een injectiepunt voor
  verzonnen "whale buys".
- Webhooks leveren **dubbele events** bij retries → idempotente ingest via de UNIQUE-constraint
  op `signature` (zie `02`).
- Webhooks kunnen events **missen**. Draai een periodieke reconciliatie-poll per wallet; behandel
  de webhook als snelle-maar-onbetrouwbare bron en polling als traag-maar-volledig.

### 2. Token- & marktdata (prijs, liquiditeit, volume, mcap)

| Optie | Wat het is | Voordelen | Nadelen |
|---|---|---|---|
| **DexScreener** | DEX-pair-data over veel chains | Brede dekking incl. zeer jonge pairs; publieke API 🔍 | Rate limits zijn krap voor per-seconde-polling 🔍; historische OHLCV beperkt |
| **Birdeye** | Solana-first token/markt-API | Diepe Solana-dekking, OHLCV, holder-data, trader-endpoints | Betaald 🔍 |
| **GeckoTerminal / CoinGecko** | On-chain pool-data | Goede OHLCV-historie, betrouwbaar | Nieuwe tokens verschijnen met vertraging — problematisch voor onze use case 🔍 |
| **Jupiter Price API** | Prijs via routing (Solana) | Prijs *inclusief realiseerbare route* — dichter bij wat je echt betaalt | Alleen prijs, geen volledige marktdata |
| **Directe pool-reads via RPC** | Zelf reserves lezen | Grondwaarheid, geen provider-vertraging | Je implementeert per DEX-programma een decoder |

**Aanbeveling:** twee bronnen, altijd. Eén primaire API + **directe pool-reads als kruiscontrole
op liquiditeit**. Reden: liquiditeit is de enige metriek waarop een aanvaller je actief wil
misleiden, en het is de metriek waar je positiegrootte van afhangt. Wijken de twee bronnen meer
dan X% af → `DATA_DISAGREEMENT`-flag en geen entry. Zie `04 §6`.

Voor prijs bij het *simuleren van een entry*: gebruik een quote-endpoint (Jupiter-achtig) in
plaats van een spot-prijs. Bij memecoins is het verschil tussen "de prijs" en "de prijs die jij
krijgt voor $2.000" enorm — en dat verschil ís je grootste kostenpost.

### 3. Token-security / rug-detectie (het belangrijkste onderdeel)

| Optie | Wat het is | Voordelen | Nadelen |
|---|---|---|---|
| **RugCheck** (Solana) | Risicorapport per mint | Solana-specifiek: LP-status, authorities, holderverdeling | 🔍 limieten; scores zijn een black box → gebruik de ruwe feiten, niet hun eindscore |
| **GoPlus Security** | Token-security-API, multi-chain | Breed, incl. honeypot- en tax-detectie | EVM-sterker dan Solana 🔍 |
| **Eigen on-chain checks** | Zelf mint/freeze authority en LP-eigendom lezen | Geen provider-afhankelijkheid, laagste latency, gratis, **niet te faken** | Je schrijft het zelf |

**Aanbeveling — belangrijk:** de vier checks die je vetos veroorzaken (mint authority, freeze
authority, LP burned/locked, top-holder-concentratie) doe je **zelf via directe RPC-reads**.
Motivatie:

1. Het zijn eenvoudige account-reads, geen hogere wiskunde.
2. Het is je laatste verdedigingslinie — daar wil je geen derde partij en geen extra netwerk-hop.
3. Een externe security-API die traag is of een 500 geeft mag nooit leiden tot "dan maar zonder
   check doorgaan". Bij een eigen read is de faalmodus expliciet.

Gebruik een externe security-API als **tweede mening** en voor de moeilijkere checks
(honeypot-simulatie, deployer-reputatie). Bij oneens: het strengste antwoord wint.

### 4. Twitter/X

**Dit is de duurste en meest beperkende component van het hele systeem.**

- X API v2 kent betaalde tiers; volume-caps en toegang tot filtered stream / recent search
  verschillen sterk per tier 🔍. Verifieer specifiek: maandelijkse tweet-cap, of jouw tier
  filtered stream mag gebruiken, en hoe ver `search/recent` terugkijkt.
- **Ontwerp hier omheen, niet doorheen.** Wij monitoren nooit "heel Twitter". De pipeline is
  **on-chain-first**: een token komt op de watchlist doordat een gevolgde wallet koopt, en pás
  dan gaan we er social data voor ophalen. Dat verlaagt het benodigde tweetvolume met ordes van
  grootte en maakt een goedkopere tier werkbaar.
- Daarnaast: een **permanente lijst van ~200–500 hoogwaardige accounts** volgen kost weinig
  volume en levert het meest bruikbare signaal (zie `04 §3` — kwaliteit slaat kwantiteit).
- Scraping van X is in strijd met hun voorwaarden en breekt continu. Ik raad het af; niet uit
  preutsheid maar omdat een fundering die willekeurig omvalt geen fundering is.
- **Overweeg Telegram als aanvullende bron.** Voor memecoins gebeurt een groot deel van de
  vroege coördinatie in Telegram-groepen en op Discord, niet op X. De Telegram-client-API is
  goedkoper en ruimer dan de X API. Modelleer het als een extra `SocialSource` achter dezelfde
  interface — de scoring in `04 §3` is bronagnostisch.

### 5. AI / LLM

**Anthropic API** met Claude voor tweet-classificatie en narratief-detectie.
- Gebruik **tool-use / structured output** zodat je een schema-gevalideerd object terugkrijgt in
  plaats van tekst die je moet parsen.
- Kies het model op kosten-per-classificatie, niet op maximale capaciteit: dit is
  hoogvolume-classificatie, geen redeneerwerk. Een kleiner model met een goede prompt is hier
  bijna altijd het juiste antwoord; escaleer alleen bij lage confidence.
- **Cache op `text_hash`.** Retweets en copy-paste-shills zijn letterlijk identiek; die betaal
  je maar één keer.
- **Batch** meerdere tweets per call.
- Harde daglimiet (`FOMO_LLM_MAX_DAILY_USD`); bij overschrijding valt de classifier terug op de
  deterministische heuristiek (zie `ai/fallback.py`). Het systeem wordt dan dommer, niet stuk.

### 6. Alerts

- **Telegram Bot API** — één HTTP-POST naar `sendMessage`. Geen SDK nodig. Rate limit ligt rond
  een paar berichten per seconde per chat 🔍; onze dedupe-laag zit daar ruim onder.
- **Discord webhooks** — idem, één POST. Rich embeds zijn hier mooier dan bij Telegram.

Beide zijn triviaal en geen van beide zit op het kritieke pad — alerts zijn fire-and-forget.

---

## Geschatte MVP-kosten

Ik geef bewust geen totaalbedrag, omdat elk onderdeel 🔍-verificatie vereist. Wat ik wél kan
zeggen over de **verhouding**, en dat stuurt je begroting:

```
X / Twitter API        ████████████████████  veruit de grootste post
Blockchain-provider    ████████              schaalt met watchlist-grootte
Markt-data             ████                  vaak een gratis tier voor de MVP
LLM                    ██                    goedkoop mits gecached en gebatcht
Security-API           █                     grotendeels zelf te doen
Hosting                █                     één VPS volstaat lang
```

De praktische consequentie: **begin met fase 1–2 zonder X API**. Verzamel eerst on-chain data,
bewijs dat je trader-scoring iets voorspelt, en koop de dure social-tier pas als je weet welk
tweetvolume je écht nodig hebt. Andersom betaal je maanden voor data waarvan je nog niet weet
of je hem kunt gebruiken.
