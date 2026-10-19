import asyncio
import json
import os
import threading
import time
from typing import Any
import httpx
from astrbot.api import logger


def _get_read_timeout(client: httpx.AsyncClient) -> float:
    timeout_obj = getattr(client, "timeout", None)
    if timeout_obj is not None:
        read_val = getattr(timeout_obj, "read", None)
        if isinstance(read_val, (int, float)) and read_val > 10.0:
            return float(read_val)
    return 120.0


_last_time = 0
_counter = 0
_id_lock = threading.Lock()


def generate_canonical_id(invert: bool) -> str:
    """生成 OpenCode 官方规范的有序唯一 ID:

    - invert=True 用于 Session ID (前缀 ses_)
    - invert=False 用于 Request ID (前缀 msg_)

    Args:
        invert: 是否反转位以标识会话级别 ID。

    Returns:
        生成的规范有序 ID 字符串。
    """
    global _last_time, _counter
    with _id_lock:
        now_ms = int(time.time() * 1000)
        if now_ms != _last_time:
            _last_time = now_ms
            _counter = 0
        _counter += 1
        val = now_ms * 0x1000 + _counter
    if invert:
        val = ~val
    hex_parts = [f"{(val >> (40 - 8 * w)) & 0xff:02x}" for w in range(6)]
    u_str = "".join(hex_parts)
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    rand_str = "".join(chars[b % 62] for b in os.urandom(14))
    return u_str + rand_str


# OpenAI Chat Completions 协议工具列表（含 function 包装）
CHAT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": name,
            "description": f"CLI tool {name}",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    for name in ["bash", "edit", "glob", "grep", "read", "skill", "task", "todowrite", "webfetch", "websearch", "write"]
]

# OpenAI Responses 协议工具列表（顶层 name）
RESPONSES_TOOLS = [
    {
        "type": "function",
        "name": name,
        "description": f"CLI tool {name}",
        "parameters": {"type": "object", "properties": {}},
    }
    for name in ["bash", "edit", "glob", "grep", "read", "skill", "task", "todowrite", "webfetch", "websearch", "write"]
]


def is_responses_native_model(model_name: str) -> bool:
    """判断指定模型是否为 OpenCode 原生 Responses 协议模型。

    OpenCode 后端路由规则：
    - Muse 系列模型 (如 muse-spark-1.3-contributor-free) 原生走 Responses 协议 (/responses)
    - 其他免费及开源模型 (如 glm-5.3-flash-free, deepseek-v4.1-flash-free, space-bunny-free, mimo-v2.6-flash-free 等) 原生走 ChatCompletions 协议 (/chat/completions)

    Args:
        model_name: 模型标识字符串。

    Returns:
        若该模型原生支持 Responses 协议则返回 True，否则返回 False。
    """
    m = str(model_name or "").lower().strip()
    return m.startswith("muse-")


