// grammar check: tokenizes every examples/*.rotor with the real VS Code engine and asserts each
// ROTOR construct lands on its scope.   npm i --no-save vscode-textmate vscode-oniguruma && node test/tokenize.cjs
const fs = require("fs");
const path = require("path");
const vsctm = require("vscode-textmate");
const oniguruma = require("vscode-oniguruma");

const here = __dirname;
const grammarPath = path.join(here, "..", "syntaxes", "rotor.tmLanguage.json");
const examples = path.join(here, "..", "..", "..", "examples");
const wasm = fs.readFileSync(path.join(path.dirname(require.resolve("vscode-oniguruma")), "onig.wasm")).buffer;

const EXPECT = [
  ["HEADER", "entity.name.rotor"], ["[.", "keyword.control.root.rotor"], ["|+|", "keyword.control.break.parallel.rotor"],
  ["<=", "keyword.control.return.rotor"], ["<-", "keyword.control.end.rotor"], ["=>", "keyword.operator.parent.rotor"],
  ["->", "keyword.operator.child.rotor"], ["&VBE.LEFT", "variable.other.reference.rotor"], ["!dim", "support.type.state.rotor"],
  ["@700ms", "constant.numeric.hold.rotor"], ["?60%", "constant.numeric.chance.rotor"], ["*4", "constant.numeric.repeat.rotor"],
  ["stage_01:", "entity.name.tag.label.rotor"], ["⬚", "string.quoted.double.glyph.rotor"],
  ["// the flick", "comment.line.double-slash.rotor"], ["]<=", "keyword.control.close.rotor"], ["| |", "keyword.control.break.rotor"],
  ["!lit", "entity.name.type.state.rotor"], ["#ffb347 bold", "constant.other.color.rotor"],
  ["> A MIVIM.artist SPEC DEVELOPMENT DESIGN", "markup.heading.title.rotor"],
];

(async () => {
  await oniguruma.loadWASM(wasm);
  const registry = new vsctm.Registry({
    onigLib: Promise.resolve({ createOnigScanner: (s) => new oniguruma.OnigScanner(s), createOnigString: (s) => new oniguruma.OnigString(s) }),
    loadGrammar: async () => vsctm.parseRawGrammar(fs.readFileSync(grammarPath, "utf8"), grammarPath),
  });
  const grammar = await registry.loadGrammar("source.rotor");
  const seen = new Map();
  for (const file of fs.readdirSync(examples).filter((f) => f.endsWith(".rotor"))) {
    let stack = vsctm.INITIAL;
    for (const line of fs.readFileSync(path.join(examples, file), "utf8").split("\n")) {
      const r = grammar.tokenizeLine(line, stack);
      for (const t of r.tokens) {
        const text = line.slice(t.startIndex, t.endIndex).trim();
        if (!seen.has(text)) seen.set(text, new Set());
        t.scopes.forEach((s) => seen.get(text).add(s));
      }
      stack = r.ruleStack;
    }
  }
  let bad = 0;
  for (const [text, scope] of EXPECT) {
    const ok = seen.has(text) && seen.get(text).has(scope);
    if (!ok) bad++;
    console.log(`  ${ok ? "pass" : "FAIL"}  ${text.padEnd(42)} ${scope}${ok ? "" : "   got " + [...(seen.get(text) || [])].join(" ")}`);
  }
  console.log(`\n  ${EXPECT.length - bad}/${EXPECT.length} scopes`);
  process.exit(bad ? 1 : 0);
})();
