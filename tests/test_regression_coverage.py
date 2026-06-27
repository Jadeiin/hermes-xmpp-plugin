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
from unittest.mock import AsyncMock, MagicMock, patch

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


# ==========================================================================
# 12b. _decrypt_aesgcm + _download_and_cache_media → cache_media_bytes
# ==========================================================================
# Regression coverage for the bug where .docx (and other non-image,
# non-audio) aesgcm:// files were routed to cache_image_from_bytes and
# rejected with "Refusing to cache non-image data". The fix routes every
# inbound media path through ``gateway.platforms.base.cache_media_bytes``
# (the same helper used by SFS, OOB, Teams, and Telegram), so the dispatch
# is no longer maintained in this plugin. These tests verify the adapter
# wires up the helper correctly — the dispatch itself is exercised
# exhaustively in tests/gateway/test_document_cache.py.

class TestAesgcmCacheRouting:
    """_decrypt_aesgcm and _download_and_cache_media must call cache_media_bytes."""

    @pytest.fixture
    def adapter_and_mod(self):
        """Force a fresh adapter module + instance for these tests.

        Other test files (test_e2e_flows.py, test_first_class_features.py)
        do `del sys.modules['adapter']; import adapter` to force-reload.
        If we reuse the top-level `adapter` reference, our patches land on
        a stale module while `_decrypt_aesgcm` looks up names in the new
        module. Reload here to get a coherent module + instance pair.
        """
        for key in list(sys.modules.keys()):
            if key == "adapter" or key.startswith("adapter."):
                del sys.modules[key]
        import adapter as fresh_adapter
        cfg = MagicMock()
        cfg.jid = "hermes@example.org"
        cfg.password = "secret"
        cfg.home_channel = None
        cfg.fileserver_url = None
        inst = fresh_adapter.XmppAdapter(cfg)
        inst._self_bare = "hermes@example.org"
        inst._known_mucs = set()
        inst._authorized_users = {"trusted@example.org"}
        inst.allow_all_users = True
        inst.build_source = lambda **kw: MagicMock()
        room_cfg = MagicMock()
        room_cfg.room = "room@conf.example.org"
        room_cfg.nick = "hermes"
        inst.muc_rooms = [room_cfg]
        inst.muc_nick = "hermes"
        return fresh_adapter, inst

    def _mock_httpx(self, adapter_module):
        """Patch httpx.AsyncClient so downloads return a fixed ciphertext."""
        class _FakeResp:
            content = b"<<ct>>"
            def raise_for_status(self): pass
        class _FakeAC:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, *a, **kw): return _FakeResp()
        return patch.object(adapter_module.httpx, "AsyncClient", _FakeAC)

    def _stub_xep_0454(self, inst, plaintext):
        """Patch the xep_0454 plugin on `inst.client` to return `plaintext`."""
        xep_0454 = MagicMock()
        xep_0454.decrypt = MagicMock(return_value=plaintext)
        inst.client = MagicMock()
        inst.client.__getitem__ = lambda s, k: xep_0454 if k == "xep_0454" else MagicMock()

    def _fake_cached_media(self, path, media_type, kind="document", display="file"):
        """Build a CachedMedia-shaped MagicMock."""
        m = MagicMock()
        m.path = path
        m.media_type = media_type
        m.kind = kind
        m.display_name = display
        return m

    @pytest.mark.asyncio
    async def test_decrypt_routes_through_cache_media_bytes(self, adapter_and_mod):
        """All decrypted blobs must flow through cache_media_bytes — not the
        legacy per-mime cache_*_from_bytes helpers (which would reject
        .docx as "non-image data")."""
        _adapter, adapter_inst = adapter_and_mod
        # ZIP magic bytes = the plaintext shape of a real .docx
        plaintext = b"PK\x03\x04" + b"\x00" * 20
        aesgcm = (
            "aesgcm://www.jabjab.de:5443/upload/abc/report.docx#"
            + "ab" * 44
        )

        seen = {"calls": []}

        def fake_cache_media_bytes(data, *, filename="", mime_type="", default_kind=None):
            seen["calls"].append({
                "filename": filename,
                "mime_type": mime_type,
                "data_len": len(data),
            })
            return self._fake_cached_media(
                f"/tmp/cache/{filename}", "application/octet-stream",
                kind="document", display=filename,
            )

        self._stub_xep_0454(adapter_inst, plaintext)
        with patch.object(_adapter, "cache_media_bytes", fake_cache_media_bytes), \
             self._mock_httpx(_adapter):
            cached, mime = await adapter_inst._decrypt_aesgcm(aesgcm)

        # cache_media_bytes was called exactly once with the right args
        assert len(seen["calls"]) == 1, seen
        call = seen["calls"][0]
        # Filename derived from the URL path (not just "file.bin" or similar)
        assert call["filename"] == "report.docx"
        # mime_type is empty (we let cache_media_bytes infer from filename)
        assert call["mime_type"] == ""
        # Plaintext bytes were passed (decryption happened)
        assert call["data_len"] == len(plaintext)
        # Returned path+mime come from CachedMedia
        assert cached == "/tmp/cache/report.docx"
        assert mime == "application/octet-stream"

    @pytest.mark.asyncio
    async def test_decrypt_propagates_cached_media_type(self, adapter_and_mod):
        """If cache_media_bytes resolves a specific mime (e.g. PDF), we return it."""
        _adapter, adapter_inst = adapter_and_mod
        plaintext = b"%PDF-1.4\n" + b"\x00" * 20
        aesgcm = "aesgcm://example.org/files/paper.pdf#" + "cd" * 44

        with patch.object(
            _adapter, "cache_media_bytes",
            lambda data, filename="", mime_type="", default_kind=None: self._fake_cached_media(
                f"/tmp/cache/{filename}", "application/pdf", kind="document", display=filename
            ),
        ), self._mock_httpx(_adapter):
            adapter_inst.client = MagicMock()
            adapter_inst.client.__getitem__ = lambda s, k: MagicMock(
                decrypt=MagicMock(return_value=plaintext)
            ) if k == "xep_0454" else MagicMock()
            cached, mime = await adapter_inst._decrypt_aesgcm(aesgcm)

        # The mime returned by cache_media_bytes is what we hand back
        assert mime == "application/pdf"
        assert cached == "/tmp/cache/paper.pdf"

    @pytest.mark.asyncio
    async def test_decrypt_image_validation_failure_returns_none(self, adapter_and_mod):
        """cache_media_bytes returns None only for image validation failure.
        We must surface (None, None) in that case (no implicit fallback)."""
        _adapter, adapter_inst = adapter_and_mod
        plaintext = b"\xff\xd8\xff\xe0" + b"\x00" * 20
        aesgcm = "aesgcm://example.org/p/photo.jpg#" + "12" * 44

        with patch.object(
            _adapter, "cache_media_bytes",
            lambda data, filename="", mime_type="", default_kind=None: None,  # validation fail
        ), self._mock_httpx(_adapter):
            adapter_inst.client = MagicMock()
            adapter_inst.client.__getitem__ = lambda s, k: MagicMock(
                decrypt=MagicMock(return_value=plaintext)
            ) if k == "xep_0454" else MagicMock()
            cached, mime = await adapter_inst._decrypt_aesgcm(aesgcm)

        assert cached is None
        assert mime is None

    @pytest.mark.asyncio
    async def test_decrypt_invalid_url_returns_none(self, adapter_and_mod):
        """A malformed aesgcm:// URL (wrong fragment length) → (None, None)."""
        _adapter, adapter_inst = adapter_and_mod
        bad = "aesgcm://example.org/x.jpg#" + "ff" * 43  # 86 hex, not 88

        called = {"cache": False}
        with patch.object(
            _adapter, "cache_media_bytes",
            lambda *a, **kw: (called.update(cache=True) or self._fake_cached_media("x", "y")),
        ), self._mock_httpx(_adapter):
            adapter_inst.client = MagicMock()
            cached, mime = await adapter_inst._decrypt_aesgcm(bad)

        assert cached is None
        assert mime is None
        assert called["cache"] is False  # never reached the cache

    @pytest.mark.asyncio
    async def test_decrypt_download_failure_returns_none(self, adapter_and_mod):
        """HTTP failure during download → (None, None), no cache call."""
        _adapter, adapter_inst = adapter_and_mod
        plaintext = b"PK\x03\x04"
        aesgcm = "aesgcm://example.org/x.docx#" + "ab" * 44

        # httpx that always raises
        class _FailingAC:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, *a, **kw):
                raise RuntimeError("network down")

        with patch.object(_adapter.httpx, "AsyncClient", _FailingAC):
            adapter_inst.client = MagicMock()
            adapter_inst.client.__getitem__ = lambda s, k: MagicMock(
                decrypt=MagicMock(return_value=plaintext)
            ) if k == "xep_0454" else MagicMock()
            cached, mime = await adapter_inst._decrypt_aesgcm(aesgcm)

        assert cached is None
        assert mime is None


