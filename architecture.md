# Architektura Zigbee LED Orchestrator

Ten dokument opisuje architekturę repozytorium i planowany kierunek integracji. Bieżący stan zadań znajduje się w [rejestrze postępu](docs/superpowers/plans/PROGRESS.md), a szczegółowe kontrakty i kroki w [planie implementacji](docs/superpowers/plans/2026-11-13-zigbee-led-orchestrator.md). Przed zmianą sprawdź kod: opis docelowy nie oznacza, że dany element już działa.

## Cel i problem

Przekaźnik fizycznie odłącza zasilanie kontrolera LED. W tym czasie ostatni stan MQTT kontrolera może pozostać widoczny, mimo że światło nie działa. Po włączeniu zasilania kontroler potrzebuje czasu na rozruch, a wczesne polecenie może zostać utracone. Integracja ma przyjmować intencje z Home Assistanta, sterować przekaźnikami i LED-ami przez Zigbee2MQTT oraz wykorzystywać natywne grupy Zigbee do wspólnych poleceń.

Pilotaż przewiduje dwa obwody zasilane kanałami jednego przekaźnika oraz jedną grupę. Nazwy, IEEE, endpointy, możliwości urządzeń, wersje oprogramowania i faktyczny skład grupy wymagają potwierdzenia w instalacji; dane w dokumentach planistycznych są założeniami.

## Stan implementacji

Stan opisany przy aktualizacji dokumentu: **P00–P13 zakończone w zakresie syntetycznym; wersja 0.1.0b1 przygotowana do instalacji i prób**. Kod obejmuje pełny rdzeń domenowy, adapter MQTT, katalog topologii, config flow, runtime lifecycle, encje light, koordynator grup z barierą gotowości oraz preferencje, diagnostykę i usługi serwisowe. `__init__.py` inicjuje lifecycle config entry. Instalacja przez HACS i zielone testy nie oznaczają gotowości do sterowania instalacją — aktywny pilotaż wymaga P14.

| Element | Odpowiedzialność | Stan |
|---|---|---|
| `models.py`, `const.py`, `protocols.py` | Typy stanu, kontrakty zegara i transportu | Istnieją |
| `configuration.py` | Walidacja modelu konfiguracji bez zależności od HA | Istnieje; UI dodaje obwody po jednej parze i opcjonalną grupę |
| `desired_store.py` | Aktualna intencja, atomowe aktualizacje i numer `generation` | Istnieje |
| `observation_store.py` | Raporty przekaźnika/LED/mostka, jakość danych, `power_epoch` i `bridge_epoch` | Istnieje |
| `command_arbiter.py` | Aktualność poleceń, priorytet OFF, ograniczanie i koordynacja wysyłek, kwarantanna niepewnej publikacji | Istnieje; integracja z rzeczywistym HA MQTT jest do sprawdzenia |
| `power_manager.py` | Konserwatywna ocena gotowości na podstawie danych i minimalnego czasu rozruchu | Istnieje `is_ready`; potwierdzenia i timeouty obsługuje reconciler |
| `state_machine.py`, `reconciler.py` | Fazy obwodu i uzgadnianie intencji z obserwacjami | Istnieje reconciler pojedynczego obwodu z obsługą zewnętrznych zmian, ograniczoną korelacją wysyłek i przerwania połączenia; adapter musi przekazywać zmiany sesji i weryfikować źródło raportów |
| `mqtt_adapter.py`, `topology.py` | Połączenie przez MQTT HA i weryfikacja topologii Z2M | Istnieją; wymagają pełnej weryfikacji formatów Z2M w rzeczywistej instalacji |
| `runtime.py`, `config_flow.py`, `light.py` | Cykl życia integracji, konfiguracja, encje light (pojedyncze i grupowa) | Istnieją i przetestowane syntetycznie; encja grupy wylicza stan z celów członków |
| `preferences.py` | Wersjonowany zapis wyłącznie jasności i CCT (nigdy zasilania, tokenów ani kolejek); ścisła walidacja, uszkodzony zapis → puste preferencje i `preferences_error` | Istnieje (P12); moduł nie importuje HA poza `PreferencesStore` |
| `metrics.py` | Ograniczone liczniki (komendy, akcje, timeouty, błędy, konflikty, anulowania) i p50/p95 rozruchu osobno dla typu dowodu | Istnieje (P12) |
| `sensor.py`, `binary_sensor.py`, `entity.py`, `diagnostics.py` | Diagnostyczne encje phase / effective_state / confirmed (dostępne także bez sterowania) i zredagowany eksport diagnostyczny | Istnieją (P12) |
| `services.py`, `services.yaml` | Usługi `pause`, `resume`, `resync`, `diagnose` adresowane przez `config_entry_id`; żadna nie publikuje do urządzeń | Istnieją (P12) |
| `group_coordinator.py` | Bariera gotowości grupy i pojedynczy `GROUP_SET` dla w pełni gotowych członków | Istnieje (P11), testowany syntetycznie |
| `room_coordinator.py` | Operacje pokojowe | Planowane w P15 |
| `tests/helpers/`, `tests/unit/` | Symulator zegara/transportu i testy rdzenia | Istnieją; nie zastępują testów sprzętowych |
| `tests/scenarios/`, `tests/fixtures/z2m/` | Scenariusze całego config entry przez atrapę HA MQTT; adapter modelowany w harnessie podnosi raporty nieretained do `DEVICE_REPORT` | Istnieją (P13); wynik to MVP lokalne, nie dowód na instalacji |

