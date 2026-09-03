from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DOCUMENTATION_ROOTS = (
    "Analysis",
    "Generation",
    "Merging",
    "Simulation",
    "UChicagoAF",
    "Workflow",
    "docs",
)


def test_markdown_uses_github_math_delimiters() -> None:
    legacy_delimiters = (r"\(", r"\)", r"\[", r"\]")
    markdown_files = [REPOSITORY_ROOT / "README.md"]
    for directory in DOCUMENTATION_ROOTS:
        markdown_files.extend((REPOSITORY_ROOT / directory).rglob("*.md"))
    offenders = [
        f"{path.relative_to(REPOSITORY_ROOT).as_posix()}: {delimiter}"
        for path in markdown_files
        for delimiter in legacy_delimiters
        if delimiter in path.read_text(encoding="utf-8")
    ]
    assert not offenders, f"legacy Markdown math delimiters found: {offenders}"
