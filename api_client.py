"""Bounded OpenAI-compatible HTTP calls using the Python standard library."""
import json
import math
import socket
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit


class ServiceError(Exception):
    pass


class APIClient:
    def __init__(self, config):
        self.config = config
        if config.base_url:
            parsed = urlsplit(config.base_url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
                raise ValueError("LLM_BASE_URL must be an HTTP(S) URL without credentials")

    def post(self, endpoint, payload, deadline=None):
        if not self.config.base_url or not self.config.api_key:
            raise ServiceError("API is not configured")
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        for attempt in range(3):
            remaining = deadline - time.monotonic() if deadline else self.config.timeout
            if remaining <= 0:
                raise ServiceError("Request deadline exceeded")
            request = urllib.request.Request(
                self.config.base_url + endpoint, data=data,
                headers={"Authorization": "Bearer " + self.config.api_key, "Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(request, timeout=min(self.config.timeout, remaining)) as response:
                    raw = response.read(16 * 1024 * 1024 + 1)
                if len(raw) > 16 * 1024 * 1024:
                    raise ServiceError("API response too large")
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise ServiceError("Invalid API response object")
                return payload
            except urllib.error.HTTPError as exc:
                status = exc.code
                exc.close()
                if status not in (408, 429, 500, 502, 503, 504) or attempt == 2:
                    raise ServiceError(f"API HTTP {status}") from None
            except (urllib.error.URLError, TimeoutError, socket.timeout, OSError):
                if attempt == 2:
                    raise ServiceError("API connection failed") from None
            except (ValueError, UnicodeError):
                raise ServiceError("Invalid API JSON") from None
            pause = 0.5 * (2 ** attempt)
            if deadline and time.monotonic() + pause >= deadline:
                raise ServiceError("Request deadline exceeded")
            time.sleep(pause)

    def embeddings(self, texts, deadline=None):
        result = self.post("/embeddings", {"model": self.config.embedding_model, "input": texts}, deadline)
        try:
            vectors = [None] * len(texts)
            for item in result["data"]:
                position = int(item["index"])
                if not 0 <= position < len(texts) or vectors[position] is not None:
                    raise ValueError()
                vector = [float(x) for x in item["embedding"]]
                norm = math.sqrt(sum(x*x for x in vector))
                if not vector or not math.isfinite(norm) or norm == 0:
                    raise ValueError()
                vectors[position] = [x / norm for x in vector]
            if any(v is None for v in vectors) or len({len(v) for v in vectors}) != 1:
                raise ValueError()
            return vectors
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ServiceError("Invalid embeddings response") from None

    def generate(self, messages, deadline=None):
        result = self.post("/chat/completions", {
            "model": self.config.model, "messages": messages,
            "temperature": self.config.temperature, "max_tokens": self.config.max_tokens,
        }, deadline)
        try:
            choice = result["choices"][0]
            text = choice["message"]["content"]
            if not isinstance(text, str) or not text.strip():
                raise ValueError()
            if choice.get("finish_reason") == "length":
                raise ServiceError("Generation exceeded token budget")
            return text.strip()
        except (KeyError, IndexError, TypeError, ValueError):
            raise ServiceError("Invalid generation response") from None
