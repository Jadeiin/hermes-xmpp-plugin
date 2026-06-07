# XMPP MUC Mention Mechanisms — Comprehensive Analysis

## Data source

XSF implementation database: https://data.xmpp.net/explore/xmpp (SQLite backend).
Queried 2026-06-08.

## Mention-related XEPs

| XEP | Number | Status | Title | What it does |
|-----|--------|--------|-------|-------------|
| **XEP-0513** | 0513 | **Experimental (accepted 2026-03-31)** | **Explicit Mentions** | **New standard** — dedicated mention mechanism replacing XEP-0372's mention type. Supports individual (jid/occupantid), group (#channel, #moderators), modifiers (active/@here, noping). |
| XEP-0372 | 0372 | Experimental (v0.5.0) | **References** | Generic reference framework; `type="mention"` marks a user mention. **Will be superseded by XEP-0513** (mentions being removed from 0372). |
| XEP-0452 | 0452 | Experimental (v0.2.2) | MUC Mention Notifications | Server-side forwarding of mentioned messages to non-present users. Relies on XEP-0372 for mention detection. NOT a mention mechanism itself. |
| XEP-xxxx | — | ProtoXEP (v0.0.1, 2016) | **JID Mention** | `urn:xmpp:mention:0` — dead ProtoXEP. Never accepted. Do NOT implement. |
| XEP-0422 | 0422 | Deferred | Message Fastening | Unrelated — fastening annotations to messages. |

## XEP-0513 (Explicit Mentions) — the new standard

Namespace: `urn:xmpp:mentions:0`  
slixmpp plugin: `xep_0513` (already shipped in slixmpp)  
Stanza access: `stanza["mentions"]` → list of `Mention` objects (iterable)

```xml
<message type="groupchat" from="room@conf/poesty" to="room@conf">
  <body>@hermes /status</body>
  <mention xmlns="urn:xmpp:mentions:0"
           begin="0" end="7"
           jid="hermes@jabjab.de"/>
</message>
```

### Mention attributes

| Attribute | Description |
|-----------|-------------|
| `jid` | JID of the mentioned user |
| `occupantid` | XEP-0421 occupant ID (preferred over JID in MUC) |
| `begin` / `end` | Dijkstra-style substring indices in `<body>` — **precise span** |
| `mentions` | Group mention URI (e.g., `#channel`, `#moderators`) |

### Modifiers (sub-elements)

| Element | Description |
|---------|-------------|
| `<active/>` | @here equivalent — only currently active users |
| `<noping/>` | Reference without notification |

### Group mention types

| URI | Meaning |
|-----|---------|
| `#channel` | All MUC occupants (@everyone) |
| `#moderators` | MUC role |
| `#participants` | MUC role |
| `#owner`, `#admin`, `#member` | MUC affiliations |

## XEP-0372 (References) — the existing standard

Namespace: `urn:xmpp:reference:0`

```xml
<message type="groupchat" from="room@conf/poesty" to="room@conf">
  <body>hermes: /status</body>
  <reference xmlns="urn:xmpp:reference:0"
             type="mention"
             uri="xmpp:hermes@jabjab.de"
             begin="0" end="6"/>
</message>
```

Key attributes:
- `type="mention"` — identifies this as a user mention
- `uri` — the mentioned user's JID (e.g., `xmpp:user@domain`)
- `begin` / `end` — optional Dijkstra-style substring indices in `<body>`

## Client implementation status (from XSF database)

| Client | ID | Platform | XEP-0372 | XEP-0513 | Mention mechanism |
|--------|----|----------|----------|----------|-------------------|
| **Conversations** | 58 | Android | ❌ not reported | ❌ not reported | ✅ **XEP-0372** (source-verified) |
| **Gajim** | 29 | Linux, Windows | ❌ not reported | ❌ not reported | ✅ **Nick-in-body only** |
| **Dino** | 25 | Linux, FreeBSD | ❌ not reported | ❌ not reported | ❓ unknown |
| **Movim** | 15 | Browser, Linux | ✅ complete | ❌ not reported | XEP-0372 |
| **Prose** | 24 | Browser, Win, Mac | ✅ complete | ❌ not reported | XEP-0372 |
| **Psi** | 31 | Linux, Win, Mac, BSD | ✅ complete | ❌ not reported | XEP-0372 |
| **Converse.js** | 28 | Browser | ❌ no status | ❌ not reported | ❓ unknown |
| **Renga** | 40 | Haiku | ⚠️ partial | ❌ not reported | XEP-0372 (partial) |

**Database caveats:**
- Voluntary submissions — Conversations sends XEP-0372 references but hasn't reported it.
- XEP-0513 (accepted 2026-03-31) is not yet in the data.xmpp.net database.
- Gajim confirmed nick-in-body only (no XEP-0372 elements in nbxmpp).

## How the Hermes XMPP adapter detects mentions

Three-layer approach (adapter.py, updated 2026-06-08):

### Layer 0: XEP-0513 Explicit Mentions (NEW — implemented 2026-06-08)
```python
for m in stanza_to_dispatch["mentions"]:
    # Individual mention via JID
    m_jid = m["jid"]
    if m_jid and str(m_jid.bare) == self._self_bare:
        mentioned = True
        mention_begin = m["begin"]   # for precise prefix stripping
        mention_end = m["end"]
        break
    # Group mention (e.g., #channel, #moderators)
    if m["mentions"]:
        mentioned = True
        break
```

### Layer 1: XEP-0372 `<reference>` (existing)
```python
for ref in stanza_to_dispatch["references"]:
    if ref["type"] == "mention":
        if self._self_bare in (ref["uri"] or ""):
            mentioned = True
            break
```

### Layer 2: Nick-in-body (fallback)
```python
if not mentioned and from_resource:
    if our_nick and body and our_nick in body:
        mentioned = True
```

## Mention prefix stripping

Two strategies, applied in order:

### Preferred: XEP-0513 begin/end (precise)
When a XEP-0513 mention provides `begin`/`end` attributes, the exact substring is cut from the body:
```python
body = body[:begin] + body[end:]  # then .lstrip()
```
This handles mentions anywhere in the body, not just at the start.

### Fallback: Nick-based prefix stripping
When only nick-in-body detection fires (no XEP-0513/0372 begin/end), strip the nick prefix:
```python
# "hermes /status" → "/status", "hermes: help" → "help"
if body.lower().startswith(nick.lower()) and body[len(nick):].startswith((": ", " ", ":", ",")):
    body = body[len(nick):].lstrip(": ,").lstrip()
```

## Scenario coverage

| Scenario | XEP-0513? | XEP-0372? | Nick-in-body? | Result |
|----------|-----------|-----------|---------------|--------|
| Future client: `@hermes /status` | ✅ `jid` match | — | ✅ | Layer 0 catches + precise strip |
| Conversations: `@hermes /status` | ❌ (not yet) | ✅ `type="mention"` | ✅ | Layer 1 catches + nick strip |
| Gajim: `hermes: /status` | ❌ | ❌ | ✅ | Layer 2 catches + nick strip |
| Group mention: `@channel /help` | ✅ `mentions="#channel"` | ❌ | ❌ | Layer 0 catches |
| Bare `/status` in MUC | ❌ | ❌ | ❌ | Neither (requires `require_mention=false`) |

## Future: occupant-id support

XEP-0513 prefers `occupantid` (XEP-0421) over `jid` for MUC mentions:
- More stable — survives nick changes
- Prevents JID leaks in semi-anonymous rooms
- Requires XEP-0421 support in the adapter (not yet implemented)
