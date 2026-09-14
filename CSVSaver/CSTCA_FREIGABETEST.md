# CSTCA-Freigabetest mit Drehmomentvorgabe null

Die separate Testkopie liegt in `.tandem_validation/cstca_term22_test/TwinCAT Projekt3.sln`, relativ zum Workspace `Dashas_ws`. Sie ist fuer **Term 22 / Motor 2 / EtherCAT-Adresse 1011** vorbereitet. Die Produktionsdateien wurden beim Erstellen nicht geaendert. In der Testkopie wird die normale MAIN-Logik durch den begrenzten Test ersetzt; Motor 1, alle Ventilausgaenge und die beiden VTEM-Ausgangsarrays werden in jedem Zyklus auf null gesetzt.

Diese Kopie prueft nur die Freigabe mit **Target torque = 0**, **Torque offset = 0** und festem Winkel **0**. Sie enthaelt keinen Drehtest. Der Encoder bleibt angeschlossen und seine Fehlerueberwachung aktiv. CSTCA ist kein Nachweis, dass diese Firmware bei einem OCT-Fehler geberlos weiterarbeiten kann.

## Schritt 4: Die Verbindung zwischen PLC und Klemme

Eine Verknuepfung verbindet eine Variable im PLC-Programm mit einem Ein- oder Ausgang der Klemme. Sie ist keine Kabelverbindung. Die Testkopie enthaelt bereits diese Verknuepfungen; im eigenen begonnenen Projekt sind die neuen Variablen noch nicht vorhanden.

1. In TwinCAT **Datei > Oeffnen > Projekt/Projektmappe** waehlen und die oben genannte Testkopie oeffnen. Am vollstaendigen Dateipfad pruefen, dass es die Kopie unter `cstca_term22_test` ist. Das bisherige Projekt vorher speichern, ohne es zu ueberschreiben.
2. Im Projektbaum **PLC > Untitled1 > Untitled1 Project > POUs > MAIN** oeffnen. Dort sind die zusaetzlichen Variablen und die Aufrufe von `Test` und `TestSettings` zu sehen. Der Task `PlcTask` ruft MAIN mit 1 ms Zykluszeit auf.
3. Das PLC-Projekt **Erstellen/Build** ausfuehren. Danach zeigt die Instanz unter **Untitled1 Instance > PlcTask Outputs** die zu verknuepfenden Ausgangsvariablen an.
4. Unter **I/O > Devices > Geraet 1 (EtherCAT) > Term 1 (EK1100) > Term 22 (EL7201-0010)** den jeweiligen Ausgang aufklappen. Mit Rechtsklick auf die Blattvariable **Change Link / Verknuepfung aendern** die Zuordnung ansehen. In der gelieferten Testkopie soll sie bereits wie folgt bestehen:

| Ausgang von Term 22 | PLC-Variable in PlcTask Outputs | Datentyp |
| --- | --- | --- |
| DRV Controlword / Controlword | MAIN.ConveyorServo2Controlword | UINT |
| DRV Target torque / Target torque | MAIN.TestTargetTorque | INT |
| DRV Commutation angle / Commutation angle | MAIN.TestCommutationAngle | UINT |
| DRV Torque offset / Torque offset | MAIN.TestTorqueOffset | INT |

Unter Prozessdaten sind deshalb die Ausgangs-PDOs **1600, 1602, 1603 und 1605** aktiv, zusammen 8 Byte. `1605` gibt den Offset ausdruecklich mit null vor. Die Testkopie ergaenzt damit die CSTCA-Vorauswahl um den Offset. `MAIN.ConveyorServo2TargetVelocity` bleibt ohne Hardwareverbindung und wird im Programm auf null gesetzt.

Die bestehenden sechs Eingangsverbindungen pro Motor bleiben erhalten: Position, Statusword, ActualVelocity, FeedbackInvalid, WcState und InfoState. Die vier Eingangs-PDOs **1A00, 1A01, 1A02 und 1A0C** bleiben aktiv. Bei Term 21 bleiben Controlword und TargetVelocity verknuepft; beide schreibt diese Test-PLC immer auf null.

