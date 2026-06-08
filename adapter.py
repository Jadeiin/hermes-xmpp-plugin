"""XMPP (Jabber) platform adapter.

Built on slixmpp. Connects to any XMPP server, supports 1:1 chats and MUC
groupchat, and uses XEP-0363 (HTTP File Upload) for attachments.

Encryption posture (ADR-0002): TLS-to-server is always on. When
`omemo_enabled` is true (the default) and slixmpp-omemo is installed,
outbound 1:1 and MUC private messages are encrypted with OMEMO where the
recipient has published device keys. Inbound OMEMO messages are decrypted
automatically.

Packaged as a third-party Hermes platform plugin.
"""
from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

import httpx
from slixmpp.clientxmpp import ClientXMPP
from slixmpp.jid import JID  # type: ignore[import-untyped]
from slixmpp.plugins import register_plugin  # type: ignore[import-untyped]
from slixmpp.stanza import Message  # type: ignore[import-untyped]
from slixmpp.xmlstream import register_stanza_plugin  # type: ignore[import-untyped]

# ----------------------------------------------------------------
# slixmpp-omemo imports — guarded but present at runtime on this host
# ----------------------------------------------------------------

if TYPE_CHECKING:
    from omemo.storage import Just, Maybe, Nothing, Storage  # type: ignore[import-untyped]
    from omemo.types import DeviceInformation, JSONType  # type: ignore[import-untyped]
    from slixmpp_omemo import TrustLevel, XEP_0384  # type: ignore[import-untyped]

try:
    from slixmpp_omemo import TrustLevel, XEP_0384
    from omemo.storage import Just, Maybe, Nothing, Storage
    from omemo.types import DeviceInformation, JSONType

    SLIXMPP_OMEMO_AVAILABLE = True
except ImportError:
    SLIXMPP_OMEMO_AVAILABLE = False
    XEP_0384 = None  # type: ignore[misc]
    Storage = None   # type: ignore[misc]
    JSONType = None  # type: ignore[misc]

from gateway.config import Platform, PlatformConfig  # pyright: ignore[reportMissingImports]
from gateway.platforms.base import (  # pyright: ignore[reportMissingImports]
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    ProcessingOutcome,
    SendResult,
    cache_audio_from_bytes,
    cache_audio_from_url,
    cache_image_from_bytes,
    cache_image_from_url,
)

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------
# MIME → extension mapping for inbound media downloads
# ----------------------------------------------------------------
_MIME_TO_EXT: Dict[str, str] = {
    # Audio
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/wav": ".wav",
    "audio/webm": ".weba",
    "audio/x-m4a": ".m4a",
    # Image
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
    # Video
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/ogg": ".ogv",
    "video/quicktime": ".mov",
    "video/x-matroska": ".mkv",
    # Documents
    "application/pdf": ".pdf",
    "application/zip": ".zip",
    "application/gzip": ".gz",
    "application/x-tar": ".tar",
    "text/plain": ".txt",
    "text/html": ".html",
}


def _mime_to_ext(mime: str) -> str:
    """Map a MIME type to a file extension (including the dot)."""
    return _MIME_TO_EXT.get(mime, ".bin")


def _mime_to_message_type(mime: str, *, body: Optional[str] = None) -> MessageType:
    """Map a MIME type to the appropriate MessageType.

    When body is empty and the media is audio, treat it as a voice note
    regardless of the exact codec — the "media-only audio" pattern is the
    de-facto voice-message signal across Conversations, Gajim, and most
    modern XMPP clients (which use .ogg, .m4a, .aac, .opus, etc.).
    """
    mime_lower = mime.lower()
    if mime_lower.startswith("image/"):
        return MessageType.PHOTO
    if mime_lower.startswith("video/"):
        return MessageType.VIDEO
    if mime_lower.startswith("audio/"):
        # Known voice-message codecs (Conversations & friends)
        if mime_lower in (
            "audio/ogg",       # Opus in Ogg (Conversations "OGG" setting)
            "audio/opus",      # raw Opus
            "audio/mp4",       # AAC in MP4 (.m4a — Conversations "AAC" setting)
            "audio/aac",       # raw AAC
            "audio/x-m4a",     # legacy .m4a
        ):
            return MessageType.VOICE
        # Body-less audio → almost certainly a voice note
        if body is not None and not (body or "").strip():
            return MessageType.VOICE
        return MessageType.AUDIO
    return MessageType.DOCUMENT


def _guess_mime_from_url(url: str) -> str:
    """Guess MIME type from a URL's file extension."""
    from urllib.parse import urlparse

    path = urlparse(url).path.lower()
    ext_to_mime = {
        # Audio
        ".ogg": "audio/ogg",
        ".opus": "audio/opus",
        ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".aac": "audio/aac",
        ".wav": "audio/wav",
        ".weba": "audio/webm",
        # Image
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".svg": "image/svg+xml",
        ".bmp": "image/bmp",
        # Video
        ".mp4": "video/mp4",
        ".webm": "video/webm",
        ".ogv": "video/ogg",
        ".mov": "video/quicktime",
        ".mkv": "video/x-matroska",
        # Documents
        ".pdf": "application/pdf",
        ".zip": "application/zip",
        ".gz": "application/gzip",
        ".tar": "application/x-tar",
        ".txt": "text/plain",
        ".html": "text/html",
    }
    for ext, mime in ext_to_mime.items():
        if path.endswith(ext):
            return mime
    return "application/octet-stream"


# aesgcm:// URL pattern — XEP-0454 OMEMO Media Sharing inbound.
# Format: aesgcm://<host>/<path>#<IV(24 hex)><key(64 hex)>
_AESGCM_URL_RE = re.compile(r"aesgcm://([^\s#]+)#([0-9a-fA-F]{88})")


# ----------------------------------------------------------------
# Lazy dependency helper for slixmpp
# ----------------------------------------------------------------

def check_xmpp_requirements() -> bool:
    """Confirm the [xmpp] extra is installed."""
    try:
        import slixmpp as _slixmpp
    except ImportError:
        return False
    return True


# ----------------------------------------------------------------
# OMEMO storage (JSON file backed)
# ----------------------------------------------------------------

if SLIXMPP_OMEMO_AVAILABLE:
    class _StorageImpl(Storage):  # type: ignore[misc]
        """Simple JSON-file backed OMEMO storage."""

        def __init__(self, json_file_path: Path) -> None:
            super().__init__()  # type: ignore[misc]
            self._path = json_file_path
            self._data: Dict[str, Any] = {}
            try:
                with open(self._path, encoding="utf-8") as f:
                    self._data = json.load(f)
            except Exception:
                pass

        async def _load(self, key: str) -> Any:  # type: ignore[override]
            if key in self._data:
                return Just(self._data[key])
            return Nothing()

        async def _store(self, key: str, value: Any) -> None:  # type: ignore[override]
            self._data[key] = value
            self._save()

        async def _delete(self, key: str) -> None:
            self._data.pop(key, None)
            self._save()

        def _save(self) -> None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)

    class _XEP_0384Impl(XEP_0384):  # type: ignore[misc,valid-type]
        """Concrete OMEMO plugin with BTBV and JSON-file storage."""

        default_config = {
            "fallback_message": "This message is OMEMO encrypted.",
            "json_file_path": None,
        }

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)  # type: ignore[misc]
            self.__storage = None  # type: ignore[var-annotated]

        def plugin_init(self) -> None:
            if not self.json_file_path:  # type: ignore[attr-defined]
                raise Exception("OMEMO JSON file path not specified.")
            self.__storage = _StorageImpl(Path(self.json_file_path))  # type: ignore[attr-defined]
            super().plugin_init()  # type: ignore[misc]

        @property
        def storage(self):
            return self.__storage

        @property
        def _btbv_enabled(self) -> bool:
            return True

        async def _devices_blindly_trusted(  # type: ignore[override]
            self,
            blindly_trusted,
            identifier,
        ) -> None:
            logger.info("OMEMO: blindly trusted %d device(s) [%s]", len(blindly_trusted), identifier)

        async def _prompt_manual_trust(  # type: ignore[override]
            self,
            manually_trusted,
            identifier,
        ) -> None:
            # BTBV is enabled so this is rare. Log and auto-distrust to avoid blocking.
            session_manager = await self.get_session_manager()
            for device in manually_trusted:
                logger.warning(
                    "OMEMO: manual trust required for %s %s — distrusting to avoid block",
                    device.bare_jid,
                    device.device_id
                )
                await session_manager.set_trust(
                    device.bare_jid,
                    device.identity_key,
                    TrustLevel.DISTRUSTED.value
                )

        async def get_session_manager(self):  # type: ignore[override]
            """Return a usable OMEMO session manager, recovering from failed init.

            slixmpp-omemo caches the in-flight initialization task. If that task
            fails once (for example, a server times out while fetching a twomemo
            device list), later decrypt/encrypt attempts await the same failed
            task forever. That makes one transient PubSub hiccup poison OMEMO
            until the whole gateway restarts. Reset the cached task on failure.

            Some servers/clients still behave much better with legacy OMEMO
            (oldmemo) than OMEMO:2 (twomemo). If initialization fails while
            touching the twomemo namespace, fall back to an oldmemo-only session
            manager so existing Conversations/Gajim-style devices can still
            decrypt instead of getting a useless "message from myself" blob.
            """
            try:
                return await super().get_session_manager()  # type: ignore[misc]
            except Exception as exc:
                self._reset_failed_session_manager()
                if "urn:xmpp:omemo:2" in str(exc):
                    logger.warning(
                        "OMEMO: twomemo initialization failed (%s); falling back to legacy OMEMO only",
                        exc,
                    )
                    try:
                        manager = await self._create_oldmemo_only_session_manager()
                        setattr(self, "_XEP_0384__session_manager", manager)
                        self.xmpp.event("omemo_initialized")  # type: ignore[attr-defined]
                        return manager
                    except Exception:
                        self._reset_failed_session_manager()
                        logger.exception("OMEMO: legacy fallback initialization failed")
                raise

        def _reset_failed_session_manager(self) -> None:
            task = getattr(self, "_XEP_0384__session_manager_task", None)
            if task is not None and not getattr(task, "done", lambda: True)():
                task.cancel()
            setattr(self, "_XEP_0384__session_manager_task", None)
            setattr(self, "_XEP_0384__session_manager", None)

        async def _create_oldmemo_only_session_manager(self):
            from slixmpp_omemo.xep_0384 import _make_session_manager  # type: ignore[import-untyped]
            from oldmemo.oldmemo import Oldmemo  # type: ignore[import-untyped]

            session_manager_cls = _make_session_manager(self.xmpp, self)  # type: ignore[attr-defined]
            manager = session_manager_cls.__new__(session_manager_cls)
            storage = self.storage
            if storage is None:
                raise RuntimeError("OMEMO storage is not initialized")
            backend = Oldmemo(storage)
            name_map = {
                "__backends": [backend],
                "__storage": storage,
                "__own_bare_jid": self.xmpp.boundjid.bare,  # type: ignore[attr-defined]
                "__undecided_trust_level_name": TrustLevel.UNDECIDED.value,
                "__synchronizing": False,
            }
            for name, value in name_map.items():
                setattr(manager, f"_SessionManager{name}", value)
            own_device_id = (await storage.load_primitive("/own_device_id", int)).from_just()
            setattr(manager, "_SessionManager__own_device_id", own_device_id)
            return manager

else:
    _StorageImpl = None  # type: ignore[misc,assignment]
    _XEP_0384Impl = None  # type: ignore[misc,assignment]

# ----------------------------------------------------------------
# MUC room helpers
# ----------------------------------------------------------------

@dataclass
class _MucRoom:
    """A configured MUC room the adapter joins on connect."""
    room: str
    nick: Optional[str] = None


