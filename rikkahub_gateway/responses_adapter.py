"""Explicit Responses upstream, retaining the gateway's Chat Completions contract.

Opaque reasoning is kept only in the owning active turn. It never goes into
client text, logs, the memory database, or another model's conversation.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field

import httpx

MAX_EVENT_BYTES = 32 * 1024 * 1024
MAX_WIRE_BYTES = 64 * 1024 * 1024


class ResponsesProtocolError(httpx.RemoteProtocolError):
    pass


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def tool_name(name):
    if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
        return name
    return "stf_" + hashlib.sha256(name.encode()).hexdigest()[:48]


def content_parts(content, *, assistant=False):
    if isinstance(content, str):
        return content
    result = []
    for part in content or []:
        kind = part.get("type")
        if kind == "text":
            item = {"type": "output_text" if assistant else "input_text", "text": part["text"]}
            if assistant:
                item["annotations"] = []
            result.append(item)
        elif kind == "image_url":
            image = part["image_url"]
            image = {"url": image} if isinstance(image, str) else image
            item = {"type": "input_image", "image_url": image["url"]}
            if image.get("detail"):
                item["detail"] = image["detail"]
            result.append(item)
        else:
            raise ResponsesProtocolError("responses_input_content_unsupported")
    return result


@dataclass
class ResponsesTurnState:
    batches: list = field(default_factory=list, repr=False)
    bytes_used: int = 0

    def remember(self, output):
        calls = [item for item in output if item.get("type") == "function_call"]
        if not calls:
            return
        # A non-stored response needs encrypted reasoning when it is replayed.
        items = [copy.deepcopy(item) for item in output
                 if item.get("type") in {"reasoning", "message", "function_call"}
                 and (item.get("type") != "reasoning" or item.get("encrypted_content"))]
        size = len(encode(items))
        if self.bytes_used + size > MAX_WIRE_BYTES:
            raise ResponsesProtocolError("responses_turn_state_too_large")
        ids = [c["call_id"] for c in calls]
        if any(ids == prior[0] for prior in self.batches):
            return
        self.batches.append((ids, items))
        self.bytes_used += size

    def replay(self, calls):
        ids = [c["id"] for c in calls]
        for stored_ids, items in self.batches:
            if stored_ids != ids:
                continue
            stored = [x for x in items if x.get("type") == "function_call"]
            if all(a["name"] == tool_name(b["function"]["name"])
                   and json.loads(a["arguments"]) == json.loads(b["function"]["arguments"])
                   for a, b in zip(stored, calls)):
                return copy.deepcopy(items)
        return None


class ResponsesAdapter:
    def __init__(self, payload, state=None):
        self.payload = payload
        self.state = state if state is not None else ResponsesTurnState()
        self.names = {}
        self.stream = payload.get("stream") is True

    def name(self, original):
        alias = tool_name(original)
        if alias in self.names and self.names[alias] != original:
            raise ResponsesProtocolError("responses_tool_name_collision")
        self.names[alias] = original
        return alias

    def request(self):
        p = self.payload
        result = {"model": p["model"], "store": False, "stream": self.stream,
                  "include": ["reasoning.encrypted_content"], "input": []}
        effort = p.get("reasoning_effort") or "medium"
        result["reasoning"] = {"effort": effort, "summary": "auto"}
        for key in ("parallel_tool_calls", "service_tier", "prompt_cache_key", "prompt_cache_retention"):
            if key in p:
                result[key] = p[key]
        maximum = p.get("max_completion_tokens", p.get("max_tokens"))
        if maximum is not None:
            result["max_output_tokens"] = maximum
        if effort == "none":
            for key in ("temperature", "top_p"):
                if key in p:
                    result[key] = p[key]
        for message in p.get("messages", []):
            role = message["role"]
            calls = message.get("tool_calls") or []
            for call in calls:
                self.name(call["function"]["name"])
            replay = self.state.replay(calls) if calls else None
            if replay is not None:
                result["input"].extend(replay)
                continue
            if role == "tool":
                result["input"].append({"type": "function_call_output", "call_id": message["tool_call_id"],
                                        "output": content_parts(message.get("content")) or ""})
                continue
            content = content_parts(message.get("content"), assistant=role == "assistant")
            if content:
                result["input"].append({"role": role, "content": content})
            for call in calls:
                result["input"].append({"type": "function_call", "call_id": call["id"],
                                        "name": self.name(call["function"]["name"]),
                                        "arguments": call["function"]["arguments"]})
        if p.get("tools"):
            result["tools"] = []
            for tool in p["tools"]:
                function = tool["function"]
                result["tools"].append({"type": "function", "name": self.name(function["name"]),
                    "description": function.get("description", ""),
                    "parameters": function.get("parameters", {"type": "object", "properties": {}}),
                    "strict": function.get("strict", False)})
        if "tool_choice" in p:
            choice = p["tool_choice"]
            result["tool_choice"] = ({"type": "function", "name": self.name(choice["function"]["name"])}
                                     if isinstance(choice, dict) else choice)
        fmt = p.get("response_format")
        if isinstance(fmt, dict) and fmt.get("type") != "text":
            result["text"] = {"format": ({"type": "json_schema", **fmt["json_schema"]}
                                         if fmt.get("type") == "json_schema" else fmt)}
        return result

    def completion(self, response):
        if "error" in response and response.get("error"):
            return {"error": response["error"]}
        status = response.get("status")
        if status not in {"completed", "incomplete"}:
            raise ResponsesProtocolError("responses_missing_terminal")
        output = response.get("output", [])
        message = {"role": "assistant", "content": ""}
        summaries = []
        calls = []
        for item in output:
            kind = item.get("type")
            if kind == "message":
                for part in item.get("content", []):
                    if part.get("type") == "output_text":
                        message["content"] += part["text"]
                    elif part.get("type") == "refusal":
                        message["content"] += part["refusal"]
            elif kind == "reasoning":
                summaries.extend(p["text"] for p in item.get("summary", []) if p.get("type") == "summary_text")
            elif kind == "function_call":
                calls.append({"id": item["call_id"], "type": "function", "function": {
                    "name": self.names.get(item["name"], item["name"]), "arguments": item["arguments"]}})
        if calls and status != "completed":
            raise ResponsesProtocolError("responses_incomplete_tool_batch")
        if calls:
            message["tool_calls"] = calls
        if summaries:
            message["reasoning_content"] = "".join(summaries)
        if status == "completed":
            self.state.remember(output)
        finish = "length" if status == "incomplete" else "tool_calls" if calls else "stop"
        result = {"id": response.get("id", ""), "object": "chat.completion", "model": self.payload["model"],
                  "created": response.get("created_at", 0),
                  "choices": [{"index": 0, "message": message, "finish_reason": finish}]}
        usage = response.get("usage")
        if isinstance(usage, dict):
            result["usage"] = {"prompt_tokens": usage.get("input_tokens", 0),
                               "completion_tokens": usage.get("output_tokens", 0),
                               "total_tokens": usage.get("total_tokens", 0),
                               "prompt_tokens_details": usage.get("input_tokens_details", {}),
                               "completion_tokens_details": usage.get("output_tokens_details", {})}
        return result

    def wrap(self, response):
        return TranslatedResponse(response, self)


class TranslatedResponse:
    def __init__(self, upstream, adapter):
        self.upstream = upstream
        self.adapter = adapter
        self.status_code = upstream.status_code
        self.headers = httpx.Headers(upstream.headers)
        for name in ("content-length", "content-encoding", "transfer-encoding"):
            self.headers.pop(name, None)
        self.headers["content-type"] = "text/event-stream" if adapter.stream else "application/json"
        self.extensions = upstream.extensions

    @property
    def is_closed(self):
        return self.upstream.is_closed

    @property
    def num_bytes_downloaded(self):
        return self.upstream.num_bytes_downloaded

    @property
    def close(self):
        return self.upstream.close

    @close.setter
    def close(self, value):
        # The gateway's disconnect fence must also cover HTTPX closing at EOF,
        # before its socket can be returned to the upstream connection pool.
        self.upstream.close = value

    def iter_bytes(self):
        try:
            yield from self._iter_bytes()
        except (ValueError, KeyError, TypeError, UnicodeError):
            raise ResponsesProtocolError("responses_invalid_payload") from None

    def _iter_bytes(self):
        if not 200 <= self.status_code < 300:
            yield from self.upstream.iter_bytes()
        elif self.adapter.stream:
            yield from self._stream()
        else:
            raw = bytearray()
            for chunk in self.upstream.iter_bytes():
                raw.extend(chunk)
                if len(raw) > MAX_EVENT_BYTES:
                    raise ResponsesProtocolError("responses_response_too_large")
            yield encode(self.adapter.completion(json.loads(raw)))

    def _stream(self):
        pending = bytearray()
        data = []
        data_size = 0
        total = 0
        seen_text = ""
        seen_reasoning = ""
        terminal = False
        response_id = ""

        def chunk(delta, finish=None, usage=None):
            event = {"id": response_id, "object": "chat.completion.chunk", "model": self.adapter.payload["model"],
                     "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            if usage is not None:
                event["usage"] = usage
            return b"data: " + encode(event) + b"\n\n"

        for incoming in self.upstream.iter_bytes():
            total += len(incoming)
            if total > MAX_WIRE_BYTES:
                raise ResponsesProtocolError("responses_stream_too_large")
            pending.extend(incoming)
            if len(pending) > MAX_EVENT_BYTES:
                raise ResponsesProtocolError("responses_event_too_large")
            while b"\n" in pending:
                raw_line, _, rest = pending.partition(b"\n")
                pending = bytearray(rest)
                line = raw_line.rstrip(b"\r")
                if line.startswith(b"data:"):
                    data.append(line[5:].lstrip(b" "))
                    data_size += len(line)
                    if data_size > MAX_EVENT_BYTES:
                        raise ResponsesProtocolError("responses_event_too_large")
                elif not line and data:
                    raw = b"\n".join(data)
                    data.clear()
                    data_size = 0
                    if raw == b"[DONE]":
                        if not terminal:
                            raise ResponsesProtocolError("responses_missing_terminal")
                        return
                    event = json.loads(raw)
                    kind = event.get("type")
                    if kind == "response.created":
                        response_id = event["response"]["id"]
                        yield chunk({"role": "assistant"})
                    elif kind in {"response.output_text.delta", "response.refusal.delta"}:
                        delta = event["delta"]
                        seen_text += delta
                        yield chunk({"content": delta})
                    elif kind == "response.reasoning_summary_text.delta":
                        delta = event["delta"]
                        seen_reasoning += delta
                        yield chunk({"reasoning_content": delta})
                    elif kind in {"response.completed", "response.incomplete"}:
                        body = self.adapter.completion(event["response"])
                        if body.get("error"):
                            yield b"data: " + encode(body) + b"\n\n"
                            yield b"data: [DONE]\n\n"
                            return
                        choice = body["choices"][0]
                        message = choice["message"]
                        if not message["content"].startswith(seen_text):
                            raise ResponsesProtocolError("responses_text_mismatch")
                        if len(message["content"]) > len(seen_text):
                            yield chunk({"content": message["content"][len(seen_text):]})
                        reasoning = message.get("reasoning_content", "")
                        if reasoning.startswith(seen_reasoning) and len(reasoning) > len(seen_reasoning):
                            yield chunk({"reasoning_content": reasoning[len(seen_reasoning):]})
                        if message.get("tool_calls"):
                            yield chunk({"tool_calls": [dict(call, index=i) for i, call in enumerate(message["tool_calls"])]})
                        yield chunk({}, choice["finish_reason"], body.get("usage"))
                        terminal = True
                        yield b"data: [DONE]\n\n"
                        return
                    elif kind in {"error", "response.failed"}:
                        error = event.get("error") or event.get("response", {}).get("error") or {
                            "type": "upstream_error", "code": event.get("code", "responses_failed"),
                            "message": event.get("message", "OpenAI Responses 请求失败")}
                        yield b"data: " + encode({"error": error}) + b"\n\n"
                        yield b"data: [DONE]\n\n"
                        return
        if not terminal:
            raise ResponsesProtocolError("responses_missing_terminal")
