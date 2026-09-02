# bina.az Login Test (GitHub Actions)

A minimal, manually-triggered test that proves one thing end to end:

> start → ask for phone → bina.az sends an SMS → you send the code → **logged in ✅**

It runs entirely inside GitHub Actions. There is no server, no database, and
nothing is saved — the browser session is destroyed the moment the job ends.

**Confirmed:** bina.az does **not** block GitHub's datacenter IP — a probe run
reached the site normally (a real bina.az 404, not a Cloudflare challenge). So
the login flow *can* run from Actions. The one wrinkle we found: bina.az's login
is a **modal** opened by the `#authentication` hash, not a page — `/login`
returns 404. The script now opens that modal, picks the phone-number option, and
enters the number in bina.az's local format (no leading zero).

---

## How it works

```
GitHub Actions runner                 You (Telegram)              bina.az
─────────────────────                 ──────────────              ───────
start job (Actions tab)
   │
   ├─ "send me your phone"  ──────────────►
   │                        557778899  ◄──── you type it (no leading 0)
   ├─ open #authentication modal ────────────────────────────────►
   ├─ click "Telefon nömrəsi ilə giriş" ─────────────────────────►
   ├─ enter phone, submit ───────────────────────────────────────►
   │                                          SMS ◄──── to your phone
   ├─ "send me the code"  ─────────────────►
   │                            4821    ◄──── you type it
   ├─ submit code ───────────────────────────────────────────────►
   ├─ open "my ads", check we're logged in ──────────────────────►
   └─ "✅ Login successful"  ─────────────►
```

**Phone format:** bina.az's field wants the local number without the leading
zero — you'd type `557778899` for `055 777 88 99`. The script accepts any form
(`0557778899`, `+994557778899`, `557778899`) and normalizes it, so don't worry
about which you send.

The runner talks to you through a Telegram bot using plain long-polling — no
framework. It only ever accepts messages from **your** Telegram user ID.

---

## Two modes

Run them in this order.

### 1. `probe` — no login, no Telegram needed

Loads the homepage, opens the `#authentication` login modal, selects the
phone-number option, and reports:

