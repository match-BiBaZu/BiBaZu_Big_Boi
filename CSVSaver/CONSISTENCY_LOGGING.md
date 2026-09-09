# Consistency: Ereigniserfassung und Rohdatenexport

## Live-Signalplot

Die Haupt-GUI **Nozzle Array Pressure Control** zeigt dieselben vier
Lichtschranken-Plots ausschließlich im Dialog **Light Barrier Settings**
rechts neben den Signaloptionen. **Online Settings** enthält nur die Statustabelle.
Diese Ansicht aktualisiert sich nur bei LB8-Aktivierung (1 → 0): Während
des nächsten Durchlaufs bleibt der vorherige vollständige Plot sichtbar.
Auch bei geschlossenem Dialog werden abgeschlossene Durchläufe erfasst;
beim erneuten Öffnen steht der letzte Plot weiterhin zur Verfügung.
Die Druck-GUI zeigt als durchgezogene Kurven die tatsächlichen SPS-Ausgänge
`PairedFirstBarrierFalling[1..4]` und `PairedSecondBarrierFalling[1..4]`,
die auch für die Geschwindigkeitsmessung und automatische Düsen-Triggerlogik
verwendet werden. Das GUI-Filterkästchen wurde entfernt; ein alter gespeicherter
Wert `light_barrier_single_part` wird ignoriert.

Die durchgezogenen Kurven zeigen **1 = akzeptierte fallende Sensorflanke**, **0 = kein Trigger**.
Es handelt sich um die tatsächlichen Ein-Zyklus-Impulse des Paarfilters, nicht
um den optischen 0/1-Pegel, eine nachträgliche GUI-Filterung oder die verzögerte
Ventilöffnung. LB1 startet die Darstellung, LB8 friert den abgeschlossenen Plot ein.

Gestrichelte Kurven in derselben Lichtschrankenfarbe überlagern die aufgezeichneten
Sensorpegel aus `MAIN.LightBarrierEventHistory`: nach Invertierung und Entprellung,
aber vor dem Paarfilter. So bleiben auch vom Paarfilter unterdrückte Flanken sichtbar.
Beide Ebenen verwenden dieselben SPS-Zeitstempel und denselben LB1–LB8-Ausschnitt.
Bei zeitversetzten ADS-Abfragen wartet die Ansicht auf den vollständigen Sensorpuffer;
anschließend bleiben beide Ebenen bis zum nächsten abgeschlossenen Durchlauf stehen.
Fehlen die Sensordaten für den Durchlauf, werden nur die Trigger mit einem Hinweis
angezeigt. Die gestrichelten Kurven sind keine elektrischen Rohpegel vor der Entprellung.

`MAIN.LightBarrierFilteredEventHistory` puffert hierfür 256 Zustandswechsel
aller acht Triggerausgänge einschließlich ihrer Rückkehr auf FALSE. Das Layout
entspricht dem stabilen Ereignispuffer (Version, Sequenz, SPS-Zeit, dann 256
Dreiergruppen); das niederwertige Codebit enthält hier den Triggerausgang.
Die GUI liest ihn separat als einen ADS-Wert in der normalen Statusabfrage
und in der Setup-Abfrage. Sequenzen erkennen verlorene oder inkonsistente Daten.
Druckstatus und Logging werden weiterhin verarbeitet.

**Voraussetzung:** Die ergänzte `MAIN.TcPOU` muss in TwinCAT gebaut und in die
SPS geladen werden. Ohne das neue Symbol zeigt der Plot „PLC trigger feed
unavailable“, statt aus Rohsignalen scheinbar tatsächliche Trigger abzuleiten.
Diese Änderung wurde hier weder auf die Anlage geladen noch mit TwinCAT gebaut.
Der Conveyor-Setup-Plot behält den unten beschriebenen Filter für den
gesamten LB1–LB8-Durchlauf.

**Save Changes** im Dialog **Light Barrier Settings** speichert direkt und ohne
Profilauswahl in `CSVSaver/light_barrier_settings.json`. Diese globale Datei enthält
die vier Paarabstände, vier Sensor-zu-Array-Abstände, acht Invertierungen,
acht Entprellungsoptionen und die Entprellungszeit.
Eine Bestätigung erscheint im Dialog. Die Datei wird über eine temporäre Datei
ersetzt; ein fehlgeschlagener Speichervorgang lässt die bisherige Datei bestehen.

