# Architektura Zigbee LED Orchestrator

Ten dokument opisuje architekturę repozytorium i planowany kierunek integracji. Bieżący stan zadań znajduje się w [rejestrze postępu](docs/superpowers/plans/PROGRESS.md), a szczegółowe kontrakty i kroki w [planie implementacji](docs/superpowers/plans/2026-11-13-zigbee-led-orchestrator.md). Przed zmianą sprawdź kod: opis docelowy nie oznacza, że dany element już działa.

## Cel i problem

Przekaźnik fizycznie odłącza zasilanie kontrolera LED. W tym czasie ostatni stan MQTT kontrolera może pozostać widoczny, mimo że światło nie działa. Po włączeniu zasilania kontroler potrzebuje czasu na rozruch, a wczesne polecenie może zostać utracone. Integracja ma przyjmować intencje z Home Assistanta, sterować przekaźnikami i LED-ami przez Zigbee2MQTT oraz wykorzystywać natywne grupy Zigbee do wspólnych poleceń.

Pilotaż przewiduje dwa obwody zasilane kanałami jednego przekaźnika oraz jedną grupę. Nazwy, IEEE, endpointy, możliwości urządzeń, wersje oprogramowania i faktyczny skład grupy wymagają potwierdzenia w instalacji; dane w dokumentach planistycznych są założeniami.

## Stan implementacji

Stan opisany przy aktualizacji dokumentu: **P00–P05 zakończone, P06 rozpoczęte**. Kod obejmuje fundamenty testowalnego rdzenia, lecz nie zawiera kompletnej maszyny sterującej ani działającej integracji z Home Assistant. `__init__.py` jest jeszcze szkieletem: nie inicjuje obsługi użytkownika, MQTT ani encji. Instalacja przez HACS i zielone testy rdzenia nie oznaczają gotowości do sterowania instalacją.

| Element | Odpowiedzialność | Stan |
|---|---|---|
| `models.py`, `const.py`, `protocols.py` | Typy stanu, kontrakty zegara i transportu | Istnieją |
| `configuration.py` | Walidacja modelu konfiguracji bez zależności od HA | Istnieje; konfiguracja przez UI jeszcze nie istnieje |
| `desired_store.py` | Aktualna intencja, atomowe aktualizacje i numer `generation` | Istnieje |
| `observation_store.py` | Raporty przekaźnika/LED/mostka, jakość danych, `power_epoch` i `bridge_epoch` | Istnieje |
| `command_arbiter.py` | Aktualność poleceń, priorytet OFF, ograniczanie i koordynacja wysyłek, kwarantanna niepewnej publikacji | Istnieje; integracja z rzeczywistym HA MQTT jest do sprawdzenia |
| `power_manager.py` | Konserwatywna ocena gotowości na podstawie danych i minimalnego czasu rozruchu | Częściowo: `is_ready`; reszta P06 oczekuje |
| `state_machine.py`, `reconciler.py` | Fazy obwodu i uzgadnianie intencji z obserwacjami | Planowane w P06 |
| `mqtt_adapter.py`, `topology.py` | Połączenie przez MQTT HA i weryfikacja topologii Z2M | Planowane w P08 |
| `runtime.py`, `config_flow.py`, `light.py`, sensory diagnostyczne | Cykl życia integracji, konfiguracja i encje HA | Planowane w P09–P12 |
| `group_coordinator.py`, `room_coordinator.py` | Gotowość członków, polecenia grupowe i pokojowe | Planowane w P11 i P15 |
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

## Testowanie i praca nad zmianami

`tests/helpers/clock.py` oraz `tests/helpers/transport.py` umożliwiają kontrolowanie czasu i wysyłek bez brokera. Ich ograniczenia, w tym wymagania wobec pętli asyncio, opisuje [środowisko deweloperskie](docs/development.md). Testy integracji HA/MQTT i scenariusze sprzętowe pozostają osobnymi etapami.

Przed implementacją porównaj kod z bieżącym zadaniem w planie, dodaj test zachowania i po weryfikacji uzupełnij rejestr. Przy zmianie architektury lub odpowiedzialności modułów zaktualizuj ten dokument i plan, zachowując rozróżnienie między tym, co istnieje, a tym, co dopiero zaplanowano.
