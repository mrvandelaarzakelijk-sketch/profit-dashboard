# 04 — Scoringmodel, riskmodel en signaallogica

Dit is het hart van het systeem. Elke formule hieronder staat 1-op-1 in `fomo/scoring/`,
`fomo/risk/` en `fomo/signals/`, en elke coëfficiënt komt uit `config/weights.toml` — geen
enkel magisch getal in de code.

---

## 0. Bouwstenen

Vier hulpfuncties komen overal terug. Ze staan in `fomo/scoring/math_utils.py`.

| Functie | Definitie | Waarvoor |
|---|---|---|
| `sat(x, k)` | `x / (x + k)`, x ≥ 0 | Verzadiging: van 1 → 2 traders is veel; van 9 → 10 nauwelijks. `k` is de halfwaarde. |
| `σ(x)` | `1 / (1 + e^(−x))` | Log-odds → kans. |
| `logscale(x, lo, hi)` | `clip((ln x − ln lo) / (ln hi − ln lo), 0, 1)` | Grootheden die over ordes van grootte lopen (liquiditeit, mcap, leeftijd). |
| `center(s)` | `(s/100 − 0.5) · 2` → `[−1, 1]` | Subscore → bewijsterm die ook *negatief* kan bijdragen. |

**Waarom overal saturatie en log-schalen?** Elke ruwe grootheid in deze markt is
zwaarstaartverdeeld. Een lineaire term op "USD gekocht" laat één $500k-order elke andere feature
overstemmen. Saturatie codeert de economische realiteit: er is een punt waarop meer bewijs niet
meer overtuigender is.

---

## 1. Waarom de master score multiplicatief-gepoort is

Herhaling van `01 §3`, want alles hangt hieraan:

```
MASTER = 100 · Veto · R_soft · Timing · σ(Evidence)
         └─┬─┘  └┬─┘   └──┬──┘  └──┬──┘  └────┬────┘
           │     │        │        │          └── bewijs: smart money, social, markt, liquiditeit
           │     │        │        └───────────── anti-FOMO: hoeveel is er al gebeurd?
           │     │        └────────────────────── graduele risicokorting  [0.30 … 1.00]
           │     └─────────────────────────────── harde veto             {0, 1}
           └───────────────────────────────────── schaal
```

Vier eigenschappen die dit correct maken:

1. **Vetos zijn absorberend.** `0 · alles = 0`. Geen hoeveelheid hype overstemt een mint authority.
2. **Timing is een multiplier, geen term.** Te laat is te laat — óók bij perfect bewijs. Als
   timing een optelterm was, kon sterk bewijs een late entry goedpraten. Precies de fout die je
   wilt vermijden.
3. **`σ(Evidence)` is een logistische regressie.** De handgekozen gewichten zijn de *prior*;
   in fase 10 vervangen we ze door gefitte coëfficiënten op dezelfde features (§9). Het
   heuristische model en het ML-model zijn dezelfde functie — daardoor is de overstap een
   parameterwissel, geen herbouw.
4. **Alles is traceerbaar.** Elke factor is opgeslagen, dus elke score is achteraf te ontleden
   in "hoeveel kwam waarvandaan".

---

## 2. Trader score

Per trader, per venster (30d / 90d / all). Output: 0–100 + een `reliability`-label.

### 2.1 Componenten

Elk in `[0, 1]`.

**a) `perf` — risk-adjusted return, niet ROI**

Per trade de log-return `rᵢ = ln(exit / entry)`. Dan:

```
IR   = mean(r) / std(r)          # per-trade information ratio
perf = σ(k_ir · IR)              # k_ir ≈ 2.0
```

> **Waarom log-returns en waarom IR?** Rauwe ROI is bij memecoins bijna betekenisloos: één 50×
> maakt van een verliesgevende trader een "+4000% ROI"-ster. Log-returns zijn optelbaar over
> tijd en behandelen ×2 en ÷2 symmetrisch (+0,69 en −0,69) — met gewone procenten is +100% en
> −50% asymmetrisch terwijl je op dezelfde plek uitkomt. Delen door de standaarddeviatie straft
> de trader die zijn rendement uit pure variantie haalt.

**b) `winrate` — Bayesiaans geschud**

```
winrate = (wins + α) / (n + α + β)      met Beta(α = 2, β = 3)
```

> **Waarom niet gewoon wins/n?** Omdat `n` klein is. Een wallet met 3 winsten uit 3 trades heeft
> geen 100% win rate; hij heeft drie datapunten. De Beta(2,3)-prior heeft gemiddelde 0,4 en
> sterkte 5, wat zegt: "voordat ik iets van je zie, ga ik uit van ~40% en je hebt ongeveer vijf
> trades nodig om me evenveel te overtuigen als mijn prior." Met 3/3 wordt de score
> `5/8 = 0,63` in plaats van 1,0. Dat is precies goed. **Dit is de belangrijkste enkele
> correctie in het hele trader-model** — zonder shrinkage domineren toevalstreffers je ranking,
> en toevalstreffers zijn per definitie niet herhaalbaar.

