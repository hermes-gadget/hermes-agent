"""Owner-protected config parents must retain authenticated cron/client bootstrap."""
import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.control_socket import GatewayControlServer
from gateway.runtime_bootstrap import TicketStore
from hermes_cli.gateway_client import _session_ticket
from hermes_cli import gateway_runtime_discovery as discovery

pytestmark = pytest.mark.platforms("linux")


def guarded_metadata(monkeypatch, home, owner, *, mode=0o770, single_uid=True):
    """Model only administrator ownership; sockets and peer credentials remain real."""
    original_lstat, original_read = Path.lstat, Path.read_text
    uid = os.getuid()

    def lstat(path):
        node = original_lstat(path)
        if path == home:
            fields = list(node)
            fields[0] = (node.st_mode & ~0o777) | mode
            fields[4] = owner
            return os.stat_result(fields)
        return node

    def read(path, *args, **kwargs):
        if path == Path('/proc/self/uid_map'):
            return f'{uid} {uid} 1\n' if single_uid else '0 0 4294967295\n'
        if path == Path('/proc/sys/kernel/overflowuid'):
            return '65534\n'
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'lstat', lstat)
    monkeypatch.setattr(Path, 'read_text', read)


@pytest.mark.parametrize('owner', [0, 65534])
def test_guarded_home_mints_a_real_same_user_ticket(tmp_path, monkeypatch, owner):
    home = tmp_path.resolve()
    home.chmod(0o770)
    guarded_metadata(monkeypatch, home, owner)
    endpoint = SimpleNamespace(profile_id=str(home), instance_id='guarded-owner', control_home=None)

    async def run():
        server = GatewayControlServer(home)
        server.ticket_store = TicketStore('guarded-owner', frozenset({str(home)}))
        assert await server.start()
        try:
            ticket = await asyncio.to_thread(_session_ticket, home, endpoint)
            assert ticket
            # An unauthenticated or different-UID listener cannot receive a request.
            for peer in (None, os.getuid() + 1):
                monkeypatch.setattr(discovery, 'socket_peer_uid', lambda _sock, uid=peer: uid)
                with pytest.raises(discovery.DiscoveryError, match='unsafe_control_peer'):
                    await asyncio.to_thread(_session_ticket, home, endpoint)
        finally:
            await server.stop()

    asyncio.run(run())


@pytest.mark.parametrize('owner,mode,single_uid,acl,reason', [
    (12345, 0o770, True, False, 'unsafe_control_path'),
    (65534, 0o770, False, False, 'unsafe_control_path'),
    (0, 0o772, True, False, 'unsafe_control_permissions'),
    (65534, 0o770, True, True, 'unsafe_control_permissions'),
])
def test_guarded_home_never_admits_foreign_or_shared_control(
        tmp_path, monkeypatch, owner, mode, single_uid, acl, reason):
    home = tmp_path.resolve()
    guarded_metadata(monkeypatch, home, owner, mode=mode, single_uid=single_uid)
    if acl:
        monkeypatch.setattr(os, 'listxattr', lambda *_a, **_k: ['system.posix_acl_access'])
    with pytest.raises(discovery.DiscoveryError, match=reason):
        discovery._private_node(home, kind='directory', home=True)
    # The exception applies only to the home, never to a socket, lock or pointer.
    with pytest.raises(discovery.DiscoveryError, match='unsafe_control_path'):
        discovery._private_node(home, kind='directory')
