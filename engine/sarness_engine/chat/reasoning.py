"""Separates model-emitted reasoning from answer text in a streamed completion.

Providers that expose reasoning natively send it in ``reasoning_content`` (llama.cpp, vLLM) or
``reasoning`` (Ollama); that path is used as-is. Some servers instead leave the model's own
``<think>…</think>`` delimiters inside ``content``. :class:`ThinkSplitter` extracts exactly that one
leading, explicitly delimited block. It never invents text and never reclassifies answer text: a
``<think>`` that appears anywhere but at the very start of the answer stays ordinary content.
"""

from __future__ import annotations

OPEN, CLOSE = "<think>", "</think>"

REASONING, ANSWER = "reasoning", "content"


class ThinkSplitter:
    def __init__(self) -> None:
        self._state = "start"  # start -> think -> answer
        self._buf = ""
        self._lead = False  # drop newlines directly after </think>, even across chunks

    def disable(self) -> list[tuple[str, str]]:
        """Stop splitting (the provider sends reasoning natively). Returns text still held back."""
        out = self.flush()
        self._state = "answer"
        return out

    def feed(self, text: str) -> list[tuple[str, str]]:
        """Consume a content fragment; returns ``(kind, text)`` pieces ready for display."""
        if not text:
            return []
        if self._state == "answer":
            if self._lead:
                text = text.lstrip("\r\n")
                self._lead = bool(not text)
            return [(ANSWER, text)] if text else []
        self._buf += text
        out: list[tuple[str, str]] = []
        if self._state == "start":
            head = self._buf.lstrip()
            if not head:
                return []  # only whitespace so far; undecided
            if head.startswith(OPEN):
                self._buf = head[len(OPEN):]
                self._state = "think"
            elif OPEN.startswith(head):
                return []  # could still become "<think>"
            else:
                self._state, buf, self._buf = "answer", self._buf, ""
                return [(ANSWER, buf)]
        if self._state == "think":
            i = self._buf.find(CLOSE)
            if i >= 0:
                if i:
                    out.append((REASONING, self._buf[:i]))
                rest = self._buf[i + len(CLOSE):].lstrip("\r\n")
                self._buf, self._state, self._lead = "", "answer", not rest
                if rest:
                    out.append((ANSWER, rest))
                return out
            # Hold back a tail that might be the start of "</think>".
            keep = 0
            for n in range(min(len(CLOSE) - 1, len(self._buf)), 0, -1):
                if CLOSE.startswith(self._buf[-n:]):
                    keep = n
                    break
            emit, self._buf = self._buf[:len(self._buf) - keep], self._buf[len(self._buf) - keep:]
            if emit:
                out.append((REASONING, emit))
        return out

    def flush(self) -> list[tuple[str, str]]:
        """End of stream: release anything held back, under the kind it was being read as."""
        buf, self._buf = self._buf, ""
        if not buf:
            return []
        return [(REASONING if self._state == "think" else ANSWER, buf)]

    def route(self, piece: str | None, native: str | None) -> tuple[str, str]:
        """One streamed delta -> ``(answer_text, reasoning_text)``. Native reasoning always wins and
        switches delimiter splitting off for the rest of the stream."""
        answer: list[str] = []
        reasoning: list[str] = []
        if native:
            for kind, text in self.disable():
                (reasoning if kind == REASONING else answer).append(text)
            reasoning.append(native)
        for kind, text in self.feed(piece or ""):
            (reasoning if kind == REASONING else answer).append(text)
        return "".join(answer), "".join(reasoning)

    def finish(self) -> tuple[str, str]:
        answer: list[str] = []
        reasoning: list[str] = []
        for kind, text in self.flush():
            (reasoning if kind == REASONING else answer).append(text)
        return "".join(answer), "".join(reasoning)
