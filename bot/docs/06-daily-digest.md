# 06 — Dagelijkse Telegram-push

Elke dag één bericht naar je Telegram-groep met wat de engine die dag heeft gezien.

---

## 0. Eerst: je token is een secret

Een Telegram bot-token heeft de vorm `<bot_id>:<secret>`, bijvoorbeeld
`8601006871:AAHG…`. Wie hem heeft, kan als jouw bot berichten sturen én lezen in elke chat
waar de bot in zit.

**Regels die dit project afdwingt in plaats van hoopt:**

- De token staat nooit in de repo. Hij komt uit een environment variable, en `.env` staat in
  `.gitignore`.
- De token komt nooit in een log. Telegram zet hem in het **URL-pad**
  (`api.telegram.org/bot<TOKEN>/sendMessage`), dus een niet-geredigeerde traceback lekt hem
  rechtstreeks in je CI-output — en die is op een publieke repo voor iedereen leesbaar.
  `alerts/telegram.py` haalt daarom elke foutmelding en elke `repr()` door `redact()`, en
  daar zit een test op.
- Is een token ooit in plaintext ergens beland (chat, ticket, screenshot): **intrekken via
  @BotFather → `/revoke`.** Een token die je niet meer vertrouwt, vervang je; je hergebruikt
  hem niet.

## 1. De `chat_id` vinden

Een token is een *afzender*, geen *bestemming*. Voor de groep heb je ook de `chat_id` nodig.

1. Voeg je bot toe aan de groep.
2. Post één willekeurig bericht in die groep.
3. Draai:

```bash
export TELEGRAM_BOT_TOKEN='...'
python -m fomo.cli telegram-chats
```

```
bot: @jouw_bot (id 8601006871)

         chat_id  type         title
------------------------------------------------------------
  -1001234567890  supergroup   FOMO alerts

Groups have a negative id; supergroups start with -100.
```

Werkt dit niet, dan is dat vrijwel altijd één van deze drie:

| Symptoom | Oorzaak |
|---|---|
| lege lijst | `getUpdates` geeft alleen *recente* updates — post opnieuw en probeer meteen daarna |
| lege lijst, blijft leeg | er staat een webhook ingesteld; `getUpdates` werkt dan niet |
| bot ziet groepsberichten niet | privacy mode staat aan — zet uit via @BotFather → `/setprivacy`, of maak de bot admin |

## 2. Lokaal testen

```bash
export TELEGRAM_BOT_TOKEN='...'
export TELEGRAM_CHAT_ID='-1001234567890'

python -m fomo.cli telegram-test     # één testbericht
python -m fomo.cli daily             # digest in de terminal, verstuurt niets
python -m fomo.cli seed-demo         # synthetische dag wegschrijven
python -m fomo.cli daily --send      # echt versturen
```

`seed-demo` schrijft een gesimuleerde dag in `data/` zodat je een volledig gevulde digest
kunt zien vóórdat er echte data is. Het is duidelijk gemarkeerd als demo; gooi `data/` weg
zodra je echte ingest hebt.

## 3. Dagelijks laten draaien

### Nu: GitHub Actions (geen server nodig)

`.github/workflows/daily-telegram.yml` staat er al. Instellen:

1. **Settings → Secrets and variables → Actions → New repository secret**
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
2. **Actions → Daily Telegram digest → Run workflow** om het één keer handmatig te testen.
   Vink `seed_demo` aan als je een gevulde digest wilt zien.

Het schema staat op `0 6 * * *` = **08:00 CEST / 07:00 CET**. GitHub-cron werkt uitsluitend
in UTC en volgt geen zomertijd, dus je kiest welk uur je belangrijker vindt. Scheduled runs
zijn bovendien *best effort* — ze kunnen enkele minuten te laat starten als het platform
druk is.

De workflow draait eerst de testsuite. Is de engine stuk, dan wordt er géén digest verstuurd —
een bericht met verkeerde cijfers is schadelijker dan geen bericht.

### Later: op de host waar de bot echt draait

Zodra fase 1–3 draait op een VPS, hoort de digest daar vandaan te komen — die machine heeft
de database. Systemd timer:

