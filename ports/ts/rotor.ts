// ROTOR v1.1 . TypeScript player for the IR emitted by `rotor compile`.
// Zero dependencies. Mirrors rotor.py's timeline exactly: same generator (mulberry32), same draw order,
// same frame times. tests/test_rotor.py runs both players over every example and requires identical output.

export type Span = { text: string; state: string | null };
export type Member = {
  op: string; label: string | null; spans: Span[];
  repeat: number; chance: number | null; hold_ms: number;
};
export type Group = { name: string; passes: number | null; loops: boolean; hold_ms: number; members: Member[] };
export type Spine = { name: string; loop: boolean; tracks: Group[][] };
export type ViewPart =
  | { text: string; state: string | null }
  | { track: number; group: string; state: string | null }
  | { spine: string; state: string | null };
export type View = { spine: string; parts: ViewPart[] };
export type State = { color: string | null; attrs: string[] };
export type IR = {
  rotor: string; title: string | null; subtitle: string | null; default_hold_ms: number; rng: string;
  states: Record<string, State>; constants: Record<string, Span[]>; spines: Spine[]; views: Record<string, View>;
};
export type Frame = { group: string; idx: number; n: number; label: string | null; spans: Span[]; hold: number };
export type Event = { t: number; frames: (Frame | null)[]; done: boolean };

const BLANK: Span[] = [{ text: " ", state: null }];

