# Ponowny pełny audyt Decky Vibrance HDR — 10.10.2026

Audyt objął aktualną, lokalnie naprawioną wersję 1.0.3, a nie tylko kod wydania
1.0.2. Bazowy HEAD to `41690d79a9c17c555427631e2b3e47aa6963aae7`; poprawki
z poprzedniej i obecnej sesji pozostają niezatwierdzone. Nie wykonano commita,
pusha, publikacji ani instalacji na urządzeniu.

Przejrzano backend, generowanie i publikowanie LUT, zapis ustawień, odzyskiwanie
sesji, reakcję na zmiany panelu, frontend i kolejkę RPC, updater, zależności,
build, watch, CI, licencje i zawartość paczki. Poniższe problemy naprawiono
od razu po reprodukcji. Testy uruchomione przed poprawkami stanowią dowód
wcześniejszego błędnego zachowania.

## Nowe ustalenia i naprawy

| ID | Priorytet | Problem i skutek | Naprawa oraz regresja |
|---|---|---|---|
| B01 | P1 | Cache rozpoznawał EDID tylko po ścieżce. Podmiana poprawnego pliku pod tą samą nazwą pozostawiała stare informacje o gamucie, punkcie bieli i HDR. | Klucz obejmuje identyfikator pliku, rozmiar i znaczniki czasu. Regresja atomowo zastępuje EDID wide-gamut plikiem standard-gamut; niezmieniony plik jest nadal odczytywany raz. Brak ścieżki nie powoduje dodatkowego xprop. |
| B02 | P1 | Każda zmiana konfiguracji panelu kasowała poprzednią wartość SDR. Disable przywracał wtedy -1 zamiast wartości sprzed włączenia. Powrót z docka również nie zapamiętywał aktualnego SDR. | Odświeżenie metadanych zachowuje baseline; powrót do panelu wewnętrznego odczytuje go przed zastosowaniem ustawień. Osobna regresja chroni baseline po nieudanym zwolnieniu podczas docking. |
| B03 | P2 | Wybór slotu LUT następował przed rozpoznaniem własnej ścieżki pozostawionej w gamescope. Pierwszy build mógł nadpisać aktywny plik i opublikować tę samą nazwę. | Własny aktywny slot jest rozpoznawany przed generowaniem; build używa przeciwnego slotu. Test obejmuje PQ i G22. Obcy look zmieniony podczas build nadal jest zapamiętywany do przywrócenia. |
| B04 | P2 | `HTTPResponse.read(64 KiB)` czekał na dużą porcję, choć serwer stale przesyłał pojedyncze bajty. Sprawdzenie całkowitego czasu następowało dopiero po tym oczekiwaniu. Sprawdzanie aktualizacji czytało jedną dużą porcję. | Pobieranie i metadata API korzystają z `read1`, limitów między odczytami i pięciosekundowego socket idle timeout. Metadata ma limit 1 MiB, ZIP 32 MiB. Test rzeczywistego HTTPResponse na lokalnej parze socketów wykazuje szybkie przerwanie strumienia; drugi test zabrania użycia dużego `read`. |
| B05 | P2 | Odczyt ścieżek xprop usuwał cudzysłowy, lecz zostawiał escape'y. Ścieżki z cudzysłowem, backslashem lub oktalnie zapisanymi bajtami były błędne; obca LUT mogła nie zostać poprawnie przywrócona. | Dekodowany jest format xprop z zachowaniem UTF-8; NUL jest odrzucany. Regresje obejmują znaki specjalne, tab/newline, polskie znaki zapisane wprost i oktalnie. |
| B06 | P2 | Monitor trzymał blokadę mutacji podczas xprop i odczytów EDID. Wolna odpowiedź panelu blokowała przyjęcie zmian suwaka; dwa odczyty xprop mogły zużyć po trzy sekundy. | Oddzielna, tylko odczytująca sonda działa poza blokadą. Konfiguracja i zapisy nadal są serializowane. Regresja zatrzymuje sondę i potwierdza zakończenie zmiany suwaka przed jej zwolnieniem. |
| B07 | P2 | Filtr pakowania sprawdzał całą absolutną ścieżkę. Checkout umieszczony np. w `node_modules-workspace` gubił wszystkie pliki runtime podczas kopiowania. | Filtr sprawdza nazwę aktualnie kopiowanego elementu. Test używa takiego katalogu nadrzędnego i potwierdza obecność runtime w staging. |
| B08 | P2 | Walidator ZIP opierał się na `assert`; Python `-O` lub `PYTHONOPTIMIZE` usuwał sprawdzanie zawartości i mógł uznać błędną paczkę za poprawną. | Jawne warunki zgłaszają ValueError niezależnie od optymalizacji. Regresja sprawdza odrzucenie błędnego ZIP w obu trybach; walidator kontroluje też kanoniczne ścieżki i kompletność source map. |

