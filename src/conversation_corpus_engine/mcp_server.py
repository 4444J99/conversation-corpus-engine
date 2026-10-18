from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .answering import build_answer, render_answer_text, search_documents_v4
from .archive_database import open_database, search, search_titles
from .archive_ingest import ArchivePaths
from .federation import list_registered_corpora, validate_corpus_root
from .paths import default_project_root as default_project_root_path
from .paths import resolve_workspace_path
from .surface_exports import build_mcp_context_payload

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "conversation-corpus-engine"
SERVER_TITLE = "Conversation Corpus Engine"

JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603

ARCHIVE_ROOT_ENV = "CCE_ARCHIVE_ROOT"
ARCHIVE_TOOL = "cce_archive_search"
ARCHIVE_LIMIT_MAX = 25


def compact_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True)


def pretty_json(payload: Any) -> str:
    return json.dumps(payload, indent=2, sort_keys=True)


def text_result(
    text: str,
    structured_content: dict[str, Any],
    *,
    is_error: bool = False,
) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": structured_content,
        "isError": is_error,
    }


def required_string(arguments: dict[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def optional_string(arguments: dict[str, Any], name: str) -> str | None:
    value = arguments.get(name)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def optional_bool(arguments: dict[str, Any], name: str, *, default: bool = False) -> bool:
    value = arguments.get(name, default)
    if isinstance(value, bool):
        return value
    raise ValueError(f"{name} must be a boolean")


def optional_limit(arguments: dict[str, Any], *, default: int = 8, maximum: int = 25) -> int:
    raw_value = arguments.get("limit", default)
    if isinstance(raw_value, bool):
        raise ValueError("limit must be an integer")
    try:
        value = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit must be an integer") from exc
    if value < 1:
        raise ValueError("limit must be at least 1")
    return min(value, maximum)


def path_from_argument(value: str | None, fallback: Path) -> Path:
    if value:
        return resolve_workspace_path(Path(value).expanduser())
    return resolve_workspace_path(fallback)


def summarize_hits(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "kind": item.get("kind"),
            "doc_id": item.get("doc_id"),
            "title": item.get("title"),
            "score": item.get("score"),
            "citations": item.get("citations", []),
            "snippet": item.get("snippet", ""),
        }
        for item in hits
    ]


class CceMcpServer:
    def __init__(
        self,
        *,
        default_project_root: Path | None = None,
        default_source_drop_root: Path | None = None,
        archive_root: Path | None = None,
    ) -> None:
        self.default_project_root = (default_project_root or default_project_root_path()).resolve()
        self.default_source_drop_root = default_source_drop_root
        self.archive_root = archive_root.resolve() if archive_root else None

    def archive_tool_definition(self) -> dict[str, Any]:
        return {
            "name": ARCHIVE_TOOL,
            "title": "Search the AI Archive",
            "description": (
                "Full-text search the private AI-conversation archive (ChatGPT, Claude, Gemini, "
                "NotebookLM, Grok, Perplexity, Copilot). Returns the best matching message per "
                "conversation. Results are the user's private conversation text and record "
                "speech acts, not verified events."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Words or a quoted phrase."},
                    "provider": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Restrict to providers, e.g. ['claude', 'grok'].",
                    },
                    "kind": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Restrict to kinds: chat, search, activity, notebook, memory.",
                    },
                    "titles_only": {"type": "boolean", "description": "Search titles only."},
                    "limit": {"type": "integer", "description": "Maximum conversations (25)."},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        }

    def tool_definitions(self) -> list[dict[str, Any]]:
        tools = self._core_tool_definitions()
        if self.archive_root is not None:
            tools.append(self.archive_tool_definition())
        return tools

    def _core_tool_definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "cce_search",
                "title": "Search CCE Corpus",
                "description": "Search a conversation corpus and return a grounded answer with citations.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Question or phrase to search in the corpus.",
                        },
                        "project_root": {
                            "type": "string",
                            "description": "Project root containing the federation registry.",
                        },
                        "corpus_id": {
                            "type": "string",
                            "description": "Registered corpus id to search. Defaults to the active default corpus.",
                        },
                        "corpus_root": {
                            "type": "string",
                            "description": "Direct corpus root. Overrides project_root and corpus_id.",
                        },
                        "mode": {
                            "type": "string",
                            "enum": ["action", "unresolved", "timeline", "family_brief"],
                            "description": "Optional retrieval mode.",
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 25,
                            "description": "Maximum number of ranked hits to return.",
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "cce_list_corpora",
                "title": "List CCE Corpora",
                "description": "List corpora registered in a CCE project.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_root": {
                            "type": "string",
                            "description": "Project root containing the federation registry.",
                        },
                        "active_only": {
                            "type": "boolean",
                            "description": "Only include active corpora.",
                        },
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "cce_surface_context",
                "title": "Build CCE Surface Context",
                "description": "Build the MCP-facing context payload for a governed CCE project.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_root": {
                            "type": "string",
                            "description": "Project root containing registry, reports, and policy state.",
                        },
                        "source_drop_root": {
                            "type": "string",
                            "description": "Provider source-drop root used for readiness checks.",
                        },
                        "include_full_payload": {
                            "type": "boolean",
                            "description": "Include the full context payload instead of only the summary.",
                        },
                    },
                    "additionalProperties": False,
                },
            },
        ]

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        args = arguments or {}
        try:
            if name == "cce_search":
                return self.search(args)
            if name == "cce_list_corpora":
                return self.list_corpora(args)
            if name == "cce_surface_context":
                return self.surface_context(args)
            if name == ARCHIVE_TOOL and self.archive_root is not None:
                return self.archive_search(args)
        except ValueError as exc:
            return text_result(str(exc), {"error": str(exc)}, is_error=True)
        raise KeyError(name)

    def project_root_from_args(self, arguments: dict[str, Any]) -> Path:
        return path_from_argument(
            optional_string(arguments, "project_root"),
            self.default_project_root,
        )

    def source_drop_root_from_args(self, arguments: dict[str, Any]) -> Path | None:
        raw_value = optional_string(arguments, "source_drop_root")
        if raw_value:
            return resolve_workspace_path(Path(raw_value).expanduser())
        if self.default_source_drop_root:
            return resolve_workspace_path(self.default_source_drop_root)
        return None

    def corpus_root_from_args(self, arguments: dict[str, Any]) -> Path:
        raw_corpus_root = optional_string(arguments, "corpus_root")
        if raw_corpus_root:
            return resolve_workspace_path(Path(raw_corpus_root).expanduser())

        project_root = self.project_root_from_args(arguments)
        corpus_id = optional_string(arguments, "corpus_id")
        corpora = list_registered_corpora(project_root, active_only=True)
        if corpus_id:
            selected = next(
                (entry for entry in corpora if entry.get("corpus_id") == corpus_id),
                None,
            )
            if not selected:
                raise ValueError(f"No active corpus registered with corpus_id={corpus_id}")
            return resolve_workspace_path(Path(selected["root"]))
        selected = next((entry for entry in corpora if entry.get("default")), None)
        if not selected and corpora:
            selected = corpora[0]
        if selected:
            return resolve_workspace_path(Path(selected["root"]))

        validation = validate_corpus_root(project_root)
        if validation["valid"]:
            return project_root
        missing = ", ".join(validation["missing_files"]) or "registry entries"
        raise ValueError(
            "No searchable corpus found. Provide corpus_root or register a corpus; "
            f"missing {missing}."
        )

    def search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = required_string(arguments, "query")
        mode = optional_string(arguments, "mode")
        if mode not in {None, "action", "unresolved", "timeline", "family_brief"}:
            raise ValueError("mode must be one of action, unresolved, timeline, or family_brief")
        limit = optional_limit(arguments)
        corpus_root = self.corpus_root_from_args(arguments)
        validation = validate_corpus_root(corpus_root)
        if not validation["valid"]:
            missing = ", ".join(validation["missing_files"])
            raise ValueError(f"Corpus root {corpus_root} is missing required files: {missing}")

        retrieval = search_documents_v4(corpus_root, query, limit=limit, mode=mode)
        answer = build_answer(query, retrieval, mode=mode)
        structured = {
            "query": query,
            "mode": mode or "default",
            "corpus_root": str(corpus_root),
            "answer": answer,
            "hits": summarize_hits(retrieval.get("hits", [])[:limit]),
            "family_focus": retrieval.get("family_focus", []),
        }
        return text_result(render_answer_text(answer), structured)

    def archive_search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if self.archive_root is None:
            raise ValueError("archive search is not configured; set --archive-root")
        query = required_string(arguments, "query")
        limit = optional_limit(arguments, default=10, maximum=ARCHIVE_LIMIT_MAX)
        titles_only = optional_bool(arguments, "titles_only", default=False)
        filters: dict[str, list[str] | None] = {}
        for key in ("provider", "kind"):
            value = arguments.get(key)
            if value is None:
                filters[key] = None
            elif isinstance(value, list) and all(isinstance(item, str) for item in value):
                filters[key] = value or None
            else:
                raise ValueError(f"{key} must be an array of strings")
        try:
            connection = open_database(ArchivePaths(self.archive_root))
        except FileNotFoundError as exc:
            raise ValueError(f"The archive search database is not built: {exc}") from exc
        try:
            if titles_only:
                results = search_titles(connection, query, limit=limit)
            else:
                results = search(
                    connection,
                    query,
                    providers=filters["provider"],
                    kinds=filters["kind"],
                    limit=limit,
                )
        finally:
            connection.close()
        structured = {"query": query, "count": len(results), "results": results}
        lines = [
            f"{hit['provider']} · {hit['created'][:10]} · {hit['title']} ({hit['uid']})"
            + (f"\n    {hit['excerpt']}" if hit.get("excerpt") else "")
            for hit in results
        ]
        return text_result("\n".join(lines) or "No matching conversations.", structured)

    def list_corpora(self, arguments: dict[str, Any]) -> dict[str, Any]:
        project_root = self.project_root_from_args(arguments)
        active_only = optional_bool(arguments, "active_only", default=False)
        corpora = list_registered_corpora(project_root, active_only=active_only)
        structured = {
            "project_root": str(project_root),
            "active_only": active_only,
            "count": len(corpora),
            "corpora": corpora,
        }
        return text_result(pretty_json(structured), structured)

    def surface_context(self, arguments: dict[str, Any]) -> dict[str, Any]:
        project_root = self.project_root_from_args(arguments)
        source_drop_root = self.source_drop_root_from_args(arguments)
        include_full_payload = optional_bool(arguments, "include_full_payload", default=False)
        payload = build_mcp_context_payload(project_root, source_drop_root=source_drop_root)
        structured = {
            "project_root": str(project_root),
            "source_drop_root": payload.get("source_drop_root"),
            "summary": payload.get("summary", {}),
            "providers": payload.get("providers", []),
            "review_queue": payload.get("review_queue", {}),
        }
        if include_full_payload:
            structured["payload"] = payload
        return text_result(pretty_json(structured), structured)

    def handle_request(self, message: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return self.error_response(
                message.get("id") if isinstance(message, dict) else None,
                JSONRPC_INVALID_REQUEST,
                "Invalid JSON-RPC request",
            )
        method = message.get("method")
        request_id = message.get("id")
        if request_id is None:
            return None
        try:
            if method == "initialize":
                params = message.get("params") or {}
                requested_version = params.get("protocolVersion") or PROTOCOL_VERSION
                return self.response(
                    request_id,
                    {
                        "protocolVersion": requested_version
                        if requested_version == PROTOCOL_VERSION
                        else PROTOCOL_VERSION,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {
                            "name": SERVER_NAME,
                            "title": SERVER_TITLE,
                            "version": __version__,
                        },
                        "instructions": "Use cce_search for grounded corpus answers and cce_surface_context for governed project context.",
                    },
                )
            if method == "ping":
                return self.response(request_id, {})
            if method == "tools/list":
                return self.response(request_id, {"tools": self.tool_definitions()})
            if method == "tools/call":
                params = message.get("params") or {}
                tool_name = params.get("name")
                if not isinstance(tool_name, str):
                    return self.error_response(
                        request_id,
                        JSONRPC_INVALID_PARAMS,
                        "tools/call requires a tool name",
                    )
                arguments = params.get("arguments") or {}
                if not isinstance(arguments, dict):
                    return self.error_response(
                        request_id,
                        JSONRPC_INVALID_PARAMS,
                        "tool arguments must be an object",
                    )
                try:
                    return self.response(request_id, self.call_tool(tool_name, arguments))
                except KeyError:
                    return self.error_response(
                        request_id, JSONRPC_INVALID_PARAMS, f"Unknown tool: {tool_name}"
                    )
            return self.error_response(
                request_id, JSONRPC_METHOD_NOT_FOUND, f"Unknown method: {method}"
            )
        except Exception as exc:  # pragma: no cover - defensive stdio server boundary
            print(f"{SERVER_NAME}: internal error: {exc}", file=sys.stderr)
            return self.error_response(request_id, JSONRPC_INTERNAL_ERROR, "Internal server error")

    @staticmethod
    def response(request_id: int | str, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def error_response(
        request_id: int | str | None,
        code: int,
        message: str,
    ) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    def serve_stdio(self) -> None:
        for raw_line in sys.stdin:
            line = raw_line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                response = self.error_response(None, JSONRPC_PARSE_ERROR, "Parse error")
            else:
                response = self.handle_request(message)
            if response is None:
                continue
            print(compact_json(response), flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the CCE MCP stdio server")
    parser.add_argument("--project-root", type=Path, default=default_project_root_path())
    parser.add_argument("--source-drop-root", type=Path)
    parser.add_argument(
        "--archive-root",
        type=Path,
        default=Path(os.environ[ARCHIVE_ROOT_ENV]) if os.environ.get(ARCHIVE_ROOT_ENV) else None,
        help=f"enable {ARCHIVE_TOOL} over this archive root (or set {ARCHIVE_ROOT_ENV})",
    )
    parser.add_argument("--version", action="store_true")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.version:
        print(f"{SERVER_NAME} {__version__}")
        return
    server = CceMcpServer(
        default_project_root=args.project_root,
        default_source_drop_root=args.source_drop_root,
        archive_root=args.archive_root,
    )
    server.serve_stdio()


if __name__ == "__main__":
    main()
