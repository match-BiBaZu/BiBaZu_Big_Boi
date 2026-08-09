# BiBaZu application icons

The PNG source assets were generated as one visual family and converted to
multi-resolution Windows ICO files. The shortcut installer references the ICO
files in this directory so updates remain reproducible on other workstations.

Rebuild the ICO files after changing a transparent PNG source:

```powershell
..\..\ReorientationControlGUI\.venv\Scripts\python.exe .\build_icons.py
```
