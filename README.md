# Arkada Soneium — квесты

Автоматизация квестов Soneium Score на [app.arkada.gg](https://app.arkada.gg): браузер (Playwright), кошелёк Rabby, ончейн-действия через Web3 (Soneium RPC).

## Установка

1. Клонировать репозиторий и перейти в каталог проекта.
2. Создать виртуальное окружение и установить зависимости:

   ```bash
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   playwright install chromium
   ```

3. Распаковать расширение Rabby-Wallet-Chrome.zip в корень проекта

## Ключи и секреты

- **keys.txt** — по одному приватному ключу кошелька на строку (в формате hex, с префиксом `0x` или без). Файл не коммитится (см. `.gitignore`).
- **mexc_api.txt** — для квеста Sake Deposit и выполнения бонусного задания Sake Finance где нужно сделать депозит 20$ и взять займ 10$. Cкрипт сначала проверит есть ли необходимая сумма ETH в Soneium, если нет, то проверить наличие необходимой суммы в сетях BASE, OP, ARB и сделает бридж в Soneium, если в этих сетях тоже нет ETH то сделает вывод ETH из MEXC в рандомной сети из BASE, OP, ARB до общей суммы в Soneium 25$. 
- **proxy.txt** — прокси для браузера, по одному на строку в формате `host:port:user:pass`

## Запуск

- Все квесты для всех кошельков из `keys.txt`:

  ```bash
  python main.py
  ```

- Только один квест (например, Velodrome):

  ```bash
  python main.py --quest velodrome
  ```

Доступные имена для `--quest`: `score`, `uniswap`, `stargate_tvl`, `stargate`, `nfts2me`, `untitled_tvl`, `velodrome`, `kyo_tvl`, `sake_tvl`, `sake_deposit`, `sake_borrow`.


## completed_quests.json

В корне проекта создаётся (или обновляется) файл **completed_quests.json** — локальная база выполненных квестов по кошелькам. Формат:

- `wallets` → адрес кошелька → `quests` → идентификатор кампании (последний сегмент URL) → статус (`already_claimed`, `verified_and_claimed`, `reward_claimed`).

Перед открытием страницы квеста скрипт проверяет этот файл: если для кошелька квест уже в статусе «награда забрана», страница не открывается (экономия времени и трафика). Файл и резервная копия `completed_quests.json.bak` в `.gitignore` не попадают.

## Логи

Логи пишутся в консоль и в каталог **logs/** — один файл на день, ротация по дням, хранение 14 дней. Каталог `logs/` добавлен в `.gitignore`.