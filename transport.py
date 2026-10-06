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
    """
    生成 OpenCode 官方规范的有序唯一 ID:
    - invert=True 用于 Session ID (前缀 ses_)
    - invert=False 用于 Request ID (前缀 msg_)
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
    """
    OpenCode 后端路由规则：
    - Muse 系列模型 (如 muse-spark-1.3-contributor-free) 原生走 Responses 协议 (/responses)
    - 其他免费及开源模型 (如 space-bunny-free, mimo-v2.6-flash-free, fledge-alpha-free) 原生走 ChatCompletions 协议 (/chat/completions)
    """
    m = str(model_name or "").lower().strip()
    return m.startswith("muse-")


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
            existing_tools = body.get("tools", [])
            existing_names = {t.get("name") or t.get("function", {}).get("name") for t in existing_tools}
            merged_tools = list(existing_tools)
            for ot in RESPONSES_TOOLS:
                if ot["name"] not in existing_names:
                    merged_tools.append(ot)
            body["tools"] = merged_tools
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
            # ChatCompletions 模型 (space-bunny, mimo, fledge 等): 自动协议路由转换至 /chat/completions
            target_url = request.url.copy_with(path=url_path.replace("/responses", "/chat/completions"))
            input_data = body.pop("input", [])
            body["messages"] = input_data
            body["tools"] = CHAT_TOOLS
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
                    async for line in upstream_resp.aiter_lines():
                        if line.startswith("data: "):
                            chunk_str = line[6:].strip()
                            if chunk_str == "[DONE]":
                                break
                            try:
                                c_obj = json.loads(chunk_str)
                                choices = c_obj.get("choices", [])
                                if choices:
                                    delta = choices[0].get("delta", {})
                                    delta_text = delta.get("content", "")
                                    if delta_text:
                                        full_content += delta_text
                                        ev_data = {
                                            "type": "response.output_text.delta",
                                            "delta": delta_text,
                                        }
                                        yield f"event: response.output_text.delta\ndata: {json.dumps(ev_data)}\n\n".encode("utf-8")
                            except Exception:
                                pass
                    comp_ev = {
                        "type": "response.completed",
                        "response": {
                            "id": f"resp_{int(time.time()*1000)}",
                            "object": "response",
                            "status": "completed",
                            "output": [
                                {
                                    "id": f"msg_{int(time.time()*1000)}",
                                    "type": "message",
                                    "role": "assistant",
                                    "content": [{"type": "output_text", "text": full_content}],
                                }
                            ],
                        },
                    }
                    yield f"event: response.completed\ndata: {json.dumps(comp_ev)}\n\n".encode("utf-8")

                return httpx.Response(status_code=200, headers={"Content-Type": "text/event-stream"}, content=sse_chat_to_responses(), request=request)
            else:
                full_content = ""
                read_timeout = _get_read_timeout(self)
                try:
                    async with asyncio.timeout(read_timeout):
                        async for line in upstream_resp.aiter_lines():
                            if line.startswith("data: "):
                                chunk_str = line[6:].strip()
                                if chunk_str == "[DONE]":
                                    break
                                try:
                                    c_obj = json.loads(chunk_str)
                                    choices = c_obj.get("choices", [])
                                    if choices:
                                        delta = choices[0].get("delta", {})
                                        delta_text = delta.get("content", "")
                                        if delta_text:
                                            full_content += delta_text
                                except Exception:
                                    pass
                except TimeoutError:
                    raise httpx.ReadTimeout(f"OpenCode upstream response timed out after {read_timeout}s", request=request)
                response_obj = {
                    "id": f"resp_{int(time.time()*1000)}",
                    "object": "response",
                    "status": "completed",
                    "output": [
                        {
                            "id": f"msg_{int(time.time()*1000)}",
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": full_content}],
                        }
                    ],
                }
                return httpx.Response(status_code=200, headers={"Content-Type": "application/json"}, content=json.dumps(response_obj).encode("utf-8"), request=request)

    # =========================================================================
    # 场景 2: 调用方请求 /chat/completions (如 AstrBot openai_chat_completion 适配器)
    # =========================================================================
    elif url_path.endswith("/chat/completions"):
        is_caller_stream = body.get("stream", False)
        if not use_responses_endpoint:
            # 原生 ChatCompletions 模型: 保持 /chat/completions，补齐工具与强制流式
            body["tools"] = CHAT_TOOLS
            body["stream"] = True

            req_fwd = _build_fwd_request(request.method, request.url, request.headers, body, request)
            upstream_resp = await _orig_httpx_send(self, req_fwd, *args, **kwargs)
            if upstream_resp.status_code != 200:
                return upstream_resp

            if is_caller_stream:
                return upstream_resp
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
                                    c_obj = json.loads(chunk_str)
                                    choices = c_obj.get("choices", [])
                                    if choices:
                                        delta = choices[0].get("delta", {})
                                        delta_text = delta.get("content", "")
                                        if delta_text:
                                            full_text += delta_text
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
        else:
            # Muse 系列模型在 /chat/completions 下被调用: 自动转至 /responses
            target_url = request.url.copy_with(path=url_path.replace("/chat/completions", "/responses"))
            messages = body.pop("messages", [])
            body["input"] = messages
            body["tools"] = RESPONSES_TOOLS
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

    return await _orig_send(self, request, *args, **kwargs)


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
