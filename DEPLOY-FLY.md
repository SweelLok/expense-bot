# Деплой на Fly.io

Приложение: `expense-bot-vadim`, регион `fra`, одна машина (`shared-cpu-1x`, 256MB), том `ukf_data`
(1 GB) подключён в `/data`.

## Главное правило: база должна лежать на томе

`/app` внутри контейнера — эфемерный слой: при каждом деплое он пересоздаётся из образа.
Файл базы в `/app/expenses.db` теряется при первом же `fly deploy`. Поэтому база живёт на томе:

```bash
fly secrets set DB_PATH=/data/expenses.db -a expense-bot-vadim
```

Секрет, а не `[env]` в fly.toml — так значение не попадает в образ и в репозиторий.
Переменная `DATA_DIR=/data` в `fly.toml` историческая (код её не читает).

## Установка flyctl

```bash
curl -L https://fly.io/install.sh | sh
export FLYCTL_INSTALL="$HOME/.fly"
export PATH="$FLYCTL_INSTALL/bin:$PATH"   # добавь в ~/.bashrc
fly auth login
```

Локальный Docker не нужен: flyctl собирает образ на удалённом builder'е.

## Деплой

```bash
cd ~/expense-bot
fly deploy -a expense-bot-vadim
```

Перед деплоем стоит убедиться, что база именно там, где ожидается:

```bash
fly secrets list -a expense-bot-vadim          # должен быть DB_PATH и BOT_TOKEN
fly ssh console -a expense-bot-vadim -C "sh -c 'ls -la /data; md5sum /data/expenses.db'"
```

## Залить/забрать базу

```bash
fly sftp get /data/expenses.db backups/expenses.db -a expense-bot-vadim   # бэкап
fly sftp put build/expenses.db /data/expenses.db -a expense-bot-vadim     # залить
```

Бэкап перед деплоем — привычка, а не паранойя: снимается одной командой, а данные руками не
восстанавливаются.

## Чистая база с настроенным планом

```bash
./venv/bin/python scripts/seed_plan.py build/expenses.db 1084411025
fly sftp put build/expenses.db /data/expenses.db -a expense-bot-vadim
```

Скрипт создаёт базу заново: категории, месячные планы, регулярные платежи, плановый доход, порог,
баланс 0. Трат и доходов в ней нет.

## Проверка после деплоя

```bash
fly logs -a expense-bot-vadim | head -30     # ждём "Database ready: /data/expenses.db" и "Bot started"
fly ssh console -a expense-bot-vadim -C "sh -c 'ls -la /data/expenses.db'"
```

Если в логах `Database ready: /app/expenses.db` — секрет `DB_PATH` не применился.

## Одна реплика

Бот работает на long polling: при `count > 1` две машины опрашивают Telegram одним токеном и
получают `409 Conflict`. Поэтому в `[[vm]]` остаётся `count = 1`.
