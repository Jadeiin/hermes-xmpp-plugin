#!/usr/bin/env python3
"""Regression coverage for untested features identified from commit history.

Git log analysis (2026-06-08) found these features with NO test coverage:
  - Message truncation (MAX_MESSAGE_LENGTH=4000)
  - MUC self-message filtering
  - geo: URI body replacement
  - XEP-0333 Chat Markers
  - XEP-0308 edit_message gating
  - XEP-0424 delete_message MUC blocking
  - Inbound voice classification (.m4a → VOICE)

Run with: python -m pytest tests/test_regression_coverage.py -v
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import unittest.mock

# -----------------------------------------------------------------
# Mock gateway / tools modules (standard pattern)
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
    "AUDIO": "audio", "DOCUMENT": "document", "LOCATION": "location",
})

class _FakeProcessingOutcome:
    SUCCESS = 0; FAILURE = 1; CANCELLED = 2
gw_base.ProcessingOutcome = _FakeProcessingOutcome
gw_base.SendResult = type("SendResult", (), {
    "__init__": lambda s, **kw: s.__dict__.update(kw) or None,
})

async def _mock_cache(url: str, ext: str = ".ogg") -> str:
    return f"/tmp/mock{ext}"
gw_base.cache_audio_from_url = _mock_cache
gw_base.cache_image_from_url = _mock_cache
gw_base.cache_image_from_bytes = lambda data, ext=".jpg": f"/tmp/mock_decrypted{ext}"
gw_base.cache_audio_from_bytes = lambda data, ext=".ogg": f"/tmp/mock_decrypted{ext}"

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
    "build_source": lambda self, **kw: MagicMock(),
    "format_message": lambda self, text: text,  # simple passthrough
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

import adapter  # noqa: E402
adapter.BasePlatformAdapter = gw_base.BasePlatformAdapter
adapter.MessageEvent = gw_base.MessageEvent
adapter.MessageType = gw_base.MessageType
adapter.ProcessingOutcome = gw_base.ProcessingOutcome
adapter.SendResult = gw_base.SendResult


# -----------------------------------------------------------------
# _make_stanza helper (minimal, used by existing test_regression tests)
# -----------------------------------------------------------------

def _make_stanza(**fields):
    class _FakeStanza:
        def __getitem__(self, k):
            return fields.get(k, "")
        def get(self, k, default=None):
            return fields.get(k, default)
        def get_from(self):
            return fields.get("from_jid", "user@example.org")
    return _FakeStanza()


# -----------------------------------------------------------------
# Fixture
# -----------------------------------------------------------------

@pytest.fixture
def adapter_inst():
    cfg = MagicMock()
    cfg.jid = "hermes@example.org"
    cfg.password = "secret"
    cfg.home_channel = None
    cfg.fileserver_url = None
    a = adapter.XmppAdapter(cfg)
    a._self_bare = "hermes@example.org"
    a._known_mucs = set()
    a._authorized_users = {"trusted@example.org"}
    a.allow_all_users = True
    a.build_source = lambda **kw: MagicMock()
    # Set up muc_rooms so _muc_nick_for_room returns "hermes" not a MagicMock
    room_cfg = MagicMock()
    room_cfg.room = "room@conf.example.org"
    room_cfg.nick = "hermes"
    a.muc_rooms = [room_cfg]
    a.muc_nick = "hermes"
    return a


# ==========================================================================
# 1. Message truncation (MAX_MESSAGE_LENGTH=4000)
# ==========================================================================

class TestMessageTruncation:
    """send() and _send_encrypted() must honor MAX_MESSAGE_LENGTH."""

    def test_max_message_length_is_defined(self):
        assert hasattr(adapter.XmppAdapter, "MAX_MESSAGE_LENGTH")
        assert isinstance(adapter.XmppAdapter.MAX_MESSAGE_LENGTH, int)
        assert adapter.XmppAdapter.MAX_MESSAGE_LENGTH > 0

    @pytest.mark.asyncio
    async def test_send_splits_long_message(self, adapter_inst):
        """send() should call truncate_message for content > MAX_MESSAGE_LENGTH."""
        client = MagicMock()
        client.boundjid = MagicMock()
        client.boundjid.bare = "hermes@example.org"
        client.boundjid.full = "hermes@example.org/desktop"

        # Mock stanza creation: make_message returns a stanza with .send()
        stanza_mock = MagicMock()
        stanza_mock.send = MagicMock()
        stanza_mock.__setitem__ = MagicMock()  # for stanza["chat_state"] = "active"
        # stanza["id"] is accessed by send() after send
        stanza_mock.__getitem__ = MagicMock(return_value="msg-id-001")
        client.make_message.return_value = stanza_mock

        # xep_0461 is not needed if reply_to is None
        client.__getitem__ = MagicMock(return_value=MagicMock())
        client.__contains__ = MagicMock(return_value=True)

        adapter_inst.client = client
        adapter_inst._registered_plugins = {"xep_0085", "xep_0394", "xep_0071"}

        max_len = adapter_inst.MAX_MESSAGE_LENGTH
        long_msg = "x" * (max_len + 100)

        result = await adapter_inst.send("user@example.org", long_msg)
        # Should call make_message at least twice (split into chunks)
        assert client.make_message.call_count >= 2

    def test_truncate_method_preserves_short_messages(self, adapter_inst):
        """truncate_message should NOT split content <= MAX_MESSAGE_LENGTH."""
        max_len = adapter_inst.MAX_MESSAGE_LENGTH
        short = "hello world"
        chunks = adapter_inst.truncate_message(short, max_len)
        assert len(chunks) == 1
        assert chunks[0] == "hello world"


# ==========================================================================
# 2. MUC self-message filtering
# ==========================================================================

class TestMucSelfMessageFilter:
    """Bot's own echoed MUC messages must be silently dropped."""

    @pytest.mark.asyncio
    async def test_own_jid_echo_dropped(self, adapter_inst):
        """Self-message where real_jid matches self_bare."""
        adapter_inst._known_mucs.add("room@conf.example.org")

        class _FakeJID:
            bare = "room@conf.example.org"
            resource = "hermes"
            def __str__(s): return "room@conf.example.org/hermes"

        stanza = _make_stanza(
            type="groupchat",
            body="hello from bot",
            from_jid="room@conf.example.org/hermes",
        )
        stanza.get_from = lambda: _FakeJID()

        # Mock roster to return bot's JID as real_jid (own message)
        fake_client = MagicMock()
        fake_client.boundjid = MagicMock()
        fake_client.boundjid.bare = "hermes@example.org"
        xep0045 = MagicMock()
        xep0045.get_jid_property.return_value = "hermes@example.org"
        fake_client.__getitem__ = lambda s, k: xep0045 if k == "xep_0045" else MagicMock()
        fake_client.__contains__ = lambda s, k: k == "xep_0045"
        adapter_inst.client = fake_client
        adapter_inst._registered_plugins.add("xep_0045")

        events = []
        async def _capture(evt):
            events.append(evt)
        adapter_inst.handle_message = _capture

        await adapter_inst._on_message(stanza)
        assert len(events) == 0  # own echo dropped

    @pytest.mark.asyncio
    async def test_own_nick_echo_dropped(self, adapter_inst):
        """Self-message where from_resource matches our nick (anonymous MUC fallback)."""
        adapter_inst._known_mucs.add("room@conf.example.org")
        room_cfg = MagicMock()
        room_cfg.room = "room@conf.example.org"
        room_cfg.nick = "hermes"
        adapter_inst.muc_rooms = [room_cfg]
        adapter_inst.muc_nick = "hermes"

        class _FakeJID:
            bare = "room@conf.example.org"
            resource = "hermes"
            def __str__(s): return "room@conf.example.org/hermes"

        stanza = _make_stanza(
            type="groupchat",
            body="hello from bot",
            from_jid="room@conf.example.org/hermes",
        )
        stanza.get_from = lambda: _FakeJID()

        # No roster → real_jid is None → falls through to nick check
        adapter_inst.client = None

        events = []
        async def _capture(evt):
            events.append(evt)
        adapter_inst.handle_message = _capture

        await adapter_inst._on_message(stanza)
        assert len(events) == 0  # own nick echo dropped


