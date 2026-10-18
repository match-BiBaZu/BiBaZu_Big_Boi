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
   `MAIN.ProbeCalibrationInterfaceVersion = 1`. Diese Softwareänderung allein
   aktiviert oder lädt keine SPS-Konfiguration.
3. Den Colibri-USB/RS485-Port auswählen. Die übernommene BAC-Anbindung verwendet
   9600 Baud, Slave `0xFF`, Referenzfahrt negativ mit 20 % Referenzstrom und
   zunächst `0,005 mm/Schritt`. Nach Home nimmt die Position beim Ausfahren zu.
   Der Achsmaßstab ist in der Gerätebedienung einstellbar; der Hub bleibt auf
   0–75 mm begrenzt.
4. **Colibri verbinden** prüft auch die neue SPS-Schnittstelle. Fehlende Symbole
   sperren ausschließlich die neuen Colibri-Funktionen; die anderen Tabs bleiben
   nutzbar. Ein fehlgeschlagener Verbindungsversuch gibt den COM-Port wieder frei.
5. **QuantumX verbinden** nutzt NDJSON auf `127.0.0.1:5500`. Läuft dort noch kein
   Server, vorher die vorhandene `QuantumXMonitor.exe` des Mini-Prüfstands
   auswählen. Die GUI startet sie bei Bedarf ohne Fenster mit `--server-only`.
   Der vollständige Programmordner mit seinen HBK/.NET-Abhängigkeiten muss
   vorhanden sein. Firmware oder Gerätekonfiguration werden nicht geändert.
6. Die Bridge muss die TAL221 A/B auf MX440B-Kanal 3/4 liefern, einschließlich
   `force_1_raw_n`, `force_2_raw_n`, Zeitstempeln und Sequenzen. Die älteren
   KD24s-Konfigurationen auf Kanal 1/2 sind für diesen Ablauf nicht passend.
7. Ein vorhandenes Plattform-Kraftprofil des Mini-Prüfstands laden. Es muss zu
   Gerät `192.168.10.20` und Kanal 3/4 passen. Beide Wägezellen ohne Luftlast
   **tarieren**. Die Tara verwendet mindestens 20 verschiedene Rohmesswerte über
   eine Sekunde und berechnet das Summenrauschen. Das Originalprofil wird nicht
   überschrieben; die tatsächlich verwendete Profil-/Tara-Kopie steht in der
   Messdatei.

Frische gültige Kraftdaten werden für jede Bewegung verlangt. Der einstellbare
Grenzwert beträgt zunächst **1 N je Wägezelle**, geprüft an den Rohkräften nach
Profilkorrektur. Die Summenkraft darf Einzelkanal-Überlastungen nicht verdecken.

## Lichtschrankenabstand

- Den Aufbau so platzieren, dass Home vor beiden Lichtschranken liegt. Beide
  müssen dort frei sein. Erste und zweite Lichtschranke in Förderrichtung wählen.
- Geschwindigkeit, maximalen Verfahrweg und Nachlauf hinter der letzten
  Ausschaltflanke einstellen. Start führt zuerst Home aus und setzt den lokalen
  Ereigniscursor zurück; globale SPS-Ereignisprotokolle werden nicht gelöscht.
- Die Achse fährt aus, erfasst Ein- und Ausschalten beider Schranken und wendet
  nach dem Nachlauf. Der maximale Verfahrweg wird auch bei fehlenden Flanken
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
  keinen mechanischen Umbau. Home muss vor der auslösenden Lichtschranke liegen.
- Array und einzelne Düse wählen. Die Zuordnung ist Array 1 → LB2, Array 2 →
  LB4, Array 3 → LB6 und Array 4 → LB8.
- Den Wendepunkt ausreichend hinter die erwartete Kraftspitze setzen. Der
  gesamte Hin-/Rückweg muss in den 75-mm-Hub passen.