```ini
# /etc/systemd/system/fomo-digest.service
[Service]
Type=oneshot
User=fomo
WorkingDirectory=/opt/fomo/bot
EnvironmentFile=/etc/fomo/env      # chmod 600, root-owned
ExecStart=/opt/fomo/.venv/bin/python -m fomo.cli daily --send
```

```ini
# /etc/systemd/system/fomo-digest.timer
[Timer]
OnCalendar=*-*-* 08:00:00
Persistent=true                     # haalt een gemiste run in na downtime
[Install]
WantedBy=timers.target
```

`OnCalendar` gebruikt de tijdzone van de machine, dus die volgt wél gewoon zomertijd.

## 4. Wat er in het bericht staat

```
📊 FOMO daily digest — 15 Aug 2026
14 Aug 06:00 → 15 Aug 06:00 UTC · mode: paper

🔎 Signalen — 48 beoordeeld, 30 entry-waardig
• POTENTIAL_ENTRY: 30
• EARLY_WATCH: 12
• WATCH: 6

🏆 Beste kandidaten
• $TKN01 86/100 · POTENTIAL_ENTRY · N_eff 2.97
...

⛔ Meest voorkomende veto's
• LP_NOT_SECURED: 214
• MINT_AUTHORITY_ACTIVE: 96

🚩 False-positive flags
• CLUSTER_ILLUSION: 31

💼 Paper trades — 12 gesloten
• win rate 50% (6W / 6L)
• expectancy +5.28% per trade
• profit factor 1.23
• netto P&L $+289.00 (fees $46.57, slippage $35.55)
• exits: STOP_LOSS 6, TRAILING 6
• MFE +44.36% / MAE -20.20%

💰 Portefeuille $10,268.47 (+2.90% vandaag)
```

Drie ontwerpkeuzes die het bericht bruikbaar houden:

- **Het leaderboard toont het beste signaal per token**, niet de beste signalen. Een token
  dat elke tick opnieuw beoordeeld wordt, zou anders de hele lijst vullen met zichzelf.
- **De veto-telling staat er expliciet in.** Dat is je belangrijkste feedbackloop: zie je
  duizenden `LIQUIDITY_FLOOR`-veto's, dan staat je drempel te hoog of je watchlist te breed.
  Zonder dit meet je nooit of je filters te streng staan.
- **MFE en MAE staan erbij.** Hoge MFE met negatieve P&L betekent dat je stops te strak
  staan; hoge MFE die je niet pakt betekent dat je targets te ver staan. Zonder die twee
  kun je je exits niet tunen.

## 5. Als er nog niets te melden is

Zolang de ingest-laag (fase 1–3) niet draait, is er geen data en zegt het bericht dat
letterlijk:

> ⚪ **Geen signalen vandaag.**
> De beslissingsengine draait, maar er is nog geen live data-ingest (roadmap fase 1–3).

Dat is een bewuste keuze. Een dagelijkse push die activiteit verzint om nuttig te lijken,
leert je hem te negeren — en dan is hij slechter dan geen push. Het lege bericht heeft
bovendien een echte functie: het bewijst elke dag dat de pijplijn nog leeft. Blijft hij weg,
dan is er iets stuk.

## 6. Storingen

| Symptoom | Oorzaak / oplossing |
|---|---|
| `chat not found` | verkeerde `chat_id`, of bot uit de groep verwijderd |
| `bot was blocked by the user` | bij privéchats: de gebruiker heeft de bot geblokkeerd |
| `Forbidden: bot is not a member` | bot opnieuw toevoegen aan de groep |
| bericht komt niet aan, geen fout | verkeerde chat — groepen zijn negatief, `-100…` voor supergroups |
| `can't parse entities` | HTML-fout in de tekst; `split_message()` splitst op regelgrenzen om dit te voorkomen |
| 429 | rate limit; de client respecteert `retry_after` automatisch |
| workflow draait niet | scheduled workflows worden op GitHub uitgezet na 60 dagen zonder repo-activiteit |

Die laatste is de meest voorkomende stille storing: **GitHub schakelt scheduled workflows
automatisch uit als er 60 dagen geen commits in de repo zijn.** Merk je dat de digest is
gestopt, kijk daar eerst.
