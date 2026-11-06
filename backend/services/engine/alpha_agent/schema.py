"""Explicit startup access to canonical mining schema migrations."""

from __future__ import annotations

import re


_DOLLAR_TAG = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$")


def _split_sql_statements(sql: str) -> list[str]:
    """Split SQL on top-level semicolons, preserving quoted/function bodies.

    Factor migrations contain PostgreSQL ``DO $tag$ ... $tag$`` blocks, whose
    PL/pgSQL bodies have their own semicolons. A plain ``str.split(';')`` cuts
    those blocks apart and leaves engine startup unable to initialize schema.
    """
    statements: list[str] = []
    start = 0
    i = 0
    quote: str | None = None
    dollar_tag: str | None = None
    line_comment = False
    block_depth = 0

    while i < len(sql):
        char = sql[i]
        following = sql[i + 1] if i + 1 < len(sql) else ""

        if line_comment:
            if char in "\r\n":
                line_comment = False
            i += 1
            continue
        if block_depth:
            if char == "/" and following == "*":
                block_depth += 1
                i += 2
            elif char == "*" and following == "/":
                block_depth -= 1
                i += 2
            else:
                i += 1
            continue
        if dollar_tag is not None:
            if sql.startswith(dollar_tag, i):
                i += len(dollar_tag)
                dollar_tag = None
            else:
                i += 1
            continue
        if quote is not None:
            if char == quote:
                if following == quote:
                    i += 2
                else:
                    quote = None
                    i += 1
            elif char == "\\" and quote == "'" and following:
                i += 2
            else:
                i += 1
            continue

        if char == "-" and following == "-":
            line_comment = True
            i += 2
        elif char == "/" and following == "*":
            block_depth = 1
            i += 2
        elif char in ("'", '"'):
            quote = char
            i += 1
        elif char == "$":
            match = _DOLLAR_TAG.match(sql, i)
            if match:
                dollar_tag = match.group(0)
                i = match.end()
            else:
                i += 1
        elif char == ";":
            statement = sql[start:i].strip()
            if statement:
                statements.append(statement)
            start = i + 1
            i += 1
        else:
            i += 1

    tail = sql[start:].strip()
    if tail:
        statements.append(tail)
    return statements


def task_schema_statements() -> list[str]:
    # OSS-ADAPT: 开源版 data/ 下没有 upgrade_v1.0.2x~6x upgrade 文件；
    # 因子挖掘表由 backend/shared/db_init.sql §68 + data/upgrade_v1.0.16.sql 创建，
    # 此处返回空列表，调用方直接跳过重放。
    return []
