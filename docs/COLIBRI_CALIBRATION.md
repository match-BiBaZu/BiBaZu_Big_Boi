# Colibri-Kalibrierung von Lichtschranken und Düsen

Die Conveyor-Setup-GUI enthält zwei unabhängig startbare Abläufe mit jeweils
fünf Hin-/Rückzyklen. Die gemeinsame Gerätebedienung erscheint beim Auswählen
eines der beiden Colibri-Tabs.

## Vorbereitung

1. Projektabhängigkeiten installieren; neu ist `pyserial`:

   ```powershell
   uv pip install --python .venv\Scripts\python.exe -r requirements.txt
   ```

2. Den geänderten `MAIN.TcPOU` im TwinCAT-Projekt bauen und den neuen SPS-Stand
   einschließlich Symbolen auf die Steuerung laden. Die GUI benötigt
   `MAIN.ProbeCalibrationInterfaceVersion = 2` für die Düsenmessung mit Luft ab
   Home. Lichtschrankenmessung und manuelle Bedienung unterstützen weiterhin
   Version 1; eine Düsenserie wird damit vor der ersten Bewegung gesperrt.
   Diese Softwareänderung allein
   aktiviert oder lädt keine SPS-Konfiguration.
3. Den Colibri-USB/RS485-Port auswählen. Die übernommene BAC-Anbindung verwendet
   9600 Baud, Slave `0xFF`, Referenzfahrt negativ mit 20 % Referenzstrom und
   zunächst `0,005 mm/Schritt`. Die bisherige Home-Referenzfahrt bleibt erhalten.
   **Hinfahrt ab Home** ist in jedem Mess-Tab zwischen positiven und negativen
   Achspositionen umschaltbar. Die Rückfahrt führt wieder zu 0 mm. Die Auswahl
   wird gespeichert; die Home-Referenzrichtung wird dadurch nicht verändert.
   Der Achsmaßstab ist in der Gerätebedienung einstellbar. Negative Encoderwerte
   sind zulässig: Positionsüberwachung und manuelle Zielpositionen erlauben
   −75 bis +75 mm (Überwachung mit 0,1 mm Toleranz). Das ist ein Koordinatenbereich,
   kein Nachweis eines mechanischen Hubs von 150 mm. Der maximale Verfahrweg
   einer Kalibrierung bleibt 75 mm ab Home; Home muss weiterhin bei 0 mm enden.
4. **Colibri verbinden** prüft auch die neue SPS-Schnittstelle. Fehlende Symbole
   sperren ausschließlich die neuen Colibri-Funktionen; die anderen Tabs bleiben
   nutzbar. Ein fehlgeschlagener Verbindungsversuch gibt den COM-Port wieder frei.
5. **QuantumX verbinden** nutzt NDJSON auf `127.0.0.1:5500`. Läuft dort noch kein
   Server, vorher die vorhandene `QuantumXMonitor.exe` des Mini-Prüfstands
   auswählen. Die GUI startet sie bei Bedarf ohne Fenster mit `--server-only`.
   Der vollständige Programmordner mit seinen HBK/.NET-Abhängigkeiten muss
   vorhanden sein. Firmware oder Gerätekonfiguration werden nicht geändert.
   Auf einem neuen Mess-PC fehlt dieses Programm zunächst. Den vollständigen
   `net48`-Programmordner vom bisherigen PC nach
   `CSVSaver/quantumx_bridge/runtime` kopieren oder über **Bridge wählen …**
   dessen EXE auswählen. Die GUI sucht außerdem im lokalen Build-Ausgabeordner
   und in einem benachbarten Mini-Prüfstand-Repository. Nur die EXE zu kopieren
   reicht nicht; fehlende HBK-DLLs werden vor dem Start gemeldet. Alternativ
   liegen Quellcode und `Build-QuantumXBridge.ps1` unter `CSVSaver/quantumx_bridge`.
   Der lokale Build benötigt HBM Common API, .NET SDK und .NET Framework 4.8
   Developer Pack. Details: [QuantumX-Bridge](../CSVSaver/quantumx_bridge/README.md).
   Startfehler werden mit dem Pfad `quantumx_bridge/logs/bridge.log` gemeldet.