## Ponowne przeglądy

Po naprawach przejrzano ponownie synchronizację, zachowanie ownership/baseline,
unieważnianie generacji i publikację plików. Dodano przypadki zmiany obcej LUT
w trakcie build, nieudanego release podczas docking, oktalnych bajtów UTF-8
i NUL. Kolejny przegląd ujawnił jeszcze wariant B02: nieudany odczyt SDR przy
powrocie mógł usunąć zachowany baseline po nieudanym release. Poprawiono ten
przypadek i dodano regresję. Następnie ponownie sprawdzono frontend, updater,
runtime i pakowanie.
W ostatnim przeglądzie nie znaleziono kolejnych potwierdzonych problemów
w zakresie możliwym do zweryfikowania na tym hoście.

Test z rzeczywiście zbudowanym frontendem sprawdza kolejkę suwak/Disable/Reset,
połączenie z API Decky, podwójne kliknięcie Check/Download oraz ignorowanie
odpowiedzi pobierania po odmontowaniu komponentu. Tryb watch ponownie wygenerował
bundle i source map po zmianie pliku; czyszczenie dist nie zgubiło wyniku.

## Weryfikacja

- 186 testów backendu: PASS, z `-W error`.
- 16 testów Node dla synchronizacji i pakowania: PASS.
- 1 test zbudowanego frontendu i wywołań API: PASS.
- Czyste npm ci, TypeScript typecheck, compileall, build, package i verify:package: PASS.
- Walidacja rzeczywistego ZIP również z `python -O`: PASS.
- npm audit: zero podatności, również w zależnościach build.
- Bandit: 18 ogólnych ostrzeżeń, 17 low i 1 medium. Po ręcznym przeglądzie
  stwierdzono wywołania subprocess bez shella, standardowy katalog socketów X11
  i opcjonalne fallbacki; nie znaleziono nowej potwierdzonej podatności.
- ZIP: CRC, 27 wpisów, wymagane pliki i zgodność bajtów z checkoutem, brak starego
  outputu i źródła/licencja @decky/api.

Gotowy plik: `DeckyVibranceHDR-1.0.3.zip`, 75 543 bajty.
SHA-256: `801a43e1c970944116cefa295e4068155efd28c0a331eb70024a7a3b237ef354`.

LUT nadal używa 33³ węzłów. Ponowny benchmark na Windows/Python 3.10.9 wykazał
medianę ciepłego build około 180 ms dla PQ i 189 ms dla G22. W porównaniu
z oryginalnym HEAD jest to około 1,90× i 1,69×; przyspieszenie generatora
pochodzi z poprzedniej sesji naprawczej. Zgodność plików pozostaje bajtowa
dla pięciu wartości saturacji i dwóch punktów bieli w obu transferach.
Nowa sesja poprawia przede wszystkim przyjmowanie zmian podczas wolnej sondy
panelu oraz reakcję na wolny strumień HTTP. Wyniki hosta nie są pomiarem SteamOS.

Logi reprodukcji i kolejnych uruchomień znajdują się obok checkoutu w katalogu
`DeckyVibranceHDR-audit-2026-10-10-round2`. Poprzednie ustalenia i ich naprawy
opisuje [REPAIR_REPORT.md](REPAIR_REPORT.md).

## Granice sprawdzenia

Nie sprawdzono fizycznego obrazu, faktycznego załadowania LUT przez gamescope,
odczytu po zapisie, obciążenia na handheldzie ani rzeczywistego docking,
suspend/resume i unload. Testy hosta nie potwierdzają tych zachowań urządzenia.

`read1` pozwala kontrolować czas podczas strumieniowania. Ustawianie połączenia,
DNS, przekierowania i ostatni blokujący odczyt mogą wydłużyć czas ścienny poza
deadline body; nie przedstawiamy go jako twardego limitu całej operacji sieciowej.
API Decky nie udostępnia tu anulowania RPC. Zapis bez odpowiedzi zachowuje
kolejność i wymaga odzyskania połączenia; timeout nie dowodzi anulowania backendu.

## Źródła sprawdzone podczas audytu

- [gamescope: GetPatchedEdidPath i WritePatchedEdid](https://github.com/ValveSoftware/gamescope/blob/master/src/edid.cpp): stabilna ścieżka i zastąpienie pliku przez rename.
- [CPython 3.10: HTTPResponse.read1](https://github.com/python/cpython/blob/3.10/Lib/http/client.py): odczyt przyrostowy; zastosowanie zweryfikowano na rzeczywistym HTTPResponse w regresji.
- [Upstream xprop](https://gitlab.freedesktop.org/xorg/app/xprop): formatowanie znaków ścieżek; sprawdzono źródło Format_String/_format_char i regresje dekodera.
