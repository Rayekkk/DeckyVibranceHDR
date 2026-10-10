# Naprawa polaczenia menu z backendem na Legion Go 2

Po instalacji wersji 1.0.3 backend uruchamial sie i publikowal ustawienia
w gamescope, ale otwarcie panelu zwracalo `Backend unavailable - Python Exception`.
Poprzednia weryfikacja wdrozenia nie obejmowala odczytu stanu przez frontend.

## Przyczyna i poprawka

`Promise.resolve().then(read)` wywolywalo Decky `callable("get_state")`
z argumentem `undefined`. Transport JSON zamienial go na `null`, a dispatcher
Pythona odrzucal dodatkowy argument: `Plugin.get_state() takes 1 positional
argument but 2 were given`.

`src/stateSync.ts` wywoluje teraz `read()` wewnatrz jawnego callbacku bez
argumentow. Manifest `api_version: 1` jest poprawny; nie zmieniano sygnatur
backendu ani protokolu. Przeglad pozostalych callbackow RPC nie wykazal
kolejnych miejsc przekazujacych w ten sposob dodatkowy argument.

Nowy test odtwarza serializacje argumentow Decky i sprawdza odczyt poczatkowy
oraz odswiezenie po zapisie. Przed poprawka otrzymywal `[null]` zamiast `[]`.
Test skompilowanego bundle'a rowniez odrzuca teraz argumenty `get_state`;
poprzedni mock je ignorowal.

## Walidacja i wdrozenie

- 244 testy Pythona, 21 testow Node oraz test skompilowanego bundle'a: 266 lacznie.
- Typecheck, build, CRC/sciezki/zawartosc ZIP i `git diff --check`: poprawne.
- Ponowne wdrozenie na Legion Go 2; wszystkie 21 plikow zgodne z ZIP.
- Ustawienia zachowane; kopia poprzedniej wersji i ustawien:
  `/home/deck/homebrew/backups/DeckyVibranceHDR-20261009T235031Z`.
- Skompilowany frontend z poprzedniej paczki odtworzyl `Python Exception`
  przez prawdziwe polaczenie Decky na Legionie.
- Skompilowany frontend z nowej paczki wykonal dwa odczyty przez to samo
  rzeczywiste API; subskrybent otrzymal `ready: true`, SDR 3.0, HDR 1.4
  i puste bledy polaczenia/ustawien/aplikacji. Nie byl to mock backendu.
  Test ten sprawdza przeplyw danych, nie wizualny wyglad panelu.
- Decky aktywne; gamescope potwierdza SDR 3.0 oraz sciezke do LUT HDR.

ZIP: `DeckyVibranceHDR-1.0.3.zip`, 80869 bajtow, 27 wpisow.

SHA256: `8afb02d07e3faee3bfaae33ff12024abd808cd893885daf975f4729e80de877f`.

Dowody poza repozytorium: `DeckyVibranceHDR-audit-2026-10-10-round6/`,
`tests-after-rpc-fix.log` i `live-rpc-after-fix.log`.

Bez commita i pusha.
