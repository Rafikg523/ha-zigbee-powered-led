# Zigbee LED Orchestrator

Integracja Home Assistant dla dwóch kontrolerów LED Zigbee, których zasilanie odłączają fizyczne przekaźniki. Przyjmuje najnowszy zamiar użytkownika, czeka na raport przekaźnika i gotowość LED-a, a dopiero potem ustawia jasność i temperaturę barwową. Opcjonalnie steruje oboma LED-ami przez istniejącą grupę Zigbee2MQTT.

**Wersja testowa 0.1.0b3:** można zainstalować i skonfigurować. Testy automatyczne obejmują runtime HA i MQTT z atrapami. Odbiór na rzeczywistym sprzęcie pozostaje do wykonania; nie jest to wydanie produkcyjne.

## Instalacja i konfiguracja

1. W HACS dodaj `https://github.com/Rafikg523/ha-zigbee-powered-led` jako własne repozytorium typu **Integration**. Włącz pokazywanie wersji beta i wybierz `v0.1.0b3` lub pobierz aktualną wersję z domyślnej gałęzi.
2. Zainstaluj i uruchom ponownie Home Assistanta.
3. W **Ustawienia → Urządzenia i usługi → Dodaj integrację** wybierz **Zigbee LED Orchestrator**.
4. Wskaż bazowy temat Z2M, dwa kontrolery i ich kanały zasilania. Grupa jest opcjonalna; jej skład musi odpowiadać dokładnie obu LED-om.
5. Integracja uruchomi się w **OBSERVE**. Przygotowanie aktywnego pilotażu, rozszerzenie raportów Z2M i procedura prób opisane są w [INSTALLATION.md](INSTALLATION.md) i [TESTING.md](TESTING.md).

Przed wyjazdem możesz zakończyć instalację i mapowanie. Przed aktywacją na miejscu potrzebne są poprawne mapowanie, kopia i rollback oraz wyłączenie innych ścieżek sterowania wybranymi obwodami.

## Co działa

- Konfiguracja przez UI z istniejącego katalogu Z2M; automatyczny zakres CCT i walidacja endpointów.
- Ograniczone odczyty przekaźnika i LED-a pozwalają rozpocząć ON bez ręcznego odświeżania stanu, gdy włączono rozszerzenie raportów.
- Dwa obwody, ON/OFF, jasność i CCT; opcjonalna grupa z barierą gotowości i degradacją indywidualną.
- Ostatnia intencja wygrywa, OFF ma priorytet, brak odtwarzania historycznego ON po restarcie lub rozłączeniu.
- Oddzielne stany celu, obserwacji i potwierdzenia; sensory diagnostyczne i usługi pause/resume/resync/diagnose.
- Opcjonalne [rozszerzenie Z2M](z2m/orchestrator-reports.mjs) przekazuje rzeczywiste raporty atrybutów, bez wysyłania komend Zigbee.

Zwykłe stany Z2M mogą zawierać cache lub optymistyczne aktualizacje. Aktywne próby wymagają rozszerzenia raportów; samo `online`, publikacja MQTT ani upływ czasu nie dowodzą gotowości i świecenia. Rozszerzenie obsługuje standardowe klastry `genOnOff`, `genLevelCtrl` i `lightingColorCtrl`; protokoły Tuya oparte wyłącznie na datapointach wymagają osobnej obsługi.

## Wymagania i ograniczenia

Cel testowy: **Home Assistant 2026.11.0**, istniejąca integracja MQTT i **Zigbee2MQTT 2.14.3**. Starsze wersje nie zostały zweryfikowane. HA używa wyłącznie swojego klienta MQTT. Rozszerzenie Z2M wymaga obsługi zewnętrznego JavaScriptu, patrz instrukcja.

Bieżący zakres to dwa obwody z dedykowanymi kanałami i najwyżej jedna grupa. Pokoje, nakładające się grupy, stale zasilane LED-y i pełna edycja mapowania są dalszymi etapami. Mapowanie zmienia się przez ponowne dodanie integracji; zapisz je wcześniej. Po restarcie, resync lub zmianie topologii ponownie jawnie włącz ACTIVE.

[Plan rozwoju](ROADMAP.md) · [Architektura](architecture.md) · [Zmiany](CHANGELOG.md)
