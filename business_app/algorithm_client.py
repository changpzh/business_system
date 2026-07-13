from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import settings


class AlgorithmClientError(RuntimeError):
    pass


class AlgorithmClient:
    def _json_request(self, path: str, method: str = "GET", payload: dict[str, Any] | None = None, timeout: int | None = None) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        request = Request(
            f"{settings.algorithm_base_url}{path}",
            data=body,
            method=method,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urlopen(request, timeout=timeout or settings.algorithm_timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                content = json.loads(exc.read().decode("utf-8"))
                if isinstance(content, dict):
                    return content
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
            raise AlgorithmClientError(f"算法服务返回 HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise AlgorithmClientError(f"无法连接算法服务 {settings.algorithm_base_url}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise AlgorithmClientError("算法服务返回了无效 JSON") from exc

    def health(self) -> dict[str, Any]:
        return self._json_request("/health", timeout=5)

    def capabilities(self) -> dict[str, Any]:
        return self._json_request("/api/v1/capabilities", timeout=5)

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        endpoint = "/api/v1/schedules/local-adjust" if payload.get("mode") == "local" else "/api/v1/schedules/execute"
        return self._json_request(endpoint, method="POST", payload=payload)


algorithm_client = AlgorithmClient()
