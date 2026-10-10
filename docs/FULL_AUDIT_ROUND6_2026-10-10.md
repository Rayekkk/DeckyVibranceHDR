# Szósty pełny audyt jakości — 2026-10-10

Przeprowadzono kolejne iteracje: pełny przegląd, reprodukcja, naprawa, regresje
i ponowny przegląd. Naprawiono pięć nowych ustaleń. Ostatni przegląd nie ujawnił
kolejnej potwierdzonej usterki ani uzasadnionej poprawki wydajności w lokalnie
sprawdzanym zakresie. Nie ma już kolejnych potwierdzonych problemów do naprawy
w tym zakresie; dalsze sprawdzanie fizycznego działania wymaga urządzenia.

Punktem odniesienia jest checkout po piątej sesji, nadal na HEAD
`41690d79a9c17c555427631e2b3e47aa6963aae7`, z lokalną wersją 1.0.3 Unreleased.
Nie wykonano commita, pusha, publikacji, instalacji ani zmian na urządzeniu.

## Ustalenia i naprawy

| ID | Priorytet | Problem | Wynik naprawy |
|---|---|---|---|
| F01 | P2 | Resume i monitor ignorowały nieudany zapis atomu LUT. Stan nie pokazywał błędu, a poprawna nazwa starego atomu mogła powstrzymać retry. | Ponowny zapis raportuje błąd właściwego komponentu. Monitor może ponowić pracę; sukces starej LUT nie ukrywa błędu nowszego żądania. |
| F02 | P2 | Generator wielokrotnie wykonywał dolny clamp, choć funkcje transferu już obsługiwały wartości niedodatnie. | Usunięto powtarzaną pracę na każdym węźle, zachowując górny clamp, rozdzielczość, kolejność węzłów i wynik pliku. |
| F03 | P2 | Po nieudanym usunięciu looku żądanie neutralnej LUT pozostawiało ownership. Resume/monitor mogły ponownie wysłać stary, nieneutralny look. | Reassert ponawia usunięcie looku zgodnie z aktualnym żądaniem. Nie buduje LUT i zachowuje normalne odtworzenie obcego baseline. |
| F04 | P2 | Updater akceptował nazwy wpisów ZIP zawierające NUL. ZipInfo skracał nazwę, więc poprawny CRC nie oznaczał jednoznacznego poprawnego pathname. | Nazwa oryginalna musi być identyczna z nazwą używaną przez zipfile. Wadliwa paczka jest odrzucana, poprzedni ZIP zachowany, a plik częściowy usuwany. |
| F05 | P1 | Pojedynczy błąd sondy kasował ownership i baseline przed potwierdzeniem restartu. Powrót do tej samej sesji gubił obcą LUT i poprzednią wartość SDR. Disable podczas przerwy mógł pozostawić efekt SDR włączony. | Ownership pozostaje do porównania tożsamości lokalnego socketu X. Ta sama rozpoznana sesja zachowuje baseline i zdrowe LUT. Nowa sesja odrzuca stare ownership, a restore podczas przerwy wymaga potwierdzenia tej samej sesji. Wymiana socketu jest wykrywana również przy zdrowych atomach. |

F01 odtworzono dwoma testami w `before-reassert.log`. Ponowny przegląd retry
ujawnił F03 (`before-neutral.log`). Przegląd pakowania/updatera odtworzył F04
(`before-archive.log`). Następny przegląd cyklu życia ujawnił trzy warianty F05
(`before-session.log`): przywrócenie -1 zamiast poprzedniego SDR, skasowanie
obcej LUT i pozostawienie SDR 1,5 po zaakceptowaniu wyłączenia.

Rozszerzenie regresji F05 wykazało dodatkowo, że pomijanie zdrowego build po
reconnect musi uwzględniać zmianę panelu. Test w `before-panel-change.log`
potwierdził pominięty SDR build dla nowego white point; ten wariant naprawiono
przed końcową walidacją. Następnie ponownie przejrzano wszystkie zmienione
ścieżki oraz pozostały runtime, frontend, updater i proces tworzenia paczki.

## Wydajność i zgodność koloru

Profil rzeczywistego, rozgrzanego generatora 33³ wykazał 323 441 wywołań `max`
dla PQ przed zmianą i 107 810 po niej: **215 631 zbędnych wywołań mniej**.
Nie usuwano górnej granicy ani clampu wymaganego wewnątrz samego transferu PQ.
Dolne granice nadal obsługują istniejące funkcje `pq_to_linear`, `linear_to_pq`
i `linear_to_g22`.

Pomiary bez profilera wykonywano parami, zmieniając kolejność wersji przed/po.
Mediana dziewięciu par przy rozgrzanej wejściowej siatce ICtCp:

| Build nowej LUT 33³, saturacja 1,3 | Przed | Po | Mniej czasu |
|---|---:|---:|---:|
| HDR/PQ | 198,879 ms | 182,286 ms | około 8% |
| SDR/G22 | 216,074 ms | 195,728 ms | około 9% |

