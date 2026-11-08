"""VisaMB Telegram intake. Python standard library + the boto3 included in Lambda."""
from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from functools import lru_cache

# Editable defaults. Real values may be supplied through Lambda environment variables.
BOT_TOKEN = ""
WEBHOOK_SECRET = ""
RECIPIENT_USERNAME = "maa_ri888"
RECIPIENT_CHAT_ID = ""  # Optional numeric private chat ID; otherwise owner /start registers it.

LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)


class ConfigurationError(Exception):
    pass


class TelegramError(Exception):
    """Intentionally excludes request URLs, tokens and customer data."""


@dataclass(frozen=True)
class Settings:
    token: str
    secret: str
    recipient_username: str = RECIPIENT_USERNAME
    recipient_chat_id: str = RECIPIENT_CHAT_ID
    table_name: str = ""
    queue_url: str = ""
    retention_days: int = 30

    @classmethod
    def from_environment(cls):
        try:
            result = cls(
                token=os.environ.get("BOT_TOKEN", BOT_TOKEN).strip(),
                secret=os.environ.get("WEBHOOK_SECRET", WEBHOOK_SECRET).strip(),
                recipient_username=os.environ.get("RECIPIENT_USERNAME", RECIPIENT_USERNAME).strip().lstrip("@").lower(),
                recipient_chat_id=os.environ.get("RECIPIENT_CHAT_ID", RECIPIENT_CHAT_ID).strip(),
                table_name=os.environ.get("STATE_TABLE", "").strip(),
                queue_url=os.environ.get("UPDATE_QUEUE_URL", "").strip(),
                retention_days=int(os.environ.get("DATA_RETENTION_DAYS", "30")),
            )
        except ValueError:
            raise ConfigurationError("Invalid retention setting") from None
        if not all((result.token, result.secret, result.table_name, result.queue_url)):
            raise ConfigurationError("Bot is not configured")
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", result.secret):
            raise ConfigurationError("Invalid webhook secret")
        if not re.fullmatch(r"[a-z0-9_]{5,32}", result.recipient_username):
            raise ConfigurationError("Invalid recipient username")
        if result.recipient_chat_id and not re.fullmatch(r"[1-9][0-9]*", result.recipient_chat_id):
            raise ConfigurationError("Recipient chat ID must be a positive private chat ID")
        if not 1 <= result.retention_days <= 365:
            raise ConfigurationError("Retention must be between 1 and 365 days")
        return result

    @property
    def namespace(self):
        # Token rotation for the same bot keeps its data; another bot gets a separate namespace.
        return self.token.split(":", 1)[0]


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class DynamoStore:
    """Persist sessions and a resumable message outbox atomically per Telegram update."""

    def __init__(self, settings):
        import boto3  # Provided by the AWS Lambda Python runtime; not needed for pure unit tests.
        self.client = boto3.client("dynamodb")
        self.table = settings.table_name
        self.prefix = "bot:" + settings.namespace + ":"
        self.retention = settings.retention_days * 86400

    def _key(self, key):
        return {"pk": {"S": self.prefix + key}}

    def _item(self, key, value):
        ttl = 2 * 86400 if key.startswith("update:") else self.retention
        item = {**self._key(key), "payload": {"S": encode(value)}}
        if not key.startswith("recipient:"):
            item["expires_at"] = {"N": str(int(time.time()) + ttl)}
        return item

    def get(self, key):
        item = self.client.get_item(TableName=self.table, Key=self._key(key), ConsistentRead=True).get("Item")
        if not item or int(item.get("expires_at", {"N": "99999999999"})["N"]) <= time.time():
            return None
        return json.loads(item["payload"]["S"])

    def commit(self, update_id, changes, operations):
        key = "update:" + str(update_id)
        record = {"operations": operations, "cursor": 0, "done": not operations}
        writes = [{"Put": {"TableName": self.table, "Item": self._item(key, record),
                           "ConditionExpression": "attribute_not_exists(pk)"}}]
        writes.extend({"Put": {"TableName": self.table, "Item": self._item(k, v)}} for k, v in changes.items())
        try:
            self.client.transact_write_items(TransactItems=writes)
            return record
        except self.client.exceptions.TransactionCanceledException:
            existing = self.get(key)
            if existing is not None:
                return existing
            raise

    def advance(self, update_id, record, result, operation, recipient_id=None):
        updated = {**record, "cursor": record["cursor"] + 1}
        updated["done"] = updated["cursor"] == len(updated["operations"])
        key = "update:" + str(update_id)
        writes = [{"Put": {"TableName": self.table, "Item": self._item(key, updated),
                           "ConditionExpression": "payload = :previous",
                           "ExpressionAttributeValues": {":previous": {"S": encode(record)}}}}]
        if operation.get("application_id"):
            app_key = "application:" + operation["application_id"]
            application = self.get(app_key)
            if application is None:
                raise RuntimeError("Application record missing")
            application.update(status="sent", delivered_at=int(time.time()),
                               recipient_chat_id=recipient_id, telegram_message_id=result.get("message_id"))
            writes.append({"Put": {"TableName": self.table, "Item": self._item(app_key, application)}})
        self.client.transact_write_items(TransactItems=writes)
        return updated


