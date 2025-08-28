import asyncio, json, inspect
import websockets

class WSClient:
	def __init__(self, url: str, *, subprotocols=None, ping_interval=20):
		self.url = url
		self.subprotocols = subprotocols
		self.ping_interval = ping_interval
		self._ws = None
		self._recv_task = None
		self._listeners = {"*": set()}

	def on(self, event: str):
		def decorator(handler_coro):
			self._listeners.setdefault(event, set()).add(handler_coro)
			return handler_coro
		return decorator

	async def connect(self):
		self._ws = await websockets.connect(self.url, subprotocols=self.subprotocols, ping_interval=self.ping_interval)
		self._recv_task = asyncio.create_task(self._receiver())

	async def close(self, code=1000, reason="bye"):
		if self._ws:
			await self._ws.close(code=code, reason=reason)
		if self._recv_task:
			self._recv_task.cancel()

	async def send_json(self, obj: dict):
		await self._ws.send(json.dumps(obj))

	async def send_binary(self, data: bytes):
		await self._ws.send(data)

	async def _receiver(self):
		try:
			async for msg in self._ws:
				if isinstance(msg, str):
					try:
						obj = json.loads(msg)
					except Exception as e:
						# invalid JSON ignored (protocol expects JSON-only for text)
						continue
					await self._emit("json", obj)
				else:
					await self._emit("binary", msg)
		except websockets.ConnectionClosedOK:
			pass
		except websockets.ConnectionClosedError as e:
			print(f"[client] connection closed with error: {e.code} {e.reason}")

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