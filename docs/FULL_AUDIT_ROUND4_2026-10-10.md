# Czwarty pełny audyt Decky Vibrance HDR — 10.10.2026

Audyt obejmuje bieżący lokalny kod 1.0.3 po trzech wcześniejszych sesjach.
HEAD pozostaje `41690d79a9c17c555427631e2b3e47aa6963aae7`. Wszystkie zmiany
pozostają niezatwierdzone. Nie wykonano commita, pusha, PR, publikacji ani
instalacji na urządzeniu.

Przejrzano backend i RPC, zapis ustawień, blokady, kolejki i tokeny generacji,
własność atomów i przywracanie wartości, monitor, reconnect, suspend/resume,
EDID, docking, generowanie i publikację LUT, frontend i jego lifecycle,
updater i HTTP, zależności, build, pakowanie, walidację ZIP, CI oraz licencje.
Największy nacisk położono na współbieżność i pracę niepotrzebnie powtarzaną.

## Potwierdzone problemy i naprawy

| ID | Priorytet | Problem przed naprawą | Naprawa i dowód |
|---|---|---|---|
| D01 | P2 | Ponowne przesłanie już zastosowanej, zdrowej wartości uruchamiało zapis ustawień i ponowne zastosowanie SDR lub build HDR LUT. Mogło też unieważnić trwający build tej samej wartości. | Rozróżniono zmianę wartości od powtórzenia. Zdrowe powtórzenia nie uruchamiają zapisu ani zastosowania; nie przerywają równoważnego build. Brudne ustawienia i nieudane lub brakujące zastosowania nadal są ponawiane. Regresje obejmują SDR, HDR, nieudany zapis/build, nieudany atom SDR i równoważny build w toku. |
| D02 | P2 | Kontrola aktualizacji z zamkniętego panelu mogła zakończyć się po kontroli z ponownie otwartego panelu. Nadpisywała wtedy wybrany ZIP, przez co nowszy widoczny wynik nie zgadzał się z autoryzacją pobrania. Mogła też odtworzyć autoryzację po błędzie najnowszej kontroli. | Kontrole mają generacje, a krótka blokada chroni publikowanie wybranego assetu. Tylko najnowsza kontrola może go opublikować; starszy poprawny wynik zwraca informację o zastąpieniu przez nowsze żądanie. Dwa testy nakładają rzeczywiste wątki: najnowszy wynik poprawny oraz najnowszy wynik błędny. Blokada nie jest trzymana podczas HTTP. |
| D03 | P1 | Po przejęciu atomu przez plugin obca LUT mogła zmienić się podczas zastępowania aktywnej LUT albo przed reassert monitora/resume. Plugin nadpisywał nową obcą ścieżkę bez zapamiętania jej. Późniejszy Disable/Reset przywracał starszą obcą LUT. | Przed zastąpieniem i reassert zapamiętywana jest nowo zaobserwowana obca ścieżka. Zniknięcie atomu i własne stare sloty nie kasują tego baseline. Regresje obejmują HDR i SDR, zmianę podczas build już posiadanej LUT, monitor, resume, zniknięcie atomu i własny stary slot. |
| D04 | P3 | Workflow uruchamiał `npm run build`, a następnie `npm run package`, który sam ponownie uruchamia build. CI wykonywało dwa identyczne buildy bez dodatkowej walidacji pomiędzy nimi. | Usunięto osobny krok build. Pakowanie nadal zawsze przebudowuje frontend, a następny krok waliduje gotowy bundle i ZIP. Sprawdzenie wykonano przez rzeczywiste `npm run package` i `npm run verify:package`; nie dodawano testu kopiującego tekst konfiguracji. |

Nowe regresje przed poprawkami: 7 testów, **8 niepowodzeń** przy uwzględnieniu
przypadków subtest. Test odzyskania po nieudanym zastosowaniu przechodził już
wcześniej i chroni dotychczasową funkcję ponawiania.

## Pomiar wydajności

Porównano zachowaną kopię backendu sprzed tej sesji z kodem po naprawie D01.
Scenariusz: po poprawnym zastosowaniu HDR 1,3 wysłano 30 powtórzeń wartości 1,3
i zaczekano na wszystkie zadania. Generator rzeczywiście budował LUT 33³
i zapisywał plik w katalogu tymczasowym; operacje X i zapis ustawień były
mockowane. Pomiar wykonano na tym komputerze, z przygotowanym cache wejściowym.