**c) `consistency` — komt de winst uit één trade?**

```
conc        = grootste_trade_winst / totale_winst
consistency = clip( (1 − conc) / (1 − 1/n), 0, 1 )
```

Bij perfect gelijke verdeling is `conc = 1/n` → `consistency = 1`. Komt alle winst uit één trade
(`conc = 1`) → `0`. De noemer normaliseert voor het feit dat concentratie mechanisch daalt bij
meer trades. Dit scheidt "goede trader" van "één keer geluk gehad".

**d) `early` — hoe vroeg stapt hij in**

```
early = 0.5 · (1 − logscale(median_entry_mcap, 30_000, 3_000_000))
      + 0.5 · (1 − logscale(median_entry_age_s,     300,    86_400))
```

Lager = vroeger = beter. De grenzen zijn memecoin-specifiek: instappen op $30k mcap binnen
5 minuten is maximaal vroeg; op $3M na een dag is dat niet meer.

**e) `exit_q` — verkoopt hij in de buurt van de top?**

```
exit_q = mediaan( exit_prijs / max_prijs_tijdens_holdperiode )   ∈ [0, 1]
```

Onderschat deze niet. Bij memecoins is *uitstappen* de schaarse vaardigheid. Iedereen kan een
20× op papier hebben gehad; de vraag is wie hem realiseerde.

### 2.2 Straffen (multiplicatief)

```
penalty = (1 − p_bot · bot_likeness) · (1 − p_churn · sat(trades_per_dag, k_churn)) · (1 − p_sus · suspicious)
```

Multiplicatief en niet aftrekkend, omdat "botachtig" het hele signaal ondermijnt in plaats van
er een beetje van af te halen.

### 2.3 Samenstellen

```
L      = b + w_perf·perf + w_wr·winrate + w_cons·consistency + w_early·early + w_exit·exit_q
raw    = σ(L)                                  ∈ (0, 1)
shrink = n / (n + k_n)                         k_n ≈ 10
score  = 100 · ( 0.5 + (raw − 0.5) · shrink ) · penalty
```

De `shrink`-term trekt traders met weinig trades naar 50 (= "ik weet het niet"), niet naar 0.
Een onbewezen trader is niet slecht, hij is onbekend — en dat is een ander ding, dat je ook
anders moet behandelen.

```
reliability:  n ≥ 40 en span ≥ 30 d  → HIGH
              n ≥ 15                 → MEDIUM
              n ≥  5                 → LOW
              anders                 → UNPROVEN
```

**Werkelijke output** (`python -m fomo.cli traders` — dit zijn berekende waarden, geen
geïllustreerde):

```
trader          score    reliab.   components
--------------------------------------------------------------------------------------
solkid           68.7       HIGH   perf=0.65 winrate=0.57 consistency=0.86 early=0.87 exit_q=0.63
                                   shrink=0.90  penalty=0.93
apewhale         39.4     MEDIUM   perf=0.50 winrate=0.39 consistency=0.20 early=0.46 exit_q=0.38
                                   shrink=0.70  penalty=0.97
mm-bot-ish       14.2     MEDIUM   perf=0.57 winrate=0.54 consistency=0.98 early=0.12 exit_q=0.51
                                   shrink=0.99  penalty=0.26
```

Let op de drie verhalen die hier verteld worden:

- **apewhale** heeft de spectaculairste headline: één trade deed 49×. Maar die ene trade is 81%
  van al zijn winst, en zijn cumulatieve log-return is ±0,00 — zónder die treffer is hij
  hooguit break-even. `consistency` klapt in naar 0,20 en hij eindigt ver onder solkid.
- **solkid** heeft een kleinere edge per trade, maar reproduceerbaar over 87 trades.
- **mm-bot-ish** heeft de langste historie (1400 trades) én een positieve win rate — maar
  180 trades per dag met een bot-likeness van 0,85 kost hem 74% van zijn score. Een market
  maker kopiëren is geen edge kopiëren.

Merk op dat een score van 91 in dit model zeldzaam hoort te zijn: die vereist dat vrijwel elke
component tegen 1,0 loopt bij een lange historie. Een model dat makkelijk 90+ uitdeelt, is niet
streng genoeg om iets mee te filteren.

---

## 3. Social momentum

### 3.1 Kwaliteitsgewogen mentions, geen tellingen

Elke mention krijgt het gewicht `qᵢ ∈ [0,1]` van zijn auteur:

```
Q(t) = Σᵢ qᵢ    over het venster [t − Δ, t]
```

`q` per account (uit `twitter_accounts.quality_score`), opgebouwd uit:

| Signaal | Richting | Waarom |
|---|---|---|
| accountleeftijd | ouder = beter | shill-farms zijn wegwerp-accounts |
| follower/following-ratio | hoger = beter | massa-follow is een botsignatuur |
| engagement-ratio op **niet-doelwit**-tweets | hoger = beter | meet echt publiek, niet de gekochte piek |
| aandeel duplicaat-tekst in historie | lager = beter | copy-paste = campagne |
| posting-regelmaat (variantie van intervallen) | onregelmatiger = beter | mensen slapen; bots niet |
| historische accuratesse | hoger = beter | gingen coins die hij noemde omhoog? |
| categorie (`researcher`/`trader` > `meme` > `giveaway`) | | door de AI toegekend, §7 |

