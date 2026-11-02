# findex

[![CI](https://github.com/Giovann1g/findex/actions/workflows/ci.yml/badge.svg)](https://github.com/Giovann1g/findex/actions/workflows/ci.yml)

Власна повнотекстова пошукова система, яку я будую протягом семестру в курсі
[Python — Build a Search Engine](https://github.com/rmalkevy/Programming-Practice-Projects/blob/main/courses/python/README.md).
Кожна лабораторна додає один крок; стан після кожної позначено git-тегом (`lab-01`, `lab-02`, …).
З лаби 4 це встановлюваний пакет з командою `findex` (реліз `v0.4.0`).

## Встановлення

```bash
uv tool install .                         # з клону репозиторію: findex з'являється в PATH
uv tool install https://github.com/Giovann1g/findex/releases/download/v0.4.0/findex-0.4.0-py3-none-any.whl
uv build && uv tool install dist/findex-0.4.0-py3-none-any.whl     # або з локально зібраного wheel
findex --help
```

## Корпус

**Документація CPython** — вихідні `.rst`-файли каталогу
[`Doc/`](https://github.com/python/cpython/tree/main/Doc) репозиторію `python/cpython`
(гілка `main`, коміт `386d9d0`, жовтень 2026).

| Параметр | Значення |
|---|---|
| Документів | 560 файлів `.rst` (1 файл = 1 документ) |
| Обсяг | ≈ 15 МБ тексту |
| Мова | англійська + фрагменти коду |

Чому саме він: це текст, яким я реально користуюсь щодня, тож шукати по ньому цікаво
(«де описано `casefold`?»). До кінця курсу корпус буде розширено до ~1 000 документів /
~50 МБ (переклади документації та сторінки, зібрані async-краулером у Lab 6).

**Як отримати** (каталог `data/` у `.gitignore`, корпус у git не потрапляє):

```bash
./scripts/get_corpus.sh      # sparse-clone cpython/Doc → data/python-docs/
```

## Запуск

```bash
findex index data/ --out index.pkl --positions        # індекс (смуга прогресу rich)
findex search index.pkl 'python AND (async OR await) NOT java "event loop"'
findex search index.pkl "unicode normalization" --scorer tfidf -k 5 --json | jq .title
findex stats index.pkl --top 20
findex -vv search index.pkl "dict hash"                # DEBUG: @timed і кеш запитів у stderr

# розробка
uv sync                                                # проєкт + dev-інструменти з uv.lock
uv run pytest                                          # 147 тестів; -m "not slow" — швидко
uv run pytest --cov                                    # покриття
uv run pyright && uv run ruff check . && uv run ruff format --check .

# дослідницькі скрипти, що друкують таблиці для README
uv run python -m findex.stats data/ [--mode eager]     # Lab 1: eager vs lazy
uv run python -m findex.bench data/ --limits 50 150 300 0
uv run python -m findex.bench_index data/              # Lab 2: пам'ять і формати
uv run python -m findex.bench_search index.bin         # Lab 2: merge vs set
uv run python -m findex.sanity index.pkl               # Lab 3: три перевірки рейтингу
uv run python -m findex.evaluate index.pkl eval/queries.json   # Lab 3: precision@5
```

Розділи лаб 2–3 нижче показують команди тих версій (`python -m findex.index`,
`python -m findex.search`); з v0.4.0 це `findex index` і `findex search`.

## Структура

```
pyproject.toml   метадані (PEP 621), залежності, група dev, [project.scripts], ruff/pyright/pytest
uv.lock          точні версії всіх залежностей (комітиться)
.github/workflows/
  ci.yml         push і PR: uv sync, ruff, ruff format, pyright, pytest + перевірка wheel
  release.yml    тег v*: uv build і wheel + sdist у GitHub Release
src/findex/
  cli.py       typer-застосунок: findex index / search / stats; логування — лише тут  # Lab 4
  __main__.py  python -m findex == findex
  corpus.py    iter_documents(root) -> Iterator[Document]   # генератор, 1 файл за раз
  tokenize.py  tokenize(text) -> Iterator[str]              # генератор на re.finditer
  index.py     Posting, DocMeta, Index (Mapping), build_index
  merge.py     intersect / union / difference, рушії merge і set
  query.py     Term/Phrase/And/Or/Not, лексер, парсер рекурсивним спуском
  rank.py      Scorer (Protocol), TfIdf, BM25, SearchResult, search (heapq.nlargest)
  snippets.py  вікно ±80 символів навколо найкращого збігу, підсвітка
  store.py     save/load/open_index: pickle, json, власний .bin (array)
  timing.py    @timed (functools.wraps, ParamSpec)
  stats.py, bench.py, bench_index.py, bench_search.py, sanity.py, evaluate.py — дослідницькі скрипти
eval/
  queries.json 10 запитів з ручною розміткою релевантних документів
tests/
  conftest.py       спільний корпус на диску + свіжий індекс на кожен тест       # Lab 4
  test_pipeline.py  збірка на корпусі з conftest, Unicode, фрази, рейтинг обох скорерів  # Lab 4
  test_cli.py       findex через typer.testing.CliRunner: вивід, --json, -vv, помилки  # Lab 4
  test_properties.py  5 властивостей Hypothesis                                # Lab 4
  test_tokenize.py, test_corpus.py, test_index.py, test_model.py, test_query.py, test_rank.py
```

`iter_documents` підтримує дерево каталогів (`*.txt`, `*.md`, `*.rst`, а також їхні `*.gz`),
один JSON-lines файл (`*.jsonl`, `*.jsonl.gz`) або окремий текстовий файл. Файл із
некоректним UTF-8 не зупиняє обробку: невалідні байти замінюються на `�`, подія логується.

## Правила токенізації

Нормалізація (у такому порядку):

1. `unicodedata.normalize("NFC", …)` — `e` + U+0301 і `é` (U+00E9) стають однаковими.
2. `str.casefold()` (не `lower()`): `Straße` → `strasse`.
3. Типографські апострофи `’ ‘ ʼ` → ASCII `'`; м'який перенос U+00AD видаляється.
4. Комбіновані знаки, що лишилися після NFC (наголос у `мо́ва` — для кириличної «о» з
   акутом немає готового символу), видаляються, інакше `\w` розрізав би слово навпіл.
   Літери `й`, `ї` після NFC є єдиними символами і не страждають.

Що є токеном (регулярка `[^\W_]+(?:['_][^\W_]+)*`):

| Випадок | Правило | Приклад |
|---|---|---|
| Літери/цифри | будь-які Unicode-літери й цифри | `Привіт` → `привіт` |
| Апострофи | лишаються **всередині** слова, по краях відкидаються | `don't`, `п’ять` → `п'ять`; `'hi'` → `hi` |
| Дефіси | **розділяють** слово | `state-of-the-art` → `state`,`of`,`the`,`art` |
| Цифри | окремі токени; крапка розділяє | `3.13` → `3`,`13`; `utf8` — один токен |
| Підкреслення | всередині слова лишаються (ідентифікатори) | `sys_path`; `__init__` → `init` |
| Решта | розділювачі | `Hi!!! (yes)` → `hi`,`yes` |

## Результати на повному корпусі

```
documents:   560
tokens:      1,808,119
vocabulary:  41,008
elapsed:     2.63 s   (без tracemalloc; 7.85 s з трасуванням)
peak memory: 7.8 MiB
```

Топ-10: `the` 98 885 · `a` 41 155 · `to` 36 669 · `is` 34 239 · `of` 29 103 · `and` 28 057 ·
`in` 25 325 · `for` 19 773 · `class` 16 002 · `be` 15 515. Повний топ-50 друкує
`python -m findex.stats data/`. Помітні «документаційні» терми: `func`, `meth`, `mod` —
це ролі reStructuredText (`:func:`…``), які в наступних лабах варто відфільтрувати.

## Вимірювання: eager vs lazy

Python 3.13, Linux x86-64. Кожен запуск — окремий процес. Пікова пам'ять — `tracemalloc`;
час — найкращий із 3 запусків **без** трасування (трасування саме уповільнює код у 3–4 рази,
тому його час наведено окремо).

| Version | Documents | Tokens | Peak memory | Elapsed | Elapsed (traced) |
|---|---:|---:|---:|---:|---:|
| eager (lists) | 50 | 84,582 | 5.4 MiB | 0.13 s | 0.49 s |
| lazy (generators) | 50 | 84,582 | 1.5 MiB | 0.13 s | 0.48 s |
| eager (lists) | 150 | 415,333 | 25.1 MiB | 0.50 s | 2.03 s |
| lazy (generators) | 150 | 415,333 | 3.0 MiB | 0.51 s | 1.43 s |
| eager (lists) | 300 | 792,713 | 47.9 MiB | 0.93 s | 3.30 s |
| lazy (generators) | 300 | 792,713 | 3.3 MiB | 0.95 s | 2.98 s |
| **eager (lists)** | **560** | **1,808,119** | **109.3 MiB** | **2.58 s** | 9.36 s |
| **lazy (generators)** | **560** | **1,808,119** | **7.8 MiB** | **2.63 s** | 7.85 s |

**Куди пішла пам'ять.** Жадібна версія тримає в RAM одночасно три речі. (1) Тексти всіх
560 документів — ≈ 18 МіБ (більше за 15 МБ на диску, бо `str` з не-ASCII символом зберігається
по 2–4 байти на символ). (2) Головний споживач — ≈ 1,7 млн окремих об'єктів `str` для
токенів, ≈ 75 МіБ: кожне слово, навіть трилітерне `the`, — це окремий об'єкт із ~50 байтами
заголовка, а `re` створює новий об'єкт на кожен збіг, тож 98 тисяч `the` — це 98 тисяч копій.
(3) Самі списки — ≈ 15 МіБ: по 8-байтовому вказівнику на кожен токен. Разом ≈ 110 МіБ, що
збігається з виміряними 109 МіБ, і росте лінійно з розміром корпусу (у 3,6 раза більше за lazy
на 50 документах, у 14 разів — на повному корпусі). У лінивій версії всі проміжні стадії — потоки: у будь-який момент живі лише
поточний документ, поточний токен і `Counter`. Пам'ять лінивої версії не нульова, бо
`Counter` — це відповідь, і він легітимно росте зі словником (41 тис. ключів ≈ 3 МіБ), а
найбільший документ разом з його нормалізованими копіями (NFC, `casefold`, `translate`,
`re.sub` — кожна повертає новий рядок) займає ще кілька МіБ; саме тому пік зростає
стрибкоподібно, коли до вибірки потрапляє великий файл. Час однаковий: роботу (регулярка
+ нормалізація) виконано ту саму, генератори лише не зберігають проміжні результати.

---

# Лаба 2 — інвертований індекс

## Що побудовано

`build_index(docs)` за **один прохід** по лінивому конвеєру з лаби 1 будує:

| Структура | Тип | Для чого |
|---|---|---|
| `postings` | `dict[str, list[Posting]]` | терм → постінги, **відсортовані за `doc_id`** |
| `doc_lengths` | `dict[int, int]` | кількість токенів документа (BM25 у лабі 3) |
| `doc_meta` | `dict[int, DocMeta]` | шлях і заголовок для показу результату |

```python
@dataclass(frozen=True, slots=True)
class Posting:
    doc_id: int
    tf: int
    positions: tuple[int, ...] | None = None  # лише з --positions


@dataclass(frozen=True, slots=True)
class DocMeta:
    doc_id: int
    path: str
    title: str  # перший заголовок reST, ролі на кшталт :mod:`!x` прибрано
```

Обидва класи незмінні та хешовані, тож їх можна класти в `set` (тест
`test_records_are_frozen_slotted_hashable`). Постінги збираються в `defaultdict(list)`.
Для кожного документа рахується окремий `Counter`, який після кінця документа «зливається»
в постінги й відкидається. `doc_id` — це щільні числа `0..N-1` у порядку потоку, і кожен
документ дописується **після** попередніх. Тому кожен список постінгів відсортований
за побудовою, окреме сортування не потрібне.

Повний корпус: **560 документів, 41 008 термів, 328 804 постінги** (у середньому 8 на терм).

## Boolean-пошук

```
query   := operand (op operand)*        # обчислюється зліва направо, без пріоритетів
op      := AND | OR | <нічого> (= AND)
operand := [NOT] слово
```

Оператори пишуться ВЕЛИКИМИ літерами, малі `and`/`or`/`not` — звичайні слова. Кожне слово
проходить той самий `tokenize`, що й корпус (`Straße` знайде `strasse`), а слово з дефісом
стає AND своїх частин. `NOT x` на початку дає доповнення до всіх документів.

* `--engine merge` — двовказівникові `intersect` / `union` / `difference`, O(m + n), без хешування;
* `--engine set` — `set(a) & set(b)` / `|` / `-`, потім `sorted`.

```text
$ uv run python -m findex.search index.bin "generator NOT async" --show 3
query:   'generator NOT async'  -> [('generator',), ('async',)]
hits:    54 of 560 documents   [engine=merge]
      4  About this documentation                                      python-docs/about.rst
     20  Pending removal in Python 3.21                                python-docs/deprecations/pending-removal-in-3.21.rst
     32  secrets --- Generate secure random numbers for managing secr  python-docs/library/secrets.rst
  ... and 51 more
load:    517.1 ms
query:   1.081 ms
peak memory: 21.6 MiB
```

CLI міряє час під увімкненим `tracemalloc` (щоб заразом показати пік пам'яті), тому
він більший за чисті цифри в таблицях нижче: той самий `load` без трасування — 68 мс.

### Бенчмарк merge vs set

Вимірюється лише сама булева операція (найкращий із 5 × 2000 викликів). Обидва движки
отримують ті самі списки `doc_id`. Пару рідкісних термів вибрано з hapax legomena (df = 1,
tf = 1) з фіксованим seed.

| Terms | df | Op | Hits | merge, µs | set, µs | Faster | merge+bisect, µs |
|---|---|---|---:|---:|---:|---|---:|
| 2 most common: `the`, `in` | 552, 536 | AND | 535 | 40.89 | 22.97 | set ×1.8 | 50.20 |
| 2 most common: `the`, `in` | 552, 536 | OR | 553 | 41.33 | 20.35 | set ×2.0 | — |
| 2 most common: `the`, `in` | 552, 536 | AND NOT | 17 | 35.66 | 14.14 | set ×2.5 | — |
| 2 rarest: `whitacre`, `throttle` | 1, 1 | AND | 0 | 0.14 | 0.23 | merge ×1.7 | 0.13 |
| 2 rarest: `whitacre`, `throttle` | 1, 1 | OR | 2 | 0.24 | 0.28 | merge ×1.2 | — |
| 2 rarest: `whitacre`, `throttle` | 1, 1 | AND NOT | 1 | 0.20 | 0.25 | merge ×1.3 | — |
| common + rare: `the`, `whitacre` | 552, 1 | AND | 1 | 27.81 | 4.86 | set ×5.7 | 0.23 |
| common + rare: `the`, `whitacre` | 552, 1 | OR | 552 | 32.29 | 10.59 | set ×3.0 | — |
| common + rare: `the`, `whitacre` | 552, 1 | AND NOT | 551 | 31.82 | 10.58 | set ×3.0 | — |

**Чому так.** Асимптотика однакова, O(m + n): merge проходить обидва списки, а set хешує
обидва. На довгих списках виграє `set` приблизно вдвічі, бо хешування й перетин написано
на C, а цикл merge виконує інтерпретатор Python, ~10 байткод-операцій на крок. На
коротких списках виграє merge: `set` платить фіксовану ціну за створення двох множин і
`sorted`, а merge робить один-два порівняння. На перекошеній парі (552 і 1) звичайний merge
усе одно проходить усі 552 елементи. Справжні рушії тут роблять «галоп» (skip pointers):
двійковий пошук кожного id короткого списку в довгому, O(m log n). Колонка `merge+bisect`
показує 0,23 мкс — у 21 раз швидше за set. Крім того, merge не потребує пам'яті під
множини і може працювати потоково, а set — ні.

## Збереження: три формати

`store.save(index, path)` / `store.load(path)`, формат визначається за розширенням.

* **`pickle`** (`.pkl`) — увесь граф об'єктів `Index`. ⚠️ **Ніколи не завантажуйте чужий
  `.pkl`.** Pickle — це не дані, а програма для віртуальної машини pickle: опкод `REDUCE`
  викликає будь-яку функцію, яку можна імпортувати, з будь-якими аргументами. Тож
  підроблений файл виконає, наприклад, `os.system(...)` вже в момент `pickle.load`.
  Попередження також є в `store.py`.
* **`json`** (`.json`) — безпечний і читабельний; постінги як пари `[doc_id, tf]`.
* **`bin`** (`.bin`) — власний формат на `array('I')`: `b"FIDX"` + версія + JSON-заголовок
  (`[term, offset, count]`, метадані) + усі `doc_id` підряд + усі `tf` підряд (uint32 LE).
  `load(path, lazy=True)` / `findex.search --lazy` робить `mmap` файлу і декодує постінги
  терму лише тоді, коли запит його торкається.

| Format | Loads into | File size | Save | Load | Index in RAM after load |
|---|---|---:|---:|---:|---:|
| `pickle` of `slots` | `list[Posting]` (slots) | 5.6 MiB | 1355 ms | 704.6 ms | 32.8 MiB |
| `pickle` of `array` | `array` pairs | 4.4 MiB | 149 ms | 85.4 ms | 15.2 MiB |
| `json` | `list[Posting]` (slots) | 3.1 MiB | 941 ms | 490.4 ms | 31.7 MiB |
| `bin` | `array` pairs | 3.5 MiB | 119 ms | 68.1 ms | 14.0 MiB |
| `bin` (`--lazy`) | lazy `mmap`, decoded per term | 3.5 MiB | 119 ms | 56.4 ms | 6.3 MiB |

`json` виявився навіть меншим за `bin`. Причина: у JSON мале число займає 1–3 байти
(`[12,3]`), а в `bin` кожне — рівно 4 байти. Стиснення різниць між сусідніми `doc_id`
(gap encoding, stretch) зробило б `bin` суттєво меншим. Час завантаження `bin` --lazy майже
весь іде на розбір JSON-заголовка (41 тис. термів). Дані постінгів не читаються, доки їх не
попросять.

## Дослідження пам'яті: три представлення постінгів

Той самий корпус, кожен рядок — в окремому процесі (`python -m findex.bench_index data/`).
Пікова пам'ять і «Index in RAM» (що лишилося після побудови) — за `tracemalloc`, час —
найкращий із 3 запусків без трасування. Файл — `pickle` відповідного представлення.

| Postings representation | Peak memory (build) | Index in RAM | Build | Pickle file | Save | Load |
|---|---:|---:|---:|---:|---:|---:|
| `list[Posting]`, plain `@dataclass` | 40.3 MiB | 39.2 MiB | 2.90 s | 7.5 MiB | 429 ms | 261 ms |
| `list[Posting]`, `slots=True` | 28.4 MiB | 26.6 MiB | 2.83 s | 5.6 MiB | 1062 ms | 622 ms |
| `array('I')` pairs (doc_ids, tfs) | 19.1 MiB | 15.3 MiB | 2.77 s | 4.4 MiB | 126 ms | 70 ms |
| `list[Posting]`, `slots=True` + positions | 99.2 MiB | 97.8 MiB | 3.70 s | 11.0 MiB | 1949 ms | 1059 ms |

**Куди пішли байти** (виміряно на 1 млн екземплярів і перевірено на індексі):

* **Plain dataclass — ≈ 104 Б на постінг**: 8 Б на слот у списку + ≈ 96 Б сам об'єкт
  (заголовок, вказівник на тип, керований `__dict__` з місцем під атрибути). Цілі числа
  майже нічого не коштують: `tf` здебільшого < 257 (кешовані малі int), а один об'єкт
  `doc_id` спільний для всіх постінгів документа.
* **`slots=True` — ≈ 64 Б на постінг**: 8 Б слот + 56 Б об'єкт (заголовок 16 Б + три
  слоти по 8 Б, без `__dict__`). Різниця 40 Б × 328 804 = 12,6 МіБ, що точно збігається
  з 39,2 − 26,6. Ціна: не можна додати атрибут, якого немає у `__slots__`.
* **`array('I')` — 8 Б корисних даних на постінг** (два uint32), але кожен терм має два
  об'єкти `array` по ~80 Б заголовка плюс запас на дописування. При 8 постінгах на терм
  заголовки (≈ 7 МіБ) переважують дані (2,5 МіБ). Тому виграш лише 1,7×, а не 8×. Решта
  15,3 МіБ — словник на 41 тис. термів (0,9 МіБ), самі рядки-терми (2,0 МіБ) і `doc_meta`.
  Один спільний масив зі зсувами, як у `.bin`, прибрав би цю ціну.
* **Позиції — +71 МіБ (×3,7)**: 1,8 млн позицій, і кожна — окремий `int` > 256
  (32 Б об'єкт + 8 Б слот у кортежі), плюс заголовки 328 тис. кортежів. У файлі це лише
  +5,4 МіБ, бо pickle пише числа компактно. Через це позиції вмикаються прапорцем `--positions`.
* **Чому pickle `slots` повільніший за plain.** Для `slots=True` dataclass генерує
  Python-функції `__getstate__`/`__setstate__`, і pickle викликає їх для кожного з 328 тис.
  об'єктів. Звичайний екземпляр pickle серіалізує через `__dict__` на C-шляху.
  `array` серіалізується одним блоком байтів, тому він найшвидший.

## REPL-експерименти

| Експеримент | Результат |
|---|---|
| `sys.getsizeof` dict під час вставки | розширення на 1, 6, 11, 22, 43, 86, 171 ключах (≈ ⅔ заповнення) |
| `__hash__` повертає константу | 1 000 ключів: 34,8 мс проти 0,3 мс; 2 000: 120 мс (×3,5 за ×2 даних — O(n²)) |
| `__eq__` без `__hash__` | `Cls.__hash__ is None` — клас стає нехешованим |
| `hash("hello")` у двох запусках | різні значення; з `PYTHONHASHSEED=0` — однакові |
| `{[1]: 1}`, `hash((1, [2]))` | `TypeError: unhashable type: 'list'` |
| 1 млн екземплярів (+ слот + int) | plain 136 Б, slots 96 Б на елемент |
| `list` vs `array('I')` з 1 млн int | 40 Б vs 4,1 Б на елемент |

---

# Лаба 3 — модель об'єктів і ранжування

## `Index` як об'єкт Python

Словники з лаби 2 тепер сховані за класом `Index(collections.abc.Mapping)`: терм → постінги.
Три абстрактні методи (`__getitem__`, `__iter__`, `__len__`) дають від `Mapping` безкоштовно
`keys()`, `items()`, `values()`, `get()` і `==`. `__contains__` перевизначено лише заради
швидкості: один пошук у словнику замість `try/except KeyError`.

| Вираз | Що викликається | Результат на корпусі |
|---|---|---|
| `len(ix)` | `__len__` — кількість **термів** | 41 008 |
| `"python" in ix` | `__contains__` | `True` |
| `ix["heapq"][0]` | `__getitem__`; невідомий терм → `KeyError` | `Posting(doc_id=8, tf=3, positions=(6098, 6101, 6104))` |
| `for t in ix` | `__iter__` | терми |
| `repr(ix)` | `__repr__` | `Index(terms=41,008, docs=560, layout='slots', positions)` |
| `ix.num_docs` | `@property` | 560 |
| `ix.avg_doc_length` | `@cached_property` — рахується один раз | 3 228,8 |
| `ix.df("python")` | | 460 |
| `bool(Index({}, {}, {}))` | `__bool__` немає → `__len__` | `False` |

`open_index(path)` — контекст-менеджер на `@contextmanager`: усе до `yield` виконується на
вході, `finally` — на виході, навіть якщо тіло `with` кинуло виняток чи зробило `return`.
Для лінивого `.bin` він закриває `mmap` (тест `test_open_index_closes_even_on_error`).

```python
>>> with open_index("index.pkl") as ix:
...     for r in ix.search('python AND (async OR await) NOT java "event loop"', k=3):
...         print(r)
[findex.timed] load took 996.4 ms
[findex.index] match cache miss: (((python AND (async OR await)) AND NOT java) AND "event loop") -> 22 docs
[findex.timed] search took 0.8 ms
SearchResult(doc_id=416, score=18.482049750219783, title='A conceptual overview of asyncio')
SearchResult(doc_id=211, score=18.26804768015061, title='Developing with asyncio')
SearchResult(doc_id=338, score=18.146703377271198, title='Coroutines and tasks')
>>> ix
Index(terms=41,008, docs=560, layout='slots', positions, closed)
```

**Пастка, знайдена по дорозі.** `python -m findex.index` виконує файл як модуль `__main__`,
тому pickle записував клас як `__main__.Index` — і жодна інша програма не могла такий файл
прочитати (`AttributeError: Can't get attribute 'Index' on <module 'findex.search'>`). Тепер
блок `if __name__ == "__main__"` викликає `main()` з імпортованого `findex.index`.

## Ранжування: `Scorer`, TF-IDF, BM25

`Scorer` — це `typing.Protocol` з одним методом `score(term, posting, index) -> float`.
`TfIdf` і `BM25` його не наслідують: `search` покладається лише на наявність методу
(duck typing), а `@runtime_checkable` дозволяє ще й `isinstance(x, Scorer)`.

```
TfIdf:  (1 + ln tf) · ln(N / df)
BM25:   idf · tf·(k1 + 1) / (tf + k1·(1 − b + b·dl/avgdl)),   idf = ln(1 + (N − df + 0.5)/(df + 0.5))
```

`search(index, query, scorer=BM25(), k=10)`: дерево запиту визначає, *які* документи
підходять; кожен позитивний терм (не під `NOT`) додає `scorer.score` кожному з них;
`heapq.nlargest(k, …)` бере топ-k за O(n log k) замість повного сортування O(n log n).
Результати — `SearchResult(doc_id, score, title)` з `order=True`: порівнюється лише
прихований `rank_key = (−score, doc_id)`, тож `sorted(results)` — від найкращого.

### Три sanity-check на корпусі (`python -m findex.sanity index.pkl`)

Index: `Index(terms=41,008, docs=560, layout='slots', positions)`, average document length 3,228.8 tokens.

#### 1. Rare term beats common term: `python OR heapq`

df(`python`) = 460, df(`heapq`) = 16 of N = 560.

| | idf BM25 | idf TF-IDF | one occurrence in an average-length doc, BM25 |
|---|---:|---:|---:|
| `python` | 0.197 | 0.197 | 0.197 |
| `heapq` | 3.526 | 3.555 | 3.526 |

- BM25(k1=1.5, b=0.75): the only document with `heapq` but no `python` (*queue --- A synchronized queue class*, tf=1, 1,803 tokens) is ranked **#12** of 461; it outranks **445 of 445** documents that contain only `python`.
- TfIdf(): the only document with `heapq` but no `python` (*queue --- A synchronized queue class*, tf=1, 1,803 tokens) is ranked **#16** of 461; it outranks **445 of 445** documents that contain only `python`.

#### 2. Saturation: term `coroutine` in a document of average length (3,229 tokens)

| tf | BM25 score | BM25: added by the tf-th occurrence | TF-IDF score | TF-IDF: added |
|---:|---:|---:|---:|---:|
| 1 | 2.350 | 2.3500 | 2.358 | 2.3576 |
| 2 | 3.357 | 1.0072 | 3.992 | 1.6342 |
| 3 | 3.917 | 0.5595 | 4.948 | 0.9559 |
| 5 | 4.519 | 0.2465 | 6.152 | 0.5261 |
| 10 | 5.109 | 0.0730 | 7.786 | 0.2484 |
| 20 | 5.465 | 0.0200 | 9.421 | 0.1209 |
| 50 | 5.704 | 0.0034 | 11.581 | 0.0476 |

Under BM25 the 20th occurrence adds **0.0200**, 0.9% of what the first one added, and the score can never pass idf·(k1+1) = 5.875. TF-IDF's 1 + ln tf also flattens (5.1% for the 20th), but has no ceiling: at tf = 50 it is 4.9× the single-occurrence score, BM25 only 2.4×.

#### 3. Length normalization: query `hashing`

`hashing` occurs exactly once in *Pending removal in Python 3.19* (16 tokens) and once in *Built-in Types* (32,784 tokens, ×2049 longer).

| Scorer | score short | score long | rank short | rank long |
|---|---:|---:|---:|---:|
| BM25(k1=1.5, b=0.75) | 5.824 | 0.628 | 4 | 22 |
| BM25(k1=1.5, b=0.0) | 3.216 | 3.216 | 12 | 19 |
| TfIdf() | 3.237 | 3.237 | 12 | 19 |

With `b = 0` and with TF-IDF the two scores are equal (the tie is broken by doc id); only BM25's length normalization puts the short document ahead.

## Мова запитів (`query.py`)

```
or_expr  := and_expr ("OR" and_expr)*
and_expr := not_expr (["AND"] not_expr)*       # два операнди поруч = AND
not_expr := "NOT" not_expr | atom
atom     := WORD | "фраза" | "(" or_expr ")"
```

Один метод парсера — одне правило граматики, тому пріоритет (`NOT` > `AND` > `OR`) задається
глибиною виклику. Вузли `Term`, `Phrase`, `And`, `Or`, `Not` — frozen-dataclass-и з `&`, `|`,
`~`, і парсер будує дерево саме цими операторами. Тести порівнюють **дерева**:

```python
parse("a OR b c") == Or(Term("a"), And(Term("b"), Term("c")))
parse('python AND (async OR await) NOT java "event loop"') == (
    (Term("python") & (Term("async") | Term("await"))) & ~Term("java")
) & Phrase(("event", "loop"))
```

Слова проходять той самий токенізатор, що й корпус (`Straße` → `strasse`, `3.13` → `3 AND 13`).
Помилки показують місце: `a OR` → `query ends where a word, phrase or '(' was expected` з
кареткою під кінцем рядка. Фрази перевіряються за позиціями: старт `p` першого слова
перетинається з позиціями `i`-го слова, зсунутими на `−i`, тим самим двовказівниковим merge.
`x AND NOT y` обчислюється як різниця, без побудови доповнення `y`.

## `@timed`, `lru_cache` і сніпети

`@timed` (з `functools.wraps`) стоїть на `build_index`, `store.load` і `search`; працює і як
`@timed`, і як `@timed(level=logging.DEBUG)`. Шлях «запит → id документів» кешується
`lru_cache(maxsize=256)` **на рівні екземпляра індексу** (`Index._match_cache`): ключ — дерево
запиту, тому `a b` і `a AND b` потрапляють в один запис, а BM25 і TF-IDF ділять той самий
кеш. `lru_cache` на самому методі не підходить: `Index` нехешований (`Mapping` визначає
`__eq__`), а кеш на класі тримав би живими всі індекси, по яких колись шукали.

```text
$ uv run python -m findex.search index.pkl 'python AND (async OR await) NOT java "event loop"' -k 3 --repeat 2 --mark md
[findex.timed] load took 960.0 ms
[findex.index] match cache miss: (((python AND (async OR await)) AND NOT java) AND "event loop") -> 22 docs
[findex.timed] search took 1.1 ms
[findex.index] match cache hit: (((python AND (async OR await)) AND NOT java) AND "event loop") -> 22 docs
[findex.timed] search took 0.2 ms
[findex.index] match cache hit: (((python AND (async OR await)) AND NOT java) AND "event loop") -> 22 docs
query:  python AND (async OR await) NOT java "event loop"
tree:   And(And(And(Term('python'), Or(Term('async'), Term('await'))), Not(Term('java'))), Phrase('event loop'))
index:  Index(terms=41,008, docs=560, layout='slots', positions)

22 matching documents, top 3 by BM25(k1=1.5, b=0.75):

 1.  18.482  A conceptual overview of asyncio  (python-docs/howto/a-conceptual-overview-of-asyncio.rst)
    … :mod:`!asyncio`: the **event** **loop**, coroutine functions, coroutine objects, tasks, and ``**await**``. ========== **Event** **loop** ========== Everything in :mod:`!asyncio` happens relative …
 2.  18.268  Developing with asyncio  (python-docs/library/asyncio-dev.rst)
    … **event** **loop**, no other Tasks can run in the same thread. When a Task executes an ``**await**`` expression, the running Task gets suspended, and the **event** **loop** executes the next …
 3.  18.147  Coroutines and tasks  (python-docs/library/asyncio-task.rst)
    … get_running_loop() deadline = **loop**.time() + 20 try: **async** with asyncio.timeout_at(deadline): **await** long_running_task() …
```

Повтор того самого запиту: `match cache hit` і 0,2 мс замість 1,1 мс (третій рядок `hit` —
це підрахунок «22 matching documents» для заголовка). Сніпет — вікно ±80 символів навколо
збігу, поруч з яким найбільше *різних* термів запиту (одним проходом ковзного вікна). Позиції
в індексі — порядкові номери токенів, а не зсуви символів, тому текст сканується ще раз і кожне
слово нормалізується тим самим токенізатором: на всьому корпусі розходяться 15 токенів із
1,8 млн (знаки телугу поза діапазоном U+0300–U+036F). У терміналі збіги підсвічуються кольором
(`--mark ansi`), у файлі — `**…**`.

## precision@5: TF-IDF vs BM25 (`python -m findex.evaluate index.pkl eval/queries.json`)

10 запитів, релевантні документи розмічено вручну **до** запуску ранжування (`eval/queries.json`,
там же критерій і обґрунтування). Документ релевантний, якщо він *присвячений* темі, а не
просто згадує слова; сторінки «What's New», покажчики та глосарій — нерелевантні. «Max P@5» —
стеля для запиту, в якого менше 5 релевантних документів.

| # | Query | Relevant | Max P@5 | TF-IDF P@5 | BM25 P@5 |
|---:|---|---:|---:|---:|---:|
| 1 | `asyncio event loop` | 7 | 1.0 | 0.6 | 0.8 |
| 2 | `"context manager"` | 4 | 0.8 | 0.2 | 0.2 |
| 3 | `generator yield` | 4 | 0.8 | 0.4 | 0.4 |
| 4 | `unicode normalization` | 3 | 0.6 | 0.6 | 0.6 |
| 5 | `lru_cache` | 2 | 0.4 | 0.4 | 0.4 |
| 6 | `pickle untrusted data` | 4 | 0.8 | 0.4 | 0.8 |
| 7 | `"global interpreter lock"` | 5 | 1.0 | 0.2 | 0.2 |
| 8 | `regular expression named groups` | 2 | 0.4 | 0.4 | 0.4 |
| 9 | `sort key function` | 4 | 0.8 | 0.6 | 0.2 |
| 10 | `virtual environment` | 3 | 0.6 | 0.4 | 0.4 |
| | **mean** | | **0.72** | **0.42** | **0.44** |

**Що видно.** Загалом BM25 трохи кращий (0,44 проти 0,42 при стелі 0,72), але по запитах
результат різний. BM25 виграє на `pickle untrusted data` (0,8 проти 0,4) завдяки
нормалізації довжини: коротка сторінка *Security Considerations* піднялася на перше місце, а
великий *Built-in Functions* випав. На `asyncio event loop` BM25 так само підняв коротку
сторінку *Runners* (0,8 проти 0,6). Програє BM25 на `sort key function` (0,2 проти 0,6): ті
самі короткі сторінки (`bisect`, `heapq`, `pstats`, 1,3–2,7 тис. токенів, у `bisect` 29 разів
`key`) обходять у 10–25 разів довші, але по суті релевантні *Built-in Types* і *Built-in Functions*. Найслабше місце
обох — **фрази** (`"context manager"`, `"global interpreter lock"`: 0,2): фраза лише відбирає
документи, а бали рахуються за окремими словами, тож `multiprocessing` зі 117 входженнями
`lock` обходить FAQ про GIL (4 входження). Наступний крок — рахувати tf самої фрази.

---

# Лаба 4 — типи, тести, пакування і CLI

`findex` тепер ставиться однією командою (`uv tool install .` або з wheel релізу `v0.4.0`)
і працює з будь-якого каталогу. Перевірки, які проганяє CI на кожен push і PR:

| Перевірка | Результат |
|---|---|
| `pyright` у режимі `strict` (усі 18 модулів `src/findex`) | **0 помилок** |
| `ruff check`, `ruff format --check` | чисто |
| `pytest` | **147 тестів**, з них 5 властивостей Hypothesis |
| покриття (`pytest --cov`, рядки + гілки) | **95 %** |
| wheel у чистому оточенні: `findex --help`, `findex --version` | працює |

## Типи (pyright strict)

* **`Scorer` — це `typing.Protocol`** з одним методом `score(term, posting, index)`. `TfIdf`
  і `BM25` нічого не наслідують: pyright структурно звіряє їхні сигнатури з протоколом.
* **Три види постінгів** (`list[Posting]`, `list[PlainPosting]`, пара `array[int]`) описано
  явним union `PostingList`, а `Index.__getitem__` звужує його через `isinstance(plist, tuple)`.
  Для читання постінгів є протокол `PostingLike` з read-only властивостями: їм відповідають
  поля обох frozen-dataclass-ів.
* **Генератори:** `iter_documents -> Iterator[Document]`, `tokenize -> Iterator[str]`,
  `build_index(docs: Iterable[Document])`. Функції приймають найширший тип (`Iterable`), а
  повертають найточніший (`Iterator`).
* **`Literal`** для всіх рядкових виборів: `Layout`, `Format`, `ScorerName`, `EngineName`.
  Ті самі типи typer перетворює на варіанти в `--help` (`<bm25|tfidf>`).
* **PEP 695:** `type PostingList = …`, `type SetOp = …`, а `@timed` типізовано через
  ParamSpec — `def timed[**P, R](func: Callable[P, R]) -> Callable[P, R]` — тож декорована
  функція зберігає свою сигнатуру для pyright.
* **JSON-файли** (`.json`, заголовок `.bin`, `eval/queries.json`) описано `TypedDict`-ами.
* У `src/` немає жодного `# type: ignore`. У тестах їх три, кожен із коментарем, навіщо він
  (наприклад, присвоєння полю frozen-dataclass навмисне: тест чекає `FrozenInstanceError`).

Що strict-режим знайшов по суті, а не лише формально:

| Знахідка | Виправлення |
|---|---|
| `Phrase.evaluate` ітерував `positions`, які можуть бути `None` | `p.positions or ()` після перевірки `has_positions` |
| `pickle.load` повертає `Any`, тож «індексом» могло стати що завгодно | `isinstance(obj, Index)`, інакше `IndexFileError` |
| `@contextmanager` над `-> Iterator[...]` у typeshed позначено застарілим | `-> Generator[Index]` |
| лямбди в циклах бенчмарка втрачали звужений тип змінних циклу (і ловили б пастку B023) | `functools.partial` |
| `__init__.py` досі містив заготовку `hello()` від `uv init` | опис пакета і `__version__` |

Ще одна знахідка — на межі типів і рантайму. typer читає анотації **під час виконання**, і
псевдонім, створений оператором `type Layout = Literal[...]` (PEP 695), для нього — це об'єкт
`TypeAliasType`, а не `Literal`. Результат — `RuntimeError: Type not yet supported: Layout`.
Тому чотири `Literal`-псевдоніми, що йдуть у CLI, записано звичайним присвоєнням, з коментарем,
чому саме так.

## Тести

| Файл | Тестів | Що перевіряє |
|---|---:|---|
| `test_tokenize.py` | 10 | правила токенізації: регістр, кирилиця, NFC, апострофи, дефіси, цифри |
| `test_corpus.py` | 8 | генератор, биті кодування, `.gz`, JSON-lines, `count_documents`, скрипт `stats` |
| `test_index.py` | 26 | записи, побудова в трьох layout-ах, merge, запити, round-trip усіх форматів |
| `test_model.py` | 10 | `Index` як `Mapping`, `open_index` при винятку, кеш запитів, `@timed` |
| `test_query.py` | 34 | парсер: 18 пар «запит → дерево», помилки з позицією, фрази |
| `test_rank.py` | 15 | скорери, `SearchResult`, сніпети |
| `test_pipeline.py` | 18 | корпус із `conftest.py`: Unicode, фрази, **три перевірки рейтингу для обох скорерів** |
| `test_cli.py` | 21 | `findex` через `CliRunner`: таблиця, `--json`, `-vv`, 10 очікуваних помилок |
| `test_properties.py` | 5 | властивості Hypothesis (нижче) |

**`conftest.py`** записує шість маленьких файлів (і один `.bin`, який має бути пропущений)
один раз за сесію і дає *свіжий* індекс на кожен тест. Каталог корпусу має область
`session`, бо його лише читають. Індекс — область `function`, бо `Index` має змінний стан
(кеш запитів, `cached_property`, `close()`), і спільний індекс протягнув би побічні ефекти
одного тесту в наступний.

**Властивості Hypothesis** — твердження «для будь-якого входу»:

1. `load(save(index))` дає той самий індекс для будь-якого корпусу, у кожному з трьох форматів,
   з позиціями та без.
2. Постінги будь-якого індексу строго зростають за `doc_id`, `tf > 0`, а сума всіх `tf`
   дорівнює сумі довжин документів (кожен токен пораховано рівно раз).
3. `intersect`, `union`, `difference` і `intersect_gallop` збігаються з операціями над `set`
   (оракул) для будь-яких відсортованих списків.
4. Токенізатор на будь-якому Unicode-тексті: жодного порожнього токена чи пробілу всередині, і
   кожен токен — нерухома точка: `tokenize(t) == [t]`. Окремо прогнано на 20 000 прикладах —
   контрприкладу немає.
5. Парсер обертає `str()`: `parse(str(tree)) == tree` для випадкових дерев з `And`/`Or`/`Not`/
   фраз, зокрема зі словами `and`, `or`, `not` у нижньому регістрі.

Маркер `slow` (`pytest -m "not slow"` їх пропускає) стоїть на властивості токенізатора:
~1,4 с на першому запуску, бо 300 довільних Unicode-рядків проходять NFC і `casefold`.

### Покриття: 95 %

```
Name                     Stmts   Miss Branch BrPart  Cover
src/findex/cli.py          162      7     38      4    94%
src/findex/corpus.py        75      2     30      4    94%
src/findex/index.py        186      4     48      5    96%
src/findex/merge.py         85      1     28      0    99%
src/findex/query.py        207      6     66      2    97%
src/findex/rank.py          70      3     18      2    94%
src/findex/snippets.py      88      4     42      7    92%
src/findex/stats.py         74      1     16      1    98%
src/findex/store.py        175     13     34      6    91%
src/findex/timing.py        24      0      2      0   100%
src/findex/tokenize.py      21      0      4      0   100%
TOTAL                     1169     42    326     31    95%
```

**Що навмисно поза покриттям і чому:**

* **Дослідницькі скрипти** `bench*.py`, `sanity.py`, `evaluate.py` виключено в
  `[tool.coverage.run] omit`. Вони запускають справжній 15-мегабайтний корпус у підпроцесах і
  друкують таблиці для README. Їхній результат перевіряється тим, що таблиці відтворюються, а
  не unit-тестами, а корпус у CI не завантажується.
* **Гілки big-endian** у `store.py` (`byteswap()`): CI і розробка — x86-64, little-endian.
* **Обгортки несподіваних винятків** у `store.load` (`AttributeError`, `ImportError` з pickle,
  `struct.error`): вони перетворюють збій на одну зрозумілу помилку, а відтворити кожен
  варіант битого файлу дорожче, ніж варто.
* **`findex --version` без встановленого пакета**, `NotImplementedError` у базовому `Node`,
  `__repr__` кількох вузлів — гілки, що не несуть логіки.

## CLI

```
findex [-v|-vv] index  CORPUS --out FILE [--positions] [--limit N] [--layout …] [--format …]
findex [-v|-vv] search INDEX "QUERY" [-k 10] [--scorer bm25|tfidf] [--json] [--engine merge|set]
findex [-v|-vv] stats  INDEX [--top 20] [--json]
```

**Потоки.** Дані (таблиця, рядки `--json`, статистика) йдуть у **stdout**; смуга прогресу,
логи й помилки — у **stderr**. Тому `findex search ix.pkl "lru_cache" --json | jq .title`
бачить лише JSON:

```text
$ findex search index.pkl "lru_cache" -k 3 --json | jq -r .title
functools --- Higher-order functions and operations on callable objects
fnmatch --- Unix filename pattern matching
Programming FAQ
```

```text
$ COLUMNS=100 findex search index.pkl '"context manager" (with OR async)' -k 3
                       68 matching documents · top 3 by BM25(k1=1.5, b=0.75)
┏━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ # ┃  score ┃ document                   ┃ snippet                                                ┃
┡━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ 1 │ 13.436 │ contextlib --- Utilities   │ … :class:`ContextDecorator`, but the context manager   │
│   │        │ for with\ -statement       │ is entered and exited with :keyword:`async with`.      │
│   │        │ contexts                   │ Decorate coroutine functions and asynchronous          │
│   │        │ python-docs/library/conte… │ generator functions with this …                        │
├───┼────────┼────────────────────────────┼────────────────────────────────────────────────────────┤
│ 2 │ 12.676 │ Coroutines and tasks       │ … of the context manager. Example of catching          │
│   │        │ python-docs/library/async… │ :exc:`TimeoutError`:: async def main(): try: async     │
│   │        │                            │ with asyncio.timeout(10): …                            │
├───┼────────┼────────────────────────────┼────────────────────────────────────────────────────────┤
│ 3 │ 11.928 │ Runners                    │ … loop_factory=None, eager_tasks=False) A context      │
│   │        │ python-docs/library/async… │ manager that simplifies *multiple* async function      │
│   │        │                            │ calls in the same context. Sometimes several top-level │
│   │        │                            │ async functions …                                      │
└───┴────────┴────────────────────────────┴────────────────────────────────────────────────────────┘
```

(у терміналі збіги в сніпетах підсвічено жовтим)

**Логування** налаштовується **один раз**, у `cli.py` (`RichHandler` на stderr). Бібліотечні
модулі лише роблять `logging.getLogger(__name__)` і пишуть у стилі `log.info("indexed %d
documents", n)`. Без прапорців видно лише WARNING; `-v` — INFO (хід індексації); `-vv` —
DEBUG (`@timed` і кеш запитів):

```text
$ findex -vv search index.pkl "dict hash" -k 1 > /dev/null
DEBUG    findex.timed: load took 1306.8 ms
DEBUG    findex.index: match cache miss: (dict AND hash) -> 45 docs
DEBUG    findex.timed: search took 1.0 ms
DEBUG    findex.index: match cache hit: (dict AND hash) -> 45 docs
```

**Очікувані помилки** — один рядок у stderr і код виходу 1, без стека викликів (усі 10 випадків
перевіряє `test_cli.py`):

```text
$ findex search nope.pkl x                  → error: index file not found: nope.pkl
$ findex search index.pkl "a OR"            → error: bad query: query ends where a word, phrase or '(' was expected (at column 5)
$ findex search index.pkl '"open'           → error: bad query: unclosed quote (at column 1)
$ findex search README.md x                 → error: can't tell the index format of 'README.md' from its suffix (.pkl, .json, .bin); pass --format
$ findex search README.md x --format pickle → error: README.md is not a findex pickle (invalid load key, '#'.)
$ findex index data --out x.bin --positions → error: the .bin format has no room for positions; use .pkl or .json
$ findex index data --out no/dir/x.pkl      → error: output directory does not exist: no/dir
```

Останні дві перевіряються **до** індексації: спершу ця помилка виникала лише після обробки
всіх 560 документів. Несподівані збої (баги) і далі показують повний стек — так і має бути.

Попутна знахідка з тестів: фраза `"loop event"` знаходить текст «event loop, event loop»,
бо розділові знаки не є токенами. Так поводяться й справжні пошукові рушії.

## Пакування, CI, реліз

* `pyproject.toml` (PEP 621): метадані, версія `0.4.0`, залежності `typer` і `rich`, група
  `dev` (pytest, pytest-cov, Hypothesis, pyright, ruff), налаштування ruff/pyright/pytest/
  coverage; `uv.lock` у git.
* `[project.scripts] findex = "findex.cli:app"`. Після встановлення wheel у `bin/` з'являється
  скрипт `findex`, що робить `from findex.cli import app; sys.exit(app())`. Він записаний як
  `entry_points.txt` у `findex-0.4.0.dist-info/` всередині wheel.
* `uv build` дає `findex-0.4.0-py3-none-any.whl` і `findex-0.4.0.tar.gz`. Wheel перевірено в
  чистому віртуальному оточенні поза репозиторієм: `findex --version` → `findex 0.4.0`.
* `.github/workflows/ci.yml` — на кожен push і PR: `uv sync --locked`, `ruff check`,
  `ruff format --check`, `pyright`, `pytest --cov`, потім збірка wheel і `findex --help` у
  чистому оточенні. Бейдж — на початку README.
* `.github/workflows/release.yml` — на тег `v*`: `uv build` і публікація GitHub Release з
  wheel і sdist. Тег `v0.4.0` і є реліз:

```bash
uv tool install https://github.com/Giovann1g/findex/releases/download/v0.4.0/findex-0.4.0-py3-none-any.whl
findex --help
```