Nazwy przyszłych modułów są planem, nie publicznym API gotowym do użycia. Szczegółowe zakresy i zależności weryfikuj w planie i kodzie.

## Docelowy przepływ sterowania

```text
HA: użytkownik / automatyzacja / encja light
                    │
                    ▼
         intencja → DesiredStateStore
                    │       generation
                    ▼
          Reconciler / StateMachine
              │               ▲
              ▼               │ obserwacje i jakość
       PowerManager    ObservationStore
              │               ▲
              ▼               │
      GroupCoordinator   MQTT adapter HA
              │               ▲
              ▼               │
         CommandArbiter ──────┴──→ MQTT HA → broker → Zigbee2MQTT
                                                    │
                                      przekaźniki / LED-y / grupy
```

Schemat jest **docelowy**: nie wszystkie połączenia w kodzie już istnieją. Grupowe polecenia przechodzą przez koordynatora grupy, pojedyncze nie muszą. Adapter ma korzystać z publicznych interfejsów integracji MQTT HA; integracja nie tworzy osobnego klienta MQTT ani własnego stosu Zigbee.

1. Intencja określa najnowszy cel, np. ON/OFF, jasność i temperaturę barwową. Zmiana celu dostaje numer `generation`; nie tworzymy kolejki dawnych kliknięć.
2. Reconciler ma na podstawie aktualnej intencji, obserwacji i fazy dobrać kolejną operację. Potwierdza włączenie przekaźnika i czeka na wiarygodne sygnały gotowości kontrolera. Sam upływ czasu lub `online` nie jest takim dowodem.
3. Arbiter ma być jedyną drogą operacyjnej publikacji: ponownie sprawdza aktualność polecenia, ogranicza liczbę wysyłek i nadaje OFF pierwszeństwo. Jeśli wynik nadanej komendy jest nieznany, nie można zakładać, że została cofnięta.
4. Adapter odbiera raporty i przekazuje je do magazynu obserwacji. Wiadomości retained, dane po reconnect i raporty optymistyczne wymagają osobnej oceny; czas odbioru nie jest automatycznie czasem powstania raportu.
5. Przyszłe encje light mają odzwierciedlać aktualny zamiar, a osobna diagnostyka ma pokazywać fazę, wiedzę o stanie i poziom potwierdzenia. Wysłanie MQTT, raport urządzenia i fizyczna emisja światła są różnymi faktami.

## Granice stanów i bezpieczeństwa

