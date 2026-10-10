# Audyt jakosci — runda 7, 2026-10-10

W tej sesji znaleziono **1 potwierdzony problem i naprawiono 1**. Ponowny
przeglad po poprawce nie wykazal kolejnych potwierdzonych problemow lokalnych.
Ta sesja nie byla czysta od poczatku.

## Zakres

Przeglad aktualnego backendu, frontendu i helpera synchronizacji, generatora LUT,
updatera, skryptow budowania/pakowania/weryfikacji i CI. Oceniono wywolania RPC
i serializacje argumentow, priorytety i laczenie zmian, optimistic UI, bledy
polaczenia, zadania/locki/generacje, odczyt i zapis ustawien, start/unload,
gamescope recovery, restore baselines, docking/resume, cache EDID/LUT, anulowanie
generowania, HTTP/TLS, walidacje pobranych archiwow i zawartosc lokalnego ZIP.
Poprawka poprzedniej sesji dotyczaca argumentu `get_state` byla juz obecna;
nie jest liczona jako nowe znalezienie tej rundy.

## G01 — P1: unload przed odczytem ustawien nadpisywal zapisane wartosci

`_main` czeka na writer LUT, inicjalizuje TLS i odczytuje plik ustawien.
Jesli `_unload` wykonal sie podczas jednego z tych oczekiwan, bezwarunkowe
`_persist` zapisalo niezainicjalizowany stan klasy: wylaczone SDR/HDR i 1.0.
Mogly zniknac rowniez nieznane pola z pliku, bo SettingsManager jeszcze ich
nie odczytal. Pozniejsze dokonczenie `_main` moglo ponownie zezwolic na zapis
LUT i uruchomic zadania mimo przeprowadzonego zamkniecia.

Potwierdzenie: trzy deterministyczne regresje z rzeczywistym tymczasowym
plikiem ustawien i produkcyjna funkcja zapisu. Przed zmiana wszystkie trzy
nadpisywaly zapisane SDR 2.6/HDR 1.4 wartosciami domyslnymi. Dowod:
`../DeckyVibranceHDR-audit-2026-10-10-round7/before-startup.log`.

Poprawka w `main.py`: stan `_settings_loaded` odroznia zaladowane ustawienia
od domyslnych wartosci startowych. Unload zapisuje ustawienia po ich
inicjalizacji lub gdy istnieja przyjete, niezapisane edycje. Start sprawdza
zamkniecie po kazdym etapie inicjalizacji, przed ponownym zezwoleniem na LUT
i przed tworzeniem zadan. Normalny unload nadal jest koncowa bariera zapisu.
Fixture gotowego backendu w `test_recovery.py` uwzglednia zaladowane ustawienia.

Dodano `tests/test_quality_round7.py`: unload podczas czekania na writer,
TLS i odczyt ustawien. Kazdy scenariusz sprawdza zachowanie pliku, brak
pozniejszych zadan recovery i utrzymanie blokady zapisow LUT po unload.

## Wyniki

- Python 3.10.9: **247/247** regresji, `-W error`, 13.467 s.
- Node 24.18.0: **21/21** testow; skompilowany bundle: **1/1**. Razem **269**.
- Typecheck, compileall, build i `git diff --check`: poprawne.
- `npm audit`: **0** podatnosci.
- Bandit: 18 wskazan, 17 low i 1 medium, 0 high. Przejrzane wskazania
  dotycza kontrolowanych wywolan xprop bez shella, importu subprocess,
  fallbackow certifi/odczytu ustawien i stalej sciezki odczytu socketow X11.
  Nie stanowia nowych potwierdzonych problemow tej rundy.
- ZIP przebudowany, CRC/sciezki/runtime/source map/licencja/manifest
  oraz zgodnosc z aktualnym kodem potwierdzone przez `verify:package`.

Paczka: `DeckyVibranceHDR-1.0.3.zip`, **81164 bajtow**, 27 wpisow, 21 plikow.

SHA256: `d0b17a9c6e6cb0e41b6960adce0f320f8014c86c52ae05a43b40f95960b4cc03`.

Dowody i snapshot sprzed poprawki:
`../DeckyVibranceHDR-audit-2026-10-10-round7/`.

## Pozostale problemy i ograniczenia

Nie pozostaly znane, potwierdzone problemy do naprawy w lokalnie sprawdzonym
kodzie. Nie stwierdzono kolejnej uzasadnionej optymalizacji wydajnosci;
ta poprawka usuwa niepotrzebne kontynuowanie startu po zamknieciu,
nie przypisano jej mierzonego przyspieszenia sliderow ani wzrostu FPS.

W tej rundzie nie laczono sie z urzadzeniem, nie instalowano paczki ani
nie restartowano Decky. Weryfikacja nowej poprawki na rzeczywistym runtime
Decky, ocena wyswietlanego obrazu, fizyczny suspend/resume i docking
pozostaja niewykonane w tej sesji. Nie przedstawiono ich jako zaliczonych.
Nie uruchamiano CI na GitHub ani lokalnie macierzy Python 3.13/Node 22.

HEAD bez zmian: `41690d79a9c17c555427631e2b3e47aa6963aae7`.
Bez commita, pusha, PR, release i zmian poza wlasnym repozytorium oraz
jego lokalnym katalogiem dowodow audytu.
