#!/usr/bin/env python3
"""
ROTOR v1.1  .  MIVIM.artist bar-spine animation notation

    rotor FILE                       check, then play (trace when piped)
    rotor fmt PATH... [--dry]        put every line on the alignment law
    rotor check PATH...              diagnostics; folders are searched for *.rotor
    rotor play FILE                  live terminal preview of every spine
    rotor trace FILE [--all]         deterministic frame timeline
    rotor compile FILE [-o PATH]     JSON IR for ports (ports/ts)
    rotor spin FILE -- COMMAND ...   run a command under a ROTOR spinner

    shared flags   --spine NAME   --view NAME   --seed N   --color 16m|256|none   --no-color

    library        from rotor import Spinner, Player, load
                   with Spinner("header.rotor", view="HEADER.VIEW", message="sniffing") as sp: ...
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass, field

VERSION = "1.1.0"
DEFAULT_HOLD = 100
M32 = 0xFFFFFFFF

BUILTIN_STATES = {
    "lit":  {"color": "#ffb347", "attrs": ["bold"]},
    "dim":  {"color": "#6b645a", "attrs": []},
    "off":  {"color": "#2e2b27", "attrs": []},
    "fill": {"color": None,      "attrs": ["fill"]},
    "hot":  {"color": "#ff5c5c", "attrs": ["bold"]},
    "ok":   {"color": "#6dff9c", "attrs": []},
    "warn": {"color": "#ffd95c", "attrs": []},
}
ATTR_CODES = {"bold": "1", "faint": "2", "italic": "3", "underline": "4", "blink": "5", "fill": "7"}

ACCENT, MUTE, RULE, TEXT = "#ffb347", "#7d766c", "#3d3933", "#e8e2d8"
ERR, WARN, OK = "#ff5c5c", "#ffd95c", "#6dff9c"


class RotorError(Exception):
    """raised by the library when a file has errors; str() is the plain check report"""


# ── text width ─────────────────────────────────────────────────────────────

def dwidth(s: str) -> int:
    n = 0
    for ch in s:
        if unicodedata.combining(ch):
            continue
        n += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return n


def pad(s: str, width: int, plain_text: str | None = None) -> str:
    return s + " " * max(0, width - dwidth(s if plain_text is None else plain_text))


def clip_text(s: str, width: int) -> str:
    if dwidth(s) <= width:
        return s
    out, w = "", 0
    for ch in s:
        cw = dwidth(ch)
        if w + cw > width - 1:
            break
        out, w = out + ch, w + cw
    return out + "~"


# ── lexing ─────────────────────────────────────────────────────────────────

STR = r'"(?:[^"\\]|\\.)*"'
DECL_RE = re.compile(r'^(?:"(?P<q>[^"]+)"|(?P<b>!?[A-Za-z_][\w.]*))\s*:=(?P<rest>.*)$')
BAR_RE = re.compile(r'^\|\s*(?P<op>=>|->|<-|<=|\+\||\|)?(?P<rest>.*)$')
TOKEN_RE = re.compile(r'''
    (?P<label>[A-Za-z_]\w*:)(?!=)
  | (?P<str>''' + STR + r''')
  | (?P<ref>&!?[A-Za-z_][\w.]*)
  | (?P<rep>\*\d+)
  | (?P<chance>\?\d+(?:\.\d+)?%)
  | (?P<state>!(?:\#[0-9A-Fa-f]{6}|[A-Za-z_]\w*))
  | (?P<hold>@\d+(?:\.\d+)?(?:ms|s))
  | (?P<comma>,)
  | (?P<word>\#[0-9A-Fa-f]{6}|[A-Za-z_]\w*)
''', re.X)


class LexError(Exception):
    pass


def unquote(lit: str) -> str:
    return re.sub(r"\\(.)", r"\1", lit[1:-1])


def split_comment(s: str):
    quoted, esc = False, False
    for i, ch in enumerate(s):
        if esc:
            esc = False
        elif quoted and ch == "\\":
            esc = True
        elif ch == '"':
            quoted = not quoted
        elif not quoted and s.startswith("//", i):
            return s[:i].rstrip(), s[i:].rstrip()
    return s.rstrip(), None


def tokenize(text: str):
    toks, pos = [], 0
    while pos < len(text):
        if text[pos].isspace():
            pos += 1
            continue
        m = TOKEN_RE.match(text, pos)
        if not m:
            raise LexError(f"unreadable token at `{text[pos:pos + 14].strip()}`")
        toks.append((m.lastgroup, m.group(m.lastgroup)))
        pos = m.end()
    return toks


def parse_hold(text: str) -> int:
    if text.endswith("ms"):
        return max(1, int(round(float(text[1:-2]))))
    return max(1, int(round(float(text[1:-1]) * 1000)))


# ── randomness: ROTOR's own generator, identical in every port ─────────────

class Mulberry32:
    def __init__(self, seed: int):
        self.a = seed & M32

    def random(self) -> float:
        self.a = (self.a + 0x6D2B79F5) & M32
        a = self.a
        t = ((a ^ (a >> 15)) * (a | 1)) & M32
        t = ((t + (((t ^ (t >> 7)) * (t | 61)) & M32)) & M32) ^ t
        return ((t ^ (t >> 14)) & M32) / 4294967296


# ── model ──────────────────────────────────────────────────────────────────

@dataclass
class Diag:
    code: str
    line: int
    msg: str

    @property
    def error(self):
        return self.code.startswith("E")


@dataclass
class Member:
    op: str
    line: int
    label: str | None = None
    value: tuple | None = None
    rep: int | None = None
    chance: float | None = None
    state: str | None = None
    hold: int | None = None


@dataclass
class Group:
    name: str
    line: int
    spine: str
    track: int
    rep: int | None = None
    hold: int | None = None
    state: str | None = None
    members: list = field(default_factory=list)
    term: str | None = None
    term_line: int = 0


@dataclass
class Spine:
    name: str
    line: int
    hold: int | None = None
    state: str | None = None
    tracks: list = field(default_factory=lambda: [[]])
    loop: bool = False
    close_line: int | None = None


@dataclass
class Decl:
    name: str
    line: int
    kind: str                     # value | state
    parts: list = field(default_factory=list)
    color: str | None = None
    attrs: list = field(default_factory=list)


@dataclass
class Frame:
    group: str
    idx: int
    n: int
    label: str | None
    spans: list
    hold: int


class RefErr(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


# ── parser ─────────────────────────────────────────────────────────────────

class Doc:
    def __init__(self, text: str, path: str = "<src>"):
        self.path = path
        self.text = text.replace("\r\n", "\n").replace("\r", "\n")
        self.lines = self.text.split("\n")
        self.title = self.subtitle = None
        self.decls: dict[str, Decl] = {}
        self.spines: list[Spine] = []
        self.groups: dict[str, Group] = {}
        self.names: dict[str, int] = {}
        self.views: dict[str, str] = {}
        self.diags: list[Diag] = []
        self._parse()
        self._validate()
        self._align()
        self.diags.sort(key=lambda d: (d.line, d.code))

    # bookkeeping
    @property
    def spine_map(self):
        return {s.name: s for s in self.spines}

    def errors(self):
        return [d for d in self.diags if d.error]

    def warnings(self):
        return [d for d in self.diags if not d.error]

    def stats(self):
        groups = [g for s in self.spines for t in s.tracks for g in t]
        return len(self.spines), len(groups), sum(len(g.members) for g in groups)

    def _d(self, code, line, msg):
        self.diags.append(Diag(code, line, msg))

    def _claim(self, name, line):
        if name in self.names:
            self._d("E05", line, f"`{name}` is already declared on L{self.names[name]}")
            return False
        self.names[name] = line
        return True

    def _toks(self, text, ln):
        try:
            return tokenize(text)
        except LexError as e:
            self._d("E07", ln, str(e))
            return None

    def _mods(self, toks, allowed, line, where):
        out = dict(label=None, value=None, rep=None, chance=None, state=None, hold=None)
        for kind, text in toks or ():
            if kind == "comma":
                continue
            if kind not in allowed:
                self._d("E07", line, f"`{text}` cannot appear {where}")
                continue
            if kind == "label":
                out["label"] = text[:-1]
            elif kind in ("str", "ref"):
                if out["value"] is not None:
                    self._d("E10", line, "a member carries exactly one value")
                    continue
                out["value"] = ("str", unquote(text)) if kind == "str" else ("ref", text[1:])
            elif kind == "rep":
                n = int(text[1:])
                if n < 1:
                    self._d("E07", line, f"`{text}`: repeat count starts at 1")
                else:
                    out["rep"] = n
            elif kind == "chance":
                c = float(text[1:-1])
                if c > 100:
                    self._d("E07", line, f"`{text}`: chance tops out at 100%")
                else:
                    out["chance"] = c
            elif kind == "state":
                out["state"] = text[1:]
            elif kind == "hold":
                out["hold"] = parse_hold(text)
        return out

    def _close_group(self, g):
        if g is not None and not g.term:
            self._d("E01", g.line, f"{g.name} never ends; finish it with `|<-` (stop) or `|<=` (loop)")

    def _parse(self):
        spine = group = track = None
        for ln, raw in enumerate(self.lines, 1):
            if "\t" in raw:
                self._d("W02", ln, "tab character; the law counts columns in spaces")
            code, _ = split_comment(raw.replace("\t", "    "))
            s = code.strip()
            if not s:
                if spine is not None and not raw.strip():
                    self._d("W05", ln, f"blank line inside {spine.name} breaks the bar")
                continue
            if s.startswith(">"):
                if spine is not None:
                    self._d("E03", ln, "header lines live outside spines")
                elif s.startswith(">>"):
                    self.subtitle = s[2:].strip()
                else:
                    self.title = s[1:].strip()
                continue
            m = DECL_RE.match(s)
            name = (m["q"] or m["b"]) if m else None
            body = m["rest"].strip() if m else s
            if body.startswith("[."):
                if spine is not None:
                    self._d("E03", ln, f"`[.` while {spine.name} (L{spine.line}) is still open; close it with `]`")
                    continue
                if not name:
                    self._d("E06", ln, 'a root needs a name: "NAME":= [.')
                    name = f"_spine{ln}"
                mods = self._mods(self._toks(body[2:], ln), {"state", "hold"}, ln, "on a root")
                self._claim(name, ln)
                spine = Spine(name, ln, hold=mods["hold"], state=mods["state"])
                track, group = spine.tracks[0], None
            elif body.startswith("|"):
                if spine is None:
                    self._d("E03", ln, 'bar outside a spine; open one with "NAME":= [.')
                    continue
                bm = BAR_RE.match(body)
                op, rest = bm["op"], bm["rest"]
                if op == "=>":
                    self._close_group(group)
                    if not name:
                        self._d("E06", ln, 'a parent needs a name: "NAME":= | =>')
                        name = f"_group{ln}"
                    mods = self._mods(self._toks(rest, ln), {"rep", "state", "hold"}, ln, "on a parent")
                    self._claim(name, ln)
                    group = Group(name, ln, spine.name, len(spine.tracks) - 1,
                                  rep=mods["rep"], hold=mods["hold"], state=mods["state"])
                    track.append(group)
                    self.groups[name] = group
                elif op in ("->", "<-", "<="):
                    if name:
                        self._d("E07", ln, "inside a spine only `=>` lines bind a name")
                    if group is None:
                        self._d("E02", ln, f"`{op}` has no parent; open a group with `| =>`")
                        continue
                    if group.term:
                        self._d("E09", ln, f"{group.name} already ended with `{group.term}` on L{group.term_line}")
                        continue
                    toks = self._toks(rest, ln)
                    if toks is None:
                        if op != "->":
                            group.term, group.term_line = op, ln
                        continue
                    mods = self._mods(toks, {"label", "str", "ref", "rep", "chance", "state", "hold"}, ln, "on a member")
                    if op != "->":
                        group.term, group.term_line = op, ln
                    if mods["value"] is None:
                        self._d("E10", ln, 'member has no value; give it a "glyph" or &REF')
                        continue
                    mem = Member(op, ln, **{k: mods[k] for k in ("label", "value", "rep", "chance", "state", "hold")})
                    if mem.label and any(x.label == mem.label for x in group.members):
                        self._d("E05", ln, f"label `{mem.label}` repeats inside {group.name}")
                    group.members.append(mem)
                elif op in ("|", "+|"):
                    if rest.strip():
                        self._d("E07", ln, f"nothing follows a break; found `{rest.strip()}`")
                    self._close_group(group)
                    group = None
                    if op == "+|":
                        track = []
                        spine.tracks.append(track)
                elif rest.strip():
                    self._d("E07", ln, f"unreadable bar line `{body}`")
            elif body.startswith("]"):
                if spine is None:
                    self._d("E03", ln, "`]` with no open spine")
                    continue
                rest = body[1:].strip()
                if rest not in ("", "<="):
                    self._d("E07", ln, f"`]` takes only `<=`; found `{rest}`")
                self._close_group(group)
                spine.loop, spine.close_line = rest == "<=", ln
                self._seal(spine)
                spine = group = None
            elif name:
                if spine is not None:
                    self._d("E03", ln, "declarations live outside spines; inside, only `=>` lines bind names")
                    continue
                toks = self._toks(body, ln)
                if toks is not None:
                    self._decl(name, toks, ln)
            else:
                self._d("E07", ln, f"unreadable line `{s[:32]}`")
        if spine is not None:
            self._close_group(group)
            self._d("E03", spine.line, f"{spine.name} is never closed with `]`")
            self._seal(spine)

    def _seal(self, spine):
        for g in [g for t in spine.tracks for g in t]:
            if not g.members:                  # every member line failed to read
                spine.tracks[g.track].remove(g)
        spine.tracks = [t for t in spine.tracks if t]
        for i, t in enumerate(spine.tracks):
            for g in t:
                g.track = i
        self.spines.append(spine)

    def _decl(self, name, toks, ln):
        if name.startswith("!"):
            d = Decl(name, ln, "state")
            for kind, text in toks:
                if kind == "word" and text.startswith("#"):
                    d.color = text
                elif kind == "word" and text in ATTR_CODES:
                    d.attrs.append(text)
                else:
                    self._d("E07", ln, f"`{text}` is not a #color or attribute ({' '.join(ATTR_CODES)})")
        else:
            d = Decl(name, ln, "value")
            for kind, text in toks:
                if kind == "str":
                    d.parts.append([("str", unquote(text)), None])
                elif kind == "ref":
                    d.parts.append([("ref", text[1:]), None])
                elif kind == "state" and d.parts:
                    d.parts[-1][1] = text[1:]
                elif kind != "comma":
                    self._d("E07", ln, f"`{text}` cannot appear in a declaration")
            if not d.parts:
                self._d("E10", ln, "declaration has no value")
        if self._claim(name, ln):
            self.decls[name] = d

    # resolution
    def _suggest(self, name):
        pool = [n for n, d in self.decls.items() if d.kind == "value"] + list(self.groups)
        pool += [f"{g.name}.{m.label}" for g in self.groups.values() for m in g.members if m.label]
        hit = difflib.get_close_matches(name, pool, n=1, cutoff=0.6)
        return f"; did you mean &{hit[0]}?" if hit else ""

    def resolve(self, name, seen=()):
        if name in seen:
            raise RefErr("E08", "circular reference " + " -> ".join((*seen, name)))
        d = self.decls.get(name)
        if d is not None:
            if d.kind == "state":
                raise RefErr("E04", f"&{name} is a state; write it as {name}")
            out = []
            for val, st in d.parts:
                if val[0] == "str":
                    out.append((val[1], st))
                else:
                    out.extend((t, s or st) for t, s in self.resolve(val[1], seen + (name,)))
            return out
        if name in self.groups or name in self.spine_map:
            raise RefErr("E11", f"&{name} is animated; this slot needs a static value")
        if "." in name:
            gname, label = name.rsplit(".", 1)
            g = self.groups.get(gname)
            if g is not None:
                for m in g.members:
                    if m.label == label:
                        return [(t, m.state or s) for t, s in self.member_spans(m, seen + (name,))]
                raise RefErr("E04", f"{gname} has no label `{label}`" + self._suggest(name))
        raise RefErr("E04", f"&{name} is not declared" + self._suggest(name))

    def member_spans(self, m, seen=()):
        if m.value[0] == "str":
            return [(m.value[1], None)]
        return self.resolve(m.value[1], seen)

    def state_style(self, st):
        if not st:
            return None, ()
        if st.startswith("#"):
            return st, ()
        d = self.decls.get("!" + st)
        if d is not None:
            return d.color, tuple(d.attrs)
        b = BUILTIN_STATES.get(st)
        return (b["color"], tuple(b["attrs"])) if b else (None, ())

    def _check_state(self, st, ln):
        if st and not st.startswith("#") and ("!" + st) not in self.decls and st not in BUILTIN_STATES:
            self._d("W06", ln, f"unknown state !{st}; it renders plain")

    def _validate(self):
        smap = self.spine_map
        for d in self.decls.values():
            if d.kind != "value":
                continue
            owners = set()
            for val, st in d.parts:
                self._check_state(st, d.line)
                if val[0] != "ref":
                    continue
                r = val[1]
                if r in self.groups:
                    owners.add(self.groups[r].spine)
                elif r in smap:
                    owners.add(r)
                else:
                    try:
                        self.resolve(r, (d.name,))
                    except RefErr as e:
                        self._d(e.code, d.line, str(e))
            if len(owners) > 1:
                self._d("E12", d.line, f"view {d.name} reaches into {len(owners)} spines; a view watches one")
            elif owners:
                self.views[d.name] = owners.pop()
        for s in self.spines:
            self._check_state(s.state, s.line)
            for track in s.tracks:
                endless = None
                for g in track:
                    if endless is not None:
                        self._d("W03", g.line, f"unreachable: {endless.name} loops forever (L{endless.term_line}); cap it with `=> *N`")
                    if g.term == "<=" and not g.rep and endless is None:
                        endless = g
                    self._check_state(g.state, g.line)
                    prev = None
                    for m in g.members:
                        self._check_state(m.state, m.line)
                        if m.value[0] == "ref":
                            try:
                                self.member_spans(m)
                            except RefErr as e:
                                self._d(e.code, m.line, str(e))
                        key = (m.value, m.state, m.hold, m.chance, m.label)
                        if prev is not None and key == prev and m.rep is None:
                            shown = m.value[1] if m.value[0] == "str" else "&" + m.value[1]
                            self._d("W04", m.line, f'repeated frame "{shown}"; write *2 on one line if the double hold is meant')
                        prev = key

    def _align(self):
        canon = format_text(self.text).split("\n")
        for ln, (a, b) in enumerate(zip(self.lines, canon), 1):
            a2 = a.replace("\t", "    ").rstrip()
            if a2 != b:
                self._d("W01", ln, drift(a2, b) + "; `rotor fmt` fixes it")


def _mask(code):
    return re.sub(STR, lambda m: "x" * len(m.group()), code)


def bar_col(line):
    code, _ = split_comment(line)
    m = re.search(r"\[\.|[|\]]", _mask(code))
    if not m:
        return None
    return dwidth(code[:m.start()]) + (2 if m.group() == "[." else 1)


def drift(a: str, b: str) -> str:
    """names the exact way line `a` misses its canonical form `b`"""
    ca, cb = bar_col(a), bar_col(b)
    if cb and ca != cb:
        what = "`.`" if "[." in _mask(b) else "bar"
        return f"{what} at col {ca}, the law puts it at col {cb}"
    acode, acm = split_comment(a)
    bcode, bcm = split_comment(b)
    if acm and bcm and dwidth(a[:a.index(acm)]) != dwidth(b[:b.index(bcm)]):
        return f"comment at col {dwidth(a[:a.index(acm)]) + 1}, the block's comment column is {dwidth(b[:b.index(bcm)]) + 1}"
    flat = lambda s: re.sub(r"\s+", "", _mask(s))
    if flat(acode).replace(",", "") == flat(bcode).replace(",", "") and flat(acode) != flat(bcode):
        return "comma: children end with `,`, end and break lines take none"
    words = lambda s: s.replace(",", " ").split()
    if sorted(words(acode)) == sorted(words(bcode)) and words(acode) != words(bcode):
        return "modifier order: label, value, *N, ?N%, !state, @hold"
    if words(acode) == words(bcode):
        return "padding between tokens"
    return "spacing off the law"


# ── formatter: the alignment law ───────────────────────────────────────────

def canon_name(name):
    return f"{name}:=" if name.startswith("!") else f'"{name}":='


ORDER = {"label": 0, "str": 1, "ref": 1, "rep": 2, "chance": 3, "state": 4, "hold": 5, "word": 6}


def canon_tokens(toks, keep_order=False):
    toks = [t for t in toks if t[0] != "comma"]
    if not keep_order:
        toks = sorted(toks, key=lambda t: ORDER[t[0]])
    return " ".join(v for _, v in toks)


def _classify(raw):
    line = raw.replace("\t", "    ").rstrip()
    code, cm = split_comment(line)
    s = code.strip()
    r = {"raw": line, "cm": cm, "name": None, "body": s}
    if not s:
        r["kind"] = "blank" if cm is None else "comment"
    elif s.startswith(">"):
        r["kind"] = "header"
    else:
        m = DECL_RE.match(s)
        if m:
            r["name"], r["body"] = m["q"] or m["b"], m["rest"].strip()
        b = r["body"]
        r["kind"] = ("root" if b.startswith("[.") else "bar" if b.startswith("|")
                     else "close" if b.startswith("]") else "decl" if r["name"] else "raw")
    return r


def _render(r, b):
    k, body = r["kind"], r["body"]
    nm = canon_name(r["name"]) if r["name"] else None
    if k == "root":
        mods = canon_tokens(tokenize(body[2:]))
        lead = pad(nm, b - 1) if nm else " " * (b - 1)
        return lead + "[." + (" " + mods if mods else "")
    if k == "bar":
        bm = BAR_RE.match(body)
        op, rest = bm["op"], bm["rest"]
        if op in ("|", "+|", None):
            if rest.strip():
                raise LexError(rest)
            seg = {"|": "| |", "+|": "|+|", None: "|"}[op]
        else:
            t = canon_tokens(tokenize(rest))
            seg = {"=>": "| =>" + (" " + t if t else ""), "->": "| -> " + t + ",",
                   "<-": "|<- " + t, "<=": "|<= " + t}[op]
        return (pad(nm, b) if nm else " " * b) + seg
    if k == "close":
        rest = body[1:].strip()
        if rest not in ("", "<="):
            raise LexError(rest)
        return " " * (b - 1) + "]" + rest
    if k == "decl":
        return pad(nm, b) + canon_tokens(tokenize(body), keep_order=True)
    return r["raw"]


def format_text(text: str) -> str:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    recs = [_classify(x) for x in lines]
    blocks, cur, inside = [], [], False
    for i, r in enumerate(recs):
        if r["kind"] == "root":
            inside = True
        if r["kind"] == "blank" and not inside:
            if cur:
                blocks.append(cur)
                cur = []
            continue
        cur.append(i)
        if r["kind"] == "close":
            inside = False
    if cur:
        blocks.append(cur)
    out = [r["raw"] for r in recs]
    for blk in blocks:
        named = [recs[i] for i in blk if recs[i]["name"]]
        lr = max((dwidth(canon_name(r["name"])) for r in named if r["kind"] == "root"), default=0)
        ln = max((dwidth(canon_name(r["name"])) for r in named if r["kind"] != "root"), default=0)
        b = max(ln + 1 if ln else 0, lr + 2 if lr else 0)
        if not b:
            bars = [bar_col(recs[i]["raw"]) for i in blk if recs[i]["kind"] in ("bar", "close")]
            b = (bars[0] - 1) if bars and bars[0] else 1
        bodies = {}
        for i in blk:
            if recs[i]["kind"] in ("root", "bar", "close", "decl"):
                try:
                    bodies[i] = _render(recs[i], b).rstrip()
                except LexError:
                    bodies[i] = None
        ccol = max((dwidth(bodies[i]) for i in bodies if bodies[i] is not None and recs[i]["cm"]), default=0) + 2
        for i in blk:
            r = recs[i]
            if r["kind"] == "header":
                out[i] = r["body"] + (" " + r["cm"] if r["cm"] else "")
            elif bodies.get(i) is not None:
                out[i] = pad(bodies[i], ccol) + r["cm"] if r["cm"] else bodies[i]
    return "\n".join(out)


# ── playback ───────────────────────────────────────────────────────────────

def apply_state(spans, st):
    return [(t, s or st) for t, s in spans]


def member_frame_spans(doc, spine, g, m):
    spans = doc.member_spans(m)
    return [(t, m.state) for t, _ in spans] if m.state else apply_state(spans, g.state or spine.state)


def group_hold(spine, g):
    return g.hold or spine.hold or DEFAULT_HOLD


def track_iter(doc, spine, groups, rng):
    last = None
    for g in groups:
        passes = g.rep if g.rep else (None if g.term == "<=" else 1)
        p = 0
        while passes is None or p < passes:
            emitted = False
            for idx, m in enumerate(g.members):
                if m.chance is not None and rng.random() * 100 >= m.chance:
                    continue
                spans = member_frame_spans(doc, spine, g, m)
                hold = m.hold or group_hold(spine, g)
                for _ in range(m.rep or 1):
                    last = Frame(g.name, idx, len(g.members), m.label, spans, hold)
                    emitted = True
                    yield last
            if not emitted:
                last = Frame(g.name, -1, len(g.members), None,
                             last.spans if last else [(" ", None)], group_hold(spine, g))
                yield last
            p += 1


def spine_seed(doc, spine, seed):
    return seed + doc.spines.index(spine)


def timeline(doc, spine, seed=0):
    """yields (t_ms, [Frame per track], done); `seed` is the spine's stream seed (see spine_seed)"""
    rng = Mulberry32(seed)
    n = len(spine.tracks)
    if n == 0:
        yield 0, [], True
        return

    def fresh():
        return [track_iter(doc, spine, tr, rng) for tr in spine.tracks]

    its, cur, due, live, t = fresh(), [None] * n, [0] * n, [True] * n, 0
    while True:
        for i in range(n):
            if live[i] and due[i] <= t:
                fr = next(its[i], None)
                if fr is None:
                    live[i] = False
                else:
                    cur[i], due[i] = fr, t + max(1, fr.hold)
        if not any(live):
            if spine.loop:
                its, live, due = fresh(), [True] * n, [t] * n
                continue
            yield t, list(cur), True
            return
        yield t, list(cur), False
        t = min(due[i] for i in range(n) if live[i])