- Druck startet bei **1000 mbar**. Maximale Ventilöffnungszeit startet bei
  **30 s** und kann bis 300 s eingestellt werden. **Öffnungszeit passend setzen**
  übernimmt die konservative Mindestzeit:

  `2 × Wendepunkt / Geschwindigkeit × 1,2 + 2 s`

  Zu kurze Zeiten werden vor dem Start abgewiesen. Der Timeout zählt in der SPS
  ab dem tatsächlichen Öffnen des Ventils, separat je Zyklus.
- Nach Home öffnet die SPS beim ersten Einschalten der ausgewählten
  Lichtschranke ausschließlich das gewählte Ventil. Es bleibt über Wendepunkt
  und Rückfahrt hinweg offen und schließt nach der Rückkehr nach Home. Erst im
  nächsten Zyklus wird erneut scharfgeschaltet.

Livewerte sind die geglätteten Bridge-Kräfte. Für die endgültige Auswertung wird
ein zentrierter gleitender Mittelwert über 20 profilkorrigierte Rohmesswerte
gebildet, getrennt je Richtung. Zeit und Position des Mittelwerts entsprechen
der Mitte desselben Fensters. Es wird keine zusätzliche nachlaufende
Softwarefilter-Verzögerung in die Peakposition übernommen.

Hauptpeak ist das Maximum von `abs(F1 + F2)`; das Kraftvorzeichen bleibt
erhalten. Einzelkanal-Peaks werden zusätzlich gespeichert. Der Peak muss mehr
als `max(0,005 N, 5 × Tara-Summenrauschen)` erreichen und ausreichend innerhalb
der Kurve liegen: mindestens zehn geglättete Messpunkte und 0,1 mm Abstand zu
deren Rändern. Andernfalls wird die Serie als ungültig beendet.

**Beide Richtungen eines Zyklus verwenden den Einschaltpunkt der Hinfahrt:**

`Abstand = Peakposition der jeweiligen Richtung − Einschaltposition der Hinfahrt`

Dieser Bezug ist absichtlich nicht die Schaltmitte der Lichtschranke. Der
Messstift und seine optische Einschaltkante beeinflussen damit den resultierenden
Abstand. Im Plot erscheinen Summen- und Einzelkräfte, getrennte Fahrtrichtungen
sowie Markierungen für Lichtschranke und Hauptpeak.

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
| `GuiProbeCalibrationMode` | 1 = Lichtschranken/manuell, 2 = Düse |
| `...Array`, `...Nozzle`, `...TriggerSensor` | Array 1–4, Düse 1–6, zugehörige LB2/4/6/8 |
| `...PressureMbar`, `...MaxOpenMs` | Gelatchte Parameter, maximal 6000 mbar / 300000 ms |
| `...Command` | 1 = übernehmen, 2 = scharfschalten, 3 = Ventil schließen, 4 = freigeben |
| `...CommandSequence` | Zuletzt schreiben; je Transaktion erhöhen |
| `ProbeCalibrationAckSequence` | Passende Quittierung abwarten |
| `GuiProbeCalibrationHeartbeat` | Aktiver Client ändert den Zähler spätestens alle 100 ms |
| `GuiProbeCalibrationStop` | Sofortiger Abbruch mit verriegeltem Fehler |
| `ProbeCalibrationActive`, `...Armed`, `...ValveOpen` | Zustand |
| `ProbeCalibrationOpenedAtMs` | Tatsächlicher Öffnungszeitpunkt auf SPS-Ereignisuhr |
| `ProbeCalibrationStatus` | 0 = inaktiv, 1 = bereit, 2 = scharf, 3 = offen, 4 = Fehler |

Die SPS übernimmt Parameter nur bei Befehl 1. Die normale Pulsbegrenzung von
1–500 ms bleibt bestehen. Der Kalibriermodus sperrt Bandbewegungen und normale
Düsenfreigaben bereits beim Eintritt und hat die letzte Entscheidung über
Ventilausgänge und Drucksollwerte. Bestehende Betriebsfreigaben werden deaktiviert
und nach einem Abbruch nicht wieder eingeschaltet.

| Fehlercode | Ursache |
| --- | --- |
| 1 | Ungültiger Befehl/Parameter, Start bei bestehendem Fehler oder Strahl beim Scharfschalten nicht frei |
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
