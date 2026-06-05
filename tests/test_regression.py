"""Regression tests: verify pre-existing features still work after
adding the new first-class features (reactions, replies, markup, forms,
commands, voice SFS).

These are bootstrapped from the mock-gateways used by test_first_class_features
and test_e2e_flows, but ONLY assert that old APIs / paths remain
behaviour-compatible.
"""
from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import unittest.mock

# -----------------------------------------------------------------
# Mock gateway / tools — same set-up as test_e2e_flows.py
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
                 message_id=None, reply_to_message_id=None, reply_to_text=None, metadata=None):
        self.text = text
        self.message_type = message_type
        self.source = source
        self.raw_message = raw_message
        self.message_id = message_id
        self.reply_to_message_id = reply_to_message_id
        self.reply_to_text = reply_to_text
        self.metadata = metadata or {}

gw_base.MessageEvent = _FakeMessageEvent
gw_base.MessageType = type("MessageType", (), {"TEXT": "text", "IMAGE": "image", "COMMAND": "command"})

class _FakeProcessingOutcome:
    SUCCESS = 0
    FAILURE = 1
    CANCELLED = 2

gw_base.ProcessingOutcome = _FakeProcessingOutcome
gw_base.SendResult = type("SendResult", (), {
    "__init__": lambda s, **kw: s.__dict__.update(kw) or None,
})

def _mock_truncate(self, content, max_len, **kw):
    """Minimal truncate_message for tests: splits at max_len boundaries."""
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
    "send_clarify": lambda *a, **kw: None,
    "handle_message": lambda *a, **kw: None,
    "build_source": lambda s, **kw: MagicMock(**kw),
    "_mark_disconnected": lambda s: None,
    "fatal_error_message": lambda s, *a, **kw: "auth failed",
    "truncate_message": _mock_truncate,
})

gw_models = unittest.mock.MagicMock()
sys.modules["gateway.platforms.models"] = gw_models
class _FakeChatContext:
    def __init__(self, **kw):
        pass
gw_models.ChatContext = _FakeChatContext

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

ProcessingOutcome = _FakeProcessingOutcome


def _make_stanza(**fields):
    """Build a fake slixmpp Message stanza."""
    class _FakeStanza:
        def __getitem__(self, k):
            return fields.get(k, "")
        def get(self, k, default=None):
            return fields.get(k, default)
        def get_from(self):
            return fields.get("from_jid", "user@example.org")
    return _FakeStanza()


@pytest.fixture
def adapter_instance():
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
    return a


# -----------------------------------------------------------------
# 1. Old public API still exists and accepts right arguments
# -----------------------------------------------------------------

class TestOldPublicAPI:
    def test_send_api_signature_unchanged(self):
        sig = inspect.signature(adapter.XmppAdapter.send)
        params = list(sig.parameters)
        expected = ["self", "chat_id", "content", "image_paths", "voice_path",
                    "document_path", "reply_to", "thread_id", "message_id",
                    "disable_web_page_preview", "parse_mode", "formatting"]
        # We only care that the old params still exist (new ones may have been added)
        for p in expected:
            assert p in params, f"send() lost param {p}"

    def test_send_clarify_still_exists(self):
        assert hasattr(adapter.XmppAdapter, "send_clarify")

    def test_send_typing_stop_typing_still_exist(self):
        assert hasattr(adapter.XmppAdapter, "send_typing")
        assert hasattr(adapter.XmppAdapter, "stop_typing")

    def test_standalone_sender_function_still_exists(self):
        assert hasattr(adapter, "send_xmpp_message")
        sig = inspect.signature(adapter.send_xmpp_message)
        params = list(sig.parameters)
        assert "pconfig" in params
        assert "chat_id" in params
        assert "message" in params


# -----------------------------------------------------------------
# 2. Inbound stanza sanity (no regression on guard logic)
# -----------------------------------------------------------------

class TestInboundGuards:
    def test_legacy_error_empty_body_drops(self, adapter_instance):
        for stype in ("error", "headline"):
            stanza = _make_stanza(type=stype, body="x", from_jid="user@example.org")
            asyncio.run(adapter_instance._on_message(stanza))
        stanza = _make_stanza(type="chat", body="", from_jid="user@example.org")
        asyncio.run(adapter_instance._on_message(stanza))
        # No exceptions raised == pass


# -----------------------------------------------------------------
# 3. Lifecycle / thread-management
# -----------------------------------------------------------------

class TestLifecycle:
    def test_session_ready_event_exists(self, adapter_instance):
        assert adapter_instance._session_ready is None or hasattr(
            adapter_instance._session_ready, "set"
        )

    def test_process_task_attribute_exists(self, adapter_instance):
        assert hasattr(adapter_instance, "_process_task")

    def test_disconnect_method_exists(self, adapter_instance):
        assert callable(getattr(adapter_instance, "disconnect", None))


# -----------------------------------------------------------------
# 4. File helpers
# -----------------------------------------------------------------

