# Próby na miejscu

To lista do odbioru pilotażu, nie raport wykonanych prób. Automatyczne testy na atrapach nie zastępują obserwacji fizycznego świecenia.

## Przed pierwszą komendą

- [ ] Zapisz wersje HA/Z2M, mapowanie fizycznych kanałów i skład wybranej grupy.
- [ ] Utwórz kopię potrzebnej konfiguracji, sprawdź jej odczyt i odtworzenie. Przygotuj listę kroków powrotu i sposób awaryjnego wyłączenia.
- [ ] Zidentyfikuj wszystkie ścieżki sterowania tymi obwodami, w tym automatyzacje, skrypty, blueprinty, dashboardy i innych klientów MQTT. Zapisz ich stan i wyłącz uzgodnione ścieżki na czas próby.
- [ ] W OBSERVE sprawdź diagnostykę rozszerzenia raportów i brak operacyjnych `/set`/`/get` z integracji. Potwierdź, że fizyczne przyciski dają raporty właściwego kanału.
- [ ] Ustal metodę obserwacji/nagrania obu LED-ów, wspólną skalę czasu i niepewność. Czas MQTT nie jest czasem zapalenia.

## Kolejność prób

1. Po restarcie HA, przy obu przekaźnikach OFF, włącz ACTIVE i wykonaj ON bez wcześniejszego przełączania przycisków ani ręcznego `/get`. Sprawdź początkowy odczyt przekaźnika, potwierdzenie ON, oczekiwanie na kontroler oraz odczyt weryfikacyjny. Wykonaj co najmniej dwa pełne cykle każdego obwodu. Sprawdź rzeczywiste światło, niezależność kanałów, phase i confirmed. Programowe OFF przy zasilonym kontrolerze ma wysłać LED OFF i /get state, odebrać rzeczywiste OFF, a dopiero potem odciąć przekaźnik. Brak odpowiedzi ma skończyć się odcięciem po ograniczonym oczekiwaniu (domyślnie 2 s), z przyczyną braku potwierdzenia LED w diagnostyce.
2. Wyłącz fizycznie przekaźnik podczas ON i podczas rozruchu. Integracja nie może go samoczynnie ponownie włączyć. Fizyczne ON musi rozpocząć nową epokę raportów.
3. Wykonaj szybkie ON/OFF oraz zmiany jasności/CCT. Liczy się ostatni zamiar; nie może pojawić się seria starych komend.
4. Jeśli podano nazwę grupy, sprawdź barierę gotowości i pojedynczy grupowy `/set`, gdy skład w Zigbee2MQTT jest dokładnie zbiorem skonfigurowanych LED-ów. Przy niepełnym składzie sterowanie idzie do członków osobno. Zapisz, których obwodów dotyczyła próba.
5. Sprawdź brak potwierdzenia jednego LED-a: ograniczony timeout, widoczny błąd/niepotwierdzony stan i brak nieskończonych ponowień.
6. Zaplanuj osobno restart HA, Z2M i połączenia MQTT. W instalacji współdzielonej wpływa to również na inne urządzenia: wykonuj tylko w uzgodnionym oknie. Sprawdź brak historycznego ON. Po powrocie katalogu sterowanie ma być włączone samo. Zniknięcie jednego urządzenia ma wyłączyć tylko jego obwód i zostawić powiadomienie.
7. Wykonaj i zapisz rollback według instrukcji instalacji.

Przerwij przy zmianie niewłaściwego obwodu, spóźnionym ON po OFF, nieznanym nadawcy, błędnym składzie grupy lub utracie możliwości wyłączenia.

## Pomiary odbioru

Dla LED 1, LED 2 i zgodnej grupy wykonaj osobno po 30 zimnych startów. Przed serią ustal minimalny czas faktycznego odłączenia zasilania i limity prób na podstawie urządzeń. Zachowaj również nieudane i niekompletne próby.

Zapisuj: numer i datę próby, wersje/konfigurację, czas intencji, publikacji, raportu przekaźnika, raportu gotowości i pierwszej widocznej emisji każdego LED-a; źródła i niepewność; wszystkie komendy, timeouty i błędy. Dla p50/p95 podaj metodę percentyla i liczbę udanych próbek, a osobno liczbę timeoutów. Dla grupy policz różnicę czasów emisji tylko z kompletnych par.

## Kontrole programowe

Python 3.14.2+, osobne środowiska według plików requirements:

```sh
python -m pytest tests/unit -q
python -m ruff check .
python -m ruff format --check .
```

W środowisku z `requirements-test-ha.txt`:

```sh
python -m pytest tests/ha tests/scenarios -q
```

Rozszerzenie, Node 22+:

```sh
node tests/z2m/report-extension.test.mjs
```

`tests/scenarios/test_cold_start.py` używa produkcyjnego adaptera, nonce i sekwencji raw readResponse, bez wstrzykniętego początkowego OFF. Obejmuje dwa cykle zasilania, brak spontanicznych raportów, późne potwierdzenia, przerwanie przez OFF i reconnect oraz pojedynczy set istniejącej grupy. Test nowego protokołu używa produkcyjnej klasyfikacji adaptera; wcześniejsze scenariusze MVP modelują zaufane źródło w harnessie. Wszystkie te testy pozostają syntetyczne.