def auto_view(cur):
    out = []
    for i, fr in enumerate(cur):
        if i:
            out.append((" ", None))
        out.extend(fr.spans if fr else [(" ", None)])
    return out


def render_view(doc, vname, cur):
    out = []
    for val, st in doc.decls[vname].parts:
        if val[0] == "str":
            out.append((val[1], st))
            continue
        r = val[1]
        if r in doc.groups:
            g = doc.groups[r]
            fr = cur[g.track] if g.track < len(cur) else None
            spans = fr.spans if fr else [(" ", None)]
        elif r in doc.spine_map:
            spans = auto_view(cur)
        else:
            spans = doc.resolve(r)
        out.extend(apply_state(spans, st))
    return out


def load(path: str) -> Doc:
    with open(path, encoding="utf-8") as fh:
        return Doc(fh.read(), path)


# ── runtime: Player (pure) and Spinner (threaded line) ─────────────────────

class Player:
    """frame source for one spine or view. no threads, no I/O: ask it for the picture at time t (ms)."""

    def __init__(self, doc, spine=None, seed=0, view=None):
        if isinstance(doc, str):
            doc = load(doc)
        if doc.errors():
            raise RotorError("\n".join(report_check(doc, Ink(doc, "none"), 78)))
        self.doc = doc
        if view is not None and view not in doc.views:
            raise RotorError(f"no view named {view}; views: {', '.join(doc.views) or 'none'}")
        if spine is None:
            spine = doc.views[view] if view else (doc.spines[0].name if doc.spines else None)
        if spine not in doc.spine_map:
            raise RotorError(f"no spine named {spine}; spines: {', '.join(s.name for s in doc.spines) or 'none'}")
        self.spine = doc.spine_map[spine]
        if view is None:
            view = next((v for v, o in doc.views.items() if o == self.spine.name), None)
        self.view = view
        self.seed = spine_seed(doc, self.spine, seed)
        self._reset()

    def _reset(self):
        self._gen = timeline(self.doc, self.spine, self.seed)
        self._now = next(self._gen)
        self._ahead = None if self._now[2] else next(self._gen, None)

    def advance(self, t):
        if t < self._now[0]:
            self._reset()
        while self._ahead is not None and self._ahead[0] <= t:
            self._now = self._ahead
            self._ahead = None if self._now[2] else next(self._gen, None)

    @property
    def next_change(self):
        """ms of the next frame change, or None once the spine has ended"""
        return self._ahead[0] if self._ahead else None

    @property
    def frames(self):
        return self._now[1]

    def spans(self, t):
        self.advance(t)
        return render_view(self.doc, self.view, self._now[1]) if self.view else auto_view(self._now[1])

    def text(self, t):
        return plain(self.spans(t))


