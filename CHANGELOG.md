# Changelog

## 0.1.3 — włączenie sterowania po odświeżeniu katalogu

- Ponowne wysłanie katalogu urządzeń przez Zigbee2MQTT (anons, interview, zmiana adresu sieciowego) traktowało niezmienione obwody jak nową topologię. Jeśli silnik już istniał, kolejna próba ACTIVE kończyła się ogólnym komunikatem i sterowanie zostawało wyłączone.
- Identyczny katalog nie odbiera uprawnień i nie odtwarza starego ON. Rzeczywista zmiana składu albo brak urządzenia nadal wymaga resync i nie włącza ACTIVE.
- Nieudane włączenie, na przykład przy offline mostka albo niegotowym rozszerzeniu raportów, nie zostawia silnika. Następne odświeżenie katalogu nie blokuje sterowania na stałe.
- Formularz trybu podaje konkretną przyczynę: zmieniona topologia, niezgodne mapowanie, integracja nieuruchomiona, mostek offline albo niegotowe rozszerzenie raportów.
- To zwykłe wydanie, bez przyrostka. Po aktualizacji uruchom ponownie cały Home Assistant i odśwież kartę przeglądarki. `zigbee_led_orchestrator.diagnose` ma pokazać `runtime_version: 0.1.3`. Potem włącz ACTIVE jeszcze raz.

## 0.1.2 — podpisy w menu konfiguracji

- Zębatka otwiera menu z widocznymi pozycjami: Dodaj obwód, Utwórz grupę, Tryb sterowania. Napisy jadą w odpowiedzi kroku, więc pusta pamięć tłumaczeń nie zostawia samych strzałek.
- To zwykłe wydanie, bez przyrostka beta. Po aktualizacji uruchom ponownie cały Home Assistant i odśwież kartę przeglądarki. `zigbee_led_orchestrator.diagnose` ma pokazać `runtime_version: 0.1.2`.

## 0.1.0b7 — obwody i grupy po instalacji

- Instalacja łączy tylko z Zigbee2MQTT. Nie pyta o kontroler ani o grupę.
- Obwód dodaje się później w **Konfiguruj → Dodaj obwód**. Nowy obwód nie wchodzi sam do żadnej grupy.
- Grupę tworzy się w **Konfiguruj → Utwórz grupę**: podajesz nazwę i zaznaczasz, które już dodane obwody mają do niej należeć. Nazwa `Salon` tworzy `Salon_orchestrator`. Potrzeba co najmniej dwóch wolnych obwodów. Obwód będący już w innej grupie nie pojawia się na liście.
- Zapis obwodu albo grupy przeładowuje wpis i wraca do OBSERVE. ACTIVE trzeba włączyć ponownie.
- Po aktualizacji uruchom ponownie cały Home Assistant. `zigbee_led_orchestrator.diagnose` ma pokazać `runtime_version: 0.1.0b7`.

## 0.1.0b6 — obwody po jednej parze

- Pierwszy formularz zapisuje jedną parę: nazwę wirtualnej encji, kontroler LED i kanał przekaźnika. Kolejne pary dopisuje się w **Konfiguruj → Dodaj obwód**, bez drugiej instancji integracji.
- Nazwa grupy zostaje opcjonalna. Puste pole zostawia sterowanie indywidualne. Nazwa `Salon` tworzy grupę i encję `Salon_orchestrator`. Każdy później dodany obwód dołącza do tej grupy.
- Zapis nowego obwodu przeładowuje wpis i wraca do OBSERVE. ACTIVE trzeba włączyć ponownie. Zajęta nazwa, kontroler albo kanał są odrzucane.
- Stary wpis bez pola `name` nadal się ładuje; encja dostaje podpis z identyfikatora, na przykład `light_1` → `Light 1`.
- Po aktualizacji uruchom ponownie cały Home Assistant. `zigbee_led_orchestrator.diagnose` ma pokazać `runtime_version: 0.1.0b6`.

## 0.1.0b5 — własne grupy Zigbee2MQTT

- W mapowaniu jest opcjonalna nazwa grupy. Puste pole zostawia sterowanie indywidualne. Nazwa `Salon` tworzy encję światła i grupę Zigbee2MQTT `Salon_orchestrator`.
- Integracja sama wybiera wolny identyfikator, wyłącza `optimistic` i `retain` oraz dopina dokładnie dwa endpointy LED przy wiarygodnym raporcie ON ich przekaźników. Cudzych grup nie zmienia.
- Komenda grupowa idzie tylko przy dokładnym składzie. Przy niepełnym składzie intencja jest zapisywana, a sterowanie idzie do członków osobno. Stan tematu grupy nie zmienia intencji.
- Po aktualizacji uruchom ponownie Home Assistant i włącz ACTIVE. Encję discovery Zigbee2MQTT o tej samej nazwie wyłącz, żeby automatyzacje nie omijały encji integracji.

## 0.1.0b4 — potwierdzone LED OFF

- Programowe wyłączenie zasilonego obwodu najpierw wysyła LED OFF i odczytuje stan, a po rzeczywistym potwierdzeniu odcina przekaźnik.
- Brak odpowiedzi lub błąd kontrolera nie blokuje wyłączenia: etap LED ma ograniczony czas oczekiwania, a diagnostyka zachowuje przyczynę niepotwierdzenia.
- Nowe ON anuluje przyszłe stare odcięcie; zmiany parametrów nie mnożą LED OFF i nie przedłużają oczekiwania. Preferencje jasności/CCT pozostają zachowane.
- Nie zmieniono ustawień startowych kontrolerów; skuteczność zapobiegania błyskowi wymaga sprawdzenia firmware na instalacji.

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
