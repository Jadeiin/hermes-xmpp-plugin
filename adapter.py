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
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from slixmpp.clientxmpp import ClientXMPP
from slixmpp.jid import JID  # type: ignore[import-untyped]
from slixmpp.plugins import register_plugin  # type: ignore[import-untyped]
from slixmpp.stanza import Message  # type: ignore[import-untyped]

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
)

logger = logging.getLogger(__name__)


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

    # -----------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------

    async def connect(self) -> bool:
        # Reset reconnection state for a fresh connection attempt
        self._reconnecting = False

        client = ClientXMPP(self.jid, self._password)
        # Plugins - core
        for plugin in ("xep_0030", "xep_0045", "xep_0066", "xep_0085", "xep_0199", "xep_0363"):
            try:
                client.register_plugin(plugin)
                self._registered_plugins.add(plugin)
            except Exception:
                logger.warning("xmpp: failed to register slixmpp plugin %s", plugin)

        # Plugins - first-class features (XEP-0394, 0444, 0004, 0050, 0461, 0447)
        # Lazy-load: if slixmpp doesn't have them the adapter continues without them.
        for plugin in ("xep_0394", "xep_0444", "xep_0004", "xep_0050", "xep_0461", "xep_0446", "xep_0447", "xep_0308", "xep_0424", "xep_0359"):
            try:
                client.register_plugin(plugin)
                self._registered_plugins.add(plugin)
                logger.debug("xmpp: registered slixmpp plugin %s", plugin)
            except Exception:
                logger.warning("xmpp: slixmpp plugin %s not available", plugin)

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
            "xmpp: unexpected disconnect — attempting reconnect via slixmpp"
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

            if not body:
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
                user_id = real_jid or self._muc_real_jid(stanza) or chat_id
                if real_jid:
                    logger.debug("xmpp: MUC real JID from roster: %s → %s", from_resource, real_jid)
                elif user_id == chat_id:
                    logger.debug("xmpp: MUC anonymous — no real JID for %s in %s", from_resource, from_bare)
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
            logger.info(
                "xmpp: inbound msg_id=%s stanza_id=%s type=%s",
                msg_id, getattr(stanza, "get", lambda k: "N/A")("id"), stanza_type,
            )
            event = MessageEvent(
                text=body,
                message_type=MessageType.TEXT,
                source=source,
                raw_message=stanza_to_dispatch,
                message_id=msg_id,
                reply_to_message_id=reply_to_message_id,
                reply_to_text=reply_to_text,
            )
            await self.handle_message(event)
        except Exception:
            logger.exception("xmpp: error handling inbound stanza")

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
            return False
        return self._bare(user_jid) in self.allowed_users

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
                return await self._send_encrypted(chat_id, content, thread_id=thread_id)
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
                    logger.info(
                        "xmpp: reply xml=%s",
                        str(stanza.xml)[:400],
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
                # Attach XEP-0394 markup if available
                if "xep_0394" in self._registered_plugins and getattr(stanza, "xml", None) is not None:
                    try:
                        markup = self._build_markup(chunk)
                        if markup is not None:
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
                        logger.info("OMEMO: edit encryption non-critical errors: %s", errors)
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
        - **bold** or __bold__ → emphasis
        - `code` or ``code`` → code span
        - ```code block``` → block-code
        """
        try:
            from slixmpp.plugins.xep_0394.stanza import Markup, Span, BlockCode, EmphasisType, CodeType
        except ImportError:
            return None
        markup = Markup(parent=None)
        spans: List[Any] = []
        import re
        for m in re.finditer(r'\*\*(.+?)\*\*|__(.+?)__', body, re.DOTALL):
            start = m.start()
            end = m.end()
            span = Span(parent=markup)
            span["start"] = start
            span["end"] = end
            span.xml.append(EmphasisType(parent=span).xml)
            spans.append(span)
        for m in re.finditer(r'`{1,2}([^`]+)`{1,2}', body, re.DOTALL):
            start = m.start()
            end = m.end()
            span = Span(parent=markup)
            span["start"] = start
            span["end"] = end
            span.xml.append(CodeType(parent=span).xml)
            spans.append(span)
        for m in re.finditer(r'```(.+?)```', body, re.DOTALL):
            start = m.start()
            end = m.end()
            bcode = BlockCode(parent=markup)
            bcode["start"] = start
            bcode["end"] = end
            spans.append(bcode)
        for s in spans:
            markup.xml.append(s.xml)
        return markup if spans else None

    async def _send_encrypted(self, chat_id: str, content: str, *, thread_id: Optional[str] = None) -> SendResult:
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
            stanza = client_local.make_message(mto=chat_id, mtype=mtype)
            stanza["body"] = chunk
            if thread_id:
                stanza["thread"] = thread_id
            stanza.set_from(client_local.boundjid)

            recipient_jid = JID(chat_id)
            message, encryption_errors = await xep_0384.encrypt_message(stanza, {recipient_jid})

            if encryption_errors:
                logger.info("OMEMO: encryption non-critical errors: %s", encryption_errors)

            if message is None:
                logger.warning("OMEMO: nothing to encrypt, falling back to plaintext")
                stanza = client_local.make_message(mto=chat_id, mbody=chunk, mtype=mtype)
                if "xep_0085" in self._registered_plugins:
                    stanza["chat_state"] = "active"
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
            logger.exception("xmpp: HTTP upload (XEP-0363) failed")
            return SendResult(success=False, error=str(exc), retryable=True)

        body = url if not caption else f"{caption}\n{url}"
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
        if self.client is None:
            return SendResult(success=False, error="xmpp not connected", retryable=True)

        if not choices or "xep_0004" not in self._registered_plugins:
            # Fallback to text-based clarify
            from tools.clarify_gateway import mark_awaiting_text
            if choices:
                lines = [f"❓ {question}", ""]
                for i, choice in enumerate(choices, start=1):
                    lines.append(f"  {i}. {choice}")
                lines.append("")
                lines.append("Reply with the number, the option text, or your own answer.")
                text = "\n".join(lines)
                mark_awaiting_text(clarify_id)
            else:
                text = f"❓ {question}"
            return await self.send(chat_id=chat_id, content=text, metadata=metadata)

        mtype = "groupchat" if self._is_muc(chat_id) else "chat"
        try:
            form = self.client["xep_0004"].make_form(
                ftype="form", title=question, instructions="Select an option and submit."
            )
            form.add_field(
                var="clarify_id",
                ftype="hidden",
                value=clarify_id,
            )
            options = [
                {"label": str(c), "value": str(c)}
                for c in (choices or [])
            ]
            options.append({"label": "Other (type your own answer)", "value": "__other__"})
            form.add_field(
                var="answer",
                ftype="list-single",
                label=question,
                options=options,
            )

            msg = self.client.make_message(mto=chat_id, mtype=mtype)
            msg["body"] = question
            msg["form"] = form
            msg.send()

            msg_id = None
            try:
                msg_id = msg["id"]
            except Exception:
                pass
            return SendResult(success=True, message_id=msg_id, raw_response=msg)
        except Exception as exc:
            logger.exception("xmpp: send_clarify with data form failed")
            return SendResult(success=False, error=str(exc), retryable=True)

    # -----------------------------------------------------------------
    # Ad-Hoc Commands (XEP-0050)
    # -----------------------------------------------------------------

    async def _setup_adhoc_commands(self) -> None:
        if self.client is None or "xep_0050" not in self._registered_plugins:
            return
        try:
            xep_0050 = self.client["xep_0050"]
            # Register a simple "hermes" command node that lists available actions
            xep_0050.add_command(
                jid=self.client.boundjid,
                node="hermes",
                name="Hermes Agent Commands",
                handler=self._adhoc_hermes_handler,
            )
        except Exception:
            logger.debug("xmpp: failed to register ad-hoc commands", exc_info=True)

    async def _adhoc_hermes_handler(self, iq: Any, session: Dict[str, Any]) -> Dict[str, Any]:
        client = self.client
        if client is None or "xep_0004" not in self._registered_plugins:
            session["notes"] = [("error", "Data forms not available")]
            return session
        try:
            form = client["xep_0004"].make_form(
                ftype="form", title="Hermes Commands", instructions="Choose a command to execute."
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
            session["has_next"] = False
            session["next"] = None
            return session
        except Exception:
            session["notes"] = [("error", "Failed to build command list")]
            return session

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
        "allowed_users", "allow_all_users",
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
