# Zonex X Bot

यह Telegram bot `aiogram 3` और async SQLAlchemy पर बना है। इसमें channel verification, referral rewards, TG/package selling, panel selling, UPI QR payment flow, admin approval/rejection, broadcast, ban/unban और product management शामिल हैं।

## Project files

- `main.py` — पूरा bot source और entrypoint
- `requirements.txt` — Python dependencies
- `.env.example` — required/optional configuration template
- `Dockerfile` — production-style container image
- `docker-compose.yml` — restart policy और persistent SQLite volume
- `tests/test_smoke.py` — syntax/config/dependency smoke tests
- `send_zip.py` — terminal helper जो clean ZIP बनाकर Admin Telegram ID पर भेजता है
- `.gitignore` / `.dockerignore` — local secrets, database और logs को बाहर रखते हैं

## बिना Docker के चलाना

Python 3.11 या 3.12 इस्तेमाल करें।

```bash
python -m venv .venv

# Linux/macOS
source .venv/bin/activate

# Windows PowerShell
# .venv\Scripts\Activate.ps1

pip install -r requirements.txt
mkdir -p data
cp .env.example .env
```

`.env` में कम-से-कम ये values भरें:

```env
BOT_TOKEN=123456:replace_this_with_your_bot_token
ADMIN_IDS=123456789
```

फिर start करें:

```bash
python main.py
```

## ZIP को सीधे Telegram Admin को भेजना

अगर ZIP को bot के जरिए अपने Admin Telegram ID पर भेजना हो, तो project folder में चलाएँ:

```bash
python send_zip.py
```

यह terminal में bot token को hidden prompt में लेता है, Admin ID पूछता है, clean ZIP बनाता है और Telegram `sendDocument` API से भेज देता है। Token किसी file में save या ZIP में शामिल नहीं होता।

## Docker से चलाना

पहले `.env.example` की copy बनाकर values भरें:

```bash
cp .env.example .env
docker compose up -d --build
docker compose logs -f zonex-x-bot
```

रोकने के लिए:

```bash
docker compose down
```

SQLite database Docker named volume `zonex_data` में रहेगा, इसलिए container recreate होने पर भी data बचा रहेगा। Database हटाने के लिए ही `docker compose down -v` चलाएँ।

## Telegram setup

1. @BotFather से bot बनाकर token लें।
2. अपना numeric Telegram user ID `ADMIN_IDS` में डालें; multiple admins comma से अलग करें।
3. Membership verification के लिए bot को required channels में admin बनाना बेहतर है।
4. `/start` से सामान्य flow और `/admin` से admin panel खोलें।
5. Admin panel में UPI ID, payment instructions, products और referral requirements configure करें।

## उपलब्ध admin commands

- `/admin`
- `/set_tg_req <number>`
- `/set_panel_req <number>`
- `/set_upi <upi-id>`
- `/set_instructions <text>`
- `/add_panel Name | Price | DeliveryInfo | Credentials | Stock`
- `/del_panel <id>`
- `/toggle_panel <id>`
- `/add_tg Name/Quantity/Price`
- `/del_tg <id>`
- `/toggle_tg <id>`

## Tests

ये tests external service या Telegram token नहीं मांगते:

```bash
python -m unittest discover -s tests -v
```

## Important production notes

- `.env`, `jonex.db`, `data/` और logs को Git या public upload में commit न करें।
- Panel credentials database में plaintext के रूप में रखे जाते हैं; production में database/file access को restricted रखें।
- Payment approval manual admin action है; UPI gateway auto-verification इस source में मौजूद नहीं है।
- Bot long polling इस्तेमाल करता है, इसलिए एक समय में एक ही running instance रखें।
- Channel usernames/IDs और invite links को अपने channels के अनुसार बदलें।