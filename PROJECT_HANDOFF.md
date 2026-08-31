# BiBaZu Big Boi - Projektuebergabe

Stand: 2026-08-11

Abgeglichen mit Git-Basis: `d6182a0` (`main`) plus lokaler
Pressure-Control-Roadmap-Auswahl

Aktiver Workspace auf dem aktuellen PC: `C:\Users\Administrator\Documents\Dashas_ws`

Dieses Dokument beschreibt das Ziel des Pruefstands, die aktuelle Hardware- und
Softwarearchitektur, die Anforderungen an beide GUIs, wichtige Kalibrierungen,
bekannte Grenzen und die Schritte fuer die Inbetriebnahme auf einem anderen PC.
Bei Widerspruechen zwischen diesem Dokument und dem laufenden System ist der
aktuelle Quellcode zusammen mit den online kontrollierten TwinCAT-Verknuepfungen
massgeblich.

## 1. Projektziel

Der BiBaZu-Big-Boi-Pruefstand soll Bauteile auf einem Foerderband erkennen, ihre
Geschwindigkeit bestimmen und sie mit zeitlich und pneumatisch einstellbaren
Dueseimpulsen gezielt umorientieren. Ein UR-Roboter kann fuer die genaue
Kalibrierung der Lichtschranken und fuer reproduzierbare Bauteil-/Versuchsposen
verwendet werden.

Die wesentlichen Aufgaben des Systems sind:

- Vier Duesenarrays mit jeweils sechs einzeln schaltbaren Ventilen steuern.
- Den Druck jedes Arrays ueber zwei Festo-VTEM-Einheiten vorgeben.
- Sechs Lichtschranken erfassen, ihre Logik einzeln invertieren und ihre
  Entprellung einzeln aktivieren oder deaktivieren.
- Aus jeweils zwei Lichtschranken die Bauteilgeschwindigkeit bestimmen.
- Ventile anhand von Lichtschranke, Geschwindigkeit, Duesenabstand,
  Kraftantwortzeit, manuellem Delay und Pulsdauer ausloesen.
- Das Foerderband mit einem encoderlosen NEMA17-Schrittmotor und einer Beckhoff
  EL7047 betreiben, kalibrieren und in mm verfahren.
- Lichtschrankenabstaende mit dem Foerderband oder dem UR-TCP kalibrieren.
- Die Plausibilitaet der gemessenen Geschwindigkeit gegen eine konstante
  Foerderband- oder UR-Geschwindigkeit pruefen.
- Die Zeit von einer akzeptierten Lichtschrankenflanke bis zum Kraftpeak messen
  und ueber mehrere Bauteile statistisch auswerten.
- Versuchsparameter als Motion-/Pressure-Profile speichern und laden.
- Druck-, Lichtschranken-, Kraftdelay- und UR-Plausibilitaetsdaten protokollieren.
- Bauteile mit Baumer-Kamera und einem Zwei-Klassen-YOLO-Modell als Pose 1 oder
  Pose 2 erkennen und einen gerichteten Reorientierungszyklus ueberwachen.
- Bauteil-, Modell-, Zielpose-, 3D-Modell- und Pressure-Profil-Zuordnungen als
  portable YAML-Konfiguration speichern und erneut bearbeiten.

## 2. Repository-Struktur

Die relevanten Dateien liegen unter `CSVSaver`:

| Pfad | Aufgabe |
| --- | --- |
| `CSVSaver/PressureControlGUI.py` | Haupt-GUI fuer Versuchsprofile, Duesen, Druck, Timing, Foerderband und Messdialoge |
| `CSVSaver/roadmap_transition_dialog.py` | Roadmap-JSON laden, Posekarten anzeigen und gerichteten Kalibrieruebergang auswaehlen |
| `CSVSaver/ConveyorSetupGUI.py` | Separate Inbetriebnahme-GUI fuer Foerderband und Lichtschranken |
| `CSVSaver/test_pressure_control_gui.py` | Unit-Tests fuer Berechnungen, ADS-Worker, Profile und GUI-Verhalten |
| `CSVSaver/ur_angle_control.py` | RTDE-Client fuer das explizite Anwenden des UR-Ry-Winkels |
| `CSVSaver/read_ur_tcp_pose.py` | Kleines Werkzeug zum Lesen der UR-TCP-Pose |
| `CSVSaver/UR16e/` | URP-Programm, URScript-Bausteine und Anleitung fuer die kontinuierliche Winkelsteuerung |
| `CSVSaver/vendor_ur_rtde/` | Mitgelieferte RTDE-Python-Bibliothek; kein separates RTDE-Paket erforderlich |
| `CSVSaver/pressure_profiles/` | Gespeicherte JSON-Profile |
| `CSVSaver/TwinCAT Projekt3 - Kopie/TwinCAT Projekt3/` | TwinCAT-System- und PLC-Projekt |
| `.../Untitled1/POUs/MAIN.TcPOU` | Zentrale SPS-Logik |
| `ReorientationControlGUI/` | Eigenstaendige GUI fuer kontinuierliche Kamera-/YOLO-gefuehrte Produktionslaeufe |
| `ReorientationControlGUI/pyproject.toml` | Python-3.12-Abhaengigkeiten und CLI `bibazu-reorientation` |
| `ReorientationControlGUI/src/bibazu_reorientation/` | Modulare Domain-, Hardware-, UI-, Logging- und Controller-Schichten |
| `ReorientationControlGUI/src/bibazu_reorientation/roadmap.py` | Handover-YAML und internen JSON-Export validieren und in Posen/Kanten normalisieren |
| `ReorientationControlGUI/src/bibazu_reorientation/ui/roadmap_pose_dialog.py` | Anklickbare Bildauswahl fuer die physische Zielpose |
| `ReorientationControlGUI/src/bibazu_reorientation/ui/roadmap_setup_dialog.py` | Roadmap-v2-Konfiguration, Klassenmapping, Zielbild und Profile je Kante |
| `ReorientationControlGUI/src/bibazu_reorientation/ui/transition_preview_dialog.py` | Animierte CAD-Orientierungsvorschau und Bild-Fallback je gerichteter Kante |
| `ReorientationControlGUI/tests/` | Unit- und Offscreen-GUI-Tests der neuen Anwendung |
| `WindowsLaunchers/` | Desktop-/Startmenue-Installer und vier anwendungsspezifische Icons |

Wichtige erzeugte Logs:

- `CSVSaver/pressure_log.csv`
- `CSVSaver/light_barrier_events.csv`
- `CSVSaver/force_peak_delay_log.csv`
- `CSVSaver/ur_speed_plausibility.csv`

Reorientation-Laeufe liegen standardmaessig unter
`%LOCALAPPDATA%\BiBaZuReorientationControl\runs`; das rotierende Diagnoseprotokoll
liegt im benachbarten Ordner `logs`.

Die historischen CSV-Dateien und Profile sind aktuell Teil des Repositories. Vor groesseren
Messreihen sollte entschieden werden, ob die Logs versioniert, archiviert oder
aus Git herausgenommen werden sollen.

`Automated Image Capture` liegt weiterhin als separates Schwesterrepository
`..\automated_image_capture`. Reorientation Control importiert daraus zur Laufzeit
nichts; Kamera-, Licht- und Inferenzbausteine sind lokal im neuen Paket enthalten.
Der Windows-Launcher fuer Automated Image Capture zeigt jedoch bewusst auf dieses
Schwesterrepository und erwartet dort eine eigene `.venv`.

## 3. Gesamtarchitektur

### 3.1 Zeitkritische Ebene: TwinCAT SPS

Die SPS laeuft mit einer Taskzeit von `1 ms` (`CycleTime = 1000000 ns`, Prioritaet
20). Alle zeitkritischen Vorgange bleiben in `MAIN.TcPOU`:

- Einlesen, Normalisieren und optionales Entprellen der Lichtschranken
- Flankenerkennung und SPS-Zeitstempel
- Geschwindigkeitsmessung
- Einfrieren der Ausloeseparameter pro Bauteil
- Ventil-Timing und Ventilausgaenge
- EL7047-Zustandsautomat
- Kraftpeak-Suche und Delay-Zeitmessung

Die Python-Pollrate darf diese Zeitauflosung nicht bestimmen.

### 3.2 Bedienebene: Python/PyQt6

Die GUIs bedienen die SPS asynchron per ADS. Die komplette
`pyads.Connection` gehoert dem dauerhaften `AdsWorker` in einem eigenen
`QThread`. Der Qt-Hauptthread greift nie direkt auf die ADS-Verbindung zu.

Aktuelle Kommunikationsparameter:

- AMS Net ID: `10.145.4.14.1.1`
- PLC-IP: `192.168.0.23`
- PLC-Port: TwinCAT 3 PLC1 / ADS-Port 851
- ADS-Timeout: `500 ms`
- automatischer Reconnect: alle `2000 ms`
- normale Live-Abfrage: Sum-Read alle `150 ms`
- Kalibrier- und Kraftdelaystatus: Sum-Read alle `100 ms`
- numerische GUI-Aenderungen: `100 ms` gesammelt, letzter Wert pro Symbol gewinnt
- Fahr-, Stop-, Reset-, Enable- und Aufnahmebefehle: ohne Debounce direkt in die
  Worker-Warteschlange

Bei ADS-Verlust werden normale ausstehende Writes verworfen. Nach einem
Reconnect wird zuerst ein sicherer Stopzustand geschrieben und danach ein
vollstaendiger Snapshot gelesen. Ein gerade laufender ADS-Zugriff kann einen
GUI-Stop trotzdem bis zu 500 ms verzoegern. Das ist keine Sicherheitsfunktion.

### 3.3 Reorientation-Control-Prozessmodell

`ReorientationControlGUI` ist bewusst keine versteckte Instanz der monolithischen
Pressure-GUI. Die Anwendung trennt die laufenden Aufgaben wie folgt:

- ADS/SPS: ein dauerhafter Worker in einem eigenen `QThread`
- Baumer/GenTL: ein eigener Kamera-Worker in einem eigenen `QThread`
- YOLO Detect/OBB: ein eigener Inferenz-Worker mit `latest frame only`
- zwei Neewer RGB660 Pro II: je ein eigener langlebiger `asyncio`-Worker-Thread;
  die GUI orchestriert beide Verbindungen seriell
- GUI, Vorschau und Zustandsanzeige: ausschliesslich im Qt-Hauptthread

Der Kamera-Worker leert den GenTL-Stream kontinuierlich, konvertiert und emittiert
aber nur die konfigurierte Vorschau-Bildrate. Default sind `15 FPS`, einstellbar
von 1 bis 60 FPS. Dadurch entsteht bei einer schnellen Baumer-Kamera keine
Warteschlange alter Vollbilder. YOLO verarbeitet ebenfalls nur das jeweils neueste
Bild und ist auf 15 FPS begrenzt. Die beiden Panels verbinden sich immer
nacheinander, damit WinRT-Scan und GATT-Aufbau nicht ueberlappen und beide Adapter
nicht dasselbe Panel waehlen. Ein blockierender Windows-GATT-Aufruf bleibt dabei
im BLE-Worker und kann den Qt-Hauptthread nicht mehr anhalten.

