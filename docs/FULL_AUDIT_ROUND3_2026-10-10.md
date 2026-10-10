# Trzeci pełny audyt Decky Vibrance HDR — 10.10.2026

Sprawdzono bieżącą lokalną wersję 1.0.3 po poprzednich dwóch sesjach naprawczych.
HEAD pozostaje `41690d79a9c17c555427631e2b3e47aa6963aae7`. Zmiany wszystkich
sesji pozostają lokalne i niezatwierdzone; nie wykonano commita, pusha, publikacji
ani instalacji na urządzeniu.

Przegląd objął wszystkie moduły runtime: RPC, synchronizację frontendu, kolejki
i priorytety sterowania, zapis ustawień, kontrolę własności i przywracanie atomów,
generowanie i publikację LUT, EDID, reconnect, suspend/resume, docking, zamykanie,
updater, HTTP, zależności, build, pakowanie, walidację ZIP, CI i licencje. Każdą
potwierdzoną usterkę naprawiono i sprawdzono testami regresji.

## Nowe ustalenia

| ID | Priorytet | Problem i reprodukcja | Naprawa i regresja |
|---|---|---|---|
| C01 | P2 | Kolejka scalała suwaki, lecz każde przełączenie i Reset zachowywała jako osobny RPC. Przy zablokowanej pierwszej odpowiedzi 200 przełączeń wysyłało wszystkie 200 operacji; Reset czekał na stare przełączenia. Każda operacja mogła wykonywać fsync i wywołania xprop. | Jawne identyfikatory znanych poleceń scalają ich oczekujące intencje. Reset usuwa starsze suwaki i przełączenia; operacja w toku zachowuje kolejność. Test 200 przełączeń wysyła tylko pierwsze i ostatnie, czyli 2 RPC zamiast 200. Osobne testy chronią Reset, późniejsze przełączenia, drugi kanał i wszystkie skutki poleceń bez identyfikatora scalania. Test gotowego bundle sprawdza użycie tej funkcji przez prawdziwe kontrolki. |
| C02 | P2 | Powolne badanie innych serwerów X wykonywało się pod blokadą mutacji, więc blokowało przyjmowanie zmian obu suwaków. Regresja zatrzymała tę sondę, a zmiana HDR nie zakończyła się przed jej zwolnieniem. | Sonda działa przed zajęciem blokady. Zastosowanie wyniku i operacje na atomach pozostają serializowane. W teście rzeczywistej pętli monitora zmiana kończy się przed zwolnieniem sondy. Gdy SDR nie wymaga kontroli, sonda nie jest uruchamiana. |
| C03 | P2 | Enable z najnowszą saturacją przyjmował wartość, ale przy zewnętrznym ekranie lub niedostępnym HDR wracał bez zaplanowania zapisu. Po restarcie znikała zaakceptowana zmiana suwaka. | Zaakceptowana wartość zostaje zapisana także przy zablokowanym Enable; nie jest wymuszane włączenie na nieobsługiwanym ekranie. Regresja obejmuje SDR podczas docking i HDR bez wsparcia panelu. |
| C04 | P2 | Porównanie wersji usuwało prerelease i traktowało cyfry build metadata jako część kolejności. Stabilne wydanie tego samego rdzenia nie było aktualizacją prerelease; sam build suffix mógł pozorować nową wersję. Usuwanie suffixu psuło też wybór dokładnego ZIP. | Porządek SemVer uwzględnia numeryczne i tekstowe identyfikatory prerelease, a pomija build metadata. Pełna wersja trafia do nazwy ZIP; dopuszczony jest znak `+`. Regresje obejmują oficjalny łańcuch precedencji, błędne wersje, rc → stable, beta.1 → beta.2, równe buildy i pobranie ZIP z suffixem build. |
| C05 | P2 | Odczyt user-dirs.dirs zostawiał escape'y i komentarze, a literalne `\$HOME` błędnie rozwijał. Powstawała inna ścieżka pobierania. Względne i uszkodzone wartości również mogły być używane. | Parser rozpoznaje cudzysłowy i komentarz, rozwija tylko nieescapowany początek `$HOME`, dekoduje dozwolone escape'y i wymaga absolutnej ścieżki bez NUL. Brak lub uszkodzenie konfiguracji daje Downloads w katalogu użytkownika. Regresje obejmują cudzysłowy, literalne dolary, komentarz oraz błędne wartości. Parser nie uruchamia powłoki. |
| C06 | P2 | Kontrola dostępności gamescope nadal miała stały timeout 5 s. Odczyt uruchomiony przez monitor po rozpoczęciu unload mógł zużyć cały pięciosekundowy czas zatrzymania, mimo skrócenia pozostałych operacji. | Kontrola dostępności używa wspólnego timeoutu xprop, skracanego do 0,25 s podczas unload. Regresja zatrzymuje odczyt monitora, rozpoczyna unload i sprawdza timeout kolejnej kontroli. Przed poprawką wykazała 5 s zamiast najwyżej 0,25 s. Limit nie skraca wstecz subprocessu uruchomionego wcześniej. |