def _convert_input_to_chat_messages(input_data: list[Any]) -> list[dict[str, Any]]:
    """将 Responses 协议 input 列表或未规范化的 messages 列表转译为 ChatCompletions 标准格式。

    Args:
        input_data: 待转译的 input 或 messages 列表。

    Returns:
        符合 OpenAI Chat Completions 规范的 messages 列表。
    """
    messages: list[dict[str, Any]] = []
    for item in input_data:
        if not isinstance(item, dict):
            continue

        item_type = item.get("type")

        # 1. Responses 协议中的函数调用项
        if item_type == "function_call":
            call_id = str(item.get("call_id") or "")
            name = str(item.get("name") or "")
            args = item.get("arguments", "{}")
            if not isinstance(args, str):
                args = json.dumps(args, ensure_ascii=False)

            tool_call = {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": args},
            }
            if messages and messages[-1].get("role") == "assistant" and "tool_calls" in messages[-1]:
                messages[-1]["tool_calls"].append(tool_call)
            else:
                messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [tool_call],
                })
            continue

        # 2. Responses 协议中的函数调用结果项
        if item_type == "function_call_output":
            call_id = str(item.get("call_id") or "")
            output = item.get("output", "")
            if not isinstance(output, str):
                output = json.dumps(output, ensure_ascii=False)
            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": output,
            })
            continue

        # 3. 忽略独立 reasoning 块
        if item_type == "reasoning":
            continue

        # 4. 常规消息项（可能带有 type="message" 或直接以 role 区分）
        role = item.get("role", "user")
        if role == "developer":
            role = "system"

        content = item.get("content")
        converted_content: str | list[dict[str, Any]] = ""

        if isinstance(content, str):
            converted_content = content
        elif isinstance(content, list):
            has_image = any(isinstance(p, dict) and p.get("type") in {"image_url", "input_image"} for p in content)
            if not has_image:
                text_parts: list[str] = []
                for p in content:
                    if isinstance(p, str):
                        text_parts.append(p)
                    elif isinstance(p, dict) and p.get("type") in {"text", "input_text"}:
                        text_parts.append(str(p.get("text", "")))
                converted_content = "".join(text_parts)
            else:
                parts: list[dict[str, Any]] = []
                for p in content:
                    if not isinstance(p, dict):
                        continue
                    p_type = p.get("type")
                    if p_type in {"text", "input_text"}:
                        parts.append({"type": "text", "text": str(p.get("text", ""))})
                    elif p_type in {"image_url", "input_image"}:
                        img_data = p.get("image_url")
                        detail = p.get("detail", "auto")
                        if isinstance(img_data, dict):
                            parts.append({"type": "image_url", "image_url": img_data})
                        elif isinstance(img_data, str):
                            parts.append({"type": "image_url", "image_url": {"url": img_data, "detail": detail}})
                converted_content = parts
        else:
            converted_content = str(content) if content is not None else ""

        msg: dict[str, Any] = {"role": role, "content": converted_content}
        if "tool_calls" in item:
            msg["tool_calls"] = item["tool_calls"]
        if "tool_call_id" in item:
            msg["tool_call_id"] = item["tool_call_id"]
        if "name" in item:
            msg["name"] = item["name"]
        messages.append(msg)

    return messages


def _merge_chat_tools(existing_tools: list[Any]) -> list[dict[str, Any]]:
    """规范化并合并工具列表，确保保留用户自定义工具并补齐 OpenCode 必需的 11 项内置工具。

    Args:
        existing_tools: 原始请求中携带的工具列表。

    Returns:
        符合 OpenAI Chat Completions 规范的合并工具列表。
    """
    normalized: list[dict[str, Any]] = []
    existing_names: set[str] = set()

    for t in existing_tools:
        if not isinstance(t, dict):
            continue
        if t.get("type") == "function":
            fn = t.get("function")
            if isinstance(fn, dict) and fn.get("name"):
                normalized.append(t)
                existing_names.add(fn["name"])
            elif t.get("name"):
                name = t["name"]
                normalized.append({
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": t.get("description", ""),
                        "parameters": t.get("parameters", {"type": "object", "properties": {}}),
                    },
                })
                existing_names.add(name)
        else:
            normalized.append(t)

    merged = list(normalized)
    for ct in CHAT_TOOLS:
        if ct["function"]["name"] not in existing_names:
            merged.append(ct)
    return merged


def _merge_responses_tools(existing_tools: list[Any]) -> list[dict[str, Any]]:
    """规范化并合并工具列表，确保保留用户自定义工具并补齐 OpenCode Responses 必需的 11 项内置工具。

    Args:
        existing_tools: 原始请求中携带的工具列表。

    Returns:
        符合 OpenAI Responses 规范的合并工具列表。
    """
    normalized: list[dict[str, Any]] = []
    existing_names: set[str] = set()

    for t in existing_tools:
        if not isinstance(t, dict):
            continue
        if t.get("type") == "function":
            fn = t.get("function")
            if isinstance(fn, dict) and fn.get("name"):
                name = fn["name"]
                normalized.append({
                    "type": "function",
                    "name": name,
                    "description": fn.get("description", ""),
                    "parameters": fn.get("parameters", {"type": "object", "properties": {}}),
                })
                existing_names.add(name)
            elif t.get("name"):
                normalized.append(t)
                existing_names.add(t["name"])
        else:
            normalized.append(t)

    merged = list(normalized)
    for rt in RESPONSES_TOOLS:
        if rt["name"] not in existing_names:
            merged.append(rt)
    return merged


_orig_httpx_send = httpx.AsyncClient.send
_is_patched = False


def _is_opencode_url(url: httpx.URL) -> bool:
    host = (url.host or "").lower()
    return "opencode.ai" in host


def _build_fwd_request(method: str, url: Any, headers: dict[str, str], body: dict[str, Any], orig_request: httpx.Request) -> httpx.Request:
    new_headers = dict(headers)
    new_headers.pop("content-length", None)
    new_headers.pop("Content-Length", None)
    req_ext = dict(getattr(orig_request, "extensions", {}))
    req_ext["timeout"] = httpx.Timeout(120.0, connect=30.0).as_dict()
    return httpx.Request(
        method=method,
        url=url,
        headers=new_headers,
        content=json.dumps(body).encode("utf-8"),
        extensions=req_ext,
    )


