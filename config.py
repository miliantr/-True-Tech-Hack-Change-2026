"""Shared configuration; no credentials are stored in the submission."""
import os
from dataclasses import dataclass


def env(*names, default=""):
    for name in names:
        value = os.getenv(name, "").strip()
        if value and value != name:
            return value
    return default


def number(name, default, minimum, maximum, cast=int):
    try:
        return max(minimum, min(maximum, cast(env(name, default=str(default)))))
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    embedding_model: str = "baai/bge-m3"
    reranker_model: str = "qwen/qwen3-reranker-8b"
    retrieval: str = "lexical"
    rerank: bool = False
    top_k: int = 16
    top_n: int = 6
    max_tokens: int = 2048
    temperature: float = 0.0
    workers: int = 3
    timeout: float = 45
    runtime: int = 1080

    @property
    def live(self):
        return bool(self.base_url and self.api_key and self.model)

    @classmethod
    def from_env(cls):
        rerank = env("USE_RERANKER", default="0") == "1"
        return cls(
            base_url=env("LLM_BASE_URL", "OPENAI_BASE_URL", "BASE_URL").rstrip("/"),
            api_key=env("LLM_API_KEY", "OPENAI_API_KEY"),
            model=env("LLM_MODEL", "OPENAI_MODEL", "GEN_MODEL"),
            embedding_model=env("EMB_MODEL", default="baai/bge-m3"),
            reranker_model=env("RERANKER_MODEL", default="qwen/qwen3-reranker-8b"),
            retrieval=env("RETRIEVAL_MODE", default="lexical"),
            rerank=rerank,
            top_k=number("TOP_K", 16, 1, 40),
            top_n=number("RERANKER_TOP_N", 6, 1, 12) if rerank else cls.top_n,
            max_tokens=number("MAX_TOKENS", 2048, 128, 8192),
            temperature=number("TEMPERATURE", 0, 0, 2, float),
            workers=number("MAX_WORKERS", 3, 1, 8),
            timeout=number("API_TIMEOUT", 45, 1, 120, float),
            runtime=number("RUN_BUDGET_SECONDS", 1080, 10, 1140),
        )