Die Druck-GUI lädt die globale Datei beim Start und stellt die gespeicherten
Maschinenwerte nach dem ersten SPS-Snapshot sowie bei Wiederverbindung wieder her.
Ohne globale Datei gelten weiterhin die aktuellen SPS-Werte. Ungültige Dateien
werden mit einer Fehlermeldung abgewiesen, bevor Einstellungen übernommen werden.
**Load Profile** verändert keine Lichtschrankeneinstellungen mehr, auch bei alten
Profilen. Die entsprechenden Profilfelder bleiben nur als dokumentierter
Maschinenzustand erhalten. Die getrennte Conveyor-Setup-GUI ist davon nicht betroffen.

Rechts neben der Tabelle zeigt ein in der Breite verstellbarer Plot die acht
Lichtschrankensignale auf Basis der stabilen Zustände nach Invertierung und
Entprellung. Die Paare
LB1–2, LB3–4, LB5–6 und LB7–8 besitzen jeweils eine eigene, gestapelte
0/1-Achse mit gemeinsamer Zeitachse in Sekunden. Jede Lichtschranke hat eine
eigene Farbe; deckungsgleiche Signale sind zur Sichtbarkeit leicht versetzt.

Mit aktiviertem **One part at a time** zeigt der Plot gespeicherte Pegel:

- Die erste Aktivierung von LB1 (1 → 0) beginnt einen Durchlauf, ersetzt
  den vorherigen Plot und setzt die Zeit auf null.
- Jede Lichtschranke wird bei ihrer ersten Aktivierung auf 0 gesetzt und
  dort gehalten. Weitere Flanken dieser Schranke werden bis zum Ende des
  Durchlaufs ignoriert, auch außerhalb des Wiederholungsfensters. Weitere
  LB1-Impulse starten währenddessen keinen neuen Plot.
- Nach Aktivierung von LB8 beendet deren Rückkehr auf 1 den Durchlauf.
  Alle acht gespeicherten Pegel werden gleichzeitig auf 1 zurückgesetzt.
  Die Plotansicht friert bereits bei der ersten LB8-Aktivierung (1 → 0) ein:
  Zeitachse und sichtbare Kurven bleiben bis zur ersten LB1-Aktivierung des
  nächsten Teils unverändert. Die LB8-Freigabe und der Pegelreset werden im
  Hintergrund weiterhin verarbeitet.
- Ohne LB8-Freigabe setzt **Maximum traversal time** die gehaltenen Pegel
  zurück; der Plot wartet danach auf eine neue LB1-Aktivierung.

Ohne **One part at a time** zeigt der Plot die tatsächlichen stabilen Pegel.
Jede fallende LB1-Flanke ersetzt dann den Plot und setzt die Zeit auf null.
Auch hier friert die Ansicht bei LB8 (1 → 0) bis zur nächsten LB1-Flanke ein.
Ein Wechsel des Modus beginnt eine neue Erfassung. Im Einzelteilmodus verwenden
nun auch Tabellenauswertung und CSV-Export die erste Aktivierung (1 → 0) jeder
Schranke. Eine Tabellenzeile wird erst nach der anschließenden LB8-Freigabe
ausgegeben. Die Plotdarstellung wurde bei dieser Korrektur nicht verändert.

Der Plot läuft bei bestehender ADS-Verbindung unabhängig vom Logging.
Mit SPS-Ereignispuffer werden auch kurze Impulse zwischen GUI-Abfragen
nachgezeichnet. Ohne Puffer werden die abgefragten Zustände dargestellt;
kurze Impulse können dann fehlen. Verbindungsverlust pausiert den Plot,
Wiederverbindung oder ein erkannter Puffer-/Zeitverlust startet ihn neu.
Pro Plot werden höchstens 20.000 Zustandsänderungen gespeichert; bei
Erreichen der Grenze bleiben die neuesten Änderungen sichtbar.

## Ursache der Meldung

`LightBarrierEventCount1..8` zählt jede akzeptierte stabile Zustandsänderung,
also ON und OFF. `LightBarrierLastEventTimeMs1..8` enthält jeweils nur den
letzten Zeitstempel. Der Setup-Poll läuft nominell alle 50 ms; kurze Pulse
oder längere ADS-Abfragen können mehrere Flanken zwischen zwei Abfragen erzeugen.
Die bisherige Auswertung hat bei jedem Zählersprung größer als eins alle
unvollständigen Durchläufe verworfen, auch wenn nur die nicht ausgewählte
Gegenflanke fehlte.

Die vorhandene `light_barrier_events.csv` enthält für den 08.09.2026 bei der
Untersuchung 10.197 Ereigniszeilen, davon 5.392 mit mehreren Flanken seit der
letzten Abfrage. Diese Datei stammt aus dem separaten Ereignislogger; sie
misst nicht die tatsächlichen Poll-Abstände des Consistency-Reiters.

## Änderungen

