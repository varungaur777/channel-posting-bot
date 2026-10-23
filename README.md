# Channel Posting Bot 🤖📢

Telegram bot jo authorized admins se DM mein post leta hai, private **approval group** mein approve/reject
karwata hai, aur approved posts ko **target channel** mein ek random gap (default 10–15 min) ke saath
publish karta hai. Sab kuch `copyMessage` se hota hai, isliye kahin bhi "Forwarded from" nahi aata.

- Telegram ka private **storage channel** = files ka permanent store (source of truth)
- **MongoDB** = sirf index (file_id, message ids, status) – files kabhi DB mein nahi jaati
- **GitHub Actions** pe 24/7 (har ~6 ghante auto-restart), ya Docker / systemd pe VPS

> ⚠️ **Zaroori note (GitHub Actions):** GitHub ke terms ke hisaab se Actions repo ke build/test/deploy ke
> liye hain, 24/7 hosting ke liye nahi. Bot chal jayega, lekin GitHub kabhi bhi aise long-running workflows
> ko rok sakta hai ya account pe action le sakta hai. Long-term ke liye sasta VPS + Docker
> ([deploy/](deploy/)) sabse safe option hai.

Flow aur internals: [ARCHITECTURE.md](ARCHITECTURE.md)

---

## Defaults (jo maine chune hain)

| Setting | Default |
|---|---|
| Interval | random 10–15 min (`MIN_INTERVAL_MIN` / `MAX_INTERVAL_MIN`, ya `/setinterval`) |
| Pehla post | turant publish |
| Album wait | 2.5 s (`ALBUM_WAIT_SECONDS`) |
| Scheduler tick | 5 s |
| Retries | network error pe 5 retry, backoff 1, 2, 4, 8, 16 min (max 30) |
| Duplicate window | 30 din (`DUP_WINDOW_DAYS`) |
| Time zone | Asia/Kolkata (DB mein UTC) |
| DB name | `channel_poster_bot` |
| Lease | heartbeat 30 s, expiry 2 min |
| GitHub run | 340 min bot, 355 min job timeout, phir next run |
| Python | 3.12, python-telegram-bot 21.11.1, Motor 3.7 |
| `/setinterval` | DB mein save hota hai aur env values ko override karta hai |

---

## Setup – step by step

