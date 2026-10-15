# Бесплатный запуск бота на Oracle Cloud

Oracle Cloud даёт бесплатную виртуальную машину навсегда (тариф **Always Free**).
Боту её мощности хватает с запасом. Внешние порты открывать не нужно: бот сам
забирает сообщения из Telegram.

Займёт примерно 30–40 минут. Интерфейс Oracle иногда меняется, поэтому названия
кнопок могут немного отличаться.

## Что понадобится

- банковская карта для проверки при регистрации (деньги не списываются, если
  не включать платные услуги; иногда временно блокируют и возвращают ~1 $);
- токен бота от @BotFather;
- Telegram id Катаржины: пусть она напишет [@userinfobot](https://t.me/userinfobot);
- по желанию ключ Claude API для фото и расписаний ([console.anthropic.com](https://console.anthropic.com)).

## 1. Регистрация

1. Открой [oracle.com/cloud/free](https://www.oracle.com/cloud/free/) → **Start for free**.
2. Заполни данные и подтверди почту.
3. **Home Region** выбери поближе (например, Frankfurt или Amsterdam). Его потом
   нельзя поменять, а бесплатные машины создаются только в нём.
4. Введи карту для проверки и дождись письма, что аккаунт готов (обычно 5–15 минут).

## 2. Создание сервера

1. В консоли Oracle: меню ☰ → **Compute** → **Instances** → **Create instance**.
2. **Image**: нажми *Change image* → **Ubuntu** (22.04 или 24.04).
3. **Shape**: *Change shape*. Подойдёт любая с пометкой **Always Free-eligible**:
   - **Ampere → VM.Standard.A1.Flex** (поставь 1 OCPU и 6 GB памяти), или
   - **Specialty → VM.Standard.E2.1.Micro**.

   Если пишет *Out of capacity*, попробуй другую форму или повтори через пару часов.
4. **Add SSH keys** → *Generate a key pair for me* → **Save private key**. Сохрани
   файл: без него на сервер не зайти.
5. **Create**. Через пару минут статус станет **Running**. Скопируй **Public IP address**.

## 3. Подключение к серверу

**С компьютера (Mac/Linux)**, в Терминале:

```bash
chmod 600 ~/Downloads/ssh-key-*.key
ssh -i ~/Downloads/ssh-key-*.key ubuntu@ПУБЛИЧНЫЙ_IP
```

**С Windows**: в PowerShell та же команда `ssh -i путь\к\ключу.key ubuntu@ПУБЛИЧНЫЙ_IP`.

**Без компьютера**: в консоли Oracle вверху справа есть значок **Cloud Shell** (`>_`).
Загрузи в него ключ (меню ⚙ → *Upload*) и выполни те же команды.

## 4. Установка бота

На сервере по очереди:

```bash
# Docker
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
newgrp docker

# Код бота
git clone -b claude/stoic-galileo-y7batz https://github.com/gengatta/gattaca.git
cd gattaca

# Настройки
cp .env.example .env
nano .env
```

Если репозиторий приватный, GitHub спросит логин и пароль. В поле пароля нужен
[токен доступа](https://github.com/settings/tokens) с правом `repo`, а не обычный пароль.

В `nano` заполни:

```
BOT_TOKEN=токен от BotFather
ALLOWED_USER_IDS=id Катаржины
ANTHROPIC_API_KEY=ключ Claude (или оставь пустым)
```

Сохранить: `Ctrl+O`, `Enter`, выйти: `Ctrl+X`.

Запуск:

```bash
docker compose up -d --build
docker compose logs -f      # посмотреть, что бот запустился; выход — Ctrl+C
```

Готово 🎉 Катаржина открывает бота в Telegram и нажимает **Start**. Бот работает
сам и после перезагрузки сервера тоже.

## Полезное

| Что | Команда (в папке `gattaca`) |
|---|---|
| Обновить бота до новой версии | `git pull && docker compose up -d --build` |
| Перезапустить | `docker compose restart` |
| Логи | `docker compose logs --tail 100` |
| Остановить | `docker compose down` |
| Резервная копия всех дел | скопировать файл `data/assistant.db` |

**Если бот перестал отвечать.** Oracle может остановить бесплатную машину, которая
долго почти не нагружена. Зайди в **Compute → Instances** и нажми **Start**: бот поднимется
сам. Чтобы такого не было, аккаунт можно перевести на *Pay As You Go*. Машина при этом
остаётся бесплатной в пределах лимитов Always Free, но карта уже привязана к
оплате, так что следи, чтобы не создавать платные ресурсы.
