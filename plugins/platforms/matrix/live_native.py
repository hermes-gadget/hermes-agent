"""Private framed IPC and bounded lifecycle for the native MatrixRTC child."""
import asyncio
from contextlib import suppress
import json
from pathlib import Path
import socket
import struct


def packet(kind, payload):
    data = bytes([kind]) + payload
    if not 0 < len(data) <= 65536:
        raise ValueError("native call packet exceeds its bound")
    return struct.pack(">I", len(data)) + data


class NativeCallLink:
    def __init__(self, helper_path, home, config, *, on_event, on_audio):
        self.helper_path, self.home, self.config = str(helper_path), Path(home), config
        self.on_event, self.on_audio = on_event, on_audio
        self.process = self.writer = self.reader = None
        self._read_task = self._stderr_task = None
        self._closed = False
        self.ready = None

    async def start(self):
        from tools.environments.local import served_profile_child_env
        self.ready = asyncio.get_running_loop().create_future()
        # The native helper owns Matrix keys only. OpenAI and unrelated profile
        # credentials stay in the gateway; preserve the profile's OS/TLS setup.
        scoped = served_profile_child_env(target_home=self.home)
        names = {"PATH", "HOME", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR",
                 "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "LD_LIBRARY_PATH", "XDG_RUNTIME_DIR"}
        env = {key: value for key, value in scoped.items() if key in names}
        if self.config.get("ca_file"):
            env["SSL_CERT_FILE"] = self.config["ca_file"]
        parent, child = socket.socketpair()
        try:
            self.process = await asyncio.create_subprocess_exec(
                self.helper_path, "run", str(child.fileno()), pass_fds=(child.fileno(),),
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE, env=env, umask=0o077)
        except BaseException:
            parent.close()
            raise
        finally:
            child.close()
        self.reader, self.writer = await asyncio.open_connection(sock=parent, limit=65536)
        self._read_task = asyncio.create_task(self._read())
        self._stderr_task = asyncio.create_task(self._stderr())
        try:
            await self.control("init", protocol=1, config=self.config)
            await asyncio.wait_for(asyncio.shield(self.ready), 30)
        except BaseException:
            await self.close()
            raise

    async def control(self, op, **data):
        if self.writer is None or self.writer.is_closing():
            raise ConnectionError("native call control is closed")
        self.writer.write(packet(0, json.dumps({"op": op, **data}).encode()))
        await asyncio.wait_for(self.writer.drain(), 5)

    async def audio(self, generation, pcm):
        if self.writer is None or self.writer.is_closing():
            raise ConnectionError("native audio is closed")
        self.writer.write(packet(1, struct.pack(">Q", generation) + pcm))
        await asyncio.wait_for(self.writer.drain(), 1)

    async def _stderr(self):
        # Drain continuously so a C++ diagnostic cannot deadlock the child.
        # Raw SDK errors and media diagnostics never become chat/log content.
        while await self.process.stderr.read(4096):
            pass

    async def _read(self):
        try:
            while True:
                size = struct.unpack(">I", await self.reader.readexactly(4))[0]
                if not 0 < size <= 65536:
                    raise ValueError("invalid native call packet length")
                data = await self.reader.readexactly(size)
                if data[0] == 0:
                    event = json.loads(data[1:])
                    if not isinstance(event, dict):
                        raise ValueError("native call event must be an object")
                    if event.get("event") == "ready" and not self.ready.done():
                        if event.get("protocol") != 1:
                            raise ValueError("unsupported native call protocol")
                        self.ready.set_result(event)
                    else:
                        await self.on_event(event)
                elif data[0] == 1 and len(data) >= 9 and (len(data) - 9) % 2 == 0:
                    self.on_audio(struct.unpack(">Q", data[1:9])[0], data[9:])
                else:
                    raise ValueError("invalid native call packet type")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self.ready.done():
                self.ready.set_exception(ConnectionError("native call helper startup failed"))
            if not self._closed:
                await self.on_event({"event": "helper_ended", "reason": type(exc).__name__})

    async def close(self):
        if self._closed:
            return
        self._closed = True
        if self.writer is not None:
            try:
                await self.control("quit")
            except (OSError, TimeoutError, ConnectionError):
                pass
        if self.process is not None and self.process.returncode is None:
            try:
                await asyncio.wait_for(self.process.wait(), 28)
            except TimeoutError:
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), 3)
                except TimeoutError:
                    self.process.kill()
                    await self.process.wait()
        if self.writer is not None:
            self.writer.close()
            with suppress(ConnectionError, OSError):
                await self.writer.wait_closed()
        tasks = [task for task in (self._read_task, self._stderr_task)
                 if task and task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
