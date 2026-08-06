export const meta = {
  name: 'optjournal-entropy-audit',
  description: 'Audit optjournal for duplication, dead code, structure, and modernization opportunities',
  phases: [
    { title: 'Find', detail: 'six independent lenses over the whole repo' },
    { title: 'Rank', detail: 'dedupe, drop philosophy-violations, rank by impact' },
    { title: 'Verify', detail: 'adversarial verification per finding' },
  ],
}

const ROOT = '/Users/robrtmar/.meshclaw/workspace/optjournal'

const PREAMBLE = `You are auditing the repo at ${ROOT} (Python 3.12, uv, pytest, ruff; an options trading journal; frontend is a single page.html with no build step).

MANDATORY FIRST STEP: Read ${ROOT}/README.md end to end. It documents deliberate design decisions that are NOT entropy and must not be reported as findings:
- Layering rules: entry points (cli.py, web.py) construct dependencies; domain modules (history, stats, analysis) return dataclasses and know nothing of JSON/HTML/argparse; presentation (serialize, render) consumes them.
- analysis.py is DELIBERATELY a leaf that imports nothing internal (not even money.py). stats.py DELIBERATELY never imports blackscholes.py (modelled-number quarantine).
- camelCase attribute names mirroring IBKR at the parse boundary are deliberate.
- The Money model: never add _base/_native/_native_ccy field triples; derived rows replace flat keys with Money, leaf rows keep the raw triple plus a money key (levels above re-aggregate the leaves).
- page.html deliberately holds the entire frontend; tests/test_web.py parses its @typedef blocks as a payload contract.
- ruff already enforces E, W, F, I, B, UP, SIM and passes clean - do not report anything those rules would catch.

Report ONLY findings that reduce real entropy: genuine duplication, dead code, drift between copies, needless complexity, misplaced code. NOT stylistic preference, NOT churn, NOT renames for taste. For every finding give exact file paths with line numbers, quote the actual evidence (both sides of a duplication), and propose one concrete fix. If a lens turns up nothing real, return an empty list - an empty answer is a good answer.

Your final output must be only the structured result.

YOUR LENS: `

const LENSES = [
  {
    key: 'py-dup',
    prompt: `Duplicated logic across the Python code: src/optjournal/*.py and cron/*.py. Read every file fully. Look for: the same computation, query, or parsing implemented in two modules; parallel helpers that have drifted apart; copy-pasted blocks shared by cron/optjournal_bars.py and cron/optjournal_sync.py (logging setup, DB opening, arg parsing, error handling); repeated SQL fragments; repeated patterns across cli.py subcommands; repeated shapes in serialize.py. Distinguish deliberate parallel structure (per README) from accidental copies.`,
  },
  {
    key: 'fe-dup',
    prompt: `Frontend duplication and dead JS: src/optjournal/page.html (the whole frontend), src/optjournal/static/replay.js, tests/frontend/replay.test.mjs. Look for: duplicated JS logic or constants between page.html and replay.js; repeated DOM-building or formatting patterns inside page.html that should share one helper; dead functions nothing calls; duplicated CSS rules. IMPORTANT: tests/test_web.py parses page.html's @typedef blocks and binding table - any proposed restructuring must keep those parseable, and the README says helpers like chargeOf/moneyOf are the single entry point by design.`,
  },
  {
    key: 'idioms',
    prompt: `Modern Python 3.12 patterns BEYOND what ruff UP/SIM already enforce, only where they genuinely reduce code or failure modes: ad-hoc dicts/tuples passed around that should be dataclasses (or existing dataclasses missing frozen/slots where mutation would be a bug); if/elif chains dispatching on shape that a match statement would make clearer; stringly-typed keys shared across modules that a StrEnum or Literal would pin; os.path vs pathlib drift; repeated try/finally that contextlib would collapse; list-building loops that should be comprehensions or generators; functools.cache opportunities on pure hot functions. Read src/optjournal/*.py and cron/*.py. Do not report preference-only changes.`,
  },
  {
    key: 'structure',
    prompt: `Module and folder organization. The layout is a flat src/optjournal package (~22 modules), cron/ scripts outside the package, tests/ flat, static/ holding a single replay.js while page.html holds everything else. Assess: modules doing two unrelated jobs; functions living in module A but used only by module B (grep the callers); whether cron/*.py scripts duplicate package functionality and should be thin wrappers or console-script entry points; anything genuinely misplaced. The repo values a flat, documented layout - recommend reorganization ONLY where the current placement actively misleads or forces duplication. "Could be grouped into subpackages" is NOT a finding.`,
  },
  {
    key: 'entropy',
    prompt: `Dead code and drift across the whole repo. For each suspected dead item, VERIFY by grepping all callers including tests, page.html, and cron/ before reporting. Look for: unused functions, methods, dataclass fields, or constants; stale comments/docstrings that contradict the code beside them; the same constant or literal defined in two places; naming drift where one concept has two names in different modules; compat.py and sections.py shims - check whether py-ibkr 0.1.7 (in .venv or uv.lock) still needs each shim; unused dependencies in pyproject.toml; stale entries in README's module table vs reality; leftover files (build/, demo/ contents) that should be gitignored or removed - check .gitignore first.`,
  },
  {
    key: 'test-entropy',
    prompt: `Test-suite entropy: tests/*.py (test_web.py alone is 1781 lines) and tests/frontend/. There is no conftest.py. Look for: setup/fixture code duplicated across test files that should be shared conftest.py fixtures (hand-built statement XML, temp DB creation, payload building, server spin-up); the same helper function copy-pasted into several test files (diff them - have they drifted?); copy-pasted test functions that should be pytest.mark.parametrize; duplicated constants. Respect: tests deliberately import private helpers (that is what they test), and the payload-contract guards in test_web.py are load-bearing - propose consolidation, not weakening.`,
  },
]

