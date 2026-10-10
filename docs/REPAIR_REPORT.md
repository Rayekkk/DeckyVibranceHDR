# Raport napraw DeckyVibranceHDR 1.0.3

Sesja: 9–10 października 2026. Baza: `41690d79a9c17c555427631e2b3e47aa6963aae7`.
Wersja lokalna: **1.0.3**, oznaczona w changelogu jako Unreleased.
Zmiany pozostają w katalogu roboczym. Nie wykonano commita, pusha ani publikacji.

Usunięto wszystkie 14 ustaleń pierwotnego audytu. Po kolejnych rundach przeglądu
naprawiono też opisane poniżej dodatkowe przypadki. Ostatni przegląd nie wykazał
kolejnego potwierdzonego błędu wymagającego naprawy w sprawdzanym zakresie.
To wynik przeglądu i testów, a nie dowód braku wszystkich możliwych błędów.

## Ustalenia pierwotnego audytu

| ID | Wdrożona naprawa | Weryfikacja |
| --- | --- | --- |
| A01 | Jeden zapis RPC w toku, scalanie niewysłanych ruchów suwaka, pierwszeństwo Disable/Reset. Wyłączenie przenosi ostatnią optymistyczną saturację. Reset aktualizuje wszystkie cztery pola. | 200 ruchów wysyła pierwszy i ostatni zapis; wyłączenie pomija oczekujący ruch; test rzeczywistego bundla sprawdza argumenty RPC. |
| A02 | Jeden worker na komponent i jedno najnowsze zlecenie. Token anulowania jest sprawdzany po wejściu w blokadę pliku, między warstwami LUT i przed podmianą pliku oraz atomu. | Zmiana podczas generowania przerywa stare obliczenia; praca unieważniona podczas oczekiwania na blokadę nie uruchamia generatora; Disable blokuje stary commit. |
| A03 | Jeden odczyt stanu w toku, polling po zakończeniu poprzedniego odczytu, timeout i widoczny błąd z ponowieniem. Wolny zapis pokazuje stan oczekiwania. | Powolny odczyt nadal publikuje wynik; timeout pozwala na ponowienie; stary odczyt nie nadpisuje zmiany. Zapisy zachowują kolejność także po przekroczeniu czasu powiadomienia. |
| A04 | Jeden snapshot czterech ustawień, zapis do unikalnego pliku tymczasowego, fsync i os.replace. Zachowanie pozostałych kluczy; aktualizacja SettingsManager po sukcesie. | Awaria podmiany zachowuje poprzedni plik i pamięć oraz usuwa plik tymczasowy. Harness odwzorowuje commit wykonywany przez setSetting. |
| A05 | Monitor wykrywa utratę sesji także przy samym HDR, unieważnia pracę, usuwa własność poprzedniej sesji i uruchamia ponowne wyszukiwanie z backoffem. | Utrata serwera uruchamia recovery; ponowny start reconfiguruje panel i stosuje ustawienia bez drugiego monitora; startup z zerową liczbą atomów nie daje ready. Brak xprop ma osobny komunikat. |
| A06 | Walidacja każdego zapisanego pola i parametrów RPC; odrzucanie bool jako liczby, NaN, Infinity i stringów; normalizacja starych zakresów. Znormalizowane pola są zapisywane również podczas oczekiwania na gamescope. | Testy typów, uszkodzonych pól i migracji. |
| A07 | Oddzielny settings_error i dirty state; błąd zapisu nie przerywa Disable/Reset; monitor ponawia zapis. | Pełny dysk nadal pozwala przywrócić poprzedni SDR i zostawia widoczny błąd ustawień. |
| A08 | Utrwalanie wartości niezależnie od aktywności LUT, wspólne scalanie zapisów obu suwaków i flush przy unload. | Wyłączony HDR zapisuje wartość; unload przed debounce utrwala ostatni ruch; oba suwaki współdzielą pojedynczy zapis. |
| A09 | Wybór dokładnego assetu z całej listy, przez parametr updatera. | Poprawny ZIP jest wybierany mimo innego ZIP-a na początku listy. |
| A10 | Walidacja sprawdzonej pary URL/nazwa przed operacjami na dysku; unikalny .part; kontrola CRC, ścieżek, plików runtime i wersji; atomowa podmiana. | Błędy sieci i ZIP zachowują poprzedni plik; udane pobranie go zastępuje; równoczesne pobranie dostaje natychmiastowy błąd zamiast blokować drugi wątek. |
| A11 | Handler sprawdza każdy redirect przed kontaktem; wymaga HTTPS, dozwolonego hosta, braku userinfo i właściwego portu. Zachowano limity redirectów urllib oraz weryfikację TLS. | Niedozwolone schematy, hosty, porty i userinfo są odrzucane; redirect do release-assets.githubusercontent.com jest dopuszczony. |
| A12 | Sprawdzanie nagłówka, długości, checksumów i granic bloków EDID. Wynik False nie zatrzymuje szukania PQ w następnych kolekcjach i rozszerzeniach. | CTA i DisplayID z mieszaną kolejnością deklaracji; uszkodzone i skrócone dane; chromatyczność i punkt bieli. |
| A13 | Minimalna konfiguracja Rollup zachowująca globale Decky i manifest, bez nieużywanego łańcucha narzędzi globowania. Lockfile i audyt w CI. | Czyste npm ci, npm audit z wynikiem 0, typecheck i test rzeczywistego bundla. |
| A14 | Aktualizacja NOTICE i pakowanie dokładnej licencji, metadanych oraz źródeł @decky/api. | Kontrola ZIP porównuje bibliotekę z zainstalowaną paczką npm i sprawdza sourcemap. |