def _parse_muc_rooms(value: str, default_nick: Optional[str]) -> List[_MucRoom]:
    rooms: List[_MucRoom] = []
    for entry in (value or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        if "/" in entry:
            room, _, nick = entry.partition("/")
            rooms.append(_MucRoom(room=room.strip(), nick=nick.strip() or default_nick))
        else:
            rooms.append(_MucRoom(room=entry, nick=default_nick))
    return rooms


# ----------------------------------------------------------------
# Adapter
# ----------------------------------------------------------------

class XmppAdapter(BasePlatformAdapter):
    """slixmpp-backed adapter satisfying BasePlatformAdapter."""

    MAX_MESSAGE_LENGTH = 4000

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("xmpp"))
        extra = config.extra or {}

        self.jid: str = str(extra.get("jid") or os.getenv("XMPP_JID", ""))
        self._password: str = str(extra.get("password") or os.getenv("XMPP_PASSWORD", ""))
        self.host: Optional[str] = extra.get("host") or os.getenv("XMPP_HOST") or None
        self.port: int = int(extra.get("port") or os.getenv("XMPP_PORT", 5222))
        self.muc_nick: str = extra.get("muc_nick") or os.getenv("XMPP_MUC_NICK") or self._default_nick()
        self.muc_rooms: List[_MucRoom] = _parse_muc_rooms(
            str(extra.get("muc_rooms") or os.getenv("XMPP_MUC_ROOMS", "")), self.muc_nick
        )

        # Allow-list
        allow_all_raw = str(extra.get("allow_all_users", os.getenv("XMPP_ALLOW_ALL_USERS", "")))
        self.allow_all_users: bool = allow_all_raw.strip().lower() in ("1", "true", "yes")
        allowed_env = str(extra.get("allowed_users") or os.getenv("XMPP_ALLOWED_USERS", "")).strip()
        self.allowed_users = {j.strip() for j in allowed_env.split(",") if j.strip()}

        # MUC mention gating — when true, only @mentioned groupchat
        # messages trigger replies.  Aligns with Telegram / Feishu /
        # WhatsApp / BlueBubbles require_mention.  Default: true.
        _rm_raw = str(extra.get("require_mention") or os.getenv("XMPP_REQUIRE_MENTION", "true"))
        self._muc_require_mention: bool = _rm_raw.strip().lower() in ("1", "true", "yes")

        # XEP-0394 / XEP-0071 rich markup (default: disabled to keep
        # message size small).  Enable explicitly in config.yaml extra
        # or via env vars.
        _mk_raw = str(extra.get("xep_0394_enabled") or os.getenv("XMPP_XEP_0394_ENABLED", "false"))
        self._xep_0394_enabled: bool = _mk_raw.strip().lower() in ("1", "true", "yes")
        _html_raw = str(extra.get("xep_0071_enabled") or os.getenv("XMPP_XEP_0071_ENABLED", "false"))
        self._xep_0071_enabled: bool = _html_raw.strip().lower() in ("1", "true", "yes")

        # OMEMO
        omemo_cfg = extra.get("omemo", {})
        self._omemo_enabled: bool = bool(
            omemo_cfg.get("enabled")
            if isinstance(omemo_cfg, dict) and "enabled" in omemo_cfg
            else extra.get("omemo_enabled", os.getenv("XMPP_OMEMO_ENABLED", "true"))
        )
        self._omemo_storage_path: str = str(
            (omemo_cfg.get("storage_path") if isinstance(omemo_cfg, dict) else None)
            or extra.get("omemo_storage_path")
            or os.getenv("XMPP_OMEMO_STORAGE_PATH", "")
        ) or str(Path(os.getenv("HERMES_HOME", os.path.expanduser("~/.hermes"))) / "xmpp_omemo.json")
        self._omemo_initialized = asyncio.Event()
        self._omemo_initialized_occurred = False

        # Reconnection state
        self._reconnecting: bool = False

        # Lazy state
        self.client: Optional[Any] = None
        self._process_task: Optional[asyncio.Task] = None
        self._session_ready: Optional[asyncio.Event] = None
        self._self_bare = self._bare(self.jid)
        self._known_mucs = {r.room for r in self.muc_rooms}
        self._registered_plugins: set[str] = set()

        # Reaction state: message_id → original stanza (for lifecycle hooks)
        self._pending_reactions: Dict[str, Any] = {}
        self._reactions_enabled: bool = os.getenv("XMPP_REACTIONS", "true").lower() not in {"false", "0", "no"}

        # Reaction-based dangerous command approvals (cf. Matrix send_exec_approval)
        self._approval_reaction_map = {
            "✅": "once",
            "❎": "deny",
        }
        self._approval_prompts_by_event: Dict[str, dict] = {}
        self._approval_prompt_by_session: Dict[str, str] = {}

        # Reaction-based clarify (cf. reaction-based exec approval pattern)
        self._clarify_prompts_by_event: Dict[str, dict] = {}
        self._clarify_prompt_by_session: Dict[str, str] = {}

        # MAM (XEP-0313) state — in-memory only (aligns with Telegram's
        # drop_pending_updates pattern; survives Phase 1 reconnect but
        # intentionally resets on Phase 2 gateway watcher restart).
        self._mam_enabled: bool = False
        self._mam_replaying: bool = False
        self._mam_last_dm: Optional[datetime] = None
        self._mam_last_rooms: Dict[str, datetime] = {}

    # -----------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------

    async def connect(self) -> bool:
        # Reset reconnection state for a fresh connection attempt
        self._reconnecting = False

        client = ClientXMPP(self.jid, self._password)
        # Plugins - core
        for plugin in ("xep_0030", "xep_0045", "xep_0066", "xep_0085", "xep_0198", "xep_0199", "xep_0363"):
            try:
                client.register_plugin(plugin)
                self._registered_plugins.add(plugin)
            except Exception:
                logger.warning("xmpp: failed to register slixmpp plugin %s", plugin)

        # Plugins - first-class features (XEP-0394, 0444, 0004, 0050, 0461, 0447)
        # Lazy-load: if slixmpp doesn't have them the adapter continues without them.
        for plugin in ("xep_0071", "xep_0394", "xep_0444", "xep_0004", "xep_0050", "xep_0461", "xep_0446", "xep_0447", "xep_0308", "xep_0424", "xep_0359", "xep_0333", "xep_0372", "xep_0513", "xep_0454", "xep_0313"):
            try:
                client.register_plugin(plugin)
                self._registered_plugins.add(plugin)
                logger.debug("xmpp: registered slixmpp plugin %s", plugin)
            except Exception:
                logger.warning("xmpp: slixmpp plugin %s not available", plugin)

        # Suppress debug print('coucou', ...) in slixmpp's xep_0394.to_xhtml_im()
        if "xep_0394" in self._registered_plugins:
            try:
                import contextlib, io, functools
                xep0394 = client["xep_0394"]
                _orig_to_xhtml = xep0394.to_xhtml_im
                @functools.wraps(_orig_to_xhtml)
                def _quiet_to_xhtml_im(body, markup):
                    with contextlib.redirect_stdout(io.StringIO()):
                        return _orig_to_xhtml(body, markup)
                xep0394.to_xhtml_im = _quiet_to_xhtml_im  # type: ignore[method-assign]
                logger.debug("xmpp: suppressed debug print in to_xhtml_im()")
            except Exception:
                pass

        # ── Stanza registrations for inbound features ──────────────
        # XEP-0080: register Geoloc on Message so stanza['geoloc'] works
        try:
            from slixmpp.plugins.xep_0080.stanza import Geoloc  # type: ignore[import-untyped]
            register_stanza_plugin(Message, Geoloc)
            logger.debug("xmpp: registered XEP-0080 Geoloc on Message stanza")
        except Exception:
            logger.debug("xmpp: XEP-0080 Geoloc stanza not available")

        # XEP-0372: re-register Reference as iterable for multi-ref messages
        try:
            from slixmpp.plugins.xep_0372.stanza import Reference  # type: ignore[import-untyped]
            Reference.plugin_multi_attrib = 'references'
            register_stanza_plugin(Message, Reference, iterable=True)
            logger.debug("xmpp: re-registered XEP-0372 Reference as iterable")
        except Exception:
            logger.debug("xmpp: XEP-0372 Reference stanza not available")

        # MAM (XEP-0313) — in-memory only; no persisted state
        if "xep_0313" in self._registered_plugins:
            self._mam_enabled = True
            logger.debug("xmpp: MAM (XEP-0313) enabled — will replay missed messages after Phase-1 reconnect")

        # OMEMO plugin registration
        omemo_ok = False
        if self._omemo_enabled and SLIXMPP_OMEMO_AVAILABLE:
            try:
                register_plugin(_XEP_0384Impl)
                client.register_plugin(
                    "xep_0384",
                    {"json_file_path": self._omemo_storage_path},
                )
                self._registered_plugins.add("xep_0384")
                client.add_event_handler("omemo_initialized", self._on_omemo_initialized)
                omemo_ok = True
            except Exception:
                logger.exception("xmpp: failed to register OMEMO plugin")
        elif self._omemo_enabled and not SLIXMPP_OMEMO_AVAILABLE:
            logger.warning(
                "xmpp: OMEMO enabled but slixmpp-omemo not installed. "
                "Install with: uv pip install slixmpp-omemo omemo"
            )

        # TLS
        client.use_starttls = True  # type: ignore[attr-defined,reportAttributeAccessIssue]
        client.force_starttls = True  # type: ignore[attr-defined,reportAttributeAccessIssue]

        client.add_event_handler("session_start", self._on_session_start)
        client.add_event_handler("message", self._on_message)
        client.add_event_handler("disconnected", self._on_disconnected)
        client.add_event_handler("failed_auth", self._on_failed_auth)
        # XEP-0444: slixmpp fires 'reactions' event for inbound reactions.
        # Use this instead of manual stanza parsing — it carries the full
        # Message with parsed Reactions data.
        if "xep_0444" in self._registered_plugins:
            client.add_event_handler("reactions", self._on_reaction)
        # XEP-0198 Stream Management — log state transitions
        if "xep_0198" in self._registered_plugins:
            client.add_event_handler("sm_enabled", self._on_sm_enabled)
            client.add_event_handler("session_resumed", self._on_sm_resumed)
            client.add_event_handler("sm_failed", self._on_sm_failed)
            client.add_event_handler("sm_disabled", self._on_sm_disabled)

        self.client = client
        self._session_ready = asyncio.Event()
        self._omemo_initialized.clear()
        self._omemo_initialized_occurred = False

        connect_kwargs: Dict[str, Any] = {}
        if self.host:
            connect_kwargs["address"] = (self.host, self.port)

        try:
            ok = client.connect(**connect_kwargs)
        except TypeError:
            ok = client.connect()
        if ok is False:
            self._set_fatal_error(
                "xmpp_connect_failed", "XMPP connect() returned False", retryable=True
            )
            return False

        loop = asyncio.get_event_loop()
        self._process_task = loop.create_task(self._run_process())

        if omemo_ok:
            logger.info("XMPP adapter: OMEMO enabled (storage: %s)", self._omemo_storage_path)
        else:
            logger.warning(
                "XMPP adapter is running without OMEMO. Messages are encrypted in "
                "transit (TLS) but visible to your XMPP server operator."
            )
        self._mark_connected()
        return True

    async def disconnect(self) -> None:
        self._reconnecting = False  # cancel any in-flight reconnect
        if self.client is not None:
            # Remove our handler so intentional disconnect doesn't trigger
            # slixmpp reconnect.  The deferred _end_stream_wait may fire
            # 'disconnected' after we return — this ensures it's a no-op.
            self.client.del_event_handler('disconnected', self._on_disconnected)
            try:
                self.client.disconnect()
            except Exception:
                logger.exception("xmpp: error during disconnect()")
        if self._process_task is not None:
            self._process_task.cancel()
            self._process_task = None
        self.client = None
        self._mark_disconnected()

    async def _run_process(self) -> None:
        """slixmpp's process loop.  Awaits client.disconnected.

        When the connected future resolves (network loss, server restart,
        or intentional shutdown), this coroutine cleans up.  If the
        disconnect was unexpected and a reconnect is in progress
        (initiated by _on_disconnected via slixmpp's reconnect()), the
        task is restarted to watch the new connection.  Intentional
        shutdowns propagate CancelledError and perform final cleanup.
        """
        if self.client is None:
            return
        try:
            await self.client.disconnected
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("xmpp: process loop crashed")
        finally:
            if self._reconnecting:
                # Reconnect in progress — slixmpp's _connect_loop is
                # retrying.  Restart this task so it watches the new
                # connection's lifecycle.
                loop = asyncio.get_event_loop()
                self._process_task = loop.create_task(self._run_process())
            elif (
                self.client is not None
                and not self.has_fatal_error
            ):
                # Unexpected exit with no reconnect in flight.
                # Signal the gateway watcher to take over.
                logger.warning(
                    "xmpp: process loop exited without explicit disconnect — "
                    "signalling reconnect"
                )
                self._set_fatal_error(
                    "xmpp_process_lost",
                    "XMPP process loop ended unexpectedly — will retry",
                    retryable=True,
                )
                if self.client is not None:
                    self._reconnecting = True  # prevent _on_disconnected re-trigger
                    try:
                        self.client.disconnect()
                    except Exception:
                        pass
                    self.client = None

    async def _on_session_start(self, _event: Any) -> None:
        if self.client is None:
            return

        was_reconnecting = self._reconnecting

        # Handle successful reconnection
        if self._reconnecting:
            self._reconnecting = False
            self._mark_connected()
            logger.info("xmpp: reconnected successfully")

        self.client.send_presence()  # type: ignore[union-attr]
        try:
            await self.client.get_roster()  # type: ignore[union-attr]
        except Exception:
            logger.exception("xmpp: get_roster failed")
        for room in self.muc_rooms:
            try:
                self.client.plugin["xep_0045"].join_muc(room.room, room.nick or self.muc_nick)  # type: ignore[union-attr]
            except Exception:
                logger.exception("xmpp: failed to join MUC %s", room.room)
        if self._session_ready is not None:
            self._session_ready.set()
        # Register ad-hoc commands now that session is active
        try:
            await self._setup_adhoc_commands()
        except Exception:
            logger.debug("xmpp: ad-hoc command setup failed", exc_info=True)

        # MAM (XEP-0313) catch-up: replay messages missed during disconnect.
        # Fires as a background task to avoid blocking session startup.
        # SM resume (XEP-0198) may have already replayed some messages — the
        # timestamp-based dedup ensures no duplicates.
        if self._mam_enabled and was_reconnecting:
            asyncio.create_task(self._mam_catch_up())

    def _on_disconnected(self, _event: Any) -> None:
        """Synchronous handler — must NOT be async.

        slixmpp fires the 'disconnected' event synchronously inside
        connection_lost(), before resolving client.disconnected (the Future).
        If this handler is async, slixmpp creates a task for it that runs
        AFTER _run_process wakes up from client.disconnected — the finally
        block sees _reconnecting=False and nukes self.client before we get
        a chance to act.

        Making it synchronous guarantees _reconnecting is set before
        _run_process resumes.
        """
        if self._reconnecting:
            # Reconnect already in progress — slixmpp's reconnect() calls
            # disconnect() internally, which fires this event again.
            return
        if self.client is None:
            # Stale event after client cleanup — nothing to reconnect.
            return
        logger.warning(
            "xmpp: unexpected disconnect — attempting slixmpp reconnect (SM resume if XEP-0198 active)"
        )
        self._reconnecting = True
        try:
            self.client.reconnect(wait=0.0, reason="Network unreachable")
        except Exception:
            logger.exception("xmpp: reconnect() call failed, falling back to gateway watcher")
            self._reconnecting = False
            self._set_fatal_error(
                "xmpp_disconnected",
                "XMPP connection lost — will retry via gateway",
                retryable=True,
            )

    async def _on_failed_auth(self, _event: Any) -> None:
        self._set_fatal_error(
            "xmpp_auth_failed",
            "XMPP authentication failed — check XMPP_JID/XMPP_PASSWORD",
            retryable=False,
        )

    # ── XEP-0198 Stream Management handlers ─────────────────────────

    def _on_sm_enabled(self, _event: Any) -> None:
        logger.info("xmpp: Stream Management (XEP-0198) enabled — stanzas will be acked")

    def _on_sm_resumed(self, _event: Any) -> None:
        """Session resumed after reconnect — unacked stanzas replayed."""
        logger.info("xmpp: SM session resumed — unacked stanzas replayed")

    def _on_sm_failed(self, _event: Any) -> None:
        """SM enable/resume failed — server may not support it."""
        logger.warning("xmpp: SM enable/resume failed — server may not support XEP-0198")

    def _on_sm_disabled(self, _event: Any) -> None:
        logger.debug("xmpp: SM disabled (connection lost or session ended)")

    # ── OMEMO ───────────────────────────────────────────────────────

    async def _on_omemo_initialized(self, _event: Any) -> None:
        logger.info("OMEMO: initialized")
        self._omemo_initialized_occurred = True
        self._omemo_initialized.set()

    # -----------------------------------------------------------------
    # Inbound
    # -----------------------------------------------------------------

    @staticmethod
    def _extract_stanza_id(stanza: Any, room_bare_jid: str) -> Optional[str]:
        """Extract XEP-0359 <stanza-id> where @by matches room bare JID."""
        try:
            sid = stanza['stanza_id']
            # Single element — check @by directly
            if getattr(sid, 'id', None) and str(getattr(sid, 'by', '')) == room_bare_jid:
                return str(sid.id)
            # Multiple <stanza-id> elements — iterate XML for the right one
            xml = getattr(stanza, 'xml', None)
            if xml is not None:
                from slixmpp.plugins.xep_0359.stanza import NS
                for elem in xml.iter():
                    if elem.tag == '{%s}stanza-id' % NS:
                        if elem.get('by', '') == room_bare_jid:
                            return elem.get('id') or None
        except Exception:
            pass
        return None

    @staticmethod
    def _extract_origin_id(stanza: Any) -> Optional[str]:
        """Extract XEP-0359 <origin-id> for 1:1 chats."""
        try:
            oid = stanza['origin_id']
            if oid is not None:
                return str(getattr(oid, 'id', '')) or None
        except Exception:
            pass
        return None

    async def _on_message(self, stanza: Any) -> None:
        try:
            stanza_type = stanza["type"]
            if stanza_type in ("error", "headline"):
                return
            if stanza_type not in ("chat", "groupchat", "normal"):
                return

            from_jid = stanza.get_from()
            from_full = str(from_jid)
            from_bare = getattr(from_jid, "bare", None) or self._bare(from_full)
            from_resource = getattr(from_jid, "resource", "") or ""

            if from_bare == self._self_bare:
                return

            body = stanza["body"] or ""
            stanza_to_dispatch = stanza

            # OMEMO decryption
            if self.client is not None and "xep_0384" in self._registered_plugins and SLIXMPP_OMEMO_AVAILABLE:
                client_local = self.client  # type: ignore[assignment]
                xep_0384 = client_local["xep_0384"]
                if xep_0384.is_encrypted(stanza):
                    try:
                        decrypted_stanza, device_info = await xep_0384.decrypt_message(stanza)
                        body = decrypted_stanza.get("body", "") or ""
                        stanza_to_dispatch = decrypted_stanza
                        logger.debug(
                            "OMEMO: decrypted message from %s (device %s)",
                            from_bare, device_info.device_id
                        )
                    except Exception as exc:
                        logger.warning("OMEMO: failed to decrypt message from %s: %s", from_bare, exc)
                        return

            # ── Inbound aesgcm:// (XEP-0454 OMEMO Media Sharing) ──────
            # After OMEMO decryption the body may contain aesgcm:// URLs
            # pointing to encrypted files.  Download + decrypt + cache
            # before the normal SFS/OOB path (which can't handle aesgcm://).
            aesgcm_urls: List[str] = []
            aesgcm_media_urls: List[str] = []
            aesgcm_media_types: List[str] = []
            try:
                # Collect aesgcm:// URLs from body
                if body:
                    for m in _AESGCM_URL_RE.finditer(body):
                        aesgcm_urls.append(m.group(0))
                # Also check OOB (XEP-0066) for aesgcm://
                if not aesgcm_urls and "xep_0066" in self._registered_plugins:
                    try:
                        oob_url = stanza_to_dispatch["oob"]["url"]
                        if oob_url and str(oob_url).startswith("aesgcm://"):
                            aesgcm_urls.append(str(oob_url))
                    except Exception:
                        pass

                for aesgcm_url in aesgcm_urls:
                    cached, mime = await self._decrypt_aesgcm(aesgcm_url)
                    if cached is not None and mime is not None:
                        aesgcm_media_urls.append(cached)
                        aesgcm_media_types.append(mime)
                        # Strip this aesgcm:// URL from body
                        body = body.replace(aesgcm_url, "").strip()
            except Exception:
                logger.debug("xmpp: aesgcm:// extraction skipped", exc_info=True)

            # ── Inbound Voice / Media Extraction ──────────────────────
            # XEP-0447 SFS (Stateless File Sharing) or XEP-0066 OOB
            media_urls: List[str] = []
            media_types: List[str] = []
            message_type = MessageType.TEXT

            # Prepend aesgcm-decrypted media (processed above)
            if aesgcm_media_urls:
                media_urls.extend(aesgcm_media_urls)
                media_types.extend(aesgcm_media_types)

            try:
                # SFS (XEP-0447) — primary path for modern clients
                sfs_urls: List[str] = []
                sfs_media: Optional[str] = None
                if "xep_0447" in self._registered_plugins:
                    try:
                        sfs_el = stanza_to_dispatch["sfs"]
                        if sfs_el and sfs_el.xml is not None:
                            for url_data in sfs_el["sources"]:
                                target = url_data["target"]
                                if target:
                                    sfs_urls.append(str(target))
                            # media-type from optional <file> metadata
                            try:
                                sfs_media = sfs_el["file"]["media-type"] or None
                            except Exception:
                                pass
                    except Exception:
                        pass  # no SFS element or plugin not active

                for url in sfs_urls:
                    try:
                        mime = sfs_media or _guess_mime_from_url(url)
                        ext = _mime_to_ext(mime)
                        if mime.startswith("image/"):
                            cached = await cache_image_from_url(url, ext=ext)
                        else:
                            cached = await cache_audio_from_url(url, ext=ext)
                        media_urls.append(cached)
                        media_types.append(mime)
                        logger.debug("xmpp: cached SFS media (%s) from %s → %s", mime, url, cached)
                    except Exception as exc:
                        logger.warning("xmpp: failed to download SFS media %s: %s", url, exc)

                # OOB (XEP-0066) — fallback for older clients
                if not sfs_urls and "xep_0066" in self._registered_plugins:
                    try:
                        oob_url = stanza_to_dispatch["oob"]["url"]
                        if oob_url:
                            url_str = str(oob_url)
                            mime = _guess_mime_from_url(url_str)
                            ext = _mime_to_ext(mime)
                            if mime.startswith("image/"):
                                cached = await cache_image_from_url(url_str, ext=ext)
                            else:
                                cached = await cache_audio_from_url(url_str, ext=ext)
                            media_urls.append(cached)
                            media_types.append(mime)
                            logger.debug("xmpp: cached OOB media (%s) from %s → %s", mime, url_str, cached)
                    except Exception:
                        pass  # no OOB element

                # Classify media type
                if media_urls:
                    message_type = _mime_to_message_type(media_types[0], body=body)
            except Exception:
                logger.debug("xmpp: media extraction skipped", exc_info=True)

            # ── Inbound GeoLoc (XEP-0080 + geo: URI fallback) ───────
            # Two paths:
            #   1. XEP-0080 <geoloc> element (standard, but few clients use it)
            #   2. geo: URI in body/OOB (Conversations' actual location format)
            geoloc_data: Optional[Dict[str, str]] = None

            if not media_urls:
                # Path 1: XEP-0080 <geoloc xmlns='http://jabber.org/protocol/geoloc'>
                try:
                    geoloc = stanza_to_dispatch["geoloc"]
                    if geoloc is not None and geoloc.xml is not None:
                        lat = geoloc["lat"]
                        lon = geoloc["lon"]
                        if lat and lon:
                            geoloc_data = {
                                "lat": str(lat),
                                "lon": str(lon),
                                "description": geoloc["description"] or "",
                            }
                except Exception:
                    pass

                # Path 2: geo: URI (RFC 5870) — used by Conversations
                if not geoloc_data:
                    geo_uri = ""
                    if body and body.strip().startswith("geo:"):
                        geo_uri = body.strip()
                    elif "xep_0066" in self._registered_plugins:
                        try:
                            oob_url = stanza_to_dispatch["oob"]["url"]
                            if oob_url and str(oob_url).startswith("geo:"):
                                geo_uri = str(oob_url)
                        except Exception:
                            pass
                    if geo_uri:
                        m = re.match(
                            r"geo:(-?\d+\.?\d*),(-?\d+\.?\d*)",
                            geo_uri,
                        )
                        if m:
                            geoloc_data = {
                                "lat": m.group(1),
                                "lon": m.group(2),
                                "description": "",
                            }

            if geoloc_data:
                message_type = MessageType.LOCATION
                parts = ["[The user shared a location pin.]"]
                if geoloc_data["description"]:
                    parts.append(f"Description: {geoloc_data['description']}")
                parts.append(
                    f"latitude: {geoloc_data['lat']}, longitude: {geoloc_data['lon']}"
                )
                location_text = "\n".join(parts)
                if not body:
                    body = location_text
                elif body.strip().startswith("geo:"):
                    # Strip the geo: URI, keep any user text after it
                    remaining = re.sub(
                        r"^geo:-?\d+\.?\d*,-?\d+\.?\d*\s*",
                        "", body.strip(),
                    ).strip()
                    if remaining:
                        body = remaining + "\n\n" + location_text
                    else:
                        body = location_text
                else:
                    body = body + "\n\n" + location_text

            if not body and not media_urls:
                return

            if stanza_type == "groupchat":
                chat_type = "group"
                chat_id = from_bare
                user_name = from_resource or None
                # Resolve real JID: roster (from presence stanzas) → stanza → room JID
                real_jid = None
                try:
                    if self.client is not None and "xep_0045" in self._registered_plugins:
                        jid_val = self.client["xep_0045"].get_jid_property(
                            from_bare, from_resource, "jid"
                        )
                        if jid_val:
                            real_jid = self._bare(str(jid_val))
                except Exception:
                    pass
                # Drop own echoed MUC messages (server reflects bot's messages back)
                if real_jid and real_jid == self._self_bare:
                    return
                if from_resource:
                    our_nick = self._muc_nick_for_room(from_bare)
                    if our_nick and from_resource == our_nick:
                        return

                user_id = real_jid or self._muc_real_jid(stanza) or chat_id
                if real_jid:
                    logger.debug("xmpp: MUC real JID from roster: %s → %s", from_resource, real_jid)
                elif user_id == chat_id:
                    logger.debug("xmpp: MUC anonymous — no real JID for %s in %s", from_resource, from_bare)

                # ── @mention detection ─────────────────────────────────
                # Three-layer detection, newest standard first:
                #  Layer 0: XEP-0513 Explicit Mentions (urn:xmpp:mentions:0)
                #  Layer 1: XEP-0372 References (urn:xmpp:reference:0)
                #  Layer 2: Nick-in-body substring (traditional fallback)
                mentioned: bool = False
                mention_begin: Optional[int] = None
                mention_end: Optional[int] = None
                our_nick = self._muc_nick_for_room(from_bare)

                # ── Layer 0: XEP-0513 Explicit Mentions ─────────────────
                try:
                    for m in stanza_to_dispatch["mentions"]:
                        # Individual mention via JID
                        m_jid = m["jid"]
                        if m_jid is not None:
                            m_bare = str(m_jid.bare) if hasattr(m_jid, 'bare') else str(m_jid)
                            if m_bare == self._self_bare:
                                mentioned = True
                                mention_begin = m["begin"]
                                mention_end = m["end"]
                                logger.debug("xmpp: XEP-0513 mention (jid=%s, begin=%s, end=%s)",
                                             m_bare, mention_begin, mention_end)
                                break
                        # Individual mention via occupant-id (XEP-0421) — not yet implemented
                        # Group mention via 'mentions' attribute (e.g. #channel, #moderators)
                        if m["mentions"]:
                            logger.debug("xmpp: XEP-0513 group mention: %s", m["mentions"])
                            mentioned = True
                            break
                except Exception:
                    pass

                # ── Layer 1: XEP-0372 References ────────────────────────
                if not mentioned:
                    try:
                        for ref in stanza_to_dispatch["references"]:
                            if ref["type"] == "mention":
                                uri = ref["uri"] or ""
                                if self._self_bare in uri:
                                    mentioned = True
                                    break
                    except Exception:
                        pass

                # ── Layer 2: Nick-in-body fallback ──────────────────────
                if not mentioned and from_resource:
                    if our_nick and body:
                        mentioned = our_nick in body

                # ── Mention prefix stripping ────────────────────────────
                if mentioned:
                    logger.debug("xmpp: bot mentioned in MUC %s by %s", from_bare, from_resource)
                    if body:
                        # Prefer XEP-0513 begin/end (precise substring indices)
                        if mention_begin is not None and mention_end is not None:
                            try:
                                _b = int(mention_begin) if not isinstance(mention_begin, int) else mention_begin
                                _e = int(mention_end) if not isinstance(mention_end, int) else mention_end
                                if 0 <= _b < _e <= len(body):
                                    body = (body[:_b] + body[_e:]).lstrip()
                                    logger.debug("xmpp: stripped mention via XEP-0513 begin/end [%d:%d] → %r", _b, _e, body)
                            except (ValueError, TypeError):
                                pass
                        # Fallback: nick-based prefix stripping
                        if body and our_nick:
                            _lower_body = body.lower()
                            _lower_nick = our_nick.lower()
                            if _lower_body.startswith(_lower_nick):
                                _after = body[len(our_nick):]
                                if _after.startswith((": ", " ", ":", ",")):
                                    body = _after.lstrip(": ,").lstrip()
                                    logger.debug("xmpp: stripped mention prefix via nick → %r", body)

                # ── MUC mention gating ────────────────────────────────
                # When require_mention is enabled, drop groupchat messages
                # that don't @mention the bot.  Aligns with Telegram /
                # Feishu / WhatsApp / BlueBubbles behaviour.
                if not mentioned and self._muc_require_mention:
                    logger.debug(
                        "xmpp: dropping unmentioned MUC message in %s from %s",
                        from_bare, from_resource,
                    )
                    return
            else:
                chat_type = "dm"
                chat_id = from_bare
                user_name = None
                user_id = from_bare

            if not self._is_authorized(chat_type=chat_type, chat_id=chat_id, user_jid=user_id):
                logger.debug(
                    "xmpp: dropping unauthorized %s from %s in %s",
                    chat_type, user_id, chat_id,
                )
                return

            # Extract XEP-0201 thread id for session isolation
            thread_id: Optional[str] = None
            try:
                thread_elem = stanza.get("thread", None)
                if thread_elem is not None:
                    thread_id = str(thread_elem) or None
            except Exception:
                pass

            source = self.build_source(
                chat_id=chat_id,
                chat_type=chat_type,
                user_id=user_id,
                user_name=user_name,
                thread_id=thread_id,
            )
            # Extract reply context for XEP-0461
            reply_to_message_id = None
            reply_to_text = None
            if self.client is not None and stanza_to_dispatch is not None:
                try:
                    reply_elem = stanza_to_dispatch.get("reply", None)
                    if reply_elem is not None:
                        reply_to_message_id = reply_elem.get("id", None)
                        # Build fallback body from the reply for context injection
                        raw_body = stanza_to_dispatch.get("body", "") or ""
                        # If the reply has fallback markers, strip them
                        if hasattr(reply_elem, "strip_fallback_content"):
                            stripped = reply_elem.strip_fallback_content()
                            if stripped:
                                reply_to_text = stripped
                            else:
                                reply_to_text = raw_body
                        else:
                            reply_to_text = raw_body
                except Exception:
                    pass
            # XEP-0461 §4.2: for groupchat, use <stanza-id> (XEP-0359) instead
            # of the stanza's 'id' attribute, which MUST NOT be used for replies.
            msg_id = stanza.get("id") or None
            reply_to_jid: Optional[str] = None
            if stanza_type == "groupchat":
                try:
                    stanza_id = self._extract_stanza_id(stanza, from_bare)
                    if stanza_id:
                        msg_id = stanza_id
                except Exception:
                    pass
                reply_to_jid = from_full  # store sender's Full JID for reply @to
            else:
                # 1:1 prefers <origin-id>, falls back to stanza 'id'
                try:
                    origin_id = self._extract_origin_id(stanza)
                    if origin_id:
                        msg_id = origin_id
                except Exception:
                    pass
            event = MessageEvent(
                text=body,
                message_type=message_type,
                source=source,
                raw_message=stanza_to_dispatch,
                message_id=msg_id,
                reply_to_message_id=reply_to_message_id,
                reply_to_text=reply_to_text,
                media_urls=media_urls,
                media_types=media_types,
            )
            await self.handle_message(event)

            # ── MAM timestamp tracking ───────────────────────────────
            # Update last-seen time so future MAM catch-up queries only
            # fetch messages after this point.  Skip during MAM replay
            # (historical messages should not advance the timestamp).
            if not self._mam_replaying:
                self._mam_update_timestamp(chat_type, chat_id)

            # ── XEP-0333 Chat Marker: acknowledge as displayed ──────
            # Skip for MAM-replayed (historical) messages.
            if not self._mam_replaying and msg_id and from_full and "xep_0333" in self._registered_plugins and self.client is not None:
                try:
                    mtype = "groupchat" if stanza_type == "groupchat" else "chat"
                    self.client["xep_0333"].send_marker(
                        mto=JID(from_full),
                        id=msg_id,
                        marker="displayed",
                        mtype=mtype,
                    )
                except Exception:
                    logger.debug("xmpp: failed to send chat marker", exc_info=True)
        except Exception:
            logger.exception("xmpp: error handling inbound stanza")

    # ------------------------------------------------------------------
    # Auth / nick helpers
    # ------------------------------------------------------------------

    def _muc_nick_for_room(self, room_jid: str) -> Optional[str]:
        """Return the bot's nick for a given MUC room, or None if unknown."""
        for room_config in self.muc_rooms:
            if room_config.room == room_jid:
                return room_config.nick or self.muc_nick
        return self.muc_nick  # fallback: default nick

    def _muc_real_jid(self, stanza: Any) -> Optional[str]:
        """Extract the real JID from a MUC stanza's <x> element.

        In non-anonymous MUCs, the server includes the sender's real JID
        in <x xmlns='http://jabber.org/protocol/muc#user'><item jid='...'/>.
        slixmpp exposes this via stanza['muc']['jid'] (MUCBase.get_jid
        reads it from the <item/> child).  We must use __getitem__ access
        ('muc['jid']') — Python attribute access (muc.jid) does NOT work
        because slixmpp's MUCMessage does not expose 'jid' as a Python
        attribute (only via __getitem__ interface resolution).
        """
        try:
            muc = stanza.get("muc")
            if muc and muc["jid"]:
                jid = str(muc["jid"])
                logger.debug("xmpp: MUC real JID extracted: %s", jid)
                return self._bare(jid)
        except Exception as exc:
            logger.debug("xmpp: MUC real JID extraction failed: %s", exc)
        return None

    def _is_authorized(self, *, chat_type: str, chat_id: str, user_jid: str) -> bool:
        if self.allow_all_users:
            return True
        if chat_type == "group":
            return chat_id in self._known_mucs
        if not self.allowed_users:
            return True  # No allowlist → delegate to gateway pairing system
        return self._bare(user_jid) in self.allowed_users

    # -----------------------------------------------------------------
    # MAM (XEP-0313) — Message Archive Management (in-memory, no disk I/O)
    #
    # State is intentionally ephemeral — aligns with Telegram's
    # drop_pending_updates pattern. Timestamps survive Phase-1 reconnect
    # (same adapter instance) but reset on Phase-2 gateway watcher restart.
    # -----------------------------------------------------------------

    def _mam_update_timestamp(self, chat_type: str, chat_id: str) -> None:
        """Update the MAM last-seen timestamp after processing a message."""
        if not self._mam_enabled:
            return
        now = datetime.now(timezone.utc)
        if chat_type == "dm":
            self._mam_last_dm = now
        else:
            self._mam_last_rooms[chat_id] = now

    async def _mam_catch_up(self) -> None:
        """Replay missed messages from the server archive after reconnect.

        Queries the user's DM archive and each joined MUC room's archive
        for messages sent between the last known timestamp and now.
        Dispatches each forwarded stanza through the normal message handler.

        Called as a background task from _on_session_start — does not
        block session startup.
        """
        if self.client is None or "xep_0313" not in self._registered_plugins:
            return

        self._mam_replaying = True
        try:
            cutoff = datetime.now(timezone.utc)
            total_replayed = 0

            # ── DM archive ──────────────────────────────────────────
            if self._mam_last_dm is not None:
                try:
                    count = 0
                    async for mam_msg in self.client["xep_0313"].iterate(
                        start=self._mam_last_dm,
                        end=cutoff,
                        reverse=False,
                        total=50,  # safety cap — don't replay too many old messages
                    ):
                        await self._mam_dispatch_forwarded(mam_msg)
                        count += 1
                    if count:
                        logger.debug(
                            "xmpp: MAM replayed %d DM messages (since %s)",
                            count, self._mam_last_dm.isoformat(),
                        )
                        total_replayed += count
                except Exception:
                    logger.debug("xmpp: MAM DM query failed", exc_info=True)

            # ── MUC archives ────────────────────────────────────────
            # Small delay to allow MUC join presence to be processed
            # by the server before querying room archives.
            if self.muc_rooms and any(
                self._mam_last_rooms.get(r.room) is not None
                for r in self.muc_rooms
            ):
                await asyncio.sleep(1.0)

            for room in self.muc_rooms:
                room_jid = room.room
                last_ts = self._mam_last_rooms.get(room_jid)
                if last_ts is None:
                    # First connect for this room — set timestamp so we
                    # don't replay old history on the next reconnect.
                    self._mam_last_rooms[room_jid] = cutoff
                    continue
                try:
                    count = 0
                    async for mam_msg in self.client["xep_0313"].iterate(
                        jid=JID(room_jid),
                        start=last_ts,
                        end=cutoff,
                        reverse=False,
                        total=50,
                    ):
                        await self._mam_dispatch_forwarded(mam_msg)
                        count += 1
                    if count:
                        logger.debug(
                            "xmpp: MAM replayed %d messages from %s (since %s)",
                            count, room_jid, last_ts.isoformat(),
                        )
                        total_replayed += count
                except Exception:
                    logger.debug(
                        "xmpp: MAM query for %s failed", room_jid, exc_info=True,
                    )

            # ── Update timestamps ───────────────────────────────────
            self._mam_last_dm = cutoff
            for room in self.muc_rooms:
                self._mam_last_rooms[room.room] = cutoff

            if total_replayed:
                logger.debug("xmpp: MAM catch-up complete — %d total messages replayed", total_replayed)

        except Exception:
            logger.exception("xmpp: MAM catch-up failed")
        finally:
            self._mam_replaying = False

    async def _mam_dispatch_forwarded(
        self,
        mam_message: Any,
    ) -> None:
        """Extract and dispatch a forwarded stanza from a MAM result.

        MAM results wrap the original message in:
          <result xmlns='urn:xmpp:mam:2'>
            <forwarded xmlns='urn:xmpp:forward:0'>
              <delay stamp='...'/>
              <message ...>original stanza</message>
            </forwarded>
          </result>

        The slixmpp MAM plugin's register_stanza_plugin chain wires up
        the stanza types so that forwarded['stanza'] returns the original
        Message object ready for dispatch through _on_message.
        """
        try:
            result = mam_message["mam_result"]
            forwarded = result["forwarded"]
            original_stanza = forwarded["stanza"]

            if not isinstance(original_stanza, Message):
                return

            # Dispatch through normal message handler — it will extract
            # from/to/type/body from the original stanza and route to
            # the gateway as if the message just arrived.
            await self._on_message(original_stanza)
        except Exception:
            logger.debug("xmpp: MAM dispatch failed", exc_info=True)

    # -----------------------------------------------------------------
    # Outbound
    # -----------------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        image_paths: Optional[List[str]] = None,
        voice_path: Optional[str] = None,
        document_path: Optional[str] = None,
        reply_to: Optional[str] = None,
        thread_id: Optional[str] = None,
        message_id: Optional[str] = None,
        disable_web_page_preview: bool = False,
        parse_mode: Optional[str] = None,
        formatting: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        if self.client is None or not getattr(self, "_running", True):
            return SendResult(success=False, error="xmpp not connected", retryable=True)

        # Extract thread_id from metadata if not provided directly
        if thread_id is None and metadata:
            thread_id = metadata.get("thread_id")

        logger.info("xmpp: send to=%s reply_to=%s mtype=%s",
                     chat_id, reply_to, "groupchat" if self._is_muc(chat_id) else "chat")

        # Forward legacy media params to dedicated methods
        if voice_path:
            return await self.send_voice(chat_id, voice_path, caption=content or None)
        if document_path:
            return await self.send_document(chat_id, document_path, caption=content or None)
        if image_paths:
            last_result: SendResult | None = None
            for path in image_paths:
                last_result = await self.send_image_file(chat_id, path, caption=content if path == image_paths[0] else None)
                if last_result is not None and not last_result.success:
                    return last_result
            return last_result or SendResult(success=True)

        mtype = "groupchat" if self._is_muc(chat_id) else "chat"

        # OMEMO encrypt when available
        if (
            "xep_0384" in self._registered_plugins
            and SLIXMPP_OMEMO_AVAILABLE
            and mtype == "chat"
        ):
            try:
                return await self._send_encrypted(
                    chat_id, content, thread_id=thread_id, reply_to=reply_to
                )
            except Exception as exc:
                logger.warning(
                    "OMEMO: encryption failed for %s (%s), sending plaintext", chat_id, exc
                )
                # fall through to plain send

        try:
            client_local = self.client  # type: ignore[assignment]
            chunks = self.truncate_message(content, self.MAX_MESSAGE_LENGTH)
            last_msg_id = None
            for i, chunk in enumerate(chunks):
                if i > 0:
                    await asyncio.sleep(0.3)  # avoid XMPP server rate-limit bursts
                # Only the first chunk gets the reply attachment;
                # subsequent chunks are standalone but share the thread.
                chunk_reply_to = reply_to if i == 0 else None
                if chunk_reply_to and "xep_0461" in self._registered_plugins:
                    stanza = client_local["xep_0461"].make_reply(
                        reply_to=JID(chat_id),
                        reply_id=chunk_reply_to,
                        mto=chat_id,
                        mbody=chunk,
                        mtype=mtype,
                    )
                else:
                    stanza = client_local.make_message(mto=chat_id, mbody=chunk, mtype=mtype)
                # Chat state (manually — make_reply's kwargs go to make_message()
                # which does not accept mchat_state)
                if "xep_0085" in self._registered_plugins:
                    stanza["chat_state"] = "active"
                # Attach XEP-0201 thread id for thread-aware clients
                if thread_id:
                    stanza["thread"] = thread_id
                # Attach XEP-0394 markup and XEP-0071 XHTML-IM if enabled.
                # to_xhtml_im() reads spans from markup['substanzas']; called
                # first so the Markup object is intact before its XML is moved
                # into the stanza by xml.append().
                if (self._xep_0394_enabled or self._xep_0071_enabled) and getattr(stanza, "xml", None) is not None:
                    try:
                        markup = self._build_markup(chunk)
                        if markup is not None:
                            # XHTML-IM first (reads from markup['substanzas'])
                            if self._xep_0071_enabled:
                                try:
                                    xhtml = client_local["xep_0394"].to_xhtml_im(chunk, markup)
                                    if xhtml is not None and getattr(xhtml, "xml", None) is not None:
                                        stanza.xml.append(xhtml.xml)
                                except Exception:
                                    logger.warning("xmpp: failed to attach XHTML-IM", exc_info=True)
                            # Markup second (no deepcopy needed — to_xhtml_im is done)
                            if self._xep_0394_enabled:
                                stanza.xml.append(markup.xml)
                    except Exception:
                        logger.debug("xmpp: failed to attach markup", exc_info=True)
                stanza.send()
                try:
                    last_msg_id = stanza["id"]
                except Exception:
                    pass
            return SendResult(success=True, message_id=last_msg_id)
        except Exception as exc:
            logger.exception("xmpp: send failed")
            return SendResult(success=False, error=str(exc), retryable=True)

    # -----------------------------------------------------------------
    # XEP-0308 Message Correction (edit_message)
    # -----------------------------------------------------------------

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        *,
        finalize: bool = False,
    ) -> SendResult:
        """Edit a previously sent message using XEP-0308 Last Message Correction."""
        if self.client is None or not getattr(self, "_running", True):
            return SendResult(success=False, error="xmpp not connected", retryable=True)

        if "xep_0308" not in self._registered_plugins:
            return SendResult(success=False, error="XEP-0308 not available")

        mtype = "groupchat" if self._is_muc(chat_id) else "chat"

        try:
            client_local = self.client
            stanza = client_local["xep_0308"].build_correction(
                id_to_replace=message_id,
                mto=JID(chat_id),
                mtype=mtype,
                mbody=content,
            )

            # Attach XHTML-IM (XEP-0071) for rich-text correction display.
            # Safe: if OMEMO encryption succeeds below, stanza is replaced with
            # a new encrypted object (XHTML-IM stripped). If encryption fails or
            # is not applicable, the XHTML-IM stays on the plaintext stanza.
            if self._xep_0071_enabled or self._xep_0394_enabled:
                try:
                    markup = self._build_markup(content)
                    if markup is not None:
                        if self._xep_0071_enabled:
                            xhtml = client_local["xep_0394"].to_xhtml_im(content, markup)
                            if xhtml is not None and getattr(xhtml, "xml", None) is not None:
                                stanza.xml.append(xhtml.xml)
                        if self._xep_0394_enabled:
                            stanza.xml.append(markup.xml)
                except Exception:
                    logger.debug("xmpp: failed to attach XHTML-IM to edit", exc_info=True)

            # OMEMO encrypt for 1:1 chats (same path as send())
            if (
                "xep_0384" in self._registered_plugins
                and SLIXMPP_OMEMO_AVAILABLE
                and mtype == "chat"
            ):
                try:
                    xep_0384 = client_local["xep_0384"]
                    recipient_jid = JID(chat_id)
                    encrypted, errors = await xep_0384.encrypt_message(stanza, {recipient_jid})
                    if errors:
                        logger.debug("OMEMO: edit encryption non-critical errors: %s", errors)
                    if encrypted is not None:
                        stanza = encrypted
                        # encrypt_message clear()s the stanza and rebuilds with only
                        # OMEMO elements — manually restore the <replace> element that
                        # was stripped during encryption (see slixmpp-omemo docstring:
                        # "other elements such as read markers are lost and have to be
                        # copied over manually").
                        stanza["replace"]["id"] = message_id
                        # XEP-0380 EME hint for compatibility
                        if "xep_0380" in self._registered_plugins:
                            try:
                                import oldmemo
                                ns = oldmemo.oldmemo.NAMESPACE
                                stanza["eme"]["namespace"] = ns
                                stanza["eme"]["name"] = client_local["xep_0380"].mechanisms[ns]
                            except Exception:
                                pass
                except Exception as exc:
                    logger.warning("OMEMO: edit encryption failed for %s (%s), sending plaintext", chat_id, exc)

            stanza.send()
            msg_id = None
            try:
                msg_id = stanza["id"]
            except Exception:
                pass
            return SendResult(success=True, message_id=msg_id)
        except Exception as exc:
            logger.exception("xmpp: edit_message failed")
            return SendResult(success=False, error=str(exc), retryable=True)

    # -----------------------------------------------------------------
    # XEP-0424 Message Retraction (delete_message)
    # -----------------------------------------------------------------

    async def delete_message(
        self,
        chat_id: str,
        message_id: str,
    ) -> bool:
        """Delete a previously sent message using XEP-0424 Message Retraction."""
        if self.client is None or not getattr(self, "_running", True):
            return False

        if "xep_0424" not in self._registered_plugins:
            return False

        mtype = "groupchat" if self._is_muc(chat_id) else "chat"

        # MUC retractions require the room-assigned <stanza-id> (XEP-0359 /
        # XEP-0424 §5.1), not the client-generated message ID. The adapter
        # does not currently capture reflected room stanzas, so returning
        # False for group chats until MUC stanza-id mapping is implemented.
        if mtype == "groupchat":
            return False

        try:
            self.client["xep_0424"].send_retraction(
                mto=JID(chat_id),
                id=message_id,
                mtype=mtype,
                include_fallback=False,
            )
            return True
        except Exception as exc:
            logger.warning("xmpp: delete_message failed: %s", exc)
            return False

    # -----------------------------------------------------------------
    # XEP-0394 Message Markup helper
    # -----------------------------------------------------------------

    def _build_markup(
        self,
        body: str,
    ) -> Any:
        """Return a slixmpp Markup element with basic formatting hints.

        Supports Markdown-lite in body:
        - **bold** or __bold__ → emphasis (strong)
        - *italic* or _italic_ → emphasis
        - ~strikethrough~ → deleted
        - `code` or ``code`` → code span
        - ```code block``` → block-code
        """
        try:
            from slixmpp.plugins.xep_0394.stanza import Markup, Span, BlockCode
        except ImportError:
            return None
        markup = Markup(parent=None)
        import re

        # Collect all spans with (start, end, type) to avoid overlaps
        raw_spans: List[Tuple[int, int, str]] = []

        # Bold: **text** or __text__
        for m in re.finditer(r'\*\*(.+?)\*\*|__(.+?)__', body, re.DOTALL):
            raw_spans.append((m.start(), m.end(), 'emphasis'))
        # Italic: *text* (single asterisk, not part of **)
        for m in re.finditer(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', body, re.DOTALL):
            raw_spans.append((m.start(), m.end(), 'emphasis'))
        # Italic: _text_ (single underscore, not part of __)
        for m in re.finditer(r'(?<!_)_(?!_)(.+?)(?<!_)_(?!_)', body, re.DOTALL):
            raw_spans.append((m.start(), m.end(), 'emphasis'))
        # Strikethrough: ~~text~~ or ~text~
        for m in re.finditer(r'~~(.+?)~~', body, re.DOTALL):
            raw_spans.append((m.start(), m.end(), 'deleted'))
        for m in re.finditer(r'(?<!~)~(?!~)(.+?)(?<!~)~(?!~)', body, re.DOTALL):
            raw_spans.append((m.start(), m.end(), 'deleted'))
        # Inline code: `text` or ``text`` (must not touch ``` fences)
        for m in re.finditer(r'(?<!`)`{1,2}([^`]+)`{1,2}(?!`)', body, re.DOTALL):
            raw_spans.append((m.start(), m.end(), 'code'))

        # Build spans via markup.append() so slixmpp's plugin registry
        # (iterables / substanzas) picks them up.  Raw xml.append() is not
        # enough — the internal plugin-tracking data structures stay stale.
        for start, end, type_ in raw_spans:
            span = Span()
            span["start"] = start
            span["end"] = end
            span["types"] = [type_]
            markup.append(span)

        # Block code: ```code block```
        for m in re.finditer(r'```(.+?)```', body, re.DOTALL):
            bcode = BlockCode()
            bcode["start"] = m.start()
            bcode["end"] = m.end()
            markup.append(bcode)

        return markup if raw_spans else None

    async def _send_encrypted(self, chat_id: str, content: str, *, thread_id: Optional[str] = None, reply_to: Optional[str] = None) -> SendResult:
        """Send an OMEMO-encrypted 1:1 chat message, split into chunks if needed."""
        if self.client is None:
            return SendResult(success=False, error="xmpp not connected", retryable=True)

        chunks = self.truncate_message(content, self.MAX_MESSAGE_LENGTH)
        client_local = self.client  # type: ignore[assignment]
        xep_0384 = client_local["xep_0384"]
        mtype = "chat"
        last_msg_id = None

        for i, chunk in enumerate(chunks):
            if i > 0:
                await asyncio.sleep(0.3)
            # Only the first chunk gets the reply attachment.
            chunk_reply_to = reply_to if i == 0 else None
            stanza = client_local.make_message(mto=chat_id, mtype=mtype)
            stanza["body"] = chunk
            if thread_id:
                stanza["thread"] = thread_id
            stanza.set_from(client_local.boundjid)

            recipient_jid = JID(chat_id)
            message, encryption_errors = await xep_0384.encrypt_message(stanza, {recipient_jid})

            if encryption_errors:
                logger.debug("OMEMO: encryption non-critical errors: %s", encryption_errors)

            if message is None:
                logger.warning("OMEMO: nothing to encrypt, falling back to plaintext")
                stanza = client_local.make_message(mto=chat_id, mbody=chunk, mtype=mtype)
                if "xep_0085" in self._registered_plugins:
                    stanza["chat_state"] = "active"
                # Attach XEP-0461 reply to fallback plaintext as well
                if chunk_reply_to and "xep_0461" in self._registered_plugins:
                    try:
                        stanza["reply"]["to"] = JID(chat_id)
                        stanza["reply"]["id"] = chunk_reply_to
                    except Exception:
                        logger.debug("xmpp: failed to attach reply to fallback stanza", exc_info=True)
                # Attach XHTML-IM on fallback (safe — already going plaintext)
                if self._xep_0071_enabled or self._xep_0394_enabled:
                    try:
                        markup = self._build_markup(chunk)
                        if markup is not None:
                            if self._xep_0071_enabled:
                                xhtml = client_local["xep_0394"].to_xhtml_im(chunk, markup)
                                if xhtml is not None and getattr(xhtml, "xml", None) is not None:
                                    stanza.xml.append(xhtml.xml)
                            if self._xep_0394_enabled:
                                stanza.xml.append(markup.xml)
                    except Exception:
                        logger.debug("xmpp: failed to attach XHTML-IM on fallback", exc_info=True)
                stanza.send()
                try:
                    last_msg_id = stanza["id"]
                except Exception:
                    pass
                continue

            # Explicit Message Encryption (XEP-0380) hint for compatibility.
            if "xep_0380" in self._registered_plugins:
                try:
                    import oldmemo
                    ns = oldmemo.oldmemo.NAMESPACE
                    message["eme"]["namespace"] = ns
                    message["eme"]["name"] = client_local["xep_0380"].mechanisms[ns]
                except Exception:
                    pass

            # Attach XEP-0085 chat state after encryption
            if "xep_0085" in self._registered_plugins:
                try:
                    message["chat_state"] = "active"
                except Exception:
                    pass
            # Attach XEP-0461 reply after encryption (first chunk only)
            if chunk_reply_to and "xep_0461" in self._registered_plugins:
                try:
                    message["reply"]["to"] = JID(chat_id)
                    message["reply"]["id"] = chunk_reply_to
                except Exception:
                    logger.debug("xmpp: failed to attach reply to encrypted message", exc_info=True)
            message.send()
            try:
                last_msg_id = message["id"]
            except Exception:
                pass

        return SendResult(success=True, message_id=last_msg_id)

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        if self.client is None or "xep_0085" not in self._registered_plugins or not getattr(self, "_running", True):
            return
        mtype = "groupchat" if self._is_muc(chat_id) else "chat"
        try:
            msg = self.client.make_message(mto=chat_id, mtype=mtype)
            msg["chat_state"] = "composing"
            msg["no-store"] = True
            msg.send()
        except Exception:
            logger.debug("xmpp: send_typing failed", exc_info=True)

    async def stop_typing(self, chat_id: str) -> None:
        if self.client is None or "xep_0085" not in self._registered_plugins or not getattr(self, "_running", True):
            return
        mtype = "groupchat" if self._is_muc(chat_id) else "chat"
        try:
            msg = self.client.make_message(mto=chat_id, mtype=mtype)
            msg["chat_state"] = "active"
            msg["no-store"] = True
            msg.send()
        except Exception:
            logger.debug("xmpp: stop_typing failed", exc_info=True)

    # ── Inbound reaction handler (slixmpp 'reactions' event) ─────
    def _on_reaction(self, message) -> None:
        """Handle inbound XEP-0444 reactions via slixmpp's 'reactions' event.

        The XEP-0444 plugin fires this event with a fully parsed Message
        stanza.  We extract the target message ID and reaction value,
        then check if it resolves a pending exec-approval or clarify prompt.
        """
        try:
            from_full = str(message["from"])
            from_bare = self._bare(from_full)
        except Exception:
            return

        # ── Self-reaction guards ────────────────────────────────────
        # DM: compare bare JID directly.
        if from_bare == self._self_bare:
            return
        # MUC: compare resource (nick) against bot's nick for this room.
        # In MUC the message["from"] is room@conf/nick — not the bot's
        # own JID — so the DM guard above doesn't catch it.
        if from_bare in self._known_mucs:
            resource = from_full.split("/", 1)[1] if "/" in from_full else ""
            our_nick = self._muc_nick_for_room(from_bare)
            if resource and our_nick and resource == our_nick:
                return  # own seed reaction echoed back by MUC

        # ── Determine auth context ──────────────────────────────────
        # In MUC, from_bare is the room JID.  Try to extract the real
        # sender JID from the MUC <x> element for user-level auth.
        # Fall back to room-level auth if the MUC is anonymous.
        is_muc = from_bare in self._known_mucs
        if is_muc:
            real_jid = self._muc_real_jid(message)
            auth_user = real_jid if real_jid else from_bare
            auth_chat_type = "group"
        else:
            auth_user = from_bare
            auth_chat_type = "dm"

        try:
            reactions_el = message["reactions"]
            if reactions_el is None or reactions_el.xml is None:
                return
            target_id = reactions_el["id"]
            for rxn in reactions_el:
                try:
                    rxn_val = str(rxn["value"])
                    if not rxn_val:
                        continue
                except Exception:
                    continue

                logger.debug(
                    "xmpp: inbound reaction %s on %s from %s",
                    rxn_val, target_id, from_bare,
                )

                # ── Check approval map ──
                prompt = self._approval_prompts_by_event.get(target_id)
                if prompt and not prompt.get("resolved"):
                    choice = self._approval_reaction_map.get(rxn_val)
                    if choice:
                        if self._is_authorized(
                            chat_type=auth_chat_type, chat_id=from_bare,
                            user_jid=auth_user,
                        ):
                            asyncio.ensure_future(
                                self._resolve_approval_reaction(
                                    from_bare, target_id, prompt, choice
                                )
                            )
                    continue  # handled — skip clarify check

                # ── Check clarify map ──
                clarify_prompt = self._clarify_prompts_by_event.get(target_id)
                if clarify_prompt and not clarify_prompt.get("resolved"):
                    if self._is_authorized(
                        chat_type=auth_chat_type, chat_id=from_bare,
                        user_jid=auth_user,
                    ):
                        asyncio.ensure_future(
                            self._resolve_clarify_reaction(
                                from_bare, target_id, clarify_prompt, rxn_val
                            )
                        )
                    continue
        except Exception:
            logger.debug("xmpp: _on_reaction error", exc_info=True)

    async def _resolve_approval_reaction(
        self, from_bare: str, target_id: str, prompt: dict, choice: str
    ) -> None:
        """Resolve a pending exec approval from a reaction."""
        try:
            from tools.approval import resolve_gateway_approval
            count = resolve_gateway_approval(prompt["session_key"], choice)
            if count:
                prompt["resolved"] = True
                self._approval_prompts_by_event.pop(target_id, None)
                self._approval_prompt_by_session.pop(prompt["session_key"], None)
                logger.debug(
                    "xmpp: reaction resolved %d approval(s) for session %s (choice=%s, user=%s)",
                    count, prompt["session_key"], choice, from_bare,
                )
                await self._cleanup_approval_reactions(prompt["chat_id"], prompt)
        except Exception as exc:
            logger.error(
                "xmpp: failed to resolve gateway approval from reaction: %s", exc
            )

    async def _resolve_clarify_reaction(
        self, from_bare: str, target_id: str, prompt: dict, rxn_val: str
    ) -> None:
        """Resolve a pending clarify prompt from a reaction."""
        choice_map = prompt.get("choice_map", {})
        if rxn_val not in choice_map:
            logger.debug(
                "xmpp: clarify reaction %s not in choice_map for %s",
                rxn_val, target_id,
            )
            return

        idx = choice_map[rxn_val]
        try:
            from tools.clarify_gateway import (
                resolve_gateway_clarify,
                mark_awaiting_text,
            )

            if idx == -1:  # ✏️ Other → text-capture mode
                mark_awaiting_text(prompt["clarify_id"])
                logger.debug(
                    "xmpp: clarify 'Other' selected — awaiting text from %s",
                    from_bare,
                )
                # Still mark resolved so further reactions are no-ops
                prompt["resolved"] = True
                self._clarify_prompts_by_event.pop(target_id, None)
                self._clarify_prompt_by_session.pop(prompt["session_key"], None)
                await self._cleanup_clarify_reactions(prompt["chat_id"], prompt)
                return

            # Numeric choice → resolve immediately
            choices = prompt.get("choices", [])
            resolved_text = choices[idx] if 0 <= idx < len(choices) else f"choice {idx + 1}"
            ok = resolve_gateway_clarify(prompt["clarify_id"], resolved_text)
            if ok:
                prompt["resolved"] = True
                self._clarify_prompts_by_event.pop(target_id, None)
                self._clarify_prompt_by_session.pop(prompt["session_key"], None)
                logger.debug(
                    "xmpp: clarify reaction resolved (id=%s, choice=%r, user=%s)",
                    prompt["clarify_id"], resolved_text, from_bare,
                )
                await self._cleanup_clarify_reactions(prompt["chat_id"], prompt)
            else:
                logger.warning(
                    "xmpp: resolve_gateway_clarify returned False (id=%s)",
                    prompt["clarify_id"],
                )
        except Exception as exc:
            logger.error(
                "xmpp: failed to resolve clarify from reaction: %s", exc
            )

    # ── Reaction-based exec approval ──────────────────────────
    async def send_exec_approval(
        self,
        chat_id: str,
        command: str,
        session_key: str,
        description: str = "dangerous command",
        metadata: Optional[dict] = None,
    ) -> SendResult:
        """Send a reaction-based exec approval prompt for XMPP.
        
        Posts a warning message with ✅/❎ reactions. When the user
        clicks a reaction, the inbound reaction handler resolves
        the pending gateway approval.
        """
        if not self.client or "xep_0444" not in self._registered_plugins:
            return SendResult(success=False, error="Not connected or reactions unavailable")

        cmd_preview = command[:2000] + "..." if len(command) > 2000 else command
        text = (
            "⚠️ **Dangerous command requires approval**\n"
            f"```\n{cmd_preview}\n```\n"
            f"Reason: {description}\n\n"
            "Reply `/approve` to execute, `/approve session` to approve this "
            "pattern for the session, `/approve always` to approve permanently, "
            "or `/deny` to cancel.\n\n"
            "You can also tap the reaction to approve:\n"
            "✅ = /approve\n"
            "❎ = /deny"
        )

        result = await self.send(chat_id, text, metadata=metadata)
        if not result.success or not result.message_id:
            return result

        prompt = {
            "session_key": session_key,
            "chat_id": chat_id,
            "message_id": result.message_id,
            "resolved": False,
            "bot_reaction_message_ids": {},
        }
        old_event = self._approval_prompt_by_session.get(session_key)
        if old_event:
            self._approval_prompts_by_event.pop(old_event, None)
        self._approval_prompts_by_event[result.message_id] = prompt
        self._approval_prompt_by_session[session_key] = result.message_id

        # Send BOTH reactions in ONE message — set_reactions REPLACES,
        # not appends.  Sending separately would leave only the last one.
        try:
            mtype = "groupchat" if self._is_muc(chat_id) else "chat"
            msg = self.client.make_message(mto=JID(chat_id), mtype=mtype)
            self.client["xep_0444"].set_reactions(
                msg, result.message_id, ["✅", "❎"]
            )
            msg.enable("store")
            msg.send()
        except Exception as exc:
            logger.debug(
                "xmpp: failed to add approval reactions: %s", exc
            )

        return result

    async def _cleanup_approval_reactions(
        self, chat_id: str, prompt: dict
    ) -> None:
        """Remove bot's seed reactions after approval is resolved."""
        if not self.client or "xep_0444" not in self._registered_plugins:
            return
        try:
            mtype = "groupchat" if self._is_muc(chat_id) else "chat"
            msg = self.client.make_message(mto=JID(chat_id), mtype=mtype)
            self.client["xep_0444"].set_reactions(
                msg, prompt["message_id"], []
            )
            msg.enable("store")
            msg.send()
            logger.debug(
                "xmpp: cleared bot approval reactions on %s",
                prompt["message_id"],
            )
        except Exception as exc:
            logger.debug(
                "xmpp: failed to clear bot approval reactions: %s", exc
            )

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        return await self._upload_and_send(chat_id, image_path, caption)

    async def send_document(
        self,
        chat_id: str,
        path: str,
        caption: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        return await self._upload_and_send(chat_id, path, caption)

    async def _decrypt_aesgcm(self, aesgcm_url: str) -> Tuple[Optional[str], Optional[str]]:
        """Download an encrypted file (XEP-0454), decrypt it, cache the plaintext.

        Returns (cached_path, mime_type) or (None, None) on failure.
        """
        match = _AESGCM_URL_RE.match(aesgcm_url)
        if not match:
            logger.warning("xmpp: invalid aesgcm:// URL: %s", aesgcm_url)
            return None, None

        https_path = match.group(1)
        fragment = match.group(2)          # 88 hex chars: IV(24) + key(64)
        https_url = "https://" + https_path
        mime = _guess_mime_from_url(https_url)
        ext = _mime_to_ext(mime)

        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                response = await client.get(
                    https_url,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; HermesAgent/1.0)", "Accept": "*/*"},
                )
                response.raise_for_status()
                encrypted_data = response.content
        except Exception as exc:
            logger.warning("xmpp: failed to download aesgcm:// file %s: %s", https_url, exc)
            return None, None

        try:
            plaintext = self.client["xep_0454"].decrypt(BytesIO(encrypted_data), fragment)
        except Exception as exc:
            logger.warning("xmpp: aesgcm:// decryption failed: %s", exc)
            return None, None

        try:
            if mime.startswith("audio/"):
                cached = cache_audio_from_bytes(plaintext, ext)
            else:
                cached = cache_image_from_bytes(plaintext, ext)
            logger.debug("xmpp: decrypted aesgcm:// media (%s, %d bytes) → %s", mime, len(plaintext), cached)
            return cached, mime
        except Exception as exc:
            logger.warning("xmpp: aesgcm:// cache failed: %s", exc)
            return None, None

    async def send_video(
        self,
        chat_id: str,
        path: str,
        caption: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        return await self._upload_and_send(chat_id, path, caption)

    async def _upload_and_send(
        self, chat_id: str, path: str, caption: Optional[str]
    ) -> SendResult:
        if self.client is None or not getattr(self, "_running", True):
            return SendResult(success=False, error="xmpp not connected", retryable=True)
        if "xep_0363" not in self._registered_plugins:
            return SendResult(
                success=False,
                error="xmpp HTTP File Upload (XEP-0363) not available",
                retryable=False,
            )

        use_omemo_media = self._use_omemo_media(chat_id)

        content_type, _ = mimetypes.guess_type(path)
        try:
            if use_omemo_media:
                # XEP-0454: encrypt file before upload, get aesgcm:// URL
                url = await self.client["xep_0454"].upload_file(
                    filename=Path(path),
                    content_type=content_type,
                )
            else:
                # Plain HTTP Upload (MUC or no OMEMO)
                upload_kwargs: Dict[str, Any] = {
                    "filename": Path(path).name,
                    "input_file": path,
                }
                if content_type:
                    upload_kwargs["content_type"] = content_type
                upload = self.client["xep_0363"].upload_file
                try:
                    url = await upload(**upload_kwargs)
                except TypeError:
                    upload_kwargs.pop("content_type", None)
                    url = await upload(**upload_kwargs)
        except Exception as exc:
            logger.exception("xmpp: HTTP upload (XEP-0363) failed")
            return SendResult(success=False, error=str(exc), retryable=True)

        body = url if not caption else f"{caption}\n{url}"

        # When using OMEMO media sharing, route through self.send() so the
        # aesgcm:// URL (with key) goes through OMEMO body encryption.
        if use_omemo_media:
            return await self.send(chat_id, body)
        else:
            mtype = "groupchat" if self._is_muc(chat_id) else "chat"
            try:
                stanza = self.client.send_message(mto=chat_id, mbody=body, mtype=mtype)
                msg_id = None
                try:
                    msg_id = stanza["id"]
                except Exception:
                    pass
                return SendResult(success=True, message_id=msg_id, raw_response=stanza)
            except Exception as exc:
                return SendResult(success=False, error=str(exc), retryable=True)

    # -----------------------------------------------------------------
    # Lifecycle hooks (reactions)
    # -----------------------------------------------------------------

    def _reactions_allowed(self, event: MessageEvent) -> bool:
        if not self._reactions_enabled:
            return False
        sender = getattr(getattr(event, "source", None), "user_id", None)
        if sender and not self.allow_all_users and self.allowed_users:
            if self._bare(sender) not in self.allowed_users:
                return False
        return True

    def _extract_reaction_target(self, event: MessageEvent) -> Optional[str]:
        return getattr(event, "message_id", None) or None

    def _send_reaction(self, chat_id: str, target_id: str, reactions: list[str]) -> None:
        """Send a reaction message with the correct type per XEP-0444 §4.

        XEP-0444 says message type SHOULD be 'chat' or 'groupchat'.
        Gajim enforces this strictly and rejects type='normal' reactions.
        """
        if self.client is None or "xep_0444" not in self._registered_plugins:
            return
        mtype = "groupchat" if self._is_muc(chat_id) else "chat"
        msg = self.client.make_message(mto=chat_id, mtype=mtype)
        self.client["xep_0444"].set_reactions(msg, target_id, reactions)
        msg.enable("store")
        msg.send()

    async def on_processing_start(self, event: MessageEvent) -> None:
        if not self._reactions_allowed(event):
            return
        target_id = self._extract_reaction_target(event)
        if target_id and self.client is not None and "xep_0444" in self._registered_plugins:
            try:
                self._send_reaction(event.source.chat_id, target_id, ["👀"])
                self._pending_reactions[target_id] = event
            except Exception:
                logger.debug("xmpp: failed to send 👀 reaction", exc_info=True)

    async def on_processing_complete(
        self, event: MessageEvent, outcome: ProcessingOutcome
    ) -> None:
        if not self._reactions_allowed(event):
            return
        if outcome == ProcessingOutcome.CANCELLED:
            return
        target_id = self._extract_reaction_target(event)
        if not target_id or self.client is None or "xep_0444" not in self._registered_plugins:
            return
        try:
            # Remove in-progress reaction
            self._send_reaction(event.source.chat_id, target_id, [])
        except Exception:
            logger.debug("xmpp: failed to remove 👀 reaction", exc_info=True)
        try:
            if outcome == ProcessingOutcome.SUCCESS:
                self._send_reaction(event.source.chat_id, target_id, ["✅"])
            elif outcome == ProcessingOutcome.FAILURE:
                self._send_reaction(event.source.chat_id, target_id, ["❌"])
        except Exception:
            logger.debug("xmpp: failed to send final reaction", exc_info=True)
        finally:
            self._pending_reactions.pop(target_id, None)

    # -----------------------------------------------------------------
    # Clarify via XEP-0004 Data Forms
    # -----------------------------------------------------------------

    async def send_clarify(
        self,
        chat_id: str,
        question: str,
        choices: Optional[list],
        clarify_id: str,
        session_key: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Reaction-based clarify for XMPP (XEP-0444).

        Multi-choice: sends question text with numbered options, then
        adds numbered emoji reactions (1️⃣–4️⃣ + ✏️ Other).  The user
        taps a reaction; _on_reaction resolves via resolve_gateway_clarify.

        Open-ended (no choices): falls through to text intercept via
        self.send() → mark_awaiting_text.
        """
        if self.client is None:
            return SendResult(success=False, error="xmpp not connected", retryable=True)

        # Build the clarify text for body
        if choices:
            lines = [f"❓ {question}", ""]
            for i, choice in enumerate(choices, start=1):
                lines.append(f"  {i}. {choice}")
            lines.append("")
            lines.append("Tap a reaction to select, or react ✏️ to type your own answer.")
            text = "\n".join(lines)
        else:
            text = f"❓ {question}"

        # Always route through self.send() — handles OMEMO encryption
        result = await self.send(chat_id=chat_id, content=text, metadata=metadata)
        if not result.success or not result.message_id:
            # Couldn't deliver prompt — clean up
            from tools.clarify_gateway import clear_session as _clear
            _clear(session_key or "")
            return result

        # Open-ended (no choices) → text-intercept mode
        if not choices:
            from tools.clarify_gateway import mark_awaiting_text
            mark_awaiting_text(clarify_id)
            return result

        # Multi-choice → add numbered emoji reactions
        if "xep_0444" not in self._registered_plugins:
            # Reactions unavailable → fall back to text-intercept
            from tools.clarify_gateway import mark_awaiting_text
            mark_awaiting_text(clarify_id)
            return result

        # Build reaction map: emoji → choice index
        # Use digit emoji keycaps: 1️⃣ through 4️⃣, then ✏️ for Other
        _NUM_EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣"]
        reaction_emojis = []
        choice_map: Dict[str, int] = {}  # emoji → choice index
        for i in range(min(len(choices), len(_NUM_EMOJIS))):
            emoji = _NUM_EMOJIS[i]
            reaction_emojis.append(emoji)
            choice_map[emoji] = i
        # ✏️ for "Other (type your own answer)"
        reaction_emojis.append("✏️")
        choice_map["✏️"] = -1  # sentinel for Other

        # Store prompt for reaction intercept
        prompt = {
            "clarify_id": clarify_id,
            "session_key": session_key,
            "chat_id": chat_id,
            "message_id": result.message_id,
            "choices": list(choices),
            "choice_map": choice_map,
            "resolved": False,
        }
        old_event = self._clarify_prompt_by_session.get(session_key)
        if old_event:
            self._clarify_prompts_by_event.pop(old_event, None)
        self._clarify_prompts_by_event[result.message_id] = prompt
        self._clarify_prompt_by_session[session_key] = result.message_id

        # Send ALL reactions in ONE message — set_reactions REPLACES
        try:
            mtype = "groupchat" if self._is_muc(chat_id) else "chat"
            msg = self.client.make_message(mto=JID(chat_id), mtype=mtype)
            self.client["xep_0444"].set_reactions(
                msg, result.message_id, reaction_emojis
            )
            msg.enable("store")
            msg.send()
        except Exception as exc:
            logger.warning("xmpp: failed to add clarify reactions: %s", exc)
            # Fall back to text-intercept
            from tools.clarify_gateway import mark_awaiting_text
            mark_awaiting_text(clarify_id)

        return result

    async def _cleanup_clarify_reactions(
        self, chat_id: str, prompt: dict
    ) -> None:
        """Remove bot's seed reactions after clarify is resolved."""
        if not self.client or "xep_0444" not in self._registered_plugins:
            return
        try:
            mtype = "groupchat" if self._is_muc(chat_id) else "chat"
            msg = self.client.make_message(mto=JID(chat_id), mtype=mtype)
            self.client["xep_0444"].set_reactions(
                msg, prompt["message_id"], []
            )
            msg.enable("store")
            msg.send()
            logger.debug(
                "xmpp: cleared bot clarify reactions on %s",
                prompt["message_id"],
            )
        except Exception as exc:
            logger.debug(
                "xmpp: failed to clear bot clarify reactions: %s", exc
            )

    # -----------------------------------------------------------------
    # Ad-Hoc Commands (XEP-0050)
    # -----------------------------------------------------------------

    async def _setup_adhoc_commands(self) -> None:
        if self.client is None or "xep_0050" not in self._registered_plugins:
            return
        try:
            xep_0050 = self.client["xep_0050"]
            xep_0050.add_command(
                jid=self.client.boundjid,
                node="hermes",
                name="Hermes Agent Commands",
                handler=self._adhoc_hermes_handler,
            )
            logger.info(
                "xmpp: ad-hoc command registered — node=hermes jid=%s",
                self.client.boundjid,
            )
        except Exception:
            logger.exception("xmpp: failed to register ad-hoc commands")

    # ── Stage 1: show the command selection form ─────────────────────
    async def _adhoc_hermes_handler(
        self, iq: Any, session: Dict[str, Any]
    ) -> Dict[str, Any]:
        # ── Access control ──────────────────────────────────────────
        # slixmpp XEP-0050 does NOT filter by sender — the spec
        # delegates access control to the handler.
        from_jid = self._bare(str(iq.get_from())) if iq.get_from() else None
        if from_jid and not self._is_authorized(
            chat_type="dm", chat_id=from_jid, user_jid=from_jid
        ):
            logger.warning(
                "xmpp: adhoc command denied for unauthorized %s", from_jid
            )
            session["notes"] = [("error", "Access denied")]
            return session

        client = self.client
        if client is None or "xep_0004" not in self._registered_plugins:
            session["notes"] = [("error", "Data forms not available")]
            return session
        try:
            form = client["xep_0004"].make_form(
                ftype="form",
                title="Hermes Commands",
                instructions="Choose a command to execute.",
            )
            form.add_field(
                var="command",
                ftype="list-single",
                label="Command",
                options=[
                    {"label": "Status", "value": "status"},
                    {"label": "Help", "value": "help"},
                    {"label": "Ping", "value": "ping"},
                ],
            )
            session["payload"] = form
            session["has_next"] = True
            session["next"] = self._adhoc_hermes_execute
            session["allow_complete"] = False
            return session
        except Exception:
            logger.exception("xmpp: adhoc stage-1 failed")
            session["notes"] = [("error", "Failed to build command list")]
            return session

    # ── Stage 2: execute the selected command ────────────────────────
    async def _adhoc_hermes_execute(
        self, form_result: Any, session: Dict[str, Any]
    ) -> Dict[str, Any]:
        client = self.client
        try:
            values = form_result.get_values() if hasattr(form_result, "get_values") else {}
            cmd = values.get("command", "")
        except Exception:
            cmd = ""

        if cmd == "status":
            note_text = self._build_status_text()
        elif cmd == "help":
            note_text = self._build_help_text()
        elif cmd == "ping":
            note_text = f"🏓 Pong! {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"
        else:
            note_text = f"Unknown command: {cmd}"

        session["payload"] = None
        session["notes"] = [("info", note_text)]
        session["has_next"] = False
        session["next"] = None
        return session

    def _build_status_text(self) -> str:
        parts = ["🤖 Hermes XMPP Adapter Status", ""]
        parts.append(f"Connected: {'✅' if self._running else '❌'}")
        if self.client and self.client.boundjid:
            parts.append(f"JID: {self.client.boundjid.bare}")
        parts.append(f"OMEMO: {'✅ enabled' if self._omemo_enabled else '❌ disabled'}")
        parts.append(f"MAM replay: {'✅' if self._mam_enabled else '❌'}")
        parts.append(
            f"Markup: XEP-0394={'✅' if self._xep_0394_enabled else '❌'} "
            f"XEP-0071={'✅' if self._xep_0071_enabled else '❌'}"
        )
        if self.muc_rooms:
            parts.append("")
            parts.append("MUC rooms:")
            for r in self.muc_rooms:
                parts.append(f"  - {r.room} (nick: {r.nick or self.muc_nick})")
        return "\n".join(parts)

    def _build_help_text(self) -> str:
        return (
            "📋 Available commands:\n\n"
            "  /stop     — Stop the current agent generation\n"
            "  /new      — Start a new conversation\n"
            "  /approve  — Approve a dangerous command\n"
            "  /deny     — Deny a dangerous command\n\n"
            "Ad-hoc commands (XEP-0050):\n"
            "  Status — Show adapter status and connection info\n"
            "  Help   — Show this help text\n"
            "  Ping   — Connection latency check\n\n"
            "For more: https://hermes-agent.nousresearch.com/docs"
        )

    # -----------------------------------------------------------------
    # Voice messages via XEP-0447 SFS
    # -----------------------------------------------------------------

    async def send_voice(
        self,
        chat_id: str,
        path: str,
        **kwargs,
    ) -> SendResult:
        if self.client is None or not getattr(self, "_running", True):
            return SendResult(success=False, error="xmpp not connected", retryable=True)

        # XEP-0454 OMEMO Media Sharing: for encrypted DMs, encrypt + upload
        # and send the aesgcm:// URL through the OMEMO body path.
        use_omemo_media = self._use_omemo_media(chat_id)
        if use_omemo_media:
            # Fall back to _upload_and_send which handles 0454 encryption
            return await self._upload_and_send(chat_id, path, caption="[Voice message]")

        if "xep_0447" not in self._registered_plugins or "xep_0363" not in self._registered_plugins:
            return await self._upload_and_send(chat_id, path, caption=None)

        content_type, _ = mimetypes.guess_type(path)
        upload_kwargs: Dict[str, Any] = {
            "filename": Path(path).name,
            "input_file": path,
        }
        if content_type:
            upload_kwargs["content_type"] = content_type
        try:
            upload = self.client["xep_0363"].upload_file
            try:
                url = await upload(**upload_kwargs)
            except TypeError:
                upload_kwargs.pop("content_type", None)
                url = await upload(**upload_kwargs)
        except Exception as exc:
            logger.exception("xmpp: HTTP upload for voice failed")
            return SendResult(success=False, error=str(exc), retryable=True)

        mtype = "groupchat" if self._is_muc(chat_id) else "chat"
        try:
            sfs = self.client["xep_0447"].get_sfs(
                path=Path(path),
                uris=[url],
                media_type=content_type or "audio/ogg",
                desc="Voice message",
                disposition="inline",
            )
            msg = self.client.make_message(mto=chat_id, mtype=mtype)
            msg["body"] = "[Voice message]"
            msg["sfs"] = sfs
            msg.send()

            msg_id = None
            try:
                msg_id = msg["id"]
            except Exception:
                pass
            return SendResult(success=True, message_id=msg_id, raw_response=msg)
        except Exception as exc:
            logger.exception("xmpp: send_voice via SFS failed")
            return SendResult(success=False, error=str(exc), retryable=True)

    # -----------------------------------------------------------------
    # Misc
    # -----------------------------------------------------------------

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        chat_type = "group" if self._is_muc(chat_id) else "dm"
        return {"chat_id": chat_id, "type": chat_type, "name": chat_id}

    def _is_muc(self, chat_id: str) -> bool:
        return chat_id in self._known_mucs

    def _use_omemo_media(self, chat_id: str) -> bool:
        """Whether to use XEP-0454 OMEMO Media Sharing for file uploads."""
        return (
            not self._is_muc(chat_id)
            and "xep_0454" in self._registered_plugins
            and "xep_0384" in self._registered_plugins
            and SLIXMPP_OMEMO_AVAILABLE
        )

    @staticmethod
    def _bare(jid: str) -> str:
        return jid.split("/", 1)[0] if "/" in jid else jid

    @staticmethod
    def _default_nick() -> str:
        return "hermes"


# ---------------------------------------------------------------------
# Standalone helper
# ---------------------------------------------------------------------

async def send_xmpp_message(
    pconfig: PlatformConfig,
    chat_id: str,
    message: str,
    *,
    thread_id: str | None = None,
    media_files: list[str] | None = None,
    force_document: bool = False,
) -> Dict[str, Any]:
    """One-shot send used by cron jobs and the send_message tool."""
    adapter = XmppAdapter(pconfig)
    if not check_xmpp_requirements():
        return {"success": False, "error": "slixmpp not installed"}
    try:
        ok = await adapter.connect()
        if not ok:
            return {"success": False, "error": adapter.fatal_error_message() or "connect failed"}
        if adapter._session_ready is not None:
            try:
                await asyncio.wait_for(adapter._session_ready.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                logger.warning("xmpp: session_start did not fire within 10s; sending anyway")
        last_result = None
        if message:
            last_result = await adapter.send(chat_id=chat_id, content=message)
            if not last_result.success:
                return {"success": False, "error": last_result.error}
        for media_path in media_files or []:
            last_result = await adapter.send_document(chat_id=chat_id, path=media_path)
            if not last_result.success:
                return {"success": False, "error": last_result.error}
        return {
            "success": True,
            "platform": "xmpp",
            "chat_id": chat_id,
            "message_id": getattr(last_result, "message_id", None),
        }
    finally:
        await adapter.disconnect()


# ---------------------------------------------------------------------
# Registration helpers
# ---------------------------------------------------------------------

def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _csv(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return ",".join(str(v).strip() for v in value if str(v).strip())
    return str(value).strip()


def _env_enablement() -> Optional[dict[str, Any]]:
    jid = os.getenv("XMPP_JID", "").strip()
    password = os.getenv("XMPP_PASSWORD", "").strip()
    if not (jid and password):
        return None
    data: dict[str, Any] = {"jid": jid, "password": password}
    for env, key in (
        ("XMPP_HOST", "host"),
        ("XMPP_PORT", "port"),
        ("XMPP_MUC_ROOMS", "muc_rooms"),
        ("XMPP_MUC_NICK", "muc_nick"),
        ("XMPP_ALLOWED_USERS", "allowed_users"),
        ("XMPP_ALLOW_ALL_USERS", "allow_all_users"),
        ("XMPP_XEP_0394_ENABLED", "xep_0394_enabled"),
        ("XMPP_XEP_0071_ENABLED", "xep_0071_enabled"),
    ):
        value = os.getenv(env, "").strip()
        if value:
            data[key] = value
    return data


def _apply_yaml_config(yaml_cfg: dict, xmpp_cfg: dict) -> Optional[dict[str, Any]]:
    raw = dict(xmpp_cfg or {})
    extra = dict(raw.get("extra") or {})
    for key in (
        "jid", "password", "host", "port", "muc_rooms", "muc_nick",
        "allowed_users", "allow_all_users", "require_mention",
        "xep_0394_enabled", "xep_0071_enabled",
    ):
        if key in raw and key not in extra:
            extra[key] = raw[key]

    # Merge omemo sub-config if present
    omemo_extra = {}
    if "omemo" in raw:
        omemo_raw = raw["omemo"]
        if isinstance(omemo_raw, dict):
            omemo_extra["omemo"] = omemo_raw
    if "omemo_enabled" in raw:
        omemo_extra.setdefault("omemo", {})["enabled"] = raw["omemo_enabled"]
    if "omemo_storage_path" in raw:
        omemo_extra.setdefault("omemo", {})["storage_path"] = raw["omemo_storage_path"]
    if omemo_extra:
        extra.update(omemo_extra)

    env_map = {
        "jid": "XMPP_JID",
        "password": "XMPP_PASSWORD",
        "host": "XMPP_HOST",
        "port": "XMPP_PORT",
        "muc_rooms": "XMPP_MUC_ROOMS",
        "muc_nick": "XMPP_MUC_NICK",
        "allowed_users": "XMPP_ALLOWED_USERS",
        "allow_all_users": "XMPP_ALLOW_ALL_USERS",
        "require_mention": "XMPP_REQUIRE_MENTION",
        "home_channel": "XMPP_HOME_CHANNEL",
    }
    for key, env in env_map.items():
        value = raw.get(key, extra.get(key))
        if key == "home_channel" and isinstance(value, dict):
            value = value.get("chat_id")
        if value is None or os.getenv(env):
            continue
        os.environ[env] = _csv(value)
    return extra or None


def validate_config(config: PlatformConfig) -> bool:
    extra = getattr(config, "extra", {}) or {}
    return bool((extra.get("jid") or os.getenv("XMPP_JID")) and (extra.get("password") or os.getenv("XMPP_PASSWORD")))


def is_connected(config: PlatformConfig) -> bool:
    return validate_config(config)


def _build_adapter(config: PlatformConfig) -> XmppAdapter:
    return XmppAdapter(config)


def register(ctx) -> None:
    ctx.register_platform(
        name="xmpp",
        label="XMPP/Jabber",
        adapter_factory=_build_adapter,
        check_fn=check_xmpp_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["XMPP_JID", "XMPP_PASSWORD"],
        install_hint="Install dependencies with: uv pip install slixmpp==1.15.0 aiohttp==3.13.4",
        env_enablement_fn=_env_enablement,
        apply_yaml_config_fn=_apply_yaml_config,
        cron_deliver_env_var="XMPP_HOME_CHANNEL",
        standalone_sender_fn=send_xmpp_message,
        allowed_users_env="XMPP_ALLOWED_USERS",
        allow_all_env="XMPP_ALLOW_ALL_USERS",
        max_message_length=10000,
        emoji="💬",
        pii_safe=False,
        allow_update_command=True,
        platform_hint=(
            "You are communicating over XMPP/Jabber. Use plain text by default. "
            "XMPP clients vary in markdown rendering, so avoid heavy markdown tables. "
            "Keep replies reasonably concise unless the user asks for detail."
        ),
    )
