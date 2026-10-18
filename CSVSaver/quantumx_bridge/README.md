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

Die sieben Dateien unter `src/QuantumXMonitor` wurden unveraendert aus
[match-BiBaZu/BiBaZu_mini_test_stand](https://github.com/match-BiBaZu/BiBaZu_mini_test_stand/tree/main/quantumx_bridge/src/QuantumXMonitor)
uebernommen. Die genaue Revision je Datei steht in `source_manifest.json`.

Wie im Mini-Pruefstand: MX440B `192.168.10.20`, TAL221 auf Kanal 3/4,
loopback-only NDJSON-Server auf Port 5500, Rohwerte und gleitende Mittelwerte
ueber 20 Messwerte. Der Modus `--server-only` blendet das Monitorfenster aus.
Details zur Bedienung stehen in `../../docs/COLIBRI_CALIBRATION.md`.