class Telegram:
    def __init__(self, token):
        self.token = token

    def call(self, method, arguments):
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self.token}/{method}",
            data=encode(arguments).encode(), headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                data = json.load(response)
        except urllib.error.HTTPError as error:
            raise TelegramError(f"Telegram {method} failed (HTTP {error.code})") from None
        except (OSError, ValueError):
            raise TelegramError(f"Telegram {method} connection failed") from None
        if not data.get("ok"):
            raise TelegramError(f"Telegram {method} rejected the request")
        return data.get("result", {})


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    question: str
    limit: int
    choices: tuple = ()
    optional: bool = False


FIELDS = (
    Field("name", "Имя", "Как к вам обращаться? Можно написать имя или использовать имя из Telegram.", 80),
    Field("destination", "Направление", "Куда планируете поехать? Для США и Канады — платная консультация, для Шенгена, Кипра и Японии — помощь с оформлением.", 80,
          (("schengen", "Шенген"), ("cyprus", "Кипр"), ("japan", "Япония"),
           ("usa", "США — консультация"), ("canada", "Канада — консультация"), ("unsure", "Пока выбираю"))),
    Field("residence", "Гражданство и проживание", "Ваше гражданство и страна проживания? Например: «Гражданство РФ, проживаю в России».", 200),
    Field("purpose", "Цель поездки", "Какова цель поездки? Выберите вариант или напишите свой.", 240,
          (("tourism", "Туризм"), ("family", "К близким"), ("business", "Деловая поездка"), ("other", "Другая цель"))),
    Field("dates", "Сроки поездки", "Когда планируете поездку и на сколько дней? Если точных дат нет, так и напишите.", 240),
    Field("history", "Визовая история", "Какие визы у вас уже были и были ли отказы? Кратко укажите страны и примерные даты. Если виз не было, нажмите кнопку ниже.", 400,
          (("none", "Виз не было"),)),
    Field("comment", "Дополнения", "Есть ли важные детали или вопросы? Например, поездка с семьёй или сложности с документами. Этот вопрос можно пропустить.", 400, optional=True),
    Field("contact", "Контакт для ответа", "У вашего аккаунта нет Telegram-ника. Напишите контакт для ответа: телефон, email или Telegram-ник. Этот контакт нужен, чтобы консультант мог вам ответить.", 120),
)
FIELD_MAP = {field.key: field for field in FIELDS}
BASE_FIELDS = [field.key for field in FIELDS if field.key != "contact"]


def identity(user):
    return {"id": int(user["id"]), "name": " ".join(filter(None, (user.get("first_name"), user.get("last_name"))))[:128],
            "username": user.get("username", "")[:32]}


def recipient(settings, store):
    if settings.recipient_chat_id:
        return int(settings.recipient_chat_id)
    binding = store.get("recipient:" + settings.recipient_username)
    return int(binding["chat_id"]) if binding else None


def message(chat_id, text, markup=None, **extra):
    arguments = {"chat_id": chat_id, "text": text, "link_preview_options": {"is_disabled": True}}
    if markup is not None:
        arguments["reply_markup"] = markup
    return {"method": "sendMessage", "arguments": arguments, **extra}


def button(text, action, session):
    return {"text": text, "callback_data": action + ":" + session["id"]}


def chunks(text, limit=3500):
    """Telegram limits UTF-16 characters; preserve every character, including emoji."""
    result, buffer, size = [], [], 0
    for character in text:
        width = len(character.encode("utf-16-le")) // 2
        if size + width > limit:
            result.append("".join(buffer))
            buffer, size = [], 0
        buffer.append(character)
        size += width
    if buffer:
        result.append("".join(buffer))
    return result


