# 02 — Databaseschema & environment variables

PostgreSQL 16 + TimescaleDB. Alles in UTC, altijd `timestamptz` — nooit `timestamp`.
Bedragen in USD als `numeric(24,8)`; **nooit floats voor geld**. Token-hoeveelheden als
`numeric(40,0)` in ruwe base units, want memecoins hebben regelmatig 9 decimalen en absurde
supplies die een float stilletjes afrondt.

---

## Ontwerpprincipes

1. **Feiten zijn append-only.** `transactions`, `tweets`, `token_metrics`, `signals` worden nooit
   ge-`UPDATE`. Wil je "de huidige stand", dan is dat een view of een aparte state-tabel. Reden:
   backtesting zonder look-ahead bias vereist dat je exact kunt reconstrueren wát je wist en
   *wanneer* je het wist.
2. **Elke afgeleide rij draagt de config-versie** waarmee hij berekend is (`weights_version`).
   Zonder dat zijn scores uit verschillende weken onvergelijkbaar.
3. **Twee tijdstempels op alles wat van buiten komt:** `observed_at` (wanneer wij het zagen) en
   `event_at` (wanneer het gebeurde). Het verschil is je werkelijke latency, en het is de enige
   manier om in de backtest te weten wat op moment T beschikbaar wás.

---

## Tabellen

### `users`
| kolom | type | opmerking |
|---|---|---|
| `id` | `uuid pk` | |
| `email` | `citext unique not null` | |
| `password_hash` | `text` | argon2id |
| `role` | `text` | `admin` / `viewer` |
| `telegram_chat_id` | `text` | voor persoonlijke alerts |
| `created_at` | `timestamptz` | |

### `traders`
De logische entiteit "een persoon/desk die we volgen". Eén trader kan meerdere wallets hebben —
dat onderscheid is essentieel voor de cluster-correctie.

| kolom | type | opmerking |
|---|---|---|
| `id` | `uuid pk` | |
| `nickname` | `text not null` | |
| `notes` | `text` | waarom volgen we deze |
| `source` | `text` | `manual` / `discovered` / `leaderboard` |
| `is_active` | `boolean` | |
| `created_at` | `timestamptz` | |

### `wallets`
| kolom | type | opmerking |
|---|---|---|
| `id` | `uuid pk` | |
| `trader_id` | `uuid fk → traders` **nullable** | null = wallet die we volgen maar nog niet aan een persoon koppelen |
| `address` | `text not null` | |
| `chain` | `text not null` | `solana`, `base`, … |
| `label` | `text` | |
| `first_seen_at` / `last_active_at` | `timestamptz` | |
| `is_bot_suspected` | `boolean` | door de MM-bot-detectie gezet |
| `cluster_id` | `uuid` **nullable** | zie `wallet_clusters` |

```sql
UNIQUE (chain, address);
INDEX ON wallets (trader_id);
INDEX ON wallets (cluster_id);
```

### `wallet_clusters` + `wallet_links`
De basis onder "vijf wallets van één persoon tellen niet als vijf signalen".

`wallet_clusters`: `id`, `label`, `confidence numeric(4,3)`, `method` (`funding_graph` /
`timing_correlation` / `cooccurrence` / `manual`), `updated_at`.

`wallet_links`: `wallet_a`, `wallet_b`, `link_type` (`funded_by` / `co_buy` / `same_cex_deposit` /
`timing`), `strength numeric(4,3)`, `evidence jsonb`, `observed_at`.

```sql
PRIMARY KEY (wallet_a, wallet_b, link_type);   -- wallet_a < wallet_b afdwingen via CHECK
INDEX ON wallet_links (wallet_b);
```

> `wallet_links` is een ongerichte graaf; de CHECK-constraint op de sortering voorkomt dat je
> elke kant apart opslaat en later dubbeltelt.

### `trader_stats`
Snapshot van berekende statistieken, niet live-berekend. Herberekening is een batchjob.