- **Desired:** cel użytkownika i jego `generation`; ostatni zaakceptowany cel wygrywa.
- **Observed:** osobne raporty o przekaźniku, kontrolerze i mostku, wraz z jakością i informacją o retained. Nowe `power_epoch` unieważnia wcześniejsze przesłanki gotowości LED-a.
- **Phase:** planowane stany `OFF`, `POWERING`, `WAITING_READY`, `APPLYING`, `ON`, `UNCONFIRMED`, `ERROR`; sama deklaracja enumu nie oznacza ukończenia maszyny stanów.
- **Effective:** `OFF_CONFIRMED`, `ON_REPORTED` lub `UNKNOWN`; `ON_REPORTED` nie dowodzi fizycznej emisji światła.
- **Single writer:** przed aktywnym sterowaniem należy wyeliminować konfliktujących nadawców `/set`; fizyczne przyciski pozostają niezależne. Niespodziewanego fizycznego OFF nie wolno automatycznie cofnąć.
- **Restart i niepewny wynik:** nie odtwarzamy starych intencji ON. Przy nierozstrzygniętej publikacji obecny arbiter blokuje zasób w procesie; pełny restart procesu i gwarancje rzeczywistego transportu wymagają dalszej weryfikacji w P08/P09.
- **Tożsamość:** plan wymaga stabilnych identyfikatorów fizycznych zasobów między przeładowaniami. Nie zastępuj ich zmienną nazwą logiczną; konstrukcja i egzekwowanie tych identyfikatorów wymagają testów podczas integracji.

## Grupy i niezależność obwodów

Bieżąca poprawka N/M usuwa ograniczenia pilotażu w konfiguracji: jeden wpis HA mieści wiele niezależnych grup i dowolną liczbę obwodów z osobnymi przekaźnikami, bez kopiowania silnika. Grupa ma przynajmniej jednego członka; obwód należy najwyżej do jednej grupy. Testy sprawdzają 5 grup po 10 obwodów, synchronizację przez adapter oraz niezależne publikacje przy niegotowej sąsiedniej grupie. Liczba rzeczywistych grup i członków pozostaje ograniczona możliwościami Zigbee/Z2M i urządzeń.

Adapter przechowuje osobne `relay_power_hints` wyłącznie do administracyjnego uzgadniania członkostwa. Nieretained stan ON może uruchomić próbę `group/members/add` bez oczekiwania na surowy DEVICE_REPORT, również przy `report_extension=true`. Nie trafia wtedy do magazynu surowych obserwacji ani do bariery gotowości `/set`. Offline, reconnect i resync czyszczą wskazówki. Komendy świetlne nadal wymagają aktualnych raportów przekaźnika i LED-a oraz czasu rozruchu. Zasilanie nigdy nie jest włączane dla samej synchronizacji.

Nieobecny kontroler pomija swoją grupę w planie utrzymania, pozostawiając pozostałe grupy. Odrzucona lub niepotwierdzona operacja administracyjna czeka 10 sekund przed ponownym uzgodnieniem; inne grupy mogą w tym czasie postępować. Członkostwo potwierdza katalog, nie sama odpowiedź MQTT. Powrót mostka uruchamia synchronizację. Zamknięcie runtime usuwa również zadanie oczekiwania na ponowienie. Operacje `/set` nie są w ten sposób ponawiane.

Docelowo wspólne ON/brightness/CCT jest wysyłane do grupy Zigbee2MQTT tylko, gdy skład został zweryfikowany, wszyscy faktyczni odbiorcy powinni otrzymać polecenie i spełniono barierę gotowości. Odpowiedź grupowa nie jest potwierdzeniem każdego członka. Timeout ma pozwolić na bezpieczną częściową degradację bez niezamierzonego sterowania innymi urządzeniami.

Historyczny pilotaż obejmował dwie lampy i jedną grupę z dedykowanymi kanałami. Bieżący kod obsługuje wiele niezależnych grup; pomieszczenia, nakładające się członkostwo i lampy stale zasilane pozostają kolejnymi etapami. Niezależne zasoby nie czekają na pełny rozruch innego obwodu. Jeden aktywny arbiter obsługuje wspólny wpis HA i instalację Z2M; wiele grup nie wymaga wielu wpisów integracji.

### Preferencje, pauza i resync (P12)

- Zapisywane są tylko jasność i CCT, po zmianie parametrów (opóźniony zapis). Odtworzenie następuje przed jakimkolwiek sterowaniem, w trybie OBSERVE, i nie zmienia `on`.
- `pause` zapamiętuje poprzedni tryb i ustawia żądany stan OFF bez publikacji. `resume` przywraca ten tryb, nie odtwarza starej intencji i nie awansuje OBSERVE do ACTIVE.
- `resync` zamyka silnik i koordynatora, unieważnia dowody i ponownie subskrybuje katalog MQTT; nie publikuje `/set`. Gdy mostek i katalog wrócą, sterowanie włącza się samo i nie odtwarza ON. Pauza zostaje pauzą do `resume`.
- Eksport diagnostyczny zastępuje IEEE, tematy, nazwy i identyfikatory konfiguracji aliasami.

