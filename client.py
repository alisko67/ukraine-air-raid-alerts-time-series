"""HTTP-клієнт для tryvoha.online/api/v1.

Використовуємо лише ендпоїнти, перевірені фактичними запитами:
  GET /regions
  GET /alerts
  GET /stats/{slug}?period=24h|7d|30d

Клієнт відповідає за мережу: пауза між запитами (ліміт сервера 120/хв), повтори
при збоях, відсіювання порожніх/некоректних відповідей. Структуру відповіді
перевіряємо за полями, які реально бачили; якщо їх немає — це помилка, а не
привід щось домислювати.
"""
import json
import time
from dataclasses import dataclass
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from timeutil import utc_now_iso

BASE_URL = "https://tryvoha.online/api/v1"
USER_AGENT = "kse-alarm-timeseries-study/0.1 (educational project)"
MIN_INTERVAL = 0.7        # с між запитами => ~85/хв при ліміті 120/хв
LOW_REMAINING = 5         # якщо x-ratelimit-remaining менший — робимо довшу паузу
LOW_REMAINING_PAUSE = 15  # с


class ApiError(Exception):
    """Запит не вдався остаточно (після повторів) або відповідь некоректна."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class _Retryable(Exception):
    def __init__(self, message: str, status: int | None = None, wait: float | None = None):
        super().__init__(message)
        self.status, self.wait = status, wait


@dataclass
class Response:
    status: int
    data: dict
    fetched_at: str  # UTC, момент отримання відповіді (наш годинник)


class TryvohaClient:
    def __init__(self, base_url=BASE_URL, min_interval=MIN_INTERVAL, max_retries=4,
                 timeout=30, sleep=time.sleep):
        self.base_url, self.min_interval = base_url, min_interval
        self.max_retries, self.timeout, self._sleep = max_retries, timeout, sleep
        self._last_request = 0.0

    # --- низький рівень ---------------------------------------------------
    def _throttle(self):
        wait = self._last_request + self.min_interval - time.monotonic()
        if wait > 0:
            self._sleep(wait)
        self._last_request = time.monotonic()

    def _get_once(self, url: str) -> Response:
        self._throttle()
        req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                body, status = resp.read(), resp.status
                remaining = resp.headers.get("x-ratelimit-remaining")
        except HTTPError as e:
            if e.code == 429:
                retry_after = e.headers.get("Retry-After") if e.headers else None
                wait = float(retry_after) if retry_after and retry_after.isdigit() else None
                raise _Retryable("HTTP 429 (ліміт запитів)", 429, wait) from e
            if e.code >= 500:
                raise _Retryable(f"HTTP {e.code}", e.code) from e
            raise ApiError(f"HTTP {e.code}", e.code) from e  # 4xx не повторюємо
        except OSError as e:  # URLError, таймаут, обрив з'єднання
            raise _Retryable(f"мережева помилка: {e}") from e

        fetched_at = utc_now_iso()
        if not body.strip():
            raise _Retryable("порожня відповідь", status)
        try:
            data = json.loads(body.decode("utf-8"))
        except ValueError as e:
            raise _Retryable("відповідь не є коректним JSON", status) from e
        if not isinstance(data, dict):
            raise ApiError("очікувався JSON-об'єкт", status)
        if remaining is not None and remaining.isdigit() and int(remaining) < LOW_REMAINING:
            self._sleep(LOW_REMAINING_PAUSE)
        return Response(status, data, fetched_at)

    def get(self, path: str, params: dict | None = None) -> Response:
        url = self.base_url + path + (("?" + urlencode(params)) if params else "")
        last = None
        for attempt in range(self.max_retries + 1):
            try:
                return self._get_once(url)
            except _Retryable as e:
                last = e
                if attempt < self.max_retries:
                    self._sleep(e.wait if e.wait else min(2 ** (attempt + 1), 60))
        raise ApiError(f"не вдалося після {self.max_retries + 1} спроб: {last}", last.status)

    # --- ендпоїнти з перевіркою структури ---------------------------------
    def regions(self) -> Response:
        r = self.get("/regions")
        _require_items(r.data.get("regions"), "regions", ("slug", "type"))
        return r

    def alerts(self) -> Response:
        r = self.get("/alerts")
        if "updated_at" not in r.data:
            raise ApiError("у /alerts немає поля updated_at", r.status)
        # Порожній список — коректна відповідь (тривог немає), порожнє тіло ми відсіяли раніше.
        _require_items(r.data.get("alerts"), "alerts", ("slug", "started_at"))
        return r

    def stats(self, slug: str, period: str = "7d") -> Response:
        r = self.get(f"/stats/{slug}", {"period": period})
        alerts = r.data.get("alerts")
        if not isinstance(alerts, dict) or "count" not in alerts:
            raise ApiError(f"/stats/{slug}: немає alerts.count", r.status)
        for key in ("from", "to"):
            if key not in r.data:
                raise ApiError(f"/stats/{slug}: немає поля {key}", r.status)
        # ended_at обов'язково має бути присутнім (null = триває), інакше не відрізнити
        # «триває» від «поле зникло».
        _require_items(alerts.get("recent"), "alerts.recent", ("started_at", "ended_at"))
        return r


def _require_items(items, name: str, keys: tuple):
    if not isinstance(items, list):
        raise ApiError(f"очікувався список '{name}'")
    for item in items:
        missing = [k for k in keys if k not in item]
        if missing:
            raise ApiError(f"у елементі '{name}' немає полів {missing}")
