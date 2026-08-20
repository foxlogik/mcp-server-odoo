#!/usr/bin/env python3
"""Rebuild the captured MCP failure corpus from local Claude Code transcripts.

The corpus in ``mcp_failures.json`` is the input side of every Odoo MCP call that
failed in a real session: the tool, its arguments, and a short head of the error
that came back. ``test_failure_corpus.py`` replays those arguments through the
local guards to prove each one is now caught (or repaired) before the RPC.

The transcripts are developer-machine artifacts, not part of the repo, so this
script only runs where they exist. Re-run it to widen the corpus:

    python tests/fixtures/build_failure_corpus.py --days 30

Text is scrubbed before it is written: no URLs, no emails, error heads truncated.
"""

import argparse
import json
import os
import re
import time
from pathlib import Path

ROOTS = [
    Path.home() / ".claude" / "projects",
    Path.home() / ".claude-tenants",
]
OUT = Path(__file__).parent / "mcp_failures.json"

# A result whose payload starts with one of these keys is a success envelope from
# the server, not an error — the word "error" appearing inside a record's data
# (a task named "Application Error") must not count as a failure.
SUCCESS_HEAD = re.compile(
    r'^\s*\{"(model|record|records|models|fields|success|count|ids|result|data|access|defaults)"'
)

SCRUB = [
    (re.compile(r"https?://[^\s\"'>]+"), "<url>"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "<email>"),
]

# Ordered most-specific first: the first pattern that matches names the class.
CLASSES = [
    ("arg-name", r"record_id\s+Field required|validation error for"),
    ("json-unparseable", r"could not be parsed as JSON|InputValidationError"),
    ("tool-missing", r"No such tool available"),
    ("harness-permission", r"requested permissions to use"),
    ("invalid-field", r"Invalid field"),
    ("access-denied", r"not allowed to access|top-secret records|AccessError|doesn't have '\w+' access"),
    ("impersonation", r"Only system administrators may use MCP user impersonation"),
    ("method-not-allowed", r"is not callable on .* via call_method"),
    ("record-missing", r"Record not found|MissingError"),
    ("model-missing", r"KeyError: '[\w.]+'|Object .* doesn'?t exist|Model .* not found"),
    ("bad-leaf", r"not enough values to unpack|Invalid leaf|tuple index out of range"),
    ("sanitizer-dump", r"xmlrpc_2 response|dispatch_rpc"),
    ("odoo-usererror", r"ValidationError|UserError|constraint"),
    ("transport", r"timed? out|timeout|refused|unreachable|Max retries|SSL"),
    ("response-too-large", r"too large|exceeds maximum|token limit"),
]


def classify(text: str) -> str:
    for name, pattern in CLASSES:
        if re.search(pattern, text, re.IGNORECASE):
            return name
    return "unclassified"


def scrub(text: str, limit: int = 240) -> str:
    for pattern, replacement in SCRUB:
        text = pattern.sub(replacement, text)
    return " ".join(text.split())[:limit]


def flatten(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(
                    block.get("text", "") if block.get("type") == "text" else json.dumps(block)[:4000]
                )
            else:
                parts.append(str(block))
        return "\n".join(parts)
    return ""


def collect(days: int) -> list:
    cutoff = time.time() - days * 86400
    records = []
    for root in ROOTS:
        if not root.exists():
            continue
        for dirpath, _, names in os.walk(root):
            for name in names:
                if not name.endswith(".jsonl"):
                    continue
                path = os.path.join(dirpath, name)
                try:
                    if os.path.getmtime(path) < cutoff:
                        continue
                except OSError:
                    continue
                records.extend(_scan(path))
    return records


def _scan(path: str) -> list:
    calls = {}
    found = []
    with open(path, "r", errors="replace") as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            message = event.get("message") or {}
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use" and str(block.get("name", "")).startswith(
                    "mcp__odoo"
                ):
                    calls[block.get("id")] = (block["name"], block.get("input") or {})
                elif block.get("type") == "tool_result" and block.get("tool_use_id") in calls:
                    tool, args = calls[block["tool_use_id"]]
                    text = flatten(block.get("content"))
                    if SUCCESS_HEAD.match(text) or '"success": true' in text or '"success":true' in text:
                        continue
                    parts = tool.split("__")
                    found.append(
                        {
                            "tool": parts[2] if len(parts) > 2 else parts[-1],
                            "server": parts[1] if len(parts) > 1 else "",
                            "args": args,
                            "error_class": classify(text),
                            "error_head": scrub(text),
                        }
                    )
    return found


def dedupe(records: list) -> list:
    """One entry per distinct (tool, argument shape, class), keeping a hit count."""
    seen = {}
    for record in records:
        key = json.dumps(
            [record["tool"], record["args"], record["error_class"]], sort_keys=True, default=str
        )
        if key in seen:
            seen[key]["count"] += 1
        else:
            entry = dict(record, count=1)
            seen[key] = entry
    return sorted(seen.values(), key=lambda r: (-r["count"], r["error_class"], r["tool"]))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=20)
    args = parser.parse_args()

    records = dedupe(collect(args.days))
    OUT.write_text(json.dumps({"window_days": args.days, "cases": records}, indent=1) + "\n")

    counts = {}
    for record in records:
        counts[record["error_class"]] = counts.get(record["error_class"], 0) + record["count"]
    print(f"{sum(r['count'] for r in records)} failures -> {len(records)} distinct cases -> {OUT}")
    for name, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {count:5}  {name}")


if __name__ == "__main__":
    main()
