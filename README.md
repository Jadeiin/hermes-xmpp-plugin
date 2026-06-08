# Hermes XMPP/Jabber plugin

Third-party XMPP/Jabber gateway platform plugin for Hermes Agent.

This packages the work from the upstream Hermes Agent XMPP PRs as an optional
user plugin so self-hosters can use XMPP now instead of waiting for the core PR
to land. It supports:

- 1:1 XMPP chats
- MUC group rooms
- mandatory STARTTLS to the XMPP server
- XEP-0085 typing indicators
- XEP-0198 Stream Management for reconnection resilience (session resumption)
- XEP-0313 MAM — catch up on missed messages after reconnect
- XEP-0333 Chat Markers — delivered/displayed read receipts
- XEP-0363 HTTP file upload for media/files
- XEP-0447 stateless file sharing for voice messages (inbound + outbound)
- XEP-0454 OMEMO Media Sharing — encrypted file uploads for DMs (aesgcm://)
- XEP-0394 message markup (bold, code, lists) — opt-in via config
- XEP-0071 XHTML-IM rich text — opt-in via config, complements XEP-0394
- XEP-0461 threaded message replies
- XEP-0444 message reactions (status 👀/✅/❌, reaction-based clarify, reaction-based exec approval)
- XEP-0513 Explicit Mentions — precise @mention tracking with begin/end indices
- XEP-0080 GeoLoc — geolocation detection (geo: URI)
- XEP-0308 Last Message Correction — message editing
- XEP-0424 Message Retraction — message deletion
- XEP-0004 data forms (ad-hoc command responses)
- XEP-0050 ad-hoc commands
- XEP-0359 Stable Stanza IDs — origin-id / stanza-id
- MUC mention gating — drop unmentioned messages (default: enabled)
- Gateway pairing — delegates DM auth when allowlist is empty
- OMEMO end-to-end encryption (optional, needs slixmpp-omemo)
- cron and `send_message` delivery through a standalone sender hook
- Hermes platform plugin registration via `ctx.register_platform(...)`

Traffic is encrypted to your XMPP server with TLS. When OMEMO is enabled and
slixmpp-omemo is installed, 1:1 messages are also end-to-end encrypted so the
server cannot read content. MUC OMEMO support depends on client/device
availability.

## Credits

Most adapter code is derived from Eric Lars Lee's upstream PR #17469. Mibay's
PR #3105 is credited for earlier XMPP/OMEMO exploration. See `ATTRIBUTION.md`
for the full credit note. This repo exists because waiting is annoying, not
because the packager wants credit for other people's work. Tiny open-source goblin
energy, responsibly attributed.

## Install

From the Hermes profile you want to use:

```bash
cd ~/.hermes/plugins
git clone https://github.com/fastfinge/hermes-xmpp-plugin.git
uv pip install --python ~/.hermes/hermes-agent/venv/bin/python -r ~/.hermes/plugins/hermes-xmpp-plugin/requirements.txt
hermes config set plugins.enabled '["hermes-xmpp-plugin"]'
```

If you run Hermes from a different checkout/venv, install requirements into that
Python environment instead.

Restart the gateway after installing or changing env vars:

```bash
hermes gateway restart
```

## Configure with env vars

Add these to `~/.hermes/.env` for the active profile:

```env
XMPP_JID=hermes@example.org
XMPP_PASSWORD=your-password
XMPP_ALLOWED_USERS=sam@example.org
# Optional:
XMPP_HOST=example.org
XMPP_PORT=5222
XMPP_MUC_ROOMS=room@conference.example.org/hermes
XMPP_MUC_NICK=hermes
XMPP_HOME_CHANNEL=sam@example.org
# MUC mention gating (default: true)
XMPP_REQUIRE_MENTION=true
# Message markup — both default OFF
XMPP_XEP_0394_ENABLED=false
XMPP_XEP_0071_ENABLED=false
```

`XMPP_ALLOWED_USERS` uses bare JIDs. MUC access is gated by room membership: if
the bot joins a room listed in `XMPP_MUC_ROOMS`, messages in that room are accepted.
For quick local testing only, set `XMPP_ALLOW_ALL_USERS=true`.

When `XMPP_ALLOWED_USERS` is empty (and `XMPP_ALLOW_ALL_USERS` is not set),
DM auth is delegated to the Hermes gateway pairing system — users are
automatically granted access when paired.

`XMPP_REQUIRE_MENTION` (default `true`) drops MUC groupchat messages that don't
@mention the bot. Set to `false` to receive all room messages (like old behaviour).

## Configure with config.yaml

You can also use config.yaml:

```yaml
plugins:
  enabled:
    - hermes-xmpp-plugin

xmpp:
  enabled: true
  jid: hermes@example.org
  password: ${XMPP_PASSWORD}
  allowed_users:
    - sam@example.org
  muc_rooms:
    - room@conference.example.org/hermes
  home_channel: sam@example.org
  require_mention: true
  xep_0394_enabled: false
  xep_0071_enabled: false
```

Env vars win when both are set.

## OMEMO end-to-end encryption (optional)

Install the extra dependencies:

```bash
uv pip install --python ~/.hermes/hermes-agent/venv/bin/python slixmpp-omemo omemo
```

Then set in `~/.hermes/.env`:

```env
XMPP_OMEMO_ENABLED=true
# Optional — defaults to ~/.hermes/xmpp_omemo.json
XMPP_OMEMO_STORAGE_PATH=/home/fastfinge/.hermes/xmpp_omemo.json
```

Or in `config.yaml`:

```yaml
xmpp:
  omemo_enabled: true
  omemo_storage_path: /home/fastfinge/.hermes/xmpp_omemo.json
```

On first connect, the adapter generates an OMEMO device key and publishes it to
the server. This may take a few seconds — the adapter waits for initialization
before sending encrypted messages. If the recipient hasn't published device keys
yet, messages fall back to plaintext (with a log warning).

MUC OMEMO is supported when all participants have compatible devices, but group
encryption reliability varies by client. 1:1 encryption is the primary use case.

## XEP-0454 OMEMO Media Sharing

When OMEMO is enabled, file uploads in DMs are encrypted using XEP-0454. The
adapter uploads encrypted content and sends an `aesgcm://` URL instead of a
plain `https://` URL. On the inbound side, `aesgcm://` URLs are automatically
downloaded, decrypted, and cached locally.

This is transparent — no extra configuration needed beyond enabling OMEMO.

## Multiple Hermes profiles

XMPP is nice for profile isolation because you can create one JID per profile:

- `sam-hermes@example.org` for your profile
- `mom-hermes@example.org` for Mom's profile
- `dad-hermes@example.org` for Dad's profile

Each Hermes profile gets its own `.env`, memory, sessions, and gateway process.

## Server notes

Prosody needs modules for MUC, HTTP file upload, and the new XEPs. On Debian/Ubuntu:

```bash
sudo apt install prosody
sudo prosodyctl adduser hermes@example.org
```

Enable/configure in Prosody:
- `muc` — group rooms
- `http_file_share` — file upload (XEP-0363)
- `mam` — message archive (XEP-0313, required for reconnect replay)
- `mod_groups` — optional, for ad-hoc command roster

For ejabberd, enable MUC plus `mod_http_upload` and `mod_mam`. The new features
(reactions, replies, markup, mentions, markers) use standard XMPP stanzas and
should work on any modern server supporting the relevant XEPs.

## Troubleshooting

- `xmpp_auth_failed`: wrong JID/password, or server auth policy issue.
- `xmpp_connect_failed`: DNS/firewall/SRV issue; try `XMPP_HOST`.
- HTTP upload failure: enable XEP-0363 on the server and check max file size.
- DM rejected: add your bare JID to `XMPP_ALLOWED_USERS`, or leave the list
  empty to use gateway pairing.
- MUC silent: add room to `XMPP_MUC_ROOMS` and invite the bot. If
  `require_mention` is true (default), make sure you @mention the bot.
- MAM replay not working: verify the server has `mod_mam` enabled and the
  archive is configured for the user. MAM state is in-memory only — a full
  gateway restart resets last-seen timestamps.
- Markup not showing: both `xep_0394_enabled` and `xep_0071_enabled` default
  to `false`. Set the one you want to `true` in env vars or config.
- OMEMO decrypt failures: ensure the other device is publishing keys. The
  adapter logs a warning when falling back to plaintext.

## Features in depth

### Stream Management (XEP-0198)

The adapter enables XEP-0198 Stream Management for reconnection resilience.
On temporary disconnections (e.g., network blips), the session is resumed
automatically without a full re-login. Message queues and presence state
are preserved.

XEP-0198 works at the TCP/XML stream level — it's transparent to the
application layer and requires no server changes beyond what modern XMPP
servers already support.

### MAM replay (XEP-0313)

After reconnect, the adapter queries the server's message archive (MAM) to
catch up on messages missed while offline. It replays DMs and each MUC room
independently, capped at 50 messages per query to avoid flooding.

MAM state uses in-memory timestamps only (no disk I/O) — aligned with
the Telegram `drop_pending_updates` pattern. Timestamps survive a Phase-1
reconnect (same adapter instance) but reset on a full gateway restart.

### Chat Markers (XEP-0333)

On receiving a message, the adapter sends a `displayed` marker to let the
sender know it was processed. This is sent as a cleartext sibling stanza
and does not interact with OMEMO encryption.

XEP-0333 markers are standard across modern XMPP clients (Conversations,
Dino, Gajim, Movim). They're more reliable than legacy XEP-0184 delivery
receipts, which are not implemented.

### Message markup (XEP-0394 + XEP-0071)

Two markup modes, both disabled by default:

**XEP-0394** (`xep_0394_enabled=true`): converts markdown-like syntax to
XEP-0394 structured markup:
- `**bold**` → `<span style="font-weight: bold">bold</span>`
- `` `code` `` → `<code>code</code>`
- `` ```block``` `` → `<blockcode>block</blockcode>`

**XEP-0071 XHTML-IM** (`xep_0071_enabled=true`): generates XHTML body
wrappers as an alternative/modern rich-text representation. Uses the same
`to_xhtml_im()` pipeline as XEP-0394.

You can enable either, both, or neither. When both are on, XHTML-IM is
attached first, then the XEP-0394 structured markup.

> **OMEMO note:** Since `encrypt_message` strips all non-body elements on the
> wire, markup from both XEP-0394 and XEP-0071 does not survive OMEMO
> encryption. Markup only works on the plaintext send path.

### Clarify — reaction-based prompts

When Hermes needs clarification (multiple-choice questions), the adapter
sends a text prompt with numbered options and adds numbered emoji reactions
(1️⃣–4️⃣ + ✏️ for "Other"). Users tap a reaction to select their choice.

This replaces the previous Data Forms (XEP-0004) approach for clarify, which
was not well supported by most XMPP clients. XEP-0004 remains available for
ad-hoc command forms where server → client compatibility is better.

When reactions are unavailable (no `xep_0444` plugin), the adapter falls
back to text-intercept mode — the user types a numbered choice or free-form
text response.

### Exec approval — reaction-based

Dangerous commands trigger an approval prompt with ✅ (approve) and ❎ (deny)
reactions. Users tap a reaction to approve or deny, or use `/approve` and
`/deny` text commands as a fallback.

### Threaded replies (XEP-0461)

When you reply to a bot message, the adapter tracks the thread using XEP-0461
message references. Replies from Hermes are sent back into the same thread so
context is preserved.

For MUC rooms, replies use XEP-0359 `<stanza-id>` rather than `<origin-id>`,
in compliance with XEP-0461 §4.1 (the server-assigned id is the canonical
message identifier in MUCs).

### Explicit Mentions (XEP-0513)

MUC @mentions are detected through a multi-layer fallback:

1. **XEP-0513** — precise `begin`/`end` indices from the `<reference>` element
   (when the client sends them)
2. **XEP-0372 References** — nick mention detection via `<reference type="mention">`
3. **Nick-in-body** — plain text "@nick" detection (last resort)

When a mention is detected, the @mention prefix is stripped from the body
before processing.

### Geo-Location (XEP-0080)

`geo:` URIs in message bodies (sent by Conversations and other location-aware
clients) are detected and parsed. The adapter extracts latitude, longitude,
and optional accuracy/altitude metadata for downstream processing.

### Message reactions (XEP-0444)

When Hermes starts processing a request, the adapter sends a 👀 reaction. When it
finishes, ✅ or ❌ is sent depending on success or error. Reactions are visible in
clients that support XEP-0444 (Conversations, Dino, Gajim, etc.).

Reactions can also be sent by Hermes tools; the adapter maps the reaction to the
original message using slixmpp's `xep_0444`.

### Voice messages (XEP-0447)

Voice messages from Hermes are sent as XEP-0447 Stateless File Sharing (SFS)
messages. The file is uploaded via XEP-0363 HTTP upload first, then a lightweight
SFS reference is sent. This allows clients to preview metadata before downloading.

Inbound voice messages are also supported — both SFS references and legacy
XEP-0066 OOB URLs are handled.

Voice calls (Jingle) are not yet supported because slixmpp does not include a
Jingle RTP implementation.

### Message editing & deletion (XEP-0308 + XEP-0424)

The adapter supports last-message correction via XEP-0308, and message
retraction via XEP-0424. These are exposed through the gateway's standard
edit/delete abstraction.

### Ad-hoc commands (XEP-0050)

The adapter registers ad-hoc commands on the bot's JID. Users can discover them
with their client's command list (e.g., `/cmd` in Conversations). Currently a
basic command list is exposed; future releases may add Hermes-specific commands.

## Development

The test suite runs standalone — no Hermes checkout required. It uses mocks for the
Hermes gateway and slixmpp internals.

```bash
python -m pytest -q
```

For testing against a real Hermes checkout, set `PYTHONPATH`:

```bash
PYTHONPATH=/path/to/hermes-agent python -m pytest -q
```

Additional documentation in `docs/`:
- `cross-branch-interactions.md` — feature × OMEMO × MUC/DM interaction matrix
- `slixmpp-xep-apis.md` — slixmpp 1.15.0 XEP plugin API reference
- `muc-mention-mechanisms.md` — MUC mention detection layers
- `jingle-voice-calls.md` — Jingle voice call roadmap
- `upstream-xmpp-doc.md` — upstream Hermes XMPP architecture notes