Der Hardwareblock zeigt einen logarithmischen Exposure-Slider mit dem von GenICam
gemeldeten Wertebereich. Slideränderungen werden 250 ms entprellt und per Queue im
Kamera-Worker auf `ExposureTime`/`ExposureTimeAbs` geschrieben; falls erforderlich,
wird `ExposureAuto` fuer diese Verbindung deaktiviert. Beim Trennen werden
urspruenglicher Exposure- und Auto-Wert wiederhergestellt. Die FPS-Zeile trennt
Kamera-Node (`cam`), gemessenen Rohabruf (`raw`) und Qt-Vorschau (`view`).

Baumer Camera Explorer, Automated Image Capture und Reorientation Control duerfen
die Kamera nicht gleichzeitig oeffnen. Dasselbe gilt praktisch fuer die beiden
BLE-Panels. Unsichtbar weiterlaufende `pythonw.exe`-Instanzen im Task-Manager
kontrollieren. Automated Image Capture und Reorientation Control muessen vor dem
Wechsel jeweils vollstaendig geschlossen werden.

## 4. Hardware und I/O-Zuordnung

### 4.1 Duesen und digitale Ausgaenge

Die ersten 16 Ventile liegen auf zwei EL2008, die acht zusaetzlichen Ventile auf
zwei EL2004.

| Logisches Ventil | PLC-Symbol | Beckhoff-Ausgang |
| --- | --- | --- |
| Array 1, Duese 1-4 | `OpenValve1..4` | Term 7 EL2008, Kanal 1-4 |
| Array 1, Duese 5-6 | `OpenValve17..18` | Term 9 EL2004, Kanal 1-2 |
| Array 2, Duese 1-4 | `OpenValve5..8` | Term 7 EL2008, Kanal 5-8 |
| Array 2, Duese 5-6 | `OpenValve19..20` | Term 9 EL2004, Kanal 3-4 |
| Array 3, Duese 1-4 | `OpenValve9..12` | Term 8 EL2008, Kanal 1-4 |
| Array 3, Duese 5-6 | `OpenValve21..22` | Term 10 EL2004, Kanal 1-2 |
| Array 4, Duese 1-4 | `OpenValve13..16` | Term 8 EL2008, Kanal 5-8 |
| Array 4, Duese 5-6 | `OpenValve23..24` | Term 10 EL2004, Kanal 3-4 |

Die GUI gruppiert die sechs Duesen nach ihrer beabsichtigten Wirkung:

| Array | Duesen 1-3 | Duesen 4-6 |
| --- | --- | --- |
| 1 | Flip um Z | Flip um X |
| 2 | Flip um Y | Flip um X |
| 3 | Flip um Z | Flip um X |
| 4 | Flip um Y | Flip um X |

Jede Duese ist einzeln aktivierbar. Ein Array ist nur aktiv, wenn sowohl das
Array als auch mindestens eine seiner sechs Duesen aktiviert ist.

### 4.2 VTEM-Druckregelung

Die vier Arraydruecke werden auf `0..6000 mbar` begrenzt und wie folgt an die
beiden VTEM-Einheiten uebergeben:

| Array | SPS-Sollwert | VTEM-Zuordnung |
| --- | --- | --- |
| 1 | `PresValueV02` | Unit 0, V02 / Setpoint 1 |
| 2 | `PresValueV04` | Unit 0, V04 / Setpoint 2 |
| 3 | `PresValueV12` | Unit 1, V02 / Setpoint 1 |
| 4 | `PresValueV14` | Unit 1, V04 / Setpoint 2, physisch Supply 4 |

Die VTEM-Prozessdaten sind je Einheit als drei WORD Eingangs- und drei WORD
Ausgangsdaten mit der CPX-FB38 gekoppelt. Die PLC verwendet
`FestoVTEMdc.FB_ValveControl`; die passende Festo-Bibliothek muss auf einem neuen
TwinCAT-Rechner vorhanden sein.

### 4.3 Lichtschranken

Die acht Lichtschranken liegen auf Term 16, EL1018, Kanal 1-8 und sind in der
SPS als `LightBarrierOn1..8` verknuepft. Verwendeter Sensortyp ist nach aktuellem
Stand SICK WTE11-2P2432.

| Paar | Aufgabe | Ausloesende Schranke |
| --- | --- | --- |
| LB1 -> LB2 | Geschwindigkeit Array 1 | LB2 startet Array 1 |
| LB3 -> LB4 | Geschwindigkeit Array 2 | LB4 startet Array 2 |
| LB5 -> LB6 | Geschwindigkeit Array 3 | LB6 startet Array 3 |
| LB7 -> LB8 | Geschwindigkeit Array 4 | LB8 startet Array 4 |

Aktuelle, experimentell nachjustierte Abstaende:

- LB1-2: `40.0 mm`
- LB3-4: `40.0 mm`
- LB5-6: `40.0 mm`
- LB7-8: `40.0 mm`

Diese Werte sind Defaults in Python und SPS. Gespeicherte Profile oder per ADS
geschriebene Werte koennen sie ueberschreiben.

Die Rohlogik wird pro Sensor normalisiert:

```text
logical = raw <> invert
```

Der relevante Bauteileintritt ist danach die fallende Flanke des stabilen
logischen Signals. Aktuelle Defaults:

| Sensor | Logik invertiert | Entprellung aktiv |
| --- | --- | --- |
| LB1 | ja | nein |
| LB2 | ja | nein |
| LB3 | ja | nein |
| LB4 | ja | nein |
| LB5 | ja | nein |
| LB6 | ja | nein |
| LB7 | ja | nein |
| LB8 | ja | nein |

Die globale Entprellzeit ist `20 ms`. Sie kann in der Pressure-GUI geaendert
werden; die Aktivierung selbst ist im Dialog `Light Barrier Settings` pro Sensor
einstellbar. Fuer eine Geschwindigkeitsmessung sollten beide Sensoren eines
Paares dieselbe Entprellstrategie verwenden. Unterschiedliche Filterlatenzen
erzeugen einen konstanten Geschwindigkeitsfehler.

Die SICK-Sensoren muessen auf das reale Objekt und den realen Hintergrund sauber
geteacht sein. Ein kurzes Ein-/Aus-Flattern beim Eintauchen des Objekts,
richtungsabhaengige Schaltpunkte oder nur blinkende Ausgaenge sind Hinweise auf
Teach-, Ausrichtungs-, Kontrast- oder Hystereseprobleme. Nach mechanischen oder
Teach-Aenderungen sind Abstand und Geschwindigkeitsplausibilitaet neu zu pruefen.

### 4.4 Kraftsensoren

Term 18 ist eine EL3068. Aktuell werden zwei Kanaele verwendet:

- `RawNozzlePressure` -> AI Standard Channel 1
- `RawNozzlePressure2` -> AI Standard Channel 2

Die GUI bezeichnet diese Signale im Kraftdelay-Dialog als Kraftsensor 1 und 2.
Angezeigt wird vorerst die vorhandene `0..10`-Signalskala, nicht Newton. Die
Wandlungs-, Sensor- und Filterlatenz ist Bestandteil der gemessenen Antwortzeit.

### 4.5 Foerderband und EL7047

Der NEMA17-Schrittmotor ist encoderlos an Term 19, EL7047, angeschlossen. Der
Motor wurde als 1.5-A-NEMA17 beschrieben. Die gemeldete physische Verdrahtung
lautet A1 -> 1, B1 -> 5, A2 -> 1', B2 -> 5'. Diese Angabe vor dem Einschalten
immer gegen Motordatenblatt, Wicklungspaare und Klemmenbeschriftung pruefen.

Die SPS verwendet das Beckhoff Positioning Interface:

- STM Status und STM Control
- POS Status compact und POS Control
- POS Istposition als interne Positionsquelle
- `StepperInternalPosition` ist im aktuellen `.tsproj` mit
  `POS Status -> Actual position` verknuepft
- `Execute`, `Emergency stop`, `Target position`, `Velocity`, `Start type`,
  `Acceleration` und `Deceleration` sind verknuepft

Wichtige Skalierung:

- `0x8010:06`: 200 Vollschritte pro Motorumdrehung
- 1 Vollschritt = 64 EL7047-Positionsinkremente
- `0x8012:05 Speed Range`: im PLC-Code mit `2000 Vollschritten/s` angenommen
- relative Kalibrierfahrt: Start Type `2`
- Dauerlauf: `ENDLESS_PLUS = 3`, `ENDLESS_MINUS = 4`
- Beschleunigung und Verzoegerung im zyklischen Befehl: jeweils `1000`

Vor dem Betrieb muss `0x8012:05` online zur PLC-Konstante passen. Motorstrom und
Haltestrom werden in der EL7047/CoE-Konfiguration eingestellt, nicht in der
Python-GUI. Der Motor darf nicht dauerhaft ueber seinen Nennstrom betrieben
werden; Temperatur und benoetigtes Drehmoment muessen praktisch geprueft werden.

Die Kommentare in `MAIN.TcPOU` nennen an einigen Stellen noch `Term 14`. Das ist
veraltet; im aktuellen Hardwareprojekt ist die Schrittmotorklemme `Term 19
(EL7047)`. Ausserdem ist die Endklemme EL9011 im Projekt ebenfalls mit `Term 19`
benannt, hat intern aber Box-ID 20. Nicht verwechseln.

## 5. SPS-Funktionslogik

### 5.1 Lichtschranken und Geschwindigkeit

Die SPS normalisiert zuerst jedes Rohsignal, wendet optional die stabile
Entprellung an und bildet danach die fallende Flanke. Ereigniszaehler,
SPS-Zeitpunkt und aktuelle Foerderbandposition werden im selben PLC-Zyklus
gespeichert.

Die Geschwindigkeitsformel pro Paar ist:

```text
velocity_mm_s = sensor_spacing_mm * 1000 / travel_time_ms
```

Die Aufloesung betraegt einen SPS-Zyklus, also nominell 1 ms. Es werden keine
hochaufloesenden Hardware-Timestamps der EL1018 benutzt. Deshalb nimmt der
relative Quantisierungsfehler bei kurzen Laufzeiten zu. Die bisherigen
Min-/Max-Travel-Time-Filter wurden bewusst wieder entfernt; die Messung basiert
direkt auf den akzeptierten Paarflanken.

### 5.2 Trigger und Ventilzeitpunkt

Beim Trigger wird der berechnete Delay fuer das jeweilige Bauteil eingefroren,
damit spaetere Geschwindigkeits- oder GUI-Aenderungen einen bereits laufenden
Impuls nicht verschieben.