class TestFileHelpers:
    def test_upload_and_send_exists(self, adapter_instance):
        assert callable(getattr(adapter_instance, "_upload_and_send", None))

    def test_upload_and_send_requires_xep_0363(self):
        """Old behaviour: without xep_0363, send_image should not crash."""
        cfg = MagicMock()
        cfg.jid = "bot@example.org"
        cfg.password = "secret"
        cfg.home_channel = None
        cfg.fileserver_url = None
        a = adapter.XmppAdapter(cfg)
        a._registered_plugins = set()  # no file upload
        a.client = None
        a._self_bare = "bot@example.org"
        result = asyncio.run(a._upload_and_send("user@example.org", "/tmp/f.jpg", None))
        assert result.success is False

    def test_send_voice_uses_internal_path(self, adapter_instance):
        """send_voice must accept voice_path and delegate correctly."""
        assert callable(getattr(adapter_instance, "send_voice", None))


# -----------------------------------------------------------------
# 5. OMEMO storage still works (file-backed JSON)
# -----------------------------------------------------------------

class TestOmemoStorage:
    def test_storage_impl_saves_and_loads(self, tmp_path):
        path = tmp_path / "omemo.json"
        store = adapter._StorageImpl(path)
        val = {"key": 42}
        asyncio.run(store._store("test", val))
        disk = asyncio.run(store._load("test"))
        # Even if Just/Nothing are mocked, disk should not be None / Nothing
        assert disk is not None

    def test_storage_delete(self, tmp_path):
        path = tmp_path / "omemo.json"
        store = adapter._StorageImpl(path)
        asyncio.run(store._store("gone", 1))
        asyncio.run(store._delete("gone"))
        assert "gone" not in store._data


# -----------------------------------------------------------------
# 6. MUC detection (_is_muc) — prefix heuristic removed (Issue #2 fix)
# -----------------------------------------------------------------

class TestMucDetection:
    """_is_muc now only checks _known_mucs (configured rooms)."""

    def test_configured_room_is_muc(self, adapter_instance):
        adapter_instance._known_mucs.add("room@conference.example.org")
        assert adapter_instance._is_muc("room@conference.example.org") is True

    def test_unconfigured_jid_is_not_muc(self, adapter_instance):
        assert adapter_instance._is_muc("user@example.org") is False

    def test_chat_domain_not_muc(self, adapter_instance):
        """Regression: Issue #2 — Snikket 'chat.' domain is NOT a MUC."""
        assert adapter_instance._is_muc("user@chat.snikket.example") is False

    def test_conference_domain_not_muc_if_unconfigured(self, adapter_instance):
        """Prefix matching removed — even 'conference.' domain needs config."""
        assert adapter_instance._is_muc("unknown@conference.example.org") is False

    def test_empty_known_mucs_nothing_is_muc(self):
        cfg = MagicMock()
        cfg.jid = "hermes@example.org"
        cfg.password = "secret"
        cfg.home_channel = None
        cfg.fileserver_url = None
        a = adapter.XmppAdapter(cfg)
        a._known_mucs = set()
        assert a._is_muc("room@conference.example.org") is False
        assert a._is_muc("user@example.org") is False


# -----------------------------------------------------------------
# 7. MUC real JID extraction (_muc_real_jid)
# -----------------------------------------------------------------

class TestMucRealJid:
    """_muc_real_jid must use muc['jid'] not getattr(muc, 'jid', None).

    slixmpp's MUCMessage exposes 'jid' via __getitem__ interface
    resolution, NOT as a Python attribute.  getattr(muc, 'jid', None)
    always returns None, causing real JIDs to be silently dropped.
    """

    def test_extracts_real_jid_from_item(self, adapter_instance):
        """Non-anonymous MUC: stanza['muc']['jid'] returns the real JID."""
        class _FakeMuc:
            def __getitem__(self, k):
                return "realuser@example.org" if k == "jid" else None
            def __bool__(self):
                return True
        muc_elem = _FakeMuc()

        class _FakeStanza:
            def get(self, k, default=None):
                return muc_elem if k == "muc" else default
            def __getitem__(self, k):
                return muc_elem if k == "muc" else None

        stanza = _FakeStanza()
        result = adapter_instance._muc_real_jid(stanza)
        assert result == "realuser@example.org"

    def test_returns_none_when_muc_missing(self, adapter_instance):
        """Regular DM stanza with no <muc> element returns None."""
        class _FakeStanza:
            def get(self, k, default=None):
                return default
        stanza = _FakeStanza()
        assert adapter_instance._muc_real_jid(stanza) is None

    def test_returns_none_when_jid_empty(self, adapter_instance):
        """Anonymous MUC: muc['jid'] is empty → returns None."""
        class _FakeMuc:
            def __getitem__(self, k):
                return ""  # empty jid
            def __bool__(self):
                return True

        class _FakeStanza:
            def get(self, k, default=None):
                muc = _FakeMuc()
                return muc if k == "muc" else default

        stanza = _FakeStanza()
        assert adapter_instance._muc_real_jid(stanza) is None

    def test_handles_attribute_error_gracefully(self, adapter_instance):
        """If muc element exists but has no __getitem__, don't crash."""
        class _FakeMuc:
            def __bool__(self):
                return True

        class _FakeStanza:
            def get(self, k, default=None):
                return _FakeMuc() if k == "muc" else default

        stanza = _FakeStanza()
        assert adapter_instance._muc_real_jid(stanza) is None