- Mit dem bisherigen SPS-Stand ist ein Zählersprung von zwei verwertbar,
  wenn die letzte Flanke der ausgewählten Flanke entspricht: Die einzige
  überschriebene Flanke war dann die Gegenflanke. Fehlende ausgewählte
  Zeitstempel werden weiterhin gemeldet, einschließlich der betroffenen
  Lichtschranken. Aus einer mehrdeutigen Abfrage wird kein neuer Durchlauf
  begonnen. Ereignisse werden nach SPS-Zeit verarbeitet.
- `MAIN.LightBarrierEventHistory` puffert im aktualisierten SPS-Quellcode
  die letzten 256 Flanken aller acht Lichtschranken. Die GUI liest den Puffer
  als einen ADS-Wert und verarbeitet seit der letzten Abfrage hinzugekommene
  Ereignisse in Reihenfolge. ON- und OFF-Zeitstempel bleiben erhalten.
- Pufferüberlauf oder Zählerrücksetzung verwirft unvollständige Durchläufe;
  anschließend beginnt die Erfassung mit einem neuen LB1-Ereignis.
  Sequenznummern verhindern doppelte Verarbeitung und erkennen unpassende
  Puffereinträge. 32-Bit-Zähler- und Zeitüberläufe werden berücksichtigt.
- Fehlt das neue SPS-Symbol, verwendet die GUI automatisch die bisherige
  Schnittstelle. Der Status beim Start zeigt an, welcher Modus verwendet wird.

Der Puffer benötigt die Übernahme der geänderten `MAIN.TcPOU` in die laufende
SPS: TwinCAT-Projekt bauen und die Änderung regulär laden. Diese Änderung
wurde hier weder auf die SPS geladen noch an der Anlage getestet. Ohne
SPS-Update können weiterhin ausgewählte Zeitstempel verloren gehen; diese
lassen sich aus dem letzten Zeitstempel allein nicht rekonstruieren.
Auch mit Puffer sind mehr als 256 neue Flanken zwischen zwei Abfragen nicht
vollständig nachholbar.

## Export

Im Consistency-Reiter speichert **Export Raw CSV...** alle aktuell in der
Tabelle enthaltenen Durchläufe, einschließlich CHECK-Zeilen. Der Export ist
auch nach Stop oder Verbindungsverlust möglich. Bereits aus der Tabelle
gelöschte Messungen werden nicht aus der automatischen Logdatei nachgeladen.

Die CSV enthält genau 15 Spalten mit Kopfzeile:

1. `plc_time_lb1_ms` bis `plc_time_lb8_ms`: ursprüngliche SPS-Zeitstempel
   in Millisekunden, keine PC-Uhrzeit; Überlauf nach 2^32 ms.
2. `speed_1_2_mm_per_sec` bis `speed_7_8_mm_per_sec`: sieben berechnete
   Abschnittsgeschwindigkeiten in mm/s mit voller gespeicherter Genauigkeit.

Trennzeichen ist ein Komma, Dezimalzeichen ein Punkt. Max dV, Diagnose,
Teilenummer und lokale Uhrzeit sind nicht Teil des Rohdatenexports.

## Einzelteilmessung und Mehrfachimpulse

Die Untersuchung von `light_barrier_raw_20260908_190249.csv` hat einen
zusätzlichen Zuordnungsfehler gezeigt: Bei mehreren ausgewählten Flanken
desselben Teils eröffnete der Collector mehrere Durchläufe. Nachfolgende
Lichtschranken wurden jeweils dem ältesten noch passenden Durchlauf zugeordnet.
Dadurch wurden Zeitstempel verschiedener physischer Teile vermischt. Der
Ereignispuffer verhindert verlorene Flanken, kann diese Zuordnung aber nicht
allein lösen. Die Geschwindigkeitsberechnung aus Abstand und Zeitdifferenz
war korrekt.

Für die bestätigte Betriebsweise mit jeweils nur einem Teil zwischen LB1
und LB8 ist jetzt **One part at a time** standardmäßig aktiviert:

- Nur ein Durchlauf kann gleichzeitig offen sein. Die erste ausgewählte
  **Aktivierung (1 → 0)** jeder Lichtschranke liefert den Zeitstempel.
  Die Flankenauswahl ist in diesem Modus auf Aktivierung festgelegt.
- Weitere Aktivierungen bereits erfasster Schranken, einschließlich LB1,
  werden für den gesamten Durchlauf ignoriert, unabhängig von ihrem Abstand.
  Das bisherige 0,5-s-Wiederholungsfenster entfällt.
- Erst nach Aktivierung von LB8 und deren Rückkehr auf frei (0 → 1) wird
  der vollständige Durchlauf in Tabelle und CSV übernommen. Danach können
  alle Schranken wieder einen neuen Durchlauf erfassen. Der gespeicherte
  LB8-Zeitstempel bleibt dabei der Zeitpunkt seiner ersten Aktivierung.
