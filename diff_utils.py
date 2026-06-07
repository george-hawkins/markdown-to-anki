import difflib
from typing import Any


class ObjectDiff:
    CONTEXT_CHARS = 8

    def compare(self, a: Any, b: Any) -> list[str]:
        if type(a) != type(b):
            return [f"type mismatch: {type(a).__name__} vs {type(b).__name__}"]
        if isinstance(a, str):
            return self._string_diff(a, b)
        if isinstance(a, list):
            return self._compare_lists(a, b)
        if isinstance(a, dict):
            return self._compare_dicts(a, b)
        return self._compare_values("value", a, b)

    def _compare_lists(self, a: list, b: list) -> list[str]:
        lines = []
        for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes():
            if op == "equal":
                continue
            if op == "replace":
                n = min(i2 - i1, j2 - j1)
                for i, j in zip(range(i1, i1 + n), range(j1, j1 + n)):
                    lines.extend(self._compare_values(f"[{i}]", a[i], b[j]))
                for i in range(i1 + n, i2):
                    lines.append(f"[{i}]: removed {a[i]!r}")
                for j in range(j1 + n, j2):
                    lines.append(f"[{j}]: added {b[j]!r}")
            elif op == "delete":
                for i in range(i1, i2):
                    lines.append(f"[{i}]: removed {a[i]!r}")
            elif op == "insert":
                for j in range(j1, j2):
                    lines.append(f"[{j}]: added {b[j]!r}")
        return lines

    def _compare_dicts(self, a: dict, b: dict) -> list[str]:
        lines = []
        for key in sorted(set(a) | set(b)):
            if key not in a:
                lines.append(f"{key!r}: added {b[key]!r}")
            elif key not in b:
                lines.append(f"{key!r}: removed {a[key]!r}")
            elif a[key] != b[key]:
                lines.extend(self._compare_values(repr(key), a[key], b[key]))
        return lines

    def _compare_values(self, label: str, a: Any, b: Any) -> list[str]:
        if isinstance(a, str) and isinstance(b, str):
            return [f"{label}: {d}" for d in self._string_diff(a, b)]
        return [f"{label}: {a!r} → {b!r}"]

    def _string_diff(self, a: str, b: str) -> list[str]:
        changes = []
        for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes():
            if op == "equal":
                continue
            left_ctx = self._left_context(a, i1)
            right_ctx = self._right_context(b, j2)
            if op == "replace":
                changes.append(f"{left_ctx}[{a[i1:i2]} → {b[j1:j2]}]{right_ctx}")
            elif op == "delete":
                changes.append(f"{left_ctx}[{a[i1:i2]} → ]{right_ctx}")
            elif op == "insert":
                changes.append(f"{left_ctx}[ → {b[j1:j2]}]{right_ctx}")
        return changes

    def _left_context(self, text: str, pos: int) -> str:
        start = max(0, pos - self.CONTEXT_CHARS)
        newline = text.rfind("\n", start, pos)
        if newline != -1:
            start = newline + 1
        return text[start:pos]

    def _right_context(self, text: str, pos: int) -> str:
        end = min(len(text), pos + self.CONTEXT_CHARS)
        newline = text.find("\n", pos, end)
        if newline != -1:
            end = newline
        return text[pos:end]