def application_text(application):
    who, answers = application["telegram"], application["answers"]
    date = time.strftime("%d.%m.%Y %H:%M UTC", time.gmtime(application["created_at"]))
    lines = [f"ЗАЯВКА №{application['id'].upper()}", date, "",
             "Telegram: " + ("@" + who["username"] if who["username"] else "ник не указан"),
             f"Telegram ID: {who['id']}", "Имя в Telegram: " + who["name"], ""]
    for field in FIELDS:
        value = answers.get(field.key)
        if field.key == "contact" and not value:
            value = "@" + who["username"] if who["username"] else "не указан"
        lines.append(field.label + ": " + (value or "нет дополнений"))
    return "\n".join(lines)


def question(session):
    field = FIELD_MAP[session["field"]]
    rows = []
    if field.key == "name" and session["telegram"]["name"]:
        rows.append([button("Использовать имя из Telegram", "pick:name:use", session)])
    for code, label in field.choices:
        rows.append([button(label, f"pick:{field.key}:{code}", session)])
    if field.optional or (field.key == "contact" and session["telegram"]["username"]):
        rows.append([button("Пропустить", "skip", session)])
    if session["field"] != "name" or session.get("editing"):
        rows.append([button("← Назад", "back", session)])
    rows.append([button("Отменить опрос", "cancel", session)])
    text = field.question
    if field.key == "contact" and session["telegram"]["username"]:
        text = "Другой удобный контакт для ответа? Можно пропустить и использовать ваш Telegram-ник."
    return message(session["telegram"]["id"], text, {"inline_keyboard": rows})


def review(session):
    application = {**session, "created_at": session["created_at"]}
    rows = [[button("Отправить заявку", "submit", session)],
            [button("Изменить ответы", "edit", session), button("Отменить", "cancel", session)]]
    parts = chunks("Проверьте ответы перед отправкой.\n\n" + application_text(application))
    return [message(session["telegram"]["id"], part,
                    {"inline_keyboard": rows} if index == len(parts) - 1 else None)
            for index, part in enumerate(parts)]


