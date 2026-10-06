"""ROTOR v1.1 test suite. Runs under pytest or plain: python tests/test_rotor.py"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import rotor  # noqa: E402

EX = os.path.join(ROOT, "examples")

# the original note's spine, spaces only, exactly as hand-aligned (duplicate ◲ kept on purpose)
ORIGINAL = '''"ROLLING_CUBES":=       [.
"ROLLING_SQUARED_NODE":= | =>
                         | -> "◰",
                         | -> "◲",
                         | -> "◲",
                         |<- "◳"
                         | |
"ROLLING_SHADOW_NODE":=  | =>
                         | -> "❏",
                         | -> "❐",
                         | -> "❑",
                         |<- "⧠"
                        ]'''


def codes(text):
    return [d.code for d in rotor.Doc(text).diags]


def frames(text, spine=0, seed=0, limit=200):
    doc = rotor.Doc(text)
    assert not doc.errors(), doc.errors()
    out = []
    for t, cur, done in rotor.timeline(doc, doc.spines[spine], seed):
        out.append((t, [rotor.plain(f.spans) if f else None for f in cur], done))
        if done or len(out) >= limit:
            break
    return out


def example(name):
    with open(os.path.join(EX, name), encoding="utf-8") as fh:
        return fh.read()


# ── the law ───────────────────────────────────────────────────────────────

def test_law_reproduces_the_original_hand_layout():
    assert rotor.format_text(ORIGINAL) == ORIGINAL


def test_original_flags_only_the_repeated_frame():
    doc = rotor.Doc(ORIGINAL)
    assert [(d.code, d.line) for d in doc.diags] == [("W04", 5)]


def test_fmt_is_idempotent_on_examples():
    for name in os.listdir(EX):
        text = example(name)
        once = rotor.format_text(text)
        assert rotor.format_text(once) == once, name
        assert once == text.rstrip("\n") + ("\n" if text.endswith("\n") else ""), f"{name} not canonical"


def test_misaligned_source_warns_then_fmt_fixes():
    bad = '"A":= [.\n"G":= | =>\n  | -> "x",\n |<- "y"\n]'
    assert set(codes(bad)) == {"W01"}
    fixed = rotor.format_text(bad)
    assert codes(fixed) == []
    assert fixed.split("\n")[2] == '       | -> "x",'


def test_root_never_touches_walrus():
    out = rotor.format_text('"LONGEST_ROOT":= [.\n"G":= | =>\n|<- "x"\n]')
    root, group = out.split("\n")[:2]
    assert root == '"LONGEST_ROOT":= [.'
    assert root.index(".") == group.index("|")


def test_tabs_warn_and_count_as_four_spaces():
    assert "W02" in codes('"A":= [.\n"G":= | =>\n\t | <- "x"\n]')


def test_trailing_comments_share_a_column():
    out = rotor.format_text('"A":= [.\n"G":= | => // g\n| -> "xx", // one\n|<- "y" // two\n]').split("\n")
    cols = {line.index("//") for line in out if "//" in line}
    assert len(cols) == 1


def test_canonical_modifier_order():
    out = rotor.format_text('"A":= [.\n"G":= | =>\n| -> @5ms !lit ?50% *2 "x" lab:,\n|<- "y"\n]')
    assert out.split("\n")[2].strip() == '| -> lab: "x" *2 ?50% !lit @5ms,'


# ── diagnostics ───────────────────────────────────────────────────────────

def test_unterminated_group():
    assert "E01" in codes('"A":= [.\n"G":= | =>\n | -> "x",\n]')


def test_orphan_member():
    assert "E02" in codes('"A":= [.\n | -> "x",\n]')


def test_unclosed_spine_and_stray_close():
    assert "E03" in codes('"A":= [.\n"G":= | =>\n |<- "x"')
    assert "E03" in codes("]")


def test_undefined_reference():
    assert "E04" in codes('"A":= [.\n"G":= | =>\n |<- &NOPE\n]')


def test_duplicate_names():
    assert "E05" in codes('"X":= "a"\n"X":= "b"')


def test_unnamed_parent():
    assert "E06" in codes('"A":= [.\n | =>\n |<- "x"\n]')


def test_unreadable_token():
    assert "E07" in codes('"A":= [.\n"G":= | =>\n |<- "x" %%%\n]')


def test_circular_reference():
    assert "E08" in codes('"X":= &Y\n"Y":= &X')


def test_member_after_end():
    assert "E09" in codes('"A":= [.\n"G":= | =>\n |<- "x"\n | -> "y",\n]')


def test_missing_value():
    assert "E10" in codes('"A":= [.\n"G":= | =>\n |<- *2\n]')


def test_animated_value_in_static_slot():
    assert "E11" in codes('"A":= [.\n"G":= | =>\n |<- &G\n]')


def test_view_across_two_spines():
    src = '"A":= [.\n"G":= | =>\n |<- "x"\n]\n"B":= [.\n"H":= | =>\n |<- "y"\n]\n"V":= &G &H'
    assert "E12" in codes(src)


def test_unreachable_after_endless_loop():
    assert "W03" in codes('"A":= [.\n"G":= | =>\n |<= "x"\n | |\n"H":= | =>\n |<- "y"\n]')
    assert "W03" not in codes('"A":= [.\n"G":= | => *2\n |<= "x"\n | |\n"H":= | =>\n |<- "y"\n]')


def test_unknown_state():
    assert "W06" in codes('"A":= [.\n"G":= | =>\n |<- "x" !nope\n]')
    assert "W06" not in codes('!nope:= #112233\n"A":= [.\n"G":= | =>\n |<- "x" !nope\n]')


def test_examples_are_clean():
    for name in os.listdir(EX):
        doc = rotor.Doc(example(name), name)
        assert doc.diags == [], (name, doc.diags)


# ── playback ──────────────────────────────────────────────────────────────

def test_stop_flag_ends_and_holds():
    out = frames('"A":= [. @10ms\n"G":= | =>\n | -> "a",\n |<- "b"\n]')
    assert [(t, f) for t, f, _ in out] == [(0, ["a"]), (10, ["b"]), (20, ["b"])]
    assert out[-1][2] is True


def test_capped_loop_then_next_group():
    out = frames('"A":= [. @10ms\n"G":= | => *2\n | -> "a",\n |<= "b"\n | |\n"H":= | =>\n |<- "c"\n]')
    assert [f[0] for _, f, _ in out[:-1]] == ["a", "b", "a", "b", "c"]


def test_endless_loop_never_ends():
    out = frames('"A":= [. @10ms\n"G":= | =>\n |<= "a"\n]', limit=50)
    assert len(out) == 50 and not any(d for _, _, d in out)


def test_spine_loop_restarts():
    out = frames('"A":= [. @10ms\n"G":= | =>\n | -> "a",\n |<- "b"\n]<=', limit=6)
    assert [f[0] for _, f, _ in out] == ["a", "b", "a", "b", "a", "b"]


def test_parallel_tracks_keep_their_own_clock():
    out = frames('"A":= [.\n"G":= | => @30ms\n |<= "g"\n|+|\n"H":= | => @20ms\n | -> "h1",\n |<= "h2"\n]', limit=6)
    assert [t for t, _, _ in out] == [0, 20, 30, 40, 60, 80]
    assert out[1][1] == ["g", "h2"]


def test_repeat_and_hold_cascade():
    out = frames('"A":= [. @7ms\n"G":= | => @9ms\n | -> "a" *3,\n |<- "b" @2ms\n]')
    assert [t for t, _, _ in out] == [0, 9, 18, 27, 29]


def test_chance_extremes():
    never = frames('"A":= [. @10ms\n"G":= | =>\n | -> "x" ?0%,\n |<- "y"\n]')
    always = frames('"A":= [. @10ms\n"G":= | =>\n | -> "x" ?100%,\n |<- "y"\n]')
    assert "x" not in [f[0] for _, f, _ in never]
    assert [f[0] for _, f, _ in always][0] == "x"


def test_seed_is_deterministic():
    src = example("header.rotor")
    assert frames(src, seed=4, limit=80) == frames(src, seed=4, limit=80)
    assert frames(src, seed=4, limit=80) != frames(src, seed=5, limit=80)


def test_state_cascade_member_beats_group_beats_spine():
    doc = rotor.Doc('"A":= [. !dim\n"G":= | => !ok\n | -> "a" !lit,\n |<- "b"\n]\n"C":= [. !dim\n"H":= | =>\n |<- "c"\n]')
    it = rotor.track_iter(doc, doc.spines[0], doc.spines[0].tracks[0], __import__("random").Random(0))
    assert [f.spans[0][1] for f in it] == ["lit", "ok"]
    it2 = rotor.track_iter(doc, doc.spines[1], doc.spines[1].tracks[0], __import__("random").Random(0))
    assert next(it2).spans[0][1] == "dim"


def test_views_compose_live_tracks_and_labels():
    doc = rotor.Doc(example("header.rotor"))
    assert doc.views == {"HEADER.VIEW": "HEADER", "STATUS.VIEW": "CUBE_LIFE"}
    _, cur, _ = next(rotor.timeline(doc, doc.spine_map["CUBE_LIFE"]))
    assert rotor.plain(rotor.render_view(doc, "STATUS.VIEW", cur)) == "⬚ sniffing ⯀"


# ── IR + CLI ──────────────────────────────────────────────────────────────

def test_ir_shape():
    ir = rotor.compile_ir(rotor.Doc(example("header.rotor")))
    json.dumps(ir, ensure_ascii=False)
    header = next(s for s in ir["spines"] if s["name"] == "HEADER")
    assert len(header["tracks"]) == 3
    node = header["tracks"][1][0]
    assert node["passes"] is None and node["loops"] is True
    assert node["members"][0] == {"op": "->", "label": None, "spans": [{"text": "⧮", "state": None}],
                                  "repeat": 4, "chance": None, "hold_ms": 110}
    assert ir["views"]["HEADER.VIEW"]["parts"][0] == {"track": 0, "group": "VBE_L", "state": None}
    assert ir["states"]["probe"] == {"color": "#7fd6ff", "attrs": ["bold"]}


def cli(*args):
    r = subprocess.run([sys.executable, os.path.join(ROOT, "rotor.py"), *args],
                       capture_output=True, text=True, encoding="utf-8")
    return r.returncode, r.stdout


def test_cli_exit_codes_and_default_command(tmp_path=None):
    import tempfile
    d = tempfile.mkdtemp()
    bad = os.path.join(d, "bad.rotor")
    with open(bad, "w", encoding="utf-8") as fh:
        fh.write('"A":= [.\n"G":= | =>\n | -> "x",\n]')
    assert cli("check", bad)[0] == 1
    code, out = cli(os.path.join(EX, "header.rotor"), "--ms", "300")
    assert code == 0 and "HEADER.VIEW" in out and "\x1b" not in out
    assert cli("check", os.path.join(EX, "rolling.rotor"))[0] == 0



# ── v1.1: portability, escapes, recovery, hints ───────────────────────────

def test_mulberry32_reference_vectors():
    # the same three draws come out of ports/ts/rotor.ts; these lock the generator for every future port
    expect = {0: [0.26642920868471265, 0.0003297457005828619, 0.2232720274478197],
              42: [0.6011037519201636, 0.44829055899754167, 0.8524657934904099],
              4294967295: [0.8964226141106337, 0.189478256739676, 0.7156526781618595]}
    for seed, vals in expect.items():
        r = rotor.Mulberry32(seed)
        assert [r.random() for _ in vals] == vals


def test_escapes_and_bars_inside_glyphs():
    doc = rotor.Doc(example("classic.rotor"))
    assert doc.diags == []
    spin = doc.groups["SPIN"]
    assert [m.value[1] for m in spin.members] == ["|", "/", "-", "\\"]
    assert [m.value[1] for m in doc.groups["BLINK"].members] == ['"·"', '"•"']
    assert rotor.bar_col('          | -> "|",') == 11


def test_comment_after_escaped_quote():
    code, cm = rotor.split_comment('| -> "\\"//" , // real')
    assert cm == "// real" and code.endswith('"\\"//" ,')


def test_one_bad_token_reports_once():
    for src in ('"A":= [. %%\n"G":= | =>\n |<- "x"\n]',
                '"A":= [.\n"G":= | => ~~\n | -> "x",\n |<- "y"\n]',
                '"A":= [.\n"G":= | =>\n | -> "x" ~~,\n |<- "y"\n]'):
        assert [d.code for d in rotor.Doc(src).errors()] == ["E07"], src


def test_did_you_mean():
    doc = rotor.Doc('"VSN.FULL":= "x"\n"A":= [.\n"G":= | =>\n |<- &VSN.FUL\n]')
    assert "did you mean &VSN.FULL?" in doc.errors()[0].msg
    doc = rotor.Doc('"A":= [.\n"G":= | =>\n |<- s1: "x"\n]\n"V":= &G.s2 &G')
    assert any("did you mean &G.s1?" in d.msg for d in doc.errors())


def test_drift_names_the_exact_problem():
    base = '"A":= [.\n"G":=  | =>\n{}\n       |<- "y"\n      ]'
    assert codes(base.format('       | -> "x",')) == []

    def w01(line):
        return [d.msg for d in rotor.Doc(base.format(line)).diags if d.code == "W01"][0]
    assert w01('  | -> "x",').startswith("bar at col 3, the law puts it at col 8")
    assert w01('       | -> "x"').startswith("comma")
    assert w01('       | -> "x" @5ms !lit,').startswith("modifier order")
    assert w01('       | ->  "x",').startswith("padding")
    commented = '"A":= [.\n"G":=  | =>\n       | -> "x",   // one\n       |<- "yy"  // two\n      ]'
    assert any(d.msg.startswith("comment at col") for d in rotor.Doc(commented).diags)


def test_trace_folds_invisible_events():
    doc = rotor.Doc(example("header.rotor"))
    sp = doc.spine_map["HEADER"]
    _, folded, events = rotor.trace_rows(doc, sp, 0, 1500, 999)
    _, everything, _ = rotor.trace_rows(doc, sp, 0, 1500, 999, keep_all=True)
    assert len(everything) == events > len(folded)
    keys = [tuple(tuple(c) for c in r[1]) for r in folded]
    assert all(a != b for a, b in zip(keys, keys[1:]))


# ── v1.1: runtime ─────────────────────────────────────────────────────────

def test_player_is_pure_and_rewinds():
    p = rotor.Player(rotor.Doc(example("rolling.rotor")))
    assert [p.text(t) for t in (0, 119, 120, 480, 960)] == ["◰", "◰", "◱", "❏", "◰"]
    assert p.next_change == 1080
    assert p.text(130) == "◱"                       # rewind replays from zero
    view = rotor.Player(rotor.Doc(example("header.rotor")), view="HEADER.VIEW")
    assert view.spine.name == "HEADER" and view.text(0) == "⟥[⧮]⟤"


def test_player_rejects_bad_input():
    for kwargs in ({"view": "NOPE"}, {"spine": "NOPE"}):
        try:
            rotor.Player(rotor.Doc(example("header.rotor")), **kwargs)
        except rotor.RotorError as e:
            assert "NOPE" in str(e)
        else:
            raise AssertionError(kwargs)
    try:
        rotor.Player(rotor.Doc('"A":= [.\n | -> "x",\n]'))
    except rotor.RotorError as e:
        assert "E02" in str(e)
    else:
        raise AssertionError("broken file accepted")


def test_spinner_settles_done_and_failed():
    import io
    buf = io.StringIO()
    with rotor.Spinner(rotor.Doc(example("rolling.rotor")), message="sniffing", stream=buf) as sp:
        sp.say("parsing")
    assert "parsing" in buf.getvalue() and "done" in buf.getvalue() and "\x1b" not in buf.getvalue()
    buf = io.StringIO()
    try:
        with rotor.Spinner(rotor.Doc(example("rolling.rotor")), message="x", stream=buf):
            raise ValueError("boom")
    except ValueError:
        pass
    else:
        raise AssertionError("exception swallowed")
    assert "failed" in buf.getvalue()
    buf = io.StringIO()
    with rotor.Spinner(rotor.Doc(example("rolling.rotor")), message="x", stream=buf) as sp:
        sp.fail("exit 3")
    assert "failed" in buf.getvalue() and "exit 3" in buf.getvalue()


def test_clip_screen_keeps_footer():
    ink = rotor.Ink(None, "none")
    lines = [f"l{i}" for i in range(40)]
    out = rotor.clip_screen(lines, 12, ink)
    assert len(out) == 11 and out[-2:] == ["l38", "l39"] and "cut" in out[-3]
    assert rotor.clip_screen(lines[:5], 12, ink) == lines[:5]


def test_color_depths():
    assert rotor.rgb256(0, 0, 0) == 16 and rotor.rgb256(255, 255, 255) == 231
    assert rotor.rgb256(255, 0, 0) == 196
    ink = rotor.Ink(None, "256")
    assert ink.paint("x", "#ff0000") == "\x1b[38;5;196mx\x1b[0m"
    assert rotor.Ink(None, "16m").paint("x", "#ff0000") == "\x1b[38;2;255;0;0mx\x1b[0m"
    assert rotor.Ink(None, "none").paint("x", "#ff0000") == "x"


# ── v1.1: IR, TS port conformance, CLI ────────────────────────────────────

def test_ir_carries_what_ports_need():
    ir = rotor.compile_ir(rotor.Doc(example("header.rotor")))
    assert ir["rng"] == "mulberry32"
    assert all("hold_ms" in g for s in ir["spines"] for tr in s["tracks"] for g in tr)


def _py_events(doc, seed, cap):
    out = []
    for s in doc.spines:
        ev = []
        for t, cur, done in rotor.timeline(doc, s, rotor.spine_seed(doc, s, seed)):
            ev.append({"t": t, "done": done, "frames": [f and {"group": f.group, "idx": f.idx, "label": f.label,
                                                               "spans": [[a, b] for a, b in f.spans]} for f in cur]})
            if done or len(ev) >= cap:
                break
        out.append({"spine": s.name, "events": ev})
    return out


def test_ts_port_matches_python_timeline():
    import shutil as sh
    import tempfile
    if not sh.which("node"):
        print("        (node not found; TS conformance skipped)")
        return
    d = tempfile.mkdtemp()
    for name in sorted(os.listdir(EX)):
        doc = rotor.Doc(example(name), name)
        irp = os.path.join(d, name + ".json")
        with open(irp, "w", encoding="utf-8") as fh:
            json.dump(rotor.compile_ir(doc), fh, ensure_ascii=False)
        for seed in (0, 7, 1234):
            r = subprocess.run(["node", os.path.join(ROOT, "ports", "ts", "cli.ts"), irp, "--seed", str(seed),
                                "--events", "250"], capture_output=True, text=True, encoding="utf-8")
            assert r.returncode == 0, r.stderr
            assert json.loads(r.stdout) == _py_events(doc, seed, 250), (name, seed)


def test_cli_check_many_and_folders():
    import tempfile
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "bad.rotor"), "w", encoding="utf-8") as fh:
        fh.write('"A":= [.\n"G":= | =>\n | -> "x",\n]')
    code, out = cli("check", EX, d)
    assert code == 1 and "4 files" in out and "bad.rotor" in out and "E01" in out
    assert cli("check", EX)[0] == 0


def test_cli_spin_returns_the_command_exit_code():
    py = sys.executable
    rolling = os.path.join(EX, "rolling.rotor")
    r = subprocess.run([py, os.path.join(ROOT, "rotor.py"), "spin", rolling, "--say", "ok", "--", py, "-c", "pass"],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0 and "done" in r.stderr
    r = subprocess.run([py, os.path.join(ROOT, "rotor.py"), "spin", rolling, "--", py, "-c", "print('why');exit(3)"],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 3 and "failed" in r.stderr and "why" in r.stderr
    r = subprocess.run([py, os.path.join(ROOT, "rotor.py"), "spin", rolling, "--", "no-such-command-rotor"],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 127


def test_vscode_grammar_scopes():
    nm = os.path.join(ROOT, "editors", "vscode", "node_modules")
    import shutil as sh
    if not (sh.which("node") and os.path.isdir(nm)):
        print("        (editors/vscode/node_modules missing; run `npm i` there to enable the grammar check)")
        return
    r = subprocess.run(["node", os.path.join(ROOT, "editors", "vscode", "test", "tokenize.cjs")],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stdout

if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    failed = 0
    for n, f in tests:
        try:
            f()
            print(f"  pass  {n}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {n}: {e!r}")
    print(f"\n  {len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
