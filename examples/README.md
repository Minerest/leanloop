# Examples

Sample `leanfile.toml` files showing how to wire `lean-loop` to different test
runners. Copy one into your project, edit the `[runner]` command and add
your own `[[tasks]]`, then run:

```
lean-loop -c leanfile.toml
```

| File                                    | Stack                              |
| --------------------------------------- | ---------------------------------- |
| [`python-pytest/leanfile.toml`](python-pytest/leanfile.toml) | Python / pytest (deluxe traceback path) |
| [`go-test/leanfile.toml`](go-test/leanfile.toml)             | Go / `go test` (fallback path)          |
| [`js-jest/leanfile.toml`](js-jest/leanfile.toml)             | JS-TS / jest (fallback path)            |

The Python example exercises the full deluxe path — `lean-loop` parses the
pytest traceback, extracts the failing frame, and feeds the model a source
window around the failing line. Go and JS use the language-agnostic
fallback: compressed error tail + git diff + the files listed in the task.