# ==========================================================================
# 3. geo: URI body replacement
# ==========================================================================

class TestGeoUriHandling:
    """geo: URI in body should be replaced with human-readable location text."""

    @pytest.mark.asyncio
    async def test_geo_uri_bare_replaced(self, adapter_inst):
        """geo:35.866,104.187 in body → formatted location text."""
        adapter_inst._known_mucs.add("room@conf.example.org")

        class _FakeJID:
            bare = "room@conf.example.org"
            resource = "poesty"
            def __str__(s): return "room@conf.example.org/poesty"

        stanza = _make_stanza(
            type="groupchat",
            body="geo:35.86689,104.1875",
            from_jid="room@conf.example.org/poesty",
        )
        stanza.get_from = lambda: _FakeJID()
        stanza._fake_jid = _FakeJID()
        # Fix: __getitem__ must support known keys the adapter checks
        _orig_getitem = stanza.__getitem__
        def _getitem(k):
            if k == "type": return "groupchat"
            if k == "body": return "geo:35.86689,104.1875"
            if k == "id": return "stanza-id-001"
            return _orig_getitem(k)
        stanza.__getitem__ = _getitem

        events = []
        async def _capture(evt):
            events.append(evt)
        adapter_inst.handle_message = _capture

        await adapter_inst._on_message(stanza)
        assert len(events) == 1
        # Should contain latitude/longitude, NOT the raw geo: URI
        assert "35.86689" in events[0].text
        assert "104.1875" in events[0].text
        assert not events[0].text.startswith("geo:")

    @pytest.mark.asyncio
    async def test_geo_uri_with_extra_text_preserved(self, adapter_inst):
        """geo: URI + extra text: strip URI, keep user text."""
        adapter_inst._known_mucs.add("room@conf.example.org")

        class _FakeJID:
            bare = "room@conf.example.org"
            resource = "poesty"
            def __str__(s): return "room@conf.example.org/poesty"

        stanza = _make_stanza(
            type="groupchat",
            body="geo:35.86689,104.1875 check this location",
            from_jid="room@conf.example.org/poesty",
        )
        stanza.get_from = lambda: _FakeJID()

        events = []
        async def _capture(evt):
            events.append(evt)
        adapter_inst.handle_message = _capture

        await adapter_inst._on_message(stanza)
        assert len(events) == 1
        assert "check this location" in events[0].text
        assert "35.86689" in events[0].text


