"""Однократный вход в Garmin Connect (запускать ЛОКАЛЬНО, у себя на компьютере).

Что делает:
1. Спрашивает логин/пароль Garmin (вводишь ты, в своём терминале — больше нигде).
2. Если Garmin просит код (MFA) — спросит и его.
3. Получает токены доступа и заливает их в твой приватный датасет Hugging Face.
   Бот на сервере скачает их сам и начнёт собирать данные. Пароль никуда не уходит.

Запуск:
    cd ~/Downloads/health-bot
    ./venv/bin/python garmin_login.py
"""
import getpass

import garth

import config
import persistence

TOKDIR = config.GARMIN_TOKENS_DIR


def main():
    print("=== Вход в Garmin Connect ===")
    email = input("Email от Garmin: ").strip()
    password = getpass.getpass("Пароль Garmin (не отображается): ")

    print("\nВхожу… если Garmin попросит код из приложения/SMS — введи его ниже.")
    garth.login(email, password, prompt_mfa=lambda: input("Код подтверждения (MFA): ").strip())

    TOKDIR.mkdir(parents=True, exist_ok=True)
    garth.save(str(TOKDIR))
    print(f"✅ Токены сохранены локально: {TOKDIR}")

    if not config.HF_TOKEN or not config.HF_BACKUP_REPO:
        print(
            "\n⚠️ В .env не заданы HF_TOKEN/HF_BACKUP_REPO — не могу залить токены в датасет.\n"
            "Заполни их в .env и запусти скрипт ещё раз, либо передай файлы из папки выше вручную."
        )
        return

    ok = True
    for f in ("oauth1_token.json", "oauth2_token.json"):
        if (TOKDIR / f).exists():
            ok = persistence.upload(TOKDIR / f, f"garmin/{f}") and ok
    if ok:
        print("✅ Токены загружены в датасет. Бот подхватит их при следующем запуске/сборе.")
    else:
        print("⚠️ Не все токены удалось загрузить — проверь HF_TOKEN.")


if __name__ == "__main__":
    main()
