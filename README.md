# irc-tg-bridge

Run a full IRC client from Telegram. WeeChat is the always-on IRC engine; a
Telegram bot mirrors every conversation into its own topic and gives you
complete control of IRC from your phone.

Each IRC buffer (every channel on every network, every private message) maps to
its own Telegram forum topic. Messages flow both ways. Anything you can type in
IRC you can type from Telegram, including raw commands.

## Features

- **Bidirectional** - read and reply to IRC from Telegram, in real time.
- **One topic per conversation** - channels and PMs each get their own thread.
  Works in the bot's private chat (Threaded Mode) or in a supergroup you pick.
- **Multi-network** - connect to any number of IRC networks at once, each with
  its own nick and identity.
- **Full IRC** - the whole command set. Any message starting with `/` is sent to
  IRC as a command (`/whois`, `/mode`, `/topic`, `/msg`, CTCP, ...). Plain text
  is sent as a message.
- **Everything from the bot** - add servers, discover and join channels, change
  nick, pick where topics live, switch language. No config files to hand-edit.
- **IRCv3** - reply threading, reactions, typing indicators and message deletion
  are mirrored both ways on networks that support them.
- **Mentions** - a line naming your nick pings you on Telegram (even in a muted
  topic) and is highlighted in the text.
- **Files** - an incoming DCC transfer and a file you send from Telegram are
  re-hosted on gofile and shared as a link, sidestepping Telegram's upload caps.
- **Catch-up** - messages that arrived while the bridge was down are backfilled
  (CHATHISTORY) and de-duplicated, so you miss nothing across a restart.
- **Delivery feedback** - every message you send is marked with a reaction:
  delivered, a command working then done, or failed.
- **Sender pool** - spread a busy chat's traffic across several bots to raise the
  effective rate limit; add one from the bot, no restart.
- **Tor per server** - route any network through Tor (SOCKS5), including
  `.onion` addresses, for anonymity. Toggle per server.
- **Secure by default** - the bot answers only to your Telegram account. IRC
  passwords live in WeeChat's encrypted secured data, never in plaintext.
- **English and Hebrew** - switch the interface language from the bot. Adding a
  language is a single JSON file (see below).

## How it works

```
IRC networks  <->  WeeChat (headless + api relay)  <->  tgbridge (Python)  <->  Telegram
```

WeeChat runs headless as a systemd service and holds all IRC connections
(SASL, IRCv3, Tor per server, encrypted secured data). The bridge is a separate
async Python program: it reads and writes IRC through WeeChat's JSON `api` relay
(no fragile screen-scraping) and talks to Telegram with a maintained Pyrogram
fork. What you set from the bot (topic mode, language, per-server options) is
stored in a small SQLite database the bridge manages for you.

## Requirements

- A Linux host with `weechat-headless` 4.3+ (for the `api` relay) and Python 3.10+.
- `tor` only if you want to route networks through Tor.
- A Telegram bot token from [@BotFather](https://t.me/BotFather) and an
  `api_id`/`api_hash` from [my.telegram.org](https://my.telegram.org).

## Install

1. Install `weechat-headless` and copy the bridge into WeeChat's Python autoload
   directory.
2. Fill in `.env` from `.env.example` (three values, set once), copy it to
   `/etc/irc-tg-bridge/env` (`chmod 600`), and install the systemd unit from
   `deploy/`.
3. Start the service, open your bot in Telegram, and configure the rest from the
   bot's menu.

## Configuration

The bot cannot bootstrap itself before it can reach you, so a handful of values
are set once in the environment file (`.env.example` is the full, commented
list). Everything else (the target group, language, servers, nicks, Tor, sender
bots) is set from the bot at runtime and persisted for you.

| Variable                              | Meaning                                             |
| ------------------------------------- | --------------------------------------------------- |
| `BOT_TOKEN`, `API_ID`, `API_HASH`     | Telegram bot credentials (@BotFather + my.telegram.org). |
| `ADMIN_TELEGRAM_ID`                   | Your numeric Telegram user id. The bot answers only to this id. |
| `RELAY_HOST`/`RELAY_PORT`/`RELAY_SECRET` | Where and how to reach WeeChat's `api` relay.    |
| `SECURE_PASSPHRASE`                   | Passphrase for WeeChat's encrypted secured data.    |

File transfers are opt-in: set `TGBRIDGE_XFER_DIR` (the directory WeeChat's xfer
plugin auto-accepts incoming DCC into) and `TGBRIDGE_XFER_SERVER` to enable
re-hosting incoming files on gofile. Without them, incoming DCC is simply not
watched; outgoing uploads still work.

## Adding a language

Copy `locales/en.json` to `locales/<code>.json`, translate the values, and the
new language appears in the bot's language menu. Keys are stable; only the
values change.

## Trust model

Access is by membership. In group mode every member of the designated
supergroup is a full operator of the shared IRC identity: they can send
messages and run any IRC command (`/mode`, `/kick`, `/nick`, ...) in a
conversation topic. Only bridge management (adding servers, settings,
anonymity) is limited to the admin. Add only people you would trust with your
IRC session. Private (threaded) mode is single-operator: just you.

## Security notes

Tor hides your IP, but your nick and SASL account still identify you to the
network, and CTCP/DCC can leak metadata. On servers marked for Tor the bridge
disables CTCP/DCC and recommends a separate nick. Anonymity is a habit, not just
a proxy.

## License

MIT. See [LICENSE](LICENSE).
