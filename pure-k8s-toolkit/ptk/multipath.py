"""Parser for multipath.conf and its conf.d fragments.

The format is nested `section { key value }` blocks with `#` or `!` comments.
Values may be double-quoted and may contain spaces ("1 alua", "service-time 0").
"""

from __future__ import annotations

import re
import shlex
from typing import Dict, List, Optional


class Block:
    def __init__(self, name: str) -> None:
        self.name = name
        self.attrs: Dict[str, str] = {}
        self.children: List["Block"] = []

    def sections(self, name: str) -> List["Block"]:
        return [c for c in self.children if c.name == name]

    def __repr__(self) -> str:
        return f"Block({self.name!r}, attrs={self.attrs}, children={self.children})"


def _strip_comment(line: str) -> str:
    in_quote = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_quote = not in_quote
        elif ch in "#!" and not in_quote:
            return line[:i]
    return line


def parse(text: str) -> Block:
    root = Block("<root>")
    stack = [root]
    pending: Optional[str] = None  # section name whose "{" is on the next line
    for raw in text.splitlines():
        line = _strip_comment(raw).strip()
        if not line:
            continue
        # Allow "}" and "name {" to share lines with other tokens by splitting braces out.
        for part in re.split(r"(?<=\{)|(?=\})|(?<=\})", line):
            part = part.strip()
            if not part:
                continue
            if part == "}":
                if len(stack) > 1:
                    stack.pop()
                continue
            if part == "{" and pending:
                block = Block(pending)
                stack[-1].children.append(block)
                stack.append(block)
                pending = None
                continue
            if part.endswith("{"):
                block = Block(part[:-1].strip())
                stack[-1].children.append(block)
                stack.append(block)
                continue
            try:
                tokens = shlex.split(part)
            except ValueError:
                tokens = part.split()
            if len(tokens) == 1:
                pending = tokens[0]
            elif tokens:
                stack[-1].attrs[tokens[0]] = " ".join(tokens[1:])
    return root


def merge(blocks: List[Block]) -> Block:
    """Combine multipath.conf and conf.d/*.conf the way multipathd reads them:
    later top-level sections add to earlier ones."""
    merged = Block("<root>")
    by_name: Dict[str, Block] = {}
    for root in blocks:
        for section in root.children:
            target = by_name.get(section.name)
            if target is None:
                target = Block(section.name)
                by_name[section.name] = target
                merged.children.append(target)
            target.attrs.update(section.attrs)
            target.children.extend(section.children)
    return merged


def find_device(conf: Block, vendor: str, product: str) -> Optional[Block]:
    """Last matching device stanza wins, as in multipathd."""
    found = None
    for devices in conf.sections("devices"):
        for dev in devices.sections("device"):
            v, p = dev.attrs.get("vendor", ""), dev.attrs.get("product", "")
            if _matches(v, vendor) and _matches(p, product):
                found = dev
    return found


def is_blacklisted(conf: Block, vendor: str, product: str) -> bool:
    for bl in conf.sections("blacklist"):
        for dev in bl.sections("device"):
            if _matches(dev.attrs.get("vendor", ""), vendor) and \
                    _matches(dev.attrs.get("product", ".*"), product):
                return True
    return False


def _matches(pattern: str, value: str) -> bool:
    if not pattern:
        return False
    try:
        return re.search(pattern, value) is not None
    except re.error:
        return pattern == value
