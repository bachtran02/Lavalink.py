"""
MIT License

Copyright (c) 2017-present Devoxin

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
import asyncio
import logging

import aiohttp

from .errors import AuthenticationError, RequestError
from .events import (PlayerUpdateEvent, TrackEndEvent, TrackExceptionEvent,
                     TrackStuckEvent, WebSocketClosedEvent)
from .stats import Stats
from .utils import decode_track

_log = logging.getLogger(__name__)
CLOSE_TYPES = (
    aiohttp.WSMsgType.CLOSE,
    aiohttp.WSMsgType.CLOSING,
    aiohttp.WSMsgType.CLOSED
)
MESSAGE_QUEUE_MAX_SIZE = 100
LAVALINK_API_VERSION = 'v4'


class Transport:
    """ The class responsible for dealing with connections to Lavalink. """
    def __init__(self, node, host: str, port: int, password: str, ssl: bool,
                 resume_key: str, resume_timeout: int):
        self.client = node.manager.client
        self._node = node

        self._session = self.client._session
        self._ws = None
        self._message_queue = []

        self._host = host
        self._port = port
        self._password = password
        self._ssl = ssl

        self._resume_key = resume_key
        self._resume_timeout = resume_timeout
        self._resuming_configured = False
        self._destroyed = False

        self.connect()

    @property
    def ws_connected(self):
        """ Returns whether the websocket is connected to Lavalink. """
        return self._ws is not None and not self._ws.closed

    @property
    def http_uri(self) -> str:
        """ Returns a 'base' URI pointing to the node's address and port, also factoring in SSL. """
        return '{}://{}:{}'.format('https' if self._ssl else 'http', self._host, self._port)

    async def close(self, code=aiohttp.WSCloseCode.OK):
        """|coro|

        Shuts down the websocket connection if there is one.
        """
        if self._ws:
            await self._ws.close(code=code)
            self._ws = None

    def connect(self):
        """ Attempts to establish a connection to Lavalink. """
        return asyncio.ensure_future(self._connect())

    async def destroy(self):
        """|coro|

        Closes the WebSocket gracefully, and stops any further reconnecting.
        Useful when needing to remove a node.
        """
        self._destroyed = True
        await self.close()

    async def _connect(self):
        if self._ws:
            await self.close()

        headers = {
            'Authorization': self._password,
            'User-Id': str(self.client._user_id),
            'Client-Name': 'Lavalink.py'
        }
        # TODO: 'User-Agent': 'Lavalink.py/{} (https://github.com/devoxin/lavalink.py)'.format(__version__)

        if self._resuming_configured and self._resume_key:
            headers['Resume-Key'] = self._resume_key

        _log.info('[Node:%s] Establishing WebSocket connection to Lavalink...', self._node.name)

        protocol = 'wss' if self._ssl else 'ws'
        attempt = 0

        while not self.ws_connected and not self._destroyed:
            attempt += 1
            try:
                self._ws = await self._session.ws_connect('{}://{}:{}'.format(protocol, self._host, self._port),
                                                          headers=headers, heartbeat=60)
            except (aiohttp.ClientConnectorError, aiohttp.WSServerHandshakeError, aiohttp.ServerDisconnectedError) as ce:
                if isinstance(ce, aiohttp.ClientConnectorError):
                    _log.warning('[Node:%s] Invalid response received; this may indicate that '
                                 'Lavalink is not running, or is running on a port different '
                                 'to the one you provided to `add_node`.', self._node.name)
                    return

                if isinstance(ce, aiohttp.WSServerHandshakeError):
                    if ce.status in (401, 403):  # Special handling for 401/403 (Unauthorized/Forbidden).
                        _log.warning('[Node:%s] Authentication failed while trying to establish a connection to the node.',
                                     self._node.name)
                        # We shouldn't try to establish any more connections as correcting this particular error
                        # would require the cog to be reloaded (or the bot to be rebooted), so further attempts
                        # would be futile, and a waste of resources.
                    else:
                        _log.warning('[Node:%s] The remote server returned code %d, the expected code was 101. This usually '
                                     'indicates that the remote server is a webserver and not Lavalink. Check your ports, '
                                     'and try again.', self._node.name, ce.status)

                    return

                _log.exception('[Node:%s] An unknown error occurred whilst trying to establish a connection to Lavalink', self._node.name)
                backoff = min(10 * attempt, 60)
                await asyncio.sleep(backoff)
            else:
                _log.info('[Node:%s] WebSocket connection established', self._node.name)
                await self._node.manager._node_connect(self._node)

                if not self._resuming_configured and self._resume_key \
                        and (self._resume_timeout and self._resume_timeout > 0):
                    await self._send(op='configureResuming', key=self._resume_key, timeout=self._resume_timeout)
                    self._resuming_configured = True

                if self._message_queue:
                    for message in self._message_queue:
                        await self._send(**message)

                    self._message_queue.clear()

                await self._listen()

    async def _listen(self):
        """ Listens for websocket messages. """
        async for msg in self._ws:
            _log.debug('[Node:%s] Received WebSocket message: %s', self._node.name, msg.data)

            if msg.type == aiohttp.WSMsgType.TEXT:
                await self._handle_message(msg.json())
            elif msg.type == aiohttp.WSMsgType.ERROR:
                exc = self._ws.exception()
                _log.error('[Node:%s] Exception in WebSocket!', self._node.name, exc_info=exc)
                break
            elif msg.type in CLOSE_TYPES:
                _log.debug('[Node:%s] Received close frame with code %d.', self._node.name, msg.data)
                await self._websocket_closed(msg.data, msg.extra)
                return

        await self._websocket_closed(self._ws.close_code, 'AsyncIterator loop exited')

    async def _websocket_closed(self, code: int = None, reason: str = None):
        """
        Handles when the websocket is closed.

        Parameters
        ----------
        code: Optional[:class:`int`]
            The response code.
        reason: Optional[:class:`str`]
            Reason why the websocket was closed. Defaults to ``None``.
        """
        _log.warning('[Node:%s] WebSocket disconnected with the following: code=%d reason=%s', self._node.name, code, reason)
        self._ws = None
        await self._node.manager._node_disconnect(self._node, code, reason)

    async def _handle_message(self, data: dict):
        """
        Handles the response from the websocket.

        Parameters
        ----------
        data: :class:`dict`
            The data given from Lavalink.
        """
        op = data['op']
        # handle ready op with sessionId etc.

        if op == 'stats':
            self._node.stats = Stats(self._node, data)
        elif op == 'playerUpdate':
            player_id = data['guildId']
            player = self.client.player_manager.get(int(player_id))

            if not player:
                _log.debug('[Node:%s] Received playerUpdate for non-existent player! GuildId: %s', self._node.name, player_id)
                return

            await player._update_state(data['state'])
            await self.client._dispatch_event(PlayerUpdateEvent(player, data['state']))
        elif op == 'event':
            await self._handle_event(data)
        else:
            _log.warning('[Node:%s] Received unknown op: %s', self._node.name, op)

    async def _handle_event(self, data: dict):
        """
        Handles the event from Lavalink.

        Parameters
        ----------
        data: :class:`dict`
            The data given from Lavalink.
        """
        player = self.client.player_manager.get(int(data['guildId']))
        event_type = data['type']

        if not player:
            if event_type not in ('TrackEndEvent', 'WebSocketClosedEvent'):  # Player was most likely destroyed if it's any of these.
                _log.warning('[Node:%s] Received event type %s for non-existent player! GuildId: %s', self._node.name, event_type, data['guildId'])
            return

        event = None

        if event_type == 'TrackEndEvent':
            track = decode_track(data['track']) if data['track'] else None
            event = TrackEndEvent(player, track, data['reason'])
        elif event_type == 'TrackExceptionEvent':
            exc_inner = data.get('exception', {})
            exception = data.get('error') or exc_inner.get('cause', 'Unknown exception')
            severity = exc_inner.get('severity', 'UNKNOWN')
            event = TrackExceptionEvent(player, player.current, exception, severity)
        # elif event_type == 'TrackStartEvent':
        #    event = TrackStartEvent(player, player.current)
        elif event_type == 'TrackStuckEvent':
            event = TrackStuckEvent(player, player.current, data['thresholdMs'])
        elif event_type == 'WebSocketClosedEvent':
            event = WebSocketClosedEvent(player, data['code'], data['reason'], data['byRemote'])
        else:
            if event_type == 'TrackStartEvent':
                return

            _log.warning('[Node:%s] Unknown event received of type \'%s\'', self._node.name, event_type)
            return

        await self.client._dispatch_event(event)

        if player:
            await player._handle_event(event)

    async def _send(self, **data):
        """
        Sends a payload to Lavalink.

        Parameters
        ----------
        data: :class:`dict`
            The data sent to Lavalink.
        """
        if not self.ws_connected:
            _log.debug('[Node:%s] WebSocket not ready; queued outgoing payload.', self._node.name)
            if len(self._message_queue) > MESSAGE_QUEUE_MAX_SIZE:
                _log.warning('[Node:%s] WebSocket message queue is currently at capacity, discarding payload.', self._node.name)
            else:
                self._message_queue.append(data)
            return

        _log.debug('[Node:%s] Sending payload %s', self._node.name, str(data))
        try:
            await self._ws.send_json(data)
        except ConnectionResetError:
            _log.warning('[Node:%s] Failed to send payload due to connection reset!', self._node.name)

    async def _get_request(self, path, **kwargs):
        to = kwargs.pop('to', None)

        async with self._session.get(f'{self.http_uri}/{LAVALINK_API_VERSION}{path}',
                                     headers={'Authorization': self._password}, **kwargs) as res:
            if res.status == 401 or res.status == 403:
                raise AuthenticationError

            if res.status == 200:
                json = await res.json()
                return json if to is None else to._from_json(json)

            _log.warning('Request to %s was unsuccessful: code=%d, body=%s', path, res.status, await res.text())
            raise RequestError('An invalid response was received from the node.')

    async def _post_request(self, path, **kwargs):
        to = kwargs.pop('to', None)

        async with self._session.post(f'{self.http_uri}/{LAVALINK_API_VERSION}{path}',
                                      headers={'Authorization': self._password}, **kwargs) as res:
            if res.status == 401 or res.status == 403:
                raise AuthenticationError

            if res.status == 200:
                json = await res.json()
                return json if to is None else to._from_json(json)

            if res.status == 204:
                return True

            _log.warning('Request to %s was unsuccessful: code=%d, body=%s', path, res.status, await res.text())
            raise RequestError('An invalid response was received from the node.')