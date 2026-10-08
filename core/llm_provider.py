import urllib.error
import urllib.request
import json
import logging
from pathlib import Path
from typing import Any
import threading

import yaml
import time

LOGGER = logging.getLogger("rif.llm_provider")
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"
DEFAULT_MODEL = "gemma"
DEFAULT_ENDPOINT = "http://localhost:11434/api/generate"
DEFAULT_TIMEOUT = 30
DEFAULT_RETRIES = 1

# Limit LLM concurrency to 2 simultaneous requests globally
_LLM_SEMAPHORE = threading.Semaphore(2)

def _configure_logging() -> None:
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        )

def _load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        LOGGER.warning("Config file %s not found. Using defaults.", CONFIG_PATH)
        return {}

    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except OSError as exc:
        LOGGER.warning("Could not read config file %s: %s", CONFIG_PATH, exc)
        return {}
    except yaml.YAMLError as exc:
        LOGGER.warning("Invalid YAML in %s: %s", CONFIG_PATH, exc)
        return {}

    if not isinstance(data, dict):
        LOGGER.warning("Expected %s to contain a key/value mapping. Using defaults.", CONFIG_PATH)
        return {}

    return data

def _settings() -> dict[str, Any]:
    config = _load_config()
    import os
    timeout = int(config.get("timeout", DEFAULT_TIMEOUT))
    if "RIF_LLM_TIMEOUT" in os.environ:
        timeout = max(1, int(os.environ["RIF_LLM_TIMEOUT"]))
    else:
        timeout = min(timeout, 120)
    # OLLAMA_HOST env var (set by docker-compose) overrides config/default
    ollama_host = os.environ.get("OLLAMA_HOST", "").strip()
    if ollama_host:
        endpoint = f"{ollama_host.rstrip('/')}/api/generate"
    else:
        endpoint = str(config.get("endpoint", DEFAULT_ENDPOINT))

    return {
        "model": str(config.get("model", DEFAULT_MODEL)),
        "endpoint": endpoint,
        "timeout": timeout,
        "retries": max(int(config.get("retries", DEFAULT_RETRIES)), 1),
    }

def _send_request(payload: dict[str, Any], endpoint: str, timeout: int) -> dict[str, Any]:
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with _LLM_SEMAPHORE:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")

    if not body.strip():
        raise RuntimeError("Ollama returned an empty response body.")

    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Ollama returned invalid JSON: {exc}") from exc

    return data

def generate(prompt: str, model: str | None = None) -> str:
    """Generate text using the configured local Ollama model."""
    _configure_logging()

    if not prompt or not prompt.strip():
        raise ValueError("Prompt must not be empty.")

    settings = _settings()
    selected_model = model or settings["model"]
    payload = {
        "model": selected_model,
        "prompt": prompt.strip(),
        "stream": False,
    }

    last_error: Exception | None = None
    for attempt in range(1, settings["retries"] + 1):
        LOGGER.info(
            "Sending request to Ollama model '%s' at %s (attempt %s/%s)",
            selected_model,
            settings["endpoint"],
            attempt,
            settings["retries"],
        )
        try:
            result = _send_request(payload, settings["endpoint"], settings["timeout"])
            text = str(result.get("response", "")).strip()
            if not text:
                raise RuntimeError("Ollama response did not contain generated text.")
            return text
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace").strip()
            message = f"Ollama returned HTTP {exc.code}: {exc.reason}"
            if details:
                message = f"{message} | {details}"
            last_error = RuntimeError(message)
            LOGGER.error("HTTP error from Ollama: %s", last_error)
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            last_error = RuntimeError(f"Unable to connect to Ollama at {settings['endpoint']}: {reason}")
            LOGGER.error("Connection error from Ollama: %s", last_error)
        except TimeoutError:
            last_error = RuntimeError(f"Request to Ollama timed out after {settings['timeout']} seconds.")
            LOGGER.error("Timeout talking to Ollama: %s", last_error)
        except RuntimeError as exc:
            last_error = exc
            LOGGER.error("Invalid Ollama response: %s", exc)
        
        if attempt < settings["retries"]:
            LOGGER.info("Retrying in %s seconds...", 2 ** attempt)
            time.sleep(2 ** attempt)

    if last_error is None:
        raise RuntimeError("Generation failed for an unknown reason.")
    raise last_error

def test_connection() -> str:
    """Run a simple connectivity test against Ollama."""
    prompt = "Explain blockchain in one sentence"
    try:
        return generate(prompt)
    except Exception as exc:
        LOGGER.warning("Ollama connectivity test failed: %s", exc)
        return f"Connection test failed: {exc}"

if __name__ == "__main__":
    print(test_connection())