## Kolejne rundy przeglądu

Przegląd po pierwszej naprawie wykazał dodatkowe przypadki:

- Komunikat o konflikcie na tym samym serwerze X był używany jako cache konfliktu
  na innym serwerze. Rozdzielono te stany i dodano regresję.
- Zmiana panelu mogła pozostawić HDR aktywny po utracie obsługi PQ. Dodano zwolnienie
  looka i bramki obsługi HDR także podczas zatwierdzania i reassert.
- Odczyt NaN/Infinity z atomu mógł stać się niepoprawnym baseline. Dane są filtrowane.
- Późne RPC po rozpoczęciu unload mogło zmienić już utrwalone wartości. Mutacje
  ignorują żądania po rozpoczęciu wyładowania.
- Kolejka należała do pojedynczego montowania panelu QAM. Jest teraz współdzielona
  między otwarciami, a demontaż samego plugina zatrzymuje niewysłane RPC.
- Resume mógł uruchamiać kolejne nakładające się drabinki reassert. Jeden worker
  obsługuje najnowsze wznowienie i budzi oczekującą drabinkę.
- Opóźniony błąd nieaktualnego workera mógł nadpisać bieżący błąd. Raportowanie
  sprawdza teraz token także w ścieżce wyjątku.
- Klasyfikacja panelu odczytywała EDID kilka razy, a cache nie uwzględniał zmiany
  ścieżki wewnętrznego EDID. Jeden snapshot jest współdzielony przez klasyfikację.
- Podwójny klik w aktualizatorze mógł rozpocząć drugi request przed renderem.
  Dodano natychmiastową blokadę i ochronę przed późnymi odpowiedziami po unmount.
- Pakowanie mogło użyć starego bundla lub pozostałych plików w dist. Komenda
  package przebudowuje frontend, a build czyści swój katalog wyjściowy.
- Błąd pakowania mógł pozbawić użytkownika istniejącego ZIP-a. Nowa paczka powstaje
  w stagingu i zastępuje poprzednią dopiero po sukcesie; regresja odtwarza awarię.
- Wybór pierwszego użytkownika z passwd nie musiał wskazywać użytkownika Decky.
  Updater korzysta z DECKY_USER, a zapis bez root nie wymaga chown.
