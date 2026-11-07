#!/usr/bin/env python3
"""Deploy from AWS CloudShell with Python and its preinstalled AWS CLI; no build tools."""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile

import lambda_function as bot

ROOT = Path(__file__).resolve().parent


class DeployError(Exception):
    pass


def build_template():
    template = json.loads((ROOT / "template.json").read_text())
    template["Resources"]["Bot"]["Properties"]["Code"]["ZipFile"] = (ROOT / "lambda_function.py").read_text()
    if len(json.dumps(template, ensure_ascii=False).encode()) > 51200:
        raise DeployError("Шаблон превышает лимит CloudFormation TemplateBody (51 200 байт).")
    return template


def build_artifacts(directory):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "cloudformation.json").write_text(json.dumps(build_template(), ensure_ascii=False, indent=2))
    with zipfile.ZipFile(directory / "bot.zip", "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(ROOT / "lambda_function.py", "lambda_function.py")


def load_config(settings_path):
    result = {"bot_token": bot.BOT_TOKEN, "webhook_secret": bot.WEBHOOK_SECRET,
              "recipient_username": bot.RECIPIENT_USERNAME, "recipient_chat_id": bot.RECIPIENT_CHAT_ID,
              "region": "", "stack_name": "visamb-telegram-bot", "data_retention_days": 30}
    if settings_path.exists():
        try:
            saved = json.loads(settings_path.read_text())
            if not isinstance(saved, dict) or saved.keys() - result.keys():
                raise ValueError()
            result.update(saved)
        except (ValueError, OSError):
            raise DeployError("Не удалось прочитать файл настроек. Проверьте его JSON и названия полей.") from None
    for key, environment in {"bot_token":"BOT_TOKEN", "webhook_secret":"WEBHOOK_SECRET",
                             "recipient_username":"RECIPIENT_USERNAME", "recipient_chat_id":"RECIPIENT_CHAT_ID"}.items():
        if environment in os.environ:
            result[key] = os.environ[environment]
    for key in result.keys() - {"data_retention_days"}:
        if not isinstance(result[key], str):
            raise DeployError("Настройки, кроме data_retention_days, должны быть строками.")
        result[key] = result[key].strip()
    result["recipient_username"] = result["recipient_username"].lstrip("@").lower()
    if not re.fullmatch(r"[a-z0-9_]{5,32}", result["recipient_username"]):
        raise DeployError("Укажите корректный Telegram-ник получателя.")
    if result["recipient_chat_id"] and not re.fullmatch(r"[1-9][0-9]*", result["recipient_chat_id"]):
        raise DeployError("recipient_chat_id должен быть числовым ID личного чата или пустой строкой.")
    if type(result["data_retention_days"]) is not int or not 1 <= result["data_retention_days"] <= 365:
        raise DeployError("data_retention_days должен быть целым числом от 1 до 365.")
    return result


def telegram_call(token, method, arguments):
    request = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}",
                                    data=json.dumps(arguments).encode(),
                                    headers={"Content-Type":"application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.load(response)
    except urllib.error.HTTPError as error:
        raise DeployError(f"Telegram отклонил {method} (HTTP {error.code}). Проверьте токен и повторите запуск.") from None
    except (OSError, ValueError):
        raise DeployError(f"Не удалось выполнить Telegram {method}. Проверьте соединение и повторите запуск.") from None
    if not data.get("ok"):
        raise DeployError(f"Telegram не подтвердил {method}.")
    return data.get("result")


def aws(region, *arguments, allow_error=False):
    environment = {**os.environ, "AWS_PAGER":"", "AWS_CLI_AUTO_PROMPT":"off"}
    try:
        result = subprocess.run(["aws", "--region", region, "--no-cli-pager", *arguments],
                                capture_output=True, text=True, env=environment, timeout=1800)
    except (OSError, subprocess.TimeoutExpired):
        raise DeployError("AWS CLI недоступен или операция превысила время ожидания. Проверьте стек в AWS Console.") from None
    if result.returncode and not allow_error:
        # CLI arguments only contain paths to private parameter files, never token values.
        raise DeployError("AWS CLI вернул ошибку:\n" + result.stderr.strip())
    return result


def webhook_secret(config, state_path):
    try:
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        if not isinstance(state, dict):
            raise ValueError()
    except (ValueError, OSError):
        raise DeployError("Повреждён .deploy-state.json. Восстановите файл перед повторным развёртыванием.") from None
    key = config["region"] + ":" + config["stack_name"]
    secret = config["webhook_secret"] or state.get(key) or secrets.token_urlsafe(32)
    if not isinstance(secret, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", secret):
        raise DeployError("webhook_secret должен содержать 16–256 латинских букв, цифр, '_' или '-'.")
    state[key] = secret
    descriptor = os.open(state_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as file:
        json.dump(state, file)
    state_path.chmod(0o600)
    return secret


def deploy(config, template):
    region, name = config["region"], config["stack_name"]
    existing = aws(region, "cloudformation", "describe-stacks", "--stack-name", name, allow_error=True)
    if existing.returncode and "does not exist" not in existing.stderr:
        raise DeployError("Не удалось прочитать стек CloudFormation. Проверьте регион, профиль AWS и права:\n" + existing.stderr.strip())
    creating = bool(existing.returncode)
    parameters = [{"ParameterKey":key, "ParameterValue":str(value)} for key, value in {
        "BotToken":config["bot_token"], "WebhookSecret":config["webhook_secret"],
        "RecipientUsername":config["recipient_username"], "RecipientChatId":config["recipient_chat_id"],
        "DataRetentionDays":config["data_retention_days"],
    }.items()]
    with tempfile.TemporaryDirectory(prefix="visamb-deploy-") as directory:
        folder = Path(directory)
        template_path, params_path = folder / "template.json", folder / "parameters.json"
        template_path.write_text(json.dumps(template, ensure_ascii=False))
        params_path.write_text(json.dumps(parameters))
        params_path.chmod(0o600)
        result = aws(region, "cloudformation", "create-stack" if creating else "update-stack",
                     "--stack-name", name, "--template-body", "file://" + str(template_path),
                     "--parameters", "file://" + str(params_path), "--capabilities", "CAPABILITY_IAM", allow_error=True)
        no_changes = not creating and "No updates are to be performed" in result.stderr
        if result.returncode and not no_changes:
            raise DeployError("Не удалось создать или обновить стек:\n" + result.stderr.strip())
        if not no_changes:
            print("AWS создаёт или обновляет ресурсы. Это может занять несколько минут…", flush=True)
            aws(region, "cloudformation", "wait", "stack-create-complete" if creating else "stack-update-complete", "--stack-name", name)
    result = aws(region, "cloudformation", "describe-stacks", "--stack-name", name,
                 "--query", "Stacks[0].Outputs", "--output", "json")
    return {item["OutputKey"]:item["OutputValue"] for item in json.loads(result.stdout)}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Развёртывание бота VisaMB на AWS Lambda")
    parser.add_argument("--settings", type=Path, default=ROOT / "settings.local.json")
    parser.add_argument("--region", default="")
    parser.add_argument("--stack-name", default="")
    parser.add_argument("--build-only", action="store_true", help="Собрать ZIP и полный шаблон, без обращения к AWS и Telegram")
    args = parser.parse_args(argv)
    template = build_template()
    if args.build_only:
        build_artifacts(ROOT / "dist")
        print("Собрано: telegram-bot/dist/bot.zip и telegram-bot/dist/cloudformation.json")
        return
    if not shutil.which("aws"):
        raise DeployError("Откройте AWS CloudShell: там AWS CLI уже установлен и авторизован. Либо установите AWS CLI v2 локально.")
    config = load_config(args.settings)
    config["region"] = args.region or config["region"] or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "eu-central-1"
    config["stack_name"] = args.stack_name or config["stack_name"]
    if not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-[0-9]+", config["region"]):
        raise DeployError("Укажите регион AWS, например eu-central-1.")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,47}", config["stack_name"]):
        raise DeployError("Имя стека: 1–48 латинских букв, цифр и дефисов, начиная с буквы.")
    if not config["bot_token"]:
        if not sys.stdin.isatty():
            raise DeployError("Укажите BOT_TOKEN в окружении или bot_token в settings.local.json; либо запустите скрипт в терминале для скрытого ввода.")
        config["bot_token"] = getpass.getpass("Токен из @BotFather (ввод скрыт): ").strip()
    me = telegram_call(config["bot_token"], "getMe", {})
    if not isinstance(me, dict) or not me.get("is_bot") or not me.get("username"):
        raise DeployError("Telegram не подтвердил аккаунт бота.")
    config["webhook_secret"] = webhook_secret(config, ROOT / ".deploy-state.json")
    print(f"Развёртывание @{me['username']} в {config['region']}, стек {config['stack_name']}…", flush=True)
    outputs = deploy(config, template)
    url = outputs["WebhookUrl"]
    if not url.startswith("https://") or not url.endswith(".on.aws/"):
        raise DeployError("CloudFormation вернул неожиданный URL. Проверьте Output WebhookUrl.")
    telegram_call(config["bot_token"], "setWebhook", {
        "url":url, "secret_token":config["webhook_secret"], "allowed_updates":["message", "callback_query"],
        "max_connections":1, "drop_pending_updates":False,
    })
    telegram_call(config["bot_token"], "setMyCommands", {"commands":[
        {"command":"start", "description":"Начать заявку"},
        {"command":"cancel", "description":"Отменить опрос"},
        {"command":"help", "description":"Как работает бот"},
    ]})
    info = telegram_call(config["bot_token"], "getWebhookInfo", {})
    if info.get("url") != url:
        raise DeployError("Telegram не подтвердил адрес webhook. Повторите запуск.")
    print("\nГотово. Бот: https://t.me/" + me["username"])
    print("Webhook: " + url)
    print("Получатель: @" + config["recipient_username"])
    print("Откройте бота с аккаунта получателя и нажмите Start один раз. Клиентам привязка не нужна.")
    print("Проверка опроса владельцем: /request. Ссылка для сайта: https://t.me/" + me["username"])


if __name__ == "__main__":
    try:
        main()
    except DeployError as error:
        print("Ошибка: " + str(error), file=sys.stderr)
        raise SystemExit(1)
