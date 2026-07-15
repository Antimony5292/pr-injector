from pathlib import Path
from types import SimpleNamespace
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from construction_toolkit.bug_transplant.scripts.verify_swebench_pro import run_pytest  # noqa: E402


def test_qutebrowser_uses_its_own_webengine_flags(tmp_path, monkeypatch) -> None:
    (tmp_path / "qutebrowser").mkdir()
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs["env"]
        return SimpleNamespace(returncode=0, stdout=b"1 passed\n", stderr=b"")

    monkeypatch.setattr("construction_toolkit.bug_transplant.scripts.verify_swebench_pro.subprocess.run", fake_run)
    monkeypatch.setattr("construction_toolkit.bug_transplant.scripts.verify_swebench_pro.shutil.which", lambda _: None)

    result = run_pytest(str(tmp_path), ["tests/test_target.py"], python="python")

    assert result["returncode"] == 0
    assert "--no-qt-log" in captured["cmd"]
    assert "QTWEBENGINE_CHROMIUM_FLAGS" not in captured["env"]
    assert captured["env"]["QTWEBENGINE_DISABLE_SANDBOX"] == "1"