async def _patched_httpx_send(self: httpx.AsyncClient, request: httpx.Request, *args: Any, **kwargs: Any) -> httpx.Response:
    if not _is_opencode_url(request.url):
        return await _orig_httpx_send(self, request, *args, **kwargs)

    # 修复 AstrBot 底层 create_proxy_client 默认 5.0 秒超时导致的频繁中断问题
    timeout_obj = getattr(self, "timeout", None)
    if timeout_obj is not None:
        read_val = getattr(timeout_obj, "read", None)
        if isinstance(read_val, (int, float)) and read_val <= 10.0:
            self.timeout = httpx.Timeout(120.0, connect=30.0)

    # 1. 注入 OpenCode 客户端特征请求头
    request.headers["User-Agent"] = "opencode/1.18.32 ai-sdk/provider-utils/4.0.40 runtime/bun/1.3.14"
    request.headers["x-opencode-client"] = "cli"
    request.headers["x-opencode-project"] = "global"
    request.headers["x-opencode-session"] = "ses_" + generate_canonical_id(True)
    request.headers["x-opencode-request"] = "msg_" + generate_canonical_id(False)

    body = None
    if request.content:
        try:
            body = json.loads(request.content.decode("utf-8"))
        except Exception:
            body = None

    if not body or not isinstance(body, dict):
        return await _orig_httpx_send(self, request, *args, **kwargs)

    model = str(body.get("model", "")).lower()
    use_responses_endpoint = is_responses_native_model(model)
    url_path = request.url.path

    # =========================================================================
    # 场景 1: 调用方请求 /responses (如 AstrBot openai_responses 适配器)
    # =========================================================================
    if url_path.endswith("/responses"):
        is_caller_stream = body.get("stream", False)

        if use_responses_endpoint:
            # 真正的 Responses 模型 (Muse): 保持 /responses，注入 Responses 工具定义并强制流式
            body["tools"] = _merge_responses_tools(body.get("tools", []))
            body["stream"] = True

            req_fwd = _build_fwd_request(request.method, request.url, request.headers, body, request)
            upstream_resp = await _orig_httpx_send(self, req_fwd, *args, **kwargs)
            if upstream_resp.status_code != 200:
                return upstream_resp

            if is_caller_stream:
                return upstream_resp
            else:
                terminal_resp = None
                read_timeout = _get_read_timeout(self)
                try:
                    async with asyncio.timeout(read_timeout):
                        async for line in upstream_resp.aiter_lines():
                            if line.startswith("data: "):
                                chunk_str = line[6:].strip()
                                if chunk_str == "[DONE]":
                                    break
                                try:
                                    ev = json.loads(chunk_str)
                                    if ev.get("type") == "response.completed":
                                        terminal_resp = ev.get("response")
                                except Exception:
                                    pass
                except TimeoutError:
                    raise httpx.ReadTimeout(f"OpenCode upstream response timed out after {read_timeout}s", request=request)

                if terminal_resp:
                    return httpx.Response(status_code=200, headers={"Content-Type": "application/json"}, content=json.dumps(terminal_resp).encode("utf-8"), request=request)
                raise RuntimeError("未能从 OpenCode Responses 流中接收到 response.completed 事件")

        else:
            # ChatCompletions 模型 (glm-5.3-flash-free, deepseek-v4.1-flash-free, space-bunny 等):
            # 自动转译 input 为标准 messages，并路由至 /chat/completions
            target_url = request.url.copy_with(path=url_path.replace("/responses", "/chat/completions"))
            input_data = body.pop("input", [])
            messages = _convert_input_to_chat_messages(input_data)
            instructions = body.pop("instructions", None)
            if instructions:
                messages.insert(0, {"role": "system", "content": str(instructions)})

            body["messages"] = messages
            body["tools"] = _merge_chat_tools(body.pop("tools", []))
            if "max_output_tokens" in body and "max_tokens" not in body:
                body["max_tokens"] = body.pop("max_output_tokens")
            body["stream"] = True

            req_fwd = _build_fwd_request(request.method, target_url, request.headers, body, request)
            upstream_resp = await _orig_httpx_send(self, req_fwd, *args, **kwargs)
            if upstream_resp.status_code != 200:
                return upstream_resp

            # 将 ChatCompletions SSE 流包装为 Responses 规范回传给 openai_responses 适配器
            if is_caller_stream:
                async def sse_chat_to_responses():
                    yield b'event: response.created\ndata: {"type":"response.created","sequence_number":0}\n\n'
                    full_content = ""
                    reasoning_content = ""
                    accumulated_tools: dict[int, dict[str, str]] = {}
                    usage_info: dict[str, Any] = {}

                    async for line in upstream_resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        chunk_str = line[6:].strip()
                        if chunk_str == "[DONE]":
                            break
                        try:
                            c_obj = json.loads(chunk_str)
                            if "usage" in c_obj and isinstance(c_obj["usage"], dict):
                                usage_info = c_obj["usage"]
                            choices = c_obj.get("choices", [])
                            if choices:
                                delta = choices[0].get("delta", {})

                                # 1. 文本内容增量
                                delta_text = delta.get("content", "")
                                if delta_text:
                                    full_content += delta_text
                                    ev_data = {
                                        "type": "response.output_text.delta",
                                        "delta": delta_text,
                                    }
                                    yield f"event: response.output_text.delta\ndata: {json.dumps(ev_data)}\n\n".encode("utf-8")

                                # 2. 思考过程增量 (DeepSeek / GLM 等)
                                delta_reasoning = delta.get("reasoning_content", "")
                                if delta_reasoning:
                                    reasoning_content += delta_reasoning
                                    ev_data = {
                                        "type": "response.reasoning_text.delta",
                                        "delta": delta_reasoning,
                                    }
                                    yield f"event: response.reasoning_text.delta\ndata: {json.dumps(ev_data)}\n\n".encode("utf-8")

                                # 3. 工具调用增量
                                for tc in delta.get("tool_calls", []):
                                    idx = tc.get("index", 0)
                                    if idx not in accumulated_tools:
                                        accumulated_tools[idx] = {
                                            "id": tc.get("id") or "",
                                            "name": tc.get("function", {}).get("name") or "",
                                            "arguments": tc.get("function", {}).get("arguments") or "",
                                        }
                                    else:
                                        if tc.get("id"):
                                            accumulated_tools[idx]["id"] = tc["id"]
                                        fn = tc.get("function", {})
                                        if fn.get("name"):
                                            accumulated_tools[idx]["name"] += fn["name"]
                                        if fn.get("arguments"):
                                            accumulated_tools[idx]["arguments"] += fn["arguments"]
                        except Exception:
                            pass

                    now_ts = int(time.time() * 1000)
                    output_items: list[dict[str, Any]] = []
                    if reasoning_content:
                        output_items.append({
                            "type": "reasoning",
                            "content": [{"type": "reasoning_text", "text": reasoning_content}],
                            "summary": [],
                        })
                    if full_content or not accumulated_tools:
                        output_items.append({
                            "id": f"msg_{now_ts}",
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": full_content}],
                        })
                    for idx in sorted(accumulated_tools.keys()):
                        tc = accumulated_tools[idx]
                        output_items.append({
                            "type": "function_call",
                            "call_id": tc["id"] or f"call_{now_ts}_{idx}",
                            "name": tc["name"],
                            "arguments": tc["arguments"],
                        })

                    comp_ev = {
                        "type": "response.completed",
                        "response": {
                            "id": f"resp_{now_ts}",
                            "object": "response",
                            "status": "completed",
                            "output": output_items,
                            "usage": usage_info or {"total_tokens": 0},
                        },
                    }
                    yield f"event: response.completed\ndata: {json.dumps(comp_ev)}\n\n".encode("utf-8")

                return httpx.Response(status_code=200, headers={"Content-Type": "text/event-stream"}, content=sse_chat_to_responses(), request=request)
            else:
                full_content = ""
                reasoning_content = ""
                accumulated_tools: dict[int, dict[str, str]] = {}
                usage_info: dict[str, Any] = {}
                read_timeout = _get_read_timeout(self)
                try:
                    async with asyncio.timeout(read_timeout):
                        async for line in upstream_resp.aiter_lines():
                            if not line.startswith("data: "):
                                continue
                            chunk_str = line[6:].strip()
                            if chunk_str == "[DONE]":
                                break
                            try:
                                c_obj = json.loads(chunk_str)
                                if "usage" in c_obj and isinstance(c_obj["usage"], dict):
                                    usage_info = c_obj["usage"]
                                choices = c_obj.get("choices", [])
                                if choices:
                                    delta = choices[0].get("delta", {})
                                    if delta.get("content"):
                                        full_content += delta["content"]
                                    if delta.get("reasoning_content"):
                                        reasoning_content += delta["reasoning_content"]
                                    for tc in delta.get("tool_calls", []):
                                        idx = tc.get("index", 0)
                                        if idx not in accumulated_tools:
                                            accumulated_tools[idx] = {
                                                "id": tc.get("id") or "",
                                                "name": tc.get("function", {}).get("name") or "",
                                                "arguments": tc.get("function", {}).get("arguments") or "",
                                            }
                                        else:
                                            if tc.get("id"):
                                                accumulated_tools[idx]["id"] = tc["id"]
                                            fn = tc.get("function", {})
                                            if fn.get("name"):
                                                accumulated_tools[idx]["name"] += fn["name"]
                                            if fn.get("arguments"):
                                                accumulated_tools[idx]["arguments"] += fn["arguments"]
                            except Exception:
                                pass
                except TimeoutError:
                    raise httpx.ReadTimeout(f"OpenCode upstream response timed out after {read_timeout}s", request=request)

                now_ts = int(time.time() * 1000)
                output_items: list[dict[str, Any]] = []
                if reasoning_content:
                    output_items.append({
                        "type": "reasoning",
                        "content": [{"type": "reasoning_text", "text": reasoning_content}],
                        "summary": [],
                    })
                if full_content or not accumulated_tools:
                    output_items.append({
                        "id": f"msg_{now_ts}",
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": full_content}],
                    })
                for idx in sorted(accumulated_tools.keys()):
                    tc = accumulated_tools[idx]
                    output_items.append({
                        "type": "function_call",
                        "call_id": tc["id"] or f"call_{now_ts}_{idx}",
                        "name": tc["name"],
                        "arguments": tc["arguments"],
                    })

                response_obj = {
                    "id": f"resp_{now_ts}",
                    "object": "response",
                    "status": "completed",
                    "output": output_items,
                    "usage": usage_info or {"total_tokens": 0},
                }
                return httpx.Response(status_code=200, headers={"Content-Type": "application/json"}, content=json.dumps(response_obj).encode("utf-8"), request=request)

    # =========================================================================
    # 场景 2: 调用方请求 /chat/completions (如 AstrBot openai_chat_completion 适配器)
    # =========================================================================
    elif url_path.endswith("/chat/completions"):
        is_caller_stream = body.get("stream", False)
        if not use_responses_endpoint:
            # 原生 ChatCompletions 模型: 保持 /chat/completions，规范化消息与补齐工具并强制流式
            body["messages"] = _convert_input_to_chat_messages(body.get("messages", []))
            body["tools"] = _merge_chat_tools(body.get("tools", []))
            body["stream"] = True

            req_fwd = _build_fwd_request(request.method, request.url, request.headers, body, request)
            upstream_resp = await _orig_httpx_send(self, req_fwd, *args, **kwargs)
            if upstream_resp.status_code != 200:
                return upstream_resp

            if is_caller_stream:
                return upstream_resp
            else:
                full_text = ""
                accumulated_tools: dict[int, dict[str, str]] = {}
                usage_info: dict[str, Any] = {}
                read_timeout = _get_read_timeout(self)
                try:
                    async with asyncio.timeout(read_timeout):
                        async for line in upstream_resp.aiter_lines():
                            if not line.startswith("data: "):
                                continue
                            chunk_str = line[6:].strip()
                            if chunk_str == "[DONE]":
                                break
                            try:
                                c_obj = json.loads(chunk_str)
                                if "usage" in c_obj and isinstance(c_obj["usage"], dict):
                                    usage_info = c_obj["usage"]
                                choices = c_obj.get("choices", [])
                                if choices:
                                    delta = choices[0].get("delta", {})
                                    if delta.get("content"):
                                        full_text += delta["content"]
                                    for tc in delta.get("tool_calls", []):
                                        idx = tc.get("index", 0)
                                        if idx not in accumulated_tools:
                                            accumulated_tools[idx] = {
                                                "id": tc.get("id") or "",
                                                "name": tc.get("function", {}).get("name") or "",
                                                "arguments": tc.get("function", {}).get("arguments") or "",
                                            }
                                        else:
                                            if tc.get("id"):
                                                accumulated_tools[idx]["id"] = tc["id"]
                                            fn = tc.get("function", {})
                                            if fn.get("name"):
                                                accumulated_tools[idx]["name"] += fn["name"]
                                            if fn.get("arguments"):
                                                accumulated_tools[idx]["arguments"] += fn["arguments"]
                            except Exception:
                                pass
                except TimeoutError:
                    raise httpx.ReadTimeout(f"OpenCode upstream response timed out after {read_timeout}s", request=request)

                msg_obj: dict[str, Any] = {"role": "assistant", "content": full_text}
                finish_reason = "stop"
                if accumulated_tools:
                    msg_obj["tool_calls"] = [
                        {
                            "id": accumulated_tools[i]["id"] or f"call_{int(time.time()*1000)}_{i}",
                            "type": "function",
                            "function": {
                                "name": accumulated_tools[i]["name"],
                                "arguments": accumulated_tools[i]["arguments"],
                            },
                        }
                        for i in sorted(accumulated_tools.keys())
                    ]
                    finish_reason = "tool_calls"

                chat_comp = {
                    "id": f"chatcmpl_{int(time.time())}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{"index": 0, "message": msg_obj, "finish_reason": finish_reason}],
                    "usage": usage_info or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                }
                return httpx.Response(status_code=200, headers={"Content-Type": "application/json"}, content=json.dumps(chat_comp).encode("utf-8"), request=request)
        else:
            # Muse 系列模型在 /chat/completions 下被调用: 自动转至 /responses
            target_url = request.url.copy_with(path=url_path.replace("/chat/completions", "/responses"))
            messages = body.pop("messages", [])
            body["input"] = messages
            body["tools"] = _merge_responses_tools(body.get("tools", []))
            body["stream"] = True

            req_fwd = _build_fwd_request(request.method, target_url, request.headers, body, request)
            upstream_resp = await _orig_httpx_send(self, req_fwd, *args, **kwargs)
            if upstream_resp.status_code != 200:
                return upstream_resp

            if is_caller_stream:
                async def sse_resp_to_chat():
                    async for line in upstream_resp.aiter_lines():
                        if line.startswith("data: "):
                            chunk_str = line[6:].strip()
                            if chunk_str == "[DONE]":
                                break
                            try:
                                ev = json.loads(chunk_str)
                                if ev.get("type") == "response.output_text.delta":
                                    d_text = ev.get("delta", "")
                                    c_chunk = {
                                        "id": "chatcmpl-chunk",
                                        "object": "chat.completion.chunk",
                                        "choices": [{"index": 0, "delta": {"content": d_text}, "finish_reason": None}],
                                    }
                                    yield f"data: {json.dumps(c_chunk)}\n\n".encode("utf-8")
                            except Exception:
                                pass
                    yield b"data: [DONE]\n\n"

                return httpx.Response(status_code=200, headers={"Content-Type": "text/event-stream"}, content=sse_resp_to_chat(), request=request)
            else:
                full_text = ""
                read_timeout = _get_read_timeout(self)
                try:
                    async with asyncio.timeout(read_timeout):
                        async for line in upstream_resp.aiter_lines():
                            if line.startswith("data: "):
                                chunk_str = line[6:].strip()
                                if chunk_str == "[DONE]":
                                    break
                                try:
                                    ev = json.loads(chunk_str)
                                    if ev.get("type") == "response.output_text.delta":
                                        full_text += ev.get("delta", "")
                                except Exception:
                                    pass
                except TimeoutError:
                    raise httpx.ReadTimeout(f"OpenCode upstream response timed out after {read_timeout}s", request=request)
                chat_comp = {
                    "id": f"chatcmpl_{int(time.time())}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": full_text}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                }
                return httpx.Response(status_code=200, headers={"Content-Type": "application/json"}, content=json.dumps(chat_comp).encode("utf-8"), request=request)

    return await _orig_httpx_send(self, request, *args, **kwargs)


def patch_httpx_for_opencode() -> None:
    """激活 httpx 请求拦截器"""
    global _is_patched
    if not _is_patched:
        httpx.AsyncClient.send = _patched_httpx_send
        _is_patched = True
        logger.info("[OpenCode] 已成功挂载 OpenCode 协议拦截器 (httpx.AsyncClient.send)")


def unpatch_httpx_for_opencode() -> None:
    """卸载 httpx 请求拦截器"""
    global _is_patched
    if _is_patched:
        httpx.AsyncClient.send = _orig_httpx_send
        _is_patched = False
        logger.info("[OpenCode] 已卸载 OpenCode 协议拦截器")