Aktuell gilt pro Array:

```text
effective_force_response_ms = Interpolation nach aktiver Duesenzahl
offset_delay_ms = max(0, offset_mm * 1000 / measured_velocity_mm_s
                         - effective_force_response_ms)
total_wait_ms = manual_delay_ms + offset_delay_ms
```

`Offset` bedeutet den gewuenschten Weg vom ausloesenden Lichtschrankenpunkt bis
zum Kraftwirkpunkt an der Duese. Die Kraftantwortzeit wird abgezogen, weil das
Ventil entsprechend frueher geschaltet werden muss. Ohne gueltige
Geschwindigkeitsmessung wird `offset_delay_ms = 0`; dann wirkt nur der manuelle
Delay.

Die Kraftantwortzeit ist pro Array mit zwei Endpunkten einstellbar:

- eine aktive Duese: `GuiForceSingleNozzleResponseDelayMsN`
- vier oder mehr aktive Duesen: `GuiForceResponseDelayMsN`
- zwei und drei aktive Duesen: lineare Interpolation zwischen beiden Werten
- fuenf und sechs aktive Duesen verwenden derzeit denselben Endpunkt wie vier

Aktueller Default fuer beide Endpunkte aller Arrays ist `15.0 ms`. Fruehere
Messungen zeigten beispielhaft etwa 34 ms fuer eine Duese und etwa 25.8 ms fuer
vier Duesen an Array 1; die tatsaechlich geeigneten Werte muessen jedoch mit dem
aktuellen Aufbau, Druck, Ventilzustand und Messverfahren neu bestaetigt werden.

Wenn der Kraftimpuls bei hoeherer Bauteilgeschwindigkeit raeumlich zunehmend zu
frueh liegt, ist die abgezogene Kraftantwortzeit wahrscheinlich zu gross. Eine
praktische Korrektur ist:

```text
correction_ms = early_distance_mm / velocity_mm_s * 1000
new_response_ms = old_response_ms - correction_ms
```

### 5.3 Ventilimpuls

Nach `total_wait_ms` werden nur die aktivierten Duesen des aktiven Arrays fuer
die eingestellte Pulsdauer eingeschaltet. Grenzen:

- Druck: `0..6000 mbar`, GUI-Schrittweite `10 mbar`
- manueller Delay: `0..1000 ms`
- Pulsdauer: `1..500 ms`
- Offset: `0..5000 mm`

Im Foerderband-Kalibriermodus oder Geschwindigkeits-Plausibilitaetsmodus sind
alle 24 Ventilausgaenge zwangsweise aus und normale Arraytrigger deaktiviert.

### 5.4 Kraftantwort-Einstellungen

Der Hauptdialog bietet `Force Delay Settings` statt der frueheren
Kraftpeak-Messung. Alle vier Arrays werden gleichzeitig dargestellt. Pro Array
koennen die Kraftantwortzeiten fuer eine aktive Duese und fuer vier oder mehr
aktive Duesen im Bereich `0..1000 ms` eingestellt werden. Zwei und drei aktive
Duesen werden weiterhin linear interpoliert. `Reset Fields to 15 ms` setzt nur
die Eingabefelder zurueck; `Apply to PLC` schreibt alle acht Werte gemeinsam in
einem ADS-Batch. Die Werte werden ausserdem in Druckprofilen gespeichert und
beim Profil-Laden wiederhergestellt.

Der Dialog zeigt fuer jedes Array auch die zugehoerige Triggerlichtschranke und
den aktuellen Debounce-Zustand. Debouncing verschiebt die akzeptierte Flanke um
ungefaehr die konfigurierte Debounce-Zeit. Wenn beide Lichtschranken eines
Geschwindigkeitspaars gleich entprellt werden, hebt sich der gemeinsame Anteil
in der Laufzeitmessung weitgehend auf. Am zweiten Sensor bleibt die Verschiebung
jedoch im Triggerzeitpunkt enthalten. Sie darf deshalb nicht in die reine
Ventil-zu-Kraft-Antwortzeit eingerechnet werden. Eine Aenderung der Entprellung
kann den raeumlichen Kraftwirkpunkt um ungefaehr
`velocity_mm_s * debounce_ms / 1000` verschieben und erfordert eine erneute
Pruefung des Offsets.

Die PLC-Unterstuetzung fuer die fruehere LB-bis-Kraftpeak-Diagnose bleibt aus
Kompatibilitaetsgruenden im Projekt, ist aber nicht mehr ueber den Hauptdialog
erreichbar. Ihre bekannte Grenze bleibt bestehen: LB-bis-Peak enthaelt jeden
manuellen, Offset- und Debounce-Delay. Eine reine Kraftantwort sollte als
`Ventilausgang EIN -> Kraftpeak` bestimmt werden.

### 5.5 Foerderbandzustandsautomat

Normalbetrieb verwendet eine endlose Positionierfahrt. Kalibrier- und Jogfahrten
sind endliche relative Fahrten. Neue Fahrbefehle werden nur im Stillstand
akzeptiert; weitere Befehle waehrend `Busy` werden abgelehnt. Stop aktiviert den
Emergency-Stop-Ausgang der Positionierschnittstelle.

Bei Kalibriermodus, Stop oder Verbindungsende darf keine endlose
Kalibrierbewegung weiterlaufen. `GuiConveyorReverse` vertauscht die physische
Links-/Rechtsrichtung, ohne die restliche Logik umzuschreiben.

## 6. Foerderbandkalibrierung

Das Band ist encoderlos. Die Kalibrierung nutzt daher die befohlene interne
EL7047-Position und kann Schlupf oder verlorene Schritte nicht erkennen.

Vorgehen:

1. Zwei Markierungen mit bekanntem Abstand verwenden; Default `315.0 mm`.
2. Mit kleinen endlichen Vollschritt-Fahrten zur ersten Markierung fahren und
   `Calibrate Left Marking` aufnehmen.
3. Zur zweiten Markierung fahren und `Calibrate Right Marking` aufnehmen.
4. Die Reihenfolge der Markierungen ist beliebig; Aufnahmen nur im Stillstand.
5. Eine Null-Differenz ist ungueltig und ersetzt keine gueltige Kalibrierung.

Berechnung:

```text
full_steps = abs(position_right - position_left) / 64
mm_per_full_step = marker_distance_mm / full_steps
full_steps_per_mm = 1 / mm_per_full_step
```

Aktueller gespeicherter/default PLC-Wert:

- `0.32960026 mm/Vollschritt`
- etwa `3.034 Vollschritte/mm`

Dieser Faktor wird sofort fuer mm-Jogging und die Umrechnung der
Foerderbandgeschwindigkeit verwendet:

```text
full_steps_per_second = requested_mm_per_second / mm_per_full_step
el7047_velocity_0_to_10000 = full_steps_per_second / speed_range * 10000
```

Ohne gueltige Kalibrierung bleibt als Rueckfall die alte proportionale Umrechnung
gegen `GuiConveyorMaxSpeedMmPerSec` erhalten. Das mm-Jogging verlangt jedoch eine
gueltige Kalibrierung.

## 7. Pressure Control GUI

Startdatei: `CSVSaver/PressureControlGUI.py`

Die Haupt-GUI ist die Bedienoberflaeche fuer normale Versuche. Sie enthaelt:

- Foerderband Enable, Reverse, Reset und Geschwindigkeit
- `Light Barrier Settings` mit Sensorabstaenden LB1-2, LB3-4, LB5-6 und LB7-8,
  globaler Debounce-Zeit sowie Invert-/Debounce-Enable je Lichtschranke
- UR-Ry-Sollwinkel mit explizitem `Apply UR Angle`
- vier Arrayzeilen mit Array-Enable, sechs Duesen-Checkboxen, Druck, manuellem
  Delay, Pulsdauer, Offset, geschaetzter Geschwindigkeit und Offsetdelay
- `Calibrate Conveyor`
- `Jog Conveyor`
- `Force Delay Settings` fuer alle vier Arrays
- Profil laden/speichern
- `Load Pose Roadmap` zur Auswahl eines gerichteten Kalibrieruebergangs
- `Write All Values`

Die Lichtschranken-Invertierung wird im Settings-Dialog sichtbar dargestellt und
zusammen mit dem Profil gespeichert. Selektives Abschalten der Entprellung ist
im selben Dialog moeglich. Alle acht Lichtschranken sind standardmaessig
invertiert und nicht entprellt; alle vier Sensorabstaende starten bei `40 mm`.

`Conveyor max` ist in der Haupt-GUI bewusst ausgeblendet und wird vorlaeufig
fest mit `1000.0 mm/s` gespeichert und an die SPS geschrieben. Auch ein Profil
mit abweichendem Altwert ueberschreibt diesen festen Wert derzeit nicht.

### Roadmap-Auswahl fuer die Uebergangskalibrierung

`Load Pose Roadmap` erwartet die versionierte `*_roadmap.json` aus dem
Schwesterrepository `bibazu_geometry_to_pose`. Der aktuelle Export bettet pro
Pose ein kleines PNG als `thumbnail_png_base64` direkt in die JSON ein. Dadurch
benoetigt die Pressure-GUI zum Anzeigen der Posekarten kein trimesh, Matplotlib
oder lokales CAD. Aeltere Roadmaps ohne Bilder bleiben ladbar und zeigen einen
Textplatzhalter.

Der modale Dialog zeigt:

- robuste Posen mit kraeftigem Rahmen,
- metastabile Zwischenposen mit gestricheltem Rahmen,
- alle gerichteten direkten Kanten in einer anklickbaren Tabelle,
- die Roadmap-ID als `Uebergang <von>-<nach>`,
- Aktuator, Sollwinkel, Einfangbreite `w` und geometrischen Score `s`.

Kanten, deren Quelle und Ziel beide robust sind, werden unabhaengig von der
Reihenfolge in der JSON zuerst angezeigt und blau/fett hervorgehoben. Alle
Kanten mit mindestens einer metastabilen Pose folgen dezenter darunter.

Passive Kippkanten bleiben sichtbar, sind aber nicht als Druckkalibrierung
auswaehlbar. Ein Doppelklick oder `Uebergang uebernehmen` schliesst den Dialog
und setzt den Kalibrierkontext. Im Hauptfenster erscheint danach oben mittig ein
blaues Banner mit Bauteil, Uebergangs-ID, Aktion und je einem Bild der Quell- und
Zielpose. Wird anschliessend `Save Profile` verwendet, lautet der vorgeschlagene
Dateiname beispielsweise:

```text
Df1a_Uebergang_9-35_wall_main_neg_x.json
```

Die Profilversion ist 9. Version 9 ergaenzt LB7/LB8 sowie den Abstand LB7-8;
der Roadmap-Kontext beeinflusst weiterhin nur Anzeige und Dateinamensvorschlag.