**`historical_accuracy` is de eerlijkste feature die er is**, en hij is gratis: je hebt al een
prijsdatabase. Voor elk account, voor elke mention, meet de forward return over 1u/6u/24u.
Accounts waarvan de mentions systematisch de top markeren, krijgen een *negatief* gewicht — een
exit-signaal in plaats van een entry-signaal.

### 3.2 Velocity — een z-score, geen procentuele stijging

Naïef: `(200 − 20)/20 = +900%`. Kapot zodra de baseline 0 is (elk nieuw token), en gevoelig
voor uitschieters.

```
baseline = EWMA van Q over de laatste 24 u (huidig venster uitgesloten)
scale    = max( MAD(Q), floor )
velocity_z = (Q_now − baseline) / scale
```

MAD (median absolute deviation) in plaats van standaarddeviatie omdat social-tijdreeksen
uitschieters bevatten die de sd opblazen precies wanneer je hem nodig hebt. De `floor` voorkomt
deling door nul bij een gloednieuw token — daar is de baseline per definitie 0 en is elke
mention nieuws.

Voor de mens rapporteren we ook de leesbare `+640%`; voor de score gebruiken we `velocity_z`.

### 3.3 Diversiteit — hier sneuvelt "1000 tweets van 50 bots"

Shannon-entropie over de mentionverdeling per account:

```
H         = −Σ pᵢ ln pᵢ           pᵢ = aandeel mentions van account i
N_eff_acc = exp(H)                # perplexiteit = "effectief aantal accounts"
diversity = N_eff_acc / N_mentions ∈ (0, 1]
```

| Situatie | N_mentions | N_eff_acc | diversity |
|---|---|---|---|
| 1000 tweets, 50 bots, gelijk verdeeld | 1000 | 50 | **0,05** |
| 100 tweets, 100 echte accounts | 100 | 100 | **1,00** |
| 300 tweets, 40 accounts, 1 spamt 200× | 300 | ~13 | **0,04** |

Precies het gedrag dat je vroeg. En het is één regel, geen ad-hoc botregels.

### 3.4 Ticker-disambiguatie

Er zijn altijd meerdere tokens die `$MOON` heten. Een mention wordt aan een token gekoppeld met
aflopend vertrouwen:

1. **contract address in de tekst** → zeker (`match_confidence = 1.0`)
2. ticker + link naar een chart-URL met dat address → zeker
3. ticker + de tweet valt binnen het tijdvenster van on-chain activiteit op precies één
   kandidaat-token → waarschijnlijk (0,7)
4. alleen ticker, meerdere kandidaten → **verdeel het gewicht, of gooi weg**

Mentions met `match_confidence < 0.5` tellen niet mee in `Q`. Liever een lager social-signaal
dan een signaal over het verkeerde token. Dit is een van de meest onderschatte foutbronnen in
dit type systeem.

### 3.5 De social score

```
L_soc = b + a_vel·sat(max(velocity_z,0), k_vel)
          + a_div·diversity
          + a_acc·sat(N_eff_acc, k_acc)
          + a_eng·sat(engagement_ratio, 1)
          + a_sent·sentiment_centered          # ∈ [−1, 1], mag negatief bijdragen
SOCIAL = 100 · σ(L_soc) · (1 − bot_share) · (1 − duplicate_text_share)
```

Bot-aandeel en duplicaat-tekst zijn multiplicatieve straffen, in lijn met de rest: gecoördineerde
shilling maakt het signaal niet "iets zwakker", het maakt het *ongeldig*.

**Voorbeeldoutput**

```
SOCIAL MOMENTUM  87/100
  Mention velocity   +640%   (z = 4.1)
  Unique accounts    +310%   (N_eff = 118 van 141 mentions → diversity 0.84)
  High-quality accts +180%   (23 accounts met q > 0.7)
  Sentiment          82/100
  Bot share          6%      Duplicate text  4%
```

---

## 4. Multiple-trader confirmation — effectief onafhankelijke traders

Vijf wallets van één persoon zijn geen vijf signalen. Dit is de **Kish effective sample size**,
de standaardcorrectie voor gecorreleerde waarnemingen:

```
          ( Σᵢ qᵢ )²
N_eff = ─────────────────
        Σᵢ Σⱼ qᵢ qⱼ ρᵢⱼ
```

met `qᵢ` = kwaliteitsgewicht van trader i (score/100) en `ρᵢᵢ = 1`.

| Scenario | Uitkomst |
|---|---|
| 3 gelijkwaardige, volledig onafhankelijke traders (ρ = 0) | `N_eff = 3` |
| 3 traders, ρ = 1 (één persoon) | `N_eff = 1` |
| 5 traders waarvan 3 in één cluster | ≈ 2,4 |

