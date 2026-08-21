# Polymarket demo bot — GitHub Actions

Бот тикает каждые 5 минут (минимум GitHub для cron), внутри каждого запуска
крутится с интервалом 5 сек почти всё окно (~280 сек), чтобы не терять
реакцию на реальные сигналы. Код в этом репозитории публичный и безобиден
сам по себе (просто механика: тянет цены Binance, проверяет сигнал, шлёт
запрос к Polymarket) — конкретные настройки стратегии и вся история сделок
живут отдельно, в приватном Gist, куда попадают только через зашифрованные
GitHub Secrets.

## Настройка (один раз)

### 1. Personal Access Token
1. github.com → Settings → Developer settings → **Personal access tokens** → **Tokens (classic)** → Generate new token.
2. Права — только галочка **gist** (больше ничего не нужно).
3. Скопировать токен (показывается один раз).

### 2. Создать приватный Gist для состояния
Через curl (замените `ВАШ_ТОКЕН`):
```bash
curl -s -X POST https://api.github.com/gists \
  -H "Authorization: token ВАШ_ТОКЕН" \
  -d '{"description":"polybot state","public":false,"files":{"state.json":{"content":"{}"},"trades.jsonl":{"content":""}}}'
```
В ответе найти `"id": "..."` — это ваш `GIST_ID`.

### 3. Создать публичный репозиторий
На github.com → New repository → любое имя → **Public** → Create.

### 4. Залить код
```bash
cd github_bot
git init
git add .
git commit -m "polybot"
git branch -M main
git remote add origin https://github.com/ВАШ_ЛОГИН/ВАШ_РЕПО.git
git push -u origin main
```

### 5. Добавить Secrets
В репозитории → Settings → Secrets and variables → Actions → New repository secret:
- `GIST_TOKEN` — токен из шага 1
- `GIST_ID` — id из шага 2
- (опционально, иначе берутся значения по умолчанию конфига "Улучшенный"):
  `THRESH_PCT=0.10`, `HOLD_MIN=10`, `MAX_DEV_PCT=0.0`, `MIN_ENTRY_DIST_PCT=0.15`, `RISK_USD=50`, `MAX_ENTRY_PRICE=0.995`

### 6. Проверить
Repository → Actions → workflow "polybot" → Run workflow (запустить вручную первый раз,
дальше сам по расписанию). Смотреть логи прямо там же.

## Проверка статуса откуда угодно
```bash
GIST_TOKEN=ваш_токен GIST_ID=ваш_id python stats.py
```

## Остановить
Actions → workflow "polybot" → "..." → Disable workflow.
