"""内置工具集:时间、日期计算、数学计算。

联网搜索、网页抓取等网络能力由 app.tools.search_tool 和 app.tools.web_tool 提供。
"""
from __future__ import annotations

import datetime
import re

from pydantic import Field

from app.tools.registry import tool


@tool("get_current_time", "获取当前日期和时间", timeout=5)
def get_current_time(tz: str = Field("local", description="时区,默认 local 本机时间")) -> str:
    if tz == "local":
        now = datetime.datetime.now()
    else:
        try:
            import zoneinfo

            now = datetime.datetime.now(zoneinfo.ZoneInfo(tz))
        except Exception:  # noqa: BLE001
            now = datetime.datetime.now()
    return {"datetime": now.strftime("%Y-%m-%d %H:%M:%S"), "weekday": now.strftime("%A")}


# 安全表达式求值器(无 eval/exec),仅支持 + - * / % 和括号
class _SafeMathParser:
    """递归下降解析算术表达式,杜绝 RCE 风险。"""

    def __init__(self, expr: str):
        self.s = expr
        self.pos = 0

    def _digit(self) -> bool:
        return self.pos < len(self.s) and self.s[self.pos].isdigit()

    def _num(self) -> float:
        start = self.pos
        while self._digit():
            self.pos += 1
        if self.pos < len(self.s) and self.s[self.pos] == '.':
            self.pos += 1
            while self._digit():
                self.pos += 1
        if self.pos == start:
            raise ValueError("期望数字")
        return float(self.s[start:self.pos])

    def _parse(self) -> float:
        left = self._term()
        while self.pos < len(self.s) and self.s[self.pos] in '+-':
            op = self.s[self.pos]
            self.pos += 1
            right = self._term()
            left = left + right if op == '+' else left - right
        return left

    def _term(self) -> float:
        left = self._factor()
        while self.pos < len(self.s) and self.s[self.pos] in '*/%':
            op = self.s[self.pos]
            self.pos += 1
            right = self._factor()
            if op == '*':
                left *= right
            elif op == '/':
                if right == 0:
                    raise ZeroDivisionError("除零")
                left /= right
            else:
                if right == 0:
                    raise ZeroDivisionError("除零")
                left %= right
        return left

    def _factor(self) -> float:
        sign = 1
        if self.pos < len(self.s) and self.s[self.pos] in '+-':
            if self.s[self.pos] == '-':
                sign = -1
            self.pos += 1
        if self.pos < len(self.s) and self.s[self.pos] == '(':
            self.pos += 1
            val = self._parse()
            if self.pos >= len(self.s) or self.s[self.pos] != ')':
                raise ValueError("缺少右括号")
            self.pos += 1
        elif self._digit():
            val = self._num()
        else:
            raise ValueError(f"非法字符: {self.s[self.pos:]!r}" if self.pos < len(self.s) else "期望表达式")
        return sign * val

    def evaluate(self) -> float:
        result = self._parse()
        if self.pos != len(self.s):
            raise ValueError(f"表达式尾部残留: {self.s[self.pos:]!r}")
        return result


def _safe_eval(expr: str) -> float:
    """安全求值,返回浮点数结果。"""
    return _SafeMathParser(expr).evaluate()


@tool("calculate", "执行数学表达式计算", timeout=5)
def calculate(expression: str = Field(description="数学表达式,如 (3+5)*2")) -> str:
    expr = expression.strip().replace(" ", "")
    if not re.fullmatch(r"[\d+\-*/().%\s]+", expr):
        return {"error": "表达式包含非法字符"}
    try:
        result = _safe_eval(expr)
        # 输出格式:整数值转为 int,浮点数保持精度
        if isinstance(result, float) and result == int(result) and result != float('inf'):
            result = int(result)
        return {"expression": expression, "result": result}
    except Exception as e:  # noqa: BLE001
        return {"error": f"计算失败: {e}"}


@tool("days_between", "计算两个日期之间的天数差", timeout=5)
def days_between(start: str = Field(description="开始日期,格式 YYYY-MM-DD"),
                 end: str = Field(description="结束日期,格式 YYYY-MM-DD")) -> str:
    try:
        d1 = datetime.date.fromisoformat(start)
        d2 = datetime.date.fromisoformat(end)
        return {"days": (d2 - d1).days}
    except ValueError as e:
        return {"error": f"日期格式错误: {e}"}
