# Zigbee LED Orchestrator

Integracja Home Assistant dla kontrolerów LED Zigbee, których zasilanie odłączają fizyczne przekaźniki. Przyjmuje najnowszy zamiar użytkownika, czeka na raport przekaźnika i gotowość LED-a, a dopiero potem ustawia jasność i temperaturę barwową. Obwody dodaje się po jednej parze. Opcjonalnie sama zakłada grupę Zigbee2MQTT o nazwie kończącej się na `_orchestrator` i steruje nią dodane LED-y.

**Wersja 0.3.0.** Testy automatyczne obejmują runtime HA i MQTT z atrapami. Odbiór na rzeczywistym sprzęcie pozostaje do wykonania.

## Instalacja i konfiguracja

1. W HACS dodaj `https://github.com/Rafikg523/ha-zigbee-powered-led` jako własne repozytorium typu **Integration** i zainstaluj wydanie `v0.3.0`.
2. Zainstaluj i uruchom ponownie Home Assistanta.
3. W **Ustawienia → Urządzenia i usługi → Dodaj integrację** wybierz **Zigbee LED Orchestrator**. Instalacja pyta tylko o temat Zigbee2MQTT i wspólne czasy.
4. Obwód dodasz w **Konfiguruj → Dodaj obwód**: nazwa encji, kontroler i kanał. Grupę utworzysz w **Konfiguruj → Utwórz grupę**: nazwa, na przykład `Salon`, i wybór już dodanych obwodów. Powstaje grupa i encja `Salon_orchestrator`.
5. Integracja uruchomi się w **OBSERVE**. Przygotowanie aktywnego pilotażu, rozszerzenie raportów Z2M i procedura prób opisane są w [INSTALLATION.md](INSTALLATION.md) i [TESTING.md](TESTING.md).

Przed wyjazdem możesz zakończyć instalację i mapowanie. Przed aktywacją na miejscu potrzebne są poprawne mapowanie, kopia i rollback oraz wyłączenie innych ścieżek sterowania wybranymi obwodami.

## Co działa

- Konfiguracja przez UI z istniejącego katalogu Z2M; automatyczny zakres CCT i walidacja endpointów.
- Ograniczone odczyty przekaźnika i LED-a pozwalają rozpocząć ON bez ręcznego odświeżania stanu, gdy włączono rozszerzenie raportów.
- Obwody dodawane po jednej parze, ON/OFF, jasność i CCT; opcjonalna grupa z barierą gotowości i degradacją indywidualną.
- Ostatnia intencja wygrywa, OFF ma priorytet, brak odtwarzania historycznego ON po restarcie lub rozłączeniu.
- Oddzielne stany celu, obserwacji i potwierdzenia; sensory diagnostyczne i usługi pause/resume/resync/diagnose.
- Opcjonalne [rozszerzenie Z2M](z2m/orchestrator-reports.mjs) przekazuje rzeczywiste raporty atrybutów, bez wysyłania komend Zigbee.

Zwykłe stany Z2M mogą zawierać cache lub optymistyczne aktualizacje. Aktywne próby wymagają rozszerzenia raportów; samo `online`, publikacja MQTT ani upływ czasu nie dowodzą gotowości i świecenia. Rozszerzenie obsługuje standardowe klastry `genOnOff`, `genLevelCtrl` i `lightingColorCtrl`; protokoły Tuya oparte wyłącznie na datapointach wymagają osobnej obsługi.

## Wymagania i ograniczenia

Cel testowy: **Home Assistant 2026.11.0**, istniejąca integracja MQTT i **Zigbee2MQTT 2.14.3**. Starsze wersje nie zostały zweryfikowane. HA używa wyłącznie swojego klienta MQTT. Rozszerzenie Z2M wymaga obsługi zewnętrznego JavaScriptu, patrz instrukcja.

Bieżący zakres to dowolna liczba obwodów z dedykowanymi kanałami w jednym wpisie. Grupa powstaje dopiero po dodaniu obwodów i obejmuje tylko te, które przy jej tworzeniu zaznaczysz. Obwód może należeć tylko do jednej grupy. Pokoje, nakładające się grupy, stale zasilane LED-y i edycja już zapisanej pary albo składu grupy są dalszymi etapami. Zmiana istniejącej pary wymaga usunięcia wpisu i dodania go ponownie; zapisz mapowanie wcześniej. Po restarcie, resync, dodaniu obwodu albo utworzeniu grupy sterowanie włącza się samo, gdy katalog Zigbee2MQTT zawiera zapisane obwody. Stare ON nie jest wysyłane. Brak jednego urządzenia wyłącza tylko ten obwód i zostawia powiadomienie.

[Plan rozwoju](ROADMAP.md) · [Architektura](architecture.md) · [Zmiany](CHANGELOG.md)
