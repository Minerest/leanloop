"""Self-tests for leanloop.py — the pure-function bits.

We don't test anything that needs a live wrapper binary or LLM server; those
are exercised end-to-end by the playground in playground/BANDEEZY/.
"""
from pathlib import Path

import pytest

import leanloop


# -- _deep_merge ---------------------------------------------------------------

class TestDeepMerge:
    def test_nested_tables_merge_recursively(self):
        base = {"a": {"x": 1, "y": 2}, "b": 10}
        overlay = {"a": {"y": 99, "z": 3}}
        out = leanloop._deep_merge(base, overlay)
        assert out == {"a": {"x": 1, "y": 99, "z": 3}, "b": 10}

    def test_scalar_in_overlay_replaces_base(self):
        out = leanloop._deep_merge({"a": 1}, {"a": 2})
        assert out == {"a": 2}

    def test_list_in_overlay_replaces_not_concatenates(self):
        # Critical: if lists concatenated, users couldn't override list values.
        out = leanloop._deep_merge({"args": ["a", "b"]}, {"args": ["c"]})
        assert out == {"args": ["c"]}

    def test_overlay_can_add_new_keys(self):
        out = leanloop._deep_merge({"a": 1}, {"b": 2})
        assert out == {"a": 1, "b": 2}

    def test_does_not_mutate_inputs(self):
        base = {"a": {"x": 1}}
        overlay = {"a": {"y": 2}}
        leanloop._deep_merge(base, overlay)
        assert base == {"a": {"x": 1}}
        assert overlay == {"a": {"y": 2}}

    def test_overlay_table_over_base_scalar_replaces(self):
        # If types disagree (scalar vs dict), overlay wins without recursing.
        out = leanloop._deep_merge({"a": 1}, {"a": {"x": 2}})
        assert out == {"a": {"x": 2}}


# -- _as_prefix_list -----------------------------------------------------------

class TestAsPrefixList:
    def test_none_returns_empty(self):
        assert leanloop._as_prefix_list(None) == []

    def test_empty_string_returns_empty(self):
        assert leanloop._as_prefix_list("") == []

    def test_string_wrapped_into_list(self):
        assert leanloop._as_prefix_list("src/") == ["src/"]

    def test_list_passed_through(self):
        assert leanloop._as_prefix_list(["a/", "b/"]) == ["a/", "b/"]


# -- compress_traceback --------------------------------------------------------

class TestCompressTraceback:
    def test_keeps_last_n_lines(self):
        text = "\n".join(f"line {i}" for i in range(1, 101))
        out = leanloop.compress_traceback(text, {"defaults": {"error_tail_lines": 5}})
        assert out.splitlines() == ["line 96", "line 97", "line 98", "line 99", "line 100"]

    def test_default_tail_is_40(self):
        text = "\n".join(f"line {i}" for i in range(1, 101))
        out = leanloop.compress_traceback(text)
        assert len(out.splitlines()) == 40

    def test_strips_python_venv_noise(self):
        text = (
            "real error 1\n"
            'File "/proj/venv/lib/python3.11/site-packages/pluggy/_callers.py", line 1, in foo\n'
            "real error 2"
        )
        out = leanloop.compress_traceback(text)
        assert "site-packages" not in out
        assert "real error 1" in out
        assert "real error 2" in out

    def test_strips_node_modules_noise(self):
        text = "real error\nat noisy (/proj/node_modules/jest/foo.js:1:1)\nfinal"
        out = leanloop.compress_traceback(text)
        assert "node_modules" not in out
        assert "real error" in out
        assert "final" in out


# -- parse_project_frames ------------------------------------------------------