Fuer die CoE-Lesepruefung werden **keine zusaetzlichen I/O-Verknuepfungen** benoetigt. `FB_CstcaReadSettings` liest den echten Modus, den Bremsen-Override und den Drehmomentoffset ueber `Tc2_EtherCAT` vom EtherCAT-Master `192.168.178.10.2.1`, Adresse 1011. Die Bibliothek ist bereits im Projekt eingetragen. Sie schreibt keine CoE-Werte.

## Schritt 5: Konfiguration und Programm auf die Steuerung laden

Das ist ein Eingriff in die laufende Steuerung: **Konfiguration aktivieren** uebertraegt die I/O-Zuordnung und Startup-Eintraege; **PLC-Download** uebertraegt das Programm. **PLC-Start** fuehrt das Programm aus. Diese Schritte sind verschieden. Das reine Oeffnen oder Erstellen der Projektdatei uebertraegt noch nichts.

Die Rollen muessen weiterhin ohne Band sein, die GUI geschlossen und die Anlage fuer diesen Test stillgesetzt sein. Die korrigierte Verdrahtung muss fertig sein. Der unabhaengige Abschaltweg muss erreichbar bleiben. Bei ausgeschalteter Motorversorgung konfigurieren und laden; Config-Modus allein ist keine elektrische Freischaltung. Eventuell noch vorhandene PLC-Forcierungen vor dem Test entfernen.

1. In TwinCAT die Zielsteuerung kontrollieren: **AMS Net ID 10.145.4.14.1.1**. Das ist nicht die oben genannte EtherCAT-Master-Net-ID.
2. In der Testkopie **Erstellen/Build** ausfuehren; bei Compiler- oder Zuordnungsfehlern hier bleiben und die Fehlermeldung pruefen.
3. Im Menue **TwinCAT > Activate Configuration / Konfiguration aktivieren** waehlen. Die Rueckfrage zum Neustart des TwinCAT-Systems betrifft diese Zielsteuerung. Die kopierte Konfiguration enthaelt fuer Term 22 **7010:03 = 11**, fuer Term 21 weiterhin CSV **9**, und fuer beide **8012:01 = FALSE**. Die bekannten Motor- und Spannungsparameter bleiben enthalten.
4. Unter **PLC > Login / Einloggen** die PLC anmelden und, falls angefordert, einen **Download** der Testanwendung ausfuehren. Bei einer angebotenen Online-Aenderung stattdessen einen vollstaendigen Download der Testkopie verwenden. Das Ziel ist, dass die neue Testanwendung und ihre passende Prozessabbild-Zuordnung gemeinsam aktiv sind.
5. Unter **PLC > Start** die Test-PLC starten. `TestStart` ist anfangs FALSE; das startet noch keinen Freigabetest. Die neue MAIN-Implementierung schreibt alle Ausgangsbefehle null.
6. Motorversorgung einschalten. Unter **Term 22 > Online** und **Term 21 > Online** muss EtherCAT **OP** angezeigt werden. Das bedeutet zyklische Kommunikation, nicht Motorfreigabe. `MAIN.ConveyorServo2Controlword` und `MAIN.ConveyorServo1Controlword` muessen weiterhin **0** sein.
7. Unter **Term 22 > CoE - Online** die echten Onlinewerte lesen: **6010:03 = 11**, **8012:01 = FALSE**. Die Test-PLC prueft diese Werte ebenfalls. Ein manuell geloester Bremsen-Override muss vor dem Test wieder FALSE sein. Auf dieser Revision gibt es kein `8012:02`.

## Einmaligen Freigabetest starten

MAIN im eingeloggten PLC-Projekt oeffnen und diese Variablen beobachten:

| Variable | Vor dem Start / Bedeutung |
| --- | --- |
| MAIN.TestSettings.ActualMode | 11 |
| MAIN.TestSettings.ManualBrakeRelease | FALSE (Automatik) |
| MAIN.TestSettings.ActualTorqueOffset | 0 |
| MAIN.TestSettings.Valid | TRUE nach erfolgreicher Lesepruefung |
| MAIN.TestSettings.ReadError / AdsError | FALSE / 0, sonst Lesezugriff klaeren |
| MAIN.Test.Ready | TRUE: Voraussetzungen fuer einen Reset-/Freigabeversuch erfuellt |
| MAIN.TestStart | FALSE; einmal TRUE **schreiben**, nicht forcen |
| MAIN.TestAbort | FALSE; TRUE schreiben bricht den Versuch ab |
| MAIN.Test.State / Result / FailureStatusword / FailureState | Verlauf und gespeichertes Ergebnis |