def detect_depth(stream, flag=None):
    if flag:
        return flag
    if os.environ.get("NO_COLOR") or not getattr(stream, "isatty", lambda: False)():
        return "none"
    if (os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit") or os.environ.get("WT_SESSION")
            or os.environ.get("TERM_PROGRAM") in ("vscode", "iTerm.app", "WezTerm", "ghostty")):
        return "16m"
    return "256"


class Spinner:
    """animates one terminal line from a ROTOR spine while the body of a `with` block runs.

        with Spinner("header.rotor", view="HEADER.VIEW", message="sniffing cluster") as sp:
            work()
            sp.say("parsing")

    settles on `done` (green) or `failed` (red) when the block exits; an exception or sp.fail() fails it.
    """

    def __init__(self, source, *, spine=None, view=None, message="", seed=0, stream=None, color=None):
        self.player = Player(source, spine=spine, seed=seed, view=view)
        self.stream = stream or sys.stderr
        self.live = getattr(self.stream, "isatty", lambda: False)()
        self.ink = Ink(self.player.doc, detect_depth(self.stream, color))
        self.message, self.note = message, ""
        self._failed = False
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self._t0 = 0.0

    def say(self, message):
        self.message = message

    def fail(self, note=""):
        self._failed, self.note = True, note

    def done(self, note=""):
        self.note = note

    def _elapsed(self):
        return int((time.monotonic() - self._t0) * 1000)

    def _line(self, spans, tail):
        cols = shutil.get_terminal_size((80, 24)).columns
        room = max(12, cols - 8 - dwidth(plain(spans)) - dwidth(re.sub(r"\x1b\[[0-9;]*m", "", tail)))
        return "  " + self.ink.spans(spans) + "   " + clip_text(self.message, room) + "   " + tail

    def _write(self, s):
        with self._lock:
            self.stream.write(s)
            self.stream.flush()

    def _run(self):
        while not self._stop.is_set():
            t = self._elapsed()
            spans = self.player.spans(t)
            self._write("\r\x1b[2K" + self._line(spans, self.ink.paint(f"{t / 1000:.1f}s", MUTE)))
            nxt = self.player.next_change
            wait = 0.1 if nxt is None else min(0.1, max(0.001, (nxt - self._elapsed()) / 1000))
            self._stop.wait(wait)

    def __enter__(self):
        self._t0 = time.monotonic()
        if self.live:
            self._write("\x1b[?25l")
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self._failed = True
        self._stop.set()
        if self._thread:
            self._thread.join()
        t = self._elapsed()
        state, word = ("hot", "failed") if self._failed else ("ok", "done")
        spans = [(x, state) for x, _ in self.player.spans(t)]
        tail = (self.ink.paint(word, ERR if self._failed else OK, ("bold",)) + "   "
                + self.ink.paint(f"{t / 1000:.1f}s", MUTE))
        note = ("    " + self.ink.paint("└ " + self.note, MUTE) + "\n") if self.note else ""
        self._write(("\r\x1b[2K" if self.live else "") + self._line(spans, tail) + "\n" + note
                    + ("\x1b[?25h" if self.live else ""))
        return False


