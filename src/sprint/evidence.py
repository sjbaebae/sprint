"""Read-only, bounded MCP access to one immutable judge evidence packet.

No filesystem paths, commands or network requests are accepted from the judge.
References always resolve within the packet supplied by the operator.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path


def resolve(packet, reference):
    if not isinstance(reference, str) or not reference.startswith("packet#/"):
        raise ValueError("expected a packet#/ JSON pointer")
    value = packet
    for part in reference[8:].split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list) and key.isdigit():
            value = value[int(key)]
        elif isinstance(value, dict):
            value = value[key]
        else:
            raise ValueError("reference does not resolve")
    return value


def serialized(value):
    return (
        value
        if isinstance(value, str)
        else json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
    )


def overview(packet, *, external_sections=(), inline_characters=512):
    """Reference long strings only in explicitly selected top-level sections.

    All other sections remain intact. The operator retains the original packet
    for exact retrieval; the overview is never a substitute for source evidence.
    """

    def visit(value, reference):
        if isinstance(value, dict):
            return {
                key: visit(
                    child, reference + "/" + key.replace("~", "~0").replace("/", "~1")
                )
                for key, child in value.items()
            }
        if isinstance(value, list):
            return [
                visit(child, reference + "/" + str(index))
                for index, child in enumerate(value)
            ]
        if isinstance(value, str) and len(value) > inline_characters:
            return {
                "read_evidence": reference,
                "characters": len(value),
                "sha256": hashlib.sha256(value.encode()).hexdigest(),
            }
        return value

    return {
        key: visit(value, "packet#/" + key) if key in external_sections else value
        for key, value in packet.items()
    }


TOOLS = [
    {
        "name": "read_evidence",
        "description": "Read exact original evidence by JSON pointer. Page through long values; no content is summarized or executed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "reference": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 16000},
            },
            "required": ["reference"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_evidence",
        "description": "List immediate children at an original evidence pointer, with pagination.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "reference": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": ["reference"],
            "additionalProperties": False,
        },
    },
]


def access(packet, tool, arguments):
    reference = arguments["reference"]
    offset = arguments.get("offset", 0)
    limit = arguments.get("limit", 12000 if tool == "read_evidence" else 100)
    maximum = 16000 if tool == "read_evidence" else 100
    if set(arguments) - {"reference", "offset", "limit"}:
        raise ValueError("unexpected arguments")
    if (
        type(offset) is not int
        or offset < 0
        or type(limit) is not int
        or not 1 <= limit <= maximum
    ):
        raise ValueError("invalid page bounds")
    value = resolve(packet, reference)
    if tool == "read_evidence":
        text = serialized(value)
        end = min(offset + limit, len(text))
        if offset > len(text):
            raise ValueError("offset beyond evidence")
        return {
            "reference": reference,
            "offset": offset,
            "end": end,
            "total_characters": len(text),
            "content": text[offset:end],
            "next_offset": end if end < len(text) else None,
        }
    if tool != "list_evidence" or not isinstance(value, (dict, list)):
        raise ValueError("expected a known tool and a container reference")
    keys = list(value) if isinstance(value, dict) else list(range(len(value)))
    if offset > len(keys):
        raise ValueError("offset beyond evidence")
    entries = []
    for key in keys[offset : offset + limit]:
        child = value[key]
        pointer = reference + "/" + str(key).replace("~", "~0").replace("/", "~1")
        entries.append(
            {
                "reference": pointer,
                "type": type(child).__name__,
                "characters": len(serialized(child)),
            }
        )
    end = offset + len(entries)
    return {
        "entries": entries,
        "total_entries": len(keys),
        "next_offset": end if end < len(keys) else None,
    }


def serve(packet):
    """Serve JSON-RPC MCP over stdio; never interpret packet text as instructions."""
    for line in sys.stdin:
        request = json.loads(line)
        if "id" not in request:
            continue
        method = request.get("method")
        if method == "initialize":
            result = {
                "protocolVersion": request.get("params", {}).get(
                    "protocolVersion", "2024-11-05"
                ),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "sprint-evidence", "version": "1"},
            }
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "ping":
            result = {}
        elif method == "tools/call":
            params = request.get("params", {})
            try:
                value = access(packet, params["name"], params.get("arguments", {}))
                result = {
                    "content": [
                        {"type": "text", "text": json.dumps(value, ensure_ascii=False)}
                    ]
                }
            except (ValueError, KeyError, IndexError, TypeError) as error:
                result = {
                    "isError": True,
                    "content": [{"type": "text", "text": str(error)}],
                }
        else:
            print(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request["id"],
                        "error": {"code": -32601, "message": "Method not found"},
                    }
                ),
                flush=True,
            )
            continue
        print(
            json.dumps(
                {"jsonrpc": "2.0", "id": request["id"], "result": result},
                ensure_ascii=False,
            ),
            flush=True,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("packet", type=Path)
    args = parser.parse_args()
    serve(json.loads(args.packet.read_text()))