`benchmark_lut_pair.py` zapisał rzeczywiście zaimportowane ścieżki i porównał
pełne pliki bajt po bajcie dla dwóch transferów, dwóch punktów bieli i pięciu
saturacji: 1,0; 1,01; 1,1; 1,3; 1,5. Wszystkie 20 porównań są identyczne.
Nowa regresja zawiera sześć niezależnych hashy z generatora sprzed zmian,
obejmujących krańce zakresu i G22 z white point innym niż D65. Istniejące
golden hashes dla 1,3 oraz testy wszystkich kroków suwaka także przechodzą.

F05 dodatkowo usuwa zbędne buildy zdrowych LUT przy odzyskaniu tej samej sesji.
Regresja dla obu looków wykazuje zero nowych buildów, nawet po zniknięciu
atomów; ponowny zapis korzysta z zachowanych plików. Zmiana parametrów panelu
nadal wymusza SDR build. Identyfikacja socketu dodaje odczyt metadanych pliku
na tick panelu, bez dodatkowego procesu xprop. Pozostała kosztowna praca
generatora wykonuje transformację, transfer, clipping i serializację 33³ węzłów.
Nie znaleziono dalszej zbędnej pracy uzasadniającej kolejną zmianę matematyki.

Podane czasy dotyczą generatora na Windows/Python 3.10. Nie obejmują debounce,
RPC Decky, odczytu przez gamescope ani prezentacji obrazu i nie są pomiarem FPS.

## Końcowy przegląd i testy

Przegląd objął całość backendu RPC, blokady, zapisy ustawień, ograniczenie liczby
workerów, generacje i anulowanie, sloty/cache LUT, EDID, zmianę panelu, obce
atomy, reconnect/resume/unload/uninstall, synchronizację QAM, timeouty i kolejkę
frontendową, walidację HTTP/ZIP, TLS, zależności, build, watch, CI i licencje.

Dodano **20 testów** w `tests/test_quality_round6.py`. Obejmują błędy reassert,
częściowy błąd jednego komponentu, zachowanie błędu nowszej LUT, neutralny
clear, niezależne hashe, NUL w ZIP, zachowanie baseline przy reconnect,
Disable podczas przerwy, odmienną/niezidentyfikowaną sesję, zniknięcie atomów,
brak zbędnych buildów, wykrycie nowego socketu przy zdrowym root oraz white
point panelu zmieniony przy reconnect. Zaktualizowano fixture izolacji sesji
i wcześniejszą regresję monitora: ownership jest zachowane do identyfikacji
serwera, podczas gdy brak obsługi HDR nadal od razu zwalnia look. Dodatkowa
regresja `before-restore-window.log` wykazała potrzebę ochrony restore już
przed tickiem monitora: znana tożsamość socketu jest sprawdzana przy samym
restore, więc stary baseline nie trafia do nowo wymienionego serwera.

- Python `-W error`: **244/244**, bez nieoczekiwanego błędu pętli w logu.
- `npm test`: **20/20**.
- `npm run typecheck`: poprawne.
- `npm audit`: **0 zgłoszonych podatności**.
- Bandit: 18 przejrzanych wskazań, 17 low i 1 medium. B108 wskazuje odczytywany
  katalog socketów, B404/B603/B607 dotyczą xprop bez powłoki z timeoutem
  i oczyszczonymi zmiennymi loadera, B110 — opcjonalnego certifi i fallbacku
  ustawień z późniejszą walidacją. Nie potwierdzono dalszej usterki.

- Czyste `npm ci`, `compileall` i `git diff --check`: poprawne.
- Końcowe `npm run package`: poprawny czysty build Rollup i ZIP z końcowego kodu.
- `npm run verify:package`: zgodność runtime i dokumentacji z checkoutem,
  CRC, ścieżki, manifesty, source map oraz źródła/licencja @decky/api sprawdzone.
  Test gotowego bundle **1/1**: Decky globals/API, kontrolki, aktualizator
  i ignorowanie odpowiedzi po unmount. Łącznie **265 testów** przechodzi.

Paczka `DeckyVibranceHDR-1.0.3.zip`: **80 631 bajtów**, 27 wpisów
(21 plików i 6 katalogów).
SHA-256: `301f5835cbb6464dafb1b9a1094c5325aa3932655bdf91a28b1d464aa990cff8`.
Wcześniejszy ZIP z tej sesji zastąpiono po ostatnim rozszerzeniu ochrony restore.

Dowody i kopie plików sprzed sesji:
`../DeckyVibranceHDR-audit-2026-10-10-round6/`. Raporty poprzednich sesji
pozostają w `docs/`; testy i raporty nie wchodzą do paczki runtime.

## Granice wyniku

Brak kolejnego potwierdzonego problemu po tych przeglądach nie jest dowodem
na brak dowolnego możliwego błędu. Nie wykonywano fizycznych testów Decky,
gamescope, koloru, opóźnienia obrazu, dockingu i suspend/resume ani zdalnego CI.
Tożsamość sesji opiera się na lokalnym sockecie X i metadanych systemu plików;
gdy nie można jej ustalić, stare ownership nie uprawnia do restore podczas
recovery. Atomowe CAS wobec innych writerów i potwierdzenie załadowania LUT
przez gamescope pozostają poza interfejsem xprop. Wykonane testy są lokalne
na Python 3.10/Node 24; CI przewiduje Python 3.13/Node 22.
