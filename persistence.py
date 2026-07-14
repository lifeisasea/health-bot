"""Бесплатный бэкап SQLite-базы в приватный Hugging Face Dataset.

Зачем: на бесплатном HF Spaces файловая система эфемерная — при перезапусках
история питания может теряться. Здесь мы при старте восстанавливаем базу из
датасета, а при изменениях периодически выгружаем её обратно.

Если HF_TOKEN/HF_BACKUP_REPO не заданы — модуль ничего не делает (локальная разработка).
"""
import logging
import shutil

import config

log = logging.getLogger("health-bot.persistence")

REMOTE_NAME = "health.db"
_dirty = False
_consec_fail = 0  # подряд неудачных выгрузок (для оповещения только при стойком сбое)


def enabled() -> bool:
    return bool(config.HF_TOKEN and config.HF_BACKUP_REPO)


def _api():
    from huggingface_hub import HfApi

    return HfApi(token=config.HF_TOKEN)


def restore_on_boot() -> None:
    """Если локальной базы нет — скачать последнюю из датасета."""
    if not enabled():
        log.info("Бэкап выключен (нет HF_TOKEN/HF_BACKUP_REPO) — работаю на локальной базе.")
        return
    try:
        _api().create_repo(
            repo_id=config.HF_BACKUP_REPO, repo_type="dataset", private=True, exist_ok=True
        )
    except Exception as e:
        log.warning("Не удалось проверить/создать датасет бэкапа: %s", e)

    if config.DB_PATH.exists():
        log.info("Локальная база уже есть — восстановление не требуется.")
        return
    try:
        from huggingface_hub import hf_hub_download

        path = hf_hub_download(
            repo_id=config.HF_BACKUP_REPO,
            filename=REMOTE_NAME,
            repo_type="dataset",
            token=config.HF_TOKEN,
        )
        shutil.copy(path, config.DB_PATH)
        log.info("База восстановлена из бэкапа HF.")
    except Exception as e:
        log.info(
            "Бэкапа в датасете пока нет (первый запуск?) — начинаю с чистой базы. [%s]",
            type(e).__name__,
        )


def upload(local_path, remote_name: str) -> bool:
    """Залить произвольный файл в датасет (для токенов Garmin и т.п.)."""
    if not enabled():
        return False
    try:
        _api().upload_file(
            path_or_fileobj=str(local_path),
            path_in_repo=remote_name,
            repo_id=config.HF_BACKUP_REPO,
            repo_type="dataset",
            commit_message=f"upload {remote_name}",
        )
        return True
    except Exception as e:
        log.warning("Не удалось залить %s: %s", remote_name, e)
        return False


def download(remote_name: str, local_path) -> bool:
    """Скачать файл из датасета. Вернуть True при успехе."""
    if not enabled():
        return False
    try:
        from huggingface_hub import hf_hub_download

        path = hf_hub_download(
            repo_id=config.HF_BACKUP_REPO,
            filename=remote_name,
            repo_type="dataset",
            token=config.HF_TOKEN,
        )
        shutil.copy(path, local_path)
        return True
    except Exception:
        return False


def mark_dirty() -> None:
    """Отметить, что база изменилась и её нужно выгрузить."""
    global _dirty
    _dirty = True


def flush_if_dirty() -> None:
    """Выгрузить базу в датасет, если были изменения. Вызывается из планировщика."""
    global _dirty, _consec_fail
    if not enabled() or not _dirty or not config.DB_PATH.exists():
        return
    try:
        _api().upload_file(
            path_or_fileobj=str(config.DB_PATH),
            path_in_repo=REMOTE_NAME,
            repo_id=config.HF_BACKUP_REPO,
            repo_type="dataset",
            commit_message="backup",
        )
        _dirty = False
        if _consec_fail >= 3:  # был стойкий сбой, о котором уведомляли — дать отбой
            try:
                import alerts

                alerts.notify(
                    "backup_ok",
                    "✅ Отбой: данные снова сохраняются в облако, всё в порядке.",
                    cooldown_hours=1,
                )
            except Exception:
                pass
        _consec_fail = 0
        log.info("База выгружена в бэкап HF.")
    except Exception as e:
        _consec_fail += 1
        log.warning("Не удалось выгрузить бэкап (%d-й раз подряд): %s", _consec_fail, e)
        # данные не теряются — флаг _dirty остаётся, выгрузим на следующем заходе.
        # Беспокоим пользователя только при СТОЙКОМ сбое (≥3 подряд ≈ 9 минут),
        # а не на разовых 504/таймаутах со стороны Hugging Face.
        if _consec_fail >= 3:
            try:
                import alerts

                alerts.notify(
                    "backup",
                    "⚠️ Уже несколько раз подряд не удаётся сохранить данные в облако. "
                    "Я продолжаю работать, данные не потеряны — но если не пройдёт само, скажи, разберёмся.",
                )
            except Exception:
                pass
