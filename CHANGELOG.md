# Changelog

## 0.1.0b3 — samodzielny rozruch i odczyty kontrolne

- Nowe ON odczytuje nieznany stan przekaźnika zamiast kończyć się `relay_state_unknown`; nie wymaga ręcznego odświeżania Z2M.
- Ograniczony odczyt gotowości kontrolera po czasie rozruchu i odczyt stanu/jasności/CCT po ustawieniu parametrów. Działa także dla niezmienionych parametrów bez spontanicznych raportów.
- Późne wiarygodne raporty mogą zakończyć `UNCONFIRMED` bez ponawiania komend. Jawne nowe ON po zakończonym błędzie odczytu może rozpocząć nową operację.
- Odczyty przechodzą przez istniejący arbiter i klient MQTT HA, z walidacją adresatów, tokenów i priorytetem OFF; nie tworzą grup Z2M.
- Diagnostyka pokazuje wersję wykonywanego kodu, liczbę odczytów oraz zaobserwowane parametry.
- Nowe scenariusze używają produkcyjnego adaptera i surowych odpowiedzi ZCL, bez przygotowanego raportu OFF. Testy lokalne nie zastępują odbioru na instalacji.
- Wymagane rozszerzenie raportów i pełny restart HA po aktualizacji; konfiguracja i rozszerzenie Z2M nie wymagają zmiany.

## 0.1.0b2 — poprawka ponownego włączania

- Usunięto blokadę kolejnego ON po wcześniejszym potwierdzonym ON i wyłączeniu z HA lub fizycznego przycisku. Zakończona komenda nie jest już traktowana jako oczekująca.
- Potwierdzone jawne OFF pozwala rozpocząć nową operację ON po wcześniejszym timeout. Nie dodano automatycznego ponawiania ON.
- Dodano trzy testy regresji cyklu zasilania i odzyskania po timeout.
- Nie zmieniono konfiguracji ani rozszerzenia Z2M; aktualizacja nie wymaga ponownego dodawania integracji. Po aktualizacji uruchom ponownie HA i ponownie włącz ACTIVE przed próbą.
- Poprawkę zweryfikowano automatycznie; jej działanie na instalacji wymaga ponownego testu po aktualizacji.

## 0.1.0b1 — wersja testowa

- Pełna implementacja dwóch obwodów, opcjonalnej grupy, diagnostyki i preferencji dostępna do instalacji.
- Konfiguracja z katalogu Z2M, automatyczny zakres CCT i poprawna obsługa brightness 0–254.
- Opcjonalne rozszerzenie Z2M dla rzeczywistych raportów ZCL, z izolacją sesji i endpointów.
- Uruchomienie w OBSERVE, jawne ACTIVE i brak odtwarzania dawnych ON.
- Instrukcja instalacji i lista prób sprzętowych. Pomiary, fizyczny odbiór i rollback na instalacji pozostają do wykonania.