### Bariera grupy (P11)

Integracja utrzymuje wyłącznie grupy, których friendly name kończy się na `_orchestrator`. Nazwa wpisana w konfiguracji jest tą nazwą bez sufiksu; ten sam pełny napis jest nazwą encji light i grupy Z2M. Żądania `bridge/request/group/*` idą, gdy sterowanie nie jest wstrzymane, bez retain i bez tokenów arbitra. `optimistic` i `retain` grupy są wyłączone, więc komenda grupowa nie publikuje wymyślonego stanu członków. Skład jest zmieniany tylko dla endpointu, którego przekaźnik ma wiarygodne ON. Gdy skład katalogu nie jest dokładny, encja grupy jest niedostępna i nie wysyła komend. Obwody, które nadal są w katalogu, steruje się osobno. Stan tematu grupy nie jest obserwacją ani intencją.

`GroupCoordinator` tworzy rezerwację operacji tylko wtedy, gdy po zastosowaniu zamiaru wszyscy członkowie grupy mają cel ON i wszystkim zaczęto nową operację; w przeciwnym razie zamiar trafia do członków indywidualnie. Rezerwacja nie jest blokadą zasobu: członek nadal samodzielnie włącza przekaźnik i czeka na gotowość (reconciler), a decyzję o ostatnim `LED_SET` przekazuje koordynatorowi. Gdy wszyscy są gotowi, a ich docelowe payloady są identyczne po konwersji do zakresów urządzeń, wysyłany jest jeden `GROUP_SET` (zasoby: grupa i LED-y członków, tokeny wszystkich członków). Każda zmiana celu członka, OFF, błąd członka, niezgodność payloadów lub odrzucenie `STALE` przez arbitra kończy rezerwację i zwalnia pozostałych do wysyłek indywidualnych — bez przywracania celu zmienionemu członkowi. Po `group_barrier_timeout_ms` niegotowi członkowie przechodzą w `ERROR` (`group_barrier_timeout`), a gotowi są obsługiwani indywidualnie. Zmiana topologii (`Reconciler.topology_changed`) cofa uprawnienia jak wyłączenie sterowania. Publikacja `GROUP_SET` nie potwierdza członków: stan i `confirmed_members` grupy wynikają z indywidualnych potwierdzeń, a OFF grupy odcina przekaźniki członków; przy kanale rzeczywistych raportów poprzedza je indywidualne LED OFF z ograniczonym potwierdzeniem. Grupy nakładające się i lampy stale zasilane pozostają poza zakresem do P16.

## Testowanie i praca nad zmianami

`tests/helpers/clock.py` oraz `tests/helpers/transport.py` umożliwiają kontrolowanie czasu i wysyłek bez brokera. Ich ograniczenia, w tym wymagania wobec pętli asyncio, opisuje [środowisko deweloperskie](docs/development.md). Testy integracji HA/MQTT i scenariusze sprzętowe pozostają osobnymi etapami.

Przed implementacją porównaj kod z bieżącym zadaniem w planie, dodaj test zachowania i po weryfikacji uzupełnij rejestr. Przy zmianie architektury lub odpowiedzialności modułów zaktualizuj ten dokument i plan, zachowując rozróżnienie między tym, co istnieje, a tym, co dopiero zaplanowano.

Czas `power_off_at` jest czasem odebrania wiarygodnego raportu o przejściu ON → OFF, a nie znaną chwilą fizycznego przełączenia. Pierwszy odczyt OFF po uruchomieniu nie tworzy takiego czasu; przed kolejnym ON stosowany jest zachowawczy odstęp liczony od odebrania tego odczytu. Ponowne raporty OFF nie przesuwają czasu znanego przejścia.

Adapter klasyfikuje zwykłe raporty Z2M jako dane cache lub o nieznanym źródle. Opcjonalna ścieżka surowych raportów z rozszerzenia Z2M opisana poniżej może dostarczyć DEVICE_REPORT; zwykłe wiadomości nigdy nie są do niego awansowane. Weryfikacja grup używa dokładnego zestawu IEEE i endpointów; po zmianie listy urządzeń wymaga ponownego raportu grup. Runtime musi przekazać bieżącą wersję topologii do walidacji arbitra i spiąć obserwacje ze sklepem; sam adapter nie jest jeszcze działającą integracją HA.

