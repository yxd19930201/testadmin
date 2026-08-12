"""AI 模型网关：OpenAI 兼容接口，key 本地加密配置，经本地中转调用。"""
from __future__ import annotations

import json
import urllib.request

from captrace.config.models import AiModelConfig


class AiGateway:
    def __init__(self, cfg: AiModelConfig):
        self.cfg = cfg

    def complete(self, system: str, user: str) -> str:
        if not self.cfg.api_key:
            raise RuntimeError(
                "AI 模型未配置：请在「连接配置 → AI 模型」中填写 API Key 后重试。"
            )
        payload = {
            "model": self.cfg.model,
            "temperature": self.cfg.temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        req = urllib.request.Request(
            f"{self.cfg.base_url.rstrip('/')}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.cfg.api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"]
