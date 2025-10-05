import asyncio, json, inspect, websockets

class WSClient:
	def __init__(self, url: str, *, subprotocols=None, heartbeat_interval=1, heartbeat_timeout=8.0, reconnect_initial=0.25, reconnect_max=1.0):
		self.url = url
		self.subprotocols = subprotocols

		# Manual heartbeat controls (client-driven, short-lived)
		self.heartbeat_interval = float(heartbeat_interval)
		self.heartbeat_timeout  = float(heartbeat_timeout)

		# Reconnect backoff (quick retries)
		self.reconnect_initial = float(reconnect_initial)
		self.reconnect_max     = float(reconnect_max)

		self._ws = None
		self._runner = None
		self._recv_task = None
		self._hb_task = None
		self._closing = False
		self._ready_evt = asyncio.Event()

		self._listeners = {"*": set()}

	def on(self, event: str):
		def decorator(handler_coro):
			self._listeners.setdefault(event, set()).add(handler_coro)
			return handler_coro
		return decorator

	async def connect(self):
		"""
		Start background runner that will keep the connection alive.
		Returns once an initial connection is established (or raises).
		"""
		if self._runner and not self._runner.done():
			# Already running: just wait for readiness.
			await self._wait_connected()
			return

		self._closing = False
		self._runner = asyncio.create_task(self._run())

		# Wait until first successful connect (fast fail if it can't connect)
		await self._wait_connected()

	async def close(self, code=1000, reason="bye"):
		self._closing = True
		try:
			if self._ws:
				await self._ws.close(code=code, reason=reason)
		except Exception:
			pass
		for t in (self._hb_task, self._recv_task, self._runner):
			if t and not t.done():
				t.cancel()

	async def send_json(self, obj: dict):
		await self._wait_connected()
		await self._safe_send(json.dumps(obj))

	async def send_binary(self, data: bytes):
		await self._wait_connected()
		await self._safe_send(data)

	# -------------------------
	# Internal background logic
	# -------------------------
	async def _run(self):
		backoff = self.reconnect_initial
		while not self._closing:
			try:
				# Disable library ping thread; we do our own ultra-fast heartbeats.
				self._ws = await websockets.connect(
					self.url,
					subprotocols=self.subprotocols,
					ping_interval=None,
					ping_timeout=None,
					close_timeout=1.0,
					open_timeout=3.0,
				)
				self._ready_evt.set()
				await self._emit("connect", None)

				# Start receiver + heartbeat
				self._recv_task = asyncio.create_task(self._receiver())
				self._hb_task = asyncio.create_task(self._heartbeat())

				# Wait until either task finishes (disconnect or error)
				done, pending = await asyncio.wait(
					{self._recv_task, self._hb_task},
					return_when=asyncio.FIRST_COMPLETED
				)
				# Ensure both tasks are stopped
				for t in pending:
					t.cancel()
				for t in done:
					# surface exceptions for logging; loop will reconnect
					try:
						_ = t.result()
					except Exception as e:
						print(f"[client] task ended with error: {e}")

			except Exception as e:
				await self._emit("disconnect", None)
				print(f"[client] connect/run error: {e}")

			# Cleanup after disconnect
			self._ready_evt.clear()
			self._ws = None
			self._recv_task = None
			self._hb_task = None

			if self._closing:
				break

			# Quick backoff, capped
			await asyncio.sleep(backoff)
			backoff = min(self.reconnect_max, max(self.reconnect_initial, backoff * 1.5))

	async def _wait_connected(self):
		while not self._closing:
			if self._ws is not None:
				return
			await self._ready_evt.wait()
			if self._ws is not None:
				return

	async def _safe_send(self, payload):
		try:
			await self._ws.send(payload)
		except Exception as e:
			# Force a reconnect by closing; sender will wait on _wait_connected again
			print(f"[client] send error, forcing reconnect: {e}")
			try:
				await self._ws.close(code=1011, reason="send-error")
			except Exception:
				pass
			raise

	async def _receiver(self):
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
		except websockets.ConnectionClosedOK:
			pass
		except websockets.ConnectionClosedError as e:
			print(f"[client] connection closed with error: {e.code} {e.reason}")
		finally:
			# Returning will let _run() reconnect
			return

	async def _heartbeat(self):
		"""
		Manual 500ms–1s ping/pong heartbeat. Any miss triggers reconnect.
		"""
		try:
			while True:
				# If socket got swapped out, exit so runner restarts a fresh HB task
				if self._ws is None:
					return
				try:
					ping = await self._ws.ping()
					await asyncio.wait_for(ping, timeout=self.heartbeat_timeout)
				except Exception as e:
					print(f"[client] heartbeat failed: {e}")
					# Triggers reconnect via receiver/runner unwind
					try:
						await self._ws.close(code=1011, reason="heartbeat-timeout")
					except Exception:
						pass
					return
				await asyncio.sleep(self.heartbeat_interval)
		finally:
			return

	# -------------------------
	# Event dispatch helpers
	# -------------------------
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
	def __init__(
		self,
		host: str = "127.0.0.1",
		port: int = 8765,
		*,
		ping_interval: float = 1.0,
		ping_timeout: float = 8.0,
		close_timeout: float = 0.5,
		max_size: int | None = 2**23, # 8mb
	):
		self.host = host
		self.port = int(port)

		# Heartbeat + close controls
		self.ping_interval = float(ping_interval)
		self.ping_timeout  = float(ping_timeout)
		self.close_timeout = float(close_timeout)
		self.max_size = max_size

		self._listeners = {"*": set()}
		self._conns = set()
		self._tasks = set()
		self._server = None
		self._stopped = asyncio.Event()

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
		if self._server:
			return
		self._stopped.clear()

		self._server = await websockets.serve(
			self._handle_conn,
			self.host,
			self.port,
			ping_interval=self.ping_interval,   # how often server pings
			ping_timeout=self.ping_timeout,     # wait for pong before drop
			close_timeout=self.close_timeout,   # graceful close wait
			max_size=self.max_size,             # optional size limit
		)
		print(f"WS server running at {self.endpoint()}")

	async def stop(self):
		if not self._server:
			return
		for ws in list(self._conns):
			try:
				await ws.close(code=1001, reason="server-shutdown")
			except Exception:
				pass
		self._server.close()
		await self._server.wait_closed()
		self._server = None
		self._stopped.set()

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
					except Exception:
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