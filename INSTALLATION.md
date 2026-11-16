# Instalacja wersji testowej

## 1. Dodaj integrację do Home Assistanta

Wymagane: Home Assistant 2026.11.0 lub zgodny z przetestowanym API, skonfigurowana integracja MQTT oraz Zigbee2MQTT publikujący katalog `bridge/devices` i `bridge/groups`. Testy prowadzono na HA 2026.11.0 i API Z2M 2.14.3; nowsze i starsze wydania wymagają sprawdzenia.

**HACS:** w menu HACS otwórz własne repozytoria, dodaj adres tego repozytorium, wybierz kategorię Integration. Zainstaluj wydanie testowe (w razie potrzeby włącz wersje beta). Uruchom ponownie HA.

**Ręcznie:** z pobranego wydania skopiuj katalog `custom_components/zigbee_led_orchestrator` do `<config HA>/custom_components/zigbee_led_orchestrator`. W środku mają być bezpośrednio `manifest.json`, `__init__.py` i pozostałe pliki. Uruchom ponownie HA.

Dodaj **Zigbee LED Orchestrator** w Ustawienia → Urządzenia i usługi. Jeśli nie ma go na liście, odśwież przeglądarkę i sprawdź log HA po restarcie.

## 2. Zmapuj obwody

Wpisz rzeczywisty base topic Z2M. Integracja pasywnie czeka do 10 sekund na katalog. Brak katalogu oznacza konieczność sprawdzenia integracji MQTT, base topic i publikowania retained metadanych przez Z2M.

- Instalacja pyta o temat bazowy i wspólne czasy. Nie wybiera się tu kontrolera ani grupy.
- Obwód dodasz w **Konfiguruj → Dodaj obwód**: nazwa wirtualnej encji, jeden kontroler LED i jego kanał przekaźnika. Nazwa `Kuchnia` tworzy encję `Kuchnia_orchestrator`. Wpisanie końcówki drugi raz jej nie podwaja. Wybory wskazują nazwy oraz IEEE i endpointy. Numer kanału nie dowodzi jego fizycznego okablowania — sprawdź je na miejscu. Zakres temperatury pochodzi z exposes kontrolera. Zajęta nazwa, ten sam endpoint LED albo ten sam kanał przekaźnika są odrzucane. Zapis przeładowuje wpis i wraca do trybu obserwacji. Nowy obwód nie wchodzi sam do grupy. Już zapisane obwody zostają przy dotychczasowych nazwach.
- Grupę utworzysz w **Konfiguruj → Utwórz grupę**, gdy jest co najmniej jeden obwód spoza innych grup. Podajesz nazwę, na przykład `Salon`, i zaznaczasz, które z dostępnych obwodów mają do niej należeć. Powstaje grupa Zigbee2MQTT i encja światła `Salon_orchestrator`. Integracja dopina wybrane endpointy, gdy ich przekaźniki są włączone, i ustawia `optimistic: false` oraz `retain: false`. Cudzych grup nie zmienia. Encję discovery Z2M o tej samej nazwie wyłącz w Home Assistantcie, żeby automatyzacje nie omijały encji integracji. Obwód może należeć tylko do jednej grupy.
- Czasy formularza są parametrami początkowymi do prób, nie wynikami pomiarów. Dostosuj boot grace i timeout gotowości do pomiarów. Sam boot grace nie odblokowuje wysyłki bez raportu.

Dla przygotowania samej konfiguracji można zostawić rozszerzenie raportów wyłączone. Do prób aktywnych w pierwszym kroku zaznacz **Użyj rozszerzenia raportów Zigbee2MQTT**. Wyboru dokonuje się przy dodawaniu integracji; aby zmienić mapowanie lub ten wybór, zanotuj konfigurację, usuń wpis i dodaj go ponownie. Usunięcie wpisu usuwa jego encje i nie wysyła do urządzeń polecenia OFF.

Sam wpis integracji nie tworzy jeszcze encji światła. Każdy dodany obwód tworzy encję o podanej nazwie i diagnostykę phase/effective_state/confirmed. Grupa pojawia się dopiero po jej utworzeniu. W OBSERVE encje sterujące są niedostępne — jest to oczekiwane. Diagnostyka pozostaje dostępna.

