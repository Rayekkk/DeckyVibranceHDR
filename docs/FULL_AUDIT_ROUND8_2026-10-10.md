# Audyt jakosci — runda 8, 2026-10-10

**AUDYT CZYSTY — 0 potwierdzonych problemow do naprawy.**

Nowa, osobna sesja przegladu aktualnego kodu, po naprawie startup/unload
z rundy 7. W tej sesji znaleziono 0 problemow i naprawiono 0; nie dodawano
zmian ani testow bez potwierdzonego powodu. Nie pozostaly znane, potwierdzone
problemy lokalne. Nie oznacza to dowodu braku wszelkich mozliwych usterek.

## Zakres przegladu

- Frontend, kontrakt Decky RPC, argumenty i odpowiedzi, synchronizacja stanu,
  priorytety disable/reset, kolejki, laczenie zmian, stale odpowiedzi,
  zamkniecie/ponowne otwarcie QAM, widocznosc i bledy polaczenia.
- Backend: mutacje/locki, generacje i ograniczenie liczby workerow,
  debouncing, niezalezne zapisy ustawien, awarie dysku, finalne utrwalenie,
  start/unload i granice ostatniej poprawki przy nieodczytanych ustawieniach.
- Gamescope: wykrywanie sesji, restore baselines, retry bledow, neutralne
  ustawienia, reconnect, zmiany panelu, docking i ladder resume.
- EDID, matematyka i format LUT, ograniczone cache, anulowanie generowania,
  nieaktywny slot i publikacja plikow.
- Updater: HTTPS/TLS, redirecty, limity czasu/rozmiaru, wersje, wspolbiezne
  check/download, walidacja archiwum i zachowanie poprzedniego pliku.
- Rollup, manifest, skrypty pakowania/weryfikacji, zaleznosci i CI.

Ponownie przeanalizowano miejsca oczekiwania, offload, locki, wyjatki,
cleanup, task tracking oraz callbacki Promise/timer. Nie potwierdzono
kolejnego problemu ani optymalizacji uzasadniajacej zmiane kodu.

## Sprawdzenia wykonane w tej sesji

- Python 3.10.9: **247/247** testow, `-W error`, 13.834 s.
- Node 24.18.0: **21/21** testow; zbudowany frontend: **1/1**. Razem **269**.
- Typecheck, compileall, nowy build i `git diff --check`: poprawne.
- `verify:package`: CRC/sciezki/manifest/runtime/licencja/source map oraz
  zgodnosc istniejącej paczki z ponownie zbudowanym frontendem: poprawne.
- `npm audit`: 0 podatnosci.
- Bandit: 17 low, 1 medium, 0 high; wskazania odpowiadaja juz przejrzanym
  wywolaniom xprop/importowi subprocess, stalej sciezce socketow X11 oraz
  fallbackom odczytu ustawien i certifi. Nie sa nowymi ustaleniami audytu.
- Hashowanie 10 plikow kodu/payloadu/ZIP przed i po sprawdzeniach:
  wszystkie pozostaly identyczne. Rebuild nie zmienil zawartosci frontendu.

Orientacyjny benchmark generatora 33³ na lokalnym Windows/Python:
PQ cold 312.14 ms, warm median 198.99 ms; G22 cold 383.52 ms,
warm median 184.01 ms. Mediany obejmuja 6 prob. Sprawdzenia innych procesow
i testy mogly obciazac hosta; nie jest to porownanie optymalizacji ani
pomiar responsywnosci/FPS na Legionie.

Kod dostarczany w ZIP nie zmienil sie, wiec zachowano zweryfikowana paczke
z rundy 7: `DeckyVibranceHDR-1.0.3.zip`, **81164 bajtow**, 27 wpisow,
21 plikow. SHA256:
`d0b17a9c6e6cb0e41b6960adce0f320f8014c86c52ae05a43b40f95960b4cc03`.

Dowody: `../DeckyVibranceHDR-audit-2026-10-10-round8/`.

## Ograniczenia

W tej rundzie nie laczono sie z urzadzeniem, nie instalowano paczki i nie
restartowano Decky. Nie wykonano fizycznej walidacji obrazu, UI, suspend/resume,
dockingu ani ostatniej poprawki startup/unload na rzeczywistym runtime Decky.
Te testy nie sa zaliczone przez wynik audytu lokalnego. Nie uruchamiano
GitHub CI ani lokalnej macierzy Python 3.13/Node 22. Mockowane testy gamescope
sprawdzaja logike, nie fizyczne dzialanie ekranu.

Bez zmian w kodzie runtime lub testach, bez commita/pusha/PR/release.
HEAD nadal `41690d79a9c17c555427631e2b3e47aa6963aae7`.