6. Die Bridge muss die TAL221 A/B auf MX440B-Kanal 3/4 liefern, einschließlich
   `force_1_raw_n`, `force_2_raw_n`, Zeitstempeln und Sequenzen. Die älteren
   KD24s-Konfigurationen auf Kanal 1/2 sind für diesen Ablauf nicht passend.
7. Beide Wägezellen ohne Luftlast **tarieren**. Ein zusätzliches Kraftprofil ist
   optional: Ohne Profil verwendet die Messung die tarierten QuantumX-Werte mit
   Verstärkungsfaktor 1. Die Tara verwendet mindestens 20 verschiedene Rohmesswerte
   über eine Sekunde und berechnet das Summenrauschen. Sie gilt für Livewerte und
   Rohmesswerte der Peak-Auswertung. Ein vorhandenes Plattform-Kraftprofil des
   Mini-Prüfstands kann weiterhin geladen werden; danach erneut tarieren.
   In der Messdatei stehen Tara und gegebenenfalls das verwendete Profil.
   `force_processing = tare_only` kennzeichnet die Auswertung ohne Zusatzprofil.

Frische gültige Kraftdaten werden für jede Bewegung verlangt. Der einstellbare
Grenzwert beträgt zunächst **1 N je Wägezelle**, geprüft an den Rohkräften nach
Tara und gegebenenfalls Profilkorrektur. Ohne Profil beziehen sich die Kraftwerte
auf die Skalierung der QuantumX-Bridge. Die Summenkraft darf Einzelkanal-Überlastungen
nicht verdecken.

