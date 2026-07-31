# Шпаргалка: бот на сервере

Всё, что нужно в повседневной работе. Сервер — Ubuntu 24.04, пользователь
`polybot`, проект в `~/flowbot_project`.

**Два разных окна, их легко перепутать:**

| Приглашение | Где ты | Что здесь делают |
|---|---|---|
| `PS C:\Users\Admin>` | свой компьютер | только `ssh` и `scp` |
| `polybot@polybot:~$` | сервер | всё остальное |

---

## Подключиться

```powershell
ssh polybot@ТВОЙ_IP
```

Спросит passphrase **ключа** (не пароль сервера). Пароль `polybot` нужен
только для `sudo` на самом сервере.

Каждый раз, зайдя на сервер, для работы с ботом:

```bash
cd ~/flowbot_project
source venv/bin/activate
```

Без `source venv/bin/activate` команды `python scan.py` и подобные не найдут
установленные пакеты.

---

## tmux — в нём живёт бот

Бот работает внутри tmux, поэтому переживает закрытие SSH, выключение
ноутбука и обрыв связи.

```bash
tmux ls                      # какие сессии есть; пусто = бот не запущен
tmux attach -t bot           # посмотреть, что происходит
tmux kill-session -t bot     # ОСТАНОВИТЬ бота
```

Внутри tmux:

| Клавиши | Что делает |
|---|---|
| `Ctrl+B`, отпустить, `D` | выйти, **не останавливая** бота |
| `Ctrl+B`, отпустить, `[` | режим прокрутки (стрелки, PageUp) |
| `q` | выйти из прокрутки |

`Ctrl+C` внутри tmux останавливает бота — не путать с выходом.

**Что бота НЕ остановит:** закрытие окон, выключение компьютера, обрыв сети.
**Что остановит:** перезагрузка сервера, `kill-session`, истечение
`RUN_DURATION_SECONDS` (сутки).

---

## Запустить

```bash
cd ~/flowbot_project
tmux new -s bot
source venv/bin/activate
python trader.py --record market.jsonl
```

Затем `Ctrl+B`, `D` — и можно закрывать окна.

Запись веди **одной** копией: рынок у всех один и тот же, а
`--record` пишет около 10 ГБ в сутки.

---

## Проверить состояние

Главная команда — показывает запись, фиды и сделки разом:

```bash
python status.py
```

Отдельные проверки:

```bash
df -h /                          # место на диске
du -h market.jsonl               # размер записи
tmux ls                          # жив ли бот
tail -3 jump_trades.csv          # последние сделки
```

**На что смотреть в `status.py`:** строка «наша цена == pm». Если совпадений
почти 100%, семь бирж не подключились, опережения нет и торговать нечем.

---

## Разобрать запись

Ради этого всё и делается. Сутки записи — потом:

```bash
cd ~/flowbot_project
source venv/bin/activate

python scan.py market.jsonl              # арбитраж / премия / лаг
python features.py market.jsonl --split  # есть ли предсказательная сила
python replay.py market.jsonl            # что дала бы стратегия
```

Перебор порогов на реальной истории:

```bash
python replay.py market.jsonl --sweep stake 1 2 5
python replay.py market.jsonl --sweep trail      0.01 0.02 0.03 0.05
python replay.py market.jsonl --sweep trail-arm  0.01 0.02 0.05 0.10
python replay.py market.jsonl --sweep stop-loss  0 0.01 0.02 0.03
python replay.py market.jsonl --sweep small      5 10 20 30
python replay.py market.jsonl --sweep max-legs   0 1 2 3
```

Каждый прогон печатает «это подгонка под одну запись» — так и есть. Порог,
не переживший вторую запись, был случайностью.

---

## Забрать файлы себе

В окне **своего компьютера**:

```powershell
scp polybot@ТВОЙ_IP:/home/polybot/flowbot_project/jump_trades.csv .
scp polybot@ТВОЙ_IP:/home/polybot/flowbot_project/market.jsonl .
```

Обратно на сервер (например новую сборку):

```powershell
scp "C:\путь\flowbot_project_XX.zip" polybot@ТВОЙ_IP:/home/polybot/
```

---

## Обновить бота на новую версию

```bash
cd ~
unzip -q flowbot_project_XX.zip -d new
cp flowbot_project/flow.env new/flowbot_project/     # пресет с ключом
cp flowbot_project/market.jsonl new/flowbot_project/      # если нужна запись
tmux kill-session -t bot
mv flowbot_project flowbot_project_old
mv new/flowbot_project .
cd flowbot_project
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python -m pytest tests/ -q
```

`flow.env` копируется отдельно и всегда — в архиве его нет и не должно
быть.

---

## Обслуживание сервера

```bash
sudo apt update && sudo apt upgrade -y   # обновления
sudo ufw status                          # файрвол активен?
sudo systemctl status fail2ban           # защита от перебора
sudo reboot                              # перезагрузка (бот НЕ поднимется сам)
```

После перезагрузки бота нужно запустить руками — см. «Запустить».

---

## Реальные деньги — только после разбора записи

```bash
nano flow.env
```

```
DRY_RUN=false
PRIVATE_KEY=0x...
FUNDER=0x...
SIGNATURE_TYPE=3
```

```bash
chmod 600 flow.env
python run.py --setup-allowances --env-file flow.env   # один раз
python trader.py --live --stake 1 --max-round 3
```

Начинать с $1 на вход и $3 потолка за раунд. Поднимать — не раньше сотни
сделок и только по цифрам из `status.py` и `replay.py`.

**Никогда:** не вписывать ключ в командную строку (попадёт в историю), не
включать Automatic Backups в панели Vultr (снимок диска скопирует ключ), не
показывать содержимое `flow.env` никому.
