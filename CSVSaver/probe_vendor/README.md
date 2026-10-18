# Herkunft der übernommenen Module

Quelle: [match-BiBaZu/BiBaZu_mini_test_stand](https://github.com/match-BiBaZu/BiBaZu_mini_test_stand), abgerufen am 18.10.2026.

| Lokale Datei | Original | Git-Blob-SHA |
| --- | --- | --- |
| `colibri.py` | `ColibriProtocolError`, `ColibriController` aus `test_run_gui.py` | `86617f6ceb5ec83bff02c54b7e2be64ba13e0711` |
| `force_sources.py` | `force_sources.py` | `6f9aee79e1066dd381808cb51f54ec7602935bbd` |
| `platform_calibration.py` | `platform_calibration.py` | `18fd715d618e7a265d60d67a84fb5bc8dcf7ec0f` |

Die beiden Kraftmodule wurden unverändert übernommen. Der Colibri-Treiber wurde
aus der Tkinter-Datei herausgelöst und mit den benötigten Konstanten versehen;
Timeouts verwenden monotone Zeit und das serielle Schreibtimeout beträgt 300 ms.
Telegrammaufbau, Escaping, Prüfsumme, Referenzparameter und Statusinterpretation
entsprechen dem Original. Geräte-I/O und Ablaufsteuerung befinden sich außerhalb
dieses Verzeichnisses in `probe_hardware.py`.