Zasady porównywania wersji zweryfikowano w [specyfikacji SemVer 2.0.0](https://semver.org/).

## Kolejne przeglądy

Po pierwszych pięciu naprawach ponownie sprawdzono kolejkę, anulowanie pracy,
zapis zaakceptowanych wartości, monitor i lifecycle. Test istniejącego resume
wychwycił błąd umiejscowienia nowej metody podczas refaktoryzacji; poprawiono go
przed zakończeniem walidacji. Ten błąd w trakcie pracy nie jest liczony jako
usterka poprzedniej wersji. Następny przegląd ujawnił C06, który również został
odtworzony i naprawiony. Końcowy przegląd kodu, regresji i artefaktu nie ujawnił
dalszej potwierdzonej usterki w zakresie możliwym do zweryfikowania lokalnie.

## Walidacja

- Czyste `npm ci`: zakończone poprawnie.
- `python -W error -m unittest discover -s tests -v`: **196/196**, bez ostrzeżeń.
- `npm test`: **20/20**, w tym 18 regresji synchronizacji i 2 regresje pakowania.
- `npm run typecheck`: poprawne.
- `git diff --check` i `python -m compileall`: poprawne.
- `npm audit`: **0 zgłoszonych podatności**.
- Bandit: 18 wskazań, 17 low i 1 medium; wszystkie przejrzane. B108 dotyczy
  odczytu katalogu socketów `/tmp/.X11-unix`, a nie zapisu pliku tymczasowego.
  B404/B603/B607 dotyczą uruchamiania xprop listą argumentów bez powłoki,
  z ograniczonym czasem i oczyszczonymi zmiennymi loadera. B110 dotyczy
  opcjonalnego certifi i odczytu ustawień z późniejszą walidacją pól.
  Nie znaleziono w tych wskazaniach kolejnej potwierdzonej usterki.

- `npm run package`: czysty build Rollup i utworzenie ZIP, poprawne.
- `npm run verify:package`: zgodność wszystkich plików runtime z checkoutem,
  CRC, ścieżki, manifesty, licencja i źródła @decky/api oraz kompletność source map
  sprawdzone poprawnie. Test gotowego bundle **1/1**: host globals i API Decky,
  prawdziwe suwaki/przełączenia/Reset, updater i odpowiedź po unmount.
- Końcowe `git diff --check`: poprawne; HEAD nie zmienił się.

Paczka: `DeckyVibranceHDR-1.0.3.zip`, **76 755 bajtów**, 27 wpisów.
SHA-256: `e48e4501dff43f1ff256f6322fa5857e38f7d792164ab581cb57430f245bd16f`.
ZIP zawiera bieżący kod i dokumentację użytkownika; testy, node_modules, cache
i raporty audytu pozostają poza paczką.

Dowody przed naprawami, kopie pięciu plików wejściowych i logi końcowe są w
`../DeckyVibranceHDR-audit-2026-10-10-round3/`. Pliki `before-backend.log`,
`before-frontend.log` i `before-unload.log` dokumentują błędy przed naprawami.
Poprzednie raporty pozostają zachowane w `docs/REPAIR_REPORT.md` oraz
`docs/FULL_AUDIT_2026-10-10.md`.

## Granice wyniku

Pomiary liczby RPC i testy blokad dotyczą kontrolowanych scenariuszy na hoście;
nie są pomiarem opóźnienia obrazu na urządzeniu. Rozdzielczość LUT 33³ i jej
matematyka pozostały bez zmian w tej sesji. Testy nadal chronią zgodność wartości
i plików z wcześniejszym generatorem.

Nie wykonano fizycznej walidacji gamescope/Decky, koloru, suspend/resume,
dockingu ani instalacji ZIP. Odebranie odpowiedzi xprop nie potwierdza
załadowania LUT przez gamescope. Trwającego zapisu RPC nie można bezpiecznie
wyprzedzić, gdy Decky nie oferuje jego anulowania. Brak dalszych potwierdzonych
usterek w tym przeglądzie nie jest gwarancją braku wszystkich błędów.