Het gedrag dat je wilt, volgt automatisch uit de formule in plaats van uit een lijst
uitzonderingen.

### Schatten van ρ

```
ρᵢⱼ = clip( max( w_j·Jaccard(tokens_i, tokens_j),
                 funding_link_strength(i, j),
                 timing_corr(i, j),
                 same_cluster(i, j) ? 0.95 : 0 ), 0, 1 )
```

- **Jaccard** over historisch gekochte tokens: hoge overlap = dezelfde informatiebron of dezelfde
  groep. Werkt óók als de wallets financieel niet gelinkt zijn — dat is precies de "zelfde
  Telegram-groep"-situatie.
- **Funding graph:** wallet A financierde B, of beide vanaf dezelfde CEX-deposit → sterke link.
- **Timing:** consistent kopen binnen enkele seconden na dezelfde wallet = copy trading. Meet de
  mediaan van Δt over gedeelde tokens; < 30 s herhaaldelijk → ρ hoog. Merk op dat dit
  *asymmetrisch* is: de volger draagt geen nieuwe informatie, de leider wel. In de
  implementatie krijgt de leider het volle gewicht en de volger bijna niets.
- **Cluster:** al toegewezen aan dezelfde `wallet_cluster`.

Deze correlatiematrix wordt offline (batch, dagelijks) berekend en gecached — hij verandert
langzaam en je wilt hem niet op het kritieke pad hebben.

---

## 5. De vijf deelscores

### 5.1 Smart Money Score

```
L_sm = b + c_neff · sat(N_eff − 1, k_neff)          # 1 trader = 0 bonus; schaalt daarna
         + c_qual · mean_quality                     # gemiddelde traderkwaliteit
         + c_usd  · sat(totaal_usd_in, k_usd)
         + c_conv · sat(max_pct_van_portefeuille, 0.10)   # conviction: 10% van zijn wallet = veel
         + c_early· (1 − logscale(token_age_bij_1e_buy, 300, 86_400))
         − c_sell · sat(smart_money_verkoop_usd / max(smart_money_koop_usd, 1), 1)
SMART = 100 · σ(L_sm)
```

Drie dingen die hierin zitten en die je in de meeste bots niet vindt:

- **`max_pct_van_portefeuille`** — dít is conviction, niet het absolute bedrag. $50k van iemand
  met $5M is een gok; $50k van iemand met $200k is een statement.
- **De verkoopterm is negatief.** Als gevolgde wallets tegelijk kopen én verkopen, is dat geen
  accumulatie maar rotatie.
- **`N_eff − 1`**: één trader levert geen confirmatie-bonus. Confirmatie begint per definitie bij
  de tweede, onafhankelijke trader.

### 5.2 Market Momentum Score

```
L_mkt = b + m_vol  · sat(volume_1h / liquiditeit, 3)          # omzetsnelheid, gezonde band
          + m_buy  · (unique_buyers / max(unique_sellers,1) − 1 geclipt)
          + m_grow · sat(groei_unieke_kopers_15m, k)
          + m_hold · sat(holder_groei_15m, k)
MARKET = 100 · σ(L_mkt)
```

Let op: **`unique_buyers`, niet `buy_count`.** 500 buys van 4 wallets is wash trading; 500 buys
van 400 wallets is vraag. Het onderscheid tussen die twee is het verschil tussen een goede en
een dure dag.

De prijsstijging zelf zit hier **bewust niet** in — die hoort thuis in de timing-multiplier (§6).
Prijsstijging als positieve scoreterm ís letterlijk FOMO in code gieten.

### 5.3 Liquidity Score

Niet "hoeveel liquiditeit is er", maar **"kan ik met mijn positiegrootte in en uit tegen
acceptabele kosten"**. Bij constant-product met liquiditeit `L` (USD, beide zijden) en
ordergrootte `S`:

```
price_impact ≈ S / (L/2 + S)
roundtrip_cost ≈ 2 · price_impact + 2 · dex_fee + priority_fee
LIQ = 100 · clip(1 − roundtrip_cost / max_acceptabele_kosten, 0, 1) · logscale(L, L_min, L_goed)
```

Waarom zo: een pool van $40k is prima voor $300 en waardeloos voor $20.000. Een score die daar
niet naar kijkt, is geen liquiditeitsscore maar een getal.

De liquiditeitsscore is bovendien de **input voor positiegrootte**: `size = min(max_pct·equity,
size_waarbij_impact ≤ drempel)`. De bot verkleint zijn positie liever dan dat hij een kans laat
lopen — maar hij verkleint hem écht.

### 5.4 Risk Score

Zie §7. Output: `veto ∈ {0,1}`, `R_soft ∈ [0.30, 1.00]`, en een leesbare `RISK`-score 0–100
(hoger = veiliger) puur voor het dashboard.

### 5.5 Master score

```
Evidence = b + k_sm·center(SMART) + k_soc·center(SOCIAL)
             + k_mkt·center(MARKET) + k_liq·center(LIQ)
             + k_conf·sat(N_eff − 1, k_neff)

MASTER = 100 · Veto · R_soft · Timing · σ(Evidence)
```

