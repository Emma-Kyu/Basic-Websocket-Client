import asyncio, json, inspect
import websockets

import asyncio, json, inspect
import websockets

class WSClient:
	def __init__(
		self,
		url: str,
		*,
		subprotocols=None,
		ping_interval=0.5,
		ping_timeout=1.0,
		open_timeout=1.0,
		close_timeout=1.0,
		reconnect_backoff=(0.5, 5.0)
	):
		self.url = url
		self.subprotocols = subprotocols
		self.ping_interval = ping_interval
		self.ping_timeout = ping_timeout
		self.open_timeout = open_timeout
		self.close_timeout = close_timeout
		self.min_backoff, self.max_backoff = reconnect_backoff

		self._ws = None
		self._listeners = {"*": set()}
		self._runner_task = None
		self._stop = False
		self._ready = asyncio.Event()

	def on(self, event: str):
		def decorator(handler_coro):
			self._listeners.setdefault(event, set()).add(handler_coro)
			return handler_coro
		return decorator

	@property
	def connected(self) -> bool:
		return self._ws is not None and not self._ws.closed

	async def start(self):
		# Idempotent: ensure a single runner
		if self._runner_task and not self._runner_task.done():
			return
		self._stop = False
		self._runner_task = asyncio.create_task(self._runner())

	async def connect(self):
		# Backwards-compat: start() now owns reconnect; connect() just calls start() and waits once
		await self.start()
		await self.wait_until_ready(timeout=self.open_timeout)

	async def close(self, code=1000, reason="bye"):
		self._stop = True
		try:
			if self._ws:
				await self._ws.close(code=code, reason=reason)
		except Exception:
			pass
		if self._runner_task:
			self._runner_task.cancel()

	async def wait_until_ready(self, timeout: float | None = None) -> bool:
		try:
			await asyncio.wait_for(self._ready.wait(), timeout)
		except asyncio.TimeoutError:
			return False
		return self._ready.is_set()

	async def send_json(self, obj: dict):
		if not self.connected:
			ok = await self.wait_until_ready(timeout=1.0)
			if not ok:
				raise ConnectionError("WebSocket not connected")
		await self._ws.send(json.dumps(obj))

	async def send_binary(self, data: bytes):
		if not self.connected:
			ok = await self.wait_until_ready(timeout=1.0)
			if not ok:
				raise ConnectionError("WebSocket not connected")
		await self._ws.send(data)

	async def _runner(self):
		backoff = self.min_backoff
		while not self._stop:
			try:
				self._ws = await websockets.connect(
					self.url,
					subprotocols=self.subprotocols,
					ping_interval=self.ping_interval,
					ping_timeout=self.ping_timeout,
					open_timeout=self.open_timeout,
					close_timeout=self.close_timeout,
					max_queue=32
				)
				self._ready.set()
				# Optional: emit "connect" to listeners that care
				await self._emit("connect", {"ok": True})

				# Receiver loop
				try:
					async for msg in self._ws:
						if isinstance(msg, str):
							try:
								obj = json.loads(msg)
							except Exception:
								# invalid JSON ignored (protocol expects JSON-only for text)
								continue
							await self._emit("json", obj)
						else:
							await self._emit("binary", msg)
				finally:
					# Drop through to reconnect on any exit
					pass

			except websockets.ConnectionClosedOK:
				# Normal closure; likely stop requested
				pass
			except websockets.ConnectionClosedError as e:
				print(f"[client] connection closed with error: {e.code} {e.reason}")
			except Exception as e:
				print(f"[client] connect/recv error: {e}")

			# Teardown & notify
			if self._ws:
				try:
					await self._ws.close()
				except Exception:
					pass
			self._ws = None
			self._ready.clear()
			await self._emit("disconnect", {"reason": "reconnect"})

			# Reconnect after short backoff (unless stopping)
			if self._stop:
				break
			await asyncio.sleep(backoff)
			backoff = min(backoff * 2, self.max_backoff)

	async def _emit(self, event: str, payload):
		for h in list(self._listeners.get("*", ())):
			await self._safe_call(h, event, payload)
		for h in list(self._listeners.get(event, ())):
			await self._safe_call(h, payload)

	async def _safe_call(self, handler_coro, *args):
		try:
			sig = inspect.signature(handler_coro)
			if len(sig.parameters) == len(args):
				await handler_coro(*args)
			else:
				await handler_coro(args[-1])
		except Exception as e:
			print(f"[client] handler error: {e}")

class WSServer:
	def __init__(self, host: str = "127.0.0.1", port: int = 8765, *, ping_interval=20):
		self.host = host
		self.port = port
		self.ping_interval = ping_interval
		self._listeners = {"*": set()}
		self._conns = set()
		self._tasks = set()

	def endpoint(self, path: str = "/") -> str:
		host = "localhost" if self.host in ("0.0.0.0", "") else self.host
		return f"ws://{host}:{self.port}{path}"

	def on(self, event: str):
		def decorator(handler_coro):
			self._listeners.setdefault(event, set()).add(handler_coro)
			return handler_coro
		return decorator

	def track(self, task: asyncio.Task):
		self._tasks.add(task)
		task.add_done_callback(self._tasks.discard)
		return task

	async def start(self):
		async with websockets.serve(self._handle_conn, self.host, self.port, ping_interval=self.ping_interval):
			await asyncio.Future()

	async def send_json(self, ws, obj: dict):
		await ws.send(json.dumps(obj))

	async def send_binary(self, ws, data: bytes):
		await ws.send(data)

	async def broadcast_json(self, obj: dict):
		msg = json.dumps(obj)
		await asyncio.gather(*(ws.send(msg) for ws in list(self._conns)), return_exceptions=True)

	async def _handle_conn(self, ws):
		self._conns.add(ws)
		await self._emit("connect", ws, None)
		try:
			async for msg in ws:
				if isinstance(msg, str):
					try:
						obj = json.loads(msg)
					except Exception as e:
						await ws.close(code=1003, reason="Text must be JSON")
						break
					await self._emit("json", ws, obj)
				else:
					await self._emit("binary", ws, msg)
		except websockets.ConnectionClosedOK:
			pass
		except websockets.ConnectionClosedError as e:
			print(f"[server] client closed with error: {e.code} {e.reason}")
		finally:
			await self._emit("disconnect", ws, None)
			self._conns.discard(ws)

	async def _emit(self, event: str, ws, payload):
		for h in list(self._listeners.get("*", ())):
			await self._safe_call(h, ws, event, payload)
		for h in list(self._listeners.get(event, ())):
			await self._safe_call(h, ws, payload)

	async def _safe_call(self, handler_coro, ws, *args):
		try:
			sig_len = len(inspect.signature(handler_coro).parameters)
			if sig_len == 3:
				await handler_coro(ws, *args)
			elif sig_len == 2:
				await handler_coro(ws, args[-1])
			elif sig_len == 1:
				await handler_coro(args[-1])
			else:
				await handler_coro()
		except Exception as e:
			print(f"[server] handler error: {e}")