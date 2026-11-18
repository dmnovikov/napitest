"""Страница «microSD»: образы ОС на карту через кардридер сервера, без сети плат.

Сервер работает без прав root и карту смонтировать не может. Он кладёт запрос в файл; systemd
(napi-card-reader.path → .service, от root) запускает scripts/card-load-images.py, тот пишет ход
работы и итог в файл состояния. В запросе только действие: check (показать, что на карте) или load.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

REQUEST = "/var/lib/napi-factory/cardreader/request"
STATUS = "/run/napi-card-reader/status.json"
STALE_S = 120                 # скрипт молчит дольше: считается, что он не работает


class CardReaderError(Exception):
    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = status


class CardReader:
    def __init__(self) -> None:
        self.request_path = Path(os.environ.get("NAPI_CARD_READER_REQUEST", REQUEST))
        self.status_path = Path(os.environ.get("NAPI_CARD_READER_STATUS", STATUS))
        self._asked: tuple[str, float] | None = None       # запрос отправлен, скрипт ещё не отозвался

    def available(self) -> bool:
        return self.request_path.parent.is_dir()

    def _status(self) -> dict | None:
        try:
            st = json.loads(self.status_path.read_text())
        except (OSError, ValueError):
            return None
        return st if isinstance(st, dict) else None

    def view(self) -> dict:
        if not self.available():
            return {"available": False}
        st = self._status() or {"state": "idle"}
        now = time.time()
        if self._asked and (st.get("started") or 0) >= self._asked[1] - 1:
            self._asked = None                               # скрипт взял запрос
        if self._asked:
            if now - self._asked[1] > 20:
                self._asked = None
                st = {"state": "error", "action": st.get("action"), "cards": [],
                      "error": "служба кардридера не отвечает (napi-card-reader.path на сервере)"}
            else:
                st = {"state": "working", "action": self._asked[0], "cards": [], "progress": None, "log": []}
        elif st.get("state") == "working" and now - (st.get("ts") or 0) > STALE_S:
            st = {**st, "state": "error", "error": "запись прервалась: служба кардридера остановилась"}
        return {"available": True, **{k: st.get(k) for k in ("state", "action", "cards", "progress", "error", "log")},
                "age": round(now - st["ts"]) if st.get("ts") else None}

    def request(self, action: str) -> None:
        if action not in ("check", "load"):
            raise CardReaderError("действие: check | load")
        if not self.available():
            raise CardReaderError("на этом сервере работа с кардридером не установлена", 409)
        if self.view().get("state") == "working":
            raise CardReaderError("с картой уже идёт работа", 409)
        self._asked = (action, time.time())
        # Обычная запись (не через переименование): systemd замечает закрытие файла после записи
        with open(self.request_path, "w") as f:
            f.write(json.dumps({"action": action, "ts": self._asked[1]}))
