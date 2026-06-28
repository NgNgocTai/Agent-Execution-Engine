import ast
import json
import logging

logger = logging.getLogger(__name__)

THINK_OPEN_TAG = "<think>"
THINK_CLOSE_TAG = "</think>"


def _split_keep_suffix(buffer: str, marker: str) -> tuple[str, str]:
    max_len = min(len(buffer), len(marker) - 1)
    for size in range(max_len, 0, -1):
        suffix = buffer[-size:]
        if marker.startswith(suffix):
            return buffer[:-size], suffix
    return buffer, ""


class ThinkTagParser:
    def __init__(self) -> None:
        self._buffer = ""
        self._in_think = False

    def feed(self, text: str) -> list[tuple[str, str]]:
        self._buffer += text
        parts: list[tuple[str, str]] = []

        while self._buffer:
            if self._in_think:
                idx = self._buffer.find(THINK_CLOSE_TAG)
                if idx >= 0:
                    thinking = self._buffer[:idx]
                    if thinking:
                        parts.append(("thinking", thinking))
                    self._buffer = self._buffer[idx + len(THINK_CLOSE_TAG):]
                    self._in_think = False
                    continue

                emit, keep = _split_keep_suffix(self._buffer, THINK_CLOSE_TAG)
                if emit:
                    parts.append(("thinking", emit))
                self._buffer = keep
                break

            idx = self._buffer.find(THINK_OPEN_TAG)
            if idx >= 0:
                text_part = self._buffer[:idx]
                if text_part:
                    parts.append(("text", text_part))
                self._buffer = self._buffer[idx + len(THINK_OPEN_TAG):]
                self._in_think = True
                continue

            emit, keep = _split_keep_suffix(self._buffer, THINK_OPEN_TAG)
            if emit:
                parts.append(("text", emit))
            self._buffer = keep
            break

        return parts

    def flush(self) -> list[tuple[str, str]]:
        if not self._buffer:
            return []

        kind = "thinking" if self._in_think else "text"
        content = self._buffer
        self._buffer = ""
        self._in_think = False
        return [(kind, content)]


def get_reasoning_delta(delta) -> str | None:
    reasoning = getattr(delta, "reasoning", None)
    if reasoning:
        return reasoning

    reasoning_content = getattr(delta, "reasoning_content", None)
    if reasoning_content:
        return reasoning_content

    model_extra = getattr(delta, "model_extra", None)
    if isinstance(model_extra, dict):
        return model_extra.get("reasoning") or model_extra.get("reasoning_content")

    return None


def parse_tool_arguments(arguments_str: str, tool_name: str = "") -> dict:
    if not arguments_str:
        return {}

    try:
        parsed = json.loads(arguments_str)
    except json.JSONDecodeError as e:
        logger.warning("Malformed tool arguments for '%s': %s", tool_name, e)
        return {}

    if not isinstance(parsed, dict):
        logger.warning(
            "Tool arguments for '%s' must be a JSON object, got %s",
            tool_name,
            type(parsed).__name__,
        )
        return {}

    return parsed


def _extract_error_body(error: Exception):
    body = getattr(error, "body", None)
    if body:
        return body

    text = str(error)
    if " - " not in text:
        return None

    _, raw_body = text.split(" - ", 1)
    try:
        return ast.literal_eval(raw_body)
    except (SyntaxError, ValueError):
        return None


def _extract_provider_error_message(error: Exception) -> str:
    body = _extract_error_body(error)
    if isinstance(body, dict):
        err = body.get("error", body)
        if isinstance(err, dict):
            metadata = err.get("metadata")
            if isinstance(metadata, dict) and metadata.get("raw"):
                return str(metadata["raw"])
            if err.get("message"):
                return str(err["message"])
        if body.get("message"):
            return str(body["message"])

    return str(error)


def format_provider_error(
    error: Exception,
    provider_name: str,
    model_name: str | None = None,
) -> str:
    status_code = getattr(error, "status_code", None)
    raw_message = _extract_provider_error_message(error)
    lowered = raw_message.lower()
    model_suffix = f" ({model_name})" if model_name else ""

    if status_code == 429 or "rate limit" in lowered or "rate-limited" in lowered:
        return (
            f"{provider_name}{model_suffix} đang bị giới hạn tốc độ từ upstream. "
            "Vui lòng thử lại sau hoặc đổi provider/model/API key."
        )

    if status_code in (401, 403) or "api key" in lowered or "unauthorized" in lowered:
        return (
            f"{provider_name}{model_suffix} chưa xác thực được. "
            "Vui lòng kiểm tra API key/base URL."
        )

    return f"Lỗi kết nối {provider_name}{model_suffix}: {raw_message}"