# ── IR ─────────────────────────────────────────────────────────────────────

def compile_ir(doc):
    def sj(spans):
        return [{"text": t, "state": s} for t, s in spans]

    states = {k: dict(v) for k, v in BUILTIN_STATES.items()}
    for d in doc.decls.values():
        if d.kind == "state":
            states[d.name[1:]] = {"color": d.color, "attrs": list(d.attrs)}
    constants = {n: sj(doc.resolve(n)) for n, d in doc.decls.items() if d.kind == "value" and n not in doc.views}
    spines = []
    for s in doc.spines:
        tracks = []
        for tr in s.tracks:
            groups = []
            for g in tr:
                members = [{"op": m.op, "label": m.label, "spans": sj(member_frame_spans(doc, s, g, m)),
                            "repeat": m.rep or 1, "chance": m.chance, "hold_ms": m.hold or group_hold(s, g)}
                           for m in g.members]
                groups.append({"name": g.name, "passes": g.rep if g.rep else (None if g.term == "<=" else 1),
                               "loops": g.term == "<=", "hold_ms": group_hold(s, g), "members": members})
            tracks.append(groups)
        spines.append({"name": s.name, "loop": s.loop, "tracks": tracks})
    views = {}
    for v, owner in doc.views.items():
        parts = []
        for val, st in doc.decls[v].parts:
            if val[0] == "str":
                parts.append({"text": val[1], "state": st})
            elif val[1] in doc.groups:
                parts.append({"track": doc.groups[val[1]].track, "group": val[1], "state": st})
            elif val[1] in doc.spine_map:
                parts.append({"spine": val[1], "state": st})
            else:
                parts.extend({"text": t, "state": x or st} for t, x in doc.resolve(val[1]))
        views[v] = {"spine": owner, "parts": parts}
    return {"rotor": VERSION, "title": doc.title, "subtitle": doc.subtitle, "default_hold_ms": DEFAULT_HOLD,
            "rng": "mulberry32", "states": states, "constants": constants, "spines": spines, "views": views}