`center()` maakt dat een zwakke deelscore *negatief* bijdraagt in plaats van "een beetje
positief". Een token met SOCIAL = 20 moet punten kósten, niet 20% van de sociale bijdrage
opleveren.

---

## 6. Timing-multiplier — de anti-FOMO-laag

Drie onafhankelijke manieren om "te laat" te meten:

```
f_smart  = 1 − sat( max(prijs_nu/prijs_1e_smart_entry − 1, 0), k_smart )
f_runup  = 1 − sat( max(prijs_nu/prijs_15m_geleden − 1, 0),    k_runup )
f_para   = 1 − sat( max(prijs_nu/EMA20_1m − 1, 0),             k_para  )

Timing = clip( min(f_smart, f_runup, f_para), 0.15, 1.00 )
```

**Waarom `min` en niet het product?** De drie maten zijn sterk gecorreleerd — een parabolische
candle geeft ook een hoge run-up en een hoge multiple. Vermenigvuldigen straft dezelfde
gebeurtenis drie keer en drukt elk sterk momentum naar nul. `min` zegt: "de strengst bindende
observatie bepaalt", wat hoe een handelaar er ook naar kijkt.

**De ondergrens 0,15 is opzettelijk geen 0.** Een token dat al hard gelopen heeft, is niet
verboden — hij is gedegradeerd. Bij extreem sterk bewijs kan `0.15 · σ(E)` nog steeds een
`WATCH` opleveren. Dat is eerlijker dan een harde deur, want soms is de beweging écht net begonnen.

Voorbeeld met `k_smart = 1.0`:

| Prijs t.o.v. eerste smart entry | `f_smart` |
|---|---|
| +0% (gelijk met de trader) | 1,00 |
| +50% | 0,67 |
| +100% (2×) | 0,50 |
| +300% (4×) | 0,25 |
| +900% (10×) | 0,10 → geclipt op 0,15 |

Dit is precies je scenario "token al +300% gestegen": de master score wordt met ~75% gekort.
Een token dat op alle overige assen 90 scoorde, zakt naar ~22 → `WATCH`, geen entry. Het systeem
zegt dan letterlijk:

> `Signal detected, but entry is too late (4.0× above first smart-money entry).`

---

## 7. Risk engine

### 7.1 Harde veto's — `MASTER = 0`, `SIGNAL = AVOID`

Geen enkele hiervan is onderhandelbaar en geen enkele wordt gecompenseerd.

| Regel | Drempel | Waarom fataal |
|---|---|---|
| `MINT_AUTHORITY_ACTIVE` | niet revoked | De dev kan oneindig bijdrukken. Je positie kan tot nul verwateren terwijl de chart stijgt. |
| `FREEZE_AUTHORITY_ACTIVE` | niet revoked | Je tokens kunnen bevroren worden. Je kunt niet verkopen. |
| `LP_NOT_SECURED` | < 90% burned én niet gelockt | De liquiditeit kan in één transactie verdwijnen. Dit is *de* klassieke rug. |
| `HONEYPOT` | sell-simulatie faalt | Je kunt kopen en niet verkopen. |
| `SELL_TAX_EXTREME` | > 10% | Economisch equivalent aan een honeypot. |
| `TRANSFER_HOOK_UNKNOWN` | hook aanwezig, niet whitelisted | Token-2022-hooks kunnen transfers willekeurig blokkeren. |
| `HOLDER_CONCENTRATION` | top-10 (excl. LP/burn) > 45% | Eén wallet kan de hele pool leeghalen. |
| `CREATOR_HOLDING` | creator > 15% | Idem, met slechtere incentives. |
| `LIQUIDITY_FLOOR` | < `min_liquidity_usd` | Je kunt niet uitstappen. Grootte-afhankelijk. |
| `SERIAL_RUGGER` | creator heeft ≥ N eerdere tokens met LP-pull | De beste voorspeller van een rug is een eerdere rug. |
| `DATA_DISAGREEMENT` | bronnen wijken > 25% af op liquiditeit | Je weet niet wat waar is. Onbekend = geen entry. |
| `STALE_DATA` | security-snapshot ouder dan N s | Handelen op verouderde veiligheidsdata is handelen zonder. |

De laatste twee zijn architectuurregels, geen marktregels, en ze zijn even belangrijk. **De
faalmodus van elke datacheck moet AVOID zijn, nooit "dan maar doorgaan".**

### 7.2 Zachte straffen — `R_soft`

```
R_soft = clip( 1 − Σ penaltyᵢ, 0.30, 1.00 )
```

| Regel | Straf |
|---|---|
| top-10-concentratie 25–45% | 0,05 – 0,25 lineair |
| bundled/sniper supply 15–35% | 0,05 – 0,20 |
| liquiditeit/mcap-ratio < 3% | tot 0,20 |
| FDV/mcap > 3 (grote unlock-overhang) | tot 0,15 |
| volume/liquiditeit > 20 (wash-verdenking) | tot 0,20 |
| token < 3 min oud | 0,10 (metadata nog niet betrouwbaar) |
| buy tax 1–5% | tot 0,10 |
| LP gelockt maar unlock < 7 d weg | 0,15 |

