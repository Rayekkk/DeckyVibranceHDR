# Piąty audyt jakości i responsywności — 2026-10-10

Wykonano kolejny przegląd całego lokalnego plugina i naprawiono trzy potwierdzone
problemy z opóźnieniami oraz zbędną pracą. Punktem odniesienia jest stan checkoutu
po czwartej sesji naprawczej, a nie pierwotne wydanie 1.0.2. HEAD pozostał
`41690d79a9c17c555427631e2b3e47aa6963aae7`. Wszystkie zmiany są lokalne;
nie wykonano commita, pusha, publikacji, instalacji ani zmian na urządzeniu.

## Zakres

Przejrzano backend RPC, blokady i workerów, zapis ustawień, anulowanie oraz
sloty LUT, monitorowanie panelu i sesji, EDID, przejmowanie i odtwarzanie atomów,
resume/unload, obliczenia koloru, aktualizator, synchronizację frontendową,
obsługę QAM, build, CI i zawartość ZIP. Ponowny przegląd po naprawach skupił się
na współbieżności zapisu, retry, unieważnianiu cache i zachowaniu przy zamykaniu.

## Potwierdzone problemy i naprawy

| ID | Priorytet | Problem i skutek | Naprawa |
|---|---|---|---|
| E01 | P2 | Zapis ustawień trzymał blokadę mutacji przez fsync/replace. Disable i reset dodatkowo zapisywały przed odtworzeniem atomów. Wolny dysk opóźniał wyłączenie efektu i kolejne RPC. | Niemutowalny snapshot powstaje pod krótką blokadą, zapis działa poza nią. Toggle/reset korzystają z workera. Tylko najnowsza generacja potwierdza zapis; unload odtwarza atomy i czeka na końcowy zapis za starszym writerem. |
| E02 | P2 | Zwykły tick panelu uruchamiał xprop trzy razy: dla zdrowia sesji, flagi monitora i ścieżki EDID. | Jeden root dump dostarcza liczbę atomów oraz metadane panelu. Wspólne parsery zachowują obsługę ścieżek i UTF-8, a EDID nadal korzysta z cache. |
| E03 | P2 | Sekwencja saturacji A → B → A ponownie liczyła i zapisywała LUT A, mimo że właściwy nieaktywny slot zawierał gotowy plik. | Cache czterech rekordów metadanych identyfikuje parametry i plik. Trafienie nie generuje tekstu i nie zapisuje pliku, ale nadal przechodzi kontrolę anulowania oraz normalną publikację atomu. |

E01 i E02 odtworzono trzema nieprzechodzącymi testami w `before.log`.
E03 odtworzono osobno dla PQ i G22 w `before-slot-cache.log`: generator
uruchamiał się trzy razy zamiast dwóch. Po naprawach te scenariusze przechodzą.

Worker ustawień przyjmuje kolejne intencje podczas trwającego zapisu i zapisuje
najnowszy snapshot. Starsze zakończenie nie może wyczyścić dirty ani błędu nowej
generacji. Powtórzenia tej samej wartości i monitor nie zmieniają generacji
już aktywnego zapisu. Błąd najnowszego zapisu jest widoczny po odświeżeniu stanu;
powtórzenie wartości ponawia zapis. Końcowy flush przy unload zachowuje także
inne klucze pliku ustawień.

Cache LUT obejmuje saturację z typem, rozmiar z typem, transfer i white point.
Tożsamość pliku obejmuje urządzenie, inode, rozmiar, mtime_ns i ctime_ns.
Usunięcie lub wymiana pliku oraz zmiana parametrów wymuszają build. Anulowanie
i zakaz zapisu podczas unload obowiązują również dla trafień cache. Limit czterech
rekordów obejmuje zmiany katalogu runtime i nie zwiększa retencji tekstu LUT.
Math, precyzja pliku i rozdzielczość 33³ pozostają takie same.

Root dump nie jest atomową transakcją X11. Naprawa E02 usuwa redundantne
procesy i wykorzystuje jeden wynik odczytu; nie gwarantuje niezmienności panelu
między jego odczytem a późniejszym zastosowaniem.

## Pomiary lokalne

`benchmark_latency.py` porównuje zachowany backend sprzed tej sesji i końcowy
backend. Każdy wynik zapisuje faktycznie zaimportowaną ścieżkę. Dla LUT
porównano bajty pliku i medianę pięciu powrotów do istniejącego slotu, przy
rozgrzanym cache wejściowej matematyki w obu wersjach, rzeczywistym build 33³
oraz operacjach plikowych w katalogu tymczasowym.

| Scenariusz | Przed | Po |
|---|---:|---:|
| Powrót do LUT HDR/PQ — mediana etapu plikowego | 179,966 ms | 0,112 ms |
| Powrót do LUT SDR/G22 — mediana etapu plikowego | 192,112 ms | 0,178 ms |
| Dodatkowe buildy przy każdym powrocie | 1 | 0 |
| RPC Disable przy symulowanym zapisie 200 ms | 205,845 ms | 5,004 ms |
| Wywołanie odtworzenia przy tym samym zapisie | 205,720 ms | 4,803 ms |
| Procesy xprop na zwykły tick panelu | 3 | 1 |
| Tick z symulowanym kosztem 20 ms/proces | 61,009 ms | 20,423 ms |