# ── terminal ink ───────────────────────────────────────────────────────────

def rgb256(r, g, b):
    if max(r, g, b) - min(r, g, b) < 12:
        v = (r + g + b) // 3
        if v < 8:
            return 16
        if v > 238:
            return 231
        return 232 + min(23, round((v - 8) / 247 * 24))
    q = lambda c: 0 if c < 48 else 1 if c < 115 else (c - 35) // 40
    return 16 + 36 * q(r) + 6 * q(g) + q(b)


class Ink:
    """depth: '16m' truecolor, '256' xterm palette, 'none' plain text"""

    def __init__(self, doc=None, depth="16m"):
        if depth is True:
            depth = "16m"
        if depth is False or depth is None:
            depth = "none"
        self.doc, self.depth = doc, depth

    @property
    def on(self):
        return self.depth != "none"

    def paint(self, text, color=None, attrs=()):
        if not self.on or not text:
            return text
        codes = [ATTR_CODES[a] for a in attrs if a in ATTR_CODES]
        if color:
            r, g, b = int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
            codes.append(f"38;2;{r};{g};{b}" if self.depth == "16m" else f"38;5;{rgb256(r, g, b)}")
        return f"\x1b[{';'.join(codes)}m{text}\x1b[0m" if codes else text

    def spans(self, spans):
        return "".join(self.paint(t, *(self.doc.state_style(s) if self.doc else (None, ()))) for t, s in spans)

    def rule(self, W):
        return "  " + self.paint("─" * (W - 2), RULE)