De ondergrens 0,30 in plaats van 0 is bewust: als iets écht fataal is, hoort het een **veto** te
zijn. Het optellen van zachte straffen tot een de-facto veto verstopt de reden in een getal, en
dan kun je achteraf niet zien waaróm iets is afgewezen.

### 7.3 Wat we bewust niet in de risk engine stoppen

- **Contract-broncode-analyse** — de meeste memecoins gebruiken het standaard-tokenprogramma.
  De risico's zitten in *authorities* en *LP-eigendom*, niet in maatwerkcode. Voor Solana-SPL
  is een "audit" grotendeels theater.
- **Team/KYC-checks** — bij memecoins is anonimiteit de norm; het discrimineert niet.
- **Whitepapers en socials-op-de-website** — kost API-calls, voorspelt niets.

---

## 8. Signaallogica

### 8.1 Toestanden

Elke toestand vereist een **score** én **structurele voorwaarden**. Een hoge score alleen is
nooit genoeg — dat is hoe je een 95 krijgt op een token met $8k liquiditeit.

| Signaal | Score | Structurele eisen (alle) |
|---|---|---|
| **AVOID** | — | veto actief, **of** een kritieke false-positive-flag, **of** MASTER < 25 |
| **WATCH** | ≥ 40 | geen veto |
| **EARLY_WATCH** | ≥ 55 | `N_eff ≥ 1.0` · `Timing ≥ 0.60` · `LIQ ≥ 40` · token < 24 u oud |
| **POTENTIAL_ENTRY** | ≥ 70 | `N_eff ≥ 1.8` · `Timing ≥ 0.50` · `LIQ ≥ 60` · `MARKET ≥ 50` · `R_soft ≥ 0.70` · geen kritieke flags |
| **STRONG_ENTRY** | ≥ 82 | `N_eff ≥ 2.5` · `Timing ≥ 0.60` · `LIQ ≥ 70` · `MARKET ≥ 60` · `R_soft ≥ 0.85` · `SOCIAL ≥ 55` · géén flags |
| **EXIT** | — | zie §8.3 |

`EARLY_WATCH` bestaat expliciet voor de tweefasige pipeline uit `01 §4`: hij kan afgegeven worden
op smart money + risk + markt alléén, vóórdat social binnen is. Daarom heeft hij geen
SOCIAL-eis. Sociale data upgradet het signaal later; hij is er nooit een voorwaarde voor.

### 8.2 Uitgewerkt voorbeeld — het hele punt van het ontwerp in één paar

Onderstaande output komt uit `python -m fomo.cli explain happy_path` en `… late_entry`. Het zijn
**exact dezelfde inputs op elke as behalve de prijs.**

```
COIN: $XYZ   —   4u12m oud   —   mcap $1.84M   —   liquiditeit $312k
3 traders (A $42k, B $18,5k, C $11,2k), zwak gecorreleerd → N_eff = 2.97

                          op tijd            te laat
  Smart Money              84.19              84.19     ← identiek
  Social Momentum          72.22              72.22     ← identiek
  Market Momentum          68.28              68.28     ← identiek
  Liquidity                64.34              64.34     ← identiek
  Safety (risk)           100.00             100.00     ← identiek
  Evidence                 +1.779             +1.779    ← identiek
  σ(Evidence)              0.8556             0.8556    ← identiek

  Timing                    0.872              0.250    ← HET ENIGE VERSCHIL
  (binding factor)      parabolic      smart_multiple
                                          (4.00× boven eerste smart entry)

  MASTER  = 100·1·1.000·0.872·0.8556 = 74.62
  MASTER  = 100·1·1.000·0.250·0.8556 = 21.39

  SIGNAL       POTENTIAL_ENTRY          AVOID
```

Het bewijs is in beide gevallen identiek en uitstekend. **Een naïeve gewogen som zou beide keren
ongeveer 78 geven en beide keren "koop" roepen.** Ons model geeft 74,6 versus 21,4.

Waarom dat correct is: de traders die we volgen kochten op $0,00041. In het rechterscenario zouden
wij op $0,00164 kopen — 4× hoger. We nemen hetzelfde neerwaartse risico (naar nul) voor een kwart
van de opwaartse kans. Het model weigert dat te belonen, en het zegt letterlijk waarom:

> `Entry is late: price is 4.0x the first smart-money entry (binding factor: smart_multiple),`
> `score cut by 75%. MASTER 21.4 -> AVOID.`

Zelfde token, zelfde bewijs, ander moment. Bij memecoins is dát de variabele die het meest
bepaalt of je verdient of verliest — en in dit model is het de enige variabele die niet
gecompenseerd kan worden.