Beim Oeffnen von Kalibrier- oder Jogdialogen wird normales Conveyor-Enable
ausgeschaltet. Beim Schliessen erfolgt kein automatischer Wiederanlauf.

## 8. Conveyor Setup GUI

Startdatei: `CSVSaver/ConveyorSetupGUI.py`

Diese separate GUI ist fuer Inbetriebnahme und Kalibrierung gedacht, nicht fuer
den normalen Duesenbetrieb. Sie bietet:

- Anzeige des Kalibrierfaktors und der aktuellen Speed-Umrechnung
- Foerderbandkalibrierung und mm-Jogging
- Live-Status aller acht Lichtschranken
- gespeicherte Logikinvertierung pro Lichtschranke
- Auswahl zweier Lichtschranken und automatische Distanzmessung ueber die im
  SPS-Zyklus gelatchte EL7047-Position
- Anwenden des gemessenen Abstandes auf Paar 1-2, 3-4 oder 5-6
- alternative Lichtschranken-Distanzkalibrierung mit der zyklisch gelesenen
  UR-TCP-Pose
- Foerderband-Geschwindigkeitsplausibilitaet bei konstanter Bandfahrt
- UR-Geschwindigkeitsplausibilitaet ueber mehrere Vorwaerts-/Rueckwaertspassagen
- High-Speed-Aufnahme und manuelle LB-bis-Bewegung-Auswertung mit der zweiten
  Baumer-USB-Kamera

Fuer die UR-TCP-Pose wird `10.10.10.10:30002` gelesen. Die Verbindung hat einen
kurzen Timeout und wird automatisch neu aufgebaut. Der UR-Plausibilitaetsmonitor
vergleicht gemessene Paarzeiten mit einer einstellbaren UR-Sollgeschwindigkeit
und protokolliert einzelne Durchlaeufe in `ur_speed_plausibility.csv`.

Empfohlenes Verfahren fuer Lichtschrankenabstaende:

1. Sensoren zuerst sauber teachen und mechanisch fixieren.
2. UR langsam und mit konstanter Geschwindigkeit in beide Richtungen durch das
   Paar fahren.
3. Vorwaerts- und Rueckwaertsergebnisse getrennt beurteilen.
4. Mehrere gute Durchlaeufe verwenden; Aussetzer und Flattern aussortieren.
5. Bei konstantem prozentualem Geschwindigkeitsfehler den effektiven Abstand
   proportional korrigieren.
6. Ergebnis per `Apply Sensor Spacing` an die SPS schreiben und danach mit einem
   neuen Sollwert plausibilisieren.

### 8.1 Pressure-Delay-Aufnahme

Der vierte Reiter `Pressure Delay` bindet die Baumer VCXU-02C mit Seriennummer
`700005072151` ueber
`C:\Program Files\Baumer Camera Explorer\bgapi2_usb.cti` an. Die Kamera liefert
bei 640x480, BayerRG8 und 4000 us Belichtung am aktuellen Rechner ungefaehr
247 FPS. Die Vorschau wird auf 30 FPS begrenzt; waehrend einer Aufnahme werden
alle abgeholten Kameraframes an einen separaten JPEG-Writer uebergeben.

`Record` speichert ab dem Tastendruck. Der aktuelle Ereigniszaehler der
ausgewaehlten Lichtschranke bildet den Ausgangswert. Bei der ersten neuen
akzeptierten fallenden SPS-Flanke wird der Trigger markiert und nach dem einstellbaren
Nachlauf, standardmaessig 200 ms, automatisch gestoppt. `Stop` beendet jederzeit
manuell. Der bestehende Pressure-/Conveyor-Ablauf loest den Druckimpuls aus; der
Reiter schreibt keine SPS-Werte.

Kamera und SPS werden ohne zusaetzliche Verdrahtung synchronisiert. Dazu werden
der vorhandene 1-ms-Zaehler `MAIN.LightBarrierEventClockMs`, die im SPS-Zyklus
gelatchte Ereigniszeit, die ADS-Roundtrip-Zeit und die Kamera-Nanosekundenzeit
auf die Host-Monotonic-Uhr abgebildet. Das Ergebnis ist framegenau, aber keine
elektrische Sub-Millisekunden-Synchronisation. Die geschaetzte Unsicherheit wird
in der GUI und in den Metadaten ausgewiesen.

Standardausgabe ist
`Pictures\BiBaZu\PressureDelayCalibration\YYYYMMDD_HHMMSS_LB<n>`. Jede Session
enthaelt JPEG-Einzelbilder, `frames.csv` und `session.json`. Der erste sichtbare
Bewegungsframe wird im integrierten Viewer markiert; der berechnete
LB-bis-Bewegung-Wert wird in der Session und in `calibration_results.csv`
gespeichert. Er enthaelt Debounce, manuellen Delay und Offsetdelay und wird daher
nicht automatisch als Kraftantwortzeit in die SPS geschrieben.

Die Legacy-Umgebung benoetigt jetzt neben PyQt6 und pyads auch Harvester,
GenICam, NumPy und OpenCV. Die reproduzierbaren Abhaengigkeiten stehen in der
neuen `requirements.txt` im Repository-Stamm.

## 8.2 BiBaZu Reorientation Control

Startordner: `ReorientationControlGUI`

Paket/CLI: `bibazu_reorientation` / `bibazu-reorientation`

Produktname: **BiBaZu Reorientation Control**

Die komplette bedienerseitige Oberfläche dieser Anwendung ist Englisch. Dazu
gehören Hauptfenster, Setup-/Roadmap-/Hardwaredialoge, Preflight, Statuswerte und
alle in der GUI angezeigten Validierungs- und Hardwarefehlermeldungen.

Die Anwendung fuehrt die Ergebnisse von PressureControl, Kameraaufnahme und YOLO
in einem kontinuierlichen Lauf mit beliebig vielen eingereihten Bauteilen zusammen.
Alle sichtbaren Teile werden getrennt von rechts nach links verfolgt. Schema v1 kennt genau
zwei Posen; YOLO-Klasse 0 muss `Pose 1`, Klasse 1 muss `Pose 2` heissen. Schema v2
nutzt die explizite Klassen-zu-Roadmap-Pose-Zuordnung und kann einen eindeutigen
direkten Pfad oder einen Pfad mit genau einer Zwischenpose ausfuehren. Als Ziel kann
in Schema v1 Pose 1 oder Pose 2 konfiguriert werden:

- Ziel Pose 1: Pose 1 wird ohne Duesenimpulse transportiert, Pose 2 verwendet das
  konfigurierte Profil `2 -> 1`.
- Ziel Pose 2: Pose 2 wird ohne Duesenimpulse transportiert, Pose 1 verwendet das
  konfigurierte Profil `1 -> 2`.

Der Pass-through verwendet Bandrichtung, Geschwindigkeit und Kalibrierung des
ausgewaehlten Pressure-Profils, aber eine effektive Arraymaske von null. Das gilt
auch fuer unsichere/unbekannte Erkennungen und nicht eindeutig planbare Posen. Die
SPS bestaetigt Transport-/Triggerabschluss je Queue-Sequenz, nicht die tatsaechlich
erreichte Endorientierung. Eine kamerabasierte Nachkontrolle kommt erst spaeter.

### Bauteilkonfiguration und Bedienoberflaeche

Die drei Schaltflaechen links oben stehen vertikal untereinander:

- `New configuration`
- `Open configuration`
- `Edit configuration`

Neue Konfigurationen beginnen jetzt mit einer Roadmap (`.yaml`, `.yml` oder
`.json`). Unterstuetzt werden das Handover-Format mit `part/poses/transitions` und
der interne Export mit `source/nodes/edges`. Beide werden streng in ein gemeinsames
Modell aus allen stabilen/metastabilen Posen, Quaternionen und gerichteten Kanten
ueberfuehrt. Bei einer Handover-YAML werden Vorschaubilder aus einem strukturell
passenden gleichnamigen JSON uebernommen.

Der Dialog fuellt Bauteilname und CAD-Pfad vor, laesst beide aber editierbar. Jede
robuste Pose erhaelt explizit eine eindeutige YOLO-Klassen-ID. Die Zielpose wird
ueber die Bildkarten ausgewaehlt. Pressure-Profile sind nur fuer aktuierte
Robust-zu-Robust-Kanten editierbar; passive und metastabile Uebergaenge bleiben in
einer separaten Infotabelle. Df1a ergibt 11 Posen, 23 Kanten, 4 robuste Posen und
die sechs Profilzeilen `9->35`, `9->60`, `24->35`, `24->60`, `35->24`, `60->9`.
Parallele Kanten werden ausschliesslich ueber `edge_id` unterschieden.

Jede editierbare Robust-zu-Robust-Kante besitzt `Preview ...`. Der Dialog laedt
das aktuell im Setup eingetragene STL/OBJ genau einmal und rendert Startpose,
Zielpose sowie eine per Quaternion-SLERP interpolierte Endlosschleife. Pause,
Scrubbing und Neustart sind moeglich; Vorzeichen des Sollwinkels und `edge_id`
bleiben sichtbar. Wenn das CAD fehlt oder nicht gerendert werden kann, zeigt der
Dialog die Roadmap-Vorschaubilder mit Richtungspfeil. Die Interpolation ist nur
eine Orientierungshilfe und keine physikalische Simulation der Rutschenbahn.

Schema-v2-Dateien duerfen ohne Profile als Entwurf gespeichert werden. Die
Readiness zeigt fehlende Profile, Erreichbarkeit zum Ziel, Klassenmapping,
Roadmap-Hash und bewusst abweichende Stammdaten. Beim Laden wird SHA-256 geprueft;
eine Aenderung verlangt `Re-import roadmap`. Dabei bleiben nur Zuordnungen
mit weiterhin identischen Pose-/Kanten-IDs erhalten.

Schema v2 ist jetzt fuer den bewusst begrenzten Roadmap-Fall ausfuehrbar: Der
Resolver akzeptiert einen eindeutigen direkten Pfad oder maximal eine Zwischenpose.
Nur Kanten mit zugeordnetem Pressure-Profil werden betrachtet. Parallele belegte
Kanten bleiben ueber `edge_id` getrennt und blockieren als mehrdeutig, statt dass
die Anwendung eine Aktion errät. Fehlende Profile bleiben fuer Drafts erlaubt;
wird spaeter eine nicht erreichbare Pose erkannt, wird sie als
`bypass_uncertain` mit Arraymaske null eingereiht.

Alle geladenen Profile werden auf `ur_ry_angle_deg` und
`conveyor_speed_mm_per_sec` verglichen. Gleiche Werte fuellen die beiden Felder im
Hardwarebereich automatisch. Bei Abweichungen zeigt die GUI jede Kante mit
Profilpfad und Einzelwerten; der Bediener kann die gemeinsamen Werte einstellen
und mit `Use machine parameters` bestaetigen. Beim Laufstart werden noch nicht
bestaetigte Eingabewerte automatisch uebernommen. Aenderungen an diesen Feldern
loeschen die bereits geladene Konfiguration nicht mehr. Ein expliziter UR-Wert braucht danach
weiterhin den separaten quittierten `Apply UR angle`-Schritt.