def plain(spans):
    return "".join(t for t, _ in spans)


def banner(ink, verb, right, W):
    gap = max(2, W - dwidth(f"ROTOR  {verb}") - dwidth(right))
    return ["", "  " + ink.paint("ROTOR", ACCENT, ("bold",)) + "  " + ink.paint(verb, MUTE)
            + " " * gap + ink.paint(right, MUTE), ink.rule(W)]


def tally(e, w, ink):
    es = ink.paint(f"{e} error{'s' * (e != 1)}", ERR if e else MUTE, ("bold",) if e else ())
    ws = ink.paint(f"{w} warning{'s' * (w != 1)}", WARN if w else MUTE)
    return f"{es}   {ws}"


def diag_lines(doc, ink, W, indent="  "):
    out = []
    for d in doc.diags:
        col = ERR if d.error else WARN
        out.append(f"{indent}{ink.paint(d.code, col, ('bold',))}  {ink.paint(f'L{d.line:<4}', MUTE)} {d.msg}")
        src = doc.lines[d.line - 1].strip() if 0 < d.line <= len(doc.lines) else ""
        if src:
            out.append(indent + "            " + ink.paint(clip_text(src, W - 14 - len(indent)), MUTE))
    return out


def report_check(doc, ink, W):
    out = banner(ink, "check", os.path.basename(doc.path), W)
    out += diag_lines(doc, ink, W)
    if not doc.diags:
        out.append("  " + ink.paint("clean", OK, ("bold",)) + ink.paint("   every line sits on the law", MUTE))
    out.append(ink.rule(W))
    s, g, m = doc.stats()
    out.append("  " + tally(len(doc.errors()), len(doc.warnings()), ink)
               + ink.paint(f"   .   {s} spines   {g} groups   {m} frames", MUTE))
    out.append("")
    return out


def report_check_many(docs, ink, W):
    out = banner(ink, "check", f"{len(docs)} files", W)
    name_w = max(dwidth(os.path.relpath(d.path)) for d in docs) + 3
    for d in docs:
        name = os.path.relpath(d.path)
        status = (ink.paint("clean", OK) if not d.diags else tally(len(d.errors()), len(d.warnings()), ink))
        out.append("  " + pad(ink.paint(name, TEXT, ("bold",)), name_w, name) + status)
        out += diag_lines(d, ink, W, indent="    ")
    out.append(ink.rule(W))
    e = sum(len(d.errors()) for d in docs)
    w = sum(len(d.warnings()) for d in docs)
    s = sum(d.stats()[0] for d in docs)
    out.append("  " + tally(e, w, ink) + ink.paint(f"   .   {len(docs)} files   {s} spines", MUTE))
    out.append("")
    return out


def dots(fr):
    if fr.n > 16:
        return f"{fr.idx + 1}/{fr.n}"
    return "".join("■" if i == fr.idx else "□" for i in range(fr.n))


def fmt_t(ms):
    return f"{ms // 60000:02d}:{(ms % 60000) / 1000:06.3f}"