### 1. BotFather se bot banao
1. Telegram mein [@BotFather](https://t.me/BotFather) kholo → `/newbot` → naam aur username do.
2. Jo **token** mile (`123456:ABC...`) wo `BOT_TOKEN` hai. Kisi ko mat dikhana.
3. (Optional) `/setprivacy` → Enable rehne do. Bot ko commands group mein phir bhi milti hain.

### 2. MongoDB Atlas (naya, free)
1. <https://cloud.mongodb.com> pe login → **New Project** (is bot ke liye alag project).
2. **Create cluster** → **M0 Free** → koi bhi region (Mumbai `ap-south-1` accha hai).
3. **Database Access** → *Add New Database User* → username + strong password (password mein `@ : / ?` ho to
   URL-encode karna padega, isliye sirf letters/numbers rakho).
4. **Network Access** → *Add IP Address* → `0.0.0.0/0` (GitHub Actions ka IP fixed nahi hota).
5. **Connect** → *Drivers* → Python → connection string copy karo, `<password>` replace karo:
   `mongodb+srv://user:pass@cluster0.xxxxx.mongodb.net/?retryWrites=true&w=majority` → ye `MONGO_URL` hai.

### 3. Group aur channels banao
1. **Approval group** (private): New Group banao. Pehle hi **supergroup** bana lo (Group settings →
   *Chat history for new members* → **Visible** kar do; isse group supergroup ban jata hai) – kyunki upgrade pe
   group ki ID badal jaati hai. Bot ko group mein add karo. Approvers ko bhi add karo.
2. **Storage channel** (private): New Channel → Private. Bot ko **admin** banao, sirf ye permissions:
   ✅ *Post Messages* ✅ *Delete Messages* (baaki off). Yahan aur kisi ko admin mat banao.
3. **Target channel**: jahan posts jaane hain. Bot ko admin banao: ✅ *Post Messages* ✅ *Delete Messages*.

### 4. IDs nikalo (`tools/get_ids.py`)
```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env        # BOT_TOKEN bhar do
python tools/get_ids.py     # 180 sec tak sunta hai
```
Jab tak ye chal raha hai:
- approval group mein `/id` bhejo,
- storage channel mein kuch bhi post karo,
- target channel mein kuch bhi post karo (baad mein delete kar dena).

Script har chat ki `ID  TYPE  TITLE` print karegi. `-100...` wali IDs `.env` mein daalo:
`APPROVAL_GROUP_ID`, `STORAGE_CHANNEL_ID`, `CHANNEL_ID`. Apna user ID bot ko DM mein `/id` bhej ke milta hai.

> Bot chal raha ho to bhi IDs mil sakti hain: group mein `/id` bhejo, ya channel mein `/id` post karo – bot
> owner ko DM mein channel ID bhej dega aur `/id` post delete kar dega.

### 5. Naye users ko access dena (bina GitHub secret badle)
Unhe channel join karne ki zaroorat **nahi** hai.
1. Naya user bot ko DM mein `/start` (ya koi bhi message) bheje.
2. Owner ko turant DM aata hai: **🙋 Access request** – naam, @username, ID ke saath, aur buttons:
   **✅ Post access** · **👮 Approver** · **🤝 Dono** · **❌ Ignore**. Ek tap – ho gaya.
3. User ko message jata hai ki access mil gaya; ab wo DM mein post bhej sakta hai.
4. Ya command se: `/adduser <user_id> [post|approve|both]`, hatane ke liye `/removeuser <user_id>`, list: `/users`.

Ye access MongoDB mein save hota hai (restart pe bhi rehta hai). `SUBMITTER_IDS` / `APPROVER_IDS` secrets
wale users bhi chalte rehte hain – unhe hatane ke liye secret hi badalna padega.
Approver ko approval group ka member bhi hona chahiye (buttons wahi hain).

### 6. Local test
```bash
python -m bot               # Ctrl+C se band
pytest -q && ruff check .   # tests + lint
```

### 7. GitHub Actions pe 24/7
1. Repo **public** rakho (private repo ko sirf 2000 free minutes/month milte hain).
2. Settings → *Secrets and variables* → *Actions* → **Secrets**: `BOT_TOKEN`, `OWNER_ID`, `SUBMITTER_IDS`,
   `APPROVER_IDS`, `APPROVAL_GROUP_ID`, `STORAGE_CHANNEL_ID`, `CHANNEL_ID`, `MONGO_URL`, `MONGO_DB_NAME`.
   **Variables** (optional): `MIN_INTERVAL_MIN`, `MAX_INTERVAL_MIN`, `TZ_NAME`, `NOTIFY_RESTART`.
   CLI se: `gh secret set BOT_TOKEN` (value paste karo), `gh variable set TZ_NAME --body Asia/Kolkata`.
3. Settings → Actions → General → *Workflow permissions* → **Read and write** (self-restart ke liye).
4. Actions → **bot** → *Run workflow*. Ya: `gh workflow run bot.yml`.

### Logs kaise dekhein
- GitHub: Actions → **bot** → running run → *Run the bot* step. CLI: `gh run list --workflow bot.yml`,
  phir `gh run view <id> --log` (ya `gh run watch`).
- Local/VPS: terminal + `logs/bot.log` (rotating). Docker: `docker compose -f deploy/docker-compose.yml logs -f`.

### Bot band kaise karein
1. Actions → **bot** → `...` → **Disable workflow** (`gh workflow disable bot.yml`)
2. Phir chal raha run **Cancel** karo (`gh run cancel <id>`). Cancelled run agla run start nahi karta.

Wapas chalu: `gh workflow enable bot.yml && gh workflow run bot.yml`.

### Code update kaise karein
`main` pe push karo → naya run queue mein lag jata hai (concurrency ki wajah se purana chalta rehta hai).
Turant naya code chahiye to purana run cancel kar do – queued run naye code ke saath start hoga.
Bot ko SIGTERM milta hai, wo gracefully band hota hai aur lease chhod deta hai.

---

## Commands

Commands bot ke DM aur approval group dono mein chalti hain (permission check ke saath).

| Command | Kaun | Example / kaam |
|---|---|---|
| `/start`, `/help` | sab | usage (Hinglish). Unauthorized user ko uska user ID dikhta hai |
| `/id` | sab | `/id` → chat ID + aapka user ID |
| `/stats` | staff | har status ka count, interval, next slot, paused? |
| `/queue` | staff | scheduled posts order mein, ETA ke saath |
| `/list <status> [n]` | staff | `/list scheduled 20`, `/list failed`, `/list all` |
| `/post <id>` | staff | `/post 12` → details, retries, last error; DM mein storage se preview bhi |
| `/approve <id>` | approvers | `/approve 12` |
| `/reject <id> [reason]` | approvers | `/reject 12 image blur hai` – reason submitter ko jata hai |
| `/changes <id> <notes>` | approvers | `/changes 12 date galat hai, 25 Oct karo` – reject ki jagah submitter ko changes bhejo; wo naya post bhejega |
| `/caption <id> <text>` | approvers | caption (ya text post ka text) badlo; `/caption 12 off` = original wapas |
| `/schedule <id> <time>` | approvers | `/schedule 12 18:30`, `/schedule 12 25-10 09:00`, `/schedule 12 +90`, `/schedule 12 +2h`, `off`. Fixed time pe jayega (gap aur quiet hours ignore) |
| `/top <id>` | approvers | scheduled post ko queue mein sabse aage |
| `/report` | staff | last 24 ghante ki summary (owner ko roz 22:00 baje apne aap) |
| `/quiethours <start> <end>` | owner | `/quiethours 23 7` – raat 11 se subah 7 tak regular posting band; `off` |
| `/setfooter <text>` | owner | `/setfooter Join @mychannel` – har caption/text ke neeche; `off` |
| `/postnow <id>` | approvers | `/postnow 12` – gap skip karke **turant** publish (pending ho to pehle approve). Pause aur backoff bhi ignore hota hai. Agla regular post iske baad normal gap pe jayega |
| `/editpost <id> <caption>` | submitter (apna post) / approvers (koi bhi) | caption badlo – **publish ke baad bhi**, channel mein seedha edit hota hai, approval nahi chahiye. `/editpost 12 remove` = caption hatao, `/editpost 12 off` = original |
| `/replyto <id ya link>` | submitters | `/replyto 12` ya `/replyto https://t.me/spitting_fact/123` – **agla post channel mein us post ka reply** banke jayega. `/replyto off`. Waiting post badalne ke liye: `/replyto 12 15` |
| `/approval on\|off` | owner | `off` = naye posts bina approval ke seedhe queue mein (group mein phir bhi dikhte hain, Cancel/Post Now kar sakte ho) |
| `/setbutton <text> \| <url>` | owner | har post ke neeche button(s), ek line = ek button. **Default: koi button nahi.** `/setbutton join` = "📢 Join <channel>" button, `off` = band |
| `/clearbuttons [n]` | owner | pehle se published last n (default 50) posts se buttons hatao |
| `/preview <id>` | staff | DM mein dekho post channel mein **bilkul kaisa dikhega** (formatting, footer, buttons ke saath) |
| `/leaderboard [days]` | staff | kis admin ne kitne post bheje / publish / reject (default 30 din) |
| `/format both\|bold\|quote\|off` | owner | caption auto-format: **pehli line bold** (headline) + **baaki quote block** mein. Default `both`. Sirf multi-line captions badalte hain |
| `/cancel <id>` | owner / post ka submitter | sirf pending/scheduled post |
| `/retry <id>` | owner | failed / needs_review post wapas queue mein |
| `/pause`, `/resume` | owner | publishing roko/chalu karo, queue safe rehti hai |
| `/setinterval <min> <max>` | owner | `/setinterval 20 30` – agle publish ke baad se lagu |
| `/adduser <user_id> [post\|approve\|both]` | owner | `/adduser 123456789` (post), `/adduser 123456789 approve`. Group mein kisi ke message pe reply karke `/adduser approve` bhi chalta hai |
| `/removeuser <user_id> [post\|approve\|both]` | owner | bot se diya hua access hatao (default: dono) |
| `/users` | owner | kis kis ke paas access hai (bot se diye + env/secret wale) |

Status: ⏳ pending · 🗓 scheduled · 🚀 publishing · ✅ published · ❌ rejected · 🚫 cancelled · ⚠️ failed ·
🔍 needs_review

**Post bhejte hi DM mein preview:** bot dikhata hai post channel mein kaisa lagega, aur neeche buttons:
**✏️ Edit caption** (bot ke message pe reply karke naya caption) · **🧹 Remove caption** · **↩️ Original caption** ·
**🚫 Cancel post**. Approve/publish se pehle wahin theek kar lo.

**🤖 AI caption helper (abhi OFF – key lagane pe chalu):** DM preview ke neeche **✨ Improve (AI)** ·
**🌐 Translate** (Hindi↔English) · **#️⃣ Hashtags** buttons aate hain – AI 2-3 caption options deta hai, **✅ Use 1/2/3**
dabao to wahi lag jata hai. Chalu karne ke liye:
1. https://console.anthropic.com pe account banao → **API Keys** → *Create Key* → copy (thoda credit add karna padega).
2. `gh secret set ANTHROPIC_API_KEY -R varungaur777/channel-posting-bot` (key paste karo, Enter) – ya GitHub → Settings →
   Secrets → *New repository secret* → naam `ANTHROPIC_API_KEY`.
3. Bot restart: current run cancel karo aur `gh workflow run bot.yml` (ya agle 6-ghante restart ka wait).
Default model `claude-opus-5-5`; doosra chahiye to Variable `AI_MODEL` (jaise `claude-sonnet-5-5`). Key hatao = feature band.

**📢 Do (ya zyada) channels:** `/addchannel @username` se doosra channel jodo (pehle bot ko wahan admin banao:
Post + Edit + Delete Messages). Phir approval group mein har post pe har channel ka button aata hai –
**✅ 𝙎𝙋𝙄𝙏𝙏𝙄𝙉𝙂 𝙁𝘼𝘾𝙏 · ✅ The GeoSight · ✅ Both channels** – jo dabao, post wahin jata hai (gap/queue same rehti hai).
Command se: `/approve 12 all`, `/approve 12 @GeoSight`. Ek channel fail ho to sirf wahi dobara try hota hai (duplicate nahi).
`/editpost`, `/replyto` aur `/clearbuttons` sab channels pe chalte hain. List: `/channels`, hatao: `/removechannel @username`.
Bina choice ke (`/approve 12`, ⚡ Post Now on a pending post, `/approval off`) post sirf main channel (`CHANNEL_ID`) mein jata hai.

**Har channel ki apni settings:**
- **Gap:** `/setinterval 30 35 @GeoSight` – har channel apne gap se chalta hai. "Both" wala post pehle jis channel ka slot
  khali ho wahan jata hai, doosre mein uske apne gap ke baad. `/stats` mein har channel ka next slot dikhta hai.
- **Footer:** `/setfooter @GeoSight Join for more @GeoSight` – sirf us channel ke liye. `/setfooter <text>` = baaki sab ke
  liye. `/setfooter @GeoSight off` = us channel mein footer nahi, `/setfooter @GeoSight default` = general footer.

**Approval group ke buttons:**
- Pending post: **✅ Approve** · **❌ Reject** · **✏️ Request Changes** · **📝 Edit Caption** · **⚡ Approve & Post Now**
- Scheduled post: **⚡ Post Now** · **🕒 Schedule** · **⬆️ Move to Top** · **📝 Edit Caption** · **🚫 Cancel**
- Request Changes / Edit Caption / Schedule dabane pe bot ek message bhejta hai – **us message pe reply** karke likho
  (changes, naya caption, ya time jaise `18:30`).
- Koi post 1 ghante se pending ho to group mein reminder aata hai (`PENDING_REMINDER_MIN`, 0 = off).

Bot ke saare messages English mein hain; ye README Hinglish mein hai.

**Post kaise bhejein:** bot ko DM mein kuch bhi bhejo – text (formatting ke saath), photo, video, GIF,
document, audio, voice, video note, sticker, poll, location, album. Album ek hi post banta hai.
Bhejne ke baad edit karna track nahi hota – `/cancel <id>` karke dobara bhejo.

---

## Troubleshooting

| Problem | Hal |
|---|---|
| **409 Conflict** (`terminated by other getUpdates request`) | Bot ki do copies chal rahi hain (GitHub + local, ya `get_ids.py`). Ek band karo. Lease 2 min mein free ho jata hai; doosri copy apne aap wait karti hai. |
| Bot channel mein post nahi kar raha | Bot target channel ka **admin** hai? *Post Messages* on hai? `CHANNEL_ID` `-100...` wali hai? `/post <id>` mein last error dekho. Fix karke `/retry <id>`. |
| `Chat not found` | ID galat hai, ya bot us chat mein nahi hai. `tools/get_ids.py` se dobara ID lo. Group supergroup bana to ID badal jaati hai (bot owner ko nayi ID bhejta hai). |
| Workflow ruk gaya | Actions tab mein dekho: disabled to nahi? Settings → Actions → *Read and write* permission? Cron har 6 ghante backup start karta hai. Manual: `gh workflow run bot.yml`. |
| Flood wait (429) | Telegram ne rate limit kiya. Bot khud diya hua time wait karta hai, retry count nahi badhta. Kuch karne ki zaroorat nahi. |
| Mongo connection error | `MONGO_URL` sahi hai? Password special characters URL-encoded hain? Atlas Network Access mein `0.0.0.0/0` hai? Cluster paused to nahi? |
| Post `needs_review` | Publish ke time timeout hua ya bot band hua – post shayad channel mein aa chuka ho. Channel check karo; nahi aaya to `/retry <id>`. |
| Admin ko notifications nahi aate | Usne bot ko DM mein `/start` nahi kiya, ya bot block kiya hai. |

---

## Project structure

```
bot/            main.py (startup/shutdown) · config.py · db.py · jsonstore.py (local fallback)
                storage.py (copy/rollback/rebuild) · lease.py · scheduler.py · service.py (lifecycle)
                texts.py (Hinglish texts) · utils.py · handlers/{submit,approval,admin}.py
tools/get_ids.py
tests/          pytest (Telegram mocked, mongomock-motor + JSON store)
deploy/         Dockerfile · docker-compose.yml · channel-posting-bot.service
.github/workflows/  bot.yml (24/7) · ci.yml (ruff + pytest) · keepalive.yml
```

## Docker / VPS
```bash
cp .env.example .env   # fill in
docker compose -f deploy/docker-compose.yml up -d --build
```
systemd ke liye [deploy/channel-posting-bot.service](deploy/channel-posting-bot.service) ke comments follow
karo. VPS pe chalao to GitHub workflow **disable** kar do – ek time pe ek hi copy chalni chahiye.

License: MIT
