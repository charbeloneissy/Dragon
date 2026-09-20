"""Non-blocking Amplitude HTTP V2 telemetry for Dragon."""
from __future__ import annotations
import atexit, os, queue, socket, threading, time
from typing import Any
import httpx

class AmplitudeTelemetry:
    def __init__(self) -> None:
        self.enabled = os.getenv("AMPLITUDE_ENABLED", "false").strip().lower() in {"1","true","yes","on"}
        self.api_key = os.getenv("AMPLITUDE_API_KEY", "").strip()
        self.endpoint = os.getenv("AMPLITUDE_ENDPOINT", "https://api2.amplitude.com/2/httpapi").strip()
        self.user_id = os.getenv("AMPLITUDE_USER_ID", "dragon-runtime").strip() or "dragon-runtime"
        self.device_id = os.getenv("AMPLITUDE_DEVICE_ID", socket.gethostname()).strip() or "dragon-runtime"
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=1000)
        self._stop = threading.Event()
        self._thread = None
        if self.enabled and self.api_key:
            self._thread = threading.Thread(target=self._worker, name="dragon-amplitude", daemon=True)
            self._thread.start()
        atexit.register(self.close)
    @property
    def active(self) -> bool:
        return self.enabled and bool(self.api_key) and self._thread is not None
    def track(self, event_type: str, properties: dict[str, Any] | None = None) -> None:
        if not self.active: return
        event = {"user_id": self.user_id, "device_id": self.device_id, "event_type": event_type,
                 "time": int(time.time()*1000), "event_properties": self._clean(properties or {})}
        try: self._queue.put_nowait(event)
        except queue.Full: pass
    def _worker(self) -> None:
        with httpx.Client(timeout=httpx.Timeout(5.0, connect=3.0)) as client:
            while not self._stop.is_set() or not self._queue.empty():
                try: event = self._queue.get(timeout=0.5)
                except queue.Empty: continue
                if event is None:
                    self._queue.task_done(); break
                try:
                    response = client.post(self.endpoint, json={"api_key": self.api_key, "events": [event]},
                                           headers={"Content-Type":"application/json","Accept":"application/json"})
                    if response.status_code == 429: time.sleep(2.0)
                except Exception: pass
                finally: self._queue.task_done()
    @staticmethod
    def _clean(value: Any) -> Any:
        if isinstance(value, dict): return {str(k): AmplitudeTelemetry._clean(v) for k,v in value.items() if v is not None}
        if isinstance(value, (list,tuple)): return [AmplitudeTelemetry._clean(v) for v in value]
        if isinstance(value, (str,int,float,bool)): return value
        return str(value)
    def close(self) -> None:
        if self._thread is None: return
        self._stop.set()
        try: self._queue.put_nowait(None)
        except queue.Full: return
        self._thread.join(timeout=2.0)
        self._thread = None