def play_screen(doc, spines, shown, T, seed, ink, W, finished):
    """finished: None live, True all spines ended, False stopped by --for"""
    out = banner(ink, "play", os.path.basename(doc.path), W)
    if doc.title:
        out.append("  " + ink.paint(doc.title, TEXT, ("bold",)))
    if doc.subtitle:
        out.append("  " + ink.paint(doc.subtitle, MUTE))
    for si, s in enumerate(spines):
        cur = shown[si] or [None] * len(s.tracks)
        forever = all(any(g.term == "<=" and not g.rep for g in tr) for tr in s.tracks)
        tag = "loops" if s.loop else "runs forever" if forever else "plays once"
        out.append("")
        out.append("  " + ink.paint(s.name, ACCENT, ("bold",)) + " " * max(2, W - dwidth(s.name) - dwidth(tag))
                   + ink.paint(tag, MUTE))
        views = [v for v, o in doc.views.items() if o == s.name] or ([None] if len(s.tracks) > 1 else [])
        for v in views:
            spans = render_view(doc, v, cur) if v else auto_view(cur)
            label = v or "auto view"
            gap = max(2, W - 4 - dwidth(plain(spans)) - dwidth(label))
            out.append("    " + ink.spans(spans) + " " * gap + ink.paint(label, MUTE))
        for ti, fr in enumerate(cur):
            num = ink.paint(f"{ti + 1:>2}", MUTE)
            if fr is None:
                out.append(f"    {num}")
                continue
            d = dots(fr)
            out.append("    " + num + "  " + pad(ink.paint(fr.group, TEXT), 16, fr.group)
                       + pad(ink.spans(fr.spans), 6, plain(fr.spans))
                       + pad(ink.paint(d, ACCENT), 18, d)
                       + pad(ink.paint(fr.label or "", MUTE), 12, fr.label or "")
                       + ink.paint(f"{fr.hold}ms", MUTE))
    out.append("")
    out.append(ink.rule(W))
    out.append("  " + ink.paint(f"t {fmt_t(T)}", TEXT) + ink.paint(
        f"   seed {seed}   " + {None: "ctrl-c stops", True: "finished", False: "stopped"}[finished], MUTE))
    return out


def clip_screen(lines, H, ink):
    """keeps the screen inside H rows so the cursor can always climb back to its top"""
    room = H - 1
    if len(lines) <= room:
        return lines
    if room < 6:
        return lines[-room:]
    body = room - 3
    cut = len(lines) - 2 - body
    note = "  " + ink.paint(f"{cut} lines cut to fit the terminal; narrow with --spine NAME", WARN)
    return lines[:body] + [note] + lines[-2:]