| kolom | type | opmerking |
|---|---|---|
| `trader_id` | `uuid fk` | |
| `computed_at` | `timestamptz` | |
| `window` | `text` | `30d` / `90d` / `all` — statistieken zijn window-afhankelijk |
| `n_trades` | `int` | **effectieve steekproefgrootte, cruciaal** |
| `n_wins` | `int` | |
| `win_rate_raw` | `numeric(5,4)` | |
| `win_rate_shrunk` | `numeric(5,4)` | Bayesiaans, zie `04 §2` |
| `median_multiple` | `numeric(12,4)` | mediaan, geen gemiddelde — de staart is extreem |
| `mean_log_return` | `numeric(12,6)` | |
| `pnl_concentration` | `numeric(5,4)` | aandeel van totale winst uit de beste trade |
| `avg_hold_seconds` | `int` | |
| `median_entry_mcap_usd` | `numeric(24,2)` | |
| `median_entry_age_seconds` | `int` | hoe vroeg stapt hij in |
| `exit_quality` | `numeric(5,4)` | mediaan (exitprijs / piekprijs binnen holdperiode) |
| `bot_likeness` | `numeric(5,4)` | |
| `churn_rate` | `numeric(10,4)` | trades per dag |
| `score` | `numeric(5,2)` | 0–100 |
| `reliability` | `text` | `HIGH` / `MEDIUM` / `LOW` / `UNPROVEN` |
| `weights_version` | `text` | |

```sql
PRIMARY KEY (trader_id, window, computed_at);
INDEX ON trader_stats (trader_id, window, computed_at DESC);
```

### `tokens`
| kolom | type | opmerking |
|---|---|---|
| `id` | `uuid pk` | |
| `chain` / `address` | `text` | `UNIQUE (chain, address)` |
| `symbol` / `name` | `text` | **niet uniek en niet te vertrouwen** — zie waarschuwing hieronder |
| `decimals` | `smallint` | |
| `launchpad` | `text` | `pumpfun` / `bonkfun` / `raydium_direct` / … |
| `created_at_chain` | `timestamptz` | launch-tijd; basis voor elke leeftijdsberekening |
| `creator_address` | `text` | |
| `migrated_at` | `timestamptz` | wanneer naar de hoofd-DEX gemigreerd |
| `total_supply` | `numeric(40,0)` | |
| `first_seen_at` | `timestamptz` | |

```sql
UNIQUE (chain, address);
INDEX ON tokens (upper(symbol));         -- ticker-lookup voor social matching
INDEX ON tokens (created_at_chain DESC);
INDEX ON tokens (creator_address);       -- serial-rugger-detectie
```

> ⚠️ **Ticker-collisions zijn bij memecoins de regel, niet de uitzondering.** Er zijn op elk
> moment tientallen tokens die `$MOON` heten. Het **contract address is de enige echte sleutel**.
> Alle social-matching op ticker is inherent ambigu en moet gedisambigueerd worden — zie
> `04 §3.4`. Een systeem dat tickers als identiteit gebruikt, koopt vroeg of laat het verkeerde
> token; dat is een van de meest voorkomende manieren waarop dit soort bots geld verliest.

### `token_security`
De risk-feiten. Apart van `tokens` omdat ze over tijd veranderen en we de historie willen.

| kolom | type |
|---|---|
| `token_id` `uuid fk`, `observed_at` `timestamptz` | |
| `mint_authority_revoked` | `boolean` |
| `freeze_authority_revoked` | `boolean` |
| `lp_burned_pct` | `numeric(5,2)` |
| `lp_locked_until` | `timestamptz` |
| `top10_holder_pct` | `numeric(5,2)` (excl. LP en bekende burn-adressen) |
| `creator_holding_pct` | `numeric(5,2)` |
| `bundled_supply_pct` | `numeric(5,2)` (in het launch-blok gekochte supply) |
| `sniper_wallet_count` | `int` |
| `transfer_fee_bps` | `int` (Token-2022) |
| `has_transfer_hook` | `boolean` |
| `honeypot_sim_passed` | `boolean` |
| `buy_tax_bps` / `sell_tax_bps` | `int` |
| `provider_flags` | `jsonb` (ruwe respons van de security-provider) |
| `source` | `text` |

