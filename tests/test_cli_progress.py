"""P1.8：并行节点返回 None 时 CLI 进度打印不应崩溃。"""
from cli import _print_progress


def test_print_progress_ignores_none(capsys):
    _print_progress("archivist", None, 0)
    _print_progress("archivist", [None, {"memory_written": 0}], 0)
    output = capsys.readouterr().out
    assert "Traceback" not in output
