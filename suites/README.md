# suites/

The versioned Suite registries (ADR-0008). Each suite is an immutable, git-tagged directory:
a `manifest.json` (index + ordered Task list + suite-level pins) plus one directory per Task
(prompt fragment, score, verifier spec, param schema). Frozen via `ckbbench.suite.freeze`.

**v1 is complete** in `ckb-v1/`: 5 scored Tasks (01-05) plus 2 labelled `PLACEHOLDER` scaffolds
(06-07, `scored=false`). Storage shape (Task directories) is deliberately different from delivery
shape (one Composed prompt) - see ADR-0008.