```sql
PRIMARY KEY (token_id, observed_at);
```

### `token_metrics` — **hypertable**
Tijdreeks van marktdata, 1-minuutbuckets.

`token_id`, `bucket timestamptz`, `price_usd`, `mcap_usd`, `fdv_usd`, `liquidity_usd`,
`volume_1m_usd`, `buy_count`, `sell_count`, `unique_buyers`, `unique_sellers`, `holders`.

```sql
SELECT create_hypertable('token_metrics', 'bucket', chunk_time_interval => INTERVAL '1 day');
PRIMARY KEY (token_id, bucket);
-- compressie na 7 dagen; memecoin-tijdreeksen zijn na een week alleen nog voor backtest nuttig
ALTER TABLE token_metrics SET (timescaledb.compress, timescaledb.compress_segmentby = 'token_id');
```

`unique_buyers` apart van `buy_count` is essentieel: 500 buys van 4 wallets is wash trading,
500 buys van 400 wallets is echte vraag. Zie `04 §5`.

### `transactions` — **hypertable**
Alleen transacties van gevolgde wallets, ná classificatie.

| kolom | type |
|---|---|
| `id` `uuid`, `signature` `text`, `event_at`, `observed_at` | |
| `wallet_id` `uuid fk`, `token_id` `uuid fk` | |
| `action` | `text` — `OPEN`/`ADD`/`TRIM`/`CLOSE`/`SELF_TRANSFER`/`MM_BOT`/`AIRDROP_DUST` |
| `side` | `text` — `buy`/`sell` |
| `amount_token` | `numeric(40,0)` |
| `amount_usd` | `numeric(24,8)` |
| `price_usd` | `numeric(24,12)` — memecoins hebben prijzen als 0.000000041 |
| `mcap_at_tx_usd` | `numeric(24,2)` |
| `liquidity_at_tx_usd` | `numeric(24,2)` |
| `token_age_seconds` | `int` |
| `pct_of_wallet_value` | `numeric(5,2)` |
| `raw` | `jsonb` |

```sql
SELECT create_hypertable('transactions', 'event_at', chunk_time_interval => INTERVAL '1 day');
UNIQUE (signature, wallet_id, token_id);   -- idempotente ingest: webhooks leveren dubbel
INDEX ON transactions (token_id, event_at DESC);
INDEX ON transactions (wallet_id, event_at DESC);
INDEX ON transactions (action, event_at DESC) WHERE action IN ('OPEN','ADD');
```

De UNIQUE-constraint is niet cosmetisch: **webhook-providers leveren gegarandeerd dubbele
events** bij retries. Zonder dit telt één aankoop als drie traders.

### `twitter_accounts`
`id`, `x_user_id` (unique — de handle verandert, het id niet), `handle`, `followers`,
`following`, `account_created_at`, `is_verified`, `bio`, `quality_score numeric(5,2)`,
`category` (`researcher`/`trader`/`influencer`/`project`/`meme`/`bot`/`shill`/`giveaway`),
`bot_probability numeric(4,3)`, `historical_accuracy numeric(4,3)`, `last_profiled_at`.

> Sla **`x_user_id`** op als sleutel, niet de handle. Shill-accounts hernoemen zichzelf continu
> om reputatie te ontlopen; het numerieke id doet dat niet.

### `tweets` — **hypertable**
`id`, `x_tweet_id unique`, `author_id fk`, `posted_at`, `observed_at`, `text`, `lang`,
`like_count`, `retweet_count`, `reply_count`, `quote_count`, `is_retweet`, `is_reply`,
`text_hash` (voor dedupe van copy-paste-shills), `matched_token_ids uuid[]`, `raw jsonb`.

```sql
INDEX ON tweets USING gin (matched_token_ids);
INDEX ON tweets (text_hash, posted_at DESC);   -- identieke tekst van N accounts = coördinatie
```

### `tweet_analysis`
Output van de AI-laag, 1-op-1 met `tweets`. Apart omdat we willen kunnen herclassificeren met
een nieuw model zonder de ruwe data te raken.

