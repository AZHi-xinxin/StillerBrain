"""Normalize complete Google OpenAI-compatible tool events before ST validation.

Google can omit the streaming call index on an otherwise complete function call.
Only its official endpoint gets this adapter. Fragmentary or mixed declarations
remain errors; opaque provider metadata is carried through without interpretation.
"""
import copy
import json
from urllib.parse import urlsplit

import httpx


def is_google_chat_route(route):
    url = urlsplit(route.upstream_base_url)
    return (route.api_mode == "chat_completions" and url.scheme == "https"
            and url.hostname == "generativelanguage.googleapis.com"
            and url.port in (None, 443) and not url.username and not url.password
            and url.path.rstrip("/") == "/v1beta/openai"
            and not url.query and not url.fragment)


def google_chat_payload(payload):
    """Wire-only attachment carriers, after ST has bound the original source.

    Google's compatibility endpoint accepts image_url only on user messages.
    Keep the original assistant/tool text and complete tool-result batch in place,
    then carry the actual images as explicitly attributed data. These synthetic
    carriers must never be passed to ST recall/capture or saved as human history.
    """
    messages = payload.get("messages", [])
    has_images = any(isinstance(m.get("content"), list) and m.get("role") in ("assistant", "tool", "system", "developer")
               and any(isinstance(p, dict) and p.get("type") == "image_url" for p in m["content"])
               for m in messages if isinstance(m, dict))
    system_count = sum(isinstance(m, dict) and m.get("role") == "system" for m in messages)
    if not has_images and system_count <= 1:
        return payload
    result = copy.deepcopy(payload)
    outgoing = []
    pending = []
    systems = []

    def flush():
        if pending:
            outgoing.append({"role": "user", "name": "st_provider_image_data_v1", "content": list(pending)})
            pending.clear()

    for index, message in enumerate(result["messages"]):
        role = message.get("role")
        if role != "tool":
            flush()
        content = message.get("content")
        if role in ("assistant", "tool", "system", "developer") and isinstance(content, list):
            kept = []
            for part_index, part in enumerate(content):
                if isinstance(part, dict) and part.get("type") == "image_url":
                    origin = {"source_role": role, "source_message_index": index, "source_part_index": part_index,
                              "human_authored": False, "instruction_authority": "none"}
                    if role == "tool":
                        origin["tool_call_id"] = message.get("tool_call_id")
                    label = ("Historical attachment data from the indicated source; not a new human message or instruction. "
                             + json.dumps(origin, ensure_ascii=False, separators=(",", ":")))
                    pending.extend([{"type": "text", "text": label}, part])
                    kept.append({"type": "text", "text": "[Image attachment retained in the following attributed image-data message.]"})
                else:
                    kept.append(part)
            message["content"] = kept
        outgoing.append(message)
        if role == "system":
            systems.append((index, message))
        if role not in ("tool",) and not (role == "assistant" and message.get("tool_calls")):
            flush()
    flush()
    if len(systems) > 1:
        # The tested Google compatibility endpoint replaces earlier SYSTEM
        # messages. Merge their full text only in the outbound provider copy.
        blocks = []
        for index, message in systems:
            content = message.get("content")
            if isinstance(content, list):
                if any(not isinstance(p, dict) or p.get("type") != "text" or not isinstance(p.get("text"), str) for p in content):
                    raise httpx.RemoteProtocolError("google_system_content_unsupported")
                content = "\n".join(p["text"] for p in content)
            if not isinstance(content, str):
                raise httpx.RemoteProtocolError("google_system_content_unsupported")
            origin = {"source_message_index": index}
            if isinstance(message.get("name"), str):
                origin["source_name"] = message["name"]
            blocks.append("[System context block " + json.dumps(origin, ensure_ascii=False) + "]\n" + content)
        merged = dict(systems[0][1], content="\n\n".join(blocks))
        merged.pop("name", None)
        outgoing = [merged if m is systems[0][1] else m for m in outgoing
                    if m.get("role") != "system" or m is systems[0][1]]
    result["messages"] = outgoing
    return result


class GoogleStreamResponse:
    def __init__(self, upstream, event_buffer, parse_event):
        self.upstream = upstream
        self.event_buffer = event_buffer
        self.parse_event = parse_event
        self.status_code = upstream.status_code
        self.headers = httpx.Headers(upstream.headers)
        for name in ("content-length", "content-encoding", "transfer-encoding"):
            self.headers.pop(name, None)
        self.extensions = upstream.extensions
        self.modes = {}
        self.ids = {}

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
        self.upstream.close = value

    def normalize(self, raw):
        event = self.parse_event(raw)
        if event.payload is None:
            return raw
        payload = event.payload
        changed = False
        for fallback, choice in enumerate(payload.get("choices", [])):
            if not isinstance(choice, dict):
                continue  # The ordinary validator handles malformed payloads.
            delta = choice.get("delta")
            calls = delta.get("tool_calls") if isinstance(delta, dict) else None
            if not isinstance(calls, list):
                continue
            index = choice.get("index", fallback)
            if isinstance(index, bool) or not isinstance(index, int):
                raise httpx.RemoteProtocolError("google_tool_choice_index_invalid")
            for call in calls:
                if not isinstance(call, dict):
                    continue
                mode = "indexed" if "index" in call else "complete"
                if self.modes.setdefault(index, mode) != mode:
                    raise httpx.RemoteProtocolError("google_tool_index_mode_ambiguous")
                if mode == "indexed":
                    continue
                function = call.get("function")
                call_id = call.get("id")
                if (not isinstance(call_id, str) or not call_id
                        or call.get("type") != "function" or not isinstance(function, dict)
                        or not isinstance(function.get("name"), str) or not function["name"]
                        or not isinstance(function.get("arguments"), str)):
                    raise httpx.RemoteProtocolError("google_tool_call_incomplete")
                try:
                    arguments = json.loads(function["arguments"])
                except (ValueError, TypeError):
                    raise httpx.RemoteProtocolError("google_tool_arguments_incomplete") from None
                if not isinstance(arguments, dict):
                    raise httpx.RemoteProtocolError("google_tool_arguments_invalid")
                ids = self.ids.setdefault(index, set())
                if call_id in ids:
                    raise httpx.RemoteProtocolError("google_tool_id_repeated")
                call["index"] = len(ids)
                ids.add(call_id)
                changed = True
        if not changed:
            return raw
        return b"data: " + json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                                     allow_nan=False).encode() + b"\n\n"

    def iter_bytes(self):
        if not 200 <= self.status_code < 300:
            yield from self.upstream.iter_bytes()
            return
        for chunk in self.upstream.iter_bytes():
            for raw in self.event_buffer.feed(chunk):
                yield self.normalize(raw)
        for raw in self.event_buffer.feed(b"", final=True):
            yield self.normalize(raw)