> **Waarom scoort `happy_path` 74,6 en geen 88?** Omdat `timing` ook daar 0,872 is (de prijs staat
> licht boven de EMA20) en de liquiditeitsscore 64 is bij $312k pool. Het model is bewust streng:
> `STRONG_ENTRY` vereist 82+ *plus* zes structurele voorwaarden. Als je regelmatig 90+ ziet, staan
> je gewichten te los.

### 8.3 Exit-logica

Exits zijn geen omgekeerde entry. Ze worden per **positie** geëvalueerd, elke tick.

| Exit-type | Trigger | Actie |
|---|---|---|
| `TAKE_PROFIT` | geschaald: 1/3 op +60%, 1/3 op +150%, rest trailing | Bij memecoins realiseer je onderweg of je realiseert nooit. |
| `TRAILING` | prijs valt `trail_pct` onder high-water (trail verkrapt naarmate winst stijgt) | |
| `STOP_LOSS` | −`stop_pct` vanaf effectieve entry | Hard. Geen "even wachten". |
| `SIGNAL_INVALIDATED` | `N_eff` van *nog-houdende* traders zakt onder 1,0, **of** MASTER < 35, **of** SOCIAL zakt > 60% vanaf piek | De aanleiding is weg; de reden om te houden dus ook. |
| `SMART_MONEY_EXIT` | ≥ 50% van de bevestigende `N_eff` heeft verkocht | **De sterkste exit die er is.** Zij zien wat wij niet zien. |
| `EMERGENCY` | liquiditeit −40% in 5 min · LP-pull gedetecteerd · nieuwe veto-conditie · sell-simulatie faalt nu wél | Direct uit, tegen elke prijs. Marktorder, maximale slippage-tolerantie. |
| `TIMEOUT` | max holdtijd bereikt zonder dat de these uitkomt | Kapitaal is er om te roteren. |

De asymmetrie is opzettelijk: **entries eisen bevestiging van meerdere onafhankelijke bronnen,
exits vuren op één enkele.** Bij memecoins is de kostenverdeling van fouten extreem scheef —
een gemiste entry kost een kans, een gemiste exit kost de hele positie.

---

## 9. False-positive-detectie

Deze draaien apart van de scoring en zetten expliciete vlaggen. **Kritiek** = forceert `AVOID`.

| Vlag | Conditie | Ernst |
|---|---|---|
| `SOCIAL_WITHOUT_BUYERS` | hoge social velocity, `unique_buyers_15m` laag | **kritiek** |
| `SINGLE_WEAK_TRADER` | `N_eff < 1.2` en mean trader score < 55 | hoog |
| `CLUSTER_ILLUSION` | `n_wallets ≥ 3` maar `N_eff < 1.5` | **kritiek** |
| `VOLUME_WITHOUT_LIQUIDITY` | volume/liq > 20 en liq < drempel | **kritiek** |
| `INSIDER_DISTRIBUTION` | creator/top-holders netto verkopers terwijl social stijgt | **kritiek** |
| `COORDINATED_SHILL` | `duplicate_text_share > 0.4` of ≥ 10 accounts posten binnen 60 s bijna identieke tekst | hoog |
| `WASH_TRADING` | buy/sell tussen dezelfde walletset > 60% van volume | **kritiek** |
| `NEW_ACCOUNT_SWARM` | > 50% van mentions van accounts jonger dan 30 d | hoog |
| `DATA_DISAGREEMENT` | providers oneens > 25% | **kritiek** |
| `PRICE_SOCIAL_DIVERGENCE` | social piek, prijs vlak/dalend | medium — vaak exit-distributie |

`CLUSTER_ILLUSION` en `SOCIAL_WITHOUT_BUYERS` zijn de twee die in de praktijk het meeste geld
besparen. Ze zijn ook precies de twee die een naïeve implementatie mist, omdat ze er in ruwe
tellingen uitzien als het *sterkste* signaal dat je die dag hebt gezien.

---

## 10. Waar AI wel en niet zit

| Taak | Wie | Waarom |
|---|---|---|
| mcap, liquiditeit, volume, prijs, slippage | **regels** | Deterministisch, exact, gratis, in microseconden. Een LLM hierop loslaten voegt latency, kosten en niet-determinisme toe aan een rekensom. |
| authorities, LP-status, holderverdeling | **regels** | Feiten van de chain. Er valt niets te interpreteren. |
| transactie-classificatie (OPEN/ADD/SELL) | **regels** | Balansvergelijking. |
| cluster-detectie, `N_eff` | **regels (graaf)** | Wiskunde, geen taal. |
| **tweet → sentiment/intent/categorie** | **AI** | Taal, sarcasme, context, meme-referenties, "ratio'd" vs "bullish". Regels falen hier volledig. |
| **account-categorisatie** | **AI** | Bio, posting-stijl en toon zijn taal. |
| **narratief-detectie** | **AI** | "Alles wat op een kat lijkt loopt vandaag" is een patroon dat geen enkele regel vangt. |
| **anomalie-uitleg** | **AI** | Van feature-vector naar leesbare uitleg in de alert. |
| **eindbeslissing / order** | **NOOIT AI** | Niet-reproduceerbaar, niet backtestbaar, niet auditbaar, prompt-injecteerbaar via tweets die we zelf inlezen. |

