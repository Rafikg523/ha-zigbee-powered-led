# Architektura Zigbee LED Orchestrator

Ten dokument opisuje architekturę repozytorium i planowany kierunek integracji. Bieżący stan zadań znajduje się w [rejestrze postępu](docs/superpowers/plans/PROGRESS.md), a szczegółowe kontrakty i kroki w [planie implementacji](docs/superpowers/plans/2026-11-13-zigbee-led-orchestrator.md). Przed zmianą sprawdź kod: opis docelowy nie oznacza, że dany element już działa.

## Cel i problem

Przekaźnik fizycznie odłącza zasilanie kontrolera LED. W tym czasie ostatni stan MQTT kontrolera może pozostać widoczny, mimo że światło nie działa. Po włączeniu zasilania kontroler potrzebuje czasu na rozruch, a wczesne polecenie może zostać utracone. Integracja ma przyjmować intencje z Home Assistanta, sterować przekaźnikami i LED-ami przez Zigbee2MQTT oraz wykorzystywać natywne grupy Zigbee do wspólnych poleceń.

Pilotaż przewiduje dwa obwody zasilane kanałami jednego przekaźnika oraz jedną grupę. Nazwy, IEEE, endpointy, możliwości urządzeń, wersje oprogramowania i faktyczny skład grupy wymagają potwierdzenia w instalacji; dane w dokumentach planistycznych są założeniami.

## Stan implementacji

Stan opisany przy aktualizacji dokumentu: **P00–P12 zakończone w zakresie syntetycznym**. Kod obejmuje pełny rdzeń domenowy, adapter MQTT, katalog topologii, config flow, runtime lifecycle, encje light, koordynator grup z barierą gotowości oraz preferencje, diagnostykę i usługi serwisowe. `__init__.py` inicjuje lifecycle config entry. Instalacja przez HACS i zielone testy nie oznaczają gotowości do sterowania instalacją — aktywny pilotaż wymaga P14.

| Element | Odpowiedzialność | Stan |
|---|---|---|
| `models.py`, `const.py`, `protocols.py` | Typy stanu, kontrakty zegara i transportu | Istnieją |
| `configuration.py` | Walidacja modelu konfiguracji bez zależności od HA | Istnieje; konfiguracja przez UI jeszcze nie istnieje |
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

Docelowo wspólne ON/brightness/CCT jest wysyłane do grupy Zigbee2MQTT tylko, gdy skład został zweryfikowany, wszyscy faktyczni odbiorcy powinni otrzymać polecenie i spełniono barierę gotowości. Odpowiedź grupowa nie jest potwierdzeniem każdego członka. Timeout ma pozwolić na bezpieczną częściową degradację bez niezamierzonego sterowania innymi urządzeniami.

W MVP plan obejmuje dwie lampy i jedną grupę z dedykowanymi kanałami. Trzy grupy, pomieszczenia, nakładające się członkostwo i lampy stale zasilane zaplanowano jako kolejne etapy. Niezależne zasoby nie powinny czekać na pełny rozruch innego obwodu. Obecny plan MVP dopuszcza jednego aktywnego arbitra w procesie HA; nie jest to jeszcze obsługa wielu niezależnych instalacji Z2M.

### Preferencje, pauza i resync (P12)

- Zapisywane są tylko jasność i CCT, po zmianie parametrów (opóźniony zapis). Odtworzenie następuje przed jakimkolwiek sterowaniem, w trybie OBSERVE, i nie zmienia `on`.
- `pause` zapamiętuje poprzedni tryb i ustawia żądany stan OFF bez publikacji. `resume` przywraca ten tryb, nie odtwarza starej intencji i nie awansuje OBSERVE do ACTIVE.
- `resync` cofa tryb do OBSERVE, zamyka silnik i koordynatora, unieważnia dowody i ponownie subskrybuje katalog MQTT; nie publikuje `/set`. ACTIVE wymaga ponownego jawnego włączenia.
- Eksport diagnostyczny zastępuje IEEE, tematy, nazwy i identyfikatory konfiguracji aliasami.

### Bariera grupy (P11)

`GroupCoordinator` tworzy rezerwację operacji tylko wtedy, gdy po zastosowaniu zamiaru wszyscy członkowie grupy mają cel ON i wszystkim zaczęto nową operację; w przeciwnym razie zamiar trafia do członków indywidualnie. Rezerwacja nie jest blokadą zasobu: członek nadal samodzielnie włącza przekaźnik i czeka na gotowość (reconciler), a decyzję o ostatnim `LED_SET` przekazuje koordynatorowi. Gdy wszyscy są gotowi, a ich docelowe payloady są identyczne po konwersji do zakresów urządzeń, wysyłany jest jeden `GROUP_SET` (zasoby: grupa i LED-y członków, tokeny wszystkich członków). Każda zmiana celu członka, OFF, błąd członka, niezgodność payloadów lub odrzucenie `STALE` przez arbitra kończy rezerwację i zwalnia pozostałych do wysyłek indywidualnych — bez przywracania celu zmienionemu członkowi. Po `group_barrier_timeout_ms` niegotowi członkowie przechodzą w `ERROR` (`group_barrier_timeout`), a gotowi są obsługiwani indywidualnie. Zmiana topologii (`Reconciler.topology_changed`) cofa uprawnienia jak wyłączenie sterowania. Publikacja `GROUP_SET` nie potwierdza członków: stan i `confirmed_members` grupy wynikają z indywidualnych potwierdzeń, a OFF grupy to wyłącznie OFF przekaźników członków. Grupy nakładające się i lampy stale zasilane pozostają poza zakresem do P16.

## Testowanie i praca nad zmianami

`tests/helpers/clock.py` oraz `tests/helpers/transport.py` umożliwiają kontrolowanie czasu i wysyłek bez brokera. Ich ograniczenia, w tym wymagania wobec pętli asyncio, opisuje [środowisko deweloperskie](docs/development.md). Testy integracji HA/MQTT i scenariusze sprzętowe pozostają osobnymi etapami.

Przed implementacją porównaj kod z bieżącym zadaniem w planie, dodaj test zachowania i po weryfikacji uzupełnij rejestr. Przy zmianie architektury lub odpowiedzialności modułów zaktualizuj ten dokument i plan, zachowując rozróżnienie między tym, co istnieje, a tym, co dopiero zaplanowano.

Czas `power_off_at` jest czasem odebrania wiarygodnego raportu o przejściu ON → OFF, a nie znaną chwilą fizycznego przełączenia. Pierwszy odczyt OFF po uruchomieniu nie tworzy takiego czasu; przed kolejnym ON stosowany jest zachowawczy odstęp liczony od odebrania tego odczytu. Ponowne raporty OFF nie przesuwają czasu znanego przejścia.

Adapter klasyfikuje zwykłe raporty Z2M jako dane cache lub o nieznanym źródle, nie jako dowód od urządzenia. Skutkiem jest zachowawcza odmowa potwierdzenia gotowości, dopóki nie zostanie zweryfikowane źródło wiarygodnych raportów. Weryfikacja grup używa dokładnego zestawu IEEE i endpointów; po zmianie listy urządzeń wymaga ponownego raportu grup. Runtime musi przekazać bieżącą wersję topologii do walidacji arbitra i spiąć obserwacje ze sklepem; sam adapter nie jest jeszcze działającą integracją HA.

`light.py` oraz `light_capabilities.py` są częściową pracą P10: testują prezentację celu i konwersje na syntetycznych danych. Nie są jeszcze uruchamiane przez HA ani podłączone do reconciler; limity muszą pochodzić z zatwierdzonych capabilities danego endpointu.