class TestParseProjectFrames:
    def test_extracts_python_frames(self, tmp_path):
        # Create real files in tmp_path so abspath/relpath produce stable output.
        (tmp_path / "foo.py").write_text("x = 1\n")
        (tmp_path / "bar.py").write_text("y = 2\n")
        tb = (
            'Traceback (most recent call last):\n'
            f'  File "{tmp_path}/foo.py", line 5, in caller\n'
            "    something()\n"
            f'  File "{tmp_path}/bar.py", line 12, in inner\n'
            "    boom()\n"
            "RuntimeError: boom"
        )
        frames = leanloop.parse_project_frames(tb, str(tmp_path))
        assert len(frames) == 2
        assert frames[0]["relative"] == "foo.py"
        assert frames[0]["line"] == 5
        assert frames[0]["function"] == "caller"
        assert frames[1]["relative"] == "bar.py"
        assert frames[1]["line"] == 12

    def test_skips_venv_frames(self, tmp_path):
        # venv-pathed files outside project shouldn't appear.
        (tmp_path / "real.py").write_text("\n")
        tb = (
            f'  File "{tmp_path}/real.py", line 1, in foo\n'
            f'  File "{tmp_path}/venv/site-packages/x.py", line 99, in bar\n'
        )
        frames = leanloop.parse_project_frames(tb, str(tmp_path))
        assert len(frames) == 1
        assert frames[0]["relative"] == "real.py"

    def test_skips_frames_outside_project_root(self, tmp_path):
        (tmp_path / "real.py").write_text("\n")
        tb = (
            f'  File "{tmp_path}/real.py", line 1, in foo\n'
            f'  File "/totally/elsewhere.py", line 1, in bar\n'
        )
        frames = leanloop.parse_project_frames(tb, str(tmp_path))
        assert len(frames) == 1

    def test_empty_text_returns_empty(self):
        assert leanloop.parse_project_frames("", "/tmp") == []

    def test_non_python_text_returns_empty(self):
        # Go panic, jest output, etc. — no `File "x.py", line N` shape.
        tb = "panic: runtime error\n\tgoroutine 1 [running]:\n\tmain.foo()"
        assert leanloop.parse_project_frames(tb, "/tmp") == []


# -- pick_target_frame ---------------------------------------------------------

class TestPickTargetFrame:
    def _frame(self, rel, line=10, func="f"):
        return {"file": f"/abs/{rel}", "line": line, "function": func, "relative": rel}

    def test_returns_none_for_empty(self):
        assert leanloop.pick_target_frame([], {}) is None

    def test_prefers_source_prefix_when_set(self):
        frames = [
            self._frame("tests/test_foo.py"),
            self._frame("src/app/bar.py"),
            self._frame("scripts/run.py"),
        ]
        cfg = {"defaults": {"source_prefix": "src/"}}
        chosen = leanloop.pick_target_frame(frames, cfg)
        assert chosen["relative"] == "src/app/bar.py"

    def test_picks_deepest_source_prefix_match(self):
        frames = [
            self._frame("src/outer.py"),
            self._frame("src/middle.py"),
            self._frame("src/inner.py"),
        ]
        cfg = {"defaults": {"source_prefix": "src/"}}
        chosen = leanloop.pick_target_frame(frames, cfg)
        # Deepest = last in frames list.
        assert chosen["relative"] == "src/inner.py"

    def test_falls_back_to_non_test_frame(self):
        frames = [
            self._frame("tests/test_a.py"),
            self._frame("lib/util.py"),
        ]
        chosen = leanloop.pick_target_frame(frames, {})
        assert chosen["relative"] == "lib/util.py"

    def test_falls_back_to_first_when_all_match_test_prefix(self):
        frames = [
            self._frame("tests/test_a.py"),
            self._frame("tests/test_b.py"),
        ]
        chosen = leanloop.pick_target_frame(frames, {})
        assert chosen["relative"] == "tests/test_a.py"

    def test_source_prefix_accepts_list(self):
        frames = [
            self._frame("other/foo.py"),
            self._frame("lib/bar.py"),
        ]
        cfg = {"defaults": {"source_prefix": ["src/", "lib/"]}}
        chosen = leanloop.pick_target_frame(frames, cfg)
        assert chosen["relative"] == "lib/bar.py"


# -- _lean_env ----------------------------------------------------------------

class TestLeanEnv:
    def test_exports_set_keys(self, monkeypatch):
        monkeypatch.delenv("LEAN_BASE_URL", raising=False)
        cfg = {"lean": {
            "base_url": "http://x:1/v1",
            "api_key": "k",
            "model": "M",
            "approval_mode": "yolo",
        }}
        env = leanloop._lean_env(cfg)
        assert env["LEAN_BASE_URL"] == "http://x:1/v1"
        assert env["LEAN_API_KEY"] == "k"
        assert env["LEAN_MODEL"] == "M"
        assert env["LEAN_APPROVAL_MODE"] == "yolo"

    def test_skips_unset_keys(self, monkeypatch):
        # If a key isn't in the config, don't export it — let the wrapper's :? default fire.
        monkeypatch.delenv("LEAN_API_KEY", raising=False)
        cfg = {"lean": {"model": "M"}}
        env = leanloop._lean_env(cfg)
        assert "LEAN_API_KEY" not in env
        assert env["LEAN_MODEL"] == "M"

    def test_preserves_existing_environ(self, monkeypatch):
        monkeypatch.setenv("PATH", "/custom/path")
        env = leanloop._lean_env({"lean": {"model": "M"}})
        assert env["PATH"] == "/custom/path"


