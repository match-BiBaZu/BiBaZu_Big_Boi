# Windows-Verknüpfungen

Ein Doppelklick auf `Verknuepfungen-installieren.cmd` erzeugt Verknüpfungen für:

- BiBaZu Reorientation Control
- BiBaZu Pressure Control
- BiBaZu Conveyor Setup
- BiBaZu Automated Image Capture

Alle Einträge erscheinen auf dem Desktop und im Startmenü im Ordner **BiBaZu**.
Über die Windows-Suche genügt anschließend die Eingabe `BiBaZu`.

Jede Anwendung besitzt ein eigenes BiBaZu-Symbol. Die transparenten PNG-Quellen
und Windows-ICOs liegen unter `icons/`; jede ICO-Datei enthält Auflösungen von
16 × 16 bis 256 × 256 Pixeln.

Die Verknüpfungen starten `pythonw.exe`; deshalb erscheint kein Terminalfenster.
Sie zeigen direkt auf diese Arbeitskopie und verwenden nach einem Git-Update beim
nächsten Start automatisch den aktuellen Python-Code. Wird der Workspace verschoben,
muss der Installer erneut ausgeführt werden.

Die benötigten `.venv`-Umgebungen müssen vorhanden sein. Der Installer prüft dies
und bricht mit einer verständlichen Meldung ab, statt eine defekte Verknüpfung
anzulegen.

Nur Desktop oder nur Startmenü:

```powershell
.\Install-BiBaZuShortcuts.ps1 -DesktopOnly
.\Install-BiBaZuShortcuts.ps1 -StartMenuOnly
```

Zum Entfernen:

```powershell
.\Uninstall-BiBaZuShortcuts.ps1
```
