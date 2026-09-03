# MaklerAssistant — VM Setup Guide

This turns the login *test* into a **persistent bot** that runs on your VM with
tappable buttons and a cached session (no OTP every time).

Two things now live in the repo:

| File | Where it runs | What it's for |
|---|---|---|
| `bina_login_test.py` | GitHub Actions | the one-shot login test (kept for reference) |
| `bina_bot.py` + `bina_core.py` | **your VM** | the real bot: buttons, cached session |

---

## 1. Clone it on the VM

Public repo, so no token needed:

```bash
cd ~
git clone https://github.com/illkeeen3434343-eng/MaklerAssistant.git
cd MaklerAssistant
```

Later, to update just the bot from `origin/main` (same pattern you use for
`binaizleme`, leaving any local changes alone):

```bash
cd ~/MaklerAssistant
git fetch origin
git checkout origin/main -- bina_bot.py bina_core.py
```

---

## 2. Install

Chromium is already installed on your VM from the `binaizleme` work, but install
into a fresh venv to keep this project isolated:

```bash
cd ~/MaklerAssistant
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium        # skips if already cached
```

---

## 3. Configure

```bash
cp .env.example .env
nano .env
```

Set:

- `BOT_TOKEN` — from @BotFather (use a **different** bot than `binaizleme`; two
  processes on one token collide)
- `ALLOWED_USER_IDS` — your numeric id from @userinfobot
- `BINA_PHONE` — your bina.az number (optional; if set, the bot won't ask)
- `HEADLESS=true` — keep true on a server

> **`.env` line-endings:** you hit `\r` contamination before on `binaizleme`.
> If the bot behaves oddly, run `sed -i 's/\r$//' .env` — same fix.

---

## 4. Run it

```bash
source .venv/bin/activate
python bina_bot.py
```

Open your bot in Telegram, send `/start`, and you'll see buttons:

- **🔑 Login / check session** — logs in (asks for OTP only if needed)
- **📋 My ads** — ensures you're logged in (readout is the next feature)
- **🩺 Session status** — shows whether a saved session exists
- **🚪 Forget session** — clears it, forcing a fresh SMS next time

**First tap of Login:** bina.az texts you, you send the code, done — and the
session is saved to `sessions/`.
**Every tap after that, for days:** it reuses the saved cookies and skips the
SMS entirely. That was your explicit ask ("save cache files to avoid OTP every
time") and it's handled by `storage_state`.

---

## 5. Run it as a service (survives reboots and crashes)

```bash
sudo nano /etc/systemd/system/makler-bot.service
```

```ini
[Unit]
Description=MaklerAssistant bina.az bot
After=network-online.target

[Service]
Type=simple
User=vboxuser
WorkingDirectory=/home/vboxuser/MaklerAssistant
ExecStart=/home/vboxuser/MaklerAssistant/.venv/bin/python bina_bot.py
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1
MemoryMax=1500M

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now makler-bot
journalctl -u makler-bot -f          # watch the logs
```

`Restart=always` covers your "automatic restart if it crashes" requirement.
`MemoryMax=1500M` stops a browser leak from touching `binaizleme`.

---

## Running alongside binaizleme

Both can run on the same VM. The essentials, from the architecture review:

- **Separate bot tokens** — non-negotiable (two pollers on one token = HTTP 409).
- **Separate directories and venvs** — already the case.
- **RAM** — this bot holds a browser context (~150 MB). With `binaizleme` also
  running, keep an eye on total usage; the `MemoryMax` cap protects the monitor.
- **Shared IP** — both hit bina.az from the same address. This bot acts only when
  you tap a button (low volume), so it adds little to your footprint, but don't
  hammer it.

---

## The three things you asked for

1. **Buttons instead of manual triggers** — done. `bina_bot.py` is a persistent
   aiogram bot with inline buttons. Unlike GitHub Actions (manual `workflow_dispatch`),
   this reacts instantly to a tap. This is *why* it has to run on the VM: Actions
   jobs are one-shot and can't sit waiting for a button.

2. **Cached session (no OTP every time)** — done. After the first login the
   cookies are saved to `sessions/<number>.json` and reused. You'll only re-enter
   an SMS code when bina.az actually expires the session (usually days/weeks).

3. **OTP "disappeared the first time" bug** — fixed. The old code deleted your
   code message immediately for privacy, with no acknowledgment, so it looked
   like it hadn't registered and you retyped it. Now the bot replies
   "🔑 Code received, thanks." **before** deleting it, so you get clear feedback.
   Fixed in both `bina_bot.py` and the original `bina_login_test.py`.

---

## What's still a stub

`bina_bot.py` logs in and caches the session — the proven-hard part. It does not
yet **read your ads** or **change prices**; "My ads" currently just confirms
login. The session is ready for those; they're the next features to build on top
of `bina_core.BinaSession` (add `fetch_listings()` and `update_price()` methods,
then wire buttons to them).
