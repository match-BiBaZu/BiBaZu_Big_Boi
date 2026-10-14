# Pressure Control API v1

Die API startet beim normalen Start von `CSVSaver/PressureControlGUI.py` und endet
mit dem Fenster. Sie bindet ausschließlich `127.0.0.1`, standardmäßig Port 8765.
`BIBAZU_PRESSURE_API_PORT` ändert den Port; `BIBAZU_API_DISABLED=1` deaktiviert den
HTTP-Server. Bei belegtem Port bleibt die GUI verwendbar und zeigt einen Fehler.
Abhängigkeiten über die aktualisierte `requirements.txt` installieren.

OpenAPI: `http://127.0.0.1:8765/openapi.json`, interaktive Ansicht: `/docs`.
Der gemeinsame Präfix sämtlicher Funktionsendpunkte lautet `/api/v1`.

## Externe Steuerung

1. `GET /health`, `GET /capabilities` und `GET /context` lesen.
2. `POST /sessions` mit `{"client":"Mein Programm"}` anfordern. Die GUI muss
   verbunden sein und eine Roadmap-Transition muss ausgewählt sein. Kalibrierung
   und UR-Winkelverstellung dürfen nicht laufen.
3. Die Antwort enthält `session_id` und `snapshot_operation_id`. Die genannte
   Operation über `GET /operations/{id}` bis `succeeded` abfragen. Anschließend
   liefert `GET /state` die frisch aus der SPS gelesenen Ausgangsparameter.
4. Bei weiteren schreibenden Aufrufen `X-Session-Id: <session_id>` mitsenden.
   Alle fünf Sekunden `POST /sessions/heartbeat`; Lebensdauer 15 Sekunden.
5. Zum Abschluss `DELETE /sessions/current` mit demselben Header aufrufen.

Während der Sitzung sind Parameter, Profile, Transition und Kalibrierung in der
GUI gesperrt. Das Förderband wird weiter manuell bedient. Die API bietet keinen
Bandstart. Nach Sitzungsende bleiben übernommene Einstellungen bestehen.

## Parameter schreiben

`PUT /parameters` erwartet vier vollständige Array-Einträge und eine globale
`conveyor_speed_mm_per_sec`. Jeder Array-Eintrag enthält genau:

| Feld | Bedeutung / Grenzen |
|---|---|
| `index` | Einmalig jeweils 1, 2, 3 und 4 |
| `enabled` | Array aktiv, boolesch |
| `nozzles_enabled` | Genau sechs boolesche Werte, Düsen 1–6 |
| `pressure_mbar` | Ganze Zahl, 0–6000 mbar |
| `pulse_duration_ms` | Ganze Zahl, 1–500 ms |
| `delay_ms` | Ganze Zahl, 0–1000 ms |
| `offset_mm` | Endliche Zahl, 0–5000 mm |
| `conveyor_velocity_delay` | Offsetberechnung mit Bandsollgeschwindigkeit |

Die Bandgeschwindigkeit liegt zwischen 0 und 200 mm/s. Ein aktiviertes Array mit
`conveyor_velocity_delay=true` benötigt eine Geschwindigkeit über 0 mm/s.
Unbekannte Felder, ungültige Typen und ungültige Werte werden abgelehnt.

HTTP 202 bestätigt nur die Annahme. `GET /operations/{operation_id}` liefert
`pending`, `succeeded` oder `failed`. Vor dem Schreiben prüft der ADS-Worker das
ausgeschaltete Band, beide Zielgeschwindigkeiten und die Kalibrierungszustände.
Anschließend schreibt er die Werte und prüft das Rücklesen. Es gibt weder eine
automatische Wiederholung noch eine SPS-Transaktion: Bei einem Teilfehler können
einige Werte bereits übernommen sein. Die Operation meldet den Fehler und liefert,
soweit lesbar, den tatsächlichen Snapshot. Vor einem nächsten Versuch prüfen.

`GET /state` enthält den letzten bestätigten Einstellungs-Snapshot und die separat
gepollte Telemetrie. Während einer laufenden Operation ist der vorherige Snapshot
noch gültig; den Abschluss immer über die Operationskennung abwarten.

`POST /profile/save-dialog` prüft ebenfalls das ausgeschaltete Band und liest die
Werte zurück. Die Operation wechselt während des bestehenden GUI-Dialogs zu
`awaiting_user`, danach zu `succeeded`, `cancelled` oder `failed`. Das Format des
gespeicherten Profils bleibt Version 13.

Konflikte und veraltete Sitzungen: HTTP 409; ungültige Eingaben: 422; offline: 503;
ausbleibende Qt-Antwort: 504. Unbekannte Operationskennungen liefern 404.
Browseroriginierte Schreibaufrufe und fremde Hostnamen werden abgewiesen.

Die kleine API-Laufzeit `CSVSaver/local_api_runtime.py` wird auch im Kamera-Projekt
als `src/bibazu_vision/local_api_runtime.py` geführt. Änderungen daran in beiden
Repositories synchron halten; beide Anwendungen bleiben unabhängig installierbar.