Der manuelle Conveyor-Start ist bewusst unabhaengig von Config, YOLO, Leuchten,
Roadmap-Vollstaendigkeit und Profil-Readiness. Er verlangt nur ADS, einen inaktiven
Automatiklauf und positive Geschwindigkeit, schreibt alle vier Array-Enables
false und setzt einen vom vorherigen Lauf verriegelten PLC-Fehler vor dem Start
automatisch zurueck.

Nach fuenf identischen Erkennungen wird die Pose eines Tracks unveraenderlich
festgeschrieben. Beim Ueberqueren der einstellbaren Uebergabelinie kombiniert der
Controller ausschliesslich aktive Arrays
der gewaehlten ein oder zwei Pfadprofile zu einem SPS-Profil. Eine doppelte Belegung
desselben physischen Arrays wird blockiert. Array 1..4 entsprechen der raeumlichen
Reihenfolge LB2/LB4/LB6/LB8. Somit kann beispielsweise `2 -> 3 -> 4` in einem
Banddurchlauf stattfinden, wenn die beiden Profile unterschiedliche, passend
aufeinanderfolgende Arrays verwenden. Automatisches Tauschen gleichartiger
Seiten-/Bodenarrays ist weiterhin ein spaeterer Ausbau.

YAML-Schema v1:

```yaml
schema_version: 1
part_name: Df1a
model_path: models/best.pt
mesh_path: ../../bibazu_geometry_to_pose/Werkstücke_STL_grob/Df1a.STL
poses:
  - {id: 1, label: Pose 1, model_class_id: 0}
  - {id: 2, label: Pose 2, model_class_id: 1}
target_pose: 1
roadmap_path: ../../bibazu_geometry_to_pose/Poses_Found_Robust/Df1a_roadmap_provisional/Df1a_roadmap.json
target_roadmap_pose_id: 15
transitions:
  - {from_pose: 2, to_pose: 1, pressure_profile: profiles/Df1a_2_to_1.json}
```

Relative Modell-, Mesh-, Roadmap- und Profilpfade werden relativ zur YAML
aufgeloest. `mesh_path`, `roadmap_path` und `target_roadmap_pose_id` sind
rueckwaertskompatibel optional; die beiden Roadmap-Felder muessen gemeinsam
gesetzt sein und die ID muss in der Roadmap robust sein. Die GUI rendert das STL/OBJ links
oben ohne zusaetzliche Rotation; fuer V1 gilt die gespeicherte Orientierung als
Zielorientierung. Der Dateidialog startet im Workspace unter
`bibazu_geometry_to_pose/Werkstücke_STL_grob`. Spaetere Pose-/Sequenzdaten sollen
Rotationen liefern, ohne dieses Config-Modell zu ersetzen.

Hardware-Robustheit: Die Reorientation-GUI migriert die alte SPS-IP
`192.168.10.23` beim Laden auf `192.168.0.23`. `Connect all components` verbindet
die beiden Neewer-Panels bewusst seriell, uebergibt das beim Scan erhaltene
Windows-BLEDevice und verhindert per gegenseitigem Adressausschluss eine doppelte
Panelwahl. BLE-Befehle sind serialisiert; Timeout oder Verbindungsverlust loesen
den Client und hinterlassen einen sauber wiederholbaren Fehlerzustand. Mit
`Disconnect all components` werden laufende BLE-Tasks abgebrochen sowie beide
Leuchten, Kamera und ADS ohne blockierendes Warten im GUI-Thread getrennt.
`Start conveyor` und `Stop conveyor` erlauben davon unabhaengig manuellen Transport.
Der Start verwendet das vorhandene Geschwindigkeitsfeld, erzwingt Vorwaertsfahrt
und schreibt alle vier Array-Enables false. Freigabe gibt es nur bei ADS-Verbindung,
ReorientationState 0, gueltiger Kalibrierung sowie stehendem, fehlerfreiem Antrieb.
In einem aktiven Produktionslauf ist manueller Start gesperrt; Stop fordert dann das
kontrollierte Laufende mit vollstaendigem Queue-Drain an.
Reorientation Control und Automated Image Capture halten beim Programmstart
zusaetzlich denselben Windows-Named-Mutex `Local\BiBaZuCameraAndLights`. Eine
zweite Hardware-GUI beendet ihren Start mit klarer Meldung, bevor Adapter erzeugt
werden. Reorientation Control, Pressure Control und Conveyor Setup halten
zusaetzlich exklusiv `Local\BiBaZuPlcControl`, sodass nur eine schreibende
SPS-Steuer-GUI starten kann. In der Reorientation-GUI sind einzelne
Auto-Reconnect-Loops deaktiviert;
ein zentraler Ablauf verbindet und wiederholt beide Panels ausschliesslich
seriell. Jeder Paneladapter fuehrt Scan, GATT-Verbindung und Befehle in einem
eigenen langlebigen Asyncio-Worker-Thread aus. Connect, BLE-Cancel und Shutdown
haben harte Zeitgrenzen, damit ein haengender WinRT-Callback weder die Qt-GUI noch
das Beenden der Anwendung blockiert.
Signale aus BLE-, YOLO- und UR-Threads sind ausschliesslich mit gebundenen
`QObject`-/`pyqtSlot`-Methoden verbunden. Keine anonyme Python-Lambda darf aus
einem Worker heraus Labels, Dialoge oder MainWindow-Zustand anfassen.

YAML-Schema v2 (gekuerzt):

```yaml
schema_version: 2
roadmap_path: ../../bibazu_geometry_to_pose/.../Df1a_roadmap.yaml
roadmap_sha256: <64 hex Zeichen>
part_name: Df1a
mesh_path: ../../bibazu_geometry_to_pose/Werkstücke_STL_grob/Df1a.STL
model_path: models/Df1a_best.pt
poses:
  - {id: 9, label: Pose 9, model_class_id: 0}
  - {id: 24, label: Pose 24, model_class_id: 1}
  - {id: 35, label: Pose 35, model_class_id: 2}
  - {id: 60, label: Pose 60, model_class_id: 3}
target_pose: 35
transition_profiles:
  "a0:9->35:wall_main_neg_x": profiles/Df1a_9_to_35.json
  "a1:9->60:free_z": null
```

Alle v2-Pfade werden soweit das Laufwerk es erlaubt auch mit `..` relativ zur
Config gespeichert und beim Laden relativ zu dieser aufgeloest. Zugeordnete
Profile werden strukturell validiert; Legacy-Felder duerfen weiterhin den spaeteren
PLC-Baseline-Snapshot benoetigen.

Das Pressure-Profil liefert Druck, aktive Arrays/Duesen, Delay, Pulsdauer,
Offset, Bandparameter, Lichtschrankeneinstellungen und Kalibrierung. Das reine
Auswaehlen oder Laden schreibt nichts an die SPS. Versionen 1 bis 9 werden in ein
kanonisches unveraenderliches Modell migriert; fehlende alte Maschinenfelder
werden erst gegen einen einmalig gelesenen PLC-Baseline-Snapshot aufgeloest.
Fehlende Arrays sind deaktiviert, alte Duesenlisten werden auf sechs Duesen
erweitert. Ein ausfuehrbares Uebergangsprofil braucht Vorwaertsfahrt, positive
Geschwindigkeit und mindestens ein aktives Array mit aktiver Duese. Rueckwaertsfahrt
ist in V1 nicht zulaessig.

### Kamera, YOLO und Beleuchtung

Unter `Configuration -> Hardware settings` werden Kamera-IP/-Seriennummer,
Baumer-CTI, Vorschau-FPS, SPS-IP/AMS-Net-ID/Port und beide BLE-Adressen gespeichert.
Backend ist `QSettings` unter
`LeibnizUniversitaetHannover/BiBaZuReorientationControl`. Defaults:

- Baumer-IP: `169.254.117.70`
- CTI: `C:\Program Files\Baumer Camera Explorer\bgapi2_gige.cti`
- Vorschau: `15 FPS`, einstellbar `1..60`
- SPS: IP `192.168.0.23`, AMS `10.145.4.14.1.1`, Port `851`

YOLO unterstuetzt normale Detect-Boxen und OBB. Jeder Track verlangt ein
vollstaendig sichtbares, bekanntes Objekt auf fuenf unterschiedlichen, frischen,
aufeinanderfolgenden Frames derselben Klasse. Fehlende, unbekannte,
widerspruechliche oder veraltete Erkennungen setzen den noch nicht gesperrten
Konsens zurueck. Das Modell wird nach dem
Laden einer Konfiguration automatisch in einem eigenen Worker geladen; ein
`Load YOLO model`-Button erlaubt Reload. Bei gemappten Erkennungen zeigt das
Box/OBB-Overlay die Roadmap-Pose groß und seitlich daneben nur die Konfidenz in
Prozent; die YOLO-Klassen-ID bleibt im Bild verborgen. Dieselbe Zuordnung wird
fuer den Konsens verwendet. Schema v2 verlangt alle explizit gemappten
Klassen im Modell, erlaubt aber weitere Klassen; deren Erkennung blockiert den
Konsens. Der Warm-up-Frame wird verworfen; GPU wird verwendet, wenn verfuegbar,
sonst CPU.

Ein Modell-Reload stoppt den bisherigen Inference-Worker nichtblockierend und
startet das neue Modell nach dessen `finished`-Signal automatisch. Waerenddessen
wird `model_ready` sofort false gesetzt; ein Lauf kann deshalb nicht mehr mit
einem bereits gestoppten Worker in `DETECTING` starten.

Beide Neewer RGB660 Pro II muessen verbunden und eingeschaltet sein. Pro aktueller
Verbindung muss mindestens ein Lichtbefehl bestaetigt und die manuelle
Zyklus-Checkbox gesetzt sein. BLE meldet nur den zuletzt bestaetigten Befehl,
keinen physisch gemessenen Istwert. Beim Trennen bleiben die Panels in ihrem
letzten Zustand; explizite Ausschaltbuttons sind vorhanden.

### Ablauf und Preflight

Zustaende der GUI:

```text
NO_CONFIG/OFFLINE -> READY -> STARTING -> RUNNING -> FINISHING ->
DRAINING -> COMPLETE
```

Aktive Zustaende koennen in einen sichtbaren, verriegelten `FAULT` wechseln.
Preflight verlangt insbesondere aufgewärmtes Modell, frischen Kameraframe,
ADS plus Reorientation-PLC-Vertrag, stehendes Band,
gueltige Bandkalibrierung, vier idle Arrays, keine Pending-Trigger, 24 geschlossene
Ventile, fehlerfreie EL7047/VTEM-Zustaende und acht fuer mindestens
Debounce+100 ms freie normalisierte Lichtschranken.

