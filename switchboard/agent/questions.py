"""追问：模型决定问什么，代码决定怎么问。

为什么不让模型直接写句子：对员工说的话是风险最高的输出。它是自由文本，
校验器无法逐句判断里面的事实真假——实测中模型说过「用序列号没查到」，
而它一次工具都没调。

所以追问改成结构化意图：
    missing_field    缺一个信息
    no_result        确实查过、返回空，请用户核对（必须附一个空结果的调用 id）
    ambiguous_asset  查到多条，请用户选（必须附每个候选的出处）

句子由这里的模板生成，槽位只来自工具结果，因此事实部分可校验。
代价是表达自由度下降，换来的是「对员工说的每句话，事实都有出处」。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping

from switchboard.agent.tool import ToolInvocation
from switchboard.agent.verify import ValueRef, verify_ref


class AskKind(StrEnum):
    MISSING_FIELD = "missing_field"
    NO_RESULT = "no_result"
    AMBIGUOUS_ASSET = "ambiguous_asset"


@dataclass(frozen=True)
class AskUser:
    """一次追问意图。"""

    kind: AskKind

    # missing_field 用：字段名（必须是输出 schema 里的 key）
    field: str | None = None

    # missing_field 用：给人看的短名词短语，例如「设备序列号」。
    # 它只命名需求，不陈述事实，所以允许模型给自由文本，
    # 但限制长度和标点，防止它塞进一整句断言。
    label: str | None = None

    # no_result 用：那次返回空的调用 id
    call_ids: tuple[str, ...] = ()

    # ambiguous_asset 用：每个候选的出处
    refs: tuple[ValueRef, ...] = ()


def validate_ask(
    ask: AskUser,
    invocations: Mapping[str, ToolInvocation],
    schema_fields: set[str],
) -> list[str]:
    """校验追问意图。返回问题列表，空列表表示合法。"""
    if ask.kind is AskKind.MISSING_FIELD:
        return _validate_missing_field(ask, schema_fields)
    if ask.kind is AskKind.NO_RESULT:
        return _validate_no_result(ask, invocations)
    if ask.kind is AskKind.AMBIGUOUS_ASSET:
        return _validate_ambiguous(ask, invocations)
    return [f"未知的追问类型：{ask.kind}"]


def _validate_missing_field(ask: AskUser, schema_fields: set[str]) -> list[str]:
    """校验 missing_field：字段必须在输出 schema 里，label 必须是短名词短语。

    field 必须在 schema 里，否则模型可以追问一个流程根本不存在的字段。
    label 限制长度并禁止句末标点，是为了防止模型借这个「给人看的槽位」
    写一整句断言——那段文字会原样出现在给员工的话里。
    """
    problems = []
    if not ask.field or ask.field not in schema_fields:
        problems.append(f"field 必须是输出字段之一：{sorted(schema_fields)}")
    if not ask.label:
        problems.append("缺少 label")
    elif len(ask.label) > 20 or any(ch in ask.label for ch in "。！？!?"):
        problems.append("label 只能是一个短名词短语，不能是整句")
    return problems


def _validate_no_result(
    ask: AskUser,
    invocations: Mapping[str, ToolInvocation],
) -> list[str]:
    """校验 no_result：必须引用一次成功且返回空的调用。

    这条规则堵的是实测出现过的问题：模型一次工具都没调，却对员工说
    「用序列号没查到」。调用失败也不等于没查到，返回了数据的调用更不是。
    只有「成功且结果为空」才能支撑「查过但没有」这句话。
    """
    if len(ask.call_ids) != 1:
        return ["no_result 必须且只能引用一次调用"]

    invocation = invocations.get(ask.call_ids[0])
    if invocation is None:
        return [f"没有 call_id={ask.call_ids[0]} 的记录"]
    if not invocation.ok:
        return ["引用的调用失败了，失败不等于没查到"]
    if invocation.result:
        return ["引用的调用返回了非空结果，不能说没查到"]
    return []


def _validate_ambiguous(ask: AskUser, invocations: Mapping[str, ToolInvocation]) -> list[str]:
    """校验 ambiguous_asset：至少两个候选，且每个候选都能追溯到工具结果。

    少于两个候选不构成「多选」；候选无法追溯说明模型在编选项——
    员工看到一个不存在的选项，整个流程就失去可信度。
    """
    if len(ask.refs) < 2:
        return ["ambiguous_asset 至少要给出两个候选"]

    problems = []
    for ref in ask.refs:
        verified = verify_ref(ref, invocations)
        if not verified.ok:
            problems.append(f"候选 {ref.value!r}: {verified.rejection} {verified.detail}")
    return problems


def render_question(
    ask: AskUser,
    invocations: Mapping[str, ToolInvocation],
) -> str:
    """把意图渲染成给员工的话。调用前必须已通过 validate_ask。"""
    if ask.kind is AskKind.MISSING_FIELD:
        return f"为了继续处理，还需要您提供{ask.label}。"

    if ask.kind is AskKind.NO_RESULT:
        return "根据您提供的信息没有查到对应记录，请核对后再试一次。"

    if ask.kind is AskKind.AMBIGUOUS_ASSET:
        items = "、".join(str(ref.value) for ref in ask.refs)
        return f"查到 {len(ask.refs)} 个可能匹配的结果：{items}。请问是哪一项？"

    raise ValueError(f"未知的追问类型：{ask.kind}")


def referenced_calls(ask: AskUser) -> tuple[str, ...]:
    """这次追问引用了哪些调用，用于结果里的证据字段。"""
    calls = set(ask.call_ids)
    calls.update(ref.call_id for ref in ask.refs)
    return tuple(sorted(calls))


__all__ = ["AskKind", "AskUser", "render_question", "validate_ask", "referenced_calls"]