Die Positioniergeschwindigkeit (Parameter 3, zwei Byte) wird in **Hz / Schritte
pro Sekunde** übertragen und zurückgelesen: bei 0,005 mm/Schritt entsprechen
2 mm/s einem Wert von **400 Hz**, 5 mm/s **1000 Hz**. Die frühere Umrechnung in
100-Hz-Einheiten war hier falsch und ergab eine um Faktor 100 zu langsame Fahrt.
Die im BAC-Handbuch genannten 100-Hz-Einheiten beziehen sich auf andere
Frequenzparameter, nicht diese Maximalgeschwindigkeit. Siehe Herstellerhandbuch,
[Maximalgeschwindigkeit](https://www.manualslib.de/manual/165134/Gunda-Colibri-34-Bac.html?page=22).

## Lichtschrankenabstand

- Beide Lichtschranken müssen an Home frei sein. **Zuerst auf der Hinfahrt** und
  **Danach auf der Hinfahrt** sind frei wählbar, beispielsweise LB2 → LB1 oder
  LB1 → LB2. Eine feste aufsteigende Nummernfolge ist nicht vorgeschrieben.
- Geschwindigkeit, maximalen Verfahrweg und Nachlauf hinter der letzten
  Ausschaltflanke einstellen. Start führt zuerst Home aus und setzt den lokalen
  Ereigniscursor zurück; globale SPS-Ereignisprotokolle werden nicht gelöscht.
- Die Achse fährt in der gewählten Richtung, erfasst Ein- und Ausschalten beider
  Schranken und wendet nach dem Nachlauf hinter der letzten Ausschaltflanke:
  Beim negativen Vorzeichen gilt `kleinste Ausschaltposition − Nachlauf`, beim
  positiven `größte Ausschaltposition + Nachlauf`. Die positive Wegeingabe von
  beispielsweise 60 mm bedeutet je nach Auswahl eine Grenze von −60 oder +60 mm.
  Der maximale Verfahrweg wird auch bei fehlenden Flanken
  eingehalten. Zurück werden dieselben vier Schaltpositionen erfasst.
- Eine Serie endet nach fünf vollständigen Zyklen an Home. Zusätzliche oder
  fehlende Flanken, Pufferüberlauf und unplausible Reihenfolge brechen sie ab.

Die SPS definiert `LightBarrierStableN = TRUE` als freien Strahl. Die fallende
Flanke ist deshalb **Einschalten / Stift erkannt**, die steigende Flanke
**Ausschalten / Strahl wieder frei**. Die bestehenden Invertierungs- und
Entprelleinstellungen bleiben wirksam. Die Messung nutzt den diagnostischen
Ereignispuffer, keine durch Paar-Gates erzeugten Ein-Zyklus-Pulse.

Je Richtung gilt `Schaltmitte = (Einposition + Ausposition) / 2`. Aus zehn
Abständen zwischen den beiden Schaltmitten werden Mittelwert und
Standardabweichung berechnet. Einschaltabstände und Richtungsmittel bleiben
separat sichtbar. Alle Ventile bleiben bei dieser Messung geschlossen.

## Düsenabstand

- Achse zwischen den Abläufen manuell versetzen. Eine Messserie benötigt danach
  keinen mechanischen Umbau. Reihenfolge von Home aus: **Düse → zweite
  Lichtschranke → Wendepunkt**, in der gewählten Achsrichtung. Auf der
  Rückfahrt: **zweite Lichtschranke → Düse → Home**.
- Array und einzelne Düse wählen. Die Zuordnung ist Array 1 → LB2, Array 2 →
  LB4, Array 3 → LB6 und Array 4 → LB8.
- Den Wendepunkt über die Lichtschranke hinaus setzen, sodass auch deren
  Ausschaltflanke auf der Hinfahrt erfasst wird. Die Eingabe ist der positive
  Abstand von Home; das Vorzeichen der Zielposition wird separat gewählt. Der gesamte
  Hin-/Rückweg muss innerhalb des 75-mm-Hubs bleiben.
- Druck startet bei **1000 mbar**. Maximale Ventilöffnungszeit startet bei
  **30 s** und kann bis 300 s eingestellt werden. **Öffnungszeit passend setzen**
  übernimmt die konservative Mindestzeit:

  `2 × Weg bis Wendepunkt / Geschwindigkeit × 1,2 + 2 s`

  Zu kurze Zeiten werden vor dem Start abgewiesen. Der Timeout zählt in der SPS
  ab dem tatsächlichen Öffnen des Ventils, separat je Zyklus.
- Nach Home öffnet die SPS mit Befehl 5 ausschließlich das gewählte Ventil,
  bevor die Hinfahrt beginnt. Damit stehen beide Kraftkurven unter Luftlast.
  Nach 0,2 s mit laufenden Kraft-/Sicherheitsprüfungen beginnt die Bewegung.
  Das Ventil bleibt über Wendepunkt und Rückfahrt hinweg offen und schließt
  nach der Rückkehr nach Home. Lichtschrankenflanken lösen kein erneutes Öffnen
  und kein Zurücksetzen des Öffnungszeitlimits aus.

Livewerte sind die geglätteten Bridge-Kräfte. Für die endgültige Auswertung wird
ein zentrierter gleitender Mittelwert über 20 tarierte Rohmesswerte (mit optionaler Profilkorrektur)
gebildet, getrennt je Richtung. Zeit und Position des Mittelwerts entsprechen
der Mitte desselben Fensters. Es wird keine zusätzliche nachlaufende
Softwarefilter-Verzögerung in die Peakposition übernommen.

Hauptpeak ist das Maximum von `abs(F1 + F2)`; das Kraftvorzeichen bleibt
erhalten. Einzelkanal-Peaks werden zusätzlich gespeichert. Der Peak muss mehr
als `max(0,005 N, 5 × Tara-Summenrauschen)` erreichen und ausreichend innerhalb
der Kurve liegen: mindestens zehn geglättete Messpunkte und 0,1 mm Abstand zu
deren Rändern. Andernfalls wird die Serie als ungültig beendet.

**Beide Richtungen eines Zyklus verwenden den Einschaltpunkt der Rückfahrt:**

`Abstand = Betrag(Peakposition der jeweiligen Richtung − Einschaltposition der Rückfahrt)`

Beispiel: Rückfahrt-Einschaltpunkt −30 mm, Kraftpeak −10 mm ergibt 20 mm Abstand.
Die Hinfahrt-Kraftkurve wird bis zum Erfassen dieses Rückfahrt-Bezugs vorläufig
gespeichert. Fehlt der Rückfahrt-Bezug oder wird vorher abgebrochen, bleibt sie
unter `pending_nozzle_cycle` erhalten; sie wird nicht als gültiger Abstand gezählt.

Dieser Bezug ist absichtlich nicht die Schaltmitte der Lichtschranke. Der
Messstift und seine optische Einschaltkante beeinflussen damit den resultierenden
Abstand. Im Plot erscheinen Summen- und Einzelkräfte, getrennte Fahrtrichtungen
sowie Markierungen für Lichtschranke und Hauptpeak.

Jede Ergebniszeile speichert `reference_direction = backward` und die tatsächlich
verwendete Referenzflanke samt Zeit und Position. Die Serien-JSON enthält unter
`geometry` die gewählte Zielposition, Sensorreihenfolge und Ventilöffnungsphase.

## Dateien und Übernahme

Jede gestartete Serie erhält unter dem ausgewählten Messordner einen eigenen
UTC-Zeitstempelordner mit `raw.csv` und `session.json`. Auch abgebrochene Serien
bleiben erhalten. CSV enthält Positionen, Kraftmesssätze, Flanken, Zeitstempel,
Sequenzen, Richtungen und Zyklen. JSON enthält Parameter, Profil, Tara,
Schaltpositionen, Peaks, verkleinerte Plotkurven, Statistik und Gültigkeitsstatus.
Nach jeder vollständigen Richtung werden Ergebnisse und gepufferte Rohdaten
in die Dateien geschrieben; bei Abschluss/Abbruch werden beide Dateien abgeschlossen.

Die Auswahlliste hält alle Serien der laufenden GUI-Sitzung, einschließlich
verschiedener Düsen. Übernahme ist erst nach zehn gültigen Fahrten möglich:

- Lichtschranken: direkt benachbarte Paare LB1–2 bis LB7–8 in das jeweilige
  SPS-Abstandsfeld übernehmen. Andere Paarungen können gemessen und exportiert,
  aber nicht in ein einzelnes vorhandenes Abstandsfeld geschrieben werden.
- Düsen: das Ergebnis der ausgewählten Serie ausdrücklich als **gemeinsamen
  Array-Abstand** übernehmen. Die Betriebssteuerung erhält keine individuellen
  Laufzeitabstände je Düse.

Die Übernahme schreibt die SPS, liest den Wert zurück und ergänzt atomar genau
das passende Feld der vorhandenen `light_barrier_settings.json`. Andere
Einstellungen bleiben erhalten. Ein Dateifehler nach erfolgreichem
SPS-Schreiben wird ausdrücklich als solcher gemeldet.

## SPS-Vertrag und Abbruch

Zusätzlich zu den bestehenden Schnittstellen gibt es die Gruppe
`MAIN.GuiProbeCalibration*` / `MAIN.ProbeCalibration*`:

| Feld | Bedeutung |
| --- | --- |
| `ProbeCalibrationInterfaceVersion` | 2 für Öffnen an Home; 1 unterstützt nur die bisherigen Befehle |
| `GuiProbeCalibrationMode` | 1 = Lichtschranken/manuell, 2 = Düse |
| `...Array`, `...Nozzle`, `...TriggerSensor` | Array 1–4, Düse 1–6, zugehörige LB2/4/6/8 |
| `...PressureMbar`, `...MaxOpenMs` | Gelatchte Parameter, maximal 6000 mbar / 300000 ms |
| `...Command` | 1 = übernehmen, 2 = an LB scharfschalten (bestehender Modus), 3 = Ventil schließen, 4 = freigeben, 5 = Ventil an Home öffnen |
| `...CommandSequence` | Zuletzt schreiben; je Transaktion erhöhen |
| `ProbeCalibrationAckSequence` | Passende Quittierung abwarten |
| `GuiProbeCalibrationHeartbeat` | Eigener Thread mit separater ADS-Verbindung schreibt alle 100 ms |
| `GuiProbeCalibrationStop` | Sofortiger Abbruch mit verriegeltem Fehler |
| `ProbeCalibrationActive`, `...Armed`, `...ValveOpen` | Zustand |
| `ProbeCalibrationOpenedAtMs` | Tatsächlicher Öffnungszeitpunkt auf SPS-Ereignisuhr |
| `ProbeCalibrationStatus` | 0 = inaktiv, 1 = bereit, 2 = scharf, 3 = offen, 4 = Fehler |

Die SPS übernimmt Parameter nur bei Befehl 1. Die normale Pulsbegrenzung von
1–500 ms bleibt bestehen. Der Kalibriermodus sperrt Bandbewegungen und normale
Düsenfreigaben bereits beim Eintritt und hat die letzte Entscheidung über
Ventilausgänge und Drucksollwerte. Bestehende Betriebsfreigaben werden deaktiviert
und nach einem Abbruch nicht wieder eingeschaltet.

Der Heartbeat wartet nicht auf serielle Positions-/Statusantworten. Sein Thread
prüft auch Abbruch, frische gültige Kräfte und Kraftgrenzen. Der Messworker muss
ihn spätestens alle 2,5 s bedienen; dadurch läuft der Heartbeat bei einem
hängenden Messworker nicht unbegrenzt weiter. Einzelne ADS-Schreibversuche haben
250 ms Timeout. Bei einer Störung endet der Heartbeat und die SPS schließt
spätestens nach ihrem bestehenden 1000-ms-Heartbeat-Timeout. Beim normalen Abbruch
fordert die GUI zuerst unmittelbar den Ventilschluss an und beendet anschließend
den Heartbeat. Der separate Ventilöffnungs-Timeout bleibt ebenfalls aktiv.

| Fehlercode | Ursache |
| --- | --- |
| 1 | Ungültiger Befehl/Parameter, Start bei bestehendem Fehler, erneutes Öffnen eines offenen Ventils oder Strahl beim Öffnen/Scharfschalten nicht frei |
| 2 | Heartbeat seit mindestens 1000 ms unverändert |
| 3 | Maximale Öffnungszeit erreicht |
| 4 | Andere Betriebssteuerung besitzt die SPS / verriegelter Betriebsfehler |
| 5 | Abbruchflag |
| 6 | Ausgewählte VTEM-Einheit nicht bereit oder fehlerhaft |

STOPP, Tab-Abbruch, Fenster schließen, Kraftausfall/-überlast, Positionsfehler
oder SPS-Fehler schließen die Luft und veranlassen Colibri-Stopp. Nach einem
Fehler fährt die Achse nicht automatisch weiter. Bei GUI-Absturz schließt die
SPS selbstständig spätestens nach dem Heartbeat-Timeout; die seriell angebundene
Colibri-Achse kann die SPS nicht direkt stoppen. Sie besitzt weiterhin einen
begrenzten Fahrbefehl. Bei einem Ausfall der seriellen Kommunikation kann die
Software keinen mechanischen Stillstand bestätigen.

## Genauigkeit und Abnahme

Es werden tatsächliche Colibri-Positionsantworten interpoliert, keine Wege aus
Sollgeschwindigkeit berechnet. SPS-Millisekunden einschließlich Überlauf und
QuantumX-UTC-Messzeiten werden auf die monotone PC-Zeit bezogen. Serielle
Antwortzeit, Abfrageabstand, ADS-Laufzeit und beobachtete Kraftdatenlatenz gehen
in die angezeigte **geschätzte** räumliche Unsicherheit ein. Lücken über 300 ms
in den Positionsdaten, fehlende Kraftdaten über 250 ms, rückläufige Sequenzen
und PC-Zeitsprünge beenden die Messung.

Die Bridge setzt ihren absoluten Zeitanker beim Empfang. Konstante Geräte- und
Übertragungsverzögerungen können deshalb verbleiben; die Anzeige ist kein
rückführbarer Genauigkeitsnachweis. Dasselbe gilt für Achsmaßstab, Referenzspiel,
Lichtschrankenhysterese und die reale Form des Luftstrahls.

Vor Verwendung als bestätigte 0,1-mm-Kalibrierung:

1. Achsmaßstab und Home-Wiederholbarkeit gegen ein bekanntes Längenmaß prüfen.
2. Lichtschrankenmessung mit bekanntem Abstand wiederholen; fünf Hin-/Rückzyklen
   und deren Richtungsabweichung bewerten.
3. Düsenmessung bei zwei Geschwindigkeiten durchführen. Peakpositionen und
   Richtungsabweichung vergleichen; bei größerer Unsicherheit langsamer messen.
4. Die vermutete Düsenmitte mechanisch vergleichen. Ein reproduzierbarer
   Kraftpeak allein beweist nicht, dass er geometrisch genau über der Düse liegt.
5. Bei geringem Druck Abbruch, fehlenden Heartbeat und Öffnungszeitgrenze prüfen.

Softwaretests verbinden keine Hardware:

```powershell
cd CSVSaver
..\.venv\Scripts\python.exe -m unittest test_probe_calibration test_probe_plc test_probe_runner test_probe_ui
```

Die SPS-Tests interpretieren die tatsächlichen neuen ST-Blöcke aus `MAIN.TcPOU`.
Die vollständigen Ablauf-Tests simulieren Achse, Optik und Kraftdaten. Sie
ersetzen weder einen TwinCAT-Build noch die Hardwareabnahme.
