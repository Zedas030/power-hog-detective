# Stromfresser-Detektiv

Lernt für jedes Gerät an einer Messsteckdose das normale Verbrauchsmuster und meldet ungewöhnliches Verhalten **mit Erklärung in Klartext**, zum Beispiel:

> Kühlschrank: Kompressor läuft seit 3 Tagen 42 % länger pro Zyklus (15 Min. statt 11 Min.) – Tür, Dichtung oder Vereisung prüfen.

> Nächtliche Grundlast seit Sonntag +36 W, keiner Steckdose zuzuordnen (136 W statt 100 W ohne Messsteckdose, ≈ 108 €/Jahr) – Geräte ohne Messsteckdose prüfen …

> Spülmaschine läuft seit 5,0 Std. ununterbrochen (eingestellte Grenze 5,0 Std.) – vergessen auszuschalten?

Der Detektiv **liest nur**. Er schaltet keine Geräte. Technisch gesperrt sind alle Home-Assistant-Dienste außer `notify.*` und `persistent_notification.*`; ein Test prüft das. Er legt nur seine eigenen Sensoren `sensor.stromfresser_*` und einmalig das Skript `script.stromfresser_war_ok` an.

---

## Inhalt

1. [Was er erkennt](#was-er-erkennt)
2. [Wie er lernt](#wie-er-lernt)
3. [Installation](#installation)
4. [Secrets (HA-Token)](#secrets)
5. [Konfiguration](#konfiguration)
6. [Bedienung: Meldungen, „War ok“, CLI](#bedienung)
7. [Home Assistant, Prometheus, Grafana](#home-assistant-prometheus-grafana)
8. [Monatsbericht](#monatsbericht)
9. [Daten und SD-Karte](#daten-und-sd-karte)
10. [Tests und Abnahme-Szenarien](#tests)
11. [Fehlersuche](#fehlersuche)
12. [Grenzen](#grenzen)

---

## Was er erkennt

| Gerätetyp | Beispiele | Gelernte Merkmale (pro Tag) | Typische Meldung |
|---|---|---|---|
| `zyklus` | Kühlschrank, Boiler | Zyklusdauer, Pause, Zyklen/Tag, Laufzeitanteil, kWh | Zyklen länger → Tür/Dichtung/Vereisung; Boiler heizt öfter nach → Warmwasser, tropfender Hahn |
| `dauerlast` | Router, Server, Raspberry Pi | Grundlast (Median) | Grundlast +9 W seit Dienstag |
| `programm` | Spülmaschine, Waschmaschine | erkennt Programme (Dauer + Energie), Läufe, Standby | Programm A braucht seit 2 Läufen 50 % mehr Energie → Heizstab verkalkt? |
| `nutzung` | PC, TV | Standby-Leistung, Stunden an | Standby 4,7 W statt 0 W → Schnellstart/Updates? |
| `ladegeraet` | iPad-Station | Leistung ohne Ladevorgang, Ladungen | zieht ohne Ladevorgang dauerhaft … |
| `zeitplan` | Zelt, Aquarium | Einschaltzeit, Leistung im Betrieb | schaltet um 07:00 statt 06:00 ein → Zeitschaltuhr? |
| Haus | Hauszähler (falls vorhanden) | nächtliche Grundlast, Anteil ohne Messsteckdose, große Lastspitzen (Durchlauferhitzer) | Nächtliche Grundlast seit Sonntag +36 W, keiner Steckdose zuzuordnen |

Dazu kommen Live-Prüfungen, jede Minute:

- **Läuft ungewöhnlich lange** (Bügeleisen-Fall): Grenze per `max_laufzeit` je Gerät oder automatisch gelernt (längster üblicher Lauf × 1,2 bzw. 99-%-Wert × 1,5). Beim Kühlschrank heißt das: Kompressor läuft ohne Pause → Tür offen?
- **Verbraucht plötzlich nichts mehr**, obwohl die Steckdose an ist (Defekt). Bei Dauerlast-Geräten nach 15 Min.; bei Zyklus-Geräten, wenn die übliche längste Pause dreifach überschritten ist. Eine absichtlich ausgeschaltete Steckdose löst keine Meldung aus.
- **Selbstüberwachung**:
  - Steckdose ist `unavailable`/`unknown`, also Verbindung weg: Push nach 30 Min. (`ueberwachung.nicht_verfuegbar_nach`).
  - Steckdose liefert keine frischen Werte mehr, ist aber verbunden: steht nur still in der Weboberfläche, weil selten genutzte Geräte (z. B. die Spülmaschine) sonst ständig melden würden. Mit `ueberwachung.keine_daten_melden: true` kommt auch dafür ein Push.
  - Home Assistant ist weg; die Meldung wird gepuffert und nach der Rückkehr zugestellt.
  - Entitäts-ID umbenannt: Der Detektiv erkennt das über die `unique_id`, verfolgt die Entität automatisch weiter und meldet es.
  - Entität fehlt ganz.
  - Hausdaten fehlen oder sind unplausibel (Haus < Summe der Steckdosen).

  Ob eine Steckdose noch lebt, sieht er auch dann, wenn sich die Leistung nicht ändert. Dafür beobachtet er einen „Lebenszeichen“-Sensor desselben Geräts (meist die Spannung) und ermittelt ihn automatisch aus der HA-Registry.

## Wie er lernt

- **Mindestens 14 Tage** Lernphase (`lernen.min_tage`), vorher keine Vergleichs-Meldungen.
- **Gleitendes Fenster von 28 Tagen** (`lernen.fenster_tage`): Normal ist der Median der letzten 28 gültigen Tage. Langsame, gewollte Änderungen wandern so ins Normal und lösen keinen Alarm aus.
- **Rauschunterdrückung:** Ein Tag gilt als auffällig, wenn er mindestens 25 % *und* 3 robuste Standardabweichungen (MAD) vom Normal abweicht und eine Mindestgröße überschreitet (z. B. 2 Min. Zyklusdauer, 3 W Grundlast). Gemeldet wird erst nach **3 Tagen in Folge** (`erkennung.stabil_tage`). Harmlose Richtungen (weniger Verbrauch, längere Pausen) melden nichts.
- **Neues Normal:**
  - nach „War ok“ sofort: Der Vergleich startet am Beginn der Abweichung neu und braucht 3 Tage,
  - ohne Feedback automatisch, wenn eine Abweichung 14 Tage anhält (mit Hinweis).
- **Entwarnung:** Nach 2 normalen Tagen gilt eine Auffälligkeit als erledigt; auf Wunsch kommt eine Meldung „… ist wieder normal“.
- **Vorlernen aus der Vergangenheit:** `stromfresser vorlernen` holt 35 Tage aus Prometheus (alle 15 s, genau genug für Kompressorzyklen). Fehlt etwas, nimmt er den HA-Recorder, außerdem die stündliche HA-Langzeitstatistik seit April für die Berichte. Alte Entitäts-IDs (vor einer Umbenennung) stehen je Gerät unter `verlauf:`.

## Installation

Auf dem Raspberry Pi (Docker mit Compose-Plugin):

```bash
sudo mkdir -p /opt/stromfresser-detektiv && sudo chown $USER /opt/stromfresser-detektiv
# Projektdateien nach /opt/stromfresser-detektiv kopieren, dann:
cd /opt/stromfresser-detektiv
cp config/config.example.yaml config/config.yaml   # anpassen, siehe unten
cp .env.example .env && chmod 600 .env              # Token eintragen, siehe "Secrets"
mkdir -p data && sudo chown 10001:10001 data         # der Container läuft als Benutzer 10001
docker build --target test -t stromfresser-detektiv:test .   # führt alle Tests aus
docker compose run --rm stromfresser-detektiv selbsttest     # Verbindung + Entitäten prüfen
docker compose run --rm stromfresser-detektiv vorlernen      # Historie einlesen (ca. 5 Min.)
docker compose up -d --build
```

Was die `docker-compose.yml` festlegt:

- Autostart (`restart: unless-stopped`).
- Ressourcenlimits: 256 MB RAM, 0,5 CPU, 64 Prozesse.
- Schreibgeschütztes Dateisystem, keine Capabilities.
- Log-Rotation: 3 × 10 MB.
- Host-Netz, damit der Container HA und Prometheus unter `127.0.0.1` erreicht.
- Healthcheck auf `/health`.

Weboberfläche: `http://<pi>:8101/` zeigt Status, offene Auffälligkeiten mit Knopf „War ok“, Geräte, letzte Meldungen und Monatsberichte.

Update: Dateien ersetzen, dann `docker compose up -d --build`. Daten und Gelerntes bleiben in `data/`.

## Secrets

| Was | Wo | Hinweis |
|---|---|---|
| Home-Assistant-Token | `/opt/stromfresser-detektiv/.env` → `HA_TOKEN=…` | Datei `chmod 600`, nie in `config.yaml` |

**Token anlegen bzw. tauschen (rotieren):**

1. In HA: unten links Profil → Reiter *Sicherheit* → *Langlebige Zugriffstoken* → *Token erstellen*, Name z. B. `stromfresser-detektiv`.
2. Den Token in `.env` hinter `HA_TOKEN=` eintragen (Datei bleibt `chmod 600`).
3. `docker compose up -d` (der Container liest die `.env` beim Start).
4. Prüfen: `docker compose exec stromfresser-detektiv python -m sfd selbsttest`.
5. Den alten Token in HA unter *Langlebige Zugriffstoken* löschen.

Der Token braucht Lesezugriff auf Zustände, Registry, Verlauf und Statistik und muss `notify` aufrufen dürfen. Einmalig legt er das Skript `script.stromfresser_war_ok` an. HA kennt keine rein lesenden Token; die Sperre gegen Schalten steckt im Detektiv selbst.

## Konfiguration

Alles steht kommentiert in `config/config.yaml`. Die wichtigsten Stellen:

```yaml
home_assistant:
  notify: notify.mobile_app_phone      # Meldungen mit Knopf "War ok"
preise:
  bezug_eur_kwh: 0.34
  einspeisung_eur_kwh: 0.00
geraete:
  kuehlschrank:
    name: Kühlschrank
    typ: zyklus                        # zyklus | dauerlast | programm | nutzung | ladegeraet | zeitplan
    profil: kuehlgeraet                # passende Tipps in den Meldungen
    leistung: sensor.kuehlschrank_leistung
    schalter: switch.kuehlschrank      # damit "aus" nicht als Defekt zählt
  buegeleisen:
    name: Bügeleisen
    typ: nutzung
    leistung: sensor.buegeleisen_leistung
    max_laufzeit: 45m                  # Bügeleisen-Fall
```

Ein neues Gerät eintragen und `docker compose restart` ausführen. Es lernt dann ab sofort; mit `vorlernen` auch rückwirkend.

**Hauszähler:** Unter `haus:` einen Leistungssensor des ganzen Hauses eintragen (z. B. IR-Lesekopf am Stromzähler oder Shelly 3EM). Dann laufen nächtliche Grundlast, nicht zugeordnete Last, Netz/Solar-Anteil und die Schätzung des Durchlauferhitzers. Ohne Zähler ruht dieser Teil. Der Solaranteil wird dann aus `solar_ins_haus` (z. B. NOAH-Ausgangsleistung) geschätzt.

## Bedienung

**Meldungen** kommen über den eingestellten `notify`-Dienst. Am Handy hat jede Auffälligkeit den Knopf **„War ok“**.

**„War ok“ (Feedback)** geht auf vier Wegen. Der Detektiv übernimmt das neue Verhalten dann als normal:

| Weg | So geht's |
|---|---|
| Handy | Knopf „War ok“ in der Benachrichtigung |
| HA-Aktion | Aktion `script.stromfresser_war_ok` mit `geraet: Kühlschrank` (Name oder Schlüssel, `Haus` für das Haus) und optional `notiz`; nutzbar in Dashboards und Automationen |
| Weboberfläche | Knopf „War ok“ neben der Auffälligkeit |
| CLI | `docker compose exec stromfresser-detektiv python -m sfd ok <ID oder Gerät> --notiz "…"` |

Was „War ok“ bewirkt:

- Tagesmerkmale: neues Normal ab Beginn der Abweichung.
- Programme: Die abweichenden Läufe werden zum Programm.
- „läuft zu lange“: Die Grenze steigt auf die beobachtete Laufzeit × 1,5.
- Ausfälle („keine Werte“, „0 W“): stumm, bis das Problem vorbei ist. Danach kann es wieder melden.

**CLI** (im Container, `docker compose exec stromfresser-detektiv python -m sfd …`):

| Befehl | Zweck |
|---|---|
| `status` | Lernfortschritt, Merkmale von gestern, offene Auffälligkeiten |
| `meldungen` | letzte Auffälligkeiten mit ID und Zustand |
| `ok <ID/Gerät>` | Feedback „War ok“ |
| `vorlernen [--tage 35] [--melden]` | aus Prometheus/HA-Historie lernen |
| `bericht [--monat 2026-10] [--senden]` | Monatsbericht erzeugen (und Kurzfassung schicken) |
| `szenario kompressor\|grundlast\|laufzeit\|alle [--senden]` | Abnahme-Szenarien, siehe unten |
| `statistik-import` | HA-Stundenstatistik nachladen |
| `selbsttest` | Token und Entitäten prüfen |

## Home Assistant, Prometheus, Grafana

**HA-Sensoren** (werden jede Minute und nach jedem HA-Neustart neu gesetzt):

- `sensor.stromfresser_status`: `ok` / `lernt` / `auffällig` / `keine Verbindung`; Attribut `offene_meldungen`.
- `sensor.stromfresser_offene_auffaelligkeiten`: Anzahl.
- `sensor.stromfresser_<gerät>`: `normal` / `lernt` / `auffällig` / `keine Daten`; Attribute Gerätetyp, Lerntage, aktuelle Meldung, Merkmale von gestern.
- mit Hauszähler zusätzlich `sensor.stromfresser_nachtgrundlast` und `sensor.stromfresser_unzugeordnete_grundlast`.

**Prometheus:** `http://<pi>:8101/metrics`. Scrape-Eintrag:

```yaml
  - job_name: stromfresser_detektiv
    scrape_interval: 60s
    static_configs:
      - targets: ['localhost:8101']
```

Metriken (`sfd_…`):

- `device_power_watts`
- `device_last_value_age_seconds`
- `device_available`
- `device_running_seconds`
- `feature_value` / `feature_baseline` (Merkmal gegen gelerntes Normal)
- `learning_days`
- `anomalies_open`
- `house_night_base_watts`
- `ha_connected`
- `outbox_pending`
- `db_size_bytes`

**Grafana:** `grafana/stromfresser-detektiv.json` importieren oder in den Provisioning-Ordner legen. Die Datenquelle ist eine Variable. Neu erzeugen mit `python grafana/make_dashboard.py`.

## Monatsbericht

Am 1. jedes Monats um 07:00 (einstellbar) schreibt der Detektiv `data/berichte/bericht-JJJJ-MM.html` (auch in der Weboberfläche) und schickt eine Kurzfassung per `notify`. Der Bericht enthält:

- Verbrauch und Kosten pro Gerät; Kosten = kWh × Netzanteil der jeweiligen Stunde × Strompreis.
- Anteil aus Solar/Batterie gegenüber Netz: mit Hauszähler gemessen, sonst aus der NOAH-Ausgangsleistung geschätzt.
- Entwicklung der Grundlast im Monat.
- Top-5-Sparpotenziale mit €/Jahr und Rechenweg. Mögliche Posten:
  - Standby abschalten,
  - Nutzungsgeräte, die nachts laufen,
  - Kühlgerät über einem Referenzwert,
  - Lasten in Sonnenstunden verlegen,
  - nicht zugeordnete Grundlast,
  - Durchlauferhitzer.
- Auffälligkeiten des Monats und Hinweise zur Datenlage.

Von Hand: `python -m sfd bericht --monat 2026-10`.

## Daten und SD-Karte

SQLite unter `data/stromfresser.sqlite` (WAL-Modus). Messwerte werden gesammelt und einmal pro Minute in einem Rutsch geschrieben.

Täglich um 00:20 läuft die Tagesauswertung. Danach wird verdichtet:

| Stufe | Aufbewahrung (einstellbar unter `speicher:`) |
|---|---|
| Rohwerte | 14 Tage |
| Minutenmittel | 90 Tage |
| Viertelstundenmittel | 2 Jahre |
| Stundenenergie, Tagesmerkmale, Läufe, Meldungen | dauerhaft (wenige MB pro Jahr) |

Größe nach 35 Tagen Vorlernen: etwa 85 MB, danach weitgehend stabil.

## Tests

```bash
docker build --target test -t stromfresser-detektiv:test .       # Unit- und End-to-End-Tests
python -m pytest -q tests/                                       # lokal (requirements.txt + requirements-dev.txt)
```

Die Testdateien:

- `tests/test_detection.py`: Erkennungslogik mit synthetischen Verbrauchsdaten, darunter Kompressorzyklen, Grundlast, Laufzeit, Defekt, Ausfall, Feedback, langsame Drift, Programme, Zeitpläne, Plausibilität und Stummschaltung.
- `tests/test_report.py`: Monatsbericht.
- `tests/test_e2e.py`: der echte Dienst gegen ein nachgebautes Home Assistant (WebSocket + REST). Abgedeckt sind:
  - Meldung mit Knopf, Knopf-Rückmeldung,
  - Umbenennung einer Entität,
  - Steckdose verstummt, Lebenszeichen über die Spannung,
  - Hausdaten weg, Feedback über die HA-Aktion,
  - HA-Ausfall, Web, Metriken,
  - es werden nur `notify`-Dienste aufgerufen.

**Abnahme-Szenarien auf echten Lerndaten** (laufen auf einer Kopie der Datenbank; mit `--senden` kommen sie als `[TEST]` aufs Handy):

```bash
docker compose exec stromfresser-detektiv python -m sfd szenario alle --senden
```

| Szenario | Was passiert |
|---|---|
| `kompressor` | Kühlschrank-Zyklen 40 % länger als gelernt, 5 Tage. Danach „War ok“ und 5 weitere Tage ohne Meldung. |
| `grundlast` | Simulierter Hauszähler über den echten Steckdosenverläufen, nach 16 Lerntagen +35 W ohne Messsteckdose. |
| `laufzeit` | Gerät mit `max_laufzeit` läuft über die Grenze und wird dann ausgeschaltet. |

## Fehlersuche

| Problem | Prüfen |
|---|---|
| Keine Meldungen | `docker logs stromfresser-detektiv`; `sfd_outbox_pending` > 0 heißt, HA nimmt `notify` nicht an. `selbsttest` ausführen. |
| „meldet keine Werte“ in der Weboberfläche, obwohl die Steckdose geht | Log-Zeile „Lebenszeichen für …“. Fehlt sie, je Gerät `lebenszeichen: sensor.<x>_spannung` eintragen. |
| Gerät „lernt“ noch lange | Es braucht 14 Tage mit mindestens 80 % Messabdeckung; `status` zeigt den Stand. |
| Port belegt | `web.port` in `config.yaml` ändern; Prometheus-Ziel anpassen. |

## Grenzen

- Ohne Hauszähler sind Hausverbrauch, Netzbezug, nächtliche Grundlast des ganzen Hauses und der Durchlauferhitzer nicht messbar. Die Logik dafür ist fertig und getestet; sie startet, sobald ein Zähler in `haus:` eingetragen ist.
- Die Durchlauferhitzer-Schätzung erkennt große Lastspitzen, die keiner Steckdose zuzuordnen sind. Mit Zählerwerten im Sekundenbereich ist sie gut, mit 5-Minuten-Werten nur grob.
- Programm-Erkennung lohnt sich bei Geräten mit wiederkehrenden Programmen. Beim 3D-Drucker ist sie abgeschaltet, weil jeder Druck anders ist.