Leuchtenverbindung, bestaetigte Befehle und Bediener-Checkboxen bleiben sichtbar
und werden geloggt, blockieren den Start auf ausdruecklichen Betreiberwunsch aber
nicht mehr.

Ein `ur_ry_angle_deg` wird nur verwendet, wenn das Feld im Profil tatsaechlich
vorhanden ist. Dann muss der Winkel ueber den separaten Button angewendet und
exakt quittiert werden. Alte Profile erhalten keinen impliziten 18-Grad-Befehl.

Laufstart-/Queue-Reihenfolge:

1. Band, Arrays und Messmodi deaktivieren und Readback pruefen.
2. Profilkonfiguration ohne Freigabe schreiben und vollstaendig zuruecklesen.
3. Einen gegen `ReorientationHeartbeatAck` neuen Heartbeat vorladen.
4. PLC-Ownership und Reset setzen und `State=10`, Fault 0 sowie HeartbeatAlive
   abwarten; Reset erst danach loesen und denselben Zustand erneut bestaetigen.
5. Bandfreigabe setzen und Start bis zur PLC-Quittierung `State=20` halten.
6. Pro Linienuebergabe alle Staging-Felder schreiben und zuletzt den monotonen
   Commit setzen. Erst `ReorientationQueueEnqueueAck` bestaetigt die Aufnahme.
7. `Finish` erst nach einer Sekunde ohne sichtbaren Track und ohne ausstehenden
   Commit setzen. Nach Queue-/Result-Drain Roh-Enables false schreiben und pruefen,
   erst dann den Owner freigeben.

Zielpose, ungesicherte Pose und nicht planbare Pose erhalten einen regulaeren
Queue-Eintrag mit Maske null. Sie durchlaufen alle acht Sensorcursor, loesen aber
keine Ventile aus und halten damit die physische Reihenfolge fuer Folgeteile stabil.

### PLC-Reorientation-Vertrag

GUI nach PLC:

- `GuiReorientationControlActive`
- `GuiReorientationHeartbeat`
- `GuiReorientationStart`
- `GuiReorientationAbort`
- `GuiReorientationReset`
- `GuiReorientationFinish`, `GuiReorientationResultAck`
- `GuiReorientationQueueSequence`, `GuiReorientationQueueTrackId`
- `GuiReorientationQueuePoseId`, `GuiReorientationQueueDecisionCode`
- `GuiReorientationQueueArrayMask`
- je Array `GuiReorientationQueueNozzleMaskN`, `PressureMbarN`, `DelayMsN`,
  `PulseMsN`, `OffsetMmN`, `ForceResponseMsN`, `ForceSingleMsN`
- `GuiReorientationQueueCommit` als letzter atomarer Publish-Schritt

PLC nach GUI:

- `ReorientationHeartbeatAck`, `ReorientationHeartbeatAlive`
- `ReorientationState`, `ReorientationFaultCode`, `ReorientationBusy`
- `ReorientationExitSeen`, `ReorientationArraysIdle`
- `ReorientationExpectedArrayMask`, `ReorientationTriggeredArrayMask`
- `ReorientationComplete`, `ReorientationCycleCounter`
- `ReorientationQueueDepth`, `ReorientationQueueCapacity`,
  `ReorientationQueueEnqueueAck`
- `ReorientationQueueEnteredCount`, `CompletedCount`, `BypassCount`
- `ReorientationSensorSequence1..8`
- `ReorientationResultAvailable`, `ResultSequence`, `ResultTriggeredMask`,
  `ResultFaultCode`

Heartbeatintervall ist 250 ms, PLC-Timeout 2 s. Die PLC besitzt einen 128-teiligen
Ring und einen Ergebnisring. Jeder der acht Sensoren fuehrt einen eigenen monotonen
Sequenzcursor. Das ungerade Signal speichert die Zeit, das folgende gerade Signal
berechnet die Geschwindigkeit und legt einen immutable Auftrag in die 16er-FIFO
des zugehoerigen Arrays. Ein Teil ist erst abgeschlossen, wenn LB8 gesehen wurde
und alle vier Finished-Bits gesetzt sind. Fehlercodes trennen Abort/Heartbeat,
Commit-/Queue-Reihenfolge, Ueberlauf, Array-FIFO, Sensorreihenfolge und Antrieb/VTEM.

Bei Heartbeatverlust bleibt ein Safe-Latch aktiv; es darf keinen automatischen
Rueckfall in die Legacy-Freigaben geben. Bei inaktivem Reorientation-Owner bleibt
die bisherige Pressure-/Conveyor-Logik unveraendert und benoetigt keine Queue.
`PressureControlGUI` und `ConveyorSetupGUI` verweigern Writes nur, solange ein
laufender Reorientation-Owner haelt. Nach einem bereits verriegelten Fail-Stop kann
Pressure Control den expliziten Legacy-Takeover ausloesen. Zusaetzlich verhindert der gemeinsame Windows-Mutex
`Local\BiBaZuPlcControl`, dass Reorientation, Pressure Control oder Conveyor Setup
parallel gestartet werden. Pressure Control nach dem Erstellen/Speichern eines
Profils immer schließen, bevor Reorientation gestartet wird. Weitere parallele ADS-Schreiber bleiben verboten. Das PLC-Programm muss
gebaut, aktiviert und auf Port 851 geladen sein; ein im Quelltext vorhandenes
Symbol beweist nicht, dass die laufende PLC bereits diesen Stand verwendet.

### Reorientation-Logging

Pro Lauf entstehen `run.json`, `result.json`, `parts.csv` sowie Kopien von Config,
Roadmap und verwendeten Profilen. Jedes uebergebene Teil erhaelt ein atomar
gespeichertes annotiertes PNG. Die CSV enthaelt unter anderem Run-/Track-/Sequenz-ID,
fuenf Konsensmessungen, Uebergabe-/ACK-/Abschlusszeit, Pose/Ziel/Pfad,
Profilpfade und SHA256, alle Array-Snapshots, Lichtadressen, Sensorcursor,
erwartete/ausgeloeste Maske und PLC-Fehlerstatus.

### Aktueller Inbetriebnahmestand 2026-08-11

- Ein programmgesteuerter Lauf der echten `MainWindow`-Klasse verband Panel 1
  (`D2:96:EB:A9:A2:86`) nach 3 s und Panel 2 (`D7:6B:0F:DC:7B:4B`) nach 6 s und
  trennte danach beide wieder sauber. Ein Reset des Bluetooth-Adapters war fuer
  diesen bestaetigten Lauf nicht erforderlich.
- Der Qt-Eventloop blieb waehrend jeder Messsekunde responsiv. Scan und GATT-
  Verbindung laufen nun ausserhalb des Qt-Hauptthreads; ein zusaetzlicher Test
  bildet einen intern synchron blockierenden WinRT-Connect nach.
- Beim Kk1a-Versuch liefen `PressureControlGUI.py` und Reorientation Control
  gleichzeitig. Die gespeicherte Kk1a-Config selbst war korrekt (`class 1 ->
  Pose 10`, Zielpose 10); die parallele Legacy-ADS-Anwendung war damit ein realer
  konkurrierender Schreiber. Der gemeinsame PLC-Mutex verhindert diesen Zustand
  nach einem Neustart beider Anwendungen. Der Roadmap-Controller besitzt
  zusaetzlich einen getesteten Zielpose-Guard mit effektiver Arraymaske null.
- Beim folgenden Startversuch blieb der Zustand in `DETECTING`, weil der sichtbare
  YOLO-Status noch `worker is still stopping` meldete, waehrend der alte
  `model_ready`-Wert den Start faelschlich erlaubte. Der Reload ist nun asynchron,
  startet automatisch neu und sperrt den Zyklus waehrend des Worker-Wechsels.
- Der erste Kk1a-Aktuierungsversuch erreichte Pose 5 und plante korrekt `5 -> 10`
  mit Maske 2, scheiterte aber beim Readback von `GuiArrayEnabled2`: Die PLC hatte
  den Wert wegen einer noch wirksamen Safe-Latch-/Heartbeat-Uebergangsphase sofort
  wieder false gesetzt. Die GUI laedt nun zuerst einen garantiert neuen Heartbeat
  und haelt Reset/Start bis zur Quittierung der PLC-Ausgangszustaende. Abort wird
  ebenfalls quittiert und danach explizit false geschrieben. Der PLC-Reset setzt
  in `MAIN.TcPOU` zusaetzlich Heartbeat-Alter/Ack/Alive deterministisch zurueck.
- Nach diesem Abbruch blieb der vorgesehene PLC-Fehler 90 verriegelt. Ein neuer
  Versuch interpretierte ihn bereits in `DETECTING` als neuen Zyklusfehler und
  brach vor der Poseentscheidung ab. Verriegelte Fehler eines vorherigen Versuchs
  werden nun waehrend Erkennung/Staging toleriert und im Ownership/Reset-Schritt
  geloescht, bevor Conveyor- oder Array-Enables geschrieben werden.
- Ein weiterer realer `AppHangB1` um 12:37 wurde nicht durch einen zweiten
  Hardwareprozess verursacht. Ursache waren noch vorhandene Lambda-Verbindungen,
  die BLE-Worker-Signale unmittelbar in Qt-Widgets weiterleiten konnten. Diese
  sowie die entsprechenden YOLO-/UR-Verbindungen wurden durch GUI-Thread-Slots
  ersetzt und mit einem echten Fremdthread-Signaltest abgesichert.
- Der danach noch ruckelnde Kamerapfad hatte zwei weitere Ursachen: ungebremste
  Qt-Preview-Queues und einen vollstaendigen Neuaufbau der Preflight-Widgets bei
  jedem Frame. Es darf nun hoechstens ein Previewframe auf Qt warten, grosse
  Bilder werden bereits im Kameraworker auf maximal 1280 x 720 reduziert, und
  unveraenderte Freshness-/Preflight-Werte erzeugen keine UI-Arbeit mehr.
- Der reale Kamera-Dauerlauf erreichte danach `14 view FPS`; nach dem Connect lag
  die groesste gemessene Qt-Timerpause bei `125 ms` statt zuvor etwa `1,8 s`.
  Ein anschliessender Gesamtlauf verband Kamera, ADS und beide Panels gleichzeitig
  und hielt die Vorschau mit `15 view FPS` ohne Hang aktiv. Die einmalige Pause
  waehrend der Hardwareinitialisierung lag bei etwa `1,2 s`.
- Die Desktop-Verknuepfung zeigt auf die aktuelle `.venv\Scripts\pythonw.exe` mit
  `-m bibazu_reorientation`; sie verwendet keine kopierte oder veraltete GUI.
