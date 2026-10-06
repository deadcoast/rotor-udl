// node ports/ts/cli.ts IR.json [--seed N] [--events N]            conformance dump: JSON events per spine
// node ports/ts/cli.ts IR.json --play [--view NAME] [--for SECONDS] live one-line spinner in the terminal
import { readFileSync } from "node:fs";
import { type IR, Player, spineSeed, timeline, toAnsi } from "./rotor.ts";

const args = process.argv.slice(2);
const flag = (name: string, dflt: string | null = null) => {
  const i = args.indexOf(name);
  return i >= 0 ? args[i + 1] : dflt;
};
const ir: IR = JSON.parse(readFileSync(args[0], "utf8"));
const seed = Number(flag("--seed", "0"));

if (args.includes("--play")) {
  const player = new Player(ir, { view: flag("--view") ?? undefined, spine: flag("--spine") ?? undefined, seed });
  const limit = Number(flag("--for", "5")) * 1000;
  const t0 = Date.now();
  const tick = () => {
    const t = Date.now() - t0;
    process.stdout.write("\r\x1b[2K  " + toAnsi(ir, player.spans(t)));
    if (t < limit) setTimeout(tick, Math.max(1, Math.min(100, (player.nextChange ?? t + 100) - t)));
    else process.stdout.write("\n");
  };
  tick();
} else {
  const maxEvents = Number(flag("--events", "300"));
  const out = ir.spines.map((spine) => {
    const events = [];
    for (const ev of timeline(spine, spineSeed(ir, spine, seed))) {
      events.push({
        t: ev.t, done: ev.done,
        frames: ev.frames.map((f) => f && { group: f.group, idx: f.idx, label: f.label, spans: f.spans.map((s) => [s.text, s.state]) }),
      });
      if (ev.done || events.length >= maxEvents) break;
    }
    return { spine: spine.name, events };
  });
  process.stdout.write(JSON.stringify(out));
}
