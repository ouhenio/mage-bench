"""The search budgets must land on the process that READS them.

ComputerPlayer6 lives in Mage.Server.Plugins and reads both budgets with
Integer.getInteger, so they are read in the SERVER JVM. They used to be built into
the GUI client's arguments, where nothing reads them, and the sequential runner
carried them nowhere at all.

Nothing errors when a property lands on the wrong process. The only symptom is a
knob that changes nothing -- which is how this survived: a think-time cap of 1s
left the measured maximum at 3.08s against a 3.10s baseline, and every run used the
engine's defaults while its metadata recorded whatever the caller asked for.

SO THESE TESTS ASSERT ON THE LAUNCH, NOT ON THE CONFIG. A test that checked
"MAGEBENCH_AI_NODES was parsed" would have passed throughout.
"""

from __future__ import annotations

from magebench.orchestration.game_processes import ai_budget_props


class TestTheBudgetsReachTheServerProcess:
    def test_nodes_and_time_become_per_skill_properties(self):
        props = ai_budget_props("1:1000,8:5000", "1:1,8:24")

        assert "-Dxmage.ai.nodes.1=1000" in props
        assert "-Dxmage.ai.nodes.8=5000" in props
        assert "-Dxmage.ai.time.1=1" in props
        assert "-Dxmage.ai.time.8=24" in props

    def test_absent_budgets_add_nothing(self):
        # Absent must leave the engine on its defaults rather than pinning them,
        # so that the default scaling stays the default.
        assert ai_budget_props(None, None) == []

    def test_whitespace_and_malformed_pairs_are_ignored_not_guessed(self):
        props = ai_budget_props(" 1 : 1000 ,garbage, 8:5000", None)

        assert props == ["-Dxmage.ai.nodes.1=1000", "-Dxmage.ai.nodes.8=5000"]

    def test_the_sequential_runner_puts_them_on_the_SERVER_command_line(self, monkeypatch, tmp_path):
        """The launch argv is the assertion. This is the check that was missing."""
        from magebench.orchestration import sequential_batch

        monkeypatch.setenv("MAGEBENCH_AI_NODES", "1:1000,8:5000")
        monkeypatch.setenv("MAGEBENCH_AI_TIME", "1:2")
        monkeypatch.setattr(
            sequential_batch, "compute_module_classpath", lambda root, module: "/cp"
        )
        captured: dict = {}

        class _Pm:
            def start_jvm_process(self, **kwargs):
                captured.update(kwargs)
                return object()

        from magebench.orchestration.config import Config

        sequential_batch._start_server(
            _Pm(), tmp_path, Config(), tmp_path / "sc.xml", tmp_path / "s.log", 17171, None
        )

        argv = captured["args"]
        assert "-Dxmage.ai.nodes.1=1000" in argv, f"not on the server argv: {argv}"
        assert "-Dxmage.ai.nodes.8=5000" in argv
        assert "-Dxmage.ai.time.1=2" in argv

    def test_the_per_game_path_puts_them_in_the_SERVER_launch_env(self, monkeypatch, tmp_path):
        """The older path reaches the JVM through MAVEN_OPTS, so assert on that."""
        from magebench.orchestration import game_processes
        from magebench.orchestration.config import Config

        monkeypatch.setenv("MAGEBENCH_AI_NODES", "8:5000")
        monkeypatch.delenv("MAGEBENCH_AI_TIME", raising=False)
        captured: dict = {}

        class _Pm:
            def start_jvm_process(self, **kwargs):
                captured.update(kwargs)
                return object()

        game_processes.start_server(
            _Pm(), tmp_path, Config(), tmp_path / "sc.xml", tmp_path / "s.log"
        )

        opts = captured["env"]["MAVEN_OPTS"]
        assert "-Dxmage.ai.nodes.8=5000" in opts, f"not in the server MAVEN_OPTS: {opts}"

    def test_they_are_NOT_on_the_gui_client(self, monkeypatch, tmp_path):
        """Where they used to be, and where nothing reads them."""
        from magebench.orchestration import game_processes
        from magebench.orchestration.config import Config

        monkeypatch.setenv("MAGEBENCH_AI_NODES", "8:5000")
        captured: dict = {}

        class _Pm:
            def start_jvm_process(self, **kwargs):
                captured.update(kwargs)
                return object()

        cfg = Config()
        cfg.cpu_players = []
        game_processes.start_gui_client(
            _Pm(), tmp_path, cfg, tmp_path / "c.log", game_dir=tmp_path
        )

        assert "-Dxmage.ai.nodes" not in captured["env"]["MAVEN_OPTS"]
