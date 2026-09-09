# Untersuchung des Rohdatenexports vom 08.09.2026, 19:02:49

Quelle: `light_barrier_raw_20260908_190249.csv`, 41 Zeilen. Laut Bediener
war jeweils nur ein physisches Teil zwischen LB1 und LB8.

Die ersten vier Geschwindigkeiten werden durch falsch zugeordnete
Zeitstempel zunehmend zu klein. Die letzten drei weisen keinen vergleichbaren
kontinuierlichen Anstieg auf. Die Rechenformel ist korrekt; aus jeder Zeile
ergeben sich exakt dieselben Abstände (40, 196, 40, 196, 40, 196, 40 mm).

| Abschnitt | Erste Zeile, mm/s | Letzte Zeile, mm/s | Median erste 5 Zeilen | Median letzte 5 Zeilen |
|---|---:|---:|---:|---:|
| LB1–2 | 240,96 | 5,54 | 240,96 | 5,71 |
| LB2–3 | 498,73 | 8,18 | 447,49 | 8,18 |
| LB3–4 | 540,54 | 2,56 | 625,00 | 2,78 |
| LB4–5 | 659,93 | 3,40 | 34,60 | 3,62 |
| LB5–6 | 740,74 | 784,31 | 784,31 | 869,57 |
| LB6–7 | 1010,31 | 1053,76 | 1059,46 | 1053,76 |
| LB7–8 | 816,33 | 869,57 | 869,57 | 869,57 |

Bereits die ersten drei Zeilen zeigen die Verschiebung (SPS-Zeit in ms):

| Exportzeile | LB1 | LB4 | LB5 | LB8 |
|---|---:|---:|---:|---:|
| 1 | 228071 | 228704 | 229001 | 229298 |
| 2 | 228099 | 228714 | 234378 | 234660 |
| 3 | 228209 | 234117 | 238791 | 239200 |

LB1 liefert hier drei ausgewählte Flanken innerhalb von 138 ms. Die GUI
hat daraus drei Teile gemacht. An LB5 liegen die entsprechenden Ereignisse
dagegen jeweils mehr als vier Sekunden auseinander. Der FIFO verbindet
folglich LB1-Flanken desselben physischen Teils mit späteren Teilen an LB5.

Von erster bis letzter Exportzeile schreitet die Zeit an LB1 nur um 64,119 s
fort, an LB8 aber um 167,679 s. Die vermeintliche Gesamtlaufzeit wächst dadurch
von 1,227 s auf 104,787 s. Allein LB4–5 benötigt in der letzten Zeile angeblich
57,674 s statt anfangs 0,297 s. Ein Geschwindigkeitsdrift kann diese
unterschiedliche Zuordnung der Ereignisse nicht erklären.

Der neue Einzelteilmodus behält die erste ausgewählte Flanke jedes Sensors
und zählt kurze Wiederholungen nicht als neue Teile. Tests verwenden unter
anderem die Flanken des ersten Durchlaufs einschließlich aller sechs
zusätzlichen LB1–LB4-Flanken aus den Folgezeilen. Daraus entsteht genau ein
Durchlauf mit den ursprünglichen Zeitstempeln der ersten Zeile.

Die Daten werden nicht künstlich geglättet oder rückwirkend umsortiert.
Insbesondere ist der gesamte Export ohne vollständige ursprüngliche
Ereignishistorie nicht zuverlässig reparierbar. Für die Auswertung ist eine
neue Messung mit der korrigierten GUI erforderlich.
