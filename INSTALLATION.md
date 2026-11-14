# Instalacja wersji testowej

## 1. Dodaj integrację do Home Assistanta

Wymagane: Home Assistant 2026.11.0 lub zgodny z przetestowanym API, skonfigurowana integracja MQTT oraz Zigbee2MQTT publikujący katalog `bridge/devices` i `bridge/groups`. Testy prowadzono na HA 2026.11.0 i API Z2M 2.14.3; nowsze i starsze wydania wymagają sprawdzenia.

**HACS:** w menu HACS otwórz własne repozytoria, dodaj adres tego repozytorium, wybierz kategorię Integration. Zainstaluj wydanie testowe (w razie potrzeby włącz wersje beta). Uruchom ponownie HA.

**Ręcznie:** z pobranego wydania skopiuj katalog `custom_components/zigbee_led_orchestrator` do `<config HA>/custom_components/zigbee_led_orchestrator`. W środku mają być bezpośrednio `manifest.json`, `__init__.py` i pozostałe pliki. Uruchom ponownie HA.

Dodaj **Zigbee LED Orchestrator** w Ustawienia → Urządzenia i usługi. Jeśli nie ma go na liście, odśwież przeglądarkę i sprawdź log HA po restarcie.

## 2. Zmapuj obwody

Wpisz rzeczywisty base topic Z2M. Integracja pasywnie czeka do 10 sekund na katalog. Brak katalogu oznacza konieczność sprawdzenia integracji MQTT, base topic i publikowania retained metadanych przez Z2M.

- Instalacja pyta o temat bazowy i wspólne czasy. Nie wybiera się tu kontrolera ani grupy.
- Obwód dodasz w **Konfiguruj → Dodaj obwód**: nazwa wirtualnej encji, jeden kontroler LED i jego kanał przekaźnika. Wybory wskazują nazwy oraz IEEE i endpointy. Numer kanału nie dowodzi jego fizycznego okablowania — sprawdź je na miejscu. Zakres temperatury pochodzi z exposes kontrolera. Zajęta nazwa, ten sam endpoint LED albo ten sam kanał przekaźnika są odrzucane. Zapis przeładowuje wpis i wraca do trybu obserwacji. Nowy obwód nie wchodzi sam do grupy.
- Grupę utworzysz w **Konfiguruj → Utwórz grupę**, gdy są co najmniej dwa obwody spoza innych grup. Podajesz nazwę, na przykład `Salon`, i zaznaczasz, które z dostępnych obwodów mają do niej należeć. Powstaje grupa Zigbee2MQTT i encja światła `Salon_orchestrator`. Integracja dopina wybrane endpointy, gdy ich przekaźniki są włączone, i ustawia `optimistic: false` oraz `retain: false`. Cudzych grup nie zmienia. Encję discovery Z2M o tej samej nazwie wyłącz w Home Assistantcie, żeby automatyzacje nie omijały encji integracji. Obwód może należeć tylko do jednej grupy.
- Czasy formularza są parametrami początkowymi do prób, nie wynikami pomiarów. Dostosuj boot grace i timeout gotowości do pomiarów. Sam boot grace nie odblokowuje wysyłki bez raportu.

Dla przygotowania samej konfiguracji można zostawić rozszerzenie raportów wyłączone. Do prób aktywnych w pierwszym kroku zaznacz **Użyj rozszerzenia raportów Zigbee2MQTT**. Wyboru dokonuje się przy dodawaniu integracji; aby zmienić mapowanie lub ten wybór, zanotuj konfigurację, usuń wpis i dodaj go ponownie. Usunięcie wpisu usuwa jego encje i nie wysyła do urządzeń polecenia OFF.

Sam wpis integracji nie tworzy jeszcze encji światła. Każdy dodany obwód tworzy encję o podanej nazwie i diagnostykę phase/effective_state/confirmed. Grupa pojawia się dopiero po jej utworzeniu. W OBSERVE encje sterujące są niedostępne — jest to oczekiwane. Diagnostyka pozostaje dostępna.

## 3. Przygotuj źródło rzeczywistych raportów

Standardowe MQTT Z2M może zawierać stan z cache i optymistyczną odpowiedź. Wersja testowa używa osobnego, opcjonalnego rozszerzenia z pliku [z2m/orchestrator-reports.mjs](z2m/orchestrator-reports.mjs). Rozszerzenie nie ustawia urządzeń i nie wykonuje odczytów radiowych. Przekazuje tylko odebrane `attributeReport` i `readResponse` dla endpointów wskazanych przez integrację.

