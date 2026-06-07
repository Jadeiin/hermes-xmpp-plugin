#!/usr/bin/env python3
"""Unit tests for MUC @mention detection — XEP-0513, XEP-0372, nick fallback.

Run with: python -m pytest tests/test_mention_detection.py -v
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import unittest.mock

# -----------------------------------------------------------------
# Mock gateway / tools modules (same pattern as test_first_class_features.py)
# -----------------------------------------------------------------
gateway_mod = unittest.mock.MagicMock()
sys.modules["gateway"] = gateway_mod

gw_config = unittest.mock.MagicMock()
sys.modules["gateway.config"] = gw_config
gw_config.Platform = type("Platform", (), {"__init__": lambda self, *a, **k: None})
gw_config.PlatformConfig = type("PlatformConfig", (), {"__init__": lambda self, *a, **k: None})

gw_platforms = unittest.mock.MagicMock()
sys.modules["gateway.platforms"] = gw_platforms

gw_base = unittest.mock.MagicMock()
sys.modules["gateway.platforms.base"] = gw_base

class _FakeMessageEvent:
    def __init__(self, *, text="", message_type=None, source=None, raw_message=None,
                 message_id=None, reply_to_message_id=None, reply_to_text=None, metadata=None,
                 media_urls=None, media_types=None):
        self.text = text
        self.message_type = message_type
        self.source = source
        self.raw_message = raw_message
        self.message_id = message_id
        self.reply_to_message_id = reply_to_message_id
        self.reply_to_text = reply_to_text
        self.metadata = metadata or {}
        self.media_urls = media_urls or []
        self.media_types = media_types or []

gw_base.MessageEvent = _FakeMessageEvent
gw_base.MessageType = type("MessageType", (), {
    "TEXT": "text", "IMAGE": "image", "COMMAND": "command",
    "VOICE": "voice", "PHOTO": "photo", "VIDEO": "video",
    "AUDIO": "audio", "DOCUMENT": "document"
})

class _FakeProcessingOutcome:
    SUCCESS = 0; FAILURE = 1; CANCELLED = 2
gw_base.ProcessingOutcome = _FakeProcessingOutcome
gw_base.SendResult = type("SendResult", (), {
    "__init__": lambda s, **kw: s.__dict__.update(kw) or None,
})

async def _mock_cache_audio_from_url(url: str, ext: str = ".ogg") -> str:
    return f"/tmp/mock_audio{ext}"
gw_base.cache_audio_from_url = _mock_cache_audio_from_url

async def _mock_cache_image_from_url(url: str, ext: str = ".jpg") -> str:
    return f"/tmp/mock_image{ext}"
gw_base.cache_image_from_url = _mock_cache_image_from_url

def _mock_truncate(self, content, max_len, **kw):
    if max_len <= 0 or len(content) <= max_len:
        return [content]
    chunks = []
    for i in range(0, len(content), max_len):
        chunks.append(content[i:i + max_len])
    return chunks

gw_base.BasePlatformAdapter = type("BasePlatformAdapter", (), {
    "__init__": lambda s, *a, **k: setattr(s, "config", a[0] if a else None) or None,
    "emit_message_raw": lambda *a, **kw: None,
    "on_processing_start": lambda *a, **kw: None,
    "on_processing_complete": lambda *a, **kw: None,
    "send": lambda *a, **kw: None,
    "truncate_message": _mock_truncate,
    "build_source": lambda self, **kw: MagicMock(),  # minimal: returns a Mock for SessionSource
})

gw_models = unittest.mock.MagicMock()
sys.modules["gateway.platforms.models"] = gw_models
gw_models.ChatContext = type("ChatContext", (), {"__init__": lambda self, **kw: None})

gw_util = unittest.mock.MagicMock()
sys.modules["gateway.util"] = gw_util

tools_mod = unittest.mock.MagicMock()
sys.modules["tools"] = tools_mod
tools_gateway = unittest.mock.MagicMock()
sys.modules["tools.clarify_gateway"] = tools_gateway
tools_gateway.mark_awaiting_text = unittest.mock.MagicMock()

slixmpp_omemo_mod = unittest.mock.MagicMock()
sys.modules["slixmpp_omemo"] = slixmpp_omemo_mod
slixmpp_omemo_mod.TrustLevel = type("TrustLevel", (), {})
slixmpp_omemo_mod.XEP_0384 = type("XEP_0384", (), {})

omemo_mod = unittest.mock.MagicMock()
sys.modules["omemo"] = omemo_mod
omemo_storage = unittest.mock.MagicMock()
sys.modules["omemo.storage"] = omemo_storage
class _FakeStorage:
    def __init__(self): pass
    async def _load(self, key): pass
    async def _store(self, key, value): pass
omemo_storage.Just = type("Just", (), {"__init__": lambda self, *a, **k: None})
omemo_storage.Maybe = type("Maybe", (), {})
omemo_storage.Nothing = type("Nothing", (), {"__init__": lambda self, *a, **k: None})
omemo_storage.Storage = _FakeStorage

omemo_types = unittest.mock.MagicMock()
sys.modules["omemo.types"] = omemo_types
omemo_types.DeviceInformation = type("DeviceInformation", (), {})
omemo_types.JSONType = type("JSONType", (), {})

# Import adapter after all mocks are set up
import adapter  # noqa: E402
adapter.BasePlatformAdapter = gw_base.BasePlatformAdapter
adapter.MessageEvent = gw_base.MessageEvent
adapter.MessageType = gw_base.MessageType
adapter.ProcessingOutcome = gw_base.ProcessingOutcome
adapter.SendResult = gw_base.SendResult


# -----------------------------------------------------------------
# Helpers: fake stanza objects with slixmpp-like stanza access
# -----------------------------------------------------------------

class _FakeJID:
    """Minimal JID for mention jid matching."""
    def __init__(self, jid_str: str):
        self._full = jid_str
        self.bare = jid_str.split("/")[0]
    def __str__(self):
        return self._full


class _FakeMention:
    """Mimics slixmpp xep_0513 Mention stanza object."""
    def __init__(self, *, jid=None, begin=None, end=None, mentions=None, occupantid=None):
        self._jid = jid
        self._begin = begin
        self._end = end
        self._mentions = mentions
        self._occupantid = occupantid

    def __getitem__(self, key):
        return getattr(self, f"_{key}", None)


class _FakeReference:
    """Mimics slixmpp xep_0372 Reference stanza object."""
    def __init__(self, *, type=None, uri=None, begin=None, end=None):
        self._type = type
        self._uri = uri
        self._begin = begin
        self._end = end
        self._id = None

    def __getitem__(self, key):
        return getattr(self, f"_{key}", None)


def _make_groupchat_stanza(*, body="", mentions=None, references=None,
                           from_jid="room@conf.example.org/poesty",
                           from_resource="poesty",
                           own_bare="hermes@example.org",
                           nick="hermes",
                           room="room@conf.example.org"):
    """Build a fake slixmpp groupchat Message stanza.

    Supports `stanza["mentions"]` and `stanza["references"]` access
    via a catch-all __getitem__ that delegates to a lookup dict.
    """
    lookup = {}
    if mentions is not None:
        lookup["mentions"] = mentions
    if references is not None:
        lookup["references"] = references

    class _FakeStanza:

        def __getitem__(self, key):
            # Known keys that the adapter reads
            if key == "type":
                return "groupchat"
            if key == "body":
                return body
            if key in lookup:
                return lookup[key]
            # Any unknown key: return None (not raise) to mimic slixmpp
            return None

        def get_from(self):
            # Return a JID-like object with .bare and .resource attributes
            class _FakeJIDFrom:
                def __init__(s):
                    s.bare = from_jid.rsplit("/", 1)[0] if "/" in from_jid else from_jid
                    s.resource = from_jid.rsplit("/", 1)[1] if "/" in from_jid else ""
                def __str__(s):
                    return from_jid
            return _FakeJIDFrom()

        def get(self, key, default=None):
            if key in lookup:
                return lookup[key]
            if key == "type":
                return "groupchat"
            if key == "body":
                return body
            if key == "id":
                return "stanza-id-001"
            return default

    return _FakeStanza()


# -----------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------

@pytest.fixture
def adapter_in_muc():
    """Return an XmppAdapter configured for MUC room detection."""
    cfg = MagicMock()
    cfg.jid = "hermes@example.org"
    cfg.password = "secret"
    cfg.home_channel = None
    cfg.fileserver_url = None

    a = adapter.XmppAdapter(cfg)
    a._self_bare = "hermes@example.org"
    a.client = None  # no OMEMO, no roster — just detect mentions
    a.allow_all_users = True
    a._registered_plugins = {
        "xep_0461", "xep_0372", "xep_0513", "xep_045",
        "xep_0444", "xep_0447", "xep_0363", "xep_0066",
        "xep_0045",
    }
    a._known_mucs = {"room@conf.example.org"}
    # Create a proper RoomConfig-like mock that returns actual nick string
    room_cfg = MagicMock()
    room_cfg.room = "room@conf.example.org"
    room_cfg.nick = "hermes"
    a.muc_rooms = [room_cfg]
    a.muc_nick = "hermes"
    a._muc_require_mention = False  # default: all MUC messages pass
    a.build_source = lambda **kw: MagicMock()  # patch directly on instance
    return a


# -----------------------------------------------------------------
# Tests: XEP-0513 individual mention via JID
# -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_xep0513_jid_mention_detected(adapter_in_muc):
    """XEP-0513 <mention jid="hermes@example.org"> should be detected."""
    events = []

    async def _capture(evt):
        events.append(evt)

    adapter_in_muc.handle_message = _capture
    adapter_in_muc._known_mucs = {"room@conf.example.org"}

    stanza = _make_groupchat_stanza(
        body="@hermes hello",
        mentions=[
            _FakeMention(
                jid=_FakeJID("hermes@example.org"),
                begin=0, end=7,
            ),
        ],
    )

    await adapter_in_muc._on_message(stanza)
    assert len(events) == 1
    # begin/end stripping: "@hermes" (0:7) should be cut
    # " hello" → ".lstrip()" → "hello"
    assert events[0].text == "hello"


@pytest.mark.asyncio
async def test_xep0513_begin_end_stripping_precise(adapter_in_muc):
    """XEP-0513 begin/end cuts exact span, not just nick prefix."""
    events = []

    async def _capture(evt):
        events.append(evt)

    adapter_in_muc.handle_message = _capture
    adapter_in_muc._known_mucs = {"room@conf.example.org"}

    # Mention is "hermes" at indices 12:18, not at start of body
    stanza = _make_groupchat_stanza(
        body="hello there hermes: what's up?",
        mentions=[
            _FakeMention(
                jid=_FakeJID("hermes@example.org"),
                begin=12, end=18,
            ),
        ],
    )

    await adapter_in_muc._on_message(stanza)
    assert len(events) == 1
    # "hello there " + ": what's up?" → strip leading whitespace
    # Nick prefix stripping should NOT fire (body doesn't start with "hermes")
    # Only begin/end cut fires: remove chars 12-18 ("hermes")
    expected = ("hello there " + ": what's up?").lstrip()
    assert events[0].text == expected


# -----------------------------------------------------------------
# Tests: XEP-0513 group mentions
# -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_xep0513_group_mention_detected(adapter_in_muc):
    """XEP-0513 <mention mentions="#channel"> should be detected (e.g. @everyone)."""
    events = []

    async def _capture(evt):
        events.append(evt)

    adapter_in_muc.handle_message = _capture
    adapter_in_muc._known_mucs = {"room@conf.example.org"}

    stanza = _make_groupchat_stanza(
        body="@channel important announcement",
        mentions=[
            _FakeMention(mentions="urn:xmpp:mentions:0#channel"),
        ],
    )

    await adapter_in_muc._on_message(stanza)
    assert len(events) == 1
    # Group mention: no begin/end, no nick prefix → body unchanged
    assert events[0].text == "@channel important announcement"


# -----------------------------------------------------------------
# Tests: XEP-0372 mention (regression)
# -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_xep0372_mention_still_detected(adapter_in_muc):
    """XEP-0372 <reference type='mention' uri='xmpp:hermes@...'> still works."""
    events = []

    async def _capture(evt):
        events.append(evt)

    adapter_in_muc.handle_message = _capture
    adapter_in_muc._known_mucs = {"room@conf.example.org"}

    stanza = _make_groupchat_stanza(
        body="hermes: /status",
        references=[
            _FakeReference(type="mention", uri="xmpp:hermes@example.org"),
        ],
    )

    await adapter_in_muc._on_message(stanza)
    assert len(events) == 1
    # XEP-0372 has no begin/end stripping in current code
    # Nick-based prefix strip should fire: "hermes: /status" → "/status"
    assert events[0].text == "/status"


# -----------------------------------------------------------------
# Tests: Nick-in-body fallback (regression)
# -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_nick_in_body_fallback(adapter_in_muc):
    """When no XEP stanza, nick substring in body (Gajim-style) still works."""
    events = []

    async def _capture(evt):
        events.append(evt)

    adapter_in_muc.handle_message = _capture
    adapter_in_muc._known_mucs = {"room@conf.example.org"}

    stanza = _make_groupchat_stanza(
        body="hermes: /help",   # Gajim "input-mention" format
        from_resource="poesty",
    )

    await adapter_in_muc._on_message(stanza)
    assert len(events) == 1
    assert events[0].text == "/help"


@pytest.mark.asyncio
async def test_nick_in_body_no_prefix_strip_when_nick_mid_body(adapter_in_muc):
    """Nick-in-body triggers mention but prefix strip only fires at start of body."""
    events = []

    async def _capture(evt):
        events.append(evt)

    adapter_in_muc.handle_message = _capture
    adapter_in_muc._known_mucs = {"room@conf.example.org"}

    stanza = _make_groupchat_stanza(
        body="hey hermes what do you think?",
        from_resource="poesty",
    )

    await adapter_in_muc._on_message(stanza)
    assert len(events) == 1
    # Nick is mid-body, not at prefix → no strip
    assert events[0].text == "hey hermes what do you think?"


# -----------------------------------------------------------------
# Tests: require_mention gating
# -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_require_mention_drops_unmentioned(adapter_in_muc):
    """When require_mention=True, unmentioned MUC messages are dropped."""
    events = []

    async def _capture(evt):
        events.append(evt)

    adapter_in_muc.handle_message = _capture
    adapter_in_muc._known_mucs = {"room@conf.example.org"}
    adapter_in_muc._muc_require_mention = True

    stanza = _make_groupchat_stanza(
        body="just chatting",  # no mention of "hermes"
        from_resource="poesty",
    )

    await adapter_in_muc._on_message(stanza)
    assert len(events) == 0  # dropped


@pytest.mark.asyncio
async def test_require_mention_passes_mentioned(adapter_in_muc):
    """When require_mention=True and bot IS mentioned, message passes."""
    events = []

    async def _capture(evt):
        events.append(evt)

    adapter_in_muc.handle_message = _capture
    adapter_in_muc._known_mucs = {"room@conf.example.org"}
    adapter_in_muc._muc_require_mention = True

    stanza = _make_groupchat_stanza(
        body="hermes: /status",
        from_resource="poesty",
    )

    await adapter_in_muc._on_message(stanza)
    assert len(events) == 1
    assert events[0].text == "/status"


# -----------------------------------------------------------------
# Tests: Layer priority (0513 > 0372 > nick)
# -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_xep0513_priority_over_0372(adapter_in_muc):
    """XEP-0513 detection should fire before XEP-0372, using 0513's begin/end."""
    events = []

    async def _capture(evt):
        events.append(evt)

    adapter_in_muc.handle_message = _capture
    adapter_in_muc._known_mucs = {"room@conf.example.org"}

    # Both 0513 and 0372 mention elements present
    # 0513 says begin=0,end=7; 0372 says uri matches but has no begin/end
    stanza = _make_groupchat_stanza(
        body="@hermes hello",
        mentions=[
            _FakeMention(jid=_FakeJID("hermes@example.org"), begin=0, end=7),
        ],
        references=[
            _FakeReference(type="mention", uri="xmpp:hermes@example.org"),
        ],
    )

    await adapter_in_muc._on_message(stanza)
    assert len(events) == 1
    # 0513 begin/end stripping should win: "@hermes " → "hello"
    assert events[0].text == "hello"


