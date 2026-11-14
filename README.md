# Zigbee LED Orchestrator

Integracja Home Assistant dla kontrolerów LED Zigbee, których zasilanie odłączają fizyczne przekaźniki. Każdy flow dodaje jedną parę włącznik–kontroler do wspólnego wpisu. Przyjmuje najnowszy zamiar użytkownika, czeka na raport przekaźnika i gotowość LED-a, a dopiero potem ustawia jasność i temperaturę barwową. Wpis, który ma już grupę, nadal może sterować jej członkami przez Zigbee2MQTT.

**Wersja testowa 0.1.0b4:** można zainstalować i skonfigurować. Testy automatyczne obejmują runtime HA i MQTT z atrapami. Odbiór na rzeczywistym sprzęcie pozostaje do wykonania; nie jest to wydanie produkcyjne.

## Instalacja i konfiguracja

1. W HACS dodaj `https://github.com/Rafikg523/ha-zigbee-powered-led` jako własne repozytorium typu **Integration**. Włącz pokazywanie wersji beta i wybierz `v0.1.0b4` lub pobierz aktualną wersję z domyślnej gałęzi.
2. Zainstaluj i uruchom ponownie Home Assistanta.
3. W **Ustawienia → Urządzenia i usługi → Dodaj integrację** wybierz **Zigbee LED Orchestrator**.
4. Wskaż bazowy temat Z2M, nazwę wirtualnej encji, jeden kontroler i jego kanał zasilania. Kolejne pary dodaje się w Konfiguruj; czasy pozostają wspólne.
5. Integracja uruchomi się w **OBSERVE**. Przygotowanie aktywnego pilotażu, rozszerzenie raportów Z2M i procedura prób opisane są w [INSTALLATION.md](INSTALLATION.md) i [TESTING.md](TESTING.md).

Przed wyjazdem możesz zakończyć instalację i mapowanie. Przed aktywacją na miejscu potrzebne są poprawne mapowanie, kopia i rollback oraz wyłączenie innych ścieżek sterowania wybranymi obwodami.

## Co działa

- Konfiguracja przez UI z istniejącego katalogu Z2M; automatyczny zakres CCT i walidacja endpointów.
- Ograniczone odczyty przekaźnika i LED-a pozwalają rozpocząć ON bez ręcznego odświeżania stanu, gdy włączono rozszerzenie raportów.
- Pary przekaźnik–kontroler w jednym wpisie, ON/OFF, jasność i CCT. Wpis z wcześniej skonfigurowaną grupą zachowuje barierę gotowości.
- Ostatnia intencja wygrywa, OFF ma priorytet, brak odtwarzania historycznego ON po restarcie lub rozłączeniu.
- Oddzielne stany celu, obserwacji i potwierdzenia; sensory diagnostyczne i usługi pause/resume/resync/diagnose.
- Opcjonalne [rozszerzenie Z2M](z2m/orchestrator-reports.mjs) przekazuje rzeczywiste raporty atrybutów, bez wysyłania komend Zigbee.

Zwykłe stany Z2M mogą zawierać cache lub optymistyczne aktualizacje. Aktywne próby wymagają rozszerzenia raportów; samo `online`, publikacja MQTT ani upływ czasu nie dowodzą gotowości i świecenia. Rozszerzenie obsługuje standardowe klastry `genOnOff`, `genLevelCtrl` i `lightingColorCtrl`; protokoły Tuya oparte wyłącznie na datapointach wymagają osobnej obsługi.

## Wymagania i ograniczenia

Cel testowy: **Home Assistant 2026.11.0**, istniejąca integracja MQTT i **Zigbee2MQTT 2.14.3**. Starsze wersje nie zostały zweryfikowane. HA używa wyłącznie swojego klienta MQTT. Rozszerzenie Z2M wymaga obsługi zewnętrznego JavaScriptu, patrz instrukcja.

Każdy flow dodaje jedną parę do wspólnego wpisu. Pokoje, nakładające się grupy, stale zasilane LED-y i edycja istniejącej pary są dalszymi etapami. Zmiana już zapisanej pary wymaga usunięcia wpisu i dodania go od nowa; zapisz mapowanie wcześniej. Po restarcie, resync, dodaniu obwodu lub zmianie topologii ponownie jawnie włącz ACTIVE.

[Plan rozwoju](ROADMAP.md) · [Architektura](architecture.md) · [Zmiany](CHANGELOG.md)
