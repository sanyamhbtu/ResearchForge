"""Optional language-model client.

The language model is used only for language tasks (query planning hints,
phrasing a clarification question, synthesis of already-retrieved evidence).
It is never the source of retrieved facts: if it is unavailable the platform
still works, with fully deterministic output.

The client degrades gracefully — every failure returns ``None``.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..config import get_settings
from ..http_client import HttpError, HttpClient
from ..logging_utils import get_logger

log = get_logger("services.llm")


class LLMClient:
    def __init__(self, http: HttpClient | None = None) -> None:
        self.settings = get_settings()
        self.http = http or HttpClient(cache_enabled=False, logger=log)

    @property
    def enabled(self) -> bool:
        return self.settings.llm_enabled

    def describe(self) -> dict:
        return {
            "enabled": self.enabled,
            "model": self.settings.llm_model if self.enabled else "",
            "base_url_configured": bool(self.settings.llm_base_url),
            "role": "language-only: never the source of retrieved facts",
        }

    def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        temperature: float = 0.0,
        max_tokens: int = 1200,
        json_mode: bool = False,
    ) -> str | None:
        if not self.enabled:
            return None
        url = self.settings.llm_base_url.rstrip("/")
        if not url.endswith("/chat/completions"):
            url = f"{url}/chat/completions"
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload: dict[str, Any] = {
            "model": self.settings.llm_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            response = self.http.request(
                "POST",
                url,
                headers={
                    "Authorization": f"Bearer {self.settings.llm_api_key}",
                    "Content-Type": "application/json",
                },
                body=json.dumps(payload).encode("utf-8"),
                accept="application/json",
                use_cache=False,
                timeout=self.settings.llm_timeout,
            )
            data = response.json()
            choices = (data or {}).get("choices") or []
            if not choices:
                log.warning("llm_empty_response", url=url)
                return None
            return (choices[0].get("message") or {}).get("content")
        except (HttpError, ValueError, KeyError) as exc:
            log.warning("llm_call_failed", error=str(exc))
            return None

    def complete_json(self, prompt: str, *, system: str = "", max_tokens: int = 1500) -> dict | None:
        raw = self.complete(prompt, system=system, json_mode=True, max_tokens=max_tokens)
        if not raw:
            return None
        parsed = _extract_json(raw)
        return parsed if isinstance(parsed, dict) else None


def _extract_json(text: str) -> Any:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except ValueError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except ValueError:
                return None
    return None


_llm: LLMClient | None = None


def get_llm() -> LLMClient:
    global _llm
    if _llm is None:
        _llm = LLMClient()
    return _llm