`light.py` i `light_capabilities.py` są podłączone do runtime i HA. Zakresy parametrów pochodzą z exposes endpointu; brightness 0–254 daje zakres komend ON 1–254, a OFF jest obsługiwany przez przekaźnik.


## Wersja testowa do instalacji (2026-11-14)

Od 0.1.2 instalacja zapisuje tylko połączenie z Zigbee2MQTT i wspólne czasy. Obwody dopisuje **Konfiguruj → Dodaj obwód**. Od 0.3.2 nazwa nowego obwodu, na przykład Kuchnia, daje encję `Kuchnia_orchestrator`. Grupę tworzy **Konfiguruj → Utwórz grupę**: nazwa, na przykład Salon, daje `Salon_orchestrator`, a skład to obwody zaznaczone w tym formularzu. Później dodany obwód nie dołącza sam. Komenda grupowa wymaga dokładnego składu wybranych endpointów LED. Zakres CCT jest odczytywany z katalogu. Do aktywnych prób służy opcjonalny `report_extension`, domyślnie false; stare wpisy zachowują schemat 1 i konserwatywną klasyfikację zwykłych stanów. Brak pola `name` daje podpis z identyfikatora.

`z2m/orchestrator-reports.mjs` używa eventBus Zigbee2MQTT 2.14.3 do przekazywania wyłącznie surowych attributeReport/readResponse dla genOnOff, genLevelCtrl i lightingColorCtrl. Nie wykonuje komend ani odczytów Zigbee. HA przez swój klient MQTT przesyła metadane sesji na `<base>/orchestrator/subscribe`; extension zwraca potwierdzenie i raporty na `<base>/orchestrator/report`, bez retain. `device_reports.py` sprawdza nonce sesji, sekwencję, dokładny IEEE/endpoint i typy atrybutów; agreguje tylko atrybuty odebrane w bieżącej epoce potwierdzonego zasilania. Po zmianie zasilania/reconnect/resync cache dowodów jest czyszczony. W tym trybie zwykłe scalone MQTT nie nadpisuje dowodów. Publikacje metadanych w OBSERVE nie są operacyjnym `/set` ani `/get`.

Źródłem zaufania jest zainstalowane rozszerzenie i uprawnienia brokera. Nie dowodzi to emisji światła, obsługi innych klastrów, spontanicznych raportów urządzenia ani fizycznego odbioru pilotażu. Instrukcje publiczne znajdują się w INSTALLATION.md i TESTING.md; lokalna dokumentacja instalacji pozostaje ignorowana.

Przy luce w sekwencji kanał dowodów jest blokowany do jawnego resync; utracone OFF nie może zostać ukryte przez następne ON. Zakończenie publikacji LED/grupy czyści agregat surowych atrybutów tych LED-ów. Reconciler utrzymuje ponadto znacznik publikacji (w locie = nieznany) i nie pomija korekty ani nie potwierdza celu na podstawie obserwacji sprzed nowszej wysyłki.


## Ograniczone odczyty operacyjne (0.1.0b3)

Przy `report_extension=true` runtime włącza PROBE w reconcilerze. Nowa intencja ON przy nieznanym stanie przekaźnika publikuje jeden `/get` jego skonfigurowanej właściwości i czeka na rzeczywistą odpowiedź. Następnie obowiązuje dotychczasowy odstęp OFF i potwierdzenie ON. Po boot grace, jeżeli brakuje dowodu gotowości LED-a, wykonywany jest jeden odczyt jego stanu; po LED_SET/GROUP_SET każdy członek otrzymuje indywidualny odczyt stanu i wymaganych parametrów. To nie jest polling ani retry `/set`.

PROBE współdzieli zasób z komendami danego przekaźnika/LED-a, podlega tokenom, topologii, limiterowi i priorytetowi OFF. Zmiany suwaka współdzielą wysłany odczyt i pierwotny termin. Adapter pozwala na dokładne `/get` skonfigurowanych urządzeń z dozwolonymi pustymi polami; nie odczytuje grup i nie tworzy ich. Odczyt nie czyści agregatu dowodów; publikacja `/set` nadal wymaga nowych dowodów po jej zakończeniu. Wiarygodny późny raport może zakończyć UNCONFIRMED przez callback bez nowej komendy. Nowe jawne ON po błędzie odczytu odnawia generation; raport nie odnawia uprawnienia do sterowania.