@pytest.mark.asyncio
async def test_xep0372_used_when_0513_absent(adapter_in_muc):
    """When XEP-0513 is absent, XEP-0372 is used."""
    events = []

    async def _capture(evt):
        events.append(evt)

    adapter_in_muc.handle_message = _capture
    adapter_in_muc._known_mucs = {"room@conf.example.org"}

    stanza = _make_groupchat_stanza(
        body="hermes: hi",
        # No XEP-0513 mentions
        references=[
            _FakeReference(type="mention", uri="xmpp:hermes@example.org"),
        ],
    )

    await adapter_in_muc._on_message(stanza)
    assert len(events) == 1
    assert events[0].text == "hi"


@pytest.mark.asyncio
async def test_nick_used_when_both_0513_and_0372_absent(adapter_in_muc):
    """When neither 0513 nor 0372, nick-in-body fallback."""
    events = []

    async def _capture(evt):
        events.append(evt)

    adapter_in_muc.handle_message = _capture
    adapter_in_muc._known_mucs = {"room@conf.example.org"}

    stanza = _make_groupchat_stanza(
        body="hermes: hey",
        from_resource="poesty",
    )

    await adapter_in_muc._on_message(stanza)
    assert len(events) == 1
    assert events[0].text == "hey"


# -----------------------------------------------------------------
# Tests: XEP-0513 different JID not matched
# -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_xep0513_different_jid_not_detected(adapter_in_muc):
    """XEP-0513 mention of a different user should NOT trigger mention of bot."""
    events = []

    async def _capture(evt):
        events.append(evt)

    adapter_in_muc.handle_message = _capture
    adapter_in_muc._known_mucs = {"room@conf.example.org"}
    adapter_in_muc._muc_require_mention = True  # block unmentioned

    stanza = _make_groupchat_stanza(
        body="@alice look at this",
        mentions=[
            _FakeMention(jid=_FakeJID("alice@example.org"), begin=0, end=6),
        ],
    )

    await adapter_in_muc._on_message(stanza)
    # bot is "hermes@example.org" not "alice@example.org"
    # No nick-in-body fallback either (no "hermes" in body)
    assert len(events) == 0