| Wynik | Przed | Po |
|---|---:|---:|
| Czas obsługi powtórzeń w jednym pomiarze | 173,876 ms | 0,310 ms |
| Dodatkowe buildy LUT | 1 | 0 |
| Dodatkowe wywołania zapisu ustawień | 1 | 0 |
| Dodatkowe zapisy atomu look | 1 | 0 |
| Końcowa zastosowana saturacja | 1,3 | 1,3 |

To pomiar usuniętej zbędnej pracy backendu, nie pomiar opóźnienia UI, obrazu
ani FPS na urządzeniu. D03 dodaje odczyt przed ponownym zapisem już posiadanej
LUT tam, gdzie wcześniej go nie było; jest potrzebny do zachowania nowej obcej
wartości. Kontrola monitora wykorzystuje odczyt, który już wykonywała.
Rozdzielczość, matematyka i zawartość generowanych LUT nie zmieniły się.

## Ponowne przeglądy i walidacja

Po naprawach ponownie sprawdzono ścieżki retry, anulowanie równoważnego build,
wartości neutralne, baseline po zniknięciu atomu i rozpoznawanie własnych slotów.
Rozszerzono regresje o te scenariusze, odtworzony błąd dysku i brakujące pliki
HDR/SDR. Sprawdzono też granice blokady kontroli aktualizacji i zachowanie po
błędzie nowszego żądania. Końcowy przegląd nie ujawnił kolejnej potwierdzonej
usterki w zakresie możliwym do zweryfikowania lokalnie.

- Czyste `npm ci`: poprawne.
- `python -W error -m unittest discover -s tests -v`: **207/207**, bez ostrzeżeń;
  11 nowych testów w `test_quality_round4.py`.
- `npm test`: **20/20**.
- `npm run typecheck`: poprawne.
- `git diff --check` i `python -m compileall`: poprawne.
- `npm audit`: **0 zgłoszonych podatności**.
- Bandit: 18 wskazań, 17 low i 1 medium; przejrzane. B108 dotyczy katalogu
  socketów `/tmp/.X11-unix`, a nie zapisu pliku tymczasowego. B404/B603/B607
  dotyczą xprop wywoływanego listą argumentów, bez powłoki, z oczyszczonymi
  zmiennymi loadera i ograniczonym czasem. B110 dotyczy opcjonalnego certifi
  i odczytu ustawień z późniejszą walidacją. Te wskazania nie ujawniły nowej
  potwierdzonej usterki.
- `npm run package`: jeden czysty build Rollup i utworzenie ZIP, poprawne.
- `npm run verify:package`: zgodność runtime z checkoutem, CRC, ścieżki ZIP,
  manifesty, source map oraz źródła/licencja @decky/api sprawdzone poprawnie.
  Test gotowego bundle **1/1** obejmuje globals i API Decky, kontrolki,
  aktualizator oraz ignorowanie odpowiedzi po unmount.

Paczka: `DeckyVibranceHDR-1.0.3.zip`, **77 649 bajtów**, 27 wpisów.
SHA-256: `749b24a2fb93d8e45791d9edb1f3a8561a9e02a87b656f3698c1deb5cb911c2f`.
Paczka zawiera aktualny runtime i dokumentację użytkownika. Raporty, testy,
node_modules i cache pozostają poza ZIP.

Kopie pięciu plików wejściowych, `before.log`, testy i pomiary przed/po znajdują
się w `../DeckyVibranceHDR-audit-2026-10-10-round4/`. Skrypt
`benchmark_duplicates.py` zawiera scenariusz pomiarowy i zapisuje ścieżkę
rzeczywiście zaimportowanego backendu, żeby potwierdzić porównywane wersje.
Wcześniejsze raporty pozostają w `docs/`.

## Granice weryfikacji

Nie wykonano instalacji ani fizycznej walidacji gamescope/Decky, wyjścia koloru,
suspend/resume i dockingu. Nie uruchamiano zdalnego workflow CI. Odpowiedź
xprop nadal nie potwierdza, że gamescope załadował LUT. Zewnętrzny writer może
zmienić atom pomiędzy odczytem i zapisem; xprop nie zapewnia tu atomowego CAS.
Naprawa D03 zachowuje obce wartości, które plugin zdążył zaobserwować.