- Sekwencyjne trzysekundowe timeouty xprop podczas unload mogły zużyć okno
  zamykania Decky. Przywracanie ma krótsze timeouty i wykonuje SDR/HDR równolegle,
  przed utrwalaniem ustawień. Kod Decky używa użytkownika hosta bez flagi root
  i kończy proces pozostający po pięciu sekundach; sprawdzono
  [sandboxed_plugin.py](https://github.com/SteamDeckHomebrew/decky-loader/blob/main/backend/decky_loader/plugin/sandboxed_plugin.py)
  oraz [plugin.py](https://github.com/SteamDeckHomebrew/decky-loader/blob/main/backend/decky_loader/plugin/plugin.py).
- Walidacja ZIP uwzględnia oryginalne nazwy wpisów, które Python na Windows może
  normalizować. Odrzuca również niejednoznaczne ścieżki z kropką i podwójnym separatorem.

Każdą rundę kończyły testy regresji i ponowny przegląd. Końcowy build i pakowanie
powtórzono po ostatniej zmianie kodu runtime.

## Pomiary wydajności

Pomiar lokalny: Windows, Python 3.10.9, sześć par pomiarów na transfer,
porównanie z bazowym commitem. Wartości w milisekundach są medianami kolejnej
generacji z przygotowanym cache; pierwszy build nowej siatki ma osobny koszt.

| Transfer | Przed naprawą | Po naprawie, ciepły cache | Przyspieszenie | Pierwszy build |
| --- | ---: | ---: | ---: | ---: |
| PQ | 321,61 ms | 179,49 ms | 1,79× | 276,60 ms |
| G22 | 319,04 ms | 180,95 ms | 1,76× | 282,05 ms |

Zachowano siatkę 33³, kolejność węzłów i sześć miejsc dziesiętnych.
Porównanie pełnych LUT z implementacją bazową dało identyczny tekst dla pięciu
wartości saturacji i dwóch punktów bieli w obu transferach. Regresje dodatkowo
sprawdzają wszystkie 51 kroków zakresu HDR na mniejszej siatce oraz hashe
produkcyjnych LUT 33³. Cache ma limit dwóch siatek i rozróżnia transfer oraz biel.

Test kolejki 200 ruchów jednego suwaka wysyła **2 RPC zamiast 200** przy jednym
zablokowanym zapisie. To pomiar liczby operacji w kontrolowanym scenariuszu,
a nie obietnica konkretnego czasu reakcji urządzenia.

## Końcowa weryfikacja i granice

- 171 testów backendu z ostrzeżeniami traktowanymi jako błędy.
- 15 testów Node dla kolejki i awarii pakowania oraz 1 test zbudowanego frontendu,
  który sprawdza połączenie z API Decky i rzeczywiste wywołania kontrolek.
- TypeScript typecheck, compileall, npm ci, build, package i verify:package.
- npm audit: 0 podatności, również w zależnościach narzędzi budowania.
- ZIP: CRC, katalog główny, oryginalne separatory, wymagane pliki, zgodność z
  checkoutem, brak pozostałości dist i dołączone informacje @decky/api.
- Pokrycie instrukcji i gałęzi: około 81% łącznie; main około 81%, generator
  około 94%, updater około 69%. Nie oznacza to pełnego pokrycia zachowania urządzenia.
- Bandit nadal zgłasza ogólne ostrzeżenia o subprocess, opcjonalnych fallbackach
  i standardowej ścieżce socketów X11. Przejrzano je ręcznie; nie stwierdzono
  przekazywania argumentów do shella ani używania tej ścieżki jako pliku tymczasowego.

Nie instalowano paczki na urządzeniu i nie wykonywano zmian przez SSH.
Pozostaje fizyczna walidacja obrazu, odczytu po zapisie, obciążenia, docking,
suspend/resume i przywrócenia po unload w rzeczywistym SteamOS/gamescope.

Timeout powiadomienia o zapisie RPC nie uruchamia równoległego następnego zapisu:
stara operacja może nadal działać w Decky. UI pokazuje oczekiwanie. Awaria transportu
bez odpowiedzi wymaga odzyskania lub przeładowania połączenia Decky; sam timeout
frontendu nie stanowi dowodu, że zapis backendu został anulowany.
