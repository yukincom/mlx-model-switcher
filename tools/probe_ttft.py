#!/usr/bin/env python3
"""Measure an OpenAI-compatible text stream without saving prompts or replies."""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from urllib.request import Request, urlopen


def measure(base_url: str, payload: dict, timeout: float = 180) -> dict:
    request_body = {**payload, "stream": True,
                    "stream_options": {**payload.get("stream_options", {}), "include_usage": True}}
    encoded = json.dumps(request_body, ensure_ascii=False).encode("utf-8")
    started = time.perf_counter()
    first_event = first_token = first_content = None
    last_token = None
    usage = None
    finish_reason = None
    done = False
    with urlopen(Request(base_url.rstrip("/") + "/chat/completions", data=encoded,
                         headers={"Content-Type": "application/json"}), timeout=timeout) as response:
        headers_at = time.perf_counter()
        for line in response:
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                done = True
                break
            event = json.loads(data)
            now = time.perf_counter()
            if "error" in event:
                raise RuntimeError("Server returned an error event")
            if first_event is None:
                first_event = now
            if event.get("usage"):
                usage = event["usage"]
            for choice in event.get("choices", []):
                delta = choice.get("delta", {})
                if delta.get("content") or delta.get("reasoning_content") or delta.get("tool_calls"):
                    first_token = first_token or now
                    last_token = now
                if delta.get("content"):
                    first_content = first_content or now
                finish_reason = choice.get("finish_reason") or finish_reason
    if not done:
        raise RuntimeError("Stream ended without a completion marker")
    ended = time.perf_counter()

    def elapsed(point):
        return round((point - started) * 1000, 1) if point is not None else None

    return {
        "payload_sha256": hashlib.sha256(encoded).hexdigest(),
        "request_bytes": len(encoded),
        "model": payload.get("model"),
        "message_count": len(payload.get("messages", [])),
        "tool_count": len(payload.get("tools", [])),
        "headers_ms": elapsed(headers_at),
        "first_event_ms": elapsed(first_event),
        "first_token_ms": elapsed(first_token),
        "first_content_ms": elapsed(first_content),
        "last_token_ms": elapsed(last_token),
        "total_ms": elapsed(ended),
        "finish_reason": finish_reason,
        "usage": usage,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8080/v1")
    parser.add_argument("--input-json", required=True, help="Local chat-completions JSON; not copied to output")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat must be positive")
    with open(args.input_json, encoding="utf-8") as source:
        payload = json.load(source)
    for index in range(args.repeat):
        print(json.dumps({"run": index + 1, **measure(args.base_url, payload, args.timeout)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