const FINDINGS_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['findings'],
  properties: {
    findings: {
      type: 'array',
      maxItems: 12,
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['title', 'category', 'impact', 'files', 'evidence', 'fix'],
        properties: {
          title: { type: 'string' },
          category: { type: 'string' },
          impact: { enum: ['high', 'medium', 'low'] },
          files: { type: 'array', items: { type: 'string' } },
          evidence: { type: 'string' },
          fix: { type: 'string' },
        },
      },
    },
  },
}

const RANKED_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['findings'],
  properties: {
    findings: {
      type: 'array',
      maxItems: 16,
      items: FINDINGS_SCHEMA.properties.findings.items,
    },
  },
}

const VERDICT_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['real', 'reason'],
  properties: {
    real: { type: 'boolean' },
    reason: { type: 'string' },
    revised_fix: { type: 'string' },
  },
}

phase('Find')
const results = await parallel(
  LENSES.map((l) => () =>
    agent(PREAMBLE + l.prompt, { label: 'find:' + l.key, phase: 'Find', schema: FINDINGS_SCHEMA })
  )
)
const found = results
  .filter(Boolean)
  .flatMap((r, i) => r.findings.map((f) => ({ ...f, lens: LENSES[i] ? LENSES[i].key : 'unknown' })))
log(found.length + ' raw findings across ' + results.filter(Boolean).length + ' lenses')

if (found.length === 0) return { confirmed: [], rejected: [] }

phase('Rank')
const ranked = await agent(
  `Six independent reviewers audited the repo at ${ROOT} for entropy (duplication, dead code, drift, misplacement). Below are their raw findings as JSON. Your job:
1. MERGE findings that describe the same underlying issue (keep the best evidence from each).
2. DROP any finding that contradicts a documented design decision in ${ROOT}/README.md (read it), or that ruff (E,W,F,I,B,UP,SIM - passing clean) would already enforce, or that is stylistic churn.
3. Return at most 16, ranked by impact: how much entropy the fix removes vs how risky the change is. Prefer fixes that delete code.
Keep each finding's exact file paths and line numbers; sharpen the fix descriptions into concrete, implementable steps.

RAW FINDINGS:
` + JSON.stringify(found, null, 1),
  { label: 'rank', schema: RANKED_SCHEMA }
)
log(ranked.findings.length + ' findings after dedup/rank')

phase('Verify')
const verified = await parallel(
  ranked.findings.map((f, i) => () =>
    agent(
      `You are an adversarial reviewer. A cleanup has been proposed for the repo at ${ROOT}. Your default position is REJECT - confirm only if you would personally defend this change in a code review.

PROPOSED FINDING:
` + JSON.stringify(f, null, 1) + `

Do this:
1. Read the cited files at the cited lines. If the evidence misquotes or mischaracterises the code, reject.
2. Read ${ROOT}/README.md. If the fix contradicts a documented design decision (layering rules, the Money model, the analysis.py/blackscholes.py quarantines, the deliberate flat layout, the page.html payload contract), reject.
3. If the "duplication" is deliberate parallel structure whose sharing would couple layers the README keeps apart, reject.
4. If the fix is churn - roughly the same line count, no failure mode removed, just different taste - reject.
5. If claimed-dead code is actually called somewhere (grep src/, cron/, tests/, page.html), reject.
6. If it survives all of that, confirm, and write revised_fix as a precise implementation plan (which functions/lines move where, what gets deleted, which tests prove it).

Return real=true only for findings you verified against the actual code.`,
      { label: 'verify:' + i + ':' + f.title.slice(0, 30), phase: 'Verify', schema: VERDICT_SCHEMA }
    ).then((v) => (v ? { ...f, verdict: v } : null))
  )
)

const judged = verified.filter(Boolean)
const confirmed = judged.filter((x) => x.verdict.real)
const rejected = judged
  .filter((x) => !x.verdict.real)
  .map((x) => ({ title: x.title, lens: x.lens, reason: x.verdict.reason }))
log(confirmed.length + ' confirmed, ' + rejected.length + ' rejected')
return { confirmed, rejected }