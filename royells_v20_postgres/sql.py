"""Safe SQL helpers shared by the PostgreSQL adapter and migrations."""

from __future__ import annotations

import re

from .errors import IntegrityFailure


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def quote_identifier(value: str) -> str:
    """Validate and quote an identifier."""

    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise IntegrityFailure(f"Unsafe SQL identifier: {value!r}")
    return f'"{value}"'


def translate_qmark(statement: str) -> str:
    """Translate qmark parameters to Psycopg `%s` outside SQL literals.

    Repository values remain separately bound.  The translator understands
    quoted strings, quoted identifiers, line comments, block comments, and
    PostgreSQL dollar-quoted strings.
    """

    result: list[str] = []
    index = 0
    length = len(statement)
    state = "normal"
    dollar_tag = ""
    while index < length:
        char = statement[index]
        pair = statement[index : index + 2]
        if state == "normal":
            if pair == "--":
                result.append(pair)
                index += 2
                state = "line_comment"
                continue
            if pair == "/*":
                result.append(pair)
                index += 2
                state = "block_comment"
                continue
            if char == "'":
                result.append(char)
                index += 1
                state = "single"
                continue
            if char == '"':
                result.append(char)
                index += 1
                state = "double"
                continue
            if char == "$":
                match = re.match(r"\$[A-Za-z_0-9]*\$", statement[index:])
                if match:
                    dollar_tag = match.group(0)
                    result.append(dollar_tag)
                    index += len(dollar_tag)
                    state = "dollar"
                    continue
            if pair == "??":
                result.append("?")
                index += 2
                continue
            result.append("%s" if char == "?" else char)
            index += 1
            continue
        if state == "single":
            result.append(char)
            index += 1
            if char == "'" and index < length and statement[index] == "'":
                result.append("'")
                index += 1
            elif char == "'":
                state = "normal"
            continue
        if state == "double":
            result.append(char)
            index += 1
            if char == '"' and index < length and statement[index] == '"':
                result.append('"')
                index += 1
            elif char == '"':
                state = "normal"
            continue
        if state == "line_comment":
            result.append(char)
            index += 1
            if char == "\n":
                state = "normal"
            continue
        if state == "block_comment":
            if pair == "*/":
                result.append(pair)
                index += 2
                state = "normal"
            else:
                result.append(char)
                index += 1
            continue
        if state == "dollar":
            if statement.startswith(dollar_tag, index):
                result.append(dollar_tag)
                index += len(dollar_tag)
                state = "normal"
            else:
                result.append(char)
                index += 1
    return "".join(result)


def split_sql_statements(script: str) -> list[str]:
    """Split Book 18 migration SQL while respecting common SQL literals."""

    statements: list[str] = []
    current: list[str] = []
    translated = translate_qmark(script)
    state = "normal"
    index = 0
    dollar_tag = ""
    while index < len(translated):
        char = translated[index]
        pair = translated[index : index + 2]
        if state == "normal":
            if pair == "--":
                current.append(pair)
                index += 2
                state = "line_comment"
                continue
            if pair == "/*":
                current.append(pair)
                index += 2
                state = "block_comment"
                continue
            if char == "'":
                state = "single"
            elif char == '"':
                state = "double"
            elif char == "$":
                match = re.match(r"\$[A-Za-z_0-9]*\$", translated[index:])
                if match:
                    dollar_tag = match.group(0)
                    current.append(dollar_tag)
                    index += len(dollar_tag)
                    state = "dollar"
                    continue
            elif char == ";":
                value = "".join(current).strip()
                if value:
                    statements.append(value)
                current = []
                index += 1
                continue
        elif state == "single" and char == "'":
            if index + 1 < len(translated) and translated[index + 1] == "'":
                current.extend(["'", "'"])
                index += 2
                continue
            state = "normal"
        elif state == "double" and char == '"':
            if index + 1 < len(translated) and translated[index + 1] == '"':
                current.extend(['"', '"'])
                index += 2
                continue
            state = "normal"
        elif state == "line_comment" and char == "\n":
            state = "normal"
        elif state == "block_comment" and pair == "*/":
            current.append(pair)
            index += 2
            state = "normal"
            continue
        elif state == "dollar" and translated.startswith(dollar_tag, index):
            current.append(dollar_tag)
            index += len(dollar_tag)
            state = "normal"
            continue
        current.append(char)
        index += 1
    value = "".join(current).strip()
    if value:
        statements.append(value)
    return statements