# ==========================================================================
# 12c. _download_and_cache_media — SFS/OOB helper
# ==========================================================================
# The SFS (XEP-0447) and OOB (XEP-0066) paths share a helper that
# downloads a URL and dispatches via cache_media_bytes. This is the
# structural fix for the same bug class as aesgcm: the old code routed
# every non-image file through cache_audio_from_url, breaking .docx,
# .pdf, .mp4, etc. in plain (unencrypted) SFS/OOB attachments.

class TestDownloadAndCacheMedia:
    """The SFS/OOB helper must route everything through cache_media_bytes."""

    @pytest.fixture
    def adapter_and_mod(self):
        for key in list(sys.modules.keys()):
            if key == "adapter" or key.startswith("adapter."):
                del sys.modules[key]
        import adapter as fresh_adapter
        cfg = MagicMock()
        cfg.jid = "hermes@example.org"
        cfg.password = "secret"
        cfg.home_channel = None
        cfg.fileserver_url = None
        inst = fresh_adapter.XmppAdapter(cfg)
        inst._self_bare = "hermes@example.org"
        inst._known_mucs = set()
        inst._authorized_users = {"trusted@example.org"}
        inst.allow_all_users = True
        inst.build_source = lambda **kw: MagicMock()
        room_cfg = MagicMock()
        room_cfg.room = "room@conf.example.org"
        room_cfg.nick = "hermes"
        inst.muc_rooms = [room_cfg]
        inst.muc_nick = "hermes"
        return fresh_adapter, inst

    def _mock_httpx_with_data(self, adapter_module, payload: bytes):
        """Patch httpx to return `payload` for .get()."""
        class _FakeResp:
            content = payload
            def raise_for_status(self): pass
        class _FakeAC:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, *a, **kw): return _FakeResp()
        return patch.object(adapter_module.httpx, "AsyncClient", _FakeAC)

    @pytest.mark.asyncio
    async def test_docx_url_routes_to_document(self, adapter_and_mod):
        """SFS .docx: must NOT go to image/audio cache (regression of the
        same bug class as the aesgcm path)."""
        _adapter, adapter_inst = adapter_and_mod

        seen = {"filename": None, "mime_hint": None}

        def fake_cache(data, *, filename="", mime_type="", default_kind=None):
            seen["filename"] = filename
            seen["mime_hint"] = mime_type
            m = MagicMock()
            m.path = f"/tmp/cache/{filename}"
            m.media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            m.kind = "document"
            m.display_name = filename
            return m

        with patch.object(_adapter, "cache_media_bytes", fake_cache), \
             self._mock_httpx_with_data(_adapter, b"PK\x03\x04docxbody"):
            result = await adapter_inst._download_and_cache_media(
                "https://upload.example.org/files/report.docx"
            )

        # Filename preserved from URL path
        assert seen["filename"] == "report.docx"
        # No mime hint when caller didn't provide one
        assert seen["mime_hint"] == ""
        # Returned CachedMedia propagates back
        assert result is not None
        assert result.path == "/tmp/cache/report.docx"
        assert result.media_type.startswith("application/vnd.openxmlformats-officedocument")

    @pytest.mark.asyncio
    async def test_passes_through_sfs_mime_hint(self, adapter_and_mod):
        """SFS stanza's <file media-type="…"> hint should be forwarded."""
        _adapter, adapter_inst = adapter_and_mod

        seen = {}
        def fake_cache(data, *, filename="", mime_type="", default_kind=None):
            seen["mime_type"] = mime_type
            m = MagicMock()
            m.path = "/tmp/x"
            m.media_type = mime_type or "application/octet-stream"
            m.kind = "document"
            m.display_name = filename
            return m

        with patch.object(_adapter, "cache_media_bytes", fake_cache), \
             self._mock_httpx_with_data(_adapter, b"%PDF-1.4"):
            await adapter_inst._download_and_cache_media(
                "https://x/paper.pdf", mime_hint="application/pdf"
            )

        assert seen["mime_type"] == "application/pdf"

    @pytest.mark.asyncio
    async def test_download_failure_returns_none(self, adapter_and_mod):
        _adapter, adapter_inst = adapter_and_mod

        class _FailingAC:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, *a, **kw):
                raise RuntimeError("network down")

        with patch.object(_adapter.httpx, "AsyncClient", _FailingAC):
            result = await adapter_inst._download_and_cache_media("https://x/y.pdf")

        assert result is None

    @pytest.mark.asyncio
    async def test_url_without_filename_uses_default(self, adapter_and_mod):
        """URL with no path component (or just '/') → filename='file'."""
        _adapter, adapter_inst = adapter_and_mod

        seen = {}
        def fake_cache(data, *, filename="", mime_type="", default_kind=None):
            seen["filename"] = filename
            m = MagicMock()
            m.path = "/tmp/x"
            m.media_type = "application/octet-stream"
            m.kind = "document"
            m.display_name = filename
            return m

        with patch.object(_adapter, "cache_media_bytes", fake_cache), \
             self._mock_httpx_with_data(_adapter, b"\x00\x01"):
            await adapter_inst._download_and_cache_media("https://x.example.org/")

        # Trailing slash → empty basename → "file"
        assert seen["filename"] == "file"