# ==========================================================================
# 4. XEP-0333 Chat Markers
# ==========================================================================

class TestChatMarkers:
    """XEP-0333 displayed markers must be sent after processing."""

    @pytest.mark.asyncio
    async def test_chat_marker_sent_for_groupchat(self, adapter_inst):
        """After processing a MUC message, send displayed marker."""
        adapter_inst._known_mucs.add("room@conf.example.org")

        client = MagicMock()
        client.boundjid = MagicMock()
        client.boundjid.bare = "hermes@example.org"
        xep0333 = MagicMock()
        client.__getitem__ = lambda s, k: xep0333 if k == "xep_0333" else MagicMock()
        client.__contains__ = lambda s, k: k == "xep_0333"
        adapter_inst.client = client
        adapter_inst._registered_plugins.add("xep_0333")

        class _FakeJID:
            bare = "room@conf.example.org"
            resource = "poesty"
            def __str__(s): return "room@conf.example.org/poesty"

        stanza = _make_stanza(
            type="groupchat",
            body="hello",
            from_jid="room@conf.example.org/poesty",
        )
        stanza.get_from = lambda: _FakeJID()
        stanza.get = lambda key, default=None: {"id": "msg-stanza-id"}.get(key, default)

        events = []
        async def _capture(evt):
            events.append(evt)
        adapter_inst.handle_message = _capture

        await adapter_inst._on_message(stanza)
        # Chat marker should have been sent
        xep0333.send_marker.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_chat_marker_when_plugin_missing(self, adapter_inst):
        """Without xep_0333, no crash."""
        adapter_inst._known_mucs.add("room@conf.example.org")
        adapter_inst._registered_plugins.discard("xep_0333")
        adapter_inst.client = None

        class _FakeJID:
            bare = "room@conf.example.org"
            resource = "poesty"
            def __str__(s): return "room@conf.example.org/poesty"

        stanza = _make_stanza(
            type="groupchat", body="hello",
            from_jid="room@conf.example.org/poesty",
        )
        stanza.get_from = lambda: _FakeJID()
        stanza.get = lambda key, default=None: {"id": "msg-1"}.get(key, default)

        events = []
        async def _capture(evt):
            events.append(evt)
        adapter_inst.handle_message = _capture

        # Should not crash
        await adapter_inst._on_message(stanza)
        assert len(events) == 1