class Intake:
    """Pure conversation decisions. Persistence and Telegram delivery are handled separately."""

    def __init__(self, settings, store):
        self.settings, self.store = settings, store

    def plan(self, update):
        callback = update.get("callback_query")
        incoming = callback.get("message", {}) if callback else update.get("message", {})
        user = callback.get("from", {}) if callback else incoming.get("from", {})
        chat = incoming.get("chat", {})
        if chat.get("type") != "private" or not user.get("id") or user.get("is_bot") or chat.get("id") != user["id"]:
            return {}, []
        who = identity(user)
        chat_id = who["id"]
        key = "session:" + str(chat_id)
        session = self.store.get(key)
        operations, changes = [], {}
        if callback:
            operations.append({"method": "answerCallbackQuery", "arguments": {"callback_query_id": callback["id"]}})
        text = str(incoming.get("text", "")).strip() if not callback else ""
        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text.startswith("/") else ""
        recipient_id = recipient(self.settings, self.store)

        def say(value, markup=None):
            operations.append(message(chat_id, value, markup))

        def save():
            changes[key] = session

        if command == "/start" and (chat_id == recipient_id or
                (recipient_id is None and who["username"].lower() == self.settings.recipient_username)):
            if recipient_id is None:
                changes["recipient:" + self.settings.recipient_username] = {"chat_id": chat_id}
            say("Получение заявок включено. Готовые заявки будут приходить в этот чат.\n"
                "Клиенты только отвечают на вопросы и подтверждают отправку.\n"
                "Для проверки клиентского опроса используйте /request.")
            return changes, operations
        if command == "/help":
            say("Я помощник Марии Беляковой. Помогаю составить заявку по визовому вопросу.\n"
                "/start — начать опрос\n/cancel — отменить\n"
                "Ответы отправляются консультанту только после вашего подтверждения.")
            return changes, operations
        if command == "/cancel":
            if session and session.get("mode") == "complete":
                say("Заявка уже отправлена. Для новой заявки нажмите /start.")
            else:
                changes[key] = {"mode": "cancelled"}
                say("Опрос отменён. Его ответы не отправлялись. Начать заново: /start.")
            return changes, operations
        if command in ("/start", "/request"):
            if recipient_id is None:
                say("Бот готовится к приёму заявок. Пожалуйста, попробуйте немного позже.")
                return changes, operations
            session = {"id": uuid.uuid4().hex[:16], "telegram": who, "answers": {}, "field": "name",
                       "mode": "question", "created_at": int(time.time())}
            save()
            say("Здравствуйте! Я помощник Марии Беляковой. Задам несколько коротких вопросов, "
                "покажу ответы и отправлю заявку только после подтверждения. "
                "Мария работает с заявлениями по Москве и Санкт-Петербургу онлайн.")
            operations.append(question(session))
            return changes, operations
        if not session or session.get("mode") in ("cancelled", "complete"):
            say("Заявка уже отправлена. Новая заявка: /start." if session and session.get("mode") == "complete"
                else "Начните опрос командой /start.")
            return changes, operations
        session = copy.deepcopy(session)
        session["telegram"] = who
        action = None
        if callback:
            data = str(callback.get("data", ""))
            if not data.endswith(":" + session["id"]):
                say("Эта кнопка относится к предыдущему опросу. Продолжите текущий или нажмите /start.")
                return changes, operations
            action = data.rsplit(":", 1)[0]
        if action == "cancel":
            changes[key] = {"mode": "cancelled"}
            say("Опрос отменён. Ответы не отправлялись. Начать заново: /start.")
            return changes, operations

        def show_review():
            if not who["username"] and not session["answers"].get("contact"):
                session.update(mode="question", field="contact", editing=False)
                operations.append(question(session))
            else:
                session.update(mode="review", editing=False)
                operations.extend(review(session))
            save()

        if session["mode"] == "review":
            if action == "edit":
                rows = [[button(field.label, "editfield:" + field.key, session)] for field in FIELDS]
                rows.append([button("← К проверке ответов", "review", session)])
                say("Какой ответ изменить?", {"inline_keyboard": rows})
            elif action and action.startswith("editfield:") and action.split(":", 1)[1] in FIELD_MAP:
                session.update(mode="question", field=action.split(":", 1)[1], editing=True)
                save()
                operations.append(question(session))
            elif action == "submit":
                missing = next((field for field in BASE_FIELDS if field != "comment" and not session["answers"].get(field)), None)
                if missing:
                    session.update(mode="question", field=missing, editing=False)
                    save()
                    operations.append(question(session))
                elif not who["username"] and not session["answers"].get("contact"):
                    show_review()
                elif recipient_id is None:
                    say("Передача заявок временно недоступна. Ваши ответы сохранены. Повторите отправку позже.")
                else:
                    application = {"id": session["id"], "telegram": who, "answers": session["answers"],
                                   "created_at": int(time.time()), "status": "pending"}
                    changes["application:" + session["id"]] = application
                    say("Заявка создана. Передаю её консультанту…")
                    parts = chunks(application_text(application))
                    url = "https://t.me/" + who["username"] if who["username"] else "tg://user?id=" + str(chat_id)
                    for index, part in enumerate(parts):
                        if len(parts) > 1:
                            part = f"Заявка №{session['id'].upper()} · часть {index + 1}/{len(parts)}\n\n" + part
                        op = message(None, part, {"inline_keyboard": [[{"text": "Открыть профиль клиента", "url": url}]]})
                        op["recipient"] = True
                        if index == len(parts) - 1:
                            op["application_id"] = session["id"]
                        operations.append(op)
                    say("Заявка №" + session["id"].upper() + " отправлена! Мария свяжется с вами по указанному контакту.\n"
                        "Для новой заявки нажмите /start.")
                    session.update(mode="complete")
                    save()
            else:
                show_review()
            return changes, operations

        field = FIELD_MAP[session["field"]]
        if action == "back":
            if session.get("editing"):
                show_review()
            else:
                session["field"] = FIELDS[max(0, list(FIELD_MAP).index(field.key) - 1)].key
                save()
                operations.append(question(session))
            return changes, operations
        if command:
            say("Можно ответить на текущий вопрос или отменить опрос командой /cancel.")
            operations.append(question(session))
            return changes, operations
        if action == "skip" and (field.optional or (field.key == "contact" and who["username"])):
            answer = ""
        elif action == "pick:name:use" and field.key == "name":
            answer = who["name"][:field.limit]
        elif action and action.startswith(f"pick:{field.key}:"):
            answer = dict(field.choices).get(action.rsplit(":", 1)[1])
            if answer is None:
                operations.append(question(session))
                return changes, operations
        elif action:
            operations.append(question(session))
            return changes, operations
        else:
            if not text:
                say("Пожалуйста, ответьте текстом или выберите кнопку.")
                operations.append(question(session))
                return changes, operations
            answer = text
        if not answer and not (field.optional or (field.key == "contact" and who["username"])):
            say("Пожалуйста, ответьте текстом или выберите кнопку.")
            operations.append(question(session))
            return changes, operations
        if len(answer) > field.limit:
            say(f"Пожалуйста, сократите ответ до {field.limit} символов.")
            operations.append(question(session))
            return changes, operations
        if field.key == "destination" and answer not in dict(field.choices).values():
            say("Выберите направление с помощью кнопок.")
            operations.append(question(session))
            return changes, operations
        if field.key == "contact" and answer and not (
                re.fullmatch(r"@[A-Za-z0-9_]{5,32}", answer) or
                re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", answer) or
                (re.fullmatch(r"\+?[0-9 ()-]+", answer) and len(re.sub(r"\D", "", answer)) >= 7)):
            say("Укажите телефон, email или Telegram-ник вида @username.")
            operations.append(question(session))
            return changes, operations
        session["answers"][field.key] = answer
        if session.get("editing") or field.key == "contact" or field.key == BASE_FIELDS[-1]:
            show_review()
        else:
            session["field"] = BASE_FIELDS[BASE_FIELDS.index(field.key) + 1]
            save()
            operations.append(question(session))
        return changes, operations


