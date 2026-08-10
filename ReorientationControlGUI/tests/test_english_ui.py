from __future__ import annotations

import ast
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "bibazu_reorientation"
GERMAN_UI_TERMS = (
    "bauteil",
    "bitte",
    "datei auswählen",
    "einstellungen",
    "fehler",
    "förderband",
    "gültig",
    "kamera",
    "konfiguration",
    "leuchte",
    "nicht verbunden",
    "nicht ausgewählt",
    "roadmap-pose",
    "speichern",
    "stabilität",
    "übergang",
    "zielpose",
    "zyklus",
)


def test_python_ui_strings_are_english() -> None:
    violations: list[str] = []
    for source in SOURCE_ROOT.rglob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            # This is a real workspace directory name, not operator-facing text.
            if "Werkstücke_STL_grob" in node.value:
                continue
            value = node.value.casefold()
            if any(term in value for term in GERMAN_UI_TERMS):
                violations.append(
                    f"{source.relative_to(SOURCE_ROOT)}:{node.lineno}: {node.value!r}"
                )
    assert not violations, "German operator-facing strings remain:\n" + "\n".join(violations)