- whether it looks like a Cloudflare challenge (already confirmed: it isn't),
- whether the modal opened and the phone input was found,
- **a printed list of every visible input and button** in the modal, with their
  `name`/`type`/`placeholder` — so you can read the real selectors straight from
  the run log,
- the full modal HTML + a screenshot, saved as downloadable artifacts.

Run this first. It costs no SMS and confirms the selectors before you spend a
real login attempt.

### 2. `login` — the real test

The full flow above. Needs the Telegram secrets set. You must be at your phone,
because you'll relay the SMS code within a few minutes.

---

## Setup (about 10 minutes, once)

### Step 1 — Create a Telegram bot

1. Open [@BotFather](https://t.me/BotFather) → `/newbot` → follow the prompts.
2. Copy the token it gives you (looks like `123456789:AA…`).
3. **Open a chat with your new bot and send it any message** (e.g. `hi`). A bot
   cannot message you until you've messaged it first — skip this and the test
   will hang waiting to talk to you.

### Step 2 — Get your Telegram user ID

Message [@userinfobot](https://t.me/userinfobot). It replies with your numeric
`Id`. Copy it.

### Step 3 — Put this project in a GitHub repo

Create a **private** repository and add these files:

```
your-repo/
├── .github/workflows/login-test.yml
├── bina_login_test.py
├── requirements.txt
└── README.md
```

You can do this in the browser (Add file → Upload files) — no git needed.

### Step 4 — Add repository secrets

In your repo: **Settings → Secrets and variables → Actions → New repository
secret**. Add:

| Secret name | Value | Required |
|---|---|---|
| `BOT_TOKEN` | The token from BotFather | Yes |
| `TELEGRAM_USER_ID` | Your numeric ID from userinfobot | Yes |
| `BINA_PHONE` | Your bina.az phone, e.g. `557778899` (any form works) | Optional — see note |

> **`BINA_PHONE` is optional.** If you set it, the bot uses it automatically and
> only asks you for the SMS code. If you leave it out, the bot asks for the
> phone number in the chat too. You can store it as `557778899`, `0557778899`,
> or `+994557778899` — the script normalizes to what bina.az's field expects.

That's it. No secret is ever printed in the logs, and your phone number is
masked (`+9945***4567`) everywhere it appears.

---

## Running it

### First: the probe

1. Go to the **Actions** tab in your repo.
2. Click **bina.az login test** in the left sidebar.
3. Click **Run workflow** (right side).
4. Set **mode** to `probe`. Click the green **Run workflow**.
5. Wait ~1 minute. Open the run and read the **Run test** step logs.

**Reading the probe result:**

- ✅ *"the login form is reachable and the phone input was found"* → great,
  proceed to the login test.
- 🚨 *"LOOKS BLOCKED"* → GitHub's IP is being challenged by Cloudflare. Jump to
  **[If the probe says BLOCKED](#if-the-probe-says-blocked)**.
- ❌ *phone input not found, but not blocked* → the page loaded but the selector
  is wrong. Download the artifacts, open `probe-login-page.html`, find the real
  input, and fix it (see **[Fixing selectors](#fixing-selectors)**).

### Then: the login test

1. **Run workflow** again, this time with **mode** = `login`.
2. Within a few seconds your Telegram bot messages you. Have your phone ready.
3. If you set `BINA_PHONE`, it goes straight to asking for the code; otherwise
   it asks for the phone first.
4. bina.az texts you. Send the code to the bot.
5. You get either **✅ Login successful** or a specific failure with instructions.

Send `/cancel` at any time to abort.

---

## Fixing selectors

The selectors shipped here are **educated guesses** for the modal. If a login
run can't find the phone choice or input, you fix it without editing code —
through the workflow inputs.

1. Run `probe`. Its log prints a **list of every visible input and button** in
   the modal, like:
   ```
   <INPUT type=tel name=phone placeholder='55 777 88 99'>
   <BUTTON>  "Telefon nömrəsi ilə giriş"
   <BUTTON type=submit>  "Davam et"
   ```
   That's usually enough to pick the right selector directly.
2. For the full picture, download the artifacts (run page → **Artifacts** →
   `bina-debug-…`) and open `probe-modal.html` / `probe-modal.png`.
3. Re-run with the correct CSS selector pasted into the matching input box.

The overridable selectors now include the **login trigger** (opens the modal)
and the **phone choice** ("log in with phone number" button), plus phone input,
phone submit, OTP input, OTP submit, and the logged-in marker.

Selector tips:

| Good (stable) | Bad (brittle) |
|---|---|
| `input[name='phone']` | `#root > div > form > div:nth-child(3) > input` |
| `input[type='tel']` | `.css-1a2b3c4` |
| `button:has-text('Daxil ol')` | `button.btn.btn-primary.mt-4` |

The five overridable selectors are: phone input, phone submit, OTP input, OTP
submit, and the logged-in marker. Everything else has sensible fallbacks.

The **logged-in marker** is the one most likely to need fixing — it's how the
script decides success. Pick something that appears **only when logged in**: a
logout link, a link to your profile, your account menu. After a successful
manual login, view the page source and find such an element.

---

## If the probe says BLOCKED

This is a real, expected outcome, not a bug in the script. It means Cloudflare
served GitHub's shared IP a challenge page instead of the login form.

What it tells you: **the OTP-login flow can't run from GitHub Actions for this
site.** That's genuinely useful — it's the cheapest possible way to learn it,
and it's exactly why we tested login separately from the reads you already know
work.

Your options, roughly in order of effort:

1. **Run the login step where your reads already work.** Your `binaizleme` VM
   has an Azerbaijan residential IP that bina.az already tolerates. Login
   belongs there, not on a shared datacenter IP. This is the honest home for it.
2. **A residential/mobile proxy** routed only for the login requests. Adds cost
   and complexity; can also violate the proxy's and the site's terms — check
   both.
3. **Confirm whether an official path exists.** Worth an email to bina.az about
   agency/API access before investing more in browser automation.

Whatever you choose, keep this test — re-running the `probe` is how you'll know
if bina.az's blocking posture changes later.

---

## What this does and doesn't do

**Does:** prove the phone → SMS → code → authenticated-session flow works, from
a GitHub runner, with the OTP relayed through Telegram. Saves debug artifacts on
every run.

**Doesn't:** save the session, read your ads, change any price, store anything,
or run continuously. It's a one-shot test. A bot that actually *does* things
needs to run somewhere persistent (your VM), for the reasons in the earlier
architecture review — a runner is stateless and dies after each job, so it can't
hold a login open between operations.

---

## Cost & limits

- **Free.** Private repos get 2,000 Actions minutes/month; each run uses ~2–4.
- **Manual only.** No push/schedule triggers — it can't run without you clicking.
- **15-minute cap.** If you walk away mid-test, the job self-terminates.
- **One run at a time.** Two runs on one bot token would collide (Telegram 409),
  so the workflow serializes them.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Bot never messages me | You didn't message the bot first (Step 1.3), or `TELEGRAM_USER_ID` is wrong. |
| "BOT_TOKEN secret is missing" | Secret not added, or named differently. It's case-sensitive. |
| Probe: "LOOKS BLOCKED" | Cloudflare is challenging GitHub's IP. See the section above. |
| "Could not find the phone input" | Wrong selector. Download artifacts, fix via workflow input. |
| "no OTP field appeared" | Phone submit didn't trigger the code step. Check `after-phone-submit.html`. |
| "Login did not complete" | Usually the logged-in marker selector. Check `final-state.html`. |
| Job waits then times out | You didn't reply in time (phone 3 min, code 5 min). Just re-run. |
| Code arrives split into boxes | The single-field selector won't fit. Tell me and I'll add split-box handling. |

---

## A note on responsible use

This logs into *your own* bina.az account with *your own* phone and *your own*
relayed code. Keep it that way. Automating other people's accounts, or running
this at volume, raises the security, consent, and terms-of-service problems
covered in the architecture review — and bina.az's own rules restrict automated
access. Run it occasionally, for your own account, as a test.
