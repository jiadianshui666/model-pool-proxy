"""
Model Pool Proxy for Claude Code.

The proxy accepts Anthropic-compatible requests from Claude Code, rewrites the
requested model to the current upstream model, and moves to the next configured
model only when the current one is exhausted or unavailable.
"""

import argparse
import copy
import json
import logging
import os
import sys
import time
import traceback
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import RLock
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse
import urllib.error
import urllib.request


def _configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_configure_console()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("model-pool-proxy")


@dataclass
class ProxyConfig:
    api_key: str
    base_url: str
    models: List[str]
    api_format: str = "auto"
    timeout_seconds: int = 300
    api_key_env: str = "MODEL_POOL_API_KEY"


@dataclass
class ProxyResponse:
    status: int
    body: bytes
    content_type: str = "application/json"


CONFIG: Optional[ProxyConfig] = None
CONFIG_PATH: Optional[str] = None
STATE_PATH: Optional[str] = None
CONFIG_LOCK = RLock()
pool: Optional["ModelPool"] = None


class ModelPool:
    """Sequential model pool with manual and automatic failover."""

    def __init__(self, models: List[str], state_path: Optional[str] = None, persist: bool = True):
        clean_models: List[str] = []
        seen = set()
        for model in models:
            name = str(model).strip()
            if not name or name in seen:
                continue
            clean_models.append(name)
            seen.add(name)

        if not clean_models:
            raise ValueError("models cannot be empty")

        self.models = clean_models
        self.available = set(clean_models)
        self.used_tokens = {m: 0 for m in clean_models}
        self.total_requests = {m: 0 for m in clean_models}
        self.disabled_reasons: Dict[str, str] = {}
        self.transient_disabled = set()
        self.current_model_index = 0
        self.switch_count = 0
        self.events: List[Dict[str, Any]] = []
        self.state_path = state_path
        self._persist_enabled = persist
        self._lock = RLock()
        self._load_state()
        self._record_locked("started", self.current(), "proxy started")
        self._save_state_locked()

    @property
    def total_models(self) -> int:
        return len(self.models)

    @property
    def active_models(self) -> int:
        return len(self.available)

    def _find_available_index_locked(self, start: int, skip: Optional[str] = None) -> int:
        if not self.available:
            raise RuntimeError("no available models")

        for offset in range(len(self.models)):
            idx = (start + offset) % len(self.models)
            model = self.models[idx]
            if model in self.available and model != skip:
                return idx

        raise RuntimeError("no available models after applying skip")

    def current(self) -> str:
        with self._lock:
            idx = self.current_model_index
            if self.models[idx] not in self.available:
                idx = self._find_available_index_locked(idx)
                self.current_model_index = idx
            return self.models[self.current_model_index]

    def advance(self, skip: Optional[str] = None) -> str:
        with self._lock:
            start = (self.current_model_index + 1) % len(self.models)
            idx = self._find_available_index_locked(start, skip=skip)
            old = self.models[self.current_model_index]
            self.current_model_index = idx
            new = self.models[idx]
            if old != new:
                self.switch_count += 1
                self._record_locked("switch", new, f"{old} -> {new}", previous=old)
                self._save_state_locked()
                log.info("[switch] %s -> %s", old, new)
            return new

    def disable(self, model: str, reason: str, persist: bool = True) -> None:
        with self._lock:
            if model not in self.models:
                return
            if model in self.available:
                self.available.remove(model)
                self.disabled_reasons[model] = reason[:120]
                if persist:
                    self.transient_disabled.discard(model)
                else:
                    self.transient_disabled.add(model)
                self._record_locked("disabled", model, reason[:120])
                log.warning("[disabled] %s | %s", model, reason[:120])
            if self.available and self.models[self.current_model_index] == model:
                old = model
                self.current_model_index = self._find_available_index_locked(
                    (self.current_model_index + 1) % len(self.models)
                )
                self.switch_count += 1
                new = self.models[self.current_model_index]
                self._record_locked("switch", new, f"{old} -> {new}", previous=old)
                log.info("[current] %s", new)
            if persist:
                self._save_state_locked()

    def enable(self, model: str) -> None:
        model = str(model).strip()
        with self._lock:
            if model not in self.models:
                raise ValueError(f"unknown model: {model}")
            self.available.add(model)
            self.disabled_reasons.pop(model, None)
            self.transient_disabled.discard(model)
            self._record_locked("enabled", model, "manual enable")
            self._save_state_locked()
            log.info("[enabled] %s", model)

    def reset_usage(self) -> None:
        with self._lock:
            for model in self.models:
                self.used_tokens[model] = 0
                self.total_requests[model] = 0
            self._record_locked("reset", self.current(), "usage counters reset")
            self._save_state_locked()

    def enable_all(self) -> None:
        with self._lock:
            self.available = set(self.models)
            self.disabled_reasons.clear()
            self.transient_disabled.clear()
            self._record_locked("enabled_all", self.current(), "all models enabled")
            self._save_state_locked()
            log.info("[enabled] all models")

    def mark_used(self, model: str, tokens: int) -> None:
        if tokens <= 0:
            return
        with self._lock:
            if model in self.used_tokens:
                self.used_tokens[model] += tokens
                self.total_requests[model] += 1
                self._save_state_locked()

    def status_data(self) -> Dict[str, Any]:
        with self._lock:
            current = self.current() if self.available else None
            return {
                "active_models": self.active_models,
                "total_models": self.total_models,
                "current_model": current,
                "switch_count": self.switch_count,
                "total_used_tokens": sum(self.used_tokens.values()),
                "config": public_config(),
                "events": list(reversed(self.events[-80:])),
                "models": [
                    {
                        "id": model,
                        "available": model in self.available,
                        "used_tokens": self.used_tokens.get(model, 0),
                        "requests": self.total_requests.get(model, 0),
                        "disabled_reason": self.disabled_reasons.get(model, ""),
                    }
                    for model in self.models
                ],
            }

    def status_text(self) -> str:
        data = self.status_data()
        lines = [
            "=" * 92,
            f"{'STATE':<7} {'MODEL':<42} {'TOKENS':>12} {'REQS':>6} REASON",
            "-" * 92,
        ]
        for item in data["models"]:
            state = "[OK]" if item["available"] else "[OFF]"
            lines.append(
                f"{state:<7} {item['id']:<42} {item['used_tokens']:>12,} "
                f"{item['requests']:>6} {item['disabled_reason']}"
            )
        lines.extend(
            [
                "-" * 92,
                "Active: {}/{} | Current: {} | Used: {:,} tokens | Switches: {}".format(
                    data["active_models"],
                    data["total_models"],
                    data["current_model"] or "<none>",
                    data["total_used_tokens"],
                    data["switch_count"],
                ),
                "=" * 92,
            ]
        )
        return "\n".join(lines)

    def _record_locked(
        self,
        event_type: str,
        model: Optional[str],
        message: str,
        previous: Optional[str] = None,
    ) -> None:
        self.events.append(
            {
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "type": event_type,
                "model": model,
                "previous": previous,
                "message": message,
            }
        )
        if len(self.events) > 200:
            self.events = self.events[-200:]

    def _state_data_locked(self) -> Dict[str, Any]:
        current = self.models[self.current_model_index] if self.models else None
        return {
            "version": 1,
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "models": list(self.models),
            "current_model": current,
            "switch_count": self.switch_count,
            "used_tokens": dict(self.used_tokens),
            "total_requests": dict(self.total_requests),
            "disabled_reasons": {
                model: reason
                for model, reason in self.disabled_reasons.items()
                if model not in self.transient_disabled
            },
            "events": list(self.events[-200:]),
        }

    def _save_state_locked(self) -> None:
        if not self._persist_enabled or not self.state_path:
            return
        try:
            state_dir = os.path.dirname(self.state_path)
            if state_dir:
                os.makedirs(state_dir, exist_ok=True)
            temp_path = self.state_path + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as fh:
                json.dump(self._state_data_locked(), fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            os.replace(temp_path, self.state_path)
        except Exception as exc:
            log.warning("[state] save failed: %s", exc)

    def _load_state(self) -> None:
        if not self._persist_enabled or not self.state_path or not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                state = json.load(fh)
        except Exception as exc:
            log.warning("[state] load failed: %s", exc)
            return

        if not isinstance(state, dict):
            return

        known = set(self.models)
        disabled = state.get("disabled_reasons") or {}
        if isinstance(disabled, dict):
            self.disabled_reasons = {
                str(model): str(reason)[:120]
                for model, reason in disabled.items()
                if str(model) in known
            }
            self.available = set(self.models) - set(self.disabled_reasons.keys())
            if not self.available:
                self.available = set(self.models)
                self.disabled_reasons = {}

        used = state.get("used_tokens") or {}
        if isinstance(used, dict):
            for model in self.models:
                try:
                    self.used_tokens[model] = int(used.get(model, 0) or 0)
                except Exception:
                    self.used_tokens[model] = 0

        requests = state.get("total_requests") or {}
        if isinstance(requests, dict):
            for model in self.models:
                try:
                    self.total_requests[model] = int(requests.get(model, 0) or 0)
                except Exception:
                    self.total_requests[model] = 0

        try:
            self.switch_count = int(state.get("switch_count") or 0)
        except Exception:
            self.switch_count = 0

        events = state.get("events")
        if isinstance(events, list):
            self.events = [event for event in events[-200:] if isinstance(event, dict)]

        current = str(state.get("current_model") or "").strip()
        if current in self.available:
            self.current_model_index = self.models.index(current)
        else:
            self.current_model_index = self._find_available_index_locked(0)


def _json_bytes(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _parse_json_bytes(raw: bytes) -> Any:
    return json.loads(raw.decode("utf-8-sig", errors="replace"))


def _extract_usage(obj: Any) -> int:
    if not isinstance(obj, dict):
        return 0

    usage = obj.get("usage")
    if isinstance(usage, dict):
        input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or 0)
        return total_tokens or input_tokens + output_tokens

    return 0


def _extract_sse_usage(raw: bytes) -> int:
    total = 0
    text = raw.decode("utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            total = max(total, _extract_usage(json.loads(payload)))
        except Exception:
            continue
    return total


def _error_fields(error_body: Any) -> Tuple[str, str, str]:
    if isinstance(error_body, dict):
        err = error_body.get("error", error_body)
        if isinstance(err, dict):
            code = str(err.get("code") or err.get("type") or "")
            message = str(err.get("message") or err.get("msg") or "")
        else:
            code = ""
            message = str(err)
        full_text = json.dumps(error_body, ensure_ascii=False)
        return code, message, full_text
    return "", str(error_body), str(error_body)


def _is_auth_error(status: int, error_body: Any) -> bool:
    code, message, full_text = _error_fields(error_body)
    text = f"{code} {message} {full_text}".lower()
    signals = [
        "invalidapikey",
        "invalid api key",
        "incorrect api key",
        "authentication",
        "unauthorized",
        "permission denied",
    ]
    return status == 401 or any(signal in text for signal in signals)


def _is_quota_error(status: int, error_body: Any) -> bool:
    code, message, full_text = _error_fields(error_body)
    text = f"{code} {message} {full_text}".lower()
    signals = [
        "quota",
        "free quota",
        "exhausted",
        "insufficient",
        "rate limit",
        "ratelimit",
        "too many requests",
        "余额",
        "额度",
        "配额",
        "用完",
    ]
    return status in (402, 403, 429) and any(signal in text for signal in signals)


def _is_model_unavailable(status: int, error_body: Any) -> bool:
    code, message, full_text = _error_fields(error_body)
    text = f"{code} {message} {full_text}".lower()
    signals = [
        "model not found",
        "model_not_found",
        "invalid model",
        "not support",
        "not supported",
        "does not exist",
        "模型不存在",
        "不支持",
    ]
    return status in (400, 404) and any(signal in text for signal in signals)


def _short_reason(error_body: Any, default: str) -> str:
    code, message, _ = _error_fields(error_body)
    return (code or message or default).replace("\n", " ")[:120]


def _build_upstream_url(base_url: str, path: str) -> str:
    base = base_url.rstrip("/")
    if "?" in path:
        path_part, query = path.split("?", 1)
        query_suffix = "?" + query
    else:
        path_part = path
        query_suffix = ""
    clean_path = "/" + path_part.lstrip("/")
    base_path = urlparse(base).path.rstrip("/").lower()
    if base_path.endswith(("/messages", "/chat/completions", "/completions", "/responses")):
        return base + query_suffix
    if base.endswith("/v1") and clean_path.startswith("/v1/"):
        clean_path = clean_path[3:]
    return base + clean_path + query_suffix


def _forward_headers(incoming_headers: Any, api_key: str) -> Dict[str, str]:
    outgoing = {"Content-Type": "application/json"}
    if api_key:
        outgoing["Authorization"] = f"Bearer {api_key}"
        outgoing["x-api-key"] = api_key
    blocked = {
        "host",
        "content-length",
        "authorization",
        "x-api-key",
        "connection",
        "accept-encoding",
    }
    for key, value in incoming_headers.items():
        lower = key.lower()
        if lower in blocked:
            continue
        outgoing[key] = value
    return outgoing


def _incoming_format(path: str) -> str:
    clean = path.split("?", 1)[0].lower()
    if clean.endswith("/chat/completions") or clean.endswith("/completions") or clean.endswith("/responses"):
        return "openai"
    if clean.endswith("/messages"):
        return "anthropic"
    return "unknown"


def _resolve_upstream_format(config: ProxyConfig, path: str) -> str:
    if config.api_format in ("anthropic", "openai"):
        return config.api_format

    lowered = config.base_url.lower()
    parsed_path = urlparse(config.base_url).path.lower()
    request_format = _incoming_format(path)

    if "anthropic" in lowered or parsed_path.endswith("/messages"):
        return "anthropic"
    if (
        "openai" in lowered
        or "compatible-mode" in lowered
        or parsed_path.endswith("/chat/completions")
        or parsed_path.endswith("/completions")
        or parsed_path.endswith("/responses")
    ):
        return "openai"
    if parsed_path.endswith("/v1") or "/v1/" in parsed_path:
        return "openai"
    return "openai"


def _target_path(path: str, incoming_format: str, upstream_format: str) -> str:
    query = ""
    clean = path
    if "?" in path:
        clean, query = path.split("?", 1)
        query = "?" + query

    if incoming_format == upstream_format:
        return clean + query
    if upstream_format == "openai":
        return "/v1/chat/completions" + query
    if upstream_format == "anthropic":
        return "/v1/messages" + query
    return clean + query


def _flatten_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
                continue
            if not isinstance(block, dict):
                parts.append(str(block))
                continue
            block_type = block.get("type")
            if block_type == "text":
                parts.append(str(block.get("text", "")))
            elif block_type == "tool_result":
                tool_id = block.get("tool_use_id") or block.get("id") or "tool"
                parts.append(f"[tool_result {tool_id}]\n{_flatten_content(block.get('content'))}")
            elif block_type == "tool_use":
                name = block.get("name") or "tool"
                payload = json.dumps(block.get("input", {}), ensure_ascii=False)
                parts.append(f"[tool_use {name}]\n{payload}")
            elif block_type == "image":
                parts.append("[image]")
            else:
                parts.append(json.dumps(block, ensure_ascii=False))
        return "\n\n".join(part for part in parts if part)
    return str(content)


def _anthropic_tools_to_openai(tools: Any) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    if not isinstance(tools, list):
        return result
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        result.append(
            {
                "type": "function",
                "function": {
                    "name": tool.get("name", "tool"),
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
                },
            }
        )
    return result


def _openai_tools_to_anthropic(tools: Any) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    if not isinstance(tools, list):
        return result
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "function":
            fn = tool.get("function") or {}
            result.append(
                {
                    "name": fn.get("name", "tool"),
                    "description": fn.get("description", ""),
                    "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
                }
            )
    return result


def _anthropic_to_openai_body(body: Dict[str, Any], model: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {"model": model, "messages": []}
    system = body.get("system")
    if system:
        out["messages"].append({"role": "system", "content": _flatten_content(system)})

    for msg in body.get("messages", []):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "user")
        if role not in ("system", "user", "assistant", "tool"):
            role = "user"
        out["messages"].append({"role": role, "content": _flatten_content(msg.get("content"))})

    if not out["messages"]:
        out["messages"].append({"role": "user", "content": ""})

    passthrough = ["temperature", "top_p", "frequency_penalty", "presence_penalty", "seed", "stop"]
    for key in passthrough:
        if key in body:
            out[key] = body[key]
    if "max_tokens" in body:
        out["max_tokens"] = body["max_tokens"]
    if "tools" in body:
        tools = _anthropic_tools_to_openai(body["tools"])
        if tools:
            out["tools"] = tools
    return out


def _openai_to_anthropic_body(body: Dict[str, Any], model: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "model": model,
        "max_tokens": body.get("max_tokens") or body.get("max_completion_tokens") or 1024,
        "messages": [],
    }
    system_parts: List[str] = []
    for msg in body.get("messages", []):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "user")
        content = _flatten_content(msg.get("content"))
        if role == "system":
            system_parts.append(content)
        elif role in ("user", "assistant"):
            out["messages"].append({"role": role, "content": content})
        elif role == "tool":
            out["messages"].append({"role": "user", "content": f"[tool]\n{content}"})

    if system_parts:
        out["system"] = "\n\n".join(part for part in system_parts if part)
    if not out["messages"]:
        out["messages"].append({"role": "user", "content": ""})

    passthrough = ["temperature", "top_p", "stop"]
    for key in passthrough:
        if key in body:
            out[key] = body[key]
    if "tools" in body:
        tools = _openai_tools_to_anthropic(body["tools"])
        if tools:
            out["tools"] = tools
    return out


def _prepare_upstream_body(
    original_body: Dict[str, Any],
    model: str,
    incoming_format: str,
    upstream_format: str,
) -> Tuple[Dict[str, Any], bool]:
    requested_stream = bool(original_body.get("stream"))
    if incoming_format == "anthropic" and upstream_format == "openai":
        body = _anthropic_to_openai_body(original_body, model)
        body["stream"] = False
        return body, requested_stream
    if incoming_format == "openai" and upstream_format == "anthropic":
        body = _openai_to_anthropic_body(original_body, model)
        body["stream"] = False
        return body, requested_stream

    body = copy.deepcopy(original_body)
    body["model"] = model
    return body, requested_stream


def _map_anthropic_stop_to_openai(reason: Optional[str]) -> str:
    return {
        "end_turn": "stop",
        "max_tokens": "length",
        "stop_sequence": "stop",
        "tool_use": "tool_calls",
    }.get(reason or "", reason or "stop")


def _map_openai_stop_to_anthropic(reason: Optional[str]) -> str:
    return {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
        "function_call": "tool_use",
    }.get(reason or "", reason or "end_turn")


def _openai_response_to_anthropic(parsed: Dict[str, Any], model: str) -> Dict[str, Any]:
    choice = (parsed.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content: List[Dict[str, Any]] = []
    text = message.get("content")
    if text:
        content.append({"type": "text", "text": str(text)})
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        raw_args = fn.get("arguments") or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except Exception:
            args = {"arguments": raw_args}
        content.append(
            {
                "type": "tool_use",
                "id": call.get("id") or f"toolu_{len(content)}",
                "name": fn.get("name", "tool"),
                "input": args,
            }
        )
    usage = parsed.get("usage") or {}
    return {
        "id": parsed.get("id") or f"msg_{int(time.time())}",
        "type": "message",
        "role": "assistant",
        "model": parsed.get("model") or model,
        "content": content or [{"type": "text", "text": ""}],
        "stop_reason": _map_openai_stop_to_anthropic(choice.get("finish_reason")),
        "stop_sequence": None,
        "usage": {
            "input_tokens": int(usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
        },
    }


def _anthropic_response_to_openai(parsed: Dict[str, Any], model: str) -> Dict[str, Any]:
    text_parts: List[str] = []
    tool_calls: List[Dict[str, Any]] = []
    for block in parsed.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text_parts.append(str(block.get("text", "")))
        elif block.get("type") == "tool_use":
            tool_calls.append(
                {
                    "id": block.get("id") or f"call_{len(tool_calls)}",
                    "type": "function",
                    "function": {
                        "name": block.get("name", "tool"),
                        "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                    },
                }
            )
    message: Dict[str, Any] = {"role": "assistant", "content": "".join(text_parts) or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    usage = parsed.get("usage") or {}
    prompt = int(usage.get("input_tokens") or 0)
    completion = int(usage.get("output_tokens") or 0)
    return {
        "id": parsed.get("id") or f"chatcmpl-{int(time.time())}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": parsed.get("model") or model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": _map_anthropic_stop_to_openai(parsed.get("stop_reason")),
            }
        ],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        },
    }


def _anthropic_sse_from_message(message: Dict[str, Any]) -> bytes:
    blocks = message.get("content") or [{"type": "text", "text": ""}]
    usage = message.get("usage") or {}
    events: List[str] = []
    start = copy.deepcopy(message)
    start["content"] = []
    start["stop_reason"] = None
    events.append("event: message_start\ndata: " + json.dumps({"type": "message_start", "message": start}, ensure_ascii=False))
    for index, block in enumerate(blocks):
        events.append(
            "event: content_block_start\ndata: "
            + json.dumps({"type": "content_block_start", "index": index, "content_block": block}, ensure_ascii=False)
        )
        if block.get("type") == "text":
            events.append(
                "event: content_block_delta\ndata: "
                + json.dumps(
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {"type": "text_delta", "text": block.get("text", "")},
                    },
                    ensure_ascii=False,
                )
            )
        events.append(
            "event: content_block_stop\ndata: "
            + json.dumps({"type": "content_block_stop", "index": index}, ensure_ascii=False)
        )
    events.append(
        "event: message_delta\ndata: "
        + json.dumps(
            {
                "type": "message_delta",
                "delta": {"stop_reason": message.get("stop_reason"), "stop_sequence": None},
                "usage": {"output_tokens": int(usage.get("output_tokens") or 0)},
            },
            ensure_ascii=False,
        )
    )
    events.append("event: message_stop\ndata: " + json.dumps({"type": "message_stop"}, ensure_ascii=False))
    return ("\n\n".join(events) + "\n\n").encode("utf-8")


def _openai_sse_from_chat(chat: Dict[str, Any]) -> bytes:
    choice = (chat.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    model = chat.get("model", "")
    created = int(time.time())
    chat_id = chat.get("id") or f"chatcmpl-{created}"
    chunks = [
        {
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }
    ]
    if message.get("content"):
        chunks.append(
            {
                "id": chat_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {"content": message.get("content")}, "finish_reason": None}],
            }
        )
    chunks.append(
        {
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": choice.get("finish_reason") or "stop"}],
        }
    )
    lines = ["data: " + json.dumps(chunk, ensure_ascii=False) for chunk in chunks]
    lines.append("data: [DONE]")
    return ("\n\n".join(lines) + "\n\n").encode("utf-8")