Pomiary dysku i xprop używają kontrolowanych opóźnień oraz stubów operacji
sprzętowych. LUT używa prawdziwego generatora i plików, ale podane czasy nie
obejmują debounce 250 ms, RPC Decky, ładowania LUT ani prezentacji obrazu.
Nie są pomiarem końcowego opóźnienia UI lub FPS. Nowy, nieobecny w slotach
look nadal wymaga pełnego obliczenia. Zwykły zapis ustawień nadal ma debounce
50 ms, a unload stanowi barierę trwałości.

## Regresje i ponowny przegląd

Dodano 17 testów w `tests/test_quality_round5.py`: wolny zapis wobec disable,
reset i drugiego suwaka; zakończenie starszego snapshotu, zachowanie błędu i
retry; unload za starszym writerem; zdrowie sesji i metadane jednym odczytem;
external/missing metadata, nieudany odczyt, escape ścieżki i cache EDID;
powrót do slotu dla obu transferów oraz przez oba rzeczywiste RPC; zmienione
parametry, wymieniony/usunięty plik, anulowanie, unload, limit rekordów i
walidację typów przy trafieniu cache.

Dostosowano istniejące testy do asynchronicznego zapisu i nowego monitora.
Przegląd logów ujawnił też błąd izolacji starego helpera testowego: kolejne
`asyncio.run` dziedziczyły blokadę z innej pętli. Helper tworzy teraz lokalną
blokadę i czeka na anulowane taski przed przywróceniem fixture. Nie zmieniono
z tego powodu produkcyjnego modelu pojedynczej pętli Decky.

Po naprawach ponownie sprawdzono workerów, granice blokad, generacje, retry,
restore, zmiany panelu, opóźnione odpowiedzi QAM, aktualizator oraz pakowanie.
Końcowy przegląd nie ujawnił kolejnej potwierdzonej usterki w zakresie
możliwym do zweryfikowania lokalnie.

## Walidacja końcowa

- Czyste `npm ci`: poprawne; 32 zależności w audycie.
- `python -W error -m unittest discover -s tests -v`: **224/224**,
  17 nowych testów; bez nieoczekiwanego błędu pętli w logu.
- `npm test`: **20/20** (synchronizacja frontendowa i błędy pakowania).
- `npm run typecheck`, `python -m compileall` i `git diff --check`: poprawne.
- `npm audit`: **0 zgłoszonych podatności**.
- Bandit: 18 wskazań, 17 low i 1 medium; przejrzane. B108 dotyczy
  odczytywanego katalogu socketów `/tmp/.X11-unix`. B404/B603/B607 dotyczą
  subprocess/xprop wywoływanego listą argumentów, bez powłoki, z oczyszczonym
  środowiskiem i ograniczeniem czasu. B110 dotyczy opcjonalnego certifi oraz
  fallbacku odczytu ustawień, których wartości są następnie walidowane.
  Wskazania nie ujawniły kolejnej potwierdzonej usterki.
- `npm run package`: poprawny czysty build Rollup i ZIP.
- `npm run verify:package`: poprawne ścieżki, CRC, manifesty, zgodność plików
  runtime z checkoutem, source map i źródła/licencja @decky/api. Test gotowego
  bundle **1/1** sprawdza Decky globals/API, kontrolki, aktualizator i ignorowanie
  odpowiedzi po unmount. Łącznie wykonano **245 testów**.

Paczka: `DeckyVibranceHDR-1.0.3.zip`, **79 089 bajtów**, 27 wpisów
(21 plików, 6 katalogów).
SHA-256: `e277a17ec2235793064adf6868f511c85728060c04aa2bccccf165a00c01c7f7`.
Paczka zawiera końcowy runtime i aktualne README/CHANGELOG. Raporty, testy,
node_modules i cache pozostają poza ZIP.

Dowody: `../DeckyVibranceHDR-audit-2026-10-10-round5/` zawiera kopie pięciu
plików wejściowych, logi regresji przed/po, skrypt i wyniki pomiarów, logi
walidacji oraz wyniki skanerów. Wcześniejsze raporty pozostają w `docs/`.

## Granice weryfikacji

Nie przeprowadzono fizycznej walidacji Decky/gamescope, wyjścia koloru,
opóźnienia obrazu, suspend/resume ani dockingu. Nie uruchamiano zdalnego CI.
Lokalny runtime to Python 3.10 i Node 24; workflow przewiduje Python 3.13
i Node 22. Odpowiedź xprop nie potwierdza załadowania LUT przez gamescope.
Operacje atomów nie zapewniają CAS wobec innych writerów, a identyfikacja
cache pliku nie zastępuje kontroli integralności kryptograficznej.