`tweet_id`, `token_id`, `sentiment numeric(4,3)`, `relevance`, `hype`, `credibility`,
`intent text`, `category text`, `confidence numeric(4,3)`, `model text`, `prompt_version text`,
`analyzed_at`.

### `social_metrics` — **hypertable**
Geaggregeerd per token per minuut — dit is wat de scoring leest, niet de losse tweets.

`token_id`, `bucket`, `mentions`, `unique_accounts`, `quality_weighted_mentions numeric(12,4)`,
`account_entropy numeric(6,4)`, `engagement_total`, `avg_sentiment`, `bot_share numeric(4,3)`,
`duplicate_text_share numeric(4,3)`, `baseline_ewma numeric(12,4)`, `baseline_mad numeric(12,4)`,
`velocity_z numeric(8,3)`.

### `signals`
Append-only beslissingslog. **De belangrijkste tabel voor het verbeteren van het systeem.**

| kolom | type | opmerking |
|---|---|---|
| `id` `uuid pk`, `token_id fk`, `created_at` | | |
| `state` | `text` | `WATCH`/`EARLY_WATCH`/`POTENTIAL_ENTRY`/`STRONG_ENTRY`/`EXIT`/`AVOID` |
| `master_score` | `numeric(5,2)` | |
| `smart_money_score` … `risk_score` | `numeric(5,2)` | de vijf deelscores |
| `veto` | `boolean` |
| `veto_reasons` | `text[]` |
| `soft_risk_multiplier` / `timing_multiplier` | `numeric(4,3)` |
| `n_traders_raw` / `n_traders_effective` | `int` / `numeric(6,3)` |
| `features` | `jsonb` | **de volledige feature-vector** |
| `explanation` | `text` | mensleesbaar |
| `false_positive_flags` | `text[]` |
| `weights_version` | `text` |

```sql
INDEX ON signals (token_id, created_at DESC);
INDEX ON signals (state, created_at DESC);
INDEX ON signals (master_score DESC, created_at DESC);
```

> `features jsonb` is niet optioneel. Dit ís je trainingsset voor fase 10. Elke beslissing die
> je neemt zonder de inputs op te slaan, is een datapunt dat je permanent kwijt bent.

### `positions` / `paper_trades`
`positions` = open state, `paper_trades` = afgesloten trades (append-only).

`positions`: `id`, `token_id`, `signal_id`, `opened_at`, `entry_price`, `entry_price_effective`
(ná slippage), `size_usd`, `size_token`, `stop_loss`, `take_profit_levels numeric[]`,
`trailing_pct`, `high_water_price`, `status`, `mode` (`paper`/`live`).

`paper_trades`: `position_id`, `closed_at`, `exit_price`, `exit_price_effective`, `exit_reason`
(`TAKE_PROFIT`/`STOP_LOSS`/`TRAILING`/`SIGNAL_INVALIDATED`/`EMERGENCY`/`TIMEOUT`),
`gross_pnl_usd`, `fees_usd`, `slippage_cost_usd`, `net_pnl_usd`, `return_pct`,
`max_favorable_excursion`, `max_adverse_excursion`, `hold_seconds`.

MFE/MAE zijn geen luxe: ze vertellen je of je stops te strak staan (hoge MFE, negatieve PnL) of
je targets te ver (hoge MFE die je niet pakt). Zonder deze twee kolommen kun je exits niet tunen.

### `risk_events`
`id`, `token_id`, `detected_at`, `rule_id`, `severity` (`veto`/`high`/`medium`/`low`),
`value numeric`, `threshold numeric`, `detail jsonb`.
Elke veto en elke soft-penalty wordt hier gelogd — ook (juist!) als het signaal daardoor
gedropt is. Anders kun je nooit meten of je filters te streng staan.

### `alerts`
`id`, `signal_id`, `channel`, `sent_at`, `status`, `dedupe_key`, `payload jsonb`.
`UNIQUE (dedupe_key)` waar `dedupe_key = token_id:state:time_bucket` — voorkomt dat één token
je Telegram vult.