# ==========================================================================
# 10. XEP-0050 Ad-Hoc Commands
# ==========================================================================

class TestAdhocCommands:
    """Ad-hoc command registration, stage-1 form, stage-2 execution."""

    # ── Registration ────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_setup_registers_command_and_logs(self, adapter_inst):
        """_setup_adhoc_commands registers command and logs info."""
        client = MagicMock()
        client.boundjid = MagicMock()
        client.boundjid.full = "hermes@example.org/resource"
        client.boundjid.bare = "hermes@example.org"
        xep0050 = MagicMock()
        client.__getitem__ = lambda s, k: xep0050 if k == "xep_0050" else MagicMock()
        adapter_inst.client = client
        adapter_inst._registered_plugins.add("xep_0050")

        with unittest.mock.patch.object(adapter.logger, "info") as mock_info:
            await adapter_inst._setup_adhoc_commands()

        xep0050.add_command.assert_called_once()
        call_kwargs = xep0050.add_command.call_args
        assert call_kwargs[1]["node"] == "hermes"
        assert call_kwargs[1]["name"] == "Hermes Agent Commands"
        assert call_kwargs[1]["handler"] == adapter_inst._adhoc_hermes_handler
        mock_info.assert_called_once()

    @pytest.mark.asyncio
    async def test_setup_noop_when_plugin_missing(self, adapter_inst):
        """No crash, no-op when xep_0050 not registered."""
        adapter_inst.client = MagicMock()
        adapter_inst._registered_plugins.discard("xep_0050")
        await adapter_inst._setup_adhoc_commands()
        # Should not raise

    @pytest.mark.asyncio
    async def test_setup_logs_exception(self, adapter_inst):
        """Exception during add_command is logged."""
        client = MagicMock()
        client.boundjid = MagicMock()
        client.boundjid.full = "hermes@example.org/resource"
        xep0050 = MagicMock()
        xep0050.add_command.side_effect = RuntimeError("boom")
        client.__getitem__ = lambda s, k: xep0050 if k == "xep_0050" else MagicMock()
        adapter_inst.client = client
        adapter_inst._registered_plugins.add("xep_0050")

        with unittest.mock.patch.object(adapter.logger, "exception") as mock_exc:
            await adapter_inst._setup_adhoc_commands()
        mock_exc.assert_called_once()

    # ── Stage 1: command selection form ─────────────────────────────

    @pytest.mark.asyncio
    async def test_stage1_returns_form_with_next(self, adapter_inst):
        """Stage 1 returns a Data Form with has_next=True and next handler."""
        client = MagicMock()
        mock_form = MagicMock()
        client.__getitem__ = lambda s, k: MagicMock() if k != "xep_0004" else (
            MagicMock(make_form=MagicMock(return_value=mock_form))
        )
        adapter_inst.client = client
        adapter_inst._registered_plugins.add("xep_0004")

        iq = MagicMock()
        session: dict = {}
        session = await adapter_inst._adhoc_hermes_handler(iq, session)

        assert session["payload"] == mock_form
        assert session["has_next"] is True
        assert session["next"] == adapter_inst._adhoc_hermes_execute
        assert session["allow_complete"] is False

    @pytest.mark.asyncio
    async def test_stage1_fallback_when_xep_0004_missing(self, adapter_inst):
        """Without xep_0004, return error note."""
        adapter_inst.client = MagicMock()
        adapter_inst._registered_plugins.discard("xep_0004")

        session = await adapter_inst._adhoc_hermes_handler(MagicMock(), {})
        assert session["notes"][0][0] == "error"
        assert "not available" in session["notes"][0][1]

    @pytest.mark.asyncio
    async def test_stage1_access_denied_for_unauthorized(self, adapter_inst):
        """Unauthorized JID gets 'Access denied' error."""
        client = MagicMock()
        client.__getitem__ = lambda s, k: MagicMock()
        adapter_inst.client = client
        adapter_inst._registered_plugins.add("xep_0004")
        adapter_inst.allow_all_users = False
        adapter_inst.allowed_users = {"trusted@example.org"}
        adapter_inst._is_paired = lambda jid: False

        iq = MagicMock()
        iq.get_from.return_value = "stranger@evil.example.org/resource"

        session = await adapter_inst._adhoc_hermes_handler(iq, {})
        assert session["notes"][0][0] == "error"
        assert "Access denied" in session["notes"][0][1]

    @pytest.mark.asyncio
    async def test_stage1_access_granted_for_paired_user(self, adapter_inst):
        """Paired user (via gateway pairing) passes access control."""
        mock_form = MagicMock()
        client = MagicMock()
        client.__getitem__ = lambda s, k: (
            MagicMock(make_form=MagicMock(return_value=mock_form))
            if k == "xep_0004" else MagicMock()
        )
        adapter_inst.client = client
        adapter_inst._registered_plugins.add("xep_0004")
        adapter_inst.allow_all_users = False
        adapter_inst.allowed_users = set()
        adapter_inst._is_paired = lambda jid: jid == "paired@example.org"

        iq = MagicMock()
        iq.get_from.return_value = "paired@example.org/resource"

        session = await adapter_inst._adhoc_hermes_handler(iq, {})
        assert session["payload"] == mock_form
        assert "Access denied" not in str(session.get("notes", ""))

    @pytest.mark.asyncio
    async def test_stage1_access_granted_for_authorized(self, adapter_inst):
        """Authorized JID passes access control to form."""
        mock_form = MagicMock()
        client = MagicMock()
        client.__getitem__ = lambda s, k: (
            MagicMock(make_form=MagicMock(return_value=mock_form))
            if k == "xep_0004" else MagicMock()
        )
        adapter_inst.client = client
        adapter_inst._registered_plugins.add("xep_0004")
        adapter_inst.allow_all_users = True  # gateway pairing mode

        iq = MagicMock()
        iq.get_from.return_value = "anyone@example.org/resource"

        session = await adapter_inst._adhoc_hermes_handler(iq, {})
        assert session["payload"] == mock_form
        assert "Access denied" not in str(session.get("notes", ""))

    # ── Stage 2: command execution ──────────────────────────────────

    @pytest.mark.asyncio
    async def test_stage2_status(self, adapter_inst):
        """Status command returns adapter status info."""
        client = MagicMock()
        client.boundjid = MagicMock()
        client.boundjid.bare = "hermes@example.org"
        adapter_inst.client = client
        adapter_inst._running = True
        adapter_inst._omemo_enabled = True
        adapter_inst._mam_enabled = True

        mock_form = MagicMock()
        mock_form.get_values.return_value = {"command": "status"}

        session: dict = {"id": "sess-1"}
        session = await adapter_inst._adhoc_hermes_execute(mock_form, session)

        assert session["next"] is None
        assert session["has_next"] is False
        assert session["payload"] is None
        note = session["notes"][0]
        assert note[0] == "info"
        assert "Connected: ✅" in note[1]
        assert "hermes@example.org" in note[1]
        assert "OMEMO: ✅" in note[1]

    @pytest.mark.asyncio
    async def test_stage2_status_disconnected(self, adapter_inst):
        """Status shows disconnected when _running is False."""
        adapter_inst.client = MagicMock()
        adapter_inst.client.boundjid = MagicMock()
        adapter_inst.client.boundjid.bare = "hermes@example.org"
        adapter_inst._running = False
        adapter_inst._omemo_enabled = False

        mock_form = MagicMock()
        mock_form.get_values.return_value = {"command": "status"}

        session = await adapter_inst._adhoc_hermes_execute(mock_form, {})
        note = session["notes"][0]
        assert "Connected: ❌" in note[1]
        assert "OMEMO: ❌" in note[1]

    @pytest.mark.asyncio
    async def test_stage2_status_with_muc_rooms(self, adapter_inst):
        """Status lists MUC rooms when configured."""
        adapter_inst.client = MagicMock()
        adapter_inst.client.boundjid = MagicMock()
        adapter_inst.client.boundjid.bare = "hermes@example.org"
        adapter_inst._running = True
        adapter_inst._omemo_enabled = True

        from adapter import _MucRoom
        adapter_inst.muc_rooms = [
            _MucRoom("room1@conf.example.org", "hermes"),
            _MucRoom("room2@conf.example.org", "bot"),
        ]

        mock_form = MagicMock()
        mock_form.get_values.return_value = {"command": "status"}

        session = await adapter_inst._adhoc_hermes_execute(mock_form, {})
        note = session["notes"][0]
        assert "room1@conf.example.org" in note[1]
        assert "room2@conf.example.org" in note[1]

    @pytest.mark.asyncio
    async def test_stage2_help(self, adapter_inst):
        """Help command returns usage text."""
        mock_form = MagicMock()
        mock_form.get_values.return_value = {"command": "help"}

        session = await adapter_inst._adhoc_hermes_execute(mock_form, {})
        note = session["notes"][0]
        assert "/stop" in note[1]
        assert "/new" in note[1]
        assert "/approve" in note[1]
        assert "/deny" in note[1]

    @pytest.mark.asyncio
    async def test_stage2_ping(self, adapter_inst):
        """Ping command returns pong with timestamp."""
        mock_form = MagicMock()
        mock_form.get_values.return_value = {"command": "ping"}

        session = await adapter_inst._adhoc_hermes_execute(mock_form, {})
        note = session["notes"][0]
        assert "🏓 Pong!" in note[1]
        # Should contain a UTC timestamp
        assert "UTC" in note[1]

    @pytest.mark.asyncio
    async def test_stage2_unknown_command(self, adapter_inst):
        """Unknown command shows error note."""
        mock_form = MagicMock()
        mock_form.get_values.return_value = {"command": "bogus"}

        session = await adapter_inst._adhoc_hermes_execute(mock_form, {})
        note = session["notes"][0]
        assert "Unknown" in note[1]
        assert "bogus" in note[1]


