# -*- coding: utf-8 -*-
"""B6 RAG retrieval layer (stub for now).

A stable ``retrieve()`` is exposed so the LangGraph ``retrieve`` node is wired
today; the real retriever (built-in demo corpus + user-uploaded KB, embed/keyword
search) lands in task B6 without touching the graph.

The in-memory ``_KB`` is populated by the server's ``upload_kb`` message.
"""
from __future__ import annotations

_KB: list[str] = []


def add_document(filename: str, content: str) -> None:
    _KB.append(f"[{filename}] {content}")


def list_documents() -> list[str]:
    return [d.split("]", 1)[0] + "]" for d in _KB]


def retrieve(query: str, mode: str) -> str:
    """Return retrieved context for the planner. Empty until B6 real retriever.

    TODO(B6): embed + search ``_KB`` + built-in demo corpus; return top-k snippets.
    Interim: naive substring match so the pipeline carries *some* signal.
    """
    if not _KB:
        return ""
    hits = [d for d in _KB if any(tok in d for tok in query)]
    return "\n".join(hits[:3])