def _normalize_success_response(
    raw: bytes,
    content_type: str,
    incoming_format: str,
    upstream_format: str,
    model: str,
    requested_stream: bool,
) -> Tuple[ProxyResponse, int]:
    if "application/json" not in content_type.lower() and not raw.lstrip().startswith(b"{"):
        token_count = _extract_sse_usage(raw) if "text/event-stream" in content_type.lower() else 0
        return ProxyResponse(200, raw, content_type), token_count

    parsed = _parse_json_bytes(raw)
    token_count = _extract_usage(parsed)
    if incoming_format == upstream_format or incoming_format == "unknown":
        return ProxyResponse(200, _json_bytes(parsed), "application/json"), token_count

    if upstream_format == "openai" and incoming_format == "anthropic":
        converted = _openai_response_to_anthropic(parsed, model)
        token_count = token_count or _extract_usage(converted)
        if requested_stream:
            return ProxyResponse(200, _anthropic_sse_from_message(converted), "text/event-stream; charset=utf-8"), token_count
        return ProxyResponse(200, _json_bytes(converted), "application/json"), token_count

    if upstream_format == "anthropic" and incoming_format == "openai":
        converted = _anthropic_response_to_openai(parsed, model)
        token_count = token_count or _extract_usage(converted)
        if requested_stream:
            return ProxyResponse(200, _openai_sse_from_chat(converted), "text/event-stream; charset=utf-8"), token_count
        return ProxyResponse(200, _json_bytes(converted), "application/json"), token_count

    return ProxyResponse(200, _json_bytes(parsed), "application/json"), token_count