## 3. Przygotuj źródło rzeczywistych raportów

Standardowe MQTT Z2M może zawierać stan z cache i optymistyczną odpowiedź. Wersja testowa używa osobnego, opcjonalnego rozszerzenia z pliku [z2m/orchestrator-reports.mjs](z2m/orchestrator-reports.mjs). Rozszerzenie nie ustawia urządzeń i nie wykonuje odczytów radiowych. Przekazuje tylko odebrane `attributeReport` i `readResponse` dla endpointów wskazanych przez integrację.

1. Zabezpiecz konfigurację Z2M i możliwość jej przywrócenia. Sprawdź [oficjalną instrukcję external extensions](https://www.zigbee2mqtt.io/advanced/more/external_extensions.html). Jeśli zewnętrzny JS jest wyłączony, jego włączenie jest osobną decyzją administratora; repozytorium nie włącza tej opcji automatycznie.
2. W Z2M otwórz sekcję zewnętrznych rozszerzeń, dodaj plik `orchestrator-reports.mjs` z tego wydania i zapisz jego zawartość. Alternatywnie umieść go w katalogu danych Z2M `external_extensions/` zgodnie z oficjalną instrukcją. Nie zastępuj innych rozszerzeń.
3. W HA dodaj integrację z zaznaczonym rozszerzeniem albo użyj usługi `zigbee_led_orchestrator.resync` po załadowaniu rozszerzenia. Jako `config_entry_id` wybierz wpis integracji w formularzu usługi.
4. Pobierz diagnostykę wpisu lub użyj `zigbee_led_orchestrator.diagnose`. Sprawdź `report_extension_enabled: true`, `report_extension_ready: true` i `bridge_online: true`.

Integracja wysyła w OBSERVE wyłącznie metadane sesji na `<base>/orchestrator/subscribe`; rozszerzenie odpowiada na `<base>/orchestrator/report`. Żadna z tych wiadomości nie jest `/set`, `/get` ani odczytem Zigbee. Nonce sesji i numer kolejny odrzucają poprzednie sesje, duplikaty i retained raporty. Luka w sekwencji może ukrywać fizyczne OFF: integracja wstrzymuje wtedy sterowanie, czyści dowody i wymaga resync. Gdy mostek i katalog wrócą, sterowanie włącza się samo. Kanał dowodowy powinien być dostępny do publikacji tylko zaufanemu Z2M; broker i jego uprawnienia są granicą zaufania.

Obsługiwane raporty: `genOnOff.onOff`, `genLevelCtrl.currentLevel`, `lightingColorCtrl.colorTemperature`. Jeśli urządzenie używa innego protokołu, raportów może brakować: pozostaw OBSERVE i zachowaj zredagowaną diagnostykę. Nie zastępuj potwierdzenia trybem optimistic. Raport urządzenia nie jest pomiarem emisji światła.

## 4. Na miejscu włącz próby

Przejdź przez [TESTING.md](TESTING.md). Po sprawdzeniu kopii/rollbacku, mapowania i wyłączeniu konkurujących nadawców sterowanie włącza się samo, gdy mostek jest online i obwody są w katalogu. Włączenie nie odtwarza wcześniejszego ON. Po restarcie HA, powrocie mostka albo resync włącza się samo. Ręczne zatrzymanie jest w usłudze `pause` i wraca przez `resume`.

Od wersji 0.1.0b5, przy włączonym rozszerzeniu raportów, nowe ON samo odczytuje stan nieznanego przekaźnika. Po potwierdzonym zasileniu LED-a i minimalnym czasie rozruchu może wykonać jeden odczyt gotowości, a po ustawieniu parametrów jeden odczyt potwierdzający. Nie trzeba wcześniej ręcznie przełączać ani odświeżać urządzeń w Z2M. Odczyty idą przez arbiter i MQTT HA, bez retain; OFF, zmiana sesji i celu anulują przyszłe nieaktualne operacje. Brak odpowiedzi kończy się timeoutem, bez pętli odczytów lub ponowień `/set`. Czas rozruchu i timeouty nadal wymagają pomiaru dla konkretnego kontrolera. Bez rozszerzenia zwykłe MQTT pozostaje niewiarygodnym źródłem potwierdzenia.

Po aktualizacji **uruchom ponownie cały Home Assistant** i odśwież kartę przeglądarki. Samo pobranie plików nie wystarcza do wymiany załadowanego kodu. Usługa `zigbee_led_orchestrator.diagnose` zwraca `runtime_version`; dla tego wydania musi pokazać `0.3.6`. Sterowanie włącza się samo. Żadne stare ON nie jest odtwarzane. Nie usuwaj wpisu integracji ani rozszerzenia Zigbee2MQTT.

## Ograniczenie komend i diagnostyka (0.3.6)

Zmiany jasności/CCT czekają na rozstrzygnięcie poprzedniej operacji na tym kontrolerze. Pozostaje tylko najnowszy cel; brak potwierdzenia zatrzymuje zwykłe wysyłki także po timeoutcie. Nowy wiarygodny raport może wznowić realizację celu. Identyczne OFF nie powtarza LED OFF ani nierozstrzygniętego odcięcia. Kolejność LED OFF → przekaźnik pozostaje, z dotychczasowym limitem 2 s dla etapu LED. Faktyczne odcięcie może trwać dłużej przez komendy już przyjęte przez Z2M.

Usługa `zigbee_led_orchestrator.diagnose` zwraca `events` (do 1024 wpisów), `events_dropped`, `captured_at_unix` i `monotonic_time`. Bufor jest w pamięci, wspólny dla wpisu integracji i znika po restarcie. Pobierz diagnostykę od razu po próbie; przy dużym ruchu starsze zdarzenia wypadają. Dla porównania z logami czas zdarzenia w Unix to `captured_at_unix + event.time - monotonic_time`. Ślad pokazuje przyjęcie celu, publikację MQTT, raporty i timeouty, ale sam nie dowodzi chwili fizycznej emisji światła.

Odbiór sprzętowy wykonuje operator po aktualizacji: osobno obwody, grupa pokoju i nadrzędne „wszystkie światła”, w obu trybach gotowości. Zapisz diagnostykę po sekwencji jasności 255 → 56 → 191 → 56, zmianie barwy podczas oczekiwania oraz pojedynczym i powtarzanym OFF. Porównaj czasy publikacji, odpowiedzi urządzeń oraz faktyczne światło. Grupy HA zachowują swoje ustawienia; ich wskaźnik może pokazywać średnią aktualizowanych członków.

## Późna gotowość i synchronizacja grup (0.3.5)

**Konfiguruj → Gotowość grup** zawiera dwa tryby wspólne dla grup tego wpisu:

- **Po limicie: niezależne obwody i późne dołączanie** (domyślny): przez ustawiony limit bariery integracja próbuje wykonać wspólny start. Potem gotowi członkowie dostają osobne komendy. Spóźniony członek nadal czeka na dowód gotowości i stosuje najnowszy cel indywidualnie.
- **Czekaj na wszystkich: wspólna komenda grupowa**: integracja czeka na wszystkich tak długo, jak ważny jest cel i zasilanie, po czym wysyła jeden multicast. Limit bariery nie kończy tego oczekiwania. Brak raportu jednego kontrolera może wstrzymać całą grupę bezterminowo. Błąd potwierdzenia zasilania członka kończy wspólną operację błędem.

Po przekroczeniu readiness_timeout_ms obwód pozostaje w WAITING_READY z reason=readiness_delayed. Oczekiwanie reaguje na wiarygodne raporty i nie generuje pętli odczytów ani ponowień /set. Jeśli raport nigdy nie nadejdzie, cel pozostanie niezrealizowany; stan ON encji oznacza zamiar, a nie potwierdzone światło. Timeout potwierdzenia przekaźnika nadal jest błędem, a nie zgodą na ponowienie ON.

Zmiana celu, OFF, fizyczne odcięcie, pauza, rozłączenie i przeładowanie przerywają odpowiednie stare oczekiwanie. Późna gotowość nie odtwarza ON z wcześniejszej sesji. Zmiana jasności/CCT podczas oczekiwania zastępuje parametry celu. Zmiana celu pojedynczego członka rozwiązuje wspólną operację, a pozostali realizują swoje aktualne cele indywidualnie. Wspólna komenda wymaga identycznych docelowych payloadów członków; różne zapamiętane jasności/CCT również powodują obsługę indywidualną. Zapis ustawień przeładowuje integrację bez odtwarzania starego ON. Pola group_readiness_strategy i group_barrier_timeout_ms są dostępne w diagnose.

## Powrót do poprzedniego sterowania

Zapisz przed próbą stany włączenia automatyzacji i dokładną listę wyłączonych ścieżek. W razie problemu użyj `pause` (wstrzymuje publikacje; nie odcina fizycznie zasilania). Zweryfikuj zakończenie komend już w locie, aktualny stan i brak dalszych `/set`. W razie potrzeby użyj fizycznego wyłącznika. Następnie przywróć wyłącznie ścieżki, które były wcześniej aktywne. Samo odinstalowanie integracji nie przywraca automatyzacji ani nie cofa nadanych komend.

Usunięcie rozszerzenia Z2M zatrzymuje przekazywanie raportów. Najpierw wstrzymaj integrację HA; rozszerzenie nie wymaga zmiany parametrów urządzeń.


## Programowe OFF przed odcięciem zasilania

Przy włączonym rozszerzeniu raportów i potwierdzonym zasilaniu wyłączenie wirtualnego światła najpierw wyłącza kontroler LED, odczytuje jego stan i czeka na rzeczywiste potwierdzenie OFF. Następnie odcina przekaźnik. Oczekiwanie na etap LED jest ograniczone parametrem verification_timeout_ms (domyślnie 2 s); w razie braku odpowiedzi przekaźnik i tak jest wyłączany, a reason opisuje brak potwierdzenia LED. Przyciski fizyczne zachowują bezpośrednie odcięcie zasilania.

Ta kolejność pozwala kontrolerowi zapamiętać OFF, ale nie ustawia `do_not_disturb` ani nie gwarantuje, że firmware pozostanie wyłączony po przywróceniu napięcia. Zachowanie startowe należy sprawdzić na urządzeniu; po nagłym fizycznym odcięciu przy ON problem błysku może pozostać.


## Wiele grup i przerwane zmiany

Nie ma ograniczenia do dwóch obwodów i jednej grupy. Dodawaj kolejne pary oraz grupy przez menu Konfiguruj. Każdy obwód może mieć osobny przekaźnik i kontroler w dowolnym miejscu; każda grupa ma własne światło HA i grupę Z2M. W jednej grupie może być jeden lub więcej obwodów. Obwód należy najwyżej do jednej grupy. Pozostają fizyczne ograniczenia pojemności Zigbee i urządzeń.

Synchronizacja członkostwa korzysta z osobnych wskazówek zasilania, bez wymagania surowego raportu rozszerzenia. Wskazówki nie dopuszczają komend świetlnych: `/set` nadal wymaga aktualnych dowodów gotowości. Synchronizacja nie włącza zasilania; niedostępny kontroler dołączy po ponownym zasileniu i raporcie. Brak lub odmowa jednej grupy nie zatrzymuje pozostałych; nieudana synchronizacja ma odstęp 10 sekund przed ponownym uzgodnieniem katalogu. Ręczne grupy i zajęte nazwy nie są przejmowane.

Zmiana kontrolera w grupie zachowuje identyfikatory encji i numer grupy Z2M. Zapewnij zasilanie obu kontrolerów na czas przeniesienia; integracja sama go nie włączy. Konfiguracja jest zapisywana po potwierdzeniu odpowiedzi i katalogu. Usunięcie grupy wymaga potwierdzenia Z2M, nigdy nie używa `force` i zostawia obwody skonfigurowane osobno.

Przerwane edycje i usuwanie zachowują poprzednią konfigurację oraz postęp także po restarcie. Jeśli wynik wcześniejszego żądania jest nieznany, sprawdź grupę i zakończenie żądań w Z2M (w razie potrzeby po restarcie Z2M). Wybierz **Konfiguruj → Dokończ operację grupy** i potwierdź sprawdzenie. Formularz przyjmie wykonaną zmianę bez powtórnej publikacji albo wykona brakujący krok. Zmieniony identyfikator, zajęta nazwa lub obce endpointy pozostawiają blokadę. Stare ON nigdy nie jest odtwarzane.