# -- preflight_lean ------------------------------------------------------------

class TestPreflightLean:
    def test_passes_for_real_executable(self, tmp_path):
        bin_path = tmp_path / "fake_lean.sh"
        bin_path.write_text("#!/bin/sh\nexit 0\n")
        bin_path.chmod(0o755)
        cfg = {"lean": {"binary": str(bin_path), "model": "M"}}
        assert leanloop.preflight_lean(cfg) is True

    def test_fails_when_binary_missing(self, capsys):
        cfg = {"lean": {"binary": "/nope/nope.sh", "model": "M"}}
        assert leanloop.preflight_lean(cfg) is False
        out = capsys.readouterr().out
        assert "not found" in out.lower()

    def test_fails_when_binary_not_executable(self, tmp_path, capsys):
        bin_path = tmp_path / "notexec.sh"
        bin_path.write_text("#!/bin/sh\n")
        bin_path.chmod(0o644)
        cfg = {"lean": {"binary": str(bin_path), "model": "M"}}
        assert leanloop.preflight_lean(cfg) is False
        assert "executable" in capsys.readouterr().out.lower()

    def test_fails_when_model_missing(self, tmp_path, capsys):
        bin_path = tmp_path / "fake_lean.sh"
        bin_path.write_text("#!/bin/sh\n")
        bin_path.chmod(0o755)
        cfg = {"lean": {"binary": str(bin_path)}}
        assert leanloop.preflight_lean(cfg) is False
        assert "model" in capsys.readouterr().out.lower()


# -- check_quality -------------------------------------------------------------

class TestCheckQuality:
    def test_rejects_empty(self):
        assert leanloop.check_quality("") is False

    def test_rejects_too_short(self):
        assert leanloop.check_quality("nope") is False

    def test_rejects_vague(self):
        # No keyword like line/error/exception/.py/function — gets rejected.
        assert leanloop.check_quality("Something is wrong somewhere in the code." * 2) is False

    def test_accepts_specific_diagnosis(self):
        good = "Test fails because parse_zip returns 9021 instead of 90210 — root cause on line 22."
        assert leanloop.check_quality(good) is True


# -- load_config (with merge) --------------------------------------------------

class TestLoadConfig:
    def test_static_and_task_merge(self, tmp_path):
        static = tmp_path / "config.toml"
        static.write_text(
            '[lean]\n'
            'binary = "./leaners/qwen.sh"\n'
            'model = "static-model"\n'
            'timeout = 600\n'
            '[defaults]\n'
            'source_window = 30\n'
        )
        task = tmp_path / "leanfile.toml"
        task.write_text(
            '[runner]\n'
            'command = "pytest"\n'
            '[defaults]\n'
            'source_prefix = "src/"\n'
            '[lean]\n'
            'timeout = 900\n'
        )
        cfg = leanloop.load_config(str(task), str(static))
        # Task overrides static.lean.timeout
        assert cfg["lean"]["timeout"] == 900
        # Static.lean.model preserved (not in task)
        assert cfg["lean"]["model"] == "static-model"
        # Defaults merged — both survive
        assert cfg["defaults"]["source_window"] == 30
        assert cfg["defaults"]["source_prefix"] == "src/"
        # lean.binary resolved relative to static config dir
        assert cfg["lean"]["binary"] == str(tmp_path / "leaners" / "qwen.sh")

    def test_task_only_works_without_static(self, tmp_path, monkeypatch):
        monkeypatch.delenv("LEANLOOP_CONFIG", raising=False)
        # Point STATIC_CONFIG_DEFAULTS at non-existent paths so nothing is picked up.
        monkeypatch.setattr(leanloop, "STATIC_CONFIG_DEFAULTS",
                            (tmp_path / "missing.toml",))
        task = tmp_path / "leanfile.toml"
        task.write_text('[runner]\ncommand = "pytest"\n')
        cfg = leanloop.load_config(str(task))
        assert cfg == {"runner": {"command": "pytest"}}

    def test_missing_task_config_exits(self, tmp_path):
        with pytest.raises(SystemExit):
            leanloop.load_config(str(tmp_path / "nonexistent.toml"))


