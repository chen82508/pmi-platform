"""Unit tests for `pmi_notify.traceback_filter.filter_traceback`.

To get a REAL traceback with a frame that reports an arbitrary filename
(e.g. as if raised from `.venv/lib/.../site-packages/httpx/_client.py`)
without installing any third-party package, we `compile()` a tiny source
string against a synthetic filename and `exec()` it — the resulting
function's frame genuinely reports that filename via `co_filename`, so
`traceback.extract_tb` sees it exactly as it would for a real dependency.
"""

from __future__ import annotations

from pmi_notify.traceback_filter import filter_traceback


def _compile_raiser(filename: str, *, message: str = "boom"):
    src = f"def _raiser():\n    raise ValueError({message!r})\n"
    namespace: dict[str, object] = {}
    exec(compile(src, filename, "exec"), namespace)  # noqa: S102 - test-only synthetic frame
    return namespace["_raiser"]


def _call_through_native_code(raiser) -> ValueError:
    def _native_caller():
        raiser()

    try:
        _native_caller()
    except ValueError as exc:
        return exc
    raise AssertionError("raiser did not raise")


def test_excludes_site_packages_frame_keeps_native_frame() -> None:
    raiser = _compile_raiser("/app/.venv/lib/python3.12/site-packages/httpx/_client.py")
    exc = _call_through_native_code(raiser)

    rendered = filter_traceback(exc)

    assert "site-packages" not in rendered
    assert "_native_caller" in rendered
    assert "test_traceback_filter.py" in rendered
    assert "ValueError: boom" in rendered


def test_excludes_dot_venv_marker() -> None:
    raiser = _compile_raiser("/repo/.venv/some_pkg/mod.py")
    exc = _call_through_native_code(raiser)

    rendered = filter_traceback(exc)

    assert "/repo/.venv/" not in rendered


def test_excludes_poetry_and_pipenv_markers() -> None:
    poetry_raiser = _compile_raiser("/home/u/.cache/pypoetry/virtualenvs/x/lib/mod.py")
    pipenv_raiser = _compile_raiser("/home/u/.local/share/pipenv/virtualenvs/x/mod.py")

    poetry_exc = _call_through_native_code(poetry_raiser)
    pipenv_exc = _call_through_native_code(pipenv_raiser)

    assert "pypoetry" not in filter_traceback(poetry_exc)
    assert "pipenv" not in filter_traceback(pipenv_exc)


def test_all_frames_excluded_falls_back_to_explicit_note() -> None:
    raiser = _compile_raiser("/app/.venv/lib/site-packages/thirdparty.py")

    def _native_caller():
        raiser()

    try:
        _native_caller()
    except ValueError as exc:
        # Force-exclude the test file itself too, simulating "raised entirely
        # inside a dependency with zero native frames on the stack".
        rendered = filter_traceback(
            exc, exclude_markers=(".venv/", "test_traceback_filter.py")
        )

    assert "no project-native frames" in rendered
    assert "ValueError: boom" in rendered  # the one fact that must survive


def test_include_root_requires_frame_under_given_root() -> None:
    raiser = _compile_raiser("/some/other/project/mod.py")
    exc = _call_through_native_code(raiser)

    # include_root scopes to a directory NEITHER the raiser frame NOR this
    # test file's own frames are under, so everything gets excluded.
    rendered = filter_traceback(exc, include_root="/completely/unrelated/root")

    assert "no project-native frames" in rendered


def test_include_root_keeps_matching_frame_even_without_exclude_markers() -> None:
    raiser = _compile_raiser("/some/other/project/mod.py")
    exc = _call_through_native_code(raiser)

    # include_root matches the raiser frame's path exactly, so it survives
    # even though it also matches none of the exclude markers.
    rendered = filter_traceback(exc, include_root="/some/other/project")

    assert "mod.py" in rendered
    assert "ValueError: boom" in rendered


def test_max_frames_truncates_and_keeps_deepest_frames() -> None:
    def level_3():
        raise ValueError("deep boom")

    def level_2():
        level_3()

    def level_1():
        level_2()

    try:
        level_1()
    except ValueError as exc:
        rendered = filter_traceback(exc, max_frames=1)

    assert "earlier native frame(s) omitted" in rendered
    assert "level_3" in rendered  # deepest frame (closest to the error) survives
    assert "level_1" not in rendered  # shallowest frame got truncated away


def test_no_traceback_object_still_returns_exception_line() -> None:
    exc = ValueError("never raised")
    rendered = filter_traceback(exc)

    assert "no project-native frames" in rendered
    assert "ValueError: never raised" in rendered
