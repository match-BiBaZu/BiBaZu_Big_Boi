# QuantumX-Bridge fuer die Colibri-Kalibrierung

Die Python-GUI liest die Kraefte ueber einen lokalen TCP-Server. Dessen Programm
`QuantumXMonitor.exe` ist ein eigener Prozess. Ein USB-Treiber oder die
Python-Abhaengigkeiten allein ersetzen die Bridge nicht.

## Vom bisherigen Mess-PC uebernehmen

Den **vollstaendigen** kompilierten Ordner des Mini-Pruefstands kopieren:

```text
BiBaZu_mini_test_stand/quantumx_bridge/src/QuantumXMonitor/bin/x86/Release/net48/
```

Ziel auf diesem PC:

```text
BiBaZu_Big_Boi/CSVSaver/quantumx_bridge/runtime/
    QuantumXMonitor.exe
    Hbm.Api.Common.dll
    Hbm.Api.QuantumX.dll
    Newtonsoft.Json.dll
    ... weitere mitgelieferte DLLs, Konfiguration und Unterordner ...
```

Die GUI findet diesen Pfad automatisch. Alternativ kann der Ordner an einem
anderen Ort liegen; dann ueber **Bridge waehlen ...** die EXE auswaehlen. Eine
schon auf `127.0.0.1:5500` laufende Bridge kann weiterhin benutzt werden.

## Auf diesem PC bauen

Voraussetzungen: HBM Common API 6.3, .NET SDK und .NET Framework 4.8 Developer Pack.
Die HBM Common API gibt es auf der
[offiziellen HBK-Downloadseite](https://www.hbkworld.com/en/products/support-resources/support-hbm/downloads/downloads-software/support-downloads-quantumx-somatxr).
Proprietaere HBK-Bibliotheken und erzeugte Binaerdateien sind nicht im Repository.

```powershell
.\Build-QuantumXBridge.ps1
# Bei abweichendem Installationsort:
.\Build-QuantumXBridge.ps1 -HbmApiRoot 'D:\HBM Common API\API'
```

Das Skript prueft die Voraussetzungen und baut nur das Programm. Es startet
keine Messung und aendert keine Geraete- oder Firmwareeinstellungen.

## Herkunft und Messpfad

Die Dateien unter `src/QuantumXMonitor` stammen aus dem Mini-Pruefstand
[match-BiBaZu/BiBaZu_mini_test_stand](https://github.com/match-BiBaZu/BiBaZu_mini_test_stand/tree/main/quantumx_bridge/src/QuantumXMonitor).
Die urspruengliche Revision je Datei und die lokalen
Anpassungen stehen in `source_manifest.json`.

Wie im Mini-Pruefstand: MX440B (Standardadresse `192.168.10.20`), TAL221 auf Kanal 3/4,
loopback-only NDJSON-Server auf Port 5500, Rohwerte und gleitende Mittelwerte
ueber 20 Messwerte. Der Modus `--server-only` blendet das Monitorfenster aus.
Details zur Bedienung stehen in `../../docs/COLIBRI_CALIBRATION.md`.

## Geraeteadresse einstellen

Die hier angepasste Bridge liest die Adresse aus `QuantumXMonitor.exe.config`
neben der EXE. Innerhalb von `<configuration>` kann beispielsweise stehen:

```xml
<appSettings>
  <add key="QuantumXDeviceIp" value="169.254.15.175" />
</appSettings>
```

Ohne Eintrag gilt die alte Adresse `192.168.10.20`. Die Einstellung aendert nur,
welchen QuantumX die Bridge sucht; die Netzwerkkonfiguration des Geraets und
des PCs bleibt erhalten. Am 18.10.2026 wurde der angeschlossene MX440B unter
`169.254.15.175` erkannt. Eine unveraenderte EXE vom alten PC muss neu gebaut
werden, damit sie diese Einstellung auswertet.

## Anschluss an diesem Mess-PC (18.10.2026)

Der MX440B mit MAC `00-09-E5-00-6E-E2` ist ueber den Windows-Adapter **Baumer**
(Interface 7, PC-Adresse `169.254.59.106`) erreichbar. Auch **Ethernet 3** liegt
im Netz `169.254.0.0/16`; dort ist der QuantumX nicht angeschlossen. Deshalb
wurde eine ausschliesslich fuer `169.254.15.175/32` geltende Route ueber Baumer
gesetzt und in ActiveStore sowie PersistentStore kontrolliert. Andere Ziele
verwenden weiter ihre bisherigen Routen. Nach einem Umstecken des Geraets muss
diese Zuordnung ueberprueft werden.

Die lokale EXE wurde mit dem vorhandenen Roslyn-Compiler aus TcXaeShell gebaut;
die urspruenglichen EXE-/Konfigurationsdateien liegen als Sicherung unter
`.task_artifacts/quantumx_live/original_runtime` im Workspace. Live-Messdaten und
Pruefberichte liegen unter `.task_artifacts/quantumx_live`.