def _should_try_next_model(status: int, error_body: Any) -> bool:
    if (
        _is_auth_error(status, error_body)
        or _is_quota_error(status, error_body)
        or _is_model_unavailable(status, error_body)
    ):
        return True
    if status in (408, 409, 429, 500, 502, 503, 504):
        return True
    return False


def _should_persist_failure(status: int, error_body: Any) -> bool:
    return (
        _is_auth_error(status, error_body)
        or _is_quota_error(status, error_body)
        or _is_model_unavailable(status, error_body)
    )


def forward_request(path: str, incoming_headers: Any, original_body: Dict[str, Any]) -> ProxyResponse:
    if CONFIG is None or pool is None:
        return ProxyResponse(503, _json_bytes({"error": {"message": "proxy is not initialized"}}))

    attempted = set()
    last_error: Any = None
    incoming_format = _incoming_format(path)
    upstream_format = _resolve_upstream_format(CONFIG, path)
    upstream_path = _target_path(path, incoming_format, upstream_format)

    while len(attempted) < pool.total_models:
        try:
            model = pool.current()
        except RuntimeError as exc:
            return ProxyResponse(503, _json_bytes({"error": {"message": str(exc), "type": "no_model"}}))

        if model in attempted:
            try:
                model = pool.advance(skip=model)
            except RuntimeError:
                break

        attempted.add(model)
        body, requested_stream = _prepare_upstream_body(original_body, model, incoming_format, upstream_format)
        original_model = original_body.get("model")
        if original_model != model:
            log.info(
                "[route] %s -> %s | client=%s upstream=%s",
                original_model or "<missing>",
                model,
                incoming_format,
                upstream_format,
            )

        url = _build_upstream_url(CONFIG.base_url, upstream_path)
        req_headers = _forward_headers(incoming_headers, CONFIG.api_key)
        data = _json_bytes(body)

        try:
            req = urllib.request.Request(url, data=data, method="POST")
            for key, value in req_headers.items():
                req.add_header(key, value)

            with urllib.request.urlopen(req, timeout=CONFIG.timeout_seconds) as resp:
                raw = resp.read()
                content_type = resp.headers.get("Content-Type", "application/json")
                proxy_response, tokens = _normalize_success_response(
                    raw,
                    content_type,
                    incoming_format,
                    upstream_format,
                    model,
                    requested_stream,
                )
                pool.mark_used(model, tokens)
                return ProxyResponse(resp.status, proxy_response.body, proxy_response.content_type)

        except urllib.error.HTTPError as exc:
            raw = exc.read()
            content_type = exc.headers.get("Content-Type", "application/json")
            try:
                error_body = _parse_json_bytes(raw)
            except Exception:
                error_body = {"error": raw.decode("utf-8", errors="replace")}
            last_error = error_body

            if _should_try_next_model(exc.code, error_body):
                pool.disable(
                    model,
                    _short_reason(error_body, f"http_{exc.code}"),
                    persist=_should_persist_failure(exc.code, error_body),
                )
                continue

            if "application/json" in content_type.lower() or isinstance(error_body, dict):
                return ProxyResponse(exc.code, _json_bytes(error_body), "application/json")
            return ProxyResponse(exc.code, raw, content_type)

        except Exception as exc:
            log.error("[upstream error] %s\n%s", exc, traceback.format_exc())
            last_error = {"error": {"message": str(exc), "type": "upstream_error"}}
            pool.disable(model, f"upstream_error: {str(exc)[:100]}", persist=False)
            continue

    return ProxyResponse(
        503,
        _json_bytes(
            {
                "error": {
                    "message": "all configured models are unavailable",
                    "type": "all_models_unavailable",
                    "last_error": last_error,
                }
            }
        ),
        "application/json",
    )