# -- _update_lean_binary -------------------------------------------------------

class TestUpdateLeanBinary:
    def test_replaces_existing_value_preserves_comment(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            '[lean]\n'
            'binary        = "./leaners/qwen.sh"        # trailing comment\n'
            'model         = "M"\n'
            '\n'
            '[health]\n'
            'check_url = "http://x/"\n'
        )
        leanloop._update_lean_binary(cfg, "/abs/path/to/claude.sh")
        out = cfg.read_text()
        assert 'binary        = "/abs/path/to/claude.sh"        # trailing comment' in out
        assert 'model         = "M"' in out
        assert '[health]' in out
        assert 'check_url = "http://x/"' in out

    def test_inserts_key_when_section_present_but_no_binary(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            '[lean]\n'
            'model = "M"\n'
            '\n'
            '[health]\n'
            'check_url = "http://x/"\n'
        )
        leanloop._update_lean_binary(cfg, "/abs/claude.sh")
        out = cfg.read_text()
        lean_block = out.split("[health]")[0]
        assert 'binary = "/abs/claude.sh"' in lean_block
        assert 'model = "M"' in lean_block
        assert 'check_url = "http://x/"' in out

    def test_appends_section_when_missing(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            '[health]\n'
            'check_url = "http://x/"\n'
        )
        leanloop._update_lean_binary(cfg, "/abs/claude.sh")
        out = cfg.read_text()
        assert '[health]' in out
        assert '[lean]' in out
        assert 'binary = "/abs/claude.sh"' in out

    def test_does_nothing_extra_when_section_only_has_binary(self, tmp_path):
        # Sanity: the simplest possible file still works.
        cfg = tmp_path / "config.toml"
        cfg.write_text('[lean]\nbinary = "./old.sh"\n')
        leanloop._update_lean_binary(cfg, "/abs/new.sh")
        assert 'binary = "/abs/new.sh"' in cfg.read_text()

    def test_updates_only_first_binary_in_section(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            '[lean]\n'
            'binary = "./old.sh"\n'
            '\n'
            '[other]\n'
            'binary = "./unrelated.sh"\n'
        )
        leanloop._update_lean_binary(cfg, "/abs/new.sh")
        out = cfg.read_text()
        assert 'binary = "/abs/new.sh"' in out
        assert 'binary = "./unrelated.sh"' in out
        assert './old.sh' not in out


# -- _task_cfg -----------------------------------------------------------------

class TestTaskCfg:
    def test_no_task_runner_returns_cfg_unchanged(self):
        cfg = {"runner": {"command": "pytest", "args": ["-q"]}}
        task = {"name": "x", "prompt": "..."}
        assert leanloop._task_cfg(cfg, task) is cfg

    def test_task_args_override_top_level_args(self):
        cfg = {"runner": {"command": "pytest", "args": ["-q"], "timeout": 30}}
        task = {"runner": {"args": ["tests/test_foo.py", "-x"]}}
        out = leanloop._task_cfg(cfg, task)
        # Args replaced (lists don't concatenate)…
        assert out["runner"]["args"] == ["tests/test_foo.py", "-x"]
        # …command and timeout inherited from top level.
        assert out["runner"]["command"] == "pytest"
        assert out["runner"]["timeout"] == 30

    def test_task_can_override_command(self):
        cfg = {"runner": {"command": "pytest", "args": ["-q"]}}
        task = {"runner": {"command": "./lint.sh"}}
        out = leanloop._task_cfg(cfg, task)
        assert out["runner"]["command"] == "./lint.sh"
        # args still inherited
        assert out["runner"]["args"] == ["-q"]

    def test_does_not_mutate_input_cfg(self):
        cfg = {"runner": {"command": "pytest", "args": ["-q"]}}
        task = {"runner": {"args": ["other"]}}
        leanloop._task_cfg(cfg, task)
        assert cfg["runner"]["args"] == ["-q"]
