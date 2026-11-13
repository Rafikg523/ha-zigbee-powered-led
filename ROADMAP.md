# Plan implementacji

Szczegółowe zadania P00–P18, kontrakty modułów i kryteria odbioru: [plan wykonawczy dla agentów](docs/superpowers/plans/2026-11-13-zigbee-led-orchestrator.md). Bieżący stan i raporty przekazania: [PROGRESS.md](docs/superpowers/plans/PROGRESS.md).

Status: przygotowano szkielet repozytorium HACS; implementacja integracji i pozostałe zadania oczekują na realizację. Kolejność odpowiada zależnościom. Specyfikacja v0.1 jest źródłem szczegółowych wymagań i macierzy T01–T16.

## 0. Inwentaryzacja i bezpieczeństwo

- [ ] Ustalić wersje HA, MQTT i Zigbee2MQTT, firmware, encje oraz obecne skrypty, blueprinty i automatyzacje.
- [ ] Zweryfikować IEEE, friendly_name, endpointy L1/L2, tematy, identyfikator i rzeczywisty skład grupy; sprawdzić ustawienia urządzeń, w tym optimistic, do_not_disturb i power_outage_memory.
- [ ] Zidentyfikować wszystkich nadawców operacyjnych /set i konflikty; sporządzić kopię konfiguracji, plan migracji oraz sprawdzalny rollback.
- [ ] Zebrać próbki raportów i przeprowadzić uzgodnione pomiary 30 zimnych startów, p50/p95, liczby komend i różnicy czasu zapalenia. Zapisać niepewności.

**Gotowe, gdy:** mapowanie i konkurencyjne sterowanie są udokumentowane, backup i rollback dostępne, a parametry czasowe oparte na pomiarach. Bez zmian produkcyjnej konfiguracji na tym etapie.

## 1. Kontrakt i testowalny rdzeń

- [ ] Zaprojektować publiczne API intencji, model konfiguracji i walidację; ustalić minimalną wersję HA po inwentaryzacji.
- [ ] Zbudować modele kanałów, LED, grup i obserwacji oraz DesiredStateStore z monotonicznym generation i ObservationStore z power_epoch i wiarygodnością.
- [ ] Dodać asynchroniczny symulator transportu i zegara oraz maszynę OFF, POWERING, WAITING_READY, APPLYING, ON, UNCONFIRMED, ERROR.
- [ ] Zaimplementować reconciler i CommandArbiter: last intent wins, priorytet OFF, blokady zasobów, redukcję zmian suwaka i limiter rzeczywistych wysyłek.
- [ ] Obsłużyć potwierdzanie przekaźnika, rozruch i sprawdzenie generation/power_epoch przed wysyłką; zapobiec zbędnym duplikatom i nieograniczonym ponowieniom.
- [ ] Pokryć testami 10 szybkich kliknięć, OFF podczas ON, spóźnione raporty, brak potwierdzenia, retained MQTT i restart bridge.

**Gotowe, gdy:** testy rdzenia przechodzą bez rzeczywistego HA, brokera i sprzętu; brak kolejki historycznych ON.

## 2. Pilotaż: dwa obwody i grupa

- [ ] Dodać adapter przez integrację MQTT HA: subskrypcje, bridge/devices/groups/events, publikacje bez retain oraz obsługę rozłączenia i zmian konfiguracji.
- [ ] Zintegrować LED1R/L1 oraz LED2R/L2: niezależne ON/OFF, brightness, CCT, pamięć parametrów i przeliczenie kelwinów na miredy.
- [ ] Dodać GroupCoordinator: barierę gotowości, pojedynczy grupowy /set, ograniczony timeout, bezpieczną częściową degradację i kontrolę rzeczywistego członkostwa.
- [ ] Obsłużyć fizyczne przełączenia, korelację komendy w locie i ochronę przed automatycznym cofnięciem zewnętrznego OFF.
- [ ] Utworzyć encje light dwóch LED i grupy, z toggle według desired, oraz sensory fazy, effective_state i confirmed.
- [ ] Dodać test integracyjny na fake MQTT: dwa LED, dwukanałowy przekaźnik i grupa; pokryć T01–T08, T12 i T16.

**Gotowe, gdy:** dwa światła i grupa działają w standardowych kartach HA, synchronizacja używa grupy Z2M, fizyczne przyciski działają, a stan niepotwierdzony jest jawny.

## 3. Odporność i bezpieczne wdrożenie MVP

- [ ] Obsłużyć restarty HA, Z2M i MQTT bez przywracania starego ON oraz bez lawiny komend po reconnect.
- [ ] Dodać diagnostykę, identyfikatory operacji, czasy, przyczyny błędów, liczniki oraz czyszczenie zadań i subskrypcji.
- [ ] Uruchomić tryb obserwacyjny i porównać raporty z inwentaryzacją; zweryfikować brak konfliktujących publikacji.
- [ ] Przeprowadzić uzgodnione testy sprzętowe, w tym 30 zimnych startów, szybkie toggle, sterowanie grupą i pomiary p50/p95; zapisać wyniki i ograniczenia.
- [ ] Przełączyć sterowanie zgodnie z planem single writer, zachowując rollback; sprawdzić kryteria odbioru MVP.

**Gotowe, gdy:** testy lokalne i uzgodnione testy sprzętowe przechodzą, stara logika nie steruje aktywnie tymi samymi zasobami, a odtworzenie poprzedniej konfiguracji jest możliwe.

## 4. Rozszerzenia po MVP

- [ ] Dodać co najmniej trzy niezależne grupy i operacje pokojowe, bez globalnej blokującej kolejki.
- [ ] Obsłużyć nakładające się grupy, konflikty zasobów i stale zasilane lampy; nie wysyłać grupowej komendy zmieniającej niezamierzonych członków.
- [ ] Pokryć T09–T15, w tym konflikt grup, zmianę członkostwa i ROOM_OFF podczas rozruchu.
- [ ] Dodać config/options flow, walidację, migracje, tłumaczenia PL, usługi resync/diagnose/pause/resume oraz dokumentację instalacji.
- [ ] Przygotować wydanie i zgłoszenie do katalogu HACS po ukończeniu i weryfikacji integracji; automatyczne strojenie czasów rozważyć po zebraniu danych.

## Zasady przez cały projekt

Jedno źródło operacyjnych publikacji /set; ostatnia intencja wygrywa; OFF ma priorytet; raport nie jest gwarancją świecenia; grupowe /set nie potwierdza każdego członka; online nie dowodzi gotowości. Nie zmieniać konfiguracji Z2M ani fizycznych ustawień kontrolerów bez osobnej akceptacji.