def dashboard_html() -> str:
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Model Pool Proxy Status</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f5f7fb;
      --panel: #ffffff;
      --text: #172033;
      --muted: #697386;
      --line: #d9e0ea;
      --ok: #117a55;
      --off: #b42318;
      --accent: #275cd3;
      --warn: #8a5a00;
      --shadow: 0 1px 2px rgba(23, 32, 51, 0.08);
    }

    * {
      box-sizing: border-box;
    }

    body {
      margin: 0;
      min-width: 320px;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.45 "Segoe UI", "Microsoft YaHei", Arial, sans-serif;
      letter-spacing: 0;
    }

    header {
      background: var(--panel);
      border-bottom: 1px solid var(--line);
      box-shadow: var(--shadow);
    }

    .wrap {
      width: min(1180px, calc(100% - 32px));
      margin: 0 auto;
    }

    .topbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      min-height: 68px;
    }

    h1 {
      margin: 0;
      font-size: 20px;
      font-weight: 650;
    }

    .subtle {
      color: var(--muted);
      font-size: 12px;
    }

    .controls {
      display: flex;
      align-items: center;
      justify-content: flex-end;
      flex-wrap: wrap;
      gap: 8px;
    }

    button,
    .toggle {
      min-height: 34px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--text);
      padding: 0 12px;
      font: inherit;
      cursor: pointer;
      white-space: nowrap;
    }

    button.primary {
      border-color: var(--accent);
      background: var(--accent);
      color: #fff;
    }

    button.warn {
      border-color: #e7c46b;
      background: #fff8df;
      color: #5f4100;
    }

    button.danger {
      border-color: #f0b8b8;
      background: #fff5f5;
      color: var(--off);
    }

    button:disabled {
      opacity: 0.55;
      cursor: wait;
    }

    .toggle {
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }

    main {
      padding: 22px 0 32px;
    }

    .metrics {
      display: grid;
      grid-template-columns: repeat(5, minmax(140px, 1fr));
      gap: 12px;
      margin-bottom: 16px;
    }

    .metric,
    section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 6px;
      box-shadow: var(--shadow);
    }

    .metric {
      padding: 14px;
      min-height: 82px;
    }

    .metric .label {
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 6px;
    }

    .metric .value {
      font-size: 22px;
      font-weight: 700;
      overflow-wrap: anywhere;
    }

    .metric .value.small {
      font-size: 16px;
      line-height: 1.25;
    }

    .grid {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 340px;
      gap: 16px;
      align-items: start;
    }

    section {
      overflow: hidden;
    }

    .section-head {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
      background: #fafcff;
    }

    h2 {
      margin: 0;
      font-size: 15px;
      font-weight: 650;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
    }

    th,
    td {
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: middle;
    }

    th {
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
      background: #fff;
    }

    tbody tr.current {
      background: #eef4ff;
    }

    .model-name,
    .reason,
    .event-message {
      overflow-wrap: anywhere;
    }

    .num {
      text-align: right;
      font-variant-numeric: tabular-nums;
    }

    .pill {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 58px;
      height: 24px;
      border-radius: 999px;
      padding: 0 8px;
      font-size: 12px;
      font-weight: 650;
    }

    .pill.ok {
      background: #e9f7f1;
      color: var(--ok);
    }

    .pill.off {
      background: #fff0f0;
      color: var(--off);
    }

    .events {
      display: flex;
      flex-direction: column;
      max-height: 560px;
      overflow: auto;
    }

    .event {
      padding: 11px 14px;
      border-bottom: 1px solid var(--line);
    }

    .event-top {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 4px;
      color: var(--muted);
      font-size: 12px;
    }

    .event-type {
      color: var(--accent);
      font-weight: 650;
      text-transform: uppercase;
    }

    .empty {
      padding: 18px 14px;
      color: var(--muted);
    }

    .status-line {
      min-height: 18px;
      color: var(--muted);
      font-size: 12px;
      margin-top: 4px;
    }

    @media (max-width: 920px) {
      .metrics {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }

      .grid {
        grid-template-columns: 1fr;
      }

      .topbar {
        align-items: flex-start;
        flex-direction: column;
        padding: 14px 0;
      }

      .controls {
        justify-content: flex-start;
      }
    }

    @media (max-width: 620px) {
      .wrap {
        width: min(100% - 20px, 1180px);
      }

      .metrics {
        grid-template-columns: 1fr;
      }

      th:nth-child(4),
      td:nth-child(4),
      th:nth-child(5),
      td:nth-child(5) {
        display: none;
      }

      button,
      .toggle {
        flex: 1 1 auto;
      }
    }
  </style>
