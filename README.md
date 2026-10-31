# Grok Usage

Adds **Grok** to Omarchy's **existing AI toolbar widget** — the AI icon already on the top bar. It does not add a second icon.

After install, click that same AI button. You get a **Grok** section next to any other agent you use. A combined X subscription shows one weekly bar, with the used percent in the label. Under it, each of the last seven days names the weekday on the left of the bar and that day's token total on the right. The line beside Grok names today, this week, and lifetime tokens from sessions still on disk. Older split plans still list each product meter.

![Grok inside Omarchy's existing AI toolbar widget](preview.png)

## Install

```sh
omarchy plugin add https://github.com/dougfour/omarchy-grok-usage.git --enable
```

Requires:

- Omarchy with the stock AI widget enabled (`omarchy.agents`, on by default)
- Python 3 on `PATH` (stdlib only)
- Grok Build signed in (`grok login`) so weekly limits can load
- Grok Bot signed in so its weekly meter can load

Leave the built-in AI icon where it is. After the first scan, click it and switch to **Grok**.

## Usage

- Left click the existing AI icon: usage panel
- Switch to **Grok** with the chip in the panel (or middle-click the icon)
- `r` or Enter in the panel: refresh (Grok follows the stock update)
- Grok also refreshes about every 5 minutes

This plugin is a headless service. It only writes a Grok usage record for the stock panel to display.

The weekly percent comes from `https://cli-chat-proxy.grok.com/v1/billing?format=credits` using the token in `~/.grok/auth.json`. On a combined plan (`isUnifiedBillingUser`) that percent is the only meter: Grok, Grok Bot, voice, and the API are shares of the same pool. A split plan still adds Grok Build, Grok Chat, and Grok Voice from `GetGrokCreditsConfig`, plus Grok Bot from that app's Cursor sand session. Token totals come from `~/.grok/sessions/**/updates.jsonl`. Cached input is counted once.

## Remove

```sh
omarchy plugin remove io.github.dougfour.grok-usage
```

Removal deletes the plugin checkout and drops `~/.local/state/omarchy/agents/usage/grok.json`, so the Grok chip leaves the stock AI panel. It does not change `~/.grok/auth.json` or session files.

## Privacy

The collector reads local Grok session files and the saved Grok login, then calls xAI's billing endpoint and the Grok credits-config endpoint. The Grok Bot meter reads `~/.config/Grok Bot/sand-secrets.json`, decrypts that app's Cursor session with the "Grok Bot" keyring item, and calls Cursor's sand usage RPC. It never logs tokens. Expired Grok CLI tokens are not refreshed; run `grok login` (or start Grok) to renew them. A refreshed Grok Bot access token stays in memory and is not written back to the Grok Bot config.

## License

MIT. See [LICENSE](LICENSE).
