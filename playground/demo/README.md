# lean-loop demo

A small demo showing lean-loop driving a model across two Python source
files. No deps beyond `pytest`.

## What's here

- `stringutils.py` — `truncate` works. `slugify` raises `NotImplementedError`.
  `count_words` is off-by-one on empty strings.
- `mathutils.py` — `fizzbuzz` raises `NotImplementedError`. `is_prime`
  returns True for 0 and 1 (wrong).
- `test_stringutils.py`, `test_mathutils.py` — pytest cases for every
  function above. Several fail initially; all pass once the three tasks run.
- `leanfile.toml` — three tasks: implement slugify, fix count_words, and a
  combined implement-plus-fix in mathutils.

## Run

1. Start your LLM server (e.g. `llama-server -m <model>.gguf --port 8080`).
2. From this directory:

   ```
   leanloop -c leanfile.toml
   ```

Watch lean-loop hand each task to your wrapped CLI, run pytest, and (if
needed) enter the fix loop until the suite is green.

To run just one task:

```
leanloop -c leanfile.toml --task implement-slugify
```

## Reset between runs

The demo edits the source files in place. Back them up first if you want a
clean re-run:

```
cp stringutils.py stringutils.py.bak
cp mathutils.py mathutils.py.bak
# ...run demo, inspect, etc...
mv stringutils.py.bak stringutils.py
mv mathutils.py.bak mathutils.py
```

## Note: playground is gitignored

`playground/` is excluded from the repo (`.gitignore`), so this demo lives
locally only. If you want to share or commit it, either move it under
`examples/` or add an exception in `.gitignore`.