</head>
<body>
  <header>
    <div class="wrap topbar">
      <div>
        <h1>Model Pool Proxy</h1>
        <div id="updated" class="status-line">Loading</div>
      </div>
      <div class="controls">
        <button class="primary" id="refreshBtn" type="button">Refresh</button>
        <button id="configBtn" type="button">Config</button>
        <button id="nextBtn" type="button">Next Model</button>
        <button id="enableAllBtn" type="button">Enable All</button>
        <button class="warn" id="resetBtn" type="button">Reset Counters</button>
        <label class="toggle"><input id="autoRefresh" type="checkbox" checked> Auto</label>
      </div>
    </div>
  </header>

  <main class="wrap">
    <div class="metrics">
      <div class="metric"><div class="label">Current Model</div><div class="value small" id="mCurrent">-</div></div>
      <div class="metric"><div class="label">Active Models</div><div class="value" id="mActive">-</div></div>
      <div class="metric"><div class="label">Disabled</div><div class="value" id="mDisabled">-</div></div>
      <div class="metric"><div class="label">Used Tokens</div><div class="value" id="mTokens">-</div></div>
      <div class="metric"><div class="label">Switches</div><div class="value" id="mSwitches">-</div></div>
    </div>

    <div class="grid">
      <section>
        <div class="section-head">
          <h2>Models</h2>
          <span class="subtle" id="modelSummary">-</span>
        </div>
        <table>
          <thead>
            <tr>
              <th style="width: 82px;">State</th>
              <th>Model</th>
              <th style="width: 112px;" class="num">Tokens</th>
              <th style="width: 80px;" class="num">Reqs</th>
              <th>Reason</th>
              <th style="width: 94px;">Action</th>
            </tr>
          </thead>
          <tbody id="modelsBody"></tbody>
        </table>
      </section>

      <section>
        <div class="section-head">
          <h2>Switch History</h2>
          <span class="subtle" id="eventSummary">-</span>
        </div>
        <div class="events" id="events"></div>
      </section>
    </div>
  </main>

  <script>
    const els = {
      updated: document.getElementById('updated'),
      refreshBtn: document.getElementById('refreshBtn'),
      nextBtn: document.getElementById('nextBtn'),
      enableAllBtn: document.getElementById('enableAllBtn'),
      resetBtn: document.getElementById('resetBtn'),
      configBtn: document.getElementById('configBtn'),
      autoRefresh: document.getElementById('autoRefresh'),
      mCurrent: document.getElementById('mCurrent'),
      mActive: document.getElementById('mActive'),
      mDisabled: document.getElementById('mDisabled'),
      mTokens: document.getElementById('mTokens'),
      mSwitches: document.getElementById('mSwitches'),
      modelSummary: document.getElementById('modelSummary'),
      eventSummary: document.getElementById('eventSummary'),
      modelsBody: document.getElementById('modelsBody'),
      events: document.getElementById('events')
    };

    const fmt = new Intl.NumberFormat('en-US');
    let busy = false;

    function text(value) {
      return value === null || value === undefined || value === '' ? '-' : String(value);
    }

    function setBusy(value) {
      busy = value;
      for (const button of [els.refreshBtn, els.nextBtn, els.enableAllBtn, els.resetBtn]) {
        button.disabled = value;
      }
    }

    async function api(path, options = {}) {
      const res = await fetch(path, { cache: 'no-store', ...options });
      const type = res.headers.get('content-type') || '';
      const body = type.includes('application/json') ? await res.json() : await res.text();
      if (!res.ok) {
        const message = body && body.error && body.error.message ? body.error.message : JSON.stringify(body);
        throw new Error(message);
      }
      return body;
    }

    function renderModels(data) {
      els.modelsBody.innerHTML = '';
      for (const item of data.models || []) {
        const tr = document.createElement('tr');
        if (item.id === data.current_model) tr.className = 'current';

        const state = document.createElement('td');
        const pill = document.createElement('span');
        pill.className = 'pill ' + (item.available ? 'ok' : 'off');
        pill.textContent = item.available ? 'Active' : 'Off';
        state.appendChild(pill);

        const name = document.createElement('td');
        name.className = 'model-name';
        name.textContent = item.id;

        const tokens = document.createElement('td');
        tokens.className = 'num';
        tokens.textContent = fmt.format(item.used_tokens || 0);

        const reqs = document.createElement('td');
        reqs.className = 'num';
        reqs.textContent = fmt.format(item.requests || 0);

        const reason = document.createElement('td');
        reason.className = 'reason';
        reason.textContent = text(item.disabled_reason);

        const action = document.createElement('td');
        const button = document.createElement('button');
        button.type = 'button';
        button.className = item.available ? 'danger' : '';
        button.textContent = item.available ? 'Disable' : 'Enable';
        button.addEventListener('click', async () => {
          if (item.available && !confirm('Disable ' + item.id + '?')) return;
          setBusy(true);
          try {
            await api((item.available ? '/disable/' : '/enable/') + encodeURIComponent(item.id), { method: 'POST' });
            await refresh();
          } finally {
            setBusy(false);
          }
        });
        action.appendChild(button);

        for (const cell of [state, name, tokens, reqs, reason, action]) tr.appendChild(cell);
        els.modelsBody.appendChild(tr);
      }
    }

    function renderEvents(events) {
      els.events.innerHTML = '';
      if (!events || events.length === 0) {
        const empty = document.createElement('div');
        empty.className = 'empty';
        empty.textContent = 'No events in this process.';
        els.events.appendChild(empty);
        return;
      }

      for (const event of events) {
        const node = document.createElement('div');
        node.className = 'event';

        const top = document.createElement('div');
        top.className = 'event-top';
        const kind = document.createElement('span');
        kind.className = 'event-type';
        kind.textContent = text(event.type);
        const when = document.createElement('span');
        when.textContent = text(event.time);
        top.appendChild(kind);
        top.appendChild(when);

        const message = document.createElement('div');
        message.className = 'event-message';
        message.textContent = [event.model, event.message].filter(Boolean).join(' | ');

        node.appendChild(top);
        node.appendChild(message);
        els.events.appendChild(node);
      }
    }

    function render(data) {
      const disabled = (data.total_models || 0) - (data.active_models || 0);
      els.mCurrent.textContent = text(data.current_model);
      els.mActive.textContent = `${fmt.format(data.active_models || 0)} / ${fmt.format(data.total_models || 0)}`;
      els.mDisabled.textContent = fmt.format(disabled);
      els.mTokens.textContent = fmt.format(data.total_used_tokens || 0);
      els.mSwitches.textContent = fmt.format(data.switch_count || 0);
      els.modelSummary.textContent = `${fmt.format(data.models ? data.models.length : 0)} configured`;
      els.eventSummary.textContent = `${fmt.format(data.events ? data.events.length : 0)} events`;
      els.updated.textContent = 'Updated ' + new Date().toLocaleString();
      renderModels(data);
      renderEvents(data.events || []);
    }

    async function refresh() {
      if (busy) return;
      const data = await api('/status.json');
      render(data);
    }

    els.refreshBtn.addEventListener('click', refresh);
    els.configBtn.addEventListener('click', () => {
      window.location.href = '/config-ui';
    });
    els.nextBtn.addEventListener('click', async () => {
      setBusy(true);
      try {
        await api('/next', { method: 'POST' });
        await refresh();
      } finally {
        setBusy(false);
      }
    });
    els.enableAllBtn.addEventListener('click', async () => {
      setBusy(true);
      try {
        await api('/enable-all', { method: 'POST' });
        await refresh();
      } finally {
        setBusy(false);
      }
    });
    els.resetBtn.addEventListener('click', async () => {
      if (!confirm('Reset in-process usage counters?')) return;
      setBusy(true);
      try {
        await api('/reset', { method: 'POST' });
        await refresh();
      } finally {
        setBusy(false);
      }
    });

    setInterval(() => {
      if (els.autoRefresh.checked && !busy) refresh().catch(err => {
        els.updated.textContent = 'Refresh failed: ' + err.message;
      });
    }, 3000);

    refresh().catch(err => {
      els.updated.textContent = 'Load failed: ' + err.message;
    });
  </script>