### `performance`
Dagelijkse rollup: `date`, `mode`, `n_trades`, `win_rate`, `avg_win_pct`, `avg_loss_pct`,
`expectancy_pct`, `profit_factor`, `sharpe`, `sortino`, `max_drawdown_pct`, `roi_pct`,
`avg_hold_seconds`, `equity_end_usd`, `weights_version`.

---

## Bewaarbeleid

| Data | Bewaartermijn | Reden |
|---|---|---|
| `transactions`, `signals`, `paper_trades` | onbeperkt | dit is de trainingsset |
| `token_metrics` | 90 d hot, daarna gecomprimeerd | 1m-data van dode tokens is alleen backtest-materiaal |
| `tweets.raw` | 30 d | volume is enorm; de geanalyseerde velden blijven wel |
| `tweet_analysis` | onbeperkt | klein en waardevol |
| tokens met `liquidity < $1k` en > 7 d oud | archiveren | het overgrote deel van alle rijen |

---

## Environment variables

```bash
# ── Core ─────────────────────────────────────────────────────────────
FOMO_ENV=development                  # development | staging | production
FOMO_LOG_LEVEL=INFO
FOMO_TRADING_MODE=paper               # paper | live  — 'live' vereist ook FOMO_LIVE_CONFIRMED
FOMO_LIVE_CONFIRMED=false             # tweede, opzettelijk omslachtige schakelaar
FOMO_WEIGHTS_VERSION=v1

# ── Database & cache ─────────────────────────────────────────────────
DATABASE_URL=postgresql+asyncpg://fomo:fomo@localhost:5432/fomo
REDIS_URL=redis://localhost:6379/0

# ── Blockchain (read-only!) ──────────────────────────────────────────
SOLANA_RPC_URL=
SOLANA_RPC_WS_URL=
HELIUS_API_KEY=
HELIUS_WEBHOOK_SECRET=                # HMAC-verificatie van inkomende webhooks

# ── Markt- & tokendata ───────────────────────────────────────────────
BIRDEYE_API_KEY=
DEXSCREENER_BASE_URL=https://api.dexscreener.com
GECKOTERMINAL_BASE_URL=https://api.geckoterminal.com/api/v2

# ── Token-security ───────────────────────────────────────────────────
RUGCHECK_API_KEY=
GOPLUS_APP_KEY=
GOPLUS_APP_SECRET=

# ── Social ───────────────────────────────────────────────────────────
X_BEARER_TOKEN=
X_API_TIER=basic

# ── AI ───────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY=
FOMO_LLM_MODEL=claude-sonnet-5
FOMO_LLM_MAX_DAILY_USD=25             # harde spend-cap; de classifier valt terug op
                                      # de deterministische heuristiek als hij eroverheen gaat

# ── Alerts ───────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
DISCORD_WEBHOOK_URL=

# ── API / dashboard ──────────────────────────────────────────────────
FOMO_API_HOST=127.0.0.1               # niet 0.0.0.0 zonder auth ervoor
FOMO_API_PORT=8000
FOMO_JWT_SECRET=
FOMO_CORS_ORIGINS=http://localhost:3000

# ── Paper trading ────────────────────────────────────────────────────
FOMO_PAPER_START_EQUITY_USD=10000
FOMO_PAPER_MAX_POSITION_PCT=5         # max % van equity per positie
FOMO_PAPER_MAX_CONCURRENT=8
FOMO_PAPER_MAX_DAILY_LOSS_PCT=15      # circuit breaker

# ── Live trading (fase 10, ANDER proces, ANDERE host) ────────────────
# Deze staan bewust NIET in dit project. De execution service leest ze zelf uit KMS.
# EXECUTION_SERVICE_URL=
# EXECUTION_MTLS_CERT_PATH=
```

**Regel:** `FOMO_TRADING_MODE=live` alléén is niet genoeg — er is een tweede vlag
(`FOMO_LIVE_CONFIRMED`) plus een aparte service nodig. Eén verkeerde env-var mag nooit
echt geld kunnen uitgeven.