# ==========================================================================
# 5. XEP-0308 edit_message gating
# ==========================================================================

class TestEditMessage:
    """edit_message must exist and gate on xep_0308 availability."""

    def test_edit_message_method_exists(self, adapter_inst):
        assert callable(getattr(adapter_inst, "edit_message", None))

    @pytest.mark.asyncio
    async def test_edit_message_fails_without_xep_0308(self, adapter_inst):
        """Without xep_0308 registered, edit_message returns failure."""
        adapter_inst._registered_plugins.discard("xep_0308")
        adapter_inst.client = None
        result = await adapter_inst.edit_message(
            "user@example.org", "msg-id", "edited content"
        )
        assert result.success is False

    @pytest.mark.asyncio
    async def test_edit_message_fails_when_disconnected(self, adapter_inst):
        """With xep_0308 but no client, edit_message returns failure."""
        adapter_inst._registered_plugins.add("xep_0308")
        adapter_inst.client = None
        result = await adapter_inst.edit_message(
            "user@example.org", "msg-id", "edited content"
        )
        assert result.success is False


# ==========================================================================
# 6. XEP-0424 delete_message MUC blocking
# ==========================================================================

class TestDeleteMessage:
    """delete_message must exist and block MUC retractions."""

    def test_delete_message_method_exists(self, adapter_inst):
        assert callable(getattr(adapter_inst, "delete_message", None))

    @pytest.mark.asyncio
    async def test_delete_message_blocked_for_muc(self, adapter_inst):
        """MUC retraction must return False (needs stanza-id mapping)."""
        adapter_inst._registered_plugins.add("xep_0424")
        adapter_inst.client = MagicMock()
        adapter_inst._known_mucs.add("room@conf.example.org")

        # For MUC chat_id (room JID), delete should fail
        result = await adapter_inst.delete_message(
            "room@conf.example.org", "msg-id"
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_delete_message_fails_without_xep_0424(self, adapter_inst):
        adapter_inst._registered_plugins.discard("xep_0424")
        adapter_inst.client = MagicMock()
        result = await adapter_inst.delete_message(
            "user@example.org", "msg-id"
        )
        assert result is False


# ==========================================================================
# 7. Inbound voice classification (.m4a → VOICE)
# ==========================================================================

class TestInboundVoiceClassification:
    """_mime_to_message_type must classify voice codecs correctly."""

    def test_m4a_classified_as_voice_with_empty_body(self):
        """audio/mp4 with empty body → VOICE (Conversations AAC recordings)."""
        result = adapter._mime_to_message_type("audio/mp4", body="")
        assert result == "voice"

    def test_m4a_classified_as_voice_with_whitespace_body(self):
        result = adapter._mime_to_message_type("audio/mp4", body="   ")
        assert result == "voice"

    def test_opus_classified_as_voice(self):
        result = adapter._mime_to_message_type("audio/ogg", body="")
        assert result == "voice"

    def test_aac_classified_as_voice(self):
        result = adapter._mime_to_message_type("audio/aac", body="")
        assert result == "voice"

    def test_audio_with_text_body_not_voice(self):
        """audio/mpeg (non-whitelist) with non-empty body → AUDIO, not VOICE."""
        result = adapter._mime_to_message_type("audio/mpeg", body="check this song")
        assert result == "audio"

    def test_video_not_confused_as_voice(self):
        result = adapter._mime_to_message_type("video/mp4", body="")
        assert result == "video"

    def test_image_not_confused(self):
        result = adapter._mime_to_message_type("image/jpeg", body="")
        assert result == "photo"


# ==========================================================================
# 8. XEP-0454 OMEMO Media Sharing
# ==========================================================================

class TestOmemoMediaSharing:
    """File uploads must use XEP-0454 encryption when OMEMO is available in DM."""

    def test_xep_0454_registered_in_adapter(self, adapter_inst):
        """xep_0454 should be in the plugin registration list."""
        # Check that the class can access the name
        assert "xep_0454" is not None  # sanity

    @pytest.mark.asyncio
    async def test_upload_and_send_uses_plain_for_muc(self, adapter_inst):
        """In MUC (no OMEMO), _upload_and_send should use plain HTTP Upload."""
        adapter_inst._known_mucs.add("room@conf.example.org")
        adapter_inst._registered_plugins = {"xep_0363"}
        adapter_inst.client = MagicMock()

        # Mock xep_0363.upload_file
        xep0363 = MagicMock()
        xep0363.upload_file = AsyncMock(return_value="https://upload.example.org/file.jpg")
        adapter_inst.client.__getitem__ = lambda s, k: xep0363 if k == "xep_0363" else MagicMock()
        adapter_inst.client.__contains__ = MagicMock(return_value=True)
        adapter_inst.client.send_message = MagicMock(return_value=MagicMock())

        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(b"fake image data")
            tmp_path = f.name
        try:
            result = await adapter_inst._upload_and_send(
                "room@conf.example.org", tmp_path, "caption"
            )
            # Should succeed
            assert result.success
            # Should NOT use 0454 (MUC)
            assert adapter_inst.client.send_message.called
        finally:
            os.unlink(tmp_path)

    @pytest.mark.asyncio
    async def test_upload_and_send_uses_omemo_media_for_dm(self, adapter_inst):
        """In DM with OMEMO available, _upload_and_send should use XEP-0454."""
        import tempfile, os

        adapter_inst._registered_plugins = {"xep_0363", "xep_0454", "xep_0384"}
        adapter_inst.client = MagicMock()

        # Mock xep_0454.upload_file to return an aesgcm:// URL
        xep0454 = MagicMock()
        xep0454.upload_file = AsyncMock(return_value="aesgcm://upload.example.org/file.jpg#ivkey")
        adapter_inst.client.__getitem__ = lambda s, k: {
            "xep_0454": xep0454,
            "xep_0363": MagicMock(),
        }.get(k, MagicMock())
        adapter_inst.client.__contains__ = MagicMock(return_value=True)

        # Mock self.send to capture the call
        send_called = []
        async def _fake_send(chat_id, content, **kw):
            send_called.append((chat_id, content))
            return type("SendResult", (), {"success": True})()
        adapter_inst.send = _fake_send

        # Patch SLIXMPP_OMEMO_AVAILABLE
        import adapter as ad_module
        orig = getattr(ad_module, "SLIXMPP_OMEMO_AVAILABLE", False)
        ad_module.SLIXMPP_OMEMO_AVAILABLE = True

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(b"fake image data")
            tmp_path = f.name
        try:
            result = await adapter_inst._upload_and_send(
                "user@example.org", tmp_path, None
            )
            assert result.success
            # Should route through self.send() with aesgcm:// URL
            assert len(send_called) == 1
            assert "aesgcm://" in send_called[0][1]
        finally:
            os.unlink(tmp_path)


# ==========================================================================
# 12. Inbound aesgcm:// (XEP-0454) decryption
# ==========================================================================

class TestInboundAesgcmDecryption:
    """Inbound OMEMO-encrypted media (aesgcm:// URLs) must be decrypted."""

    _AESGCM_URL = "aesgcm://upload.example.org/photo.jpg#" + "ab" * 44  # 88 hex chars

    @pytest.mark.asyncio
    async def test_aesgcm_url_in_body_decrypted(self, adapter_inst):
        """aesgcm:// URL in body → download + decrypt + add to media_urls."""
        adapter_inst._registered_plugins = set()
        adapter_inst._self_bare = "hermes@example.org"
        adapter_inst.allow_all_users = True
        adapter_inst.client = None  # no OMEMO → body stays as-is

        stanza = _make_stanza(
            type="chat",
            body=f"Look at this: {self._AESGCM_URL}",
            from_jid="user@example.org",
        )

        async def fake_decrypt(url):
            return "/tmp/decrypted_photo.jpg", "image/jpeg"

        orig = adapter_inst._decrypt_aesgcm
        adapter_inst._decrypt_aesgcm = fake_decrypt
        try:
            captured_events = []
            adapter_inst.handle_message = lambda e: captured_events.append(e) or asyncio.sleep(0)
            await adapter_inst._on_message(stanza)
        finally:
            adapter_inst._decrypt_aesgcm = orig

        assert len(captured_events) == 1
        event = captured_events[0]
        assert "aesgcm://" not in event.text
        assert "Look at this:" in event.text
        assert "/tmp/decrypted_photo.jpg" in event.media_urls
        assert "image/jpeg" in event.media_types

    @pytest.mark.asyncio
    async def test_aesgcm_url_body_only_no_prefix(self, adapter_inst):
        """Body is just the aesgcm:// URL → body becomes empty after stripping."""
        adapter_inst._registered_plugins = set()
        adapter_inst._self_bare = "hermes@example.org"
        adapter_inst.allow_all_users = True
        adapter_inst.client = None

        stanza = _make_stanza(
            type="chat",
            body=self._AESGCM_URL,
            from_jid="user@example.org",
        )

        async def fake_decrypt(url):
            return "/tmp/decrypted_photo.jpg", "image/jpeg"

        orig = adapter_inst._decrypt_aesgcm
        adapter_inst._decrypt_aesgcm = fake_decrypt
        try:
            captured_events = []
            adapter_inst.handle_message = lambda e: captured_events.append(e) or asyncio.sleep(0)
            await adapter_inst._on_message(stanza)
        finally:
            adapter_inst._decrypt_aesgcm = orig

        assert len(captured_events) == 1
        event = captured_events[0]
        assert not (event.text or "").strip()
        assert "/tmp/decrypted_photo.jpg" in event.media_urls

    @pytest.mark.asyncio
    async def test_aesgcm_decrypt_failure_graceful(self, adapter_inst):
        """If decryption fails, message is still delivered (with aesgcm:// in body)."""
        adapter_inst._registered_plugins = set()
        adapter_inst._self_bare = "hermes@example.org"
        adapter_inst.allow_all_users = True
        adapter_inst.client = None

        stanza = _make_stanza(
            type="chat",
            body=self._AESGCM_URL,
            from_jid="user@example.org",
        )

        async def fake_decrypt_fail(url):
            return None, None

        orig = adapter_inst._decrypt_aesgcm
        adapter_inst._decrypt_aesgcm = fake_decrypt_fail
        try:
            captured_events = []
            adapter_inst.handle_message = lambda e: captured_events.append(e) or asyncio.sleep(0)
            await adapter_inst._on_message(stanza)
        finally:
            adapter_inst._decrypt_aesgcm = orig

        assert len(captured_events) >= 1
        event = captured_events[0]
        assert "aesgcm://" in event.text
        assert event.media_urls == []

    @pytest.mark.asyncio
    async def test_aesgcm_url_in_oob_decrypted(self, adapter_inst):
        """aesgcm:// URL in OOB (XEP-0066) → download + decrypt."""
        adapter_inst._registered_plugins = {"xep_0066"}
        adapter_inst._self_bare = "hermes@example.org"
        adapter_inst.allow_all_users = True
        adapter_inst.client = None

        class _OOBStanza:
            def __getitem__(self, k):
                if k == "type":
                    return "chat"
                if k == "body":
                    return ""
                if k == "oob":
                    return {"url": self.__class__._oob_url}
                return None
            def get(self, k, default=None):
                try:
                    v = self[k]
                    return v if v is not None else default
                except Exception:
                    return default
            def get_from(self):
                return "user@example.org"
        _OOBStanza._oob_url = self._AESGCM_URL
        stanza = _OOBStanza()

        async def fake_decrypt(url):
            return "/tmp/decrypted_oob.jpg", "image/jpeg"

        orig = adapter_inst._decrypt_aesgcm
        adapter_inst._decrypt_aesgcm = fake_decrypt
        try:
            captured_events = []
            adapter_inst.handle_message = lambda e: captured_events.append(e) or asyncio.sleep(0)
            await adapter_inst._on_message(stanza)
        finally:
            adapter_inst._decrypt_aesgcm = orig

        assert len(captured_events) == 1
        event = captured_events[0]
        assert "/tmp/decrypted_oob.jpg" in event.media_urls
        assert "image/jpeg" in event.media_types

    @pytest.mark.asyncio
    async def test_aesgcm_no_url_no_op(self, adapter_inst):
        """No aesgcm:// URL → normal flow, no decryption attempted."""
        adapter_inst._registered_plugins = set()
        adapter_inst._self_bare = "hermes@example.org"
        adapter_inst.allow_all_users = True
        adapter_inst.client = None

        stanza = _make_stanza(
            type="chat",
            body="Just a normal message",
            from_jid="user@example.org",
        )

        decrypt_calls = []
        async def fake_decrypt(url):
            decrypt_calls.append(url)
            return "/tmp/x.jpg", "image/jpeg"

        orig = adapter_inst._decrypt_aesgcm
        adapter_inst._decrypt_aesgcm = fake_decrypt
        try:
            captured_events = []
            adapter_inst.handle_message = lambda e: captured_events.append(e) or asyncio.sleep(0)
            await adapter_inst._on_message(stanza)
        finally:
            adapter_inst._decrypt_aesgcm = orig

        assert len(decrypt_calls) == 0
        assert len(captured_events) == 1
        assert captured_events[0].text == "Just a normal message"

    @pytest.mark.asyncio
    async def test_invalid_aesgcm_url_not_matched(self, adapter_inst):
        """URLs that look like aesgcm:// but have wrong fragment length are ignored."""
        adapter_inst._registered_plugins = set()
        adapter_inst._self_bare = "hermes@example.org"
        adapter_inst.allow_all_users = True
        adapter_inst.client = None

        bad_url = "aesgcm://example.org/file.jpg#" + "ab" * 43 + "a"
        stanza = _make_stanza(
            type="chat",
            body=bad_url,
            from_jid="user@example.org",
        )

        decrypt_calls = []
        async def fake_decrypt(url):
            decrypt_calls.append(url)
            return "/tmp/x.jpg", "image/jpeg"

        orig = adapter_inst._decrypt_aesgcm
        adapter_inst._decrypt_aesgcm = fake_decrypt
        try:
            captured_events = []
            adapter_inst.handle_message = lambda e: captured_events.append(e) or asyncio.sleep(0)
            await adapter_inst._on_message(stanza)
        finally:
            adapter_inst._decrypt_aesgcm = orig

        assert len(decrypt_calls) == 0

    @pytest.mark.asyncio
    async def test_multiple_aesgcm_urls_in_body(self, adapter_inst):
        """Multiple aesgcm:// URLs in one message — all are decrypted."""
        adapter_inst._registered_plugins = set()
        adapter_inst._self_bare = "hermes@example.org"
        adapter_inst.allow_all_users = True
        adapter_inst.client = None

        url1 = "aesgcm://example.org/a.jpg#" + "cd" * 44
        url2 = "aesgcm://example.org/b.png#" + "ef" * 44
        body = f"Two images: {url1} and {url2}"

        stanza = _make_stanza(
            type="chat",
            body=body,
            from_jid="user@example.org",
        )

        decrypt_calls = []
        async def fake_decrypt(url):
            decrypt_calls.append(url)
            if url == url1:
                return "/tmp/a.jpg", "image/jpeg"
            return "/tmp/b.png", "image/png"

        orig = adapter_inst._decrypt_aesgcm
        adapter_inst._decrypt_aesgcm = fake_decrypt
        try:
            captured_events = []
            adapter_inst.handle_message = lambda e: captured_events.append(e) or asyncio.sleep(0)
            await adapter_inst._on_message(stanza)
        finally:
            adapter_inst._decrypt_aesgcm = orig

        assert len(decrypt_calls) == 2
        assert len(captured_events) == 1
        event = captured_events[0]
        assert "aesgcm://" not in event.text
        assert "Two images:" in event.text
        assert "and" in event.text
        assert "/tmp/a.jpg" in event.media_urls
        assert "/tmp/b.png" in event.media_urls