`Test.Ready` darf auch bei einem noch quittierbaren Fehler von Motor 2 TRUE sein: Der Versuch enthaelt genau einen Resetimpuls. Motor 1 muss deaktiviert sein. Beide Feedbackdaten muessen gueltig und die Position von Motor 2 fuer 100 ms stationaer sein. Bei falschem Modus, aktivem Bremsen-Override, altem/fehlerhaftem CoE-Lesewert oder ungueltigen Prozessdaten bleibt die Freigabe gesperrt.

In der Onlineansicht bei **TestStart** in der Spalte fuer den vorbereiteten Wert **TRUE** eintragen und **PLC > Write values / Werte schreiben** ausfuehren. Nicht **Force values / Werte forcen** verwenden. MAIN setzt den Startwunsch wieder FALSE. Die Steuerwortfolge laeuft automatisch: 0, ein Resetimpuls 128, 0, 6, 7, 15, anschliessend wieder 0. Die Bremse bleibt auf automatischer Ansteuerung der Klemme.

Der Baustein qualifiziert 250 ms lang den Zustand Operation enabled. Ab Switch on ist die Freigabephase auf hoechstens 1 s begrenzt (bei laufender 1-ms-PLC-Task); zusaetzlich gibt es Reset-, Gesamt- und Abschaltzeitgrenzen. Ein neuer Drivefehler, ungueltige Daten, Bewegung ueber 262 Encoderzaehler oder TestAbort setzt den Controlword-Ausgang im selben PLC-Zyklus auf null. Die Zeitgrenzen ersetzen keine unabhaengige Abschaltung bei Ausfall der PLC.

| Result | Bedeutung |
| --- | --- |
| 0 | Noch kein abgeschlossenes Ergebnis |
| 1 und Done = TRUE | 250 ms freigegeben, anschliessend deaktivierten Zustand bestaetigt |
| 2 | Voraussetzung fehlte oder ging verloren |
| 3 | Drivefehler waehrend Freigabe; FailureStatusword ist gespeichert |
| 4 | Zeitgrenze beim Freigeben erreicht |
| 5 | Unerwartete Positionsaenderung |
| 6 | Abbruch ueber TestAbort |
| 7 | Einmaliger Reset fuehrte nicht zum deaktivierten, fehlerfreien Zustand |
| 8 | Operation enabled ging waehrend der Haltezeit verloren |
| 9 | Deaktivierung wurde nicht bestaetigt |

Bei einem Fehler keinen Strom-/Drehmomentwert erhoehen. Result, FailureState, FailureStatusword und neue Eintraege der TwinCAT-Fehlerliste/Diag History festhalten. Die PLC-Ergebnisnummer ist keine CoE-Diagnosenummer. Erfolg bei null Drehmoment beweist weder eine fehlerfreie OCT-Verbindung unter Motorstrom noch geberlosen Betrieb.

## Nach dem Test

Controlwords und Sollwerte auf null sowie **8012:01 = FALSE** pruefen. Fuer den normalen Foerderbandbetrieb die urspruengliche Produktionskonfiguration **einschliesslich CSV-PDOs, Mode 9 und passender Produktions-PLC** wiederherstellen; nicht allein das alte PLC-Programm auf die CSTCA-I/O-Zuordnung laden. Die Testkopie nicht als Produktions-Bootprojekt einrichten.

Beckhoff-Referenzen: [CSTCA und Freigabesequenz ohne NC](https://infosys.beckhoff.com/content/1031/el72x1-001x/1859316107.html), [automatische PLC-I/O-Adressen](https://infosys.beckhoff.com/content/1033/tc3_plc_intro/2529360523.html), [CoE-Lesebaustein](https://infosys.beckhoff.com/content/1033/tcplclib_tc2_ethercat/56996235.html).

