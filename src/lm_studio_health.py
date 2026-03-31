import os

from openai import OpenAI

LM_STUDIO_BASE_URL = os.getenv("LM_STUDIO_BASE_URL", "http://localhost:1234/v1")
LM_STUDIO_API_KEY = os.getenv("LM_STUDIO_API_KEY", "lm-studio")


def ensure_lm_studio_ready() -> None:
    client = OpenAI(base_url=LM_STUDIO_BASE_URL, api_key=LM_STUDIO_API_KEY)
    try:
        _ = client.models.list()
    except Exception as exc:
        raise RuntimeError(
            "LM Studio server is not reachable. Start LM Studio Developer server and verify LM_STUDIO_BASE_URL."
        ) from exc
