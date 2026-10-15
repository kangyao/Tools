"""在相邻的命名函数之间补空行，跳过 Lua 字面量、注释和忽略区域。"""

from __future__ import annotations

import re
from bisect import bisect_right


NON_CODE = re.compile(
    r"--\[(?P<comment_equals>=*)\[[\s\S]*?\](?P=comment_equals)\]"
    r"|--[^\r\n]*"
    r"|\[(?P<string_equals>=*)\[[\s\S]*?\](?P=string_equals)\]"
    r'''|"(?:\\[\s\S]|[^"\\])*"|'(?:\\[\s\S]|[^'\\])*'|`(?:\\[\s\S]|[^`\\])*`'''
)
TOKEN = re.compile(r"[A-Za-z_][A-Za-z_0-9]*|[^\s]")
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z_0-9]*\Z")
IGNORE = re.compile(r"--\s*stylua:\s*ignore(?:\s+(start|end))?\s*\Z")


def separate_functions(text: str, syntax: str = "Lua54") -> tuple[str, list[int]]:
    """返回补空行后的文本及原文本中缺少间距的下一函数行号。"""
    directives = []
    non_code_spans = []

    def mask(match: re.Match) -> str:
        non_code_spans.append((match.start(), match.end()))
        directive = IGNORE.fullmatch(match.group())
        if directive:
            directives.append((match.start(), match.end(), directive.group(1)))
        return re.sub(r"[^\r\n]", " ", match.group())

    code = NON_CODE.sub(mask, text)
    tokens = list(TOKEN.finditer(code))
    if not tokens:
        return text, []
    values = [token.group() for token in tokens]
    token_offsets = [token.start() for token in tokens]
    # 以词法块配对 end，避免将 if/循环的 end 当成函数结束。
    stack = []
    pairs = {}
    function_ends = {}
    candidates = []
    for index, value in enumerate(values):
        if value == "function":
            named = index + 1 < len(tokens) and IDENTIFIER.fullmatch(values[index + 1]) is not None
            start = index - 1 if index and values[index - 1] == "local" else index
            if not named and index and values[index - 1] == "=":
                line_start = max(code.rfind("\n", 0, tokens[index].start()), code.rfind("\r", 0, tokens[index].start())) + 1
                prefix = code[line_start:tokens[index].start()]
                if re.fullmatch(r"[ \t]*(?:local[ \t]+)?[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)*[ \t]*=[ \t]*", prefix):
                    named = True
                    start = bisect_right(token_offsets, line_start) - 1
                    if start < 0 or token_offsets[start] < line_start:
                        start += 1
            previous = start - 1
            while previous >= 0 and values[previous] == ";":
                previous -= 1
            if named and previous in function_ends and function_ends[previous] == len(stack):
                candidates.append((previous, start))
            stack.append((value, index, named))
        elif value in ("if", "do", "repeat"):
            # Luau 条件表达式使用 if/then/else，但没有配对的 end。
            if value == "if" and syntax in ("Luau", "All") and index and values[index - 1] in (
                "=", "return", "(", ",", "then", "else", "[", "{",
            ):
                continue
            stack.append((value, index, False))
        elif value in ("end", "until") and stack:
            kind, opening, named = stack[-1]
            if (value == "until") != (kind == "repeat"):
                continue
            stack.pop()
            pairs[opening] = index
            if kind == "function" and named:
                function_ends[index] = len(stack)

    ignored = []
    ignore_start = None
    for start, end, mode in directives:
        if mode == "start":
            if ignore_start is None:
                ignore_start = start
        elif mode == "end":
            if ignore_start is not None:
                ignored.append((ignore_start, end))
                ignore_start = None
        else:
            # 单条 ignore 覆盖下一条语句，包括函数体和表内的嵌套函数。
            index = bisect_right(token_offsets, end - 1)
            if index >= len(tokens):
                continue
            last = index
            brackets = 0
            while last < len(tokens):
                value = values[last]
                if value in ("(", "[", "{"):
                    brackets += 1
                elif value in (")", "]", "}"):
                    brackets -= 1
                if last in pairs:
                    last = pairs[last]
                following = last + 1
                if following == len(tokens) or (brackets <= 0 and re.search(r"[\r\n]", code[tokens[last].end():tokens[following].start()])):
                    break
                last += 1
            ignored.append((start, tokens[min(last, len(tokens) - 1)].end()))
    if ignore_start is not None:
        ignored.append((ignore_start, len(text)))

    line_starts = [0] + [match.end() for match in re.finditer(r"\r\n|\r|\n", text)]
    insertions = {}
    for previous, current in candidates:
        end_offset, start_offset = tokens[previous].end(), tokens[current].start()
        if any(start <= start_offset < end or start <= end_offset - 1 < end for start, end in ignored):
            continue
        old_line = bisect_right(line_starts, end_offset - 1) - 1
        new_line = bisect_right(line_starts, start_offset) - 1
        if old_line == new_line:
            continue  # StyLua 负责把同一物理行上的多条语句展开。
        position = line_starts[old_line + 1]
        # end 同行开始的多行注释属于前一个函数，空行应放在注释结束后。
        for start, end in non_code_spans:
            if start < position < end:
                old_line = bisect_right(line_starts, end - 1) - 1
                if old_line >= new_line:
                    break
                position = line_starts[old_line + 1]
        if old_line >= new_line:
            continue
        next_line_end = line_starts[old_line + 2] if old_line + 2 < len(line_starts) else len(text)
        if not text[position:next_line_end].strip():
            continue
        ending = re.search(r"(\r\n|\r|\n)$", text[line_starts[old_line]:position]).group()
        insertions[position] = (ending, new_line + 1)
    output = text
    for position, (ending, _) in sorted(insertions.items(), reverse=True):
        output = output[:position] + ending + output[position:]
    return output, [line for _, line in insertions.values()]