export function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = Math.imul(a ^ (a >>> 15), a | 1);
    t = (t + Math.imul(t ^ (t >>> 7), t | 61)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function* trackIter(groups: Group[], rng: () => number): Generator<Frame> {
  let last: Frame | null = null;
  for (const g of groups) {
    for (let p = 0; g.passes === null || p < g.passes; p++) {
      let emitted = false;
      for (let idx = 0; idx < g.members.length; idx++) {
        const m = g.members[idx];
        if (m.chance !== null && rng() * 100 >= m.chance) continue;
        for (let r = 0; r < m.repeat; r++) {
          last = { group: g.name, idx, n: g.members.length, label: m.label, spans: m.spans, hold: m.hold_ms };
          emitted = true;
          yield last;
        }
      }
      if (!emitted) {
        last = { group: g.name, idx: -1, n: g.members.length, label: null, spans: last ? last.spans : BLANK, hold: g.hold_ms };
        yield last;
      }
    }
  }
}

export function spineSeed(ir: IR, spine: Spine, seed: number): number {
  return seed + ir.spines.indexOf(spine);
}

/** yields one Event per frame change; `seed` is the spine's stream seed (see spineSeed) */
export function* timeline(spine: Spine, seed: number): Generator<Event> {
  const rng = mulberry32(seed);
  const n = spine.tracks.length;
  if (n === 0) {
    yield { t: 0, frames: [], done: true };
    return;
  }
  const fresh = () => spine.tracks.map((tr) => trackIter(tr, rng));
  let its = fresh();
  const cur: (Frame | null)[] = new Array(n).fill(null);
  let due: number[] = new Array(n).fill(0);
  let live: boolean[] = new Array(n).fill(true);
  let t = 0;
  while (true) {
    for (let i = 0; i < n; i++) {
      if (live[i] && due[i] <= t) {
        const r = its[i].next();
        if (r.done) live[i] = false;
        else {
          cur[i] = r.value;
          due[i] = t + Math.max(1, r.value.hold);
        }
      }
    }
    if (!live.some(Boolean)) {
      if (spine.loop) {
        its = fresh();
        live = new Array(n).fill(true);
        due = new Array(n).fill(t);
        continue;
      }
      yield { t, frames: [...cur], done: true };
      return;
    }
    yield { t, frames: [...cur], done: false };
    t = Math.min(...due.filter((_, i) => live[i]));
  }
}

export function applyState(spans: Span[], st: string | null): Span[] {
  return spans.map((s) => ({ text: s.text, state: s.state || st }));
}

export function autoView(cur: (Frame | null)[]): Span[] {
  const out: Span[] = [];
  cur.forEach((fr, i) => {
    if (i) out.push({ text: " ", state: null });
    out.push(...(fr ? fr.spans : BLANK));
  });
  return out;
}

export function renderView(view: View, cur: (Frame | null)[]): Span[] {
  const out: Span[] = [];
  for (const part of view.parts) {
    if ("text" in part) out.push({ text: part.text, state: part.state });
    else if ("track" in part) out.push(...applyState(cur[part.track]?.spans ?? BLANK, part.state));
    else out.push(...applyState(autoView(cur), part.state));
  }
  return out;
}

export const plain = (spans: Span[]) => spans.map((s) => s.text).join("");

/** pull-based frame source: ask for the picture at time t (ms). no timers, no I/O. */
export class Player {
  readonly ir: IR;
  readonly spine: Spine;
  readonly view: View | null;
  readonly seed: number;
  private gen!: Generator<Event>;
  private now!: Event;
  private ahead: Event | null = null;

  constructor(ir: IR, opts: { spine?: string; view?: string; seed?: number } = {}) {
    this.ir = ir;
    const viewName = opts.view;
    if (viewName !== undefined && !(viewName in ir.views)) throw new Error(`no view named ${viewName}`);
    const spineName = opts.spine ?? (viewName ? ir.views[viewName].spine : ir.spines[0]?.name);
    const spine = ir.spines.find((s) => s.name === spineName);
    if (!spine) throw new Error(`no spine named ${spineName}`);
    this.spine = spine;
    const vn = viewName ?? Object.keys(ir.views).find((v) => ir.views[v].spine === spine.name);
    this.view = vn ? ir.views[vn] : null;
    this.seed = spineSeed(ir, spine, opts.seed ?? 0);
    this.reset();
  }

  private reset() {
    this.gen = timeline(this.spine, this.seed);
    this.now = this.gen.next().value as Event;
    this.ahead = this.now.done ? null : this.pull();
  }

  private pull(): Event | null {
    const r = this.gen.next();
    return r.done ? null : r.value;
  }

  advance(t: number) {
    if (t < this.now.t) this.reset();
    while (this.ahead && this.ahead.t <= t) {
      this.now = this.ahead;
      this.ahead = this.now.done ? null : this.pull();
    }
  }

  /** ms of the next frame change, or null once the spine has ended */
  get nextChange(): number | null {
    return this.ahead ? this.ahead.t : null;
  }

  spans(t: number): Span[] {
    this.advance(t);
    return this.view ? renderView(this.view, this.now.frames) : autoView(this.now.frames);
  }

  text(t: number): string {
    return plain(this.spans(t));
  }
}

const ATTR: Record<string, string> = { bold: "1", faint: "2", italic: "3", underline: "4", blink: "5", fill: "7" };

/** spans to an ANSI truecolor string, for terminals and xterm.js */
export function toAnsi(ir: IR, spans: Span[]): string {
  return spans.map((s) => {
    const st = s.state ? (s.state.startsWith("#") ? { color: s.state, attrs: [] } : ir.states[s.state]) : null;
    if (!st) return s.text;
    const codes = st.attrs.map((a) => ATTR[a]).filter(Boolean);
    if (st.color) {
      const c = st.color;
      codes.push(`38;2;${parseInt(c.slice(1, 3), 16)};${parseInt(c.slice(3, 5), 16)};${parseInt(c.slice(5, 7), 16)}`);
    }
    return codes.length ? `\x1b[${codes.join(";")}m${s.text}\x1b[0m` : s.text;
  }).join("");
}

/** spans to inline-styled HTML, for Svelte/DOM headers */
export function toHtml(ir: IR, spans: Span[]): string {
  const esc = (x: string) => x.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  return spans.map((s) => {
    const st = s.state ? (s.state.startsWith("#") ? { color: s.state, attrs: [] } : ir.states[s.state]) : null;
    if (!st) return esc(s.text);
    const css: string[] = [];
    if (st.attrs.includes("fill")) css.push(`background:${st.color ?? "currentColor"}`, "color:#000");
    else if (st.color) css.push(`color:${st.color}`);
    if (st.attrs.includes("bold")) css.push("font-weight:700");
    if (st.attrs.includes("faint")) css.push("opacity:.55");
    if (st.attrs.includes("italic")) css.push("font-style:italic");
    if (st.attrs.includes("underline")) css.push("text-decoration:underline");
    return `<span style="${css.join(";")}">${esc(s.text)}</span>`;
  }).join("");
}