def play(doc, spines, seed, ink, W, limit_s=None):
    tls = [timeline(doc, s, spine_seed(doc, s, seed)) for s in spines]
    pend = [next(t) for t in tls]
    shown, fin = [None] * len(spines), [False] * len(spines)
    start, drawn, T = time.monotonic(), 0, 0

    def draw(lines):
        lines = clip_screen(lines, shutil.get_terminal_size((80, 24)).lines, ink)
        buf = [f"\x1b[{drawn}F"] if drawn else []
        buf += ["\x1b[2K" + x + "\n" for x in lines]
        buf.append("\x1b[J")
        sys.stdout.write("".join(buf))
        sys.stdout.flush()
        return len(lines)

    sys.stdout.write("\x1b[?25l")
    try:
        while True:
            live = [i for i in range(len(tls)) if not fin[i]]
            if not live:
                break
            T = min(pend[i][0] for i in live)
            if limit_s is not None and T > limit_s * 1000:
                break
            for i in live:
                if pend[i][0] == T:
                    _, cur, done = pend[i]
                    shown[i] = cur
                    if done:
                        fin[i] = True
                    else:
                        pend[i] = next(tls[i])
            wait = start + T / 1000 - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            drawn = draw(play_screen(doc, spines, shown, T, seed, ink, W, None))
        drawn = draw(play_screen(doc, spines, shown, T, seed, ink, W, all(fin)))
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\x1b[?25h\n")
        sys.stdout.flush()


def trace_rows(doc, spine, seed, ms, rows_max, keep_all=False):
    """visible-change rows: (t, cells, done); returns rows, event count"""
    views = [v for v, o in doc.views.items() if o == spine.name]
    rows, events, prev = [], 0, None
    for t, cur, done in timeline(doc, spine, spine_seed(doc, spine, seed)):
        if t > ms or len(rows) >= rows_max:
            break
        events += 1
        cells = [render_view(doc, v, cur) for v in views]
        for fr in cur:
            if fr is None:
                cells.append([(" ", None)])
            else:
                tag = " ".join(x for x in (fr.label, fr.spans[0][1] if fr.spans else None) if x)
                cells.append(fr.spans + ([(" " + tag, MUTE)] if tag else []))
        key = tuple(tuple(c) for c in cells)
        if keep_all or key != prev or done:
            rows.append((t, cells, done))
        prev = key
        if done:
            break
    return views, rows, events


def report_trace(doc, spine, seed, ms, rows_max, ink, keep_all=False):
    views, rows, events = trace_rows(doc, spine, seed, ms, rows_max, keep_all)
    folded = events - len(rows)
    out = ["  " + ink.paint(spine.name, ACCENT, ("bold",))
           + ink.paint(f"   seed {seed}   first {ms}ms   {events} events   {len(rows)} visible changes"
                       + (f"   {folded} folded" if folded else "")
                       + ("   ends" if rows and rows[-1][2] else ""), MUTE), ""]
    heads = ["t ms"] + views + [tr[0].name if len(tr) == 1 else f"track {i + 1} ({len(tr)} groups)"
                                for i, tr in enumerate(spine.tracks)]
    widths = [max(dwidth(heads[0]), max((dwidth(str(r[0])) for r in rows), default=0)) + 3]
    for c, h in enumerate(heads[1:]):
        widths.append(max([dwidth(h)] + [dwidth(plain(r[1][c])) for r in rows]) + 3)
    out.append(("    " + "".join(pad(ink.paint(h, MUTE), widths[c], h) for c, h in enumerate(heads))).rstrip())
    out.append(("    " + "".join(pad(ink.paint("─" * dwidth(h), RULE), widths[c], "─" * dwidth(h))
                                for c, h in enumerate(heads))).rstrip())
    for t, cells, _ in rows:
        ts = str(t)
        line = "    " + " " * (dwidth(heads[0]) - dwidth(ts)) + ink.paint(ts, MUTE) + " " * (widths[0] - dwidth(heads[0]))
        line += "".join(pad(ink.spans(c), widths[k + 1], plain(c)) for k, c in enumerate(cells))
        out.append(line.rstrip())
    out.append("")
    return out


# ── cli ────────────────────────────────────────────────────────────────────

COMMANDS = ("run", "fmt", "check", "play", "trace", "compile", "spin")


def setup_console():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    if os.name == "nt":
        os.system("")


def emit(lines):
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


def expand(paths):
    out = []
    for p in paths:
        if os.path.isdir(p):
            for root, dirs, files in os.walk(p):
                dirs.sort()
                out += [os.path.join(root, f) for f in sorted(files) if f.endswith(".rotor")]
        else:
            out.append(p)
    return out


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def cmd_fmt(a, ink, W):
    files = expand(a.paths)
    if a.dry:
        for f in files:
            sys.stdout.write(format_text(read(f)))
        return 0
    rows = []
    for f in files:
        text = read(f)
        new = format_text(text)
        old = text.replace("\r\n", "\n").replace("\r", "\n")
        moved = sum(1 for x, y in zip(old.split("\n"), new.split("\n")) if x != y)
        if new != text:
            with open(f, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(new)
        rows.append((os.path.relpath(f), moved))
    out = banner(ink, "fmt", rows[0][0] if len(rows) == 1 else f"{len(rows)} files", W)
    name_w = max(dwidth(r[0]) for r in rows) + 3
    for name, moved in rows:
        status = (ink.paint(f"{moved} line{'s' * (moved != 1)}", ACCENT, ("bold",)) + ink.paint(" moved onto the law", MUTE)
                  if moved else ink.paint("already on the law", OK))
        out.append("  " + (pad(ink.paint(name, TEXT), name_w, name) if len(rows) > 1 else "") + status)
    out.append("")
    emit(out)
    return 0


def cmd_spin(a, doc, depth):
    cmd = list(a.command)
    if not cmd:
        print("  ROTOR  spin needs a command:  rotor spin FILE -- COMMAND ...")
        return 2
    use_shell = shutil.which(cmd[0]) is None and os.name == "nt"
    run_cmd = subprocess.list2cmdline(cmd) if use_shell else cmd
    try:
        sp = Spinner(doc, spine=a.spine, view=a.view, message=a.say or " ".join(cmd), seed=a.seed, color=depth)
    except RotorError as e:
        print(str(e))
        return 2
    proc, out = None, b""
    with sp:
        try:
            proc = subprocess.run(run_cmd, shell=use_shell, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            out = proc.stdout
            if proc.returncode:
                sp.fail(f"exit {proc.returncode}")
        except (FileNotFoundError, PermissionError) as e:
            sp.fail(f"cannot run {cmd[0]}: {e.strerror}")
    code = proc.returncode if proc else 127
    text = out.decode("utf-8", errors="replace").rstrip("\n")
    if text and (a.show or code):
        lines = text.split("\n")
        shown = lines if a.show else lines[-20:]
        ink = sp.ink
        if len(shown) < len(lines):
            sys.stderr.write("    " + ink.paint(f"... {len(lines) - len(shown)} earlier lines", MUTE) + "\n")
        sys.stderr.write("\n".join("    " + ink.paint("│", RULE) + " " + x for x in shown) + "\n")
    return code


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    setup_console()
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] == "--version":
        print(VERSION)
        return 0
    if argv[0] not in COMMANDS:
        argv.insert(0, "run")
    ap = argparse.ArgumentParser(prog="rotor")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in COMMANDS:
        p = sub.add_parser(c)
        if c in ("fmt", "check"):
            p.add_argument("paths", nargs="+")
        else:
            p.add_argument("file")
        p.add_argument("--no-color", action="store_true")
        p.add_argument("--color", choices=("16m", "256", "none"))
        if c in ("run", "play", "trace", "spin"):
            p.add_argument("--seed", type=int, default=0)
            p.add_argument("--spine")
        if c in ("run", "play"):
            p.add_argument("--for", dest="limit", type=float)
        if c in ("run", "trace"):
            p.add_argument("--ms", type=int, default=3000)
            p.add_argument("--rows", type=int, default=48)
            p.add_argument("--all", action="store_true")
        if c == "fmt":
            p.add_argument("--dry", action="store_true")
        if c == "compile":
            p.add_argument("-o", dest="out")
        if c == "spin":
            p.add_argument("--view")
            p.add_argument("--say")
            p.add_argument("--show", action="store_true")
    command = []
    if "--" in argv:
        command, argv = argv[argv.index("--") + 1:], argv[:argv.index("--")]
    a = ap.parse_args(argv)
    a.command = command
    depth = detect_depth(sys.stdout, "none" if a.no_color else a.color)
    W = max(58, min(78, shutil.get_terminal_size((80, 24)).columns - 2))
    try:
        if a.cmd == "fmt":
            return cmd_fmt(a, Ink(None, depth), W)
        if a.cmd == "check":
            docs = [Doc(read(f), f) for f in expand(a.paths)]
            if not docs:
                print("  ROTOR  no .rotor files found")
                return 2
            ink = Ink(docs[0] if len(docs) == 1 else None, depth)
            emit(report_check(docs[0], ink, W) if len(docs) == 1 else report_check_many(docs, ink, W))
            return 1 if any(d.errors() for d in docs) else 0
        doc = Doc(read(a.file), a.file)
    except OSError as e:
        print(f"  ROTOR  cannot read {e.filename}: {e.strerror}")
        return 2
    ink = Ink(doc, depth)
    if doc.errors():
        emit(report_check(doc, ink, W))
        return 1
    if a.cmd == "spin":
        return cmd_spin(a, doc, detect_depth(sys.stderr, "none" if a.no_color else a.color))
    if a.cmd == "compile":
        js = json.dumps(compile_ir(doc), ensure_ascii=False, indent=2)
        if a.out:
            with open(a.out, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(js + "\n")
            emit(banner(ink, "compile", os.path.basename(a.file), W) + ["  " + ink.paint("IR written to ", MUTE) + a.out, ""])
        else:
            print(js)
        return 0
    spines = [s for s in doc.spines if not a.spine or s.name == a.spine]
    if not spines:
        print(f"  ROTOR  no spine named {a.spine}")
        return 2
    mode = a.cmd
    if mode == "run":
        if doc.warnings():
            emit(report_check(doc, ink, W))
        mode = "play" if sys.stdout.isatty() else "trace"
    if mode == "play":
        play(doc, spines, a.seed, ink, W, a.limit)
        return 0
    out = banner(ink, "trace", os.path.basename(a.file), W)
    if doc.title:
        out.append("  " + ink.paint(doc.title, TEXT, ("bold",)))
    out.append("")
    for s in spines:
        out += report_trace(doc, s, a.seed, a.ms, a.rows, ink, a.all)
    emit(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