class Runtime:
    def __init__(self, settings, store, telegram, queue):
        self.settings, self.store, self.telegram, self.queue = settings, store, telegram, queue

    def process(self, update):
        update_id = update["update_id"]
        record = self.store.get("update:" + str(update_id))
        if record is None:
            changes, operations = Intake(self.settings, self.store).plan(update)
            record = self.store.commit(update_id, changes, operations)
        while not record["done"]:
            operation = record["operations"][record["cursor"]]
            arguments = copy.deepcopy(operation["arguments"])
            recipient_id = None
            if operation.get("recipient"):
                recipient_id = recipient(self.settings, self.store)
                if recipient_id is None:
                    raise ConfigurationError("Recipient has not started the bot")
                arguments["chat_id"] = recipient_id
            try:
                result = self.telegram.call(operation["method"], arguments)
            except TelegramError:
                if operation["method"] != "answerCallbackQuery":
                    raise
                # Telegram expires callback acknowledgements; this must not block delivery retries.
                result = {}
            record = self.store.advance(update_id, record, result, operation, recipient_id)


@lru_cache(maxsize=1)
def get_runtime():
    import boto3
    settings = Settings.from_environment()
    return Runtime(settings, DynamoStore(settings), Telegram(settings.token), boto3.client("sqs"))


def response(status, body):
    return {"statusCode": status, "headers": {"Content-Type": "application/json"}, "body": encode(body)}


def lambda_handler(event, context):
    """One function handles its public webhook and its private SQS FIFO event source."""
    if "Records" in event:
        records = event["Records"]
        for index, record in enumerate(records):
            try:
                if record.get("eventSource") != "aws:sqs":
                    raise ValueError("Unsupported event source")
                get_runtime().process(json.loads(record["body"]))
            except Exception as error:
                # No customer answers, tokens, request URLs or exception text are logged.
                LOG.error("update_processing_failed type=%s", type(error).__name__)
                return {"batchItemFailures": [{"itemIdentifier": item["messageId"]} for item in records[index:]]}
        return {"batchItemFailures": []}
    try:
        settings = Settings.from_environment()
    except ConfigurationError:
        return response(503, {"ok": False, "error": "not_configured"})
    method = event.get("requestContext", {}).get("http", {}).get("method", "")
    if method == "GET":
        return response(200, {"ok": True, "service": "visamb-telegram-bot"})
    if method != "POST":
        return response(405, {"ok": False})
    headers = {key.lower(): str(value) for key, value in (event.get("headers") or {}).items()}
    if not hmac.compare_digest(headers.get("x-telegram-bot-api-secret-token", "").encode(), settings.secret.encode()):
        return response(403, {"ok": False})
    try:
        body = event.get("body") or ""
        if len(body) > 90000:
            return response(413, {"ok": False})
        raw = base64.b64decode(body, validate=True) if event.get("isBase64Encoded") else body.encode()
        if len(raw) > 65536:
            return response(413, {"ok": False})
        update = json.loads(raw)
        if not isinstance(update, dict) or type(update.get("update_id")) is not int:
            raise ValueError("Invalid update")
        incoming = update.get("message") or update.get("callback_query", {}).get("message") or {}
        chat_id = incoming.get("chat", {}).get("id")
        if not chat_id:
            return response(200, {"ok": True})
    except (ValueError, TypeError, AttributeError, UnicodeError):
        return response(400, {"ok": False})
    try:
        runtime = get_runtime()
        runtime.queue.send_message(QueueUrl=settings.queue_url, MessageBody=encode(update),
                                   MessageGroupId=str(chat_id), MessageDeduplicationId=str(update["update_id"]))
    except Exception as error:
        LOG.error("webhook_enqueue_failed type=%s", type(error).__name__)
        return response(503, {"ok": False})
    return response(200, {"ok": True})