1. Zabezpiecz konfigurację Z2M i możliwość jej przywrócenia. Sprawdź [oficjalną instrukcję external extensions](https://www.zigbee2mqtt.io/advanced/more/external_extensions.html). Jeśli zewnętrzny JS jest wyłączony, jego włączenie jest osobną decyzją administratora; repozytorium nie włącza tej opcji automatycznie.
2. W Z2M otwórz sekcję zewnętrznych rozszerzeń, dodaj plik `orchestrator-reports.mjs` z tego wydania i zapisz jego zawartość. Alternatywnie umieść go w katalogu danych Z2M `external_extensions/` zgodnie z oficjalną instrukcją. Nie zastępuj innych rozszerzeń.
3. W HA dodaj integrację z zaznaczonym rozszerzeniem albo użyj usługi `zigbee_led_orchestrator.resync` po załadowaniu rozszerzenia. Jako `config_entry_id` wybierz wpis integracji w formularzu usługi.
4. Pobierz diagnostykę wpisu lub użyj `zigbee_led_orchestrator.diagnose`. Sprawdź `report_extension_enabled: true`, `report_extension_ready: true` i `bridge_online: true`.

Integracja wysyła w OBSERVE wyłącznie metadane sesji na `<base>/orchestrator/subscribe`; rozszerzenie odpowiada na `<base>/orchestrator/report`. Żadna z tych wiadomości nie jest `/set`, `/get` ani odczytem Zigbee. Nonce sesji i numer kolejny odrzucają poprzednie sesje, duplikaty i retained raporty. Luka w sekwencji może ukrywać fizyczne OFF: integracja wstrzymuje wtedy sterowanie, czyści dowody i wymaga resync oraz ponownego ACTIVE. Kanał dowodowy powinien być dostępny do publikacji tylko zaufanemu Z2M; broker i jego uprawnienia są granicą zaufania.

Obsługiwane raporty: `genOnOff.onOff`, `genLevelCtrl.currentLevel`, `lightingColorCtrl.colorTemperature`. Jeśli urządzenie używa innego protokołu, raportów może brakować: pozostaw OBSERVE i zachowaj zredagowaną diagnostykę. Nie zastępuj potwierdzenia trybem optimistic. Raport urządzenia nie jest pomiarem emisji światła.

## 4. Na miejscu włącz próby

Przejdź przez [TESTING.md](TESTING.md). Po sprawdzeniu kopii/rollbacku, mapowania i wyłączeniu konkurujących nadawców sterowanie włącza się samo, gdy mostek jest online i obwody są w katalogu. Włączenie nie odtwarza wcześniejszego ON. Po restarcie HA, powrocie mostka albo resync nie trzeba włączać go ponownie. Jawne wyłączenie w **Konfiguruj → Tryb sterowania** zostaje wyłączone, dopóki nie włączysz go ręcznie.

Od wersji 0.1.0b5, przy włączonym rozszerzeniu raportów, nowe ON samo odczytuje stan nieznanego przekaźnika. Po potwierdzonym zasileniu LED-a i minimalnym czasie rozruchu może wykonać jeden odczyt gotowości, a po ustawieniu parametrów jeden odczyt potwierdzający. Nie trzeba wcześniej ręcznie przełączać ani odświeżać urządzeń w Z2M. Odczyty idą przez arbiter i MQTT HA, bez retain; OFF, zmiana sesji i celu anulują przyszłe nieaktualne operacje. Brak odpowiedzi kończy się timeoutem, bez pętli odczytów lub ponowień `/set`. Czas rozruchu i timeouty nadal wymagają pomiaru dla konkretnego kontrolera. Bez rozszerzenia zwykłe MQTT pozostaje niewiarygodnym źródłem potwierdzenia.

Po aktualizacji **uruchom ponownie cały Home Assistant** i odśwież kartę przeglądarki. Samo pobranie plików nie wystarcza do wymiany załadowanego kodu. Usługa `zigbee_led_orchestrator.diagnose` zwraca `runtime_version`; dla tego wydania musi pokazać `0.1.4`. Sterowanie włącza się samo. Żadne stare ON nie jest odtwarzane. Nie usuwaj wpisu integracji ani rozszerzenia Zigbee2MQTT.

## Powrót do poprzedniego sterowania

Zapisz przed próbą stany włączenia automatyzacji i dokładną listę wyłączonych ścieżek. W razie problemu użyj `pause` (wstrzymuje publikacje; nie odcina fizycznie zasilania) lub wyłącz aktywne sterowanie. Zweryfikuj zakończenie komend już w locie, aktualny stan i brak dalszych `/set`. W razie potrzeby użyj fizycznego wyłącznika. Następnie przywróć wyłącznie ścieżki, które były wcześniej aktywne. Samo odinstalowanie integracji nie przywraca automatyzacji ani nie cofa nadanych komend.

Usunięcie rozszerzenia Z2M zatrzymuje przekazywanie raportów. Najpierw wstrzymaj integrację HA; rozszerzenie nie wymaga zmiany parametrów urządzeń.


## Programowe OFF przed odcięciem zasilania

Przy włączonym rozszerzeniu raportów i potwierdzonym zasilaniu wyłączenie wirtualnego światła najpierw wyłącza kontroler LED, odczytuje jego stan i czeka na rzeczywiste potwierdzenie OFF. Następnie odcina przekaźnik. Oczekiwanie na etap LED jest ograniczone parametrem verification_timeout_ms (domyślnie 2 s); w razie braku odpowiedzi przekaźnik i tak jest wyłączany, a reason opisuje brak potwierdzenia LED. Przyciski fizyczne zachowują bezpośrednie odcięcie zasilania.

Ta kolejność pozwala kontrolerowi zapamiętać OFF, ale nie ustawia `do_not_disturb` ani nie gwarantuje, że firmware pozostanie wyłączony po przywróceniu napięcia. Zachowanie startowe należy sprawdzić na urządzeniu; po nagłym fizycznym odcięciu przy ON problem błysku może pozostać.
