# Stan i kolejne etapy

## Dostępne w wersji testowej 0.1.0b1

- [x] Testowalny rdzeń, ostatnia intencja, priorytet OFF i arbiter publikacji.
- [x] Adapter przez MQTT Home Assistanta, katalog Z2M, konfiguracja przez UI i lifecycle.
- [x] Obwody dodawane po jednej parze, opcjonalna dokładnie zweryfikowana grupa i bariera gotowości.
- [x] Encje light, diagnostyka, preferencje i usługi serwisowe.
- [x] Testy jednostkowe, HA i scenariusze na atrapach MQTT.
- [x] Instrukcja instalacji, konfiguracja bez grupy i opcjonalny kanał rzeczywistych raportów ZCL.

## Odbiór pilotażu P14 — pozostaje otwarty

- [ ] Kopia i sprawdzony rollback, fizyczne mapowanie oraz wyłączone konkurujące ścieżki.
- [ ] Weryfikacja na instalacji: pojedyncze obwody, przyciski, grupa, szybkie zmiany i restarty.
- [ ] Po 30 zimnych startów w trzech wariantach, pomiary emisji i p50/p95, timeouty i liczby komend.
- [ ] Kalibracja polityk i decyzja o gotowości MVP na podstawie wyników.

## Dalszy rozwój po odbiorze

- [ ] P15: trzy niezależne grupy i atomowe operacje pokojowe.
- [ ] P16: nakładające się grupy i lampy stale zasilane.
- [ ] P17: pełna edycja konfiguracji, opcje i migracje schematów.
- [ ] P18: produkcyjny kandydat wydania i pełna walidacja HACS.

Przygotowanie obecnej wersji do instalacji nie oznacza ukończenia P14 ani P18. Wymagania odbioru są w [TESTING.md](TESTING.md); lokalny rejestr agentów i źródłowa specyfikacja pozostają poza publikowanym repozytorium.
