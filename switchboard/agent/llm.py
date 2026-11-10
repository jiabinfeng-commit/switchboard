"""真实模型适配：OpenAI 兼容的 chat completions 接口。

国内几家（DeepSeek、通义、Kimi）都提供 OpenAI 兼容端点，所以这里不绑定厂商，
只依赖 base_url + model 两个配置。

这个类唯一的职责是把「模型返回的文本」变成「runtime 认识的 ModelOutput」。
格式不对就抛 ModelError，由 runtime 打回重来——解析失败的判断只在这里做一次。
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Mapping

from openai import OpenAI

from switchboard.agent.input import ModelView
from switchboard.agent.prompt import build_data_prompt, build_system_prompt
from switchboard.agent.questions import AskKind, AskUser
from switchboard.agent.runtime import (
    Draft,
    Model,
    ModelError,
    ModelOutput,
    ToolCallRequest,
)
from switchboard.agent.tool import ToolInvocation
from switchboard.agent.verify import ValueRef

# 匹配「整段回复被 markdown 代码块包住」的情况，例如：
#   ```json
#   {"action": "draft", ...}
#   ```
#   - 外层 ^...$ 表示只剥掉整段就是代码块的输出；正文中间的代码块不动。
#   - (?:json)? 允许语言标记写成 json 或不写。
#   - (.*?) 惰性捕获，第 1 组是要保留的块内正文。
#   - re.DOTALL 让 . 能跨行匹配，否则多行 JSON 匹配不上。
_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


def parse_model_output(text: str) -> ModelOutput:
    """把模型返回的文本解析成 ModelOutput。任何不符合协议的地方都抛 ModelError。"""
    raw = text.strip()
    fenced = _FENCE.match(raw)
    if fenced:
        raw = fenced.group(1)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ModelError(f"不是合法 JSON：{exc}") from exc

    if not isinstance(data, dict):
        raise ModelError("输出必须是 JSON 对象")

    action = data.get("action")
    if action == "tool_call":
        return _parse_tool_call(data)
    if action == "draft":
        return _parse_draft(data)
    if action == "ask_user":
        return _parse_ask_user(data)
    raise ModelError(f"未知的 action：{action!r}")


def _parse_tool_call(data: Mapping[str, Any]) -> ToolCallRequest:
    """解析 tool_call：只检查协议形状，不检查工具是否存在、参数是否符合 schema。

    「工具不在白名单」和「参数不合法」由 runtime 在执行阶段处理，
    那样会留下一条失败调用记录，方便排查模型在试什么。
    这里只保证能构造出请求对象，否则抛 ModelError 让 runtime 打回重选。
    """
    tool = data.get("tool")
    arguments = data.get("arguments", {})
    if not isinstance(tool, str) or not tool:
        raise ModelError("tool_call 缺少 tool")
    if not isinstance(arguments, dict):
        raise ModelError("tool_call 的 arguments 必须是对象")
    return ToolCallRequest(tool_name=tool, arguments=arguments)


def _parse_ref(name: str, item: Any) -> ValueRef:
    """解析 {"value", "call_id", "path"} 三元组，构造一个带出处的值。

    只校验字段齐全和类型，不校验这个值是否真的来自那次调用——
    来源校验需要工具调用记录，是 runtime 的职责。
    name 只用于错误信息，让模型知道是哪个字段出的问题。
    """
    if not isinstance(item, dict):
        raise ModelError(f"字段 {name} 必须包含 value / call_id / path")
    missing = {"value", "call_id", "path"} - item.keys()
    if missing:
        raise ModelError(f"字段 {name} 缺少 {sorted(missing)}")
    if not isinstance(item["call_id"], str) or not isinstance(item["path"], str):
        raise ModelError(f"字段 {name} 的 call_id 和 path 必须是字符串")
    return ValueRef(value=item["value"], call_id=item["call_id"], path=item["path"])


def _parse_draft(data: Mapping[str, Any]) -> Draft:
    """解析草稿：fields 必须是非空对象，每个字段都要带出处。

    空草稿没有业务意义，而且会让「校验通过」变成一个假成功；
    字段结构不合法时抛 ModelError，由 runtime 打回给模型重选。
    """
    fields = data.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise ModelError("draft 缺少 fields")
    return Draft(fields={name: _parse_ref(name, item) for name, item in fields.items()})


def _parse_ask_user(data: Mapping[str, Any]) -> AskUser:
    """按 kind 解析三种追问意图，构造对应的 AskUser。

    三种 kind 需要的字段不同：missing_field 要 field+label，
    no_result 要一个 call_id，ambiguous_asset 要 candidates 列表。
    这里只做结构校验；「引用的调用是否真的返回空」等语义校验在
    runtime 的 validate_ask 里做。
    """
    kind = data.get("kind")

    if kind == AskKind.MISSING_FIELD:
        field = data.get("field")
        label = data.get("label")
        if not isinstance(field, str) or not isinstance(label, str):
            raise ModelError("missing_field 需要 field 和 label")
        return AskUser(kind=AskKind.MISSING_FIELD, field=field, label=label)

    if kind == AskKind.NO_RESULT:
        call_id = data.get("call_id")
        if not isinstance(call_id, str):
            raise ModelError("no_result 需要 call_id")
        return AskUser(kind=AskKind.NO_RESULT, call_ids=(call_id,))

    if kind == AskKind.AMBIGUOUS_ASSET:
        candidates = data.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise ModelError("ambiguous_asset 需要 candidates 列表")
        return AskUser(
            kind=AskKind.AMBIGUOUS_ASSET,
            refs=tuple(
                _parse_ref(f"candidates[{i}]", item)
                for i, item in enumerate(candidates)
            ),
        )

    raise ModelError(f"未知的追问类型：{kind!r}")


class OpenAIChatModel:
    """通过 OpenAI 兼容接口调用模型。"""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None,
        model: str,
        temperature: float = 0.0,
        timeout: float = 60.0,
        json_mode: bool = True,
    ) -> None:
        """保存连接参数并创建客户端。

        temperature 默认 0：同一输入尽量得到同一输出，评测才有可比性。
        json_mode 单独开关：部分 OpenAI 兼容端点不支持 response_format，
        默认打开，遇到 400 时可以关掉重试。
        timeout 控制单次请求，避免上游等待无限拉长。
        """
        self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)
        self._model = model
        self._temperature = temperature
        # 部分兼容端点不支持 response_format，遇到 400 就把它关掉
        self._json_mode = json_mode

    @classmethod
    def from_env(cls) -> "OpenAIChatModel":
        """从环境变量构造模型客户端。

        读取 OPENAI_API_KEY、OPENAI_BASE_URL、MODEL_NAME。
        缺少 API key 直接报错，不静默退化成假模型——假模型会让演示和评测
        看起来通过，实际上什么都没测到。
        """
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("缺少 OPENAI_API_KEY，请在 .env 里填写")
        return cls(
            api_key=api_key,
            base_url=os.getenv("OPENAI_BASE_URL") or None,
            model=os.getenv("MODEL_NAME", "deepseek-chat"),
        )

    def respond(
        self,
        view: ModelView,
        invocations: Mapping[str, ToolInvocation],
        feedback: str | None,
    ) -> ModelOutput:
        """完成一次模型调用，返回解析后的 ModelOutput。

        负责组装 system（规则、工具、输出协议）和 user（数据区）两条消息，
        发起请求，并把返回文本交给 parse_model_output 解析。
        空内容或解析失败抛 ModelError，由 runtime 决定是否打回重来；
        这里不重试、不做业务校验，只执行一次请求。
        """
        kwargs: dict[str, Any] = {
            "model": self._model,
            "temperature": self._temperature,
            "messages": [
                {"role": "system", "content": build_system_prompt(view)},
                {
                    "role": "user",
                    "content": build_data_prompt(view, invocations, feedback),
                },
            ],
        }
        if self._json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        response = self._client.chat.completions.create(**kwargs)
        content = response.choices[0].message.content
        if not content:
            raise ModelError("模型返回了空内容")

        return parse_model_output(content)


__all__ = ["OpenAIChatModel", "parse_model_output", "Model"]