- Der reale Baumer-Test mit Seriennummer `700006383255` bestaetigte
  `ExposureTime=4000 us`, den Kamerabereich `8..60000000 us`, beschreibbaren
  manuellen Modus und die exakte Apply-Quittierung. Im Test wurden ca. `105 raw`
  und `16 view FPS` gemessen; die Kamera-Node meldete `10 cam FPS`. Danach wurde
  die Kamera sauber getrennt und der Ausgangswert wiederhergestellt.
- Automated Image Capture erreicht Kamera und uebrige Hardware auf diesem PC.
  Der Reorientation-Kameraadapter wurde danach an dessen robuste Windows-/Baumer-
  Behandlung angenaehert; Kamera- und Gesamtdauerlauf wurden erfolgreich bestaetigt.
- SPS-IP `192.168.0.23` ist vom Betreiber bestaetigt. AMS bleibt
  `10.145.4.14.1.1`. Die GUI validiert nun auch `MAIN.ReorientationState`, statt
  fehlende Vertragssymbole still durch Defaultwerte zu ersetzen.
- Vollstaendiger Pose-1-/Pose-2-Zyklus, Watchdog, LB8/Drain und Fehlerabnahme sind
  noch nicht an druckbeaufschlagter Hardware freigegeben.
- `ReorientationControlGUI/Ql1i.yaml` ist als erster Roadmap-Executor-Fall
  vorbereitet: OBB-Modell `Ql1i_best.pt`, Klasse `0 -> Roadmap-Pose 2`, Klasse
  `1 -> Roadmap-Pose 3`, Zielpose 3 und eindeutige Kante
  `a7:2->3:free_z`. Das Profil nutzt `100 mm/s`, `UR Ry=18.0 deg` und Array 2
  (erwartete Maske `2`). Die irrtuemliche parallele Zuordnung desselben
  `free_z`-Profils zur `free_y`-Kante a6 wurde aus der YAML entfernt. Der reale
  pneumatische Zyklus bleibt Bediener-/Not-Aus-pflichtige Hardwareabnahme.

## 9. UR-Roboter

Aktuelle Netzwerkdaten:

- Host: `10.10.10.10`
- Primary Interface fuer TCP-Pose: Port `30002`
- RTDE fuer Winkelbefehle: Port `30004`

Die Pressure-GUI kann einen Ry/Pitch-Winkel von `15.5..21.0 deg` in
`0.1 deg`-Schritten senden; Default ist `18.0 deg`. Die RTDE-Kommunikation
verwendet:

- Input Integer Register 42: Winkel in Zehntelgrad
- Input Integer Register 43: Befehlsnummer
- Output Integer Register 41: angewendeter Winkel in Zehntelgrad
- Output Integer Register 42: bestaetigte Befehlsnummer
- Output Integer Register 43: Programmstatus

Das Programm `UR16e/BiBaZu_Continuous.urp` muss auf dem UR geladen und laufend
sein. Der teachbare Wegpunkt `Rotation_Position` legt die XYZ-Position fest. Beim
Programmstart faehrt der Roboter einmal dorthin und speichert XYZ; nachfolgende
GUI-Befehle aendern nur die fest definierte Orientierung mit variablem Ry.

Das Laden eines Pressure-Profils bewegt den Roboter absichtlich nicht.
`Apply UR Angle` muss explizit gedrueckt werden. Nur ein RTDE-Client darf die
Input-Register 42/43 gleichzeitig kontrollieren.

### Offener UR-Punkt vor dem naechsten Einsatz

Nach der Korrektur der UR-Installation wurde festgestellt, dass der feste
Sollwinkel um Z/Yaw `180 deg` sein muss. Der aktuell eingecheckte Baustein
`UR16e/BiBaZu_Continuous_Move.script` und die zugehoerige README enthalten noch
`Rz = -90 deg` sowie `Roll = -45 deg`. Vor dem naechsten realen Betrieb muss
geprueft werden, welche Festorientierung zur aktuellen Installation gehoert, das
Script gegebenenfalls auf `Rz = 180 deg` geaendert und die `.urp` mit
`Build_BiBaZu_Continuous.ps1` neu erzeugt und auf den Roboter uebertragen werden.
Diese Abweichung ist derzeit dokumentiert, aber noch nicht im Code korrigiert.

## 10. Profile

Profile liegen als JSON in `CSVSaver/pressure_profiles`. Aktuelle Version ist 8;
Versionen 1 bis 8 werden weiterhin geladen. Das aktuelle Format speichert:

- Erstellzeit und Profilversion
- UR-Ry-Sollwinkel
- globale Lichtschranken-Entprellzeit
- sechs Invertierungsflags
- sechs Debounce-Enable-Flags
- Conveyor Enable, Reverse und Geschwindigkeit; der weiterhin gespeicherte
  Maximalwert ist fest auf `1000.0 mm/s` gesetzt
- Conveyor-Markierungsabstand, `mm/Vollschritt` und Gueltigkeit
- Kraftantwortzeit je Array fuer eine Duese und fuer vier oder mehr Duesen
- je Array: Enable, sechs Duesen-Flags, Druck, manueller Delay, Pulsdauer und
  Offset

Temporare Markierungspositionen der Bandkalibrierung werden nicht gespeichert.
Alte Profile ohne Kalibrierfaktor werden als unkalibriert behandelt. Sehr alte
Profile mit zwei Eintraegen pro Array werden beim Laden auf das aktuelle
Sechs-Duesen-Format migriert.

Wichtig: Ein Profil kann aktuelle SPS-Defaults, Sensorabstaende,
Invertierungs-/Debounce-Zustaende und Kalibrierwerte ueberschreiben. Nach dem
Laden deshalb die sichtbaren Werte pruefen. Der UR bewegt sich erst nach dem
separaten Apply-Befehl.

## 11. Logging

### `pressure_log.csv`

Ein Eintrag pro neuem SPS-`ShotCounter` mit lokalem Zeitstempel, den zwei
gemittelten Analogeingangswerten und den vier Array-Geschwindigkeiten.

### `light_barrier_events.csv`

Pro vom GUI-Polling beobachteter Aenderung: lokaler Zeitstempel,
SPS-Ereigniszeit, Lichtschranke, gefilterter Zustand, Rohzustand beim Poll,
Ereigniszaehler, eventuell zwischen Polls verpasste Einzelereignisse,
Foerderbandposition, Paarlaufzeit, Geschwindigkeit und Gueltigkeit.

Die eigentliche SPS-Flanke ist genauer als der lokale CSV-Zeitstempel. Wenn
mehrere Ereignisse zwischen zwei GUI-Polls auftreten, zeigt
`events_not_individually_logged` dies an.

### `force_peak_delay_log.csv`

Zeitstempel, Array, Lichtschranke, Kraftsensor, Fenster, Baseline, Peak,
Peakanstieg, Delay, Gueltigkeit und Ablehnungsgrund fuer jede abgeschlossene
Kraftdelaymessung.

### `ur_speed_plausibility.csv`

Einzelne UR-Passagen mit Sollgeschwindigkeit, Sensorpaar, Richtung, Laufzeit,
gemessener Geschwindigkeit und Abweichung.

### Reorientation-Runexport

Standardpfad: `%LOCALAPPDATA%\BiBaZuReorientationControl\runs`. Jeder Versuch
erhaelt eine Cycle-ID, ein atomar gespeichertes PNG des Entscheidungsframes, eine
CSV-Ergebniszeile sowie Kopien von YAML und Pressure-Profil. Pfade und SHA256
halten den verwendeten Stand nachvollziehbar. Erfolg, manueller Abbruch,
PLC-/ADS-/Loggingfehler und Watchdogzustand werden als terminales Ergebnis
protokolliert. Diagnosemeldungen rotieren getrennt unter
`%LOCALAPPDATA%\BiBaZuReorientationControl\logs`.

## 12. Einrichtung auf einem neuen PC

### 12.1 Software

1. Das komplette Repository klonen oder kopieren, einschliesslich
   `pressure_profiles`, `UR16e` und `vendor_ur_rtde`.
2. TwinCAT XAE in einer mit dem Projekt kompatiblen Version installieren. Das
   aktuelle PLC-Objekt nennt TwinCAT `3.1.4026.18`.
3. Die im Projekt benoetigte Festo-Bibliothek `FestoVTEMdc` installieren.
4. Python installieren. Die Legacy-GUIs liefen bisher mit Python 3.14.6. Die neue
   Reorientation-Anwendung verlangt reproduzierbar Python `>=3.12,<3.13`.
5. Legacy-Abhaengigkeiten installieren:

```powershell
python -m pip install PyQt6==6.11.0 pyads==3.6.0
```

Die vendorte UR-RTDE-Bibliothek wird direkt aus dem Repository importiert.

6. Reorientation Control mit `uv` einrichten:

```powershell
cd ReorientationControlGUI
uv sync --extra dev
uv run bibazu-reorientation
```

Das eigene `pyproject.toml` enthaelt PyQt6/qasync, pyads, Harvester/GenICam,
Bleak/neewerlite, NumPy/OpenCV, PyYAML, Ultralytics sowie gepinnte
Torch/Torchvision-CUDA-11.8-Pakete.

7. Fuer Bediener ohne Terminal im Repository
   `WindowsLaunchers\Verknuepfungen-installieren.cmd` doppelklicken. Dadurch
   entstehen Desktop- und Startmenueeintraege mit eigenen Icons fuer Reorientation
   Control, Pressure Control, Conveyor Setup und Automated Image Capture. Die
   Links starten `pythonw.exe` direkt aus der jeweiligen `.venv` und verwenden
   nach einem Quellcodeupdate automatisch den aktuellen Stand. Nach Verschieben
   des Workspace den Installer erneut ausfuehren.

### 12.2 Netzwerk und ADS

1. Netzwerkzugriff auf die SPS unter `192.168.0.23` herstellen.
2. Eine funktionierende ADS-Route zur AMS Net ID `10.145.4.14.1.1` einrichten.
3. Netzwerkzugriff auf den UR unter `10.10.10.10` herstellen.
4. Ports 30002 und 30004 zum UR pruefen.
5. Falls IPs oder AMS Net ID geaendert werden, die Konstanten in
   `PressureControlGUI.py` sowie `UR_HOST` in den UR-Hilfsmodulen anpassen.

### 12.3 TwinCAT

1. `CSVSaver/TwinCAT Projekt3 - Kopie/TwinCAT Projekt3/TwinCAT Projekt3.tsproj`
   oeffnen.
2. Hardware scannen bzw. die vorhandene Konfiguration mit dem realen Aufbau
   vergleichen.
3. Alle I/O-Verknuepfungen kontrollieren, insbesondere Term 16, Term 18, Term 19
   EL7047 und `OpenValve17..24`.
4. Beim EL7047 Positioning Interface, interne Rueckfuehrung, Motorvollschritte,
   Speed Range und Motorstrom pruefen.
5. PLC-Projekt bauen, aktivieren und herunterladen.
6. Taskzeit `1 ms` beibehalten, solange Timingberechnungen in Zyklen arbeiten.
7. Online zuerst WcState, STM/POS Warning/Error, Ready to execute und interne
   Position pruefen.

