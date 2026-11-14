# Changelog

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