Dat laatste punt is een echte aanvalsvector, geen theorie: **elke tweet die we inlezen is
door een aanvaller geschreven tekst.** Als een LLM met handelsbevoegdheid tweets leest, kan
iemand een tweet posten die instructies bevat. Onze AI-laag produceert daarom uitsluitend
schema-gevalideerde velden binnen vaste grenzen, en die velden gaan door dezelfde numerieke
scoring als alles anders. Er is geen pad van tweettekst naar een beslissing dat niet door de
scoring loopt.

**Outputschema van de AI-laag** (schema-afgedwongen via tool-use):

```json
{
  "coin": "XYZ",
  "contract_address": "…",
  "match_confidence": 0.94,
  "sentiment": 0.82,
  "relevance": 0.91,
  "hype": 0.74,
  "credibility": 0.88,
  "intent": "bullish",
  "category": "trade_signal",
  "confidence": 0.86
}
```

`intent ∈ {bullish, bearish, neutral}` · `category ∈ {news, launch, partnership, listing,
meme_hype, technical_analysis, trade_signal, shill, scam_warning, liquidity_warning}`.

Bij `category ∈ {scam_warning, liquidity_warning}` gaat de tweet **niet** door de sentiment-
weging maar rechtstreeks naar de risk engine als extra observatie. Een gewaarschuwde scam die
als "bearish sentiment" wordt meegewogen, verdrinkt in tien bullish shills.

---

## 11. Machine learning (fase 10+)

### Doelvariabele

Niet "gaat hij omhoog", maar iets waar je een positie op kunt baseren:

```
y = 1  als max(prijs) in de komende 6 u ≥ entry · 1.20  vóórdat  min(prijs) ≤ entry · 0.80
y = 0  anders
```

Dit is een **first-passage/barrier-label** (triple-barrier): het respecteert de volgorde waarin
de barrières geraakt worden. Een gewoon "return na 6 uur"-label is misleidend, want een token
die eerst −60% doet en dan terugkomt, is geen winnende trade — je stop was al geraakt.

### Features

Exact de feature-vector die al in `signals.features` staat. Dat is geen toeval: dit systeem
verzamelt vanaf dag één zijn eigen trainingsdata, **inclusief de negatieve gevallen** (elke
`AVOID` en elke afgewezen kandidaat). De meeste ML-projecten in dit domein stranden doordat men
alleen de trades bewaart die men nam — dan train je op een steekproef die je eigen filter al
heeft geselecteerd, en het model leert je bestaande bias in plaats van de markt.

### Correct trainen — de valkuilen die er hier toe doen

1. **Purged walk-forward CV met embargo.** Chronologische splits, en verwijder trainingssamples
   waarvan het label-venster (6 u) de validatieperiode binnenloopt. Zonder purging lekt de
   uitkomst; standaard k-fold is hier ronduit fout.
2. **Groepeer per token.** Meerdere signalen op hetzelfde token zijn geen onafhankelijke samples.
   Ze horen samen aan één kant van de split.
3. **Realistische kosten in het label.** Trainen op bruto returns levert een model dat gokt op
   marges die na fees en slippage negatief zijn.
4. **Survivorship bias.** Tokens die gedelist/verwijderd zijn moeten in de dataset blijven. Ze
   zijn de meerderheid en het zijn allemaal nullen.
5. **Klassenonbalans.** Positieven zijn zeldzaam. Gebruik PR-AUC en verwachte waarde, niet
   accuracy — een model dat altijd "nee" zegt haalt 95% accuracy en verdient niets.
6. **Kalibratie boven rangschikking.** We hebben een *kans* nodig om positiegrootte op te baseren,
   niet een ranglijst. Isotonic of Platt-kalibratie op een aparte holdout, en meet de Brier score.
7. **Begin simpel.** Gepenaliseerde logistische regressie op ~15 features. Dat is dezelfde
   functievorm als §5.5 — je vervangt letterlijk alleen de coëfficiënten. Gradient boosting pas
   als je aantoonbaar > 5.000 gelabelde signalen hebt en de lineaire versie duidelijk verslaat op
   out-of-sample walk-forward.
8. **Deploy als shadow-model.** Laat het nieuwe model minimaal een maand meelopen zonder
   handelsbevoegdheid en vergelijk zijn beslissingen met de heuristiek. Pas overstappen als het
   op *echte* nieuwe data beter is, niet op de backtest.

### Uitgebreide output

```
P(+20% binnen 6u vóór −20%)  = 0.31
P(−20% binnen 6u vóór +20%)  = 0.54
P(geen van beide)            = 0.15
```

Met die drie plus de verwachte payoff bereken je Kelly-fractionele positiegrootte in plaats van
een vaste 5%. Dat is de eigenlijke opbrengst van het ML-traject — niet betere signalen, maar
betere *sizing*. Bij zwaarstaartige uitkomsten is sizing waar het meeste geld gewonnen en
verloren wordt.
