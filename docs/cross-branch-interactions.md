# XMPP Adapter Feature Interaction Matrix

Cross-cutting analysis of how features interact with OMEMO encryption,
MUC vs DM, and each other. Use this when adding/modifying features to
avoid silent breakage in encrypted or groupchat paths.

## Table of Contents

1. [OMEMO: The Parallel Path Problem](#1-omemo-the-parallel-path-problem)
2. [Feature × OMEMO × Context Matrix](#2-feature--omemo--context-matrix)
3. [Feature Interaction Details](#3-feature-interaction-details)
   - [3.1 Markup (XEP-0394 + XEP-0071)](#31-markup-xep-0394--xep-0071)
   - [3.2 Chat Markers / Read Receipts (XEP-0333 + XEP-0184)](#32-chat-markers--read-receipts-xep-0333--xep-0184)
   - [3.3 Geo-Location (XEP-0080 + geo: URI)](#33-geo-location-xep-0080--geo-uri)
   - [3.4 Mentions (XEP-0513 + XEP-0372 + nick)](#34-mentions-xep-0513--xep-0372--nick)
   - [3.5 Reactions (XEP-0444)](#35-reactions-xep-0444)
   - [3.6 Replies (XEP-0461)](#36-replies-xep-0461)
   - [3.7 Chat States (XEP-0085)](#37-chat-states-xep-0085)
   - [3.8 Edit/Delete (XEP-0308 + XEP-0424)](#38-editdelete-xep-0308--xep-0424)
   - [3.9 Voice/SFS Media (XEP-0447 + XEP-0363)](#39-voicesfs-media-xep-0447--xep-0363)
4. [Stanza Lifecycle: What Survives Encryption](#4-stanza-lifecycle-what-survives-encryption)

---

## 1. OMEMO: The Parallel Path Problem

The adapter has **two independent send paths** that must stay in sync:

```
send()
  ├── OMEMO path: _send_encrypted()
  │     encrypt_message(stanza) → copy + clear (ALL non-body elements lost)
  │     → re-attach features manually on the new message object
  │
  └── Plaintext path: inline in send()
        make_message() → attach features → .send()
```

**Rule:** Every feature added to the plaintext path MUST also be added to
`_send_encrypted()`. The skill's OMEMO checklist (Pitfalls section) covers
the exact pattern.

## 2. Feature × OMEMO × Context Matrix

| Feature | OMEMO path | Plaintext DM | MUC groupchat | Notes |
|---------|-----------|-------------|---------------|-------|
| **Markup (0394/0071)** | ❌ stripped by encrypt | ✅ attached before send | ✅ attached before send | XHTML-IM on discarded original, not on wire for OMEMO |
| **Chat Markers (0333)** | ✅ cleartext sibling | ✅ works | ✅ works (mtype=groupchat) | Sent on original stanza after `handle_message`, not on encrypted payload |
| **Delivery Receipts (0184)** | ❌ not implemented | ❌ not implemented | ❌ not implemented | Niche; superseded by 0333 in practice |
| **Geo-Location (0080)** | N/A (inbound only) | ✅ detected | ✅ detected | geo: URI in body survives OMEMO decryption |
| **Mentions (0513/0372)** | ❓ inside encrypted body | ✅ detected | ✅ detected | XEP-0513 elements inside encrypted payload → visible on `stanza_to_dispatch` |
| **Reactions (0444)** | ✅ cleartext sibling | ✅ works | ✅ works (mtype=groupchat) | Reactions are cleartext, NOT inside OMEMO payload |
| **Replies (0461)** | ✅ re-attached after encrypt | ✅ via make_reply() | ✅ via make_reply() | Uses room JID for @to (SHOULD be Full JID) |
| **Chat States (0085)** | ✅ re-attached after encrypt | ✅ embedded in body msg | ✅ embedded in body msg | Standalone states use make_message (OMEMO-safe) |
| **Edit Message (0308)** | ✅ `<replace>` restored post-encrypt | ✅ works | ⚠️ last-msg-only | Correction replaces most recent; MUC OK |
| **Delete Message (0424)** | ✅ works (1:1 only) | ✅ works | ❌ MUC blocked | Needs room-assigned `<stanza-id>` for MUC |
| **Voice/SFS Media (0447/0363)** | ❌ not OMEMO-encrypted | ✅ HTTP Upload | ✅ HTTP Upload | Media URLs in body; not encrypted separately |
| **Truncation (4000)** | ✅ chunks in _send_encrypted | ✅ chunks in send | ✅ chunks in send | Both paths call truncate_message() |

**Legend:**
- ✅ = fully supported
- ⚠️ = supported with limitations
- ❌ = not supported / stripped
- ❓ = depends on implementation details
- N/A = not applicable

## 3. Feature Interaction Details

### 3.1 Markup (XEP-0394 + XEP-0071)

**Pipeline:** `_build_markup()` → XEP-0394 `<markup>` → `to_xhtml_im()` → XEP-0071 `<html>`

**OMEMO interaction:**
```
send() plaintext path:
  stanza = make_message(mbody=chunk)
  markup = _build_markup(chunk)         # regex → <markup>
  stanza.xml.append(markup.xml)         # XEP-0394 on stanza
  xhtml = to_xhtml_im(chunk, markup)    # 0394 → XHTML-IM
  stanza.xml.append(xhtml.xml)          # XEP-0071 on stanza
  stanza.send()
  ✅ Both 0394 and 0071 on wire

send() → _send_encrypted() path:
  _send_encrypted(chat_id, content, ...)
  markup = _build_markup(chunk)
  stanza.xml.append(markup.xml)
  stanza.xml.append(xhtml.xml)
  message, _ = encrypt_message(stanza, ...)  # copy + clear — 0394/0071 LOST
  → message is a NEW object without markup
  ❌ XHTML-IM on discarded original; only body on wire  
```

**Why XHTML-IM doesn't leak cleartext:** `encrypt_message()` does `copy(stanza) + clear()`.
XHTML-IM stays on the discarded original. The encrypted message only has `<body>`.
No cleartext leak. Correct behavior.

**MUC vs DM:** Both work the same for markup. Config-gated via `xep_0394_enabled` / `xep_0071_enabled` (default false).

**Key pitfalls:**
- `markup.xml.append()` MOVES the element — `to_xhtml_im()` must read markup BEFORE append
- `markup.append(span)` not `markup.xml.append(span.xml)` — the latter bypasses iterables registry
- Span overlap from inline code regex matching inside ``` fences → `assert False` crash (fixed with lookbehind guards)

### 3.2 Chat Markers / Read Receipts (XEP-0333 + XEP-0184)

**XEP-0333 (Chat Markers):**
- **Sent:** displayed marker on inbound MUC/DM messages AFTER processing
- **Not sent:** for MAM-replayed (historical) messages
- **OMEMO-safe:** marker is a separate cleartext stanza, not inside encrypted payload
- **MUC:** `mtype="groupchat"`, sent to `from_full` (Full JID)

**XEP-0184 (Message Delivery Receipts):**
- **Not implemented.** XEP-0333 covers "displayed" which is what we need.
- XEP-0333 §1: "They complement XEP-0184, which tracks delivery, not display."
- Adding 0184 would require tracking outbound message receipts — low value for a bot.

**Inbound flow:**
```
_on_message()
  → process message
  → _mam_replaying? → skip marker
  → xep_0333.send_marker(mto=from_full, id=msg_id, marker="displayed", mtype=mtype)
```

### 3.3 Geo-Location (XEP-0080 + geo: URI)

**Two detection paths:**

| Path | Stanza access | Client | OMEMO-safe? |
|------|--------------|--------|-------------|
| XEP-0080 `<geoloc>` | `stanza_to_dispatch["geoloc"]["lat"]` | Few clients | ✅ inside encrypted body |
| geo: URI in body | regex on `body` | Conversations | ✅ inside encrypted body |

**Body replacement logic:**
| Scenario | Behavior |
|----------|----------|
| Empty body + `<geoloc>` | Set body to formatted location text |
| `geo:lat,lon` only | Strip URI, replace with location text |
| `geo:lat,lon extra text` | Strip URI, keep "extra text", append location |
| `<geoloc>` + user text | Append location to user text |

**MUC vs DM:** Both paths work the same; no MUC-specific behavior.

**Fixed bug (2026-06-07):** `if not body` guard blocked geo: URI replacement because
Conversations sets body to the geo: URI string (non-empty). Changed to nuanced check
handling all four cases above.

### 3.4 Mentions (XEP-0513 + XEP-0372 + nick)

Three-layer detection, newest standard first:

| Layer | Standard | Stanza access | begin/end strip |
|-------|----------|--------------|-----------------|
| 0 | XEP-0513 Explicit Mentions | `stanza["mentions"]` | ✅ precise substring indices |
| 1 | XEP-0372 References | `stanza["references"]` | ❌ fallback to nick |
| 2 | nick-in-body | string match | ❌ fallback to nick |

**OMEMO interaction:** XEP-0513 `<mention>` and XEP-0372 `<reference>` elements are
inside the encrypted payload. They appear on `stanza_to_dispatch` (the decrypted stanza),
so detection works correctly.

**Group mentions (XEP-0513 only):** Supports `#channel`, `#moderators`, `#participants`,
`#owner`, `#admin`, `#member`. These trigger `mentioned=True` for any bot in the room.

**MUC gating:** `require_mention` config drops unmentioned MUC messages. Default false.
When true, the bot only responds when explicitly mentioned (XEP-0513/0372/nick).

### 3.5 Reactions (XEP-0444)

**Key distinction:** Reactions are **cleartext siblings** — they sit alongside the
encrypted `<body>`, NOT inside it.

**Inbound detection:**
```python
# reactions are on the ORIGINAL stanza, not stanza_to_dispatch
reactions_el = stanza["reactions"]   # ← use stanza, not decrypted version
```

This is critical: for OMEMO-encrypted chats, `stanza_to_dispatch` is the decrypted stanza
which does NOT carry `<reactions>`. Always check the original `stanza` for inbound reactions.

**Outbound sending:**
```python
mtype = "groupchat" if self._is_muc(chat_id) else "chat"
msg = self.client.make_message(mto=JID(chat_id), mtype=mtype)
self.client["xep_0444"].set_reactions(msg, target_id, reactions)
msg.enable("store")
msg.send()
```

**Gajim interop:** Gajim rejects reactions with `type="normal"` — MUST use `type="chat"` or `"groupchat"`.

**Clarify reactions:** `send_clarify()` uses numbered emoji reactions (1️⃣-4️⃣ + ✏️).
All reactions set in ONE `set_reactions()` call (reactions REPLACE, not append).

**Exec approval reactions:** ✅/❎ seed reactions for gateway command approval.

### 3.6 Replies (XEP-0461)

**MUC reply @to:** Uses room bare JID instead of Full JID (SHOULD level in spec).
All major clients resolve via `reply_id` (stanza-id), not `reply_to`. No known breakage.

**MUC reply @id:** Uses `<stanza-id>` (XEP-0359) for groupchat, `<origin-id>` for DM.

**OMEMO path:** `<reply>` element is attached AFTER `encrypt_message()` returns:
```python
message, errors = await encrypt_message(stanza, ...)
# encrypt_message does copy(stanza) + clear() — reply is lost
message["reply"]["to"] = JID(chat_id)
message["reply"]["id"] = reply_to
message.send()
```

**API:** Uses `make_reply()` not `send_reply()` (upstream TypeError bug fixed locally).
`make_reply` creates stanza without sending, giving caller control over additional elements.

### 3.7 Chat States (XEP-0085)

**Two patterns:**

1. **Embedded in body messages** (Poezio pattern):
   ```python
   stanza["chat_state"] = "active"    # injected AFTER make_reply / make_message
   ```
   Used in `send()` for every chunk. OMEMO path: set on `message` after encrypt.

2. **Standalone states** (typing indicators):
   ```python
   msg = client.make_message(mto=chat_id, mtype=mtype)
   msg["chat_state"] = "composing"
   msg["no-store"] = True             # prevent MAM pollution
   msg.send()
   ```
   Used in `send_typing()` / `stop_typing()`. OMEMO-safe (cleartext).

**make_reply pitfall:** `make_reply()` passes all kwargs to `make_message()` which does NOT accept
`mchat_state`. Always inject `stanza["chat_state"] = "active"` AFTER stanza creation.

### 3.8 Edit/Delete (XEP-0308 + XEP-0424)

**Edit Message (XEP-0308):**
- Only MOST RECENT message can be corrected
- Gateway progress bubble pattern (send → edit → edit → new bubble) is compatible
- OMEMO: `<replace>` restored after encryption (same pattern as reply)
- MUC: Works (correction goes to room, only latest message replaceable)

**Delete Message (XEP-0424):**
- **1:1 DM:** ✅ Works. Uses client-generated message ID.
- **MUC groupchat:** ❌ Blocked. XEP-0424 §5.1 requires room-assigned `<stanza-id>`,
  which the adapter doesn't capture from reflected stanzas.
- **OMEMO:** Works for 1:1.

**MUC retraction limitation detail:**
```
Client sends:  <message id="client-id-123"><body>hello</body></message>
Server reflects: <message from="room@conf/bot"><stanza-id by="room@conf" id="room-id-456">...</message>
```
The adapter captures `client-id-123` but not `room-id-456`. XEP-0424 needs `room-id-456`.

### 3.9 Voice/SFS Media (XEP-0447 + XEP-0363)

**Inbound (XEP-0447 SFS + XEP-0066 OOB):**

| Path | Stanza access | Description |
|------|--------------|-------------|
| SFS (0447) | `stanza_to_dispatch["sfs"]["sources"]` → URL targets | Modern clients |
| OOB (0066) | `stanza_to_dispatch["oob"]["url"]` | Fallback for older clients |

**Voice note classification (`_mime_to_message_type`):**

| Priority | Check | Result |
|----------|-------|--------|
| 1 | Whitelist: ogg, opus, mp4 (.m4a), aac, x-m4a | → VOICE |
| 2 | Body-empty heuristic: any `audio/*` + empty body | → VOICE |
| 3 | Other `audio/*` with text body | → AUDIO |
| 4 | `image/*` | → PHOTO |
| 5 | `video/*` | → VIDEO |
| 6 | Everything else | → DOCUMENT |

**.m4a voice note pitfall (fixed):** Conversations' AAC recording produces `.m4a` with
MIME `audio/mp4`. Originally classified as AUDIO (suppressing STT). Fixed by adding
to whitelist AND adding body-empty heuristic.

**Outbound:** HTTP Upload (XEP-0363) → URL in message body. Not OMEMO-encrypted.

## 4. Stanza Lifecycle: What Survives Encryption

`slixmpp_omemo.encrypt_message()` does:
```python
message = copy(stanza)
message.clear()          # ALL child elements removed
# Only <body> is re-added inside the encrypted payload
```

**Elements on the original `stanza` before encryption:**

| Element | Survives? | Where it ends up |
|---------|-----------|-----------------|
| `<body>` | ✅ inside encrypted payload | `message` |
| `<replace>` (edit) | ❌ stripped | Must restore on `message` |
| `<reply>` (0461) | ❌ stripped | Must re-attach on `message` |
| `<thread>` (0201) | ❌ stripped | Must re-attach on `message` |
| `<active/>` (0085) | ❌ stripped | Must re-attach on `message` |
| `<markup>` (0394) | ❌ stripped | Discarded (no cleartext leak) |
| `<html>` (0071) | ❌ stripped | Discarded (no cleartext leak) |
| `<reactions>` (0444) | ✅ cleartext sibling | Unaffected (not on encrypted stanza) |
| `<marker>` (0333) | ✅ separate stanza | Unaffected (sent on separate stanza) |
| `<mention>` (0513) | ✅ inside encrypted body | Appears on `stanza_to_dispatch` after decrypt |
| `<reference>` (0372) | ✅ inside encrypted body | Appears on `stanza_to_dispatch` after decrypt |

**Inbound decryption:**
```
stanza (encrypted, OMEMO) → xep_0384.decrypt_message(stanza) → stanza_to_dispatch
  stanza:                  has cleartext reactions, chat markers
  stanza_to_dispatch:      has decrypted body, mentions, references, geoloc
```

**Two patterns for post-encryption element attachment:**

1. **"Restore"** — element was on `stanza` before, must copy to `message` after:
   - `<replace>` (edit_message)

2. **"Add-after"** — element is attached to `message` fresh after encryption:
   - `<reply>`, `<thread>`, `<active>` (chat_state)