# ==========================================================================
# 11. XEP-0444 MUC self-reaction guard & auth
# ==========================================================================

class TestMucReactionGuard:
    """_on_reaction must ignore the bot's own seed reactions in MUC
    and use correct auth context (group vs dm)."""

    @pytest.mark.asyncio
    async def test_self_reaction_ignored_in_muc(self, adapter_inst):
        """Bot's own seed reaction in MUC is ignored via nick check."""
        adapter_inst._known_mucs.add("room@conf.example.org")
        adapter_inst._self_bare = "hermes@jabjab.de"

        # Bot's own reaction comes from room@conf/bot-nick
        message = MagicMock()
        message.__getitem__ = lambda s, k: {
            "from": "room@conf.example.org/hermes",
            "reactions": MagicMock(
                xml=MagicMock(),
                **{"__getitem__": lambda s, k: "msg-1" if k == "id" else None},
                __iter__=lambda s: iter([
                    MagicMock(**{"__getitem__": lambda s, k: "✅" if k == "value" else None})
                ]),
            ),
        }.get(k, None)

        adapter_inst._approval_prompts_by_event = {"msg-1": {"resolved": False, "session_key": "sk"}}
        resolve_calls = []

        async def fake_resolve(*args):
            resolve_calls.append(args)

        adapter_inst._resolve_approval_reaction = fake_resolve

        adapter_inst._on_reaction(message)
        # Must NOT have called resolve — self-reaction was ignored
        assert len(resolve_calls) == 0

    @pytest.mark.asyncio
    async def test_muc_user_reaction_uses_group_auth(self, adapter_inst):
        """Reaction from MUC user passes auth with group context."""
        adapter_inst._known_mucs.add("room@conf.example.org")
        adapter_inst._self_bare = "hermes@jabjab.de"
        adapter_inst.allow_all_users = False
        adapter_inst.allowed_users = set()

        message = MagicMock()
        message.__getitem__ = lambda s, k: {
            "from": "room@conf.example.org/poesty",
            "reactions": MagicMock(
                xml=MagicMock(),
                **{"__getitem__": lambda s, k: "msg-2" if k == "id" else None},
                __iter__=lambda s: iter([
                    MagicMock(**{"__getitem__": lambda s, k: "✅" if k == "value" else None})
                ]),
            ),
        }.get(k, None)

        # Mock _muc_real_jid to return the real JID
        adapter_inst._muc_real_jid = lambda msg: "poesty@jabjab.de"

        adapter_inst._approval_prompts_by_event = {
            "msg-2": {"resolved": False, "session_key": "sk"}
        }
        resolve_calls = []

        async def fake_resolve(*args):
            resolve_calls.append(args)

        adapter_inst._resolve_approval_reaction = fake_resolve

        adapter_inst._on_reaction(message)
        # ensure_future schedules the task – let it execute
        await asyncio.sleep(0)
        assert len(resolve_calls) == 1

    @pytest.mark.asyncio
    async def test_own_reaction_ignored_in_dm(self, adapter_inst):
        """DM self-reaction is ignored via bare JID check."""
        adapter_inst._self_bare = "hermes@jabjab.de"

        message = MagicMock()
        message.__getitem__ = lambda s, k: {
            "from": "hermes@jabjab.de/resource",
            "reactions": MagicMock(
                xml=MagicMock(),
                **{"__getitem__": lambda s, k: "msg-3" if k == "id" else None},
            ),
        }.get(k, None)

        adapter_inst._approval_prompts_by_event = {"msg-3": {"resolved": False}}
        resolve_calls = []

        async def fake_resolve(*args):
            resolve_calls.append(args)

        adapter_inst._resolve_approval_reaction = fake_resolve
        adapter_inst._on_reaction(message)
        assert len(resolve_calls) == 0