</body>
</html>"""


def config_ui_html() -> str:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config_ui.html")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except Exception as exc:
        return (
            "<!doctype html><meta charset=\"utf-8\"><title>Config</title>"
            "<h1>Config UI unavailable</h1><pre>" + str(exc) + "</pre>"
        )


class PoolHandler(BaseHTTPRequestHandler):
    server_version = "ModelPoolProxy/2.0"

    def _path(self) -> str:
        return urlparse(self.path).path.rstrip("/") or "/"

    def _forward_path(self) -> str:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if parsed.query:
            return path + "?" + parsed.query
        return path

    def do_GET(self) -> None:
        path = self._path()
        if self._handle_management(path, method="GET"):
            return
        self._send_json(404, {"error": {"message": f"not found: {path}"}})

    def do_POST(self) -> None:
        path = self._path()
        if path in ("/config/test", "/v1/config/test"):
            body = self._read_json_body()
            if body is None:
                return
            try:
                self._send_json(200, test_config_payload(body))
            except Exception as exc:
                self._send_json(400, {"error": {"message": str(exc)}})
            return

        if path in ("/config", "/v1/config"):
            body = self._read_json_body()
            if body is None:
                return
            try:
                updated = update_runtime_config(body)
                self._send_json(200, {"ok": True, "config": updated})
            except Exception as exc:
                self._send_json(400, {"error": {"message": str(exc)}})
            return

        if self._handle_management(path, method="POST"):
            return

        body = self._read_json_body()
        if body is None:
            return

        if not isinstance(body, dict):
            self._send_json(400, {"error": {"message": "JSON body must be an object"}})
            return

        response = forward_request(self._forward_path(), self.headers, body)
        self._send_proxy_response(response)

    def _handle_management(self, path: str, method: str) -> bool:
        assert pool is not None

        if path in ("/", "/dashboard"):
            self._send_html(200, dashboard_html())
            return True

        if path in ("/config-ui", "/settings"):
            self._send_html(200, config_ui_html())
            return True

        if path in ("/config", "/v1/config"):
            self._send_json(200, public_config())
            return True

        if path in ("/health", "/v1/health"):
            self._send_json(
                200,
                {
                    "ok": True,
                    "active_models": pool.active_models,
                    "total_models": pool.total_models,
                    "current_model": pool.current() if pool.active_models else None,
                },
            )
            return True

        if path in ("/status", "/v1/status"):
            if method == "GET":
                self._send_text(200, pool.status_text())
            else:
                self._send_json(200, pool.status_data())
            return True

        if path in ("/status.json", "/v1/status.json"):
            self._send_json(200, pool.status_data())
            return True

        if path in ("/models", "/v1/models"):
            self._send_json(
                200,
                {
                    "object": "list",
                    "data": [
                        {"id": model, "object": "model", "owned_by": "model-pool-proxy"}
                        for model in pool.models
                        if model in pool.available
                    ],
                },
            )
            return True

        if path in ("/next", "/v1/next"):
            old = pool.current()
            try:
                new = pool.advance(skip=old)
                self._send_json(200, {"previous": old, "current_model": new})
            except RuntimeError as exc:
                self._send_json(400, {"error": {"message": str(exc)}})
            return True

        if path in ("/reset", "/v1/reset"):
            pool.reset_usage()
            self._send_json(200, {"ok": True, "message": "usage counters reset"})
            return True

        if path in ("/enable-all", "/v1/enable-all"):
            pool.enable_all()
            self._send_json(200, {"ok": True, "message": "all models enabled"})
            return True

        for prefix in ("/enable/", "/v1/enable/"):
            if path.startswith(prefix):
                model = unquote(path[len(prefix):])
                try:
                    pool.enable(model)
                    self._send_json(200, {"ok": True, "enabled": model})
                except ValueError as exc:
                    self._send_json(404, {"error": {"message": str(exc)}})
                return True

        for prefix in ("/disable/", "/v1/disable/"):
            if path.startswith(prefix):
                model = unquote(path[len(prefix):])
                pool.disable(model, "manual")
                self._send_json(200, {"ok": True, "disabled": model})
                return True

        return False

    def _read_json_body(self) -> Optional[Any]:
        try:
            content_length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            self._send_json(400, {"error": {"message": "invalid Content-Length"}})
            return None

        raw = self.rfile.read(content_length) if content_length else b"{}"
        try:
            return _parse_json_bytes(raw)
        except Exception:
            self._send_json(400, {"error": {"message": "invalid JSON body"}})
            return None

    def _send_proxy_response(self, response: ProxyResponse) -> None:
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(response.body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(response.body)

    def _send_json(self, status: int, data: Any) -> None:
        self._send_proxy_response(ProxyResponse(status, _json_bytes(data), "application/json"))

    def _send_text(self, status: int, text: str) -> None:
        self._send_proxy_response(
            ProxyResponse(status, text.encode("utf-8"), "text/plain; charset=utf-8")
        )

    def _send_html(self, status: int, html: str) -> None:
        self._send_proxy_response(
            ProxyResponse(status, html.encode("utf-8"), "text/html; charset=utf-8")
        )

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug(fmt, *args)


def load_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8-sig") as fh:
        return json.load(fh)


def _normalize_api_format(value: Any) -> str:
    fmt = str(value or "auto").strip().lower()
    if fmt not in ("auto", "anthropic", "openai"):
        return "auto"
    return fmt


def _normalize_models(value: Any) -> List[str]:
    if isinstance(value, str):
        candidates = []
        for line in value.replace(",", "\n").splitlines():
            candidates.append(line.strip())
    elif isinstance(value, list):
        candidates = [str(item).strip() for item in value]
    else:
        candidates = []

    models: List[str] = []
    seen = set()
    for model in candidates:
        if not model or model in seen:
            continue
        models.append(model)
        seen.add(model)
    return models


def _set_user_env_var(name: str, value: str) -> None:
    if value:
        os.environ[name] = value
    else:
        os.environ.pop(name, None)
    if os.name != "nt":
        return
    try:
        import ctypes
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            "Environment",
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            if value:
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
            else:
                try:
                    winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    pass

        HWND_BROADCAST = 0xFFFF
        WM_SETTINGCHANGE = 0x001A
        SMTO_ABORTIFHUNG = 0x0002
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST,
            WM_SETTINGCHANGE,
            0,
            "Environment",
            SMTO_ABORTIFHUNG,
            5000,
            None,
        )
    except Exception as exc:
        log.warning("[env] failed to persist %s to user environment: %s", name, exc)


def _get_config_api_key(raw: Dict[str, Any], args: Optional[argparse.Namespace] = None) -> str:
    api_key_env = str(raw.get("api_key_env") or "MODEL_POOL_API_KEY")
    arg_key = args.key if args and getattr(args, "key", None) else None
    api_key = str(arg_key or os.environ.get(api_key_env) or raw.get("api_key", "")).strip()
    if api_key.startswith("sk-your-") or api_key.startswith("sk-xxxx"):
        return ""
    return api_key


def _config_from_raw(raw: Dict[str, Any], args: Optional[argparse.Namespace] = None) -> ProxyConfig:
    api_key_env = str(raw.get("api_key_env") or "MODEL_POOL_API_KEY")
    arg_base_url = args.base_url if args and getattr(args, "base_url", None) else None
    arg_timeout = args.timeout if args and getattr(args, "timeout", None) else None

    api_key = _get_config_api_key(raw, args)

    base_url = str(arg_base_url or raw.get("base_url") or "").strip()
    if not base_url:
        raise ValueError("base_url is required")

    models = _normalize_models(raw.get("models") or [])
    if not models:
        raise ValueError("models cannot be empty")

    timeout_seconds = int(arg_timeout or raw.get("timeout_seconds") or 300)
    api_format = _normalize_api_format(raw.get("api_format") or raw.get("upstream_format") or "auto")
    return ProxyConfig(api_key, base_url, models, api_format, timeout_seconds, api_key_env)


def public_config() -> Dict[str, Any]:
    config = CONFIG
    if config is None:
        return {}
    return {
        "base_url": config.base_url,
        "api_format": config.api_format,
        "detected_format": _resolve_upstream_format(config, "/v1/messages"),
        "has_api_key": bool(config.api_key),
        "api_key_env": config.api_key_env,
        "api_key_storage": "environment" if bool(config.api_key) else "none",
        "model_count": len(config.models),
        "models": list(config.models),
        "models_text": "\n".join(config.models),
        "timeout_seconds": config.timeout_seconds,
    }


def _config_from_payload_for_test(payload: Dict[str, Any]) -> ProxyConfig:
    current = CONFIG
    base_url = str(payload.get("base_url") or (current.base_url if current else "")).strip()
    api_format = _normalize_api_format(
        payload.get("api_format") or (current.api_format if current else "auto")
    )
    timeout_seconds = int(payload.get("timeout_seconds") or (current.timeout_seconds if current else 300))

    models_value = payload.get("models")
    if models_value is None:
        models_value = payload.get("models_text")
    models = _normalize_models(models_value if models_value is not None else (current.models if current else []))

    if payload.get("clear_api_key"):
        api_key = ""
    else:
        submitted_key = str(payload.get("api_key") or "").strip()
        api_key = submitted_key if submitted_key else (current.api_key if current else "")

    if not base_url:
        raise ValueError("base_url is required")
    if not models:
        raise ValueError("models cannot be empty")
    if timeout_seconds < 5:
        timeout_seconds = 5
    return ProxyConfig(api_key, base_url, models, api_format, timeout_seconds)


def _test_body_for_format(upstream_format: str, model: str) -> Dict[str, Any]:
    if upstream_format == "anthropic":
        return {
            "model": model,
            "max_tokens": 8,
            "messages": [{"role": "user", "content": "只回复 OK"}],
        }
    return {
        "model": model,
        "max_tokens": 8,
        "messages": [{"role": "user", "content": "Reply OK only."}],
    }


def _summarize_test_success(raw: bytes, content_type: str, upstream_format: str) -> Tuple[str, int]:
    if "application/json" not in content_type.lower() and not raw.lstrip().startswith(b"{"):
        return raw.decode("utf-8", errors="replace")[:220], 0

    parsed = _parse_json_bytes(raw)
    tokens = _extract_usage(parsed)
    if upstream_format == "anthropic":
        parts = []
        for block in parsed.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return ("".join(parts) or json.dumps(parsed, ensure_ascii=False))[:220], tokens

    choice = (parsed.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    return (str(message.get("content") or json.dumps(parsed, ensure_ascii=False)))[:220], tokens


def test_one_model(config: ProxyConfig, model: str) -> Dict[str, Any]:
    upstream_format = _resolve_upstream_format(config, "/v1/chat/completions")
    path = "/v1/messages" if upstream_format == "anthropic" else "/v1/chat/completions"
    url = _build_upstream_url(config.base_url, path)
    body = _test_body_for_format(upstream_format, model)
    headers = _forward_headers({}, config.api_key)
    data = _json_bytes(body)
    started = time.time()

    result: Dict[str, Any] = {
        "model": model,
        "ok": False,
        "status": None,
        "latency_ms": None,
        "format": upstream_format,
        "tokens": 0,
        "message": "",
    }

    try:
        req = urllib.request.Request(url, data=data, method="POST")
        for key, value in headers.items():
            req.add_header(key, value)
        with urllib.request.urlopen(req, timeout=config.timeout_seconds) as resp:
            raw = resp.read()
            content_type = resp.headers.get("Content-Type", "application/json")
            message, tokens = _summarize_test_success(raw, content_type, upstream_format)
            result.update(
                {
                    "ok": 200 <= int(resp.status) < 300,
                    "status": int(resp.status),
                    "latency_ms": int((time.time() - started) * 1000),
                    "tokens": tokens,
                    "message": message,
                }
            )
            return result

    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            error_body = _parse_json_bytes(raw)
        except Exception:
            error_body = {"error": raw.decode("utf-8", errors="replace")}
        _, message, full_text = _error_fields(error_body)
        result.update(
            {
                "status": int(exc.code),
                "latency_ms": int((time.time() - started) * 1000),
                "message": (message or full_text or f"HTTP {exc.code}")[:400],
            }
        )
        return result

    except Exception as exc:
        result.update(
            {
                "latency_ms": int((time.time() - started) * 1000),
                "message": str(exc)[:400],
            }
        )
        return result


def test_config_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    config = _config_from_payload_for_test(payload)
    mode = str(payload.get("mode") or "first").lower()
    if mode == "all":
        models = list(config.models)
    else:
        requested_model = str(payload.get("model") or "").strip()
        models = [requested_model] if requested_model else [config.models[0]]

    results = [test_one_model(config, model) for model in models]
    ok_count = sum(1 for item in results if item.get("ok"))
    return {
        "ok": ok_count == len(results) if results else False,
        "tested": len(results),
        "ok_count": ok_count,
        "failed_count": len(results) - ok_count,
        "format": _resolve_upstream_format(config, "/v1/chat/completions"),
        "base_url": config.base_url,
        "results": results,
    }


def _write_config(path: str, raw: Dict[str, Any]) -> None:
    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as fh:
        json.dump(raw, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(temp_path, path)


def default_state_path(config_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(config_path)), "state.json")


def update_runtime_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    if CONFIG_PATH is None:
        raise RuntimeError("config path is not initialized")

    with CONFIG_LOCK:
        raw = load_config(CONFIG_PATH)
        api_key_env = str(raw.get("api_key_env") or "MODEL_POOL_API_KEY")
        before = {
            "base_url": str(raw.get("base_url") or "").strip(),
            "api_format": _normalize_api_format(raw.get("api_format") or raw.get("upstream_format") or "auto"),
            "models": _normalize_models(raw.get("models") or []),
            "api_key": _get_config_api_key(raw),
        }
        raw["api_key_env"] = api_key_env

        if "base_url" in payload:
            raw["base_url"] = str(payload.get("base_url") or "").strip()
        if "api_format" in payload:
            raw["api_format"] = _normalize_api_format(payload.get("api_format"))
        if "timeout_seconds" in payload:
            raw["timeout_seconds"] = int(payload.get("timeout_seconds") or 300)

        models_value = payload.get("models")
        if models_value is None:
            models_value = payload.get("models_text")
        if models_value is not None:
            raw["models"] = _normalize_models(models_value)

        if payload.get("clear_api_key"):
            _set_user_env_var(api_key_env, "")
            raw.pop("api_key", None)
        else:
            submitted_key = str(payload.get("api_key") or "").strip()
            if submitted_key and submitted_key not in ("********", "************"):
                _set_user_env_var(api_key_env, submitted_key)
                raw.pop("api_key", None)
            else:
                raw.pop("api_key", None)

        config = _config_from_raw(raw)
        after = {
            "base_url": config.base_url,
            "api_format": config.api_format,
            "models": list(config.models),
            "api_key": config.api_key,
        }
        significant_change = before != after
        if significant_change and STATE_PATH and os.path.exists(STATE_PATH):
            try:
                os.remove(STATE_PATH)
            except Exception as exc:
                log.warning("[state] reset failed after config change: %s", exc)

        global CONFIG, pool
        CONFIG = config
        pool = ModelPool(config.models, STATE_PATH)
        pool._record_locked("config", pool.current(), "config saved from UI")
        pool._save_state_locked()

        _write_config(CONFIG_PATH, raw)
        return public_config()


def build_config(args: argparse.Namespace) -> Tuple[ProxyConfig, str, int, str, str]:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = args.config or os.path.join(script_dir, "config.json")
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"config file not found: {config_path}")

    raw = load_config(config_path)
    config = _config_from_raw(raw, args)
    bind_host = str(args.bind or raw.get("bind_host") or "127.0.0.1")
    port = int(args.port or raw.get("port") or 19190)
    state_path = str(raw.get("state_path") or default_state_path(config_path))

    return config, bind_host, port, config_path, state_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Model Pool Proxy")
    parser.add_argument("-c", "--config", default=None, help="config file path")
    parser.add_argument("--bind", default=None, help="bind host, default from config or 127.0.0.1")
    parser.add_argument("--port", type=int, default=None, help="override listen port")
    parser.add_argument("--key", default=None, help="override API key")
    parser.add_argument("--base-url", default=None, help="override upstream base URL")
    parser.add_argument("--timeout", type=int, default=None, help="upstream timeout seconds")
    parser.add_argument("--check", action="store_true", help="validate config and exit")
    parser.add_argument("--verbose", action="store_true", help="enable debug logging")
    args = parser.parse_args()

    if args.verbose:
        log.setLevel(logging.DEBUG)

    try:
        config, bind_host, port, config_path, state_path = build_config(args)
    except Exception as exc:
        log.error("%s", exc)
        return 1

    global CONFIG, CONFIG_PATH, STATE_PATH, pool
    CONFIG = config
    CONFIG_PATH = config_path
    STATE_PATH = state_path
    try:
        pool = ModelPool(config.models, STATE_PATH, persist=not args.check)
    except Exception as exc:
        log.error("%s", exc)
        return 1

    if args.check:
        log.info(
            "config ok | models=%s | format=%s | listen=http://%s:%s",
            len(config.models),
            config.api_format,
            bind_host,
            port,
        )
        return 0

    log.info("Model Pool Proxy")
    log.info("upstream: %s", config.base_url)
    log.info("format: %s", config.api_format)
    log.info("models: %s", len(config.models))
    log.info("listen: http://%s:%s", bind_host, port)
    print()
    print(pool.status_text())

    try:
        server = ThreadingHTTPServer((bind_host, port), PoolHandler)
    except OSError as exc:
        log.error("cannot listen on %s:%s | %s", bind_host, port, exc)
        return 2

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("proxy stopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