- Übersprungene Sensoren und ungültige Zeitstempelfolgen verwerfen den
  Datensatz. Bis zur beobachteten LB8-Aktivierung und Freigabe bleibt die
  Erfassung gesperrt, damit Ereignisse des nächsten Teils nicht einen
  unvollständigen alten Datensatz ergänzen. Stop/Start setzt die Erfassung
  explizit zurück. Zeigt der Plot dabei schon ein laufendes Teil, wartet
  die Tabellenaufzeichnung zunächst dessen LB8-Freigabe ab.
- **Maximum traversal time** (Vorgabe 10 s ab LB1) begrenzt die Gesamtdauer
  einschließlich Wartezeit ohne neue Flanken. Der Grenzwert ist bei langsamen
  Messungen entsprechend zu erhöhen. Nach Zeitüberschreitung bleibt der
  verworfene Durchlauf für die Tabelle bis zur LB8-Freigabe gesperrt; es wird
  kein unvollständiger Datensatz veröffentlicht.
- Die GUI zeigt die Anzahl ignorierter Wiederholungen und verworfener
  Durchläufe an. Beide Zähler beginnen bei jedem Logging-Start neu.

Bei gleichzeitig mehreren Teilen ist der Einzelteilmodus ungeeignet; der
bisherige Modus bleibt durch Abwählen verfügbar und setzt einen ausgewählten
Impuls pro Teil und Sensor voraus. Dort ist die Auswahl zwischen Aktivierung
und Freigabe weiterhin möglich. Ein bereits fehlerhaftes erstes Sensorsignal
wird durch das Halten allein nicht korrigiert.

## LB3–4 im Export von 19:43:46

Die 120 Zeilen aus `light_barrier_raw_20260908_194346.csv` stimmen mit
120 automatisch gespeicherten ON-Messungen in `light_barrier_consistency.csv`
überein. In der SPS bedeutet der normalisierte Zustand TRUE/ON **frei**,
FALSE/OFF dagegen **Teil vorhanden**. Die Tabelle hatte daher Freigaben
aufgezeichnet, während der korrekte Plot Aktivierungen hielt.

Bei LB3–4 beträgt die Zeitdifferenz im Median 58 ms, in 19 Zeilen aber nur
9–19 ms. Beispiel Zeile 2: 2.495.123 − 2.495.104 = 19 ms; daraus ergeben sich
bei 40 mm rechnerisch korrekt 2.105,26 mm/s. Bei Mehrfachimpulsen kann eine
vorzeitige erste Freigabe einen anderen Zeitpunkt bezeichnen als die
gewünschte erste Aktivierung. Die konkrete Signalursache dieser alten
Ausreißer ist aus den acht ausgewählten Zeitstempeln nicht rekonstruierbar.
Der separate Ereignislog endet vor dieser Messung; vollständige Flanken
liegen dafür nicht vor. Die Zahlen werden daher nicht nachträglich verändert.

Diese Korrektur betrifft die GUI und benötigt kein weiteres SPS-Update.
Nach Neustart der GUI eine neue Messung starten. Bereits falsch zugeordnete
CSV-Zeilen werden nicht nachträglich korrigiert: Der Export enthält nicht
alle ursprünglichen Ereignisse, insbesondere fehlen spätere LB1–LB4-Flanken.
Die physische Ursache der Mehrfachimpulse (z. B. Signalflattern oder mehrfache
Unterbrechung durch ein Teil) ist aus dieser Datei nicht eindeutig bestimmbar.

## Prüfung

Aus `CSVSaver`:

```powershell
& ../.venv/Scripts/python.exe -B -m unittest test_consistency_logging test_consistency_single_part test_pressure_control_gui test_high_speed_calibration test_profile_pose_context test_pose_preview_axes test_light_barrier_plot
```

149 Tests erfolgreich, darunter Aktivierung statt Freigabe bei unterschiedlichen
LB3/LB4-Pulsbreiten, lange Mehrfachimpulse, Sperre bis zur LB8-Freigabe,
Start während eines bereits im Plot gehaltenen Teils, Fehler-/Timeoutsperre,
GUI-Moduswahl und Diagnosezähler sowie beide Flankenrichtungen im Mehrteilmodus, mehrere überlappende
Teile zwischen Abfragen, Puffergrenze/Überlauf, Zählerrücksetzung, Zeit- und
Zählerüberlauf, inkonsistente Einträge sowie CSV-Inhalt, Abbruch und Dateifehler.
Die SPS-XML-Struktur ist parsebar; ein TwinCAT-Build steht noch aus.
