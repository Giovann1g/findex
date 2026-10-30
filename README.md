# findex

Власна повнотекстова пошукова система, яку я будую протягом семестру в курсі
[Python — Build a Search Engine](https://github.com/rmalkevy/Programming-Practice-Projects/blob/main/courses/python/README.md).
Кожна лабораторна додає один крок; стан після кожної позначено git-тегом (`lab-01`, `lab-02`, …).

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
uv sync
uv run python -m findex.stats data/              # статистика (lazy, за замовчуванням)
uv run python -m findex.stats data/ --limit 100  # лише перші 100 документів (itertools.islice)
uv run python -m findex.stats data/ --mode eager # «жадібна» версія через списки
uv run python -m findex.bench data/ --limits 50 150 300 0   # таблиця eager vs lazy

# Lab 2 — інвертований індекс
uv run python -m findex.index data/ --out index.bin          # побудувати й зберегти
uv run python -m findex.search index.bin "dict hash"          # пошук (AND за замовчуванням)
uv run python -m findex.search index.bin "asyncio OR threading NOT thread" --engine set
uv run python -m findex.bench_index data/                     # таблиці пам'яті та форматів
uv run python -m findex.bench_search index.bin                # merge vs set

# Lab 3 — ранжування, мова запитів, сніпети
uv run python -m findex.index data/ --out index.pkl --positions   # позиції потрібні для фраз
uv run python -m findex.search index.pkl 'python AND (async OR await) NOT java "event loop"'
uv run python -m findex.search index.pkl "unicode normalization" --scorer tfidf -k 5
uv run python -m findex.search index.pkl "dict hash" --repeat 2  # другий запуск — cache hit
uv run python -m findex.sanity index.pkl                          # три перевірки рейтингу
uv run python -m findex.evaluate index.pkl eval/queries.json      # precision@5
uv run pytest                                     # тести
uv run ruff check . && uv run ruff format --check .
```

## Структура

```
src/findex/
  corpus.py    iter_documents(root) -> Iterator[Document]   # генератор, 1 файл за раз
  tokenize.py  tokenize(text) -> Iterator[str]              # генератор на re.finditer
  stats.py     ліниве й «жадібне» обчислення статистики + CLI
  bench.py     вимірювання eager vs lazy в окремих процесах
  index.py     Posting, DocMeta, Index, build_index + CLI     # Lab 2
  search.py    парсер запитів, merge/set-движки + CLI         # Lab 2
  store.py     save/load: pickle, json, власний .bin (array)  # Lab 2
  bench_index.py   пам'ять трьох layout-ів + формати          # Lab 2
  bench_search.py  merge vs set на частих і рідкісних термах   # Lab 2
  merge.py     intersect / union / difference, рушії merge і set (з search.py)
  query.py     Term/Phrase/And/Or/Not, лексер, парсер рекурсивним спуском  # Lab 3
  rank.py      Scorer, TfIdf, BM25, SearchResult, search (heapq.nlargest)  # Lab 3
  snippets.py  вікно ±80 символів навколо найкращого збігу, підсвітка     # Lab 3
  timing.py    @timed (functools.wraps), працює і як @timed(level=...)    # Lab 3
  sanity.py    три sanity-check рейтингу на корпусі                       # Lab 3
  evaluate.py  precision@5 для TF-IDF і BM25                              # Lab 3
eval/
  queries.json 10 запитів з ручною розміткою релевантних документів        # Lab 3
tests/
  test_tokenize.py  10 перевірок правил токенізації
  test_corpus.py    генераторність, биті кодування, .gz, JSON-lines, eager == lazy
  test_index.py     записи, побудова, merge, запити, round-trip усіх форматів
  test_model.py     Index як Mapping, open_index при винятку, кеш, @timed     # Lab 3
  test_query.py     парсер: порівняння дерев, помилки, фрази                 # Lab 3
  test_rank.py      скорери, три властивості рейтингу, SearchResult, сніпети # Lab 3
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
