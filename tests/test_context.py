from app.context import ChangedFile, annotate_patch, build_context
from app.models import RepositoryPolicy


def test_annotate_patch_tracks_only_right_side_lines() -> None:
    patch = """@@ -8,3 +8,4 @@
 context
-removed
+added
 context2
@@ -20,1 +21,2 @@
 old
+new
"""
    rendered, lines = annotate_patch(patch, max_chars=10_000)
    assert lines == {8, 9, 10, 21, 22}
    assert "     - | -removed" in rendered


def test_context_selection_is_deterministic_and_reports_coverage() -> None:
    files = [
        ChangedFile(path="docs/readme.md", patch="@@ -1 +1 @@\n-old\n+new", additions=1, deletions=1),
        ChangedFile(path="src/auth.py", patch="@@ -1 +1 @@\n-old\n+new", additions=1, deletions=1),
        ChangedFile(path="vendor/lib.py", patch="@@ -1 +1 @@\n-old\n+new", additions=1, deletions=1),
    ]
    policy = RepositoryPolicy(max_files=1)
    context = build_context(files, policy, max_patch_chars=10_000, max_input_chars=20_000)

    assert [item.path for item in context.files] == ["src/auth.py"]
    assert context.total_files == 3
    assert context.excluded_files == 1
    assert context.coverage == "已检查 1/3 个文件"