### 12.4 Python-Test und Start

Im Verzeichnis `CSVSaver`:

```powershell
python -m unittest test_pressure_control_gui.py
python ConveyorSetupGUI.py
python PressureControlGUI.py
```

Zuletzt liefen 54 Pressure-GUI-Unit-Tests erfolgreich. Hardwaretests sind davon getrennt und
muessen nach jedem Umzug erneut durchgefuehrt werden.

Im Verzeichnis `ReorientationControlGUI`:

```powershell
uv run ruff check src tests
uv run pytest
```

Aktueller Stand: Ruff ohne Befund und `108` bestandene Reorientation-Tests. Diese
decken YAML/relative Pfade, Zielpose 1/2, Profilversionen 1..9, Legacy-Migration,
Detect/OBB, Fuenf-Frame-Mehrteiltracking, Controller/Queue-/Readback-Reihenfolge, STL/OBJ-Preview,
Settings, Df1a-YAML/JSON-Normalisierung, Roadmap-Validierung, v2-Roundtrip,
Hash-Neuuebernahme, Graph-Readiness, sechs Df1a-Profilzeilen, eindeutige direkte
und zweistufige Roadmap-Pfade, Mehrprofil-Parametervergleich/-Komposition sowie
die animierte Uebergangsvorschau mit
Bild-Fallback ab. Zusaetzlich sind SPS-IP-Migration, BLE-Auswahl, Timeout-Cleanup,
Connect-Cancel, serielle Doppelverbindung, Connect-Timeout, Isolation eines
blockierenden WinRT-Aufrufs, Exposure-/FPS-UI, Langzeit-Exposure-Fetch-Timeout und
Qt-Thread-Affinität der Worker-Signale, asynchronen YOLO-Reload sowie sichere
manuelle Bandfreigabe/-abschaltung und `Disconnect all components`
abgedeckt. Sie ersetzen keine Hardwareabnahme.

## 13. Empfohlene Wiederinbetriebnahme

1. Anlage drucklos und Motorleistung aus: Verdrahtung aller Ventile,
   Lichtschranken, Analogeingaenge und Motorwicklungen kontrollieren.
2. TwinCAT in Run bringen und EtherCAT/EL7047-Status online pruefen.
3. Motorstrom konservativ einstellen und Temperatur beobachten.
4. Mit sehr kleinen relativen Schrittzahlen und niedriger Joggeschwindigkeit
   beide Richtungen pruefen.
5. Foerderband ueber die 315-mm-Markierungen neu validieren; bei Bedarf
   kalibrieren.
6. Alle acht Lichtschranken in der Setup-GUI pruefen, Invertierung und
   Entprellung bestaetigen.
7. Sensorabstaende mit mehreren UR-Passagen in beide Richtungen validieren.
8. Geschwindigkeitsplausibilitaet zuerst bei konstanter, langsamer Bewegung und
   danach bei mehreren Sollgeschwindigkeiten pruefen.
9. Force-Delay-Einstellungen fuer alle vier Arrays und Profil-Roundtrip pruefen.
10. Erst danach Druck, Pulsdauer, aktive Duesenzahl und Geschwindigkeit schrittweise
    erhoehen.
11. Aktualisiertes `MAIN.TcPOU` bauen, aktivieren und online alle
    `Reorientation*`-Symbole auf Port 851 kontrollieren.
12. Reorientation zunaechst drucklos verbinden: Kamera-Livebild, YOLO-Warm-up,
    beide Panels, PLC-Vertrag und Preflight pruefen.
13. Pose-Pass-through mit erwarteter Maske null pruefen; kein Ventil darf pulsen.
14. Danach genau einen Uebergang mit konservativem Profil und physischem Not-Aus
    testen. LB8, Trigger-Maske, Drain, Ventil-idle und CycleCounter online pruefen.
15. Abschliessend Heartbeatverlust, ADS-Abbruch, manuellen Stop, blockierte LB8
    und fehlenden Trigger kontrolliert abnehmen. Kein Fall darf automatisch
    wieder anlaufen.

## 14. Sicherheit und bekannte Grenzen

- GUI-Stop, ADS-Stop und EL7047 Emergency Stop sind keine
  sicherheitsgerichteten Funktionen. Ein unabhaengiger physischer Not-Aus bleibt
  erforderlich.
- Beim Arbeiten an Motorleitungen, Ventilen oder Ausgaengen Leistung und Druck
  sicher abschalten.
- Der encoderlose Schrittmotor meldet befohlene Position, nicht garantierte
  Bandbewegung. Schlupf, verlorene Schritte und elastische Effekte bleiben
  unerkannt.
- Die Lichtschrankenzeit hat 1-ms-SPS-Aufloesung und keine EL1018-Hardware-
  Timestamps. Sehr kurze Sensorlaufzeiten haben entsprechend groesseren
  relativen Fehler.
- Array 3 nutzt die Geschwindigkeitsmessung LB5-6 und den Trigger LB6; Array 4
  nutzt getrennt LB7-8 und den Trigger LB8.
- Die Kraftantwortinterpolation endet derzeit bei vier aktiven Duesen; vier,
  fuenf und sechs verwenden denselben Endwert.
- Lichtschranken-Debouncing verschiebt den Arraytrigger; bei einer Aenderung der
  Entprellung muss deshalb der raeumliche Offset erneut geprueft werden.
- Sensorabstaende sind effektive Schaltpunktabstaende, nicht zwingend der mit
  einem Lineal gemessene Gehaeuseabstand.
- Teachzustand, Objektfarbe, Einfallsrichtung, Stabgeometrie und Sensorhysterese
  koennen Vorwaerts-/Rueckwaertswerte verschieben.
- Ein laufender ADS-Aufruf kann einen GUI-Stop bis zum 500-ms-Timeout verzogern.
- Relative Dateipfade wie `pressure_profiles` und `pressure_log.csv` setzen
  voraus, dass die GUI aus `CSVSaver` gestartet wird.
- Reorientation-`Completed` bestaetigt PLC-Transport und erwartete Trigger, aber
  keine kamerabestaetigte Endorientierung.
- Kamera und BLE-Panels duerfen nicht gleichzeitig von Automated Image Capture
  und Reorientation Control kontrolliert werden.
- PLC-Watchdog und GUI-Stopp sind nicht sicherheitsgerichtet. Physischer Not-Aus
  und pneumatische Druckentlastung bleiben zwingend.
- Der historische `ShotCounter` zaehlt nur abgeschlossene Array-1-Impulse und ist
  kein gueltiger Gesamtabschluss; Reorientation verwendet den neuen PLC-Vertrag.

## 15. Offene bzw. naechste sinnvolle Arbeiten

1. UR-Festorientierung fuer die aktuelle Installation auf `Rz = 180 deg`
   verifizieren und URP neu bauen/deployen.
2. Reine pneumatische Antwortzeit optional als `Ventilausgang EIN -> Kraftpeak`
   messen, getrennt vom geometrischen Lichtschranken-/Duesendelay.
3. Kraftsensoren bei Bedarf von 0-10-Skala nach Newton kalibrieren.
4. Kraftantwort-Endpunkt fuer fuenf und sechs Duesen separat untersuchen, falls
   sich diese deutlich von vier Duesen unterscheiden.
5. Sensorabstaende und Force-Delay-Werte nach jedem mechanischen Umbau neu
   validieren.
6. Fuer die beiden Legacy-GUIs ebenfalls ein Dependency-Manifest ergaenzen. Die
   neue Reorientation-GUI besitzt bereits ein eigenes `pyproject.toml`/`uv.lock`.
7. Grosse Mess-CSV-Dateien aus dem normalen Source-Control-Workflow auslagern,
   falls das Repository weiter stark waechst.
8. Veraltete `Term 14`-Kommentare in `MAIN.TcPOU` auf `Term 19` korrigieren.
9. Reorientation-Hardwareabnahme gemaess Abschnitt 13 vollstaendig durchfuehren
   und Ergebnisse/PLC-Buildstand dokumentieren.
10. Spaetere `sequence_yaml`-Anbindung, mehr als zwei Posen, gerichteten
    Mehrkanten-Resolver und kamerabasierte Nachkontrolle implementieren.
11. PLC-seitigen Cycle-Snapshot aller live verwendeten Profilparameter ergaenzen
    bzw. online verifizieren, damit Fremdwrites nach Arm den laufenden Impuls
    nicht veraendern koennen.

## 16. Entwicklungsregeln fuer weitere Aenderungen

- Zeitkritische Erfassung und Aktorsteuerung bleibt in der 1-ms-SPS-Task.
- Die GUI darf keine `pyads.Connection` im Qt-Hauptthread verwenden.
- Neue ADS-Livewerte nach Moeglichkeit in vorhandene Sum-Reads aufnehmen.
- Normale Zahlenfeld-Writes debouncen; Stop- und Bewegungsbefehle priorisieren.
- Bei ADS-Reconnect zuerst einen sicheren Stopzustand schreiben.
- Neue Profilfelder erfordern eine neue Profilversion und rueckwaertskompatibles
  Laden alter Versionen.
- I/O-Erweiterungen immer gleichzeitig in Hardwareprojekt, PLC-Symbolen,
  Zustandsautomaten, GUI und Tests abbilden.
- In Kalibrier-/Plausibilitaetsmodi muessen normale Ventiltrigger gesperrt und
  alle Duesenausgaenge aus sein.
- Neue Timingkorrekturen mit mehreren Geschwindigkeiten und beiden
  Bewegungsrichtungen pruefen, nicht nur mit einem Einzelversuch.
- Nach PLC-Aenderungen TwinCAT bauen/downloaden und PDO-Verknuepfungen online
  kontrollieren; Python-Tests allein pruefen die Hardware nicht.

## 17. Kurzreferenz

```text
Pressure-GUI:       python PressureControlGUI.py
Setup-GUI:          python ConveyorSetupGUI.py
Reorientation:      cd ReorientationControlGUI; uv run bibazu-reorientation
Legacy-Tests:       python -m unittest test_pressure_control_gui.py (54)
Reorientation-Test: uv run pytest (108), uv run ruff check src tests
Shortcuts:          WindowsLaunchers\Verknuepfungen-installieren.cmd
PLC:                192.168.0.23 / AMS 10.145.4.14.1.1 / Port 851
UR:                 10.10.10.10 / TCP 30002 / RTDE 30004
PLC-Zyklus:         1 ms
Arrays/Duesen:      4 / 6
Sensorabstaende:    40.0 mm, 40.0 mm, 40.0 mm, 40.0 mm
Bandkalibrierung:   0.32960026 mm/Vollschritt, 64 Inkremente/Vollschritt
Force-Delay:        2000-ms-Fenster, 0.05 Mindestanstieg
Profile:            JSON Version 9, alte Versionen 1-8 ladbar
```