Scenariusze `test_cold_start.py` używają rzeczywistej klasy adaptera i sesji surowych readResponse, bez TrustingAdapter i bez początkowego raportu OFF. Starsze scenariusze pozostają testami modelu. Odbiór na rzeczywistej instalacji jest odrębną bramką. `diagnose.runtime_version` identyfikuje wykonywany kod po aktualizacji.


## Potwierdzone LED OFF przed odcięciem zasilania

Na jawne zlecenie użytkownika zmieniono wcześniejszy kontrakt bezpośredniego RELAY_OFF. Przy `report_extension=true` i wiarygodnym ON przekaźnika programowe OFF encji (także członka operacji grupowej) wysyła indywidualne LED_OFF z transition=0, odczytuje state i czeka na nieretained DEVICE_REPORT OFF w bieżącej epoce. Dopiero potem wysyła RELAY_OFF. Obie komendy i odczyt mają priorytet OFF, wspólny arbiter i tokeny; preferencje jasności/CCT nie są modyfikowane.

Etap LED ma łączny limit `verification_timeout_ms` (domyślnie 2000 ms), obejmujący publikację i odczyt. Brak dowodu lub błąd LED nie uniemożliwia odcięcia przekaźnika. Po potwierdzonym odcięciu faza to OFF, a reason może zawierać `led_off_confirmation_timeout` lub `led_off_failed`; potwierdzenie OFF dotyczy zasilania, nie zapamiętania OFF w firmware. Bez zasilania lub kanału surowych raportów stosowane jest bezpośrednie RELAY_OFF.

Zmiany samych parametrów podczas wyłączania współdzielą rozpoczęte LED OFF i pierwotny deadline. Nowe ON unieważnia przyszłe stare odcięcie; nadanej komendy LED OFF nie można cofnąć. Fizyczny przycisk nadal odcina zasilanie niezależnie i nigdy nie jest automatycznie cofany. Integracja nie zmienia `do_not_disturb`; eliminacja błysku po przywróceniu napięcia zależy od zachowania firmware i wymaga próby sprzętowej.

## Zarządzanie urządzeniami (0.3.0)

Każdy obwód i grupa mają osobne DeviceInfo z tożsamością config entry + logical id. Sensory obwodu należą do tego samego urządzenia co światło; istniejące unique_id, entity_id i nazwy użytkownika pozostają zachowane. Opcje udostępniają edycję obwodów i grup oraz jawne wznowienie zapisanej operacji. Usunięte logical ids są rezerwowane w opcjach, aby nowe zasoby nie odziedziczyły starych encji.

`group_management.py` serializuje edycję i zmianę mapowania członka przez ten sam lock co usuwanie grupy. Przed zapisem sprawdza całą konfigurację i topologię. Dziennik w options przechowuje cel, kroki potwierdzone, krok o nieznanym wyniku i ewentualne nowe mapowanie. Konfiguracja przechodzi na nową dopiero po odpowiedziach Z2M i dokładnym katalogu. Jawne operacje członkostwa nie wymagają wcześniejszego raportu zasilania: niedostępne urządzenie może spowodować odmowę Z2M, a integracja nie włącza zasilania. Tło provisioning nadal wymaga dowodów zasilania.

Rozpoczęcie operacji unieważnia poprzednie intencje i blokuje sterowanie grupą oraz jej członkami; provisioning grup jest wstrzymany do przeładowania. Po restarcie dziennik odtwarza blokadę, bez wykonywania żądań. Interfejs odzyskiwania wymaga jawnego potwierdzenia, że operator sprawdził grupę i Z2M zakończyło poprzednie żądania (restart Z2M przy nieznanym wyniku). Dopiero wtedy katalog może rozstrzygnąć utraconą odpowiedź: wykonany krok jest przyjmowany bez wysyłki, brakujący wymaga jawnej kontynuacji. Obcy identyfikator, nazwa lub endpointy uniemożliwiają kontynuację. Brak katalogu po rozłączeniu nie dowodzi usunięcia. Wszystkie żądania bridge są bez retain, przez MQTT HA; nie ma automatycznych ponowień ani ON przy zarządzaniu.